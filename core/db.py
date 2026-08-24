"""SQLite storage.

The catalog is ~93k items; holding it as live Python objects costs hundreds of
MB, so everything lives on disk and the UI queries slices of it. Every catalog
table carries playlist_id with ON DELETE CASCADE so sources stay isolated and
removing one is atomic.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import unicodedata
from pathlib import Path

SCHEMA_VERSION = 1

def app_dir() -> Path:
    """Per-platform config directory."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / "IPTVPlayer"
    path.mkdir(parents=True, exist_ok=True)
    return path

def default_download_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Movies" / "IPTV Player"
    if os.name == "nt":
        return Path.home() / "Videos" / "IPTV Player"
    return Path.home() / "Videos" / "IPTV Player"

def fold(text: str) -> str:
    """Lowercase + strip accents, for cheap case-insensitive sort/search."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS playlists (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    type                TEXT NOT NULL DEFAULT 'xtream',   -- xtream | m3u_url | m3u_file
    server_url          TEXT,
    username            TEXT,
    epg_url             TEXT,
    file_path           TEXT,
    position            INTEGER NOT NULL DEFAULT 0,
    last_sync_at        INTEGER,
    sync_interval_hours INTEGER NOT NULL DEFAULT 24,
    sync_at_time        TEXT    NOT NULL DEFAULT '04:00',
    auto_sync           INTEGER NOT NULL DEFAULT 1,
    exp_date            INTEGER,
    max_connections     INTEGER,
    is_active           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    playlist_id   INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    category_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    group_name    TEXT NOT NULL,
    sub_name      TEXT NOT NULL,
    item_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, kind, category_id)
);

CREATE TABLE IF NOT EXISTS streams (
    playlist_id         INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,          -- live | movie | series
    stream_id           TEXT NOT NULL,
    name                TEXT NOT NULL,
    name_folded         TEXT NOT NULL,
    category_id         TEXT,
    num                 INTEGER,
    icon                TEXT,
    container_extension TEXT,
    rating              REAL,
    tmdb                TEXT,
    epg_channel_id      TEXT,
    added               INTEGER,
    is_adult            INTEGER NOT NULL DEFAULT 0,
    available           INTEGER NOT NULL DEFAULT 1,
    dup_rank            INTEGER NOT NULL DEFAULT 1,
    missing_since       INTEGER,
    missing_syncs       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, kind, stream_id)
);

CREATE TABLE IF NOT EXISTS series_episodes (
    playlist_id         INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    series_id           TEXT NOT NULL,
    season              INTEGER NOT NULL,
    episode_num         INTEGER NOT NULL,
    episode_id          TEXT NOT NULL,
    title               TEXT,
    container_extension TEXT,
    duration_secs       INTEGER,
    plot                TEXT,
    image               TEXT,
    fetched_at          INTEGER,
    PRIMARY KEY (playlist_id, series_id, season, episode_num, episode_id)
);

-- The provider's series metadata, from get_series_info's "info" object. Kept
-- apart from streams because it arrives per-series on demand, not in the bulk
-- catalog sync, and most series never get opened.
CREATE TABLE IF NOT EXISTS series_info (
    playlist_id  INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    series_id    TEXT NOT NULL,
    cover        TEXT,
    backdrop     TEXT,
    plot         TEXT,
    cast_list    TEXT,      -- not "cast": that is a SQL keyword
    director     TEXT,
    genre        TEXT,
    release_date TEXT,
    rating       REAL,
    run_time     INTEGER,
    trailer      TEXT,
    fetched_at   INTEGER,
    PRIMARY KEY (playlist_id, series_id)
);

CREATE TABLE IF NOT EXISTS favourites (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    stream_id   TEXT NOT NULL,
    added_at    INTEGER,
    PRIMARY KEY (playlist_id, kind, stream_id)
);

CREATE TABLE IF NOT EXISTS history (
    playlist_id   INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    stream_id     TEXT NOT NULL,
    episode_id    TEXT NOT NULL DEFAULT '',
    position_secs INTEGER NOT NULL DEFAULT 0,
    duration_secs INTEGER NOT NULL DEFAULT 0,
    watched_at    INTEGER,
    PRIMARY KEY (playlist_id, kind, stream_id, episode_id)
);

-- Categories and groups the user has pinned to the homepage. Arrives in an
-- existing database on the next open, the same way series_info did: the whole
-- schema is CREATE TABLE IF NOT EXISTS and runs every time.
CREATE TABLE IF NOT EXISTS home_rails (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,          -- live | movie | series
    node_type   TEXT NOT NULL,          -- group | category
    payload     TEXT NOT NULL,          -- group_name | category_id
    title       TEXT NOT NULL,          -- the heading, as it read when pinned
    pinned_at   INTEGER,
    PRIMARY KEY (playlist_id, kind, node_type, payload)
);

CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    stream_id     TEXT NOT NULL,
    PRIMARY KEY (collection_id, kind, stream_id)
);

CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    stream_id   TEXT NOT NULL,
    episode_id  TEXT,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    dest_path   TEXT NOT NULL,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    done_bytes  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued|active|paused|done|failed
    error       TEXT,
    added_at    INTEGER,
    finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Indexes live apart from the table DDL because they may reference columns that
# a migration adds: they must be created *after* _migrate() has run, or opening
# an older database fails with "no such column".
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_streams_cat
    ON streams(playlist_id, kind, category_id, name_folded);
CREATE INDEX IF NOT EXISTS idx_streams_name
    ON streams(playlist_id, kind, name_folded);
CREATE INDEX IF NOT EXISTS idx_streams_added
    ON streams(playlist_id, kind, added DESC);

-- Serves the ALL/RECENT views: dup_rank is computed once at sync time so the
-- UI never runs a ROW_NUMBER() window over 55k rows (two temp B-trees, ~250 ms
-- every visit).
CREATE INDEX IF NOT EXISTS idx_streams_dedup
    ON streams(playlist_id, kind, dup_rank, name_folded);
CREATE INDEX IF NOT EXISTS idx_streams_dedup_num
    ON streams(playlist_id, kind, dup_rank, num);

CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status, id);
"""


