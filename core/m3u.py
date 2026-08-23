"""Streaming M3U parser for providers that offer no Xtream API.

Parsed line-by-line and yielded as it goes: a provider M3U measured 163 MB with
569,925 entries, so it must never be read into memory whole.

M3U is a fallback, not the preferred source. It carries no series structure
(every episode appears as a top-level entry) and no ratings or TMDB ids, which
is why Xtream sources use the JSON API instead.
"""

from __future__ import annotations

import re
from typing import Iterator

from .http import make_session

_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_EPISODE = re.compile(
    r"^(?P<show>.+?)[ ._-]+S(?P<season>\d{1,2})[ ._-]?E(?P<episode>\d{1,3})\b",
    re.I,
)


def parse_extinf(line: str) -> dict:
    """Pull attributes and display name out of one #EXTINF line."""
    attrs = dict(_ATTR.findall(line))
    name = line.split(",", 1)[1].strip() if "," in line else ""
    return {
        "name": name or attrs.get("tvg-name", ""),
        "tvg_id": attrs.get("tvg-id", ""),
        "tvg_name": attrs.get("tvg-name", ""),
        "logo": attrs.get("tvg-logo", ""),
        "group": attrs.get("group-title", ""),
    }


def guess_kind(url: str) -> str:
    """Xtream-style M3U URLs encode the kind in their path."""
    lowered = url.lower()
    if "/series/" in lowered:
        return "series"
    if "/movie/" in lowered:
        return "movie"
    return "live"


def extension_of(url: str) -> str:
    tail = url.rsplit("/", 1)[-1]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].split("?")[0]
        if 1 <= len(ext) <= 5:
            return ext
    return ""


def stream_id_of(url: str) -> str:
    """Use the trailing id when present, else the URL itself as a stable key."""
    tail = url.rsplit("/", 1)[-1].split("?")[0]
    base = tail.rsplit(".", 1)[0] if "." in tail else tail
    return base or url


def split_episode(name: str):
    """Return (show, season, episode) when a name looks like S01E02."""
    match = _EPISODE.match(name or "")
    if not match:
        return None
    show = re.sub(r"[._]+", " ", match.group("show")).strip(" -")
    return show, int(match.group("season")), int(match.group("episode"))


def iter_lines_from_url(url: str, session=None) -> Iterator[str]:
    session = session or make_session()
    with session.get(url, stream=True, timeout=(15, 300)) as resp:
        resp.raise_for_status()
        resp.encoding = resp.encoding or "utf-8"
        for line in resp.iter_lines(decode_unicode=True):
            if line is not None:
                yield line


def iter_lines_from_file(path: str) -> Iterator[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line.rstrip("\n")


def parse(lines: Iterator[str]) -> Iterator[dict]:
    """Yield one dict per entry, holding at most a single entry in memory."""
    pending = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            pending = parse_extinf(line)
        elif line.startswith("#"):
            continue
        elif pending is not None:
            pending["url"] = line
            pending["kind"] = guess_kind(line)
            pending["container_extension"] = extension_of(line)
            pending["stream_id"] = stream_id_of(line)
            yield pending
            pending = None
