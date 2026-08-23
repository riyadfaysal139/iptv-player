"""Xtream Codes client.

Notes from probing a live portal, which shape this module:
  * Live and VOD URLs 302-redirect to a tokenized CDN host, so redirects must
    be followed (requests does by default; libVLC does too).
  * `allowed_output_formats` was ["ts"], so live uses .ts rather than .m3u8.
  * HEAD returns nothing useful; file size comes from a ranged GET's
    Content-Range header.
  * API calls succeed while a stream is open, so catalog sync does not need to
    pause playback.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from .http import TIMEOUT, USER_AGENT, ensure_user_agent, make_session


class ApiError(RuntimeError):
    pass


class AuthError(ApiError):
    pass


@dataclass
class AccountInfo:
    username: str
    status: str
    auth: bool
    exp_date: int | None
    max_connections: int
    active_connections: int
    allowed_formats: list
    is_trial: bool

    @property
    def active(self) -> bool:
        return self.auth and self.status.lower() == "active"


def normalise_server(url: str) -> str:
    """Accept anything paste-like and return a clean scheme://host[:port] base."""
    text = (url or "").strip()
    if not text:
        raise ValueError("Server URL is required")
    if not re.match(r"^https?://", text, re.I):
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.hostname:
        raise ValueError("Could not read a hostname from that URL")
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"
    return base


def parse_pasted(text: str) -> dict:
    """Extract server/username/password from a pasted portal or M3U link.

    Lets the user paste a full `get.php?username=...&password=...` URL and
    still end up on the (better) JSON API.
    """
    out = {"server": "", "username": "", "password": ""}
    raw = (text or "").strip()
    if not raw:
        return out
    if not re.match(r"^https?://", raw, re.I):
        raw = "http://" + raw
    parsed = urlparse(raw)
    qs = parse_qs(parsed.query or "")
    out["username"] = (qs.get("username") or [""])[0]
    out["password"] = (qs.get("password") or [""])[0]
    if parsed.hostname:
        base = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            base += f":{parsed.port}"
        out["server"] = base
    return out


class XtreamClient:
    def __init__(self, server: str, username: str, password: str, session=None):
        self.server = normalise_server(server)
        self.username = username or ""
        self.password = password or ""
        self.session = ensure_user_agent(session) if session else make_session()

    # -- low level ----------------------------------------------------------

    def _call(self, action: str | None = None, **params):
        query = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update({k: v for k, v in params.items() if v is not None})
        url = f"{self.server}/player_api.php"
        import requests

        try:
            resp = self.session.get(url, params=query, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ApiError(f"Cannot reach {self.server}: {exc}") from exc
        if resp.status_code != 200:
            raise ApiError(f"{action or 'auth'} returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ApiError(f"{action or 'auth'} did not return JSON") from exc
        # Xtream reports auth failure as {"user_info":{"auth":0}}
        if isinstance(data, dict) and "user_info" in data:
            if not data["user_info"].get("auth"):
                raise AuthError("Username or password rejected by the server")
        return data

    # -- account ------------------------------------------------------------

    def account(self) -> AccountInfo:
        data = self._call()
        info = data.get("user_info") or {}
        return AccountInfo(
            username=info.get("username", self.username),
            status=str(info.get("status", "")),
            auth=bool(info.get("auth")),
            exp_date=_as_int(info.get("exp_date")),
            max_connections=_as_int(info.get("max_connections")) or 1,
            active_connections=_as_int(info.get("active_cons")) or 0,
            allowed_formats=info.get("allowed_output_formats") or ["ts"],
            is_trial=str(info.get("is_trial", "0")) == "1",
        )

    # -- catalog ------------------------------------------------------------

    def categories(self, kind: str) -> list:
        action = {
            "live": "get_live_categories",
            "movie": "get_vod_categories",
            "series": "get_series_categories",
        }[kind]
        return _as_list(self._call(action))

    def streams(self, kind: str) -> list:
        action = {
            "live": "get_live_streams",
            "movie": "get_vod_streams",
            "series": "get_series",
        }[kind]
        return _as_list(self._call(action))

    def series_info(self, series_id) -> dict:
        data = self._call("get_series_info", series_id=series_id)
        return data if isinstance(data, dict) else {}

    def short_epg(self, stream_id, limit: int = 6) -> list:
        data = self._call("get_short_epg", stream_id=stream_id, limit=limit)
        listings = (data or {}).get("epg_listings") or []
        out = []
        for entry in listings:
            out.append(
                {
                    "title": _b64(entry.get("title")),
                    "description": _b64(entry.get("description")),
                    "start": entry.get("start"),
                    "end": entry.get("end"),
                }
            )
        return out

    # -- stream urls --------------------------------------------------------

    def live_url(self, stream_id, ext: str = "ts") -> str:
        return f"{self.server}/{self.username}/{self.password}/{stream_id}.{ext}"

    def movie_url(self, stream_id, ext: str = "mp4") -> str:
        return f"{self.server}/movie/{self.username}/{self.password}/{stream_id}.{ext or 'mp4'}"

    def episode_url(self, episode_id, ext: str = "mp4") -> str:
        return f"{self.server}/series/{self.username}/{self.password}/{episode_id}.{ext or 'mp4'}"

    def url_for(self, kind: str, stream_id, ext: str | None = None) -> str:
        if kind == "live":
            return self.live_url(stream_id, ext or "ts")
        if kind == "movie":
            return self.movie_url(stream_id, ext or "mp4")
        return self.episode_url(stream_id, ext or "mp4")

    def resolve_stream_url(self, url: str, timeout: int = 8) -> str | None:
        """Follow the portal's 302 one hop and return the CDN URL.

        The portal redirect costs ~700 ms and libVLC pays it before it can even
        reach the CDN; resolving ahead of time and handing VLC the final URL
        measured 1.75 s -> 0.92 s to first frame.

        `allow_redirects=False` means this never opens a stream to the CDN, and
        `active_cons` was verified to stay at 0 across repeated calls, so it
        does not consume the account's single connection. Returns None on
        anything unexpected so the caller falls back to the original URL.
        """
        import requests

        try:
            resp = self.session.get(
                url, allow_redirects=False, stream=True, timeout=(5, timeout)
            )
        except requests.RequestException:
            return None
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location") or ""
                return location if location.startswith("http") else None
            return None
        finally:
            resp.close()

    # -- downloads ----------------------------------------------------------

    def remote_size(self, url: str) -> int:
        """Total size in bytes, via Content-Range (HEAD is useless here)."""
        import requests

        try:
            resp = self.session.get(
                url, headers={"Range": "bytes=0-0"}, stream=True, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            raise ApiError(f"Could not read size: {exc}") from exc
        try:
            rng = resp.headers.get("Content-Range") or ""
            match = re.search(r"/(\d+)\s*$", rng)
            if match:
                return int(match.group(1))
            length = resp.headers.get("Content-Length")
            if length and resp.status_code == 200:
                return int(length)
            return 0
        finally:
            resp.close()


def _b64(value) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except Exception:
        return str(value)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Some panels return {"0": {...}, "1": {...}}
        return [v for v in data.values() if isinstance(v, dict)]
    return []