class Database:
    """Thread-aware SQLite wrapper.

    sqlite3 connections cannot be shared across threads, and the app has a GUI
    thread plus sync/download workers, so each thread gets its own connection
    to the same WAL database.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else app_dir() / "catalog.sqlite"
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()

    # -- connection handling ------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA temp_store=MEMORY")
            # SQLite defaults to a 2 MB page cache, which is far too small for a
            # ~30 MB catalog: the first query over a big category was measured
            # at 1414 ms cold and 178 ms with these settings. Applied per
            # connection because sync/download workers open their own.
            conn.execute("PRAGMA cache_size=-65536")     # 64 MB
            conn.execute("PRAGMA mmap_size=268435456")   # 256 MB
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    MIGRATIONS = (
        ("streams", "dup_rank", "INTEGER NOT NULL DEFAULT 1"),
        # series_episodes exists in every database already, so the new column
        # cannot ride in on CREATE TABLE IF NOT EXISTS the way series_info does.
        ("series_episodes", "image", "TEXT"),
    )

    def _migrate(self, conn):
        """Add columns introduced after a user's database was created."""
        for table, column, decl in self.MIGRATIONS:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _init_schema(self):
        conn = self.conn
        with self._write_lock:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.executescript(INDEXES)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()

    # -- helpers ------------------------------------------------------------

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=(), default=None):
        row = self.one(sql, params)
        return row[0] if row is not None and row[0] is not None else default

    def execute(self, sql: str, params=()):
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, seq):
        with self._write_lock:
            cur = self.conn.executemany(sql, seq)
            self.conn.commit()
            return cur

    @property
    def instance_id(self) -> str:
        """Stable id for this database file.

        Keychain entries are namespaced with it so two databases (a test
        fixture, a second profile) can both hold a playlist numbered 1 without
        overwriting each other's stored credentials.
        """
        cached = getattr(self._local, "instance_id", None)
        if cached:
            return cached
        row = self.one("SELECT value FROM meta WHERE key='instance_id'")
        if row is None:
            import uuid

            value = uuid.uuid4().hex[:12]
            self.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('instance_id', ?)", (value,)
            )
            row = self.one("SELECT value FROM meta WHERE key='instance_id'")
        value = row["value"]
        self._local.instance_id = value
        return value

    # -- settings -----------------------------------------------------------

    def get_setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value):
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_bool(self, key: str, default: bool) -> bool:
        raw = self.get_setting(key)
        if raw is None:
            return default
        return raw in ("1", "true", "True")

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get_setting(key, default))
        except (TypeError, ValueError):
            return default
