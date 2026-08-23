"""Subtitle fetching: shared types, file hashing, and the REST fallback.

There are two backends, and they return the same `SubtitleResult` so the UI has
one code path:

* `core/vlsub.py` — **the default.** The keyless XML-RPC protocol VLSub itself
  uses. Nothing to register, nothing to paste.
* `OpenSubtitlesClient` below — OpenSubtitles' current REST API, which requires
  a user-supplied key. Kept as a fallback in case the legacy endpoint is ever
  retired, and never reached unless the user selects it.

The OpenSubtitles hash lives here because both backends use the identical
algorithm: file size plus the first and last 64 KiB, summed as 64-bit words.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .http import ensure_user_agent, make_session

API_ROOT = "https://api.opensubtitles.com/api/v1"
SITE_URL = "https://www.opensubtitles.com/en/consumers"
USER_AGENT = "IPTVPlayer v1.0"   # OpenSubtitles requires its own UA string
CHUNK = 65536

BACKEND_VLSUB = "vlsub"
BACKEND_REST = "rest"

# (label, ISO 639-2/B id for the VLSub endpoint, ISO 639-1 for the REST API).
# The endpoint wants bibliographic codes — French is "fre", not "fra".
LANGUAGES = (
    ("English", "eng", "en"),
    ("Bengali", "ben", "bn"),
    ("Hindi", "hin", "hi"),
    ("Urdu", "urd", "ur"),
    ("Arabic", "ara", "ar"),
    ("Spanish", "spa", "es"),
    ("French", "fre", "fr"),
    ("German", "ger", "de"),
    ("Portuguese", "por", "pt"),
    ("Portuguese (BR)", "pob", "pt-br"),
    ("Italian", "ita", "it"),
    ("Turkish", "tur", "tr"),
    ("Tamil", "tam", "ta"),
    ("Telugu", "tel", "te"),
    ("Malayalam", "mal", "ml"),
    ("Dutch", "dut", "nl"),
    ("Russian", "rus", "ru"),
    ("Chinese", "chi", "zh-cn"),
    ("Japanese", "jpn", "ja"),
    ("Korean", "kor", "ko"),
    ("Indonesian", "ind", "id"),
    ("Malay", "may", "ms"),
    ("Persian", "per", "fa"),
    ("Nepali", "nep", "ne"),
)


class SubtitleError(RuntimeError):
    """A failure worth showing the user verbatim.

    `status` carries the backend's own status code when there is one, so the
    caller can tell an expired session (retryable) from a spent quota (not).
    """

    def __init__(self, message: str, status: str = ""):
        super().__init__(message)
        self.status = status


@dataclass
class SubtitleResult:
    ref: str            # backend-specific handle: a REST file id, or a URL
    file_name: str
    language: str
    release: str
    downloads: int
    rating: float
    from_hash: bool
    backend: str = BACKEND_VLSUB
    encoding: str = ""
    fmt: str = "srt"
    hearing_impaired: bool = False

    @property
    def label(self) -> str:
        parts = ["★" if self.from_hash else " ", self.release or self.file_name]
        if self.language:
            parts.append(f"[{self.language}]")
        if self.hearing_impaired:
            parts.append("[HI]")
        if self.downloads:
            parts.append(f"{self.downloads:,}↓")
        return "  ".join(part for part in parts if part).strip()


# --------------------------------------------------------------------------
# hashing
# --------------------------------------------------------------------------


def hash_file(path: str | Path) -> tuple[str, int]:
    """OpenSubtitles hash of a local file: size + first and last 64 KiB."""
    path = Path(path)
    size = path.stat().st_size
    if size < CHUNK * 2:
        raise SubtitleError("File is too small to hash")
    value = size
    fmt = "<%dQ" % (CHUNK // 8)
    with open(path, "rb") as handle:
        for chunk in (handle.read(CHUNK),):
            for piece in struct.unpack(fmt, chunk):
                value = (value + piece) & 0xFFFFFFFFFFFFFFFF
        handle.seek(-CHUNK, 2)
        for piece in struct.unpack(fmt, handle.read(CHUNK)):
            value = (value + piece) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}", size


def local_path(url: str) -> Path | None:
    """The local file behind a URL, or None if it is a network stream.

    A downloaded title plays from a `file://` URI, and `requests` has no
    adapter for that scheme — fingerprinting one over HTTP fails outright.
    Downloads are also the case where an exact match actually succeeds, since a
    provider's own re-encode of a stream is rarely in OpenSubtitles' index, so
    this is the path that matters most.
    """
    if not url:
        return None
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse

        candidate = Path(unquote(urlparse(url).path))
    elif "://" in url:
        return None
    else:
        candidate = Path(url)
    return candidate if candidate.is_file() else None


def hash_any(url: str) -> tuple[str, int]:
    """Fingerprint whatever is playing, local file or remote stream."""
    local = local_path(url)
    return hash_file(local) if local is not None else hash_url(url)


def hash_url(url: str, session=None) -> tuple[str, int]:
    """Same hash for a remote file, via two ranged GETs.

    The portal 302-redirects to a tokenized CDN URL, so both ranges are taken
    against the final URL. Callers fall back to name search if this fails.
    """
    # This fetches the *provider's* stream URL, not OpenSubtitles, and the
    # provider answers HTTP 461 to the default python-requests User-Agent - so
    # the exact-match hash path silently failed every time without this.
    session = ensure_user_agent(session) if session else make_session()
    head = session.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(15, 60))
    try:
        import re

        match = re.search(r"/(\d+)\s*$", head.headers.get("Content-Range") or "")
        if not match:
            raise SubtitleError("Server did not report a file size")
        size = int(match.group(1))
        final_url = head.url
    finally:
        head.close()
    if size < CHUNK * 2:
        raise SubtitleError("Stream is too small to hash")

    def grab(start: int, end: int) -> bytes:
        resp = session.get(
            final_url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=(15, 60)
        )
        try:
            data = resp.content
        finally:
            resp.close()
        if len(data) < CHUNK:
            raise SubtitleError("Server returned a short range")
        return data[:CHUNK]

    value = size
    fmt = "<%dQ" % (CHUNK // 8)
    for chunk in (grab(0, CHUNK - 1), grab(size - CHUNK, size - 1)):
        for piece in struct.unpack(fmt, chunk):
            value = (value + piece) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}", size


# --------------------------------------------------------------------------
# REST client (fallback; needs a user-supplied key)
# --------------------------------------------------------------------------


class OpenSubtitlesClient:
    """OpenSubtitles.com REST v1.

    Only used when the user explicitly selects it under Config. It exposes the
    same `search_hash` / `search_name` / `download` surface as `VLSubClient`.
    """

    backend = BACKEND_REST

    def __init__(self, api_key: str, session=None):
        self.api_key = (api_key or "").strip()
        # OpenSubtitles wants its own UA, set per-request in _headers().
        self.session = session or make_session(USER_AGENT)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "Api-Key": self.api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict):
        if not self.configured:
            raise SubtitleError(
                "This backend needs an OpenSubtitles API key.\n"
                f"Create a free key at {SITE_URL} and paste it into Config — or "
                "switch back to VLSub, which needs no key at all."
            )
        import requests

        try:
            resp = self.session.get(
                f"{API_ROOT}{path}", params=params, headers=self._headers(), timeout=(15, 45)
            )
        except requests.RequestException as exc:
            raise SubtitleError(f"Could not reach OpenSubtitles: {exc}") from exc
        if resp.status_code == 401:
            raise SubtitleError("OpenSubtitles rejected the API key.", "401")
        if resp.status_code == 429:
            raise SubtitleError("OpenSubtitles rate limit reached — try again shortly.", "429")
        if resp.status_code != 200:
            raise SubtitleError(f"OpenSubtitles returned HTTP {resp.status_code}",
                                str(resp.status_code))
        try:
            return resp.json()
        except ValueError as exc:
            raise SubtitleError("OpenSubtitles returned an unreadable response") from exc

    def _search(self, params: dict) -> list[SubtitleResult]:
        data = self._get("/subtitles", params)
        results = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            files = attrs.get("files") or []
            if not files:
                continue
            results.append(
                SubtitleResult(
                    ref=str(files[0].get("file_id")),
                    file_name=files[0].get("file_name") or "",
                    language=(attrs.get("language") or "").upper(),
                    release=attrs.get("release") or "",
                    downloads=int(attrs.get("download_count") or 0),
                    rating=float(attrs.get("ratings") or 0.0),
                    from_hash=bool(attrs.get("moviehash_match")),
                    backend=BACKEND_REST,
                    hearing_impaired=bool(attrs.get("hearing_impaired")),
                )
            )
        results.sort(key=lambda r: (not r.from_hash, -r.downloads))
        return results

    def search_hash(self, moviehash: str, bytesize: int,
                    languages=()) -> list[SubtitleResult]:
        params = {"moviehash": moviehash}
        if languages:
            params["languages"] = ",".join(languages)
        return self._search(params)

    def search_name(self, query: str, season: int = 0, episode: int = 0,
                    languages=()) -> list[SubtitleResult]:
        params = {"query": query}
        if languages:
            params["languages"] = ",".join(languages)
        if season:
            params["season_number"] = season
        if episode:
            params["episode_number"] = episode
        return self._search(params)

    def download(self, result: SubtitleResult, dest_dir: Path, base_name: str) -> Path:
        if not self.configured:
            raise SubtitleError("This backend needs an OpenSubtitles API key.")
        import requests

        try:
            resp = self.session.post(
                f"{API_ROOT}/download",
                json={"file_id": int(result.ref)},
                headers={**self._headers(), "Content-Type": "application/json"},
                timeout=(15, 45),
            )
        except requests.RequestException as exc:
            raise SubtitleError(f"Could not reach OpenSubtitles: {exc}") from exc
        if resp.status_code != 200:
            raise SubtitleError(
                f"Download request failed (HTTP {resp.status_code}). "
                "Free accounts have a daily limit.",
                str(resp.status_code),
            )
        link = (resp.json() or {}).get("link")
        if not link:
            raise SubtitleError("OpenSubtitles did not return a download link")

        try:
            data = self.session.get(link, timeout=(15, 60))
            data.raise_for_status()
        except requests.RequestException as exc:
            raise SubtitleError(f"Could not fetch the subtitle file: {exc}") from exc

        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{base_name}.srt"
        target.write_bytes(data.content)
        return target
