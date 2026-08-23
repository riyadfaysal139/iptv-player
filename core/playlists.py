"""Playlist (source) management: add, edit, delete, switch.

Nothing is hardcoded — the first playlist is created by the user through the
add wizard. Passwords go to the OS keychain keyed by playlist id, never into
the SQLite file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import api
from .db import Database

KEYRING_SERVICE = "IPTVPlayer"

try:
    import keyring

    _KEYRING_OK = True
except Exception:  # pragma: no cover - keyring is optional at runtime
    keyring = None
    _KEYRING_OK = False


TYPE_XTREAM = "xtream"
TYPE_M3U_URL = "m3u_url"
TYPE_M3U_FILE = "m3u_file"


@dataclass
class Playlist:
    id: int
    name: str
    type: str
    server_url: str = ""
    username: str = ""
    epg_url: str = ""
    file_path: str = ""
    position: int = 0
    last_sync_at: int | None = None
    sync_interval_hours: int = 24
    sync_at_time: str = "04:00"
    auto_sync: bool = True
    exp_date: int | None = None
    max_connections: int | None = None
    is_active: bool = False
    namespace: str = ""

    @property
    def is_xtream(self) -> bool:
        return self.type == TYPE_XTREAM

    @property
    def password(self) -> str:
        return get_password(self.id, self.namespace)

    def client(self) -> api.XtreamClient:
        if not self.is_xtream:
            raise ValueError(f"{self.name} is not an Xtream playlist")
        return api.XtreamClient(self.server_url, self.username, self.password)


# --------------------------------------------------------------------------
# credential storage
# --------------------------------------------------------------------------


def _key(playlist_id: int, namespace: str = "") -> str:
    """Namespaced so playlist 1 in one database cannot clobber playlist 1 in
    another (a second profile, or a test fixture)."""
    return f"{namespace}-playlist-{playlist_id}" if namespace else f"playlist-{playlist_id}"


def set_password(playlist_id: int, password: str, namespace: str = ""):
    if not _KEYRING_OK:
        return
    try:
        if password:
            keyring.set_password(KEYRING_SERVICE, _key(playlist_id, namespace), password)
        else:
            keyring.delete_password(KEYRING_SERVICE, _key(playlist_id, namespace))
    except Exception:
        # A locked or unavailable keychain must not break the app.
        pass


def get_password(playlist_id: int, namespace: str = "") -> str:
    if not _KEYRING_OK:
        return ""
    try:
        return keyring.get_password(KEYRING_SERVICE, _key(playlist_id, namespace)) or ""
    except Exception:
        return ""


def clear_password(playlist_id: int, namespace: str = ""):
    if not _KEYRING_OK:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, _key(playlist_id, namespace))
    except Exception:
        pass


def set_secret(name: str, value: str, namespace: str = ""):
    """Store a non-playlist credential — currently the OpenSubtitles login.

    Same keychain, same namespacing rule: a second profile or a test fixture
    must not be able to overwrite the real one.
    """
    if not _KEYRING_OK:
        return
    key = f"{namespace}-{name}" if namespace else name
    try:
        if value:
            keyring.set_password(KEYRING_SERVICE, key, value)
        else:
            keyring.delete_password(KEYRING_SERVICE, key)
    except Exception:
        pass


def get_secret(name: str, namespace: str = "") -> str:
    if not _KEYRING_OK:
        return ""
    key = f"{namespace}-{name}" if namespace else name
    try:
        return keyring.get_password(KEYRING_SERVICE, key) or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


def probe(server: str, username: str, password: str) -> api.AccountInfo:
    """Test credentials against player_api.php."""
    return api.XtreamClient(server, username, password).account()


def detect_type(pasted: str) -> dict:
    """Work out the best source type for whatever the user pasted.

    An Xtream API is strictly better than the same provider's M3U (it carries
    series structure, ratings and posters), so when a get.php link is pasted
    and the API answers, this upgrades it.
    """
    parts = api.parse_pasted(pasted)
    result = {
        "type": TYPE_M3U_URL,
        "server": parts["server"],
        "username": parts["username"],
        "password": parts["password"],
        "url": pasted.strip(),
        "account": None,
        "note": "",
    }
    if parts["server"] and parts["username"] and parts["password"]:
        try:
            account = probe(parts["server"], parts["username"], parts["password"])
        except api.ApiError:
            result["note"] = "No Xtream API found — will use the M3U list."
        else:
            result["type"] = TYPE_XTREAM
            result["account"] = account
            result["note"] = "Xtream API detected — using it for full categories and series."
    return result


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def _row_to_playlist(row, namespace: str = "") -> Playlist:
    return Playlist(
        namespace=namespace,
        id=row["id"],
        name=row["name"],
        type=row["type"],
        server_url=row["server_url"] or "",
        username=row["username"] or "",
        epg_url=row["epg_url"] or "",
        file_path=row["file_path"] or "",
        position=row["position"],
        last_sync_at=row["last_sync_at"],
        sync_interval_hours=row["sync_interval_hours"],
        sync_at_time=row["sync_at_time"],
        auto_sync=bool(row["auto_sync"]),
        exp_date=row["exp_date"],
        max_connections=row["max_connections"],
        is_active=bool(row["is_active"]),
    )


class PlaylistStore:
    def __init__(self, db: Database):
        self.db = db

    @property
    def namespace(self) -> str:
        return self.db.instance_id

    def all(self) -> list[Playlist]:
        rows = self.db.query("SELECT * FROM playlists ORDER BY position, id")
        return [_row_to_playlist(r, self.namespace) for r in rows]

    def get(self, playlist_id: int) -> Playlist | None:
        row = self.db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
        return _row_to_playlist(row, self.namespace) if row else None

    def active(self) -> Playlist | None:
        row = self.db.one("SELECT * FROM playlists WHERE is_active=1 LIMIT 1")
        if row:
            return _row_to_playlist(row, self.namespace)
        rows = self.all()
        return rows[0] if rows else None

    def add(
        self,
        name: str,
        type: str,
        server_url: str = "",
        username: str = "",
        password: str = "",
        epg_url: str = "",
        file_path: str = "",
        account: api.AccountInfo | None = None,
    ) -> Playlist:
        position = self.db.scalar("SELECT COALESCE(MAX(position), -1) + 1 FROM playlists", (), 0)
        cur = self.db.execute(
            "INSERT INTO playlists(name, type, server_url, username, epg_url, "
            "file_path, position, exp_date, max_connections, is_active) "
            "VALUES(?,?,?,?,?,?,?,?,?,0)",
            (
                name.strip() or "Playlist",
                type,
                server_url,
                username,
                epg_url,
                file_path,
                position,
                account.exp_date if account else None,
                account.max_connections if account else None,
            ),
        )
        playlist_id = cur.lastrowid
        if password:
            set_password(playlist_id, password, self.namespace)
        if self.db.scalar("SELECT COUNT(*) FROM playlists", (), 0) == 1:
            self.set_active(playlist_id)
        return self.get(playlist_id)

    def update(self, playlist_id: int, password: str | None = None, **fields):
        allowed = {
            "name", "type", "server_url", "username", "epg_url", "file_path",
            "position", "last_sync_at", "sync_interval_hours", "sync_at_time",
            "auto_sync", "exp_date", "max_connections",
        }
        pairs = {k: v for k, v in fields.items() if k in allowed}
        if pairs:
            assignments = ", ".join(f"{k}=?" for k in pairs)
            self.db.execute(
                f"UPDATE playlists SET {assignments} WHERE id=?",
                (*pairs.values(), playlist_id),
            )
        if password is not None:
            set_password(playlist_id, password, self.namespace)
        return self.get(playlist_id)

    def delete(self, playlist_id: int, delete_downloads: bool = False) -> list[str]:
        """Remove a playlist. Returns download paths the caller may delete.

        Downloaded files are large, so they are kept by default and the caller
        decides — the cascade only removes catalog rows.
        """
        paths = [
            r["dest_path"]
            for r in self.db.query(
                "SELECT dest_path FROM downloads WHERE playlist_id=? AND status='done'",
                (playlist_id,),
            )
        ]
        was_active = bool(
            self.db.scalar("SELECT is_active FROM playlists WHERE id=?", (playlist_id,), 0)
        )
        self.db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
        clear_password(playlist_id, self.namespace)
        if was_active:
            remaining = self.all()
            if remaining:
                self.set_active(remaining[0].id)
        return paths if delete_downloads else []

    def set_active(self, playlist_id: int):
        with self.db._write_lock:
            self.db.conn.execute("UPDATE playlists SET is_active=0")
            self.db.conn.execute(
                "UPDATE playlists SET is_active=1 WHERE id=?", (playlist_id,)
            )
            self.db.conn.commit()

    def mark_synced(self, playlist_id: int, account: api.AccountInfo | None = None):
        fields = {"last_sync_at": int(time.time())}
        if account:
            fields["exp_date"] = account.exp_date
            fields["max_connections"] = account.max_connections
        self.update(playlist_id, **fields)

    def due_for_sync(self, playlist: Playlist, now: float | None = None) -> bool:
        if not playlist.auto_sync:
            return False
        if not playlist.last_sync_at:
            return True
        now = now if now is not None else time.time()
        age_hours = (now - playlist.last_sync_at) / 3600.0
        return age_hours >= max(1, playlist.sync_interval_hours)
