"""Play-through disk cache: a local HTTP proxy that files the stream as it goes.

libVLC is pointed at http://127.0.0.1:<port>/<token>/stream.<ext> instead of
the provider. This server fetches the real URL with one upstream connection,
writes every byte to a cache file, and serves the player from that file - so
playback starts at once (nothing waits for a download) while the whole film
or episode accumulates on disk behind it.

What that buys:

* A dropped upstream socket is invisible to the player. The fetcher reconnects
  with a Range header and carries on; the player's read simply waits.
* Seeking backwards, or replaying, reads from disk and costs no bandwidth.
* The account's single connection is respected. libVLC probes an MP4/MKV with
  several ranged requests of its own (tail for the index, then the start);
  here they all land on the proxy, and only the fetcher talks to the portal.
* A partly cached file resumes filling next time it is played, and a fully
  cached one plays with no connection at all.

The cache lives under the app's config directory and is trimmed oldest-first
to a size limit before each new stream is opened. Live streams never come
through here - they have no length and no end.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .http import make_session

CHUNK = 256 * 1024
# A cached island shorter than this is read through (and rewritten) rather
# than skipped with a fresh connection: reconnecting costs more than the bytes.
SKIP_THRESHOLD = 8 * 1024 * 1024
# How far ahead of the fetcher a reader may ask for before the fetcher jumps.
NEAR_AHEAD = 4 * 1024 * 1024
META_FLUSH_BYTES = 32 * 1024 * 1024
# Upstream read timeout: a peer that vanishes without a FIN only shows up as
# silence, and this is how long the fetcher gives it before reconnecting.
UPSTREAM_TIMEOUT = (15, 20)
TOTAL_WAIT_S = 25          # for the first upstream response
# A reader waits longer than one upstream timeout plus a retry, so a silent
# drop is repaired underneath it rather than surfacing as a short response.
READ_WAIT_S = 75
DEFAULT_LIMIT_GB = 10

CONTENT_TYPES = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mkv": "video/x-matroska",
    "avi": "video/x-msvideo", "ts": "video/mp2t", "mov": "video/quicktime",
    "webm": "video/webm", "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
}


def merge_intervals(intervals):
    """Sorted, non-overlapping [start, end) pairs."""
    out = []
    for start, end in sorted((int(a), int(b)) for a, b in intervals if b > a):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out


def available_end(intervals, pos: int):
    """End of the cached run containing `pos`, or None if `pos` is not cached."""
    for start, end in intervals:
        if start <= pos < end:
            return end
        if start > pos:
            break
    return None


def next_gap(intervals, pos: int, total: int):
    """Start of the first uncached byte at or after `pos`, or None if none."""
    while pos < total:
        end = available_end(intervals, pos)
        if end is None:
            return pos
        pos = end
    return None


class CachedStream:
    """One upstream URL, one cache file, one fetcher thread."""

    def __init__(self, url: str, path: Path, session_factory=make_session):
        self.url = url
        self.path = path
        self.meta_path = path.with_suffix(path.suffix + ".json")
        self._session_factory = session_factory
        self.total = None
        self.intervals = []
        self.error = None
        self._lock = threading.Condition()
        self._want = None            # a reader's position the fetcher should serve
        self._stop = threading.Event()
        self._thread = None
        self._since_flush = 0
        self._load_meta()

    # ------------------------------------------------------------ metadata

    def _load_meta(self):
        try:
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if meta.get("url") == self.url and self.path.exists():
                self.total = int(meta["total"])
                self.intervals = merge_intervals(meta.get("intervals", []))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_meta(self):
        try:
            self.meta_path.write_text(json.dumps({
                "url": self.url, "total": self.total, "intervals": self.intervals,
            }), encoding="utf-8")
        except OSError:
            pass

    @property
    def complete(self) -> bool:
        return (self.total is not None and len(self.intervals) == 1
                and self.intervals[0] == [0, self.total])

    def cached_bytes(self) -> int:
        return sum(end - start for start, end in self.intervals)

    # ------------------------------------------------------------- control

    def start(self):
        if self._thread is not None or self.complete:
            return
        self._thread = threading.Thread(target=self._run, name="stream-cache", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            self._lock.notify_all()
        if self._thread is not None:
            self._thread.join(5)
        self._save_meta()

    # -------------------------------------------------------------- readers

    def wait_total(self, timeout: float):
        with self._lock:
            deadline = time.monotonic() + timeout
            while self.total is None and self.error is None and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lock.wait(remaining)
            return self.total

    def read(self, pos: int, size: int, timeout: float) -> bytes:
        """Up to `size` bytes at `pos`, waiting for them to be fetched.

        Returns b"" only at the end of the file, on stop, or once the wait
        runs out - which a caller treats as a dead stream.
        """
        with self._lock:
            deadline = time.monotonic() + timeout
            while True:
                if self.total is not None and pos >= self.total:
                    return b""
                end = available_end(self.intervals, pos)
                if end is not None:
                    break
                if self._stop.is_set():
                    return b""
                self._want = pos
                self._lock.notify_all()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._lock.wait(min(remaining, 1.0))
        length = min(size, end - pos)
        with open(self.path, "rb") as handle:
            handle.seek(pos)
            return handle.read(length)

    # -------------------------------------------------------------- fetcher

    def _run(self):
        session = self._session_factory()
        backoff = 1.0
        pos = 0
        while not self._stop.is_set():
            with self._lock:
                pos = self._pick_start(pos)
                if pos is None:
                    self._save_meta()
                    return
            try:
                if self._fetch_from(session, pos):
                    backoff = 1.0
                    with self._lock:
                        pos = self._resume_point()
                    continue
            except Exception as exc:       # network, disk: retry, do not die
                self.error = str(exc)
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 10.0)
        self._save_meta()

    def _pick_start(self, pos: int):
        """Where to open the next connection. Called with the lock held."""
        want, self._want = self._want, None
        if self.total is None:
            return want or 0
        for candidate in (want, pos, 0):
            if candidate is None:
                continue
            gap = next_gap(self.intervals, candidate, self.total)
            if gap is not None:
                return gap
        return None

    def _resume_point(self) -> int:
        return self._want if self._want is not None else 0

    def _fetch_from(self, session, pos: int) -> bool:
        """One upstream connection from `pos`. True if it ended cleanly."""
        headers = {"Range": f"bytes={pos}-"} if pos else {}
        with session.get(self.url, headers=headers, stream=True, timeout=UPSTREAM_TIMEOUT,
                         allow_redirects=True) as resp:
            if resp.status_code not in (200, 206):
                raise OSError(f"HTTP {resp.status_code}")
            if pos and resp.status_code != 206:
                # No range support after all: start over from the top.
                pos = 0
            total = self._total_from(resp, pos)
            with self._lock:
                if self.total is None:
                    self.total = total
                    self._lock.notify_all()
                elif total != self.total:
                    raise OSError("upstream length changed")
            if total is None:
                raise OSError("no content length")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            mode = "r+b" if self.path.exists() else "wb"
            with open(self.path, mode) as handle:
                handle.seek(pos)
                for chunk in resp.iter_content(CHUNK):
                    if self._stop.is_set():
                        return True
                    if not chunk:
                        continue
                    handle.write(chunk)
                    handle.flush()          # readers open their own handle
                    with self._lock:
                        self.intervals = merge_intervals(self.intervals + [[pos, pos + len(chunk)]])
                        pos += len(chunk)
                        self._lock.notify_all()
                        jump = self._should_jump(pos)
                    self._since_flush += len(chunk)
                    if self._since_flush >= META_FLUSH_BYTES:
                        self._since_flush = 0
                        self._save_meta()
                    if jump:
                        return True
                    if pos >= self.total:
                        return True
            return True

    def _should_jump(self, pos: int) -> bool:
        """Drop this connection for a better starting point. Lock held."""
        want = self._want
        if want is not None and available_end(self.intervals, want) is None \
                and not (pos <= want < pos + NEAR_AHEAD):
            return True
        if want is not None and available_end(self.intervals, want) is not None:
            self._want = None
        # Reached a long cached island: skip it rather than re-download it.
        end = available_end(self.intervals, pos)
        if end is not None and end - pos >= SKIP_THRESHOLD and end < (self.total or 0):
            self._want = end
            return True
        return False

    @staticmethod
    def _total_from(resp, pos: int):
        match = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range") or "")
        if match:
            return int(match.group(1))
        length = resp.headers.get("Content-Length")
        if length and length.isdigit():
            return int(length) + (pos if resp.status_code == 206 else 0)
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):     # silence the per-request stderr line
        pass

    # Every open client socket is registered so release() can cut it: a
    # player that has stopped reading leaves the handler blocked in write(),
    # and libVLC's own stop() can wait on that connection in turn.
    def setup(self):
        super().setup()
        self.server.cache.track(self.connection, True)

    def finish(self):
        self.server.cache.track(self.connection, False)
        super().finish()

    def _stream(self):
        match = re.match(r"^/([0-9a-f]{16})/", self.path)
        stream = self.server.cache.stream_for(match.group(1)) if match else None
        if stream is None:
            self.send_error(404)
        return stream

    def _range(self, total: int):
        header = self.headers.get("Range") or ""
        match = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
        if not match:
            return 0, total - 1, False
        start, end = match.group(1), match.group(2)
        if start == "":
            length = int(end or 0)
            return max(0, total - length), total - 1, True
        start = int(start)
        end = min(int(end), total - 1) if end else total - 1
        return start, end, True

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head: bool):
        stream = self._stream()
        if stream is None:
            return
        total = stream.wait_total(TOTAL_WAIT_S)
        if total is None:
            self.send_error(502, "upstream gave no length")
            return
        start, end, partial = self._range(total)
        if start >= total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", self.server.cache.content_type_for(self.path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if head:
            return
        pos = start
        try:
            while pos <= end:
                data = stream.read(pos, min(CHUNK, end - pos + 1), READ_WAIT_S)
                if not data:
                    break                  # stream dead or stopped: the player will notice
                self.wfile.write(data)
                pos += len(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        if pos <= end:
            # Short answer: make sure the client does not reuse the connection.
            self.close_connection = True


class StreamCache:
    """The proxy server plus the one stream it is currently filling."""

    def __init__(self, directory: Path, limit_bytes: int = DEFAULT_LIMIT_GB * 1024 ** 3):
        self.directory = Path(directory)
        self.limit_bytes = int(limit_bytes)
        self._server = None
        self._thread = None
        self._current = None
        self._token = None
        self._ext = "mp4"
        self._lock = threading.Lock()
        self._clients = set()

    # --------------------------------------------------------------- server

    @property
    def port(self) -> int:
        self._ensure_server()
        return self._server.server_address[1]

    def _ensure_server(self):
        if self._server is not None:
            return
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.cache = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="stream-cache-http", daemon=True)
        self._thread.start()

    def track(self, connection, live: bool):
        with self._lock:
            if live:
                self._clients.add(connection)
            else:
                self._clients.discard(connection)

    def drop_clients(self):
        """Cut every player connection so blocked handlers unwind at once."""
        import socket

        with self._lock:
            clients = list(self._clients)
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stream_for(self, token: str):
        with self._lock:
            return self._current if token == self._token else None

    def content_type_for(self, path: str) -> str:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return CONTENT_TYPES.get(ext, "application/octet-stream")

    # --------------------------------------------------------------- streams

    @staticmethod
    def token_for(url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]

    def open(self, url: str, ext: str = "mp4") -> str:
        """Start filing `url` and return the local URL to play instead."""
        ext = (ext or "mp4").lstrip(".").lower() or "mp4"
        token = self.token_for(url)
        with self._lock:
            if self._current is not None and self._token == token:
                self._current.start()
                return self._local_url(token, ext)
            previous, self._current, self._token = self._current, None, None
        if previous is not None:
            previous.stop()
            self.drop_clients()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._trim(keep=token)
        stream = CachedStream(url, self.directory / f"{token}.{ext}")
        with self._lock:
            self._current, self._token, self._ext = stream, token, ext
        stream.start()
        return self._local_url(token, ext)

    def _local_url(self, token: str, ext: str) -> str:
        return f"http://127.0.0.1:{self.port}/{token}/stream.{ext}"

    def is_local(self, url: str) -> bool:
        return bool(url) and self._server is not None and \
            url.startswith(f"http://127.0.0.1:{self._server.server_address[1]}/")

    @property
    def current(self):
        return self._current

    def release(self):
        """Stop filling: playback has stopped, and the connection is wanted back."""
        with self._lock:
            stream, self._current, self._token = self._current, None, None
        if stream is not None:
            stream.stop()
        self.drop_clients()

    # ------------------------------------------------------------ housekeeping

    def _files(self):
        try:
            return [p for p in self.directory.iterdir()
                    if p.is_file() and p.suffix != ".json"]
        except OSError:
            return []

    def size_bytes(self) -> int:
        total = 0
        for path in self._files():
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def _trim(self, keep: str = ""):
        files = []
        for path in self._files():
            try:
                files.append((path.stat().st_mtime, path.stat().st_size, path))
            except OSError:
                pass
        used = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if used <= self.limit_bytes:
                break
            if path.stem == keep:
                continue
            self._remove(path)
            used -= size

    def _remove(self, path: Path):
        for target in (path, path.with_suffix(path.suffix + ".json")):
            try:
                target.unlink()
            except OSError:
                pass

    def clear(self) -> int:
        """Delete everything but the stream being played. Returns bytes freed."""
        freed = 0
        with self._lock:
            keep = self._token
        for path in self._files():
            if path.stem == keep:
                continue
            try:
                freed += path.stat().st_size
            except OSError:
                pass
            self._remove(path)
        return freed

    def shutdown(self):
        self.release()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
