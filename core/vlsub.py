"""Keyless OpenSubtitles access — the protocol VLSub itself speaks.

VLSub, the Lua extension bundled with the VLC application, does not use an API
key. It talks to the legacy XML-RPC endpoint at api.opensubtitles.org and logs
in *anonymously* (empty username and password) with a registered user-agent
string. This module speaks that same protocol, so subtitle search works out of
the box with nothing for the user to register or paste.

Two implementation notes, both learned the hard way:

1. **Do not use `xmlrpc.client.ServerProxy`.** Its transport goes through the
   stdlib `ssl` module, which verifies against the *system* CA store — that
   raises `CERTIFICATE_VERIFY_FAILED` against this endpoint on a python.org
   install, and a frozen PyInstaller bundle has no system store to fall back
   on. `requests` ships certifi. So: encode and decode with the stdlib, but
   transport with `requests`.

2. **The download link returns gzip, and the charset is whatever the uploader
   used.** Many rows are CP1256 or CP1251, so the bytes are transcoded to UTF-8
   before being written; VLC renders Arabic and Bengali as mojibake otherwise.
"""

from __future__ import annotations

import gzip
import time
import xmlrpc.client

from .http import make_session
from .subtitles import SubtitleError, SubtitleResult

ENDPOINT = "https://api.opensubtitles.org/xml-rpc"

# OpenSubtitles wants a *registered* agent string. This is VLSub's own, which
# is what the user asked for ("vlsub as we use in windows") and is registered
# by VideoLAN.
USER_AGENT = "VLSub 0.10.2"

SIGN_UP_URL = "https://www.opensubtitles.org/en/newuser"

TIMEOUT = (10, 45)
SEARCH_LIMIT = 60
TOKEN_TTL = 600         # the server drops idle sessions; cheap to re-login

GZIP_MAGIC = b"\x1f\x8b"

LIMIT_HINT = (
    "The free daily limit is counted per internet connection. Adding your free "
    "opensubtitles.org account under Config raises it."
)

# Status strings the endpoint returns in place of an HTTP error code.
_STATUS_MESSAGES = {
    "401": "OpenSubtitles rejected the request as unauthorized.",
    "402": "OpenSubtitles rejected the subtitle data as invalid.",
    "403": "OpenSubtitles could not verify that request.",
    "406": "The OpenSubtitles session expired.",
    "407": f"OpenSubtitles' daily download limit is used up. {LIMIT_HINT}",
    "408": "OpenSubtitles said the request was missing information.",
    "410": "OpenSubtitles could not understand that request.",
    "411": "OpenSubtitles needs a valid session — try again.",
    "412": "OpenSubtitles rejected the file fingerprint.",
    "413": "OpenSubtitles rejected the search — the details were incomplete.",
    "414": "OpenSubtitles rejected this application's user agent.",
    "415": "OpenSubtitles has not enabled this application's user agent.",
    "429": "Too many requests to OpenSubtitles — wait a moment and try again.",
    "503": "OpenSubtitles is busy right now — try again shortly.",
    "506": "OpenSubtitles is down for maintenance.",
    "520": "OpenSubtitles reported an internal error.",
}

# Session-level failures worth one silent re-login before giving up.
_SESSION_STATUSES = ("401", "406", "411")


def status_code(status: str) -> str:
    """The bare numeric part of a status like "407 Download limit reached"."""
    parts = (status or "").strip().split(None, 1)
    return parts[0] if parts else ""


def status_message(status: str) -> str:
    """Map an XML-RPC status string to a sentence, or "" if it means success."""
    code = status_code(status)
    if not code or code == "200":
        return ""
    return _STATUS_MESSAGES.get(code, f"OpenSubtitles returned “{status.strip()}”.")


