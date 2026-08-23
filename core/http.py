"""Shared HTTP session factory.

Every outbound request must carry the app User-Agent. This provider answers
**HTTP 461** to the default `python-requests` UA and 200 to ours, and that
failure is silent and baffling when it happens on a code path that forgot the
header. Routing session creation through here makes it impossible to forget.

`requests` costs ~170 ms to import, so it is imported lazily: startup does not
need it, only sync/downloads/subtitles do.
"""

from __future__ import annotations

USER_AGENT = "IPTVPlayer/1.0"

# (connect, read) defaults. Catalog payloads are tens of MB, so reads are given
# room; callers that must stay responsive pass their own.
TIMEOUT = (15, 120)
IMAGE_TIMEOUT = (4, 6)


def make_session(user_agent: str = USER_AGENT):
    """A requests.Session with the app User-Agent already applied."""
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    return session


def ensure_user_agent(session, user_agent: str = USER_AGENT):
    """Stamp the UA onto a session supplied by a caller."""
    if session is not None and not session.headers.get("User-Agent", "").startswith("IPTVPlayer"):
        session.headers["User-Agent"] = user_agent
    return session
