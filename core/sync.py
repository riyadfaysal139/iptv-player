"""Catalog synchronisation.

Two properties matter more than speed here:

1. An interrupted sync must never damage the catalog. Rows are written into
   staging tables and swapped in one transaction, so a crash mid-sync leaves
   the previous catalog intact and complete.

2. A refresh must never destroy user data. Providers shuffle their catalogs
   constantly; items that vanish are marked unavailable rather than deleted,
   so favourites survive a provider hiccup and light back up if the item
   returns.

Catalog sync does not disturb playback (API calls succeed while a stream is
open), so it can run whenever it is due.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import m3u
from .api import ApiError, XtreamClient
from .classify import UNCATEGORIZED, classify
from .db import Database, fold
from .playlists import TYPE_M3U_FILE, TYPE_M3U_URL, Playlist, PlaylistStore

KINDS = ("live", "movie", "series")

# An item must be missing this many consecutive syncs before removal is offered.
MISSING_SYNCS_BEFORE_CLEANUP = 3


@dataclass
class SyncResult:
    playlist_id: int
    added: dict = field(default_factory=dict)
    removed: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    categories: int = 0
    error: str | None = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled

    def summary(self) -> str:
        if self.error:
            return f"Update failed: {self.error}"
        if self.cancelled:
            return "Update cancelled"
        bits = []
        labels = {"live": "channels", "movie": "movies", "series": "series"}
        for kind in KINDS:
            n = self.added.get(kind, 0)
            if n:
                bits.append(f"{n:,} new {labels[kind]}")
        gone = sum(self.removed.values())
        if gone:
            bits.append(f"{gone:,} removed")
        if not bits:
            return "Catalog is up to date"
        return " · ".join(bits)


class Cancelled(Exception):
    pass


def _with_retry(fn, *args, attempts: int = 3, base_delay: float = 1.5):
    """Retry a fetch through transient portal hiccups.

    The portal intermittently answers a valid request with a non-JSON body.
    Reporting "Update failed" for that is noisy when a retry a second later
    succeeds, so back off and try again before giving up.
    """
    last = None
    for attempt in range(attempts):
        try:
            return fn(*args)
        except (ApiError, ValueError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last


class Syncer:
    """Runs one playlist refresh. Safe to call from a worker thread."""

    def __init__(self, db: Database, progress=None, should_cancel=None):
        self.db = db
        self._progress = progress
        self._should_cancel = should_cancel

    def emit(self, message: str, pct: int = -1):
        if self._progress:
            self._progress(message, pct)

    def check_cancel(self):
        if self._should_cancel and self._should_cancel():
            raise Cancelled()

    # ----------------------------------------------------------------- run

    def run(self, playlist: Playlist) -> SyncResult:
        result = SyncResult(playlist_id=playlist.id)
        try:
            if playlist.type in (TYPE_M3U_URL, TYPE_M3U_FILE):
                categories, streams = self._collect_m3u(playlist)
                account = None
            else:
                categories, streams, account = self._collect_xtream(playlist)

            self.check_cancel()
            self._commit(playlist.id, categories, streams, result)

            store = PlaylistStore(self.db)
            store.mark_synced(playlist.id, account)
        except Cancelled:
            result.cancelled = True
        except (ApiError, OSError, ValueError) as exc:
            result.error = str(exc)
        except Exception as exc:  # keep a background task from killing the app
            result.error = f"{type(exc).__name__}: {exc}"
        return result

    # ------------------------------------------------------------- collect

    def _collect_xtream(self, playlist: Playlist):
        from concurrent.futures import ThreadPoolExecutor

        client = playlist.client()
        self.emit("Checking account…", 2)
        account = client.account()

        # The three payloads total ~38 MB and were fetched one after another;
        # in parallel this measured 9.9 s -> 7.0 s. Each thread gets its own
        # client because a requests.Session is not thread-safe.
        def grab(kind):
            worker = playlist.client()
            return kind, worker.categories(kind), worker.streams(kind)

        self.emit("Fetching catalog…", 10)
        results = []
        with ThreadPoolExecutor(3) as pool:
            futures = [pool.submit(_with_retry, grab, kind) for kind in KINDS]
            for done, future in enumerate(futures, start=1):
                self.check_cancel()
                results.append(future.result())
                self.emit(f"Fetched {done}/3 lists…", 10 + done * 25)

        categories, streams = [], []
        for kind, cats, items in results:
            for cat in cats:
                name = str(cat.get("category_name") or "").strip()
                cid = str(cat.get("category_id"))
                group, sub = classify(kind, name)
                categories.append((playlist.id, kind, cid, name, group, sub, 0))
            for item in items:
                row = self._xtream_row(playlist.id, kind, item)
                if row:
                    streams.append(row)
        self.emit(f"{len(streams):,} items…", 92)
        return categories, streams, account

    @staticmethod
    def _xtream_row(playlist_id: int, kind: str, item: dict):
        if kind == "series":
            stream_id = item.get("series_id")
            icon = item.get("cover")
            ext = None
        else:
            stream_id = item.get("stream_id")
            icon = item.get("stream_icon")
            ext = item.get("container_extension")
        if stream_id is None:
            return None
        name = str(item.get("name") or "").strip()
        try:
            rating = float(item.get("rating")) if item.get("rating") not in (None, "") else None
        except (TypeError, ValueError):
            rating = None
        try:
            added = int(item.get("added")) if item.get("added") else None
        except (TypeError, ValueError):
            added = None
        if added is None:
            try:
                added = int(item.get("last_modified")) if item.get("last_modified") else None
            except (TypeError, ValueError):
                added = None
        return (
            playlist_id,
            kind,
            str(stream_id),
            name,
            fold(name),
            str(item.get("category_id")) if item.get("category_id") is not None else None,
            _as_int(item.get("num")),
            icon or "",
            ext or "",
            rating,
            str(item.get("tmdb") or ""),
            str(item.get("epg_channel_id") or ""),
            added,
            1 if str(item.get("is_adult", "0")) == "1" else 0,
        )

    def _collect_m3u(self, playlist: Playlist):
        """Parse an M3U without ever holding the file in memory."""
        self.emit("Reading playlist…", 5)
        if playlist.type == TYPE_M3U_FILE:
            lines = m3u.iter_lines_from_file(playlist.file_path)
        else:
            lines = m3u.iter_lines_from_url(playlist.server_url or playlist.file_path)

        seen_categories: dict[tuple, str] = {}
        streams = []
        seen_ids = set()
        for index, entry in enumerate(m3u.parse(lines)):
            if index % 5000 == 0:
                self.check_cancel()
                self.emit(f"Parsed {index:,} entries…", min(90, 5 + index // 2000))
            kind = entry["kind"]
            group_name = entry["group"] or UNCATEGORIZED
            key = (kind, group_name)
            if key not in seen_categories:
                seen_categories[key] = str(len(seen_categories) + 1)
            cid = seen_categories[key]

            stream_id = entry["stream_id"]
            dedupe = (kind, stream_id)
            if dedupe in seen_ids:
                continue
            seen_ids.add(dedupe)

            name = entry["name"]
            streams.append(
                (
                    playlist.id, kind, stream_id, name, fold(name), cid, None,
                    entry["logo"], entry["container_extension"], None, "",
                    entry["tvg_id"], None, 0,
                )
            )

        categories = []
        for (kind, group_name), cid in seen_categories.items():
            group, sub = classify(kind, group_name)
            categories.append((playlist.id, kind, cid, group_name, group, sub, 0))
        return categories, streams

    # -------------------------------------------------------------- commit

    def _commit(self, playlist_id: int, categories, streams, result: SyncResult):
        """Swap the new catalog in atomically, preserving user data."""
        self.emit("Saving…", 96)
        db = self.db
        now = int(time.time())

        with db._write_lock:
            conn = db.conn
            try:
                conn.execute("BEGIN IMMEDIATE")

                conn.execute("DROP TABLE IF EXISTS stage_streams")
                conn.execute("DROP TABLE IF EXISTS stage_categories")
                conn.execute(
                    "CREATE TEMP TABLE stage_streams("
                    "playlist_id INT, kind TEXT, stream_id TEXT, name TEXT, "
                    "name_folded TEXT, category_id TEXT, num INT, icon TEXT, "
                    "container_extension TEXT, rating REAL, tmdb TEXT, "
                    "epg_channel_id TEXT, added INT, is_adult INT)"
                )
                conn.execute(
                    "CREATE TEMP TABLE stage_categories("
                    "playlist_id INT, kind TEXT, category_id TEXT, name TEXT, "
                    "group_name TEXT, sub_name TEXT, item_count INT)"
                )
                conn.executemany(
                    "INSERT INTO stage_streams VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    streams,
                )
                conn.executemany(
                    "INSERT INTO stage_categories VALUES(?,?,?,?,?,?,?)", categories
                )
                # Index the staging table before anything joins against it.
                # Without this the "retire vanished items" UPDATE below runs a
                # correlated full scan of ~93k staged rows for each of ~93k
                # existing rows: measured 256 s here versus 0.04 s indexed.
                # (It only shows up once the catalog is populated, which is why
                # a first-run sync looked fast.)
                conn.execute(
                    "CREATE INDEX idx_stage_streams ON stage_streams(kind, stream_id)"
                )

                # What is genuinely new, for RECENTLY ADDED and the summary.
                for kind in KINDS:
                    added = conn.execute(
                        "SELECT COUNT(*) FROM stage_streams s WHERE s.kind=? AND NOT EXISTS("
                        " SELECT 1 FROM streams t WHERE t.playlist_id=? AND t.kind=s.kind"
                        " AND t.stream_id=s.stream_id)",
                        (kind, playlist_id),
                    ).fetchone()[0]
                    gone = conn.execute(
                        "SELECT COUNT(*) FROM streams t WHERE t.playlist_id=? AND t.kind=?"
                        " AND t.available=1 AND NOT EXISTS("
                        " SELECT 1 FROM stage_streams s WHERE s.kind=t.kind"
                        " AND s.stream_id=t.stream_id)",
                        (playlist_id, kind),
                    ).fetchone()[0]
                    total = conn.execute(
                        "SELECT COUNT(*) FROM stage_streams WHERE kind=?", (kind,)
                    ).fetchone()[0]
                    result.added[kind] = added
                    result.removed[kind] = gone
                    result.totals[kind] = total

                # Retire vanished items instead of deleting them, so a
                # favourite does not disappear because the provider blinked.
                conn.execute(
                    "UPDATE streams SET available=0,"
                    " missing_since=COALESCE(missing_since, ?),"
                    " missing_syncs=missing_syncs+1 "
                    "WHERE playlist_id=? AND NOT EXISTS("
                    " SELECT 1 FROM stage_streams s WHERE s.kind=streams.kind"
                    " AND s.stream_id=streams.stream_id)",
                    (now, playlist_id),
                )

                conn.execute(
                    "INSERT INTO streams(playlist_id, kind, stream_id, name, name_folded,"
                    " category_id, num, icon, container_extension, rating, tmdb,"
                    " epg_channel_id, added, is_adult, available, missing_since, missing_syncs)"
                    " SELECT playlist_id, kind, stream_id, name, name_folded, category_id,"
                    " num, icon, container_extension, rating, tmdb, epg_channel_id, added,"
                    " is_adult, 1, NULL, 0 FROM stage_streams WHERE true"
                    " ON CONFLICT(playlist_id, kind, stream_id) DO UPDATE SET"
                    " name=excluded.name, name_folded=excluded.name_folded,"
                    " category_id=excluded.category_id, num=excluded.num,"
                    " icon=excluded.icon,"
                    " container_extension=excluded.container_extension,"
                    " rating=excluded.rating, tmdb=excluded.tmdb,"
                    " epg_channel_id=excluded.epg_channel_id,"
                    " added=COALESCE(streams.added, excluded.added),"
                    " is_adult=excluded.is_adult, available=1,"
                    " missing_since=NULL, missing_syncs=0"
                )

                conn.execute("DELETE FROM categories WHERE playlist_id=?", (playlist_id,))
                conn.execute(
                    "INSERT INTO categories(playlist_id, kind, category_id, name,"
                    " group_name, sub_name, item_count)"
                    " SELECT playlist_id, kind, category_id, name, group_name, sub_name, 0"
                    " FROM stage_categories"
                )
                conn.execute(
                    "UPDATE categories SET item_count=("
                    " SELECT COUNT(*) FROM streams s WHERE s.playlist_id=categories.playlist_id"
                    " AND s.kind=categories.kind AND s.category_id=categories.category_id"
                    " AND s.available=1) WHERE playlist_id=?",
                    (playlist_id,),
                )

                # Materialise the duplicate rank. The provider lists the same
                # title once per category it belongs to (55,724 movie rows cover
                # 33,916 distinct titles), so aggregate views show one
                # representative. Computing that with a window function at query
                # time cost ~250 ms per visit; doing it once here makes the view
                # an ordered index scan.
                #
                # Tie-breaks must match what the UI used to do: live keeps the
                # provider's channel numbering (lowest `num`), VOD keeps the
                # best-rated copy.
                # Materialise into a temp table first, then join. A correlated
                # subquery here re-ran the window function per row and took
                # 9 minutes on 92k rows; UPDATE..FROM against an indexed temp
                # table does the same work in about a second.
                conn.execute("DROP TABLE IF EXISTS rank_tmp")
                conn.execute(
                    "CREATE TEMP TABLE rank_tmp AS"
                    " SELECT kind, stream_id, ROW_NUMBER() OVER ("
                    "   PARTITION BY kind, name_folded"
                    "   ORDER BY CASE WHEN kind='live' THEN num END,"
                    "            (rating IS NULL), rating DESC, stream_id) AS r"
                    " FROM streams WHERE playlist_id=? AND available=1 AND name<>''",
                    (playlist_id,),
                )
                conn.execute(
                    "CREATE INDEX idx_rank_tmp ON rank_tmp(kind, stream_id)"
                )
                conn.execute(
                    "UPDATE streams SET dup_rank=1 WHERE playlist_id=?", (playlist_id,)
                )
                conn.execute(
                    "UPDATE streams SET dup_rank=rank_tmp.r FROM rank_tmp"
                    " WHERE streams.playlist_id=? AND streams.kind=rank_tmp.kind"
                    " AND streams.stream_id=rank_tmp.stream_id",
                    (playlist_id,),
                )
                conn.execute("DROP TABLE IF EXISTS rank_tmp")
                result.categories = conn.execute(
                    "SELECT COUNT(*) FROM categories WHERE playlist_id=?", (playlist_id,)
                ).fetchone()[0]

                conn.execute("DROP TABLE IF EXISTS stage_streams")
                conn.execute("DROP TABLE IF EXISTS stage_categories")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.emit("Done", 100)


def fetch_episodes(db: Database, playlist: Playlist, series_id) -> int:
    """Lazily load one series' episodes (10.7k series is far too many to prefetch)."""
    client = playlist.client()
    info = client.series_info(series_id)
    episodes = info.get("episodes") or {}
    rows = []
    now = int(time.time())
    for season, items in episodes.items():
        try:
            season_no = int(season)
        except (TypeError, ValueError):
            season_no = 0
        for ep in items or []:
            meta = ep.get("info") or {}
            try:
                episode_num = int(ep.get("episode_num") or 0)
            except (TypeError, ValueError):
                episode_num = 0
            rows.append(
                (
                    playlist.id, str(series_id), season_no, episode_num,
                    str(ep.get("id")), ep.get("title") or "",
                    ep.get("container_extension") or "mp4",
                    _as_int(meta.get("duration_secs")) or 0,
                    meta.get("plot") or "", now,
                )
            )
    if rows:
        db.execute(
            "DELETE FROM series_episodes WHERE playlist_id=? AND series_id=?",
            (playlist.id, str(series_id)),
        )
        db.executemany(
            "INSERT OR REPLACE INTO series_episodes(playlist_id, series_id, season,"
            " episode_num, episode_id, title, container_extension, duration_secs,"
            " plot, fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