class VLSubClient:
    """Anonymous-by-default OpenSubtitles client.

    A username and password are optional: supplying a free opensubtitles.org
    account only raises the daily download quota, it is not needed to search.
    """

    backend = "vlsub"

    def __init__(self, username: str = "", password: str = "", session=None):
        self.username = (username or "").strip()
        self.password = password or ""
        self.session = session or make_session(USER_AGENT)
        self._token = ""
        self._token_at = 0.0

    @property
    def configured(self) -> bool:
        """Always usable — that is the whole point of this backend."""
        return True

    # ----------------------------------------------------------- transport

    def _call(self, method: str, *args):
        import requests

        body = xmlrpc.client.dumps(args, method, allow_none=True).encode("utf-8")
        try:
            resp = self.session.post(
                ENDPOINT, data=body, timeout=TIMEOUT,
                headers={"Content-Type": "text/xml", "User-Agent": USER_AGENT},
            )
        except requests.RequestException as exc:
            raise SubtitleError(f"Could not reach OpenSubtitles: {exc}") from exc
        if resp.status_code != 200:
            raise SubtitleError(f"OpenSubtitles returned HTTP {resp.status_code}")
        try:
            (result,), _ = xmlrpc.client.loads(resp.content)
        except xmlrpc.client.Fault as exc:
            raise SubtitleError(f"OpenSubtitles reported: {exc.faultString}") from exc
        except Exception as exc:
            raise SubtitleError("OpenSubtitles returned an unreadable response") from exc
        return result

    def _checked(self, method: str, *args) -> dict:
        result = self._call(method, *args)
        if not isinstance(result, dict):
            raise SubtitleError("OpenSubtitles returned an unexpected response")
        status = result.get("status", "")
        message = status_message(status)
        if message:
            raise SubtitleError(message, status_code(status))
        return result

    # ---------------------------------------------------------------- auth

    def log_in(self, force: bool = False) -> str:
        if not force and self._token and (time.time() - self._token_at) < TOKEN_TTL:
            return self._token
        try:
            result = self._checked("LogIn", self.username, self.password, "en", USER_AGENT)
        except SubtitleError as exc:
            if self.username and exc.status == "401":
                raise SubtitleError(
                    "OpenSubtitles rejected that username or password. Clear "
                    "them under Config to search anonymously instead.", "401",
                ) from exc
            raise
        token = (result.get("token") or "").strip()
        if not token:
            raise SubtitleError("OpenSubtitles did not return a session token.")
        self._token = token
        self._token_at = time.time()
        return token

    # -------------------------------------------------------------- search

    def _search(self, criteria: dict) -> list[SubtitleResult]:
        token = self.log_in()
        try:
            data = self._checked("SearchSubtitles", token, [criteria], {"limit": SEARCH_LIMIT})
        except SubtitleError as exc:
            # The server drops idle sessions and answers "401 Unauthorized" to
            # the stale token. One silent re-login, then give up.
            if exc.status not in _SESSION_STATUSES:
                raise
            token = self.log_in(force=True)
            data = self._checked("SearchSubtitles", token, [criteria], {"limit": SEARCH_LIMIT})

        results = [_to_result(row) for row in (data.get("data") or []) if row]
        results.sort(key=lambda r: (not r.from_hash, -r.downloads))
        return results

    def search_hash(self, moviehash: str, bytesize: int,
                    languages=()) -> list[SubtitleResult]:
        """VLSub's "Search by hash" — an exact match on the file's fingerprint."""
        return self._search({
            "sublanguageid": _languages(languages),
            "moviehash": moviehash,
            "moviebytesize": str(int(bytesize)),
        })

    def search_name(self, query: str, season: int = 0, episode: int = 0,
                    languages=()) -> list[SubtitleResult]:
        """VLSub's "Search by name" — title, optionally with season/episode."""
        criteria = {"sublanguageid": _languages(languages), "query": query}
        if season:
            criteria["season"] = str(int(season))
        if episode:
            criteria["episode"] = str(int(episode))
        return self._search(criteria)

    def languages(self) -> list[tuple[str, str]]:
        data = self._checked("GetSubLanguages", "en")
        return [
            (row.get("LanguageName") or row.get("SubLanguageID", ""),
             row.get("SubLanguageID", ""))
            for row in (data.get("data") or [])
        ]

    # ------------------------------------------------------------ download

    def download(self, result: SubtitleResult, dest_dir, base_name: str):
        """Fetch, gunzip and transcode one subtitle, exactly as VLSub does."""
        import requests

        if not result.ref:
            raise SubtitleError("That result has no download link.")
        try:
            resp = self.session.get(result.ref, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SubtitleError(f"Could not fetch the subtitle file: {exc}") from exc
        if resp.status_code != 200:
            raise SubtitleError(
                f"The subtitle download failed (HTTP {resp.status_code}). {LIMIT_HINT}"
            )

        text = decode_payload(resp.content, result.encoding)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{base_name}.{result.fmt or 'srt'}"
        target.write_text(text, encoding="utf-8")
        return target


# --------------------------------------------------------------------------
# payload handling (pure, so it is unit-testable without the network)
# --------------------------------------------------------------------------


def decode_payload(raw: bytes, encoding: str = "") -> str:
    """gunzip a download and return UTF-8 text.

    When the daily quota is spent the server sends an HTML page with HTTP 200
    rather than an error, so a non-gzip body is treated as that page instead of
    being written to disk as a broken subtitle.
    """
    if raw[:2] != GZIP_MAGIC:
        raise SubtitleError(
            "OpenSubtitles sent a web page instead of a subtitle, which means "
            f"the download limit is reached. {LIMIT_HINT}"
        )
    try:
        data = gzip.decompress(raw)
    except (OSError, EOFError) as exc:
        raise SubtitleError("The downloaded subtitle file was corrupt.") from exc

    for candidate in (encoding, "utf-8-sig", "cp1252", "latin-1"):
        if not candidate:
            continue
        try:
            return data.decode(candidate).lstrip("\ufeff")
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
    return data.decode("utf-8", "replace").lstrip("\ufeff")


def _languages(languages) -> str:
    """Comma-separated ISO 639-2/B ids, which is what this endpoint wants."""
    codes = [code for code in (languages or ()) if code]
    return ",".join(dict.fromkeys(codes)) or "eng"


def _to_result(row: dict) -> SubtitleResult:
    def text(key: str) -> str:
        value = row.get(key)
        return "" if value in (None, False) else str(value)

    def number(key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return SubtitleResult(
        ref=text("SubDownloadLink"),
        file_name=text("SubFileName"),
        language=text("SubLanguageID").upper(),
        release=text("MovieReleaseName") or text("SubFileName"),
        downloads=int(number("SubDownloadsCnt")),
        rating=number("SubRating"),
        from_hash=text("MatchedBy") == "moviehash",
        backend="vlsub",
        encoding=text("SubEncoding"),
        fmt=text("SubFormat").lower() or "srt",
        hearing_impaired=text("SubHearingImpaired") == "1",
    )
