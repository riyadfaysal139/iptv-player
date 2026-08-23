"""Resumable download queue.

Measured against a real portal: ranged GETs return 206 at arbitrary offsets, so
transfers resume across restarts; a single movie was 1.36 GB, so disk checks
are necessary rather than decorative; and HEAD returns nothing useful, so the
total size comes from a ranged GET's Content-Range.

The account allows one connection, so exactly one transfer runs at a time and
downloads pause themselves while something is playing.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .db import Database, default_download_dir
from .http import make_session

CHUNK = 1024 * 1024
SAFETY_MARGIN = 200 * 1024 * 1024  # keep some headroom on the target volume

STATUS_QUEUED = "queued"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, fallback: str = "download") -> str:
    cleaned = _ILLEGAL.sub("_", (text or "").strip()).strip(". ")
    return (cleaned or fallback)[:150]


def destination_for(root: Path, kind: str, title: str, show: str = "",
                    season: int = 0, episode: int = 0, ext: str = "mp4") -> Path:
    ext = (ext or "mp4").lstrip(".")
    if kind == "series" and show:
        return (root / "Series" / safe_name(show) / f"Season {season:02d}" /
                f"S{season:02d}E{episode:02d} - {safe_name(title)}.{ext}")
    if kind == "live":
        stamp = time.strftime("%Y-%m-%d %H-%M")
        return root / "Recordings" / f"{safe_name(title)} {stamp}.{ext}"
    return root / "Movies" / f"{safe_name(title)}.{ext}"


@dataclass
class Job:
    id: int
    playlist_id: int
    kind: str
    stream_id: str
    episode_id: str
    title: str
    url: str
    dest_path: str
    total_bytes: int
    done_bytes: int
    status: str
    error: str | None


def _row_to_job(row) -> Job:
    return Job(
        id=row["id"], playlist_id=row["playlist_id"], kind=row["kind"],
        stream_id=row["stream_id"], episode_id=row["episode_id"] or "",
        title=row["title"], url=row["url"], dest_path=row["dest_path"],
        total_bytes=row["total_bytes"], done_bytes=row["done_bytes"],
        status=row["status"], error=row["error"],
    )


class DownloadManager:
    """Single-slot download worker.

    Callbacks (on_progress/on_finished) fire on the worker thread; the UI layer
    marshals them onto the GUI thread with queued signals.
    """

    def __init__(self, db: Database, on_progress=None, on_finished=None):
        self.db = db
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.session = make_session()

        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._playback_active = False
        self._allow_while_playing = db.get_bool("download_while_playing", False)
        self._current_id = None
        self._lock = threading.RLock()
        self._recording_until = {}

    # ------------------------------------------------------------ lifecycle

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Anything left 'active' by a crash is really queued.
        self.db.execute(
            "UPDATE downloads SET status=? WHERE status=?", (STATUS_QUEUED, STATUS_ACTIVE)
        )
        self._thread = threading.Thread(target=self._run, name="downloads", daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    # -------------------------------------------------------------- queueing

    def root_dir(self) -> Path:
        configured = self.db.get_setting("download_dir")
        return Path(configured) if configured else default_download_dir()

    def enqueue(self, playlist_id: int, kind: str, stream_id, title: str, url: str,
                episode_id: str = "", show: str = "", season: int = 0,
                episode: int = 0, ext: str = "mp4") -> int:
        dest = destination_for(self.root_dir(), kind, title, show, season, episode, ext)
        existing = self.db.one(
            "SELECT id, status FROM downloads WHERE playlist_id=? AND kind=? "
            "AND stream_id=? AND COALESCE(episode_id,'')=?",
            (playlist_id, kind, str(stream_id), episode_id or ""),
        )
        if existing:
            if existing["status"] in (STATUS_FAILED, STATUS_PAUSED):
                self.db.execute(
                    "UPDATE downloads SET status=?, error=NULL WHERE id=?",
                    (STATUS_QUEUED, existing["id"]),
                )
                self._wake.set()
            return existing["id"]
        cur = self.db.execute(
            "INSERT INTO downloads(playlist_id, kind, stream_id, episode_id, title, url,"
            " dest_path, status, added_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (playlist_id, kind, str(stream_id), episode_id or "", title, url,
             str(dest), STATUS_QUEUED, int(time.time())),
        )
        self._wake.set()
        return cur.lastrowid

    def record_live(self, playlist_id: int, stream_id, title: str, url: str,
                    duration_secs: int, ext: str = "ts") -> int:
        """A live stream never ends, so recording is time-bounded."""
        job_id = self.enqueue(playlist_id, "live", stream_id, title, url, ext=ext)
        self._recording_until[job_id] = duration_secs
        return job_id

    def jobs(self, status: str | None = None) -> list[Job]:
        if status:
            rows = self.db.query(
                "SELECT * FROM downloads WHERE status=? ORDER BY id", (status,)
            )
        else:
            rows = self.db.query("SELECT * FROM downloads ORDER BY id DESC")
        return [_row_to_job(r) for r in rows]

    def completed(self, playlist_id: int) -> list[Job]:
        rows = self.db.query(
            "SELECT * FROM downloads WHERE playlist_id=? AND status=? ORDER BY finished_at DESC",
            (playlist_id, STATUS_DONE),
        )
        return [_row_to_job(r) for r in rows]

    def pause(self, job_id: int):
        self.db.execute(
            "UPDATE downloads SET status=? WHERE id=? AND status IN (?,?)",
            (STATUS_PAUSED, job_id, STATUS_QUEUED, STATUS_ACTIVE),
        )

    def resume(self, job_id: int):
        self.db.execute(
            "UPDATE downloads SET status=?, error=NULL WHERE id=?", (STATUS_QUEUED, job_id)
        )
        self._wake.set()

    def cancel(self, job_id: int, delete_file: bool = True):
        row = self.db.one("SELECT dest_path FROM downloads WHERE id=?", (job_id,))
        self.db.execute("DELETE FROM downloads WHERE id=?", (job_id,))
        if delete_file and row:
            for path in (Path(row["dest_path"]), Path(row["dest_path"] + ".part")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    # ------------------------------------------------------- playback gating

    def set_playback_active(self, active: bool):
        """Downloads yield to playback: the account allows one connection."""
        self._playback_active = active
        if not active:
            self._wake.set()

    def set_allow_while_playing(self, allow: bool):
        self._allow_while_playing = allow
        self.db.set_setting("download_while_playing", "1" if allow else "0")
        self._wake.set()

    @property
    def blocked_by_playback(self) -> bool:
        return self._playback_active and not self._allow_while_playing

    # ------------------------------------------------------------- the loop

    def _run(self):
        while not self._stop.is_set():
            if self.blocked_by_playback:
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue
            row = self.db.one(
                "SELECT * FROM downloads WHERE status=? ORDER BY id LIMIT 1", (STATUS_QUEUED,)
            )
            if row is None:
                self._wake.wait(timeout=5.0)
                self._wake.clear()
                continue
            self._transfer(_row_to_job(row))

    def _transfer(self, job: Job):
        # Imported here rather than at module scope: `requests` costs ~170 ms to
        # import and startup does not need it (see core/http.py).
        import requests

        with self._lock:
            self._current_id = job.id
        dest = Path(job.dest_path)
        part = Path(str(dest) + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._fail(job, f"Cannot create folder: {exc}")
            return

        self.db.execute("UPDATE downloads SET status=? WHERE id=?", (STATUS_ACTIVE, job.id))

        offset = part.stat().st_size if part.exists() else 0
        total = job.total_bytes
        is_live = job.kind == "live"

        try:
            if not total and not is_live:
                total = self._remote_size(job.url)
                if total:
                    self.db.execute(
                        "UPDATE downloads SET total_bytes=? WHERE id=?", (total, job.id)
                    )
            if total and not self._has_space(dest.parent, total - offset):
                self._fail(job, "Not enough free disk space for this download.")
                return

            headers = {"Range": f"bytes={offset}-"} if offset else {}
            resp = self.session.get(job.url, headers=headers, stream=True, timeout=(15, 120))
            if resp.status_code not in (200, 206):
                resp.close()
                self._fail(job, f"Server returned HTTP {resp.status_code}")
                return
            if offset and resp.status_code == 200:
                offset = 0  # server ignored the range; start over

            deadline = None
            if is_live:
                seconds = self._recording_until.get(job.id)
                if seconds:
                    deadline = time.time() + seconds

            written = offset
            last_report = 0.0
            mode = "ab" if offset else "wb"
            with open(part, mode) as handle:
                for chunk in resp.iter_content(CHUNK):
                    if self._stop.is_set():
                        # Interrupted by app shutdown, not by the user: leave it
                        # queued so the next launch picks it up and resumes.
                        # A user-initiated pause is handled separately below and
                        # is meant to persist.
                        resp.close()
                        handle.flush()
                        os.fsync(handle.fileno())
                        self.db.execute(
                            "UPDATE downloads SET status=?, done_bytes=? WHERE id=?",
                            (STATUS_QUEUED, written, job.id),
                        )
                        return
                    if self.blocked_by_playback:
                        resp.close()
                        handle.flush()
                        os.fsync(handle.fileno())
                        self.db.execute(
                            "UPDATE downloads SET status=?, done_bytes=? WHERE id=?",
                            (STATUS_QUEUED, written, job.id),
                        )
                        self._report(job, written, total, "Paused — playback is using your single connection")
                        return
                    if self._status_of(job.id) == STATUS_PAUSED:
                        resp.close()
                        self.db.execute(
                            "UPDATE downloads SET done_bytes=? WHERE id=?", (written, job.id)
                        )
                        return
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    now = time.time()
                    if now - last_report > 0.5:
                        handle.flush()
                        os.fsync(handle.fileno())
                        self.db.execute(
                            "UPDATE downloads SET done_bytes=? WHERE id=?", (written, job.id)
                        )
                        self._report(job, written, total)
                        last_report = now
                    if deadline and now >= deadline:
                        break
            resp.close()

            # A truncated file is worse than a failed one: verify before naming.
            if total and not is_live and written < total:
                self.db.execute(
                    "UPDATE downloads SET status=?, done_bytes=?, error=? WHERE id=?",
                    (STATUS_FAILED, written,
                     f"Incomplete: got {written:,} of {total:,} bytes", job.id),
                )
                self._finish(job, False)
                return

            part.replace(dest)
            self.db.execute(
                "UPDATE downloads SET status=?, done_bytes=?, total_bytes=?, finished_at=?,"
                " error=NULL WHERE id=?",
                (STATUS_DONE, written, total or written, int(time.time()), job.id),
            )
            self._finish(job, True)
        except requests.RequestException as exc:
            self._fail(job, f"Network error: {exc}")
        except OSError as exc:
            self._fail(job, f"Disk error: {exc}")
        except Exception as exc:
            self._fail(job, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._current_id = None

    # ------------------------------------------------------------- helpers

    def _remote_size(self, url: str) -> int:
        resp = self.session.get(
            url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(15, 60)
        )
        try:
            match = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range") or "")
            if match:
                return int(match.group(1))
            if resp.status_code == 200 and resp.headers.get("Content-Length"):
                return int(resp.headers["Content-Length"])
            return 0
        finally:
            resp.close()

    @staticmethod
    def _has_space(directory: Path, needed: int) -> bool:
        try:
            return shutil.disk_usage(directory).free > needed + SAFETY_MARGIN
        except OSError:
            return True

    def _status_of(self, job_id: int) -> str:
        row = self.db.one("SELECT status FROM downloads WHERE id=?", (job_id,))
        return row["status"] if row else STATUS_FAILED

    def _fail(self, job: Job, message: str):
        self.db.execute(
            "UPDATE downloads SET status=?, error=? WHERE id=?",
            (STATUS_FAILED, message, job.id),
        )
        self._finish(job, False, message)

    def _report(self, job: Job, done: int, total: int, note: str = ""):
        if self.on_progress:
            try:
                self.on_progress(job.id, done, total, note)
            except Exception:
                pass

    def _finish(self, job: Job, ok: bool, message: str = ""):
        if self.on_finished:
            try:
                self.on_finished(job.id, ok, message)
            except Exception:
                pass


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"
