"""Playlist manager and add/edit wizard.

Nothing about a provider is hardcoded — the first playlist is created here.
The add form accepts anything paste-like (a portal URL, an M3U get.php link,
host:port) and probes for an Xtream API, preferring it when present because it
carries series structure, posters and ratings that an M3U does not.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from core import api, playlists as pl


class _ProbeWorker(QObject):
    done = Signal(object, str)

    def __init__(self, server, username, password):
        super().__init__()
        self.server, self.username, self.password = server, username, password

    def run(self):
        try:
            account = pl.probe(self.server, self.username, self.password)
            self.done.emit(account, "")
        except Exception as exc:
            self.done.emit(None, str(exc))


class PlaylistEditor(QDialog):
    """Add or edit one source."""

    def __init__(self, parent=None, playlist: pl.Playlist | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Playlist" if playlist else "Add Playlist")
        self.setMinimumWidth(520)
        self.playlist = playlist
        self.account: api.AccountInfo | None = None
        self._thread = None
        self._original = None

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Paste your provider's portal address, or a full M3U link — the "
            "details below fill in automatically."
        )
        intro.setWordWrap(True)
        intro.setObjectName("statusNote")
        layout.addWidget(intro)

        self.paste = QLineEdit()
        self.paste.setPlaceholderText("http://example.com:8080/get.php?username=…&password=…")
        self.paste.editingFinished.connect(self._autofill)
        layout.addWidget(self.paste)

        form = QFormLayout()
        form.setSpacing(8)

        self.name = QLineEdit()
        self.name.setPlaceholderText("My provider")
        form.addRow("Name", self.name)

        self.type = QComboBox()
        self.type.addItem("Xtream Codes (recommended)", pl.TYPE_XTREAM)
        self.type.addItem("M3U URL", pl.TYPE_M3U_URL)
        self.type.addItem("Local M3U file", pl.TYPE_M3U_FILE)
        self.type.currentIndexChanged.connect(self._sync_fields)
        form.addRow("Type", self.type)

        self.server = QLineEdit()
        self.server.setPlaceholderText("http://example.com:8080")
        form.addRow("Server", self.server)

        self.username = QLineEdit()
        form.addRow("Username", self.username)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("Password", self.password)

        file_row = QWidget()
        file_layout = QHBoxLayout(file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_path = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        file_layout.addWidget(self.file_path, 1)
        file_layout.addWidget(browse)
        self.file_row = file_row
        form.addRow("M3U file", file_row)

        self.epg_url = QLineEdit()
        self.epg_url.setPlaceholderText("Optional XMLTV URL")
        form.addRow("EPG URL", self.epg_url)

        self.auto_sync = QCheckBox("Update this playlist automatically every day")
        self.auto_sync.setChecked(True)
        form.addRow("", self.auto_sync)

        layout.addLayout(form)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("statusNote")
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.status, 1)
        layout.addLayout(test_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if playlist:
            self._load(playlist)
        self._sync_fields()

    # ------------------------------------------------------------- helpers

    def _load(self, playlist: pl.Playlist):
        self.name.setText(playlist.name)
        index = self.type.findData(playlist.type)
        if index >= 0:
            self.type.setCurrentIndex(index)
        self.server.setText(playlist.server_url)
        self.username.setText(playlist.username)
        self.password.setText(playlist.password)
        self.file_path.setText(playlist.file_path)
        self.epg_url.setText(playlist.epg_url)
        self.auto_sync.setChecked(playlist.auto_sync)
        self.paste.hide()
        self._original = (playlist.server_url, playlist.username, playlist.password)

    def _sync_fields(self):
        kind = self.type.currentData()
        is_xtream = kind == pl.TYPE_XTREAM
        is_file = kind == pl.TYPE_M3U_FILE
        for widget in (self.username, self.password):
            widget.setEnabled(is_xtream)
        self.server.setEnabled(not is_file)
        self.server.setPlaceholderText(
            "http://example.com:8080" if is_xtream else "http://example.com/playlist.m3u"
        )
        self.file_row.setVisible(is_file)
        self.test_button.setEnabled(is_xtream)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an M3U file", "", "Playlists (*.m3u *.m3u8);;All files (*)"
        )
        if path:
            self.file_path.setText(path)

    def _autofill(self):
        text = self.paste.text().strip()
        if not text:
            return
        self.status.setText("Checking…")
        detected = pl.detect_type(text)
        if detected["server"]:
            self.server.setText(detected["server"])
        if detected["username"]:
            self.username.setText(detected["username"])
        if detected["password"]:
            self.password.setText(detected["password"])
        index = self.type.findData(detected["type"])
        if index >= 0:
            self.type.setCurrentIndex(index)
        if detected["type"] == pl.TYPE_M3U_URL:
            self.server.setText(text)
        if not self.name.text().strip() and detected["server"]:
            self.name.setText(detected["server"].split("//")[-1].split(":")[0])
        self.account = detected["account"]
        self.status.setText(detected["note"] or "")
        if self.account:
            self.status.setText(self._describe(self.account))

    @staticmethod
    def _describe(account: api.AccountInfo) -> str:
        bits = [account.status or "unknown"]
        if account.exp_date:
            bits.append("expires " + time.strftime("%Y-%m-%d", time.localtime(account.exp_date)))
        bits.append(f"{account.max_connections} connection"
                    + ("s" if account.max_connections != 1 else ""))
        if account.allowed_formats:
            bits.append("output: " + ", ".join(account.allowed_formats))
        return " · ".join(bits)

    def _test(self):
        server = self.server.text().strip()
        if not server:
            self.status.setText("Enter a server address first.")
            return
        self.test_button.setEnabled(False)
        self.status.setText("Connecting…")
        self._thread = QThread(self)
        self._worker = _ProbeWorker(server, self.username.text().strip(),
                                    self.password.text())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._probe_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _probe_done(self, account, error):
        self.test_button.setEnabled(True)
        if account is None:
            self.account = None
            self.status.setText(f"Could not connect: {error}")
            return
        self.account = account
        self.status.setText(self._describe(account))

    # -------------------------------------------------------------- saving

    def values(self) -> dict:
        return {
            "name": self.name.text().strip() or self.server.text().strip() or "Playlist",
            "type": self.type.currentData(),
            "server_url": self.server.text().strip(),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "epg_url": self.epg_url.text().strip(),
            "file_path": self.file_path.text().strip(),
            "auto_sync": self.auto_sync.isChecked(),
            "account": self.account,
        }

    def credentials_changed(self) -> bool:
        if self._original is None:
            return True
        values = self.values()
        return self._original != (
            values["server_url"], values["username"], values["password"]
        )

    def _save(self):
        values = self.values()
        if values["type"] == pl.TYPE_M3U_FILE:
            if not values["file_path"]:
                QMessageBox.warning(self, "Missing file", "Choose an M3U file.")
                return
        elif not values["server_url"]:
            QMessageBox.warning(self, "Missing server", "Enter the server address.")
            return
        if values["type"] == pl.TYPE_XTREAM and not (values["username"] and values["password"]):
            QMessageBox.warning(
                self, "Missing credentials",
                "Xtream playlists need a username and password.",
            )
            return
        self.accept()


class PlaylistManager(QDialog):
    """List of sources with add / edit / delete."""

    changed = Signal()

    def __init__(self, store: pl.PlaylistStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Playlists")
        self.setMinimumSize(520, 360)

        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.edit())
        layout.addWidget(self.list, 1)

        row = QHBoxLayout()
        for label, slot in (
            ("Add…", self.add), ("Edit…", self.edit),
            ("Delete", self.delete), ("Make active", self.make_active),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            if label == "Add…":
                button.setObjectName("primaryButton")
            row.addWidget(button)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)

        self.refresh()

    def refresh(self):
        self.list.clear()
        for playlist in self.store.all():
            label = playlist.name
            if playlist.is_active:
                label += "   ● active"
            if playlist.last_sync_at:
                label += "   · updated " + time.strftime(
                    "%d %b %H:%M", time.localtime(playlist.last_sync_at)
                )
            else:
                label += "   · never updated"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, playlist.id)
            self.list.addItem(item)

    def _selected_id(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def add(self):
        editor = PlaylistEditor(self)
        if editor.exec() != QDialog.Accepted:
            return
        values = editor.values()
        playlist = self.store.add(
            values["name"], values["type"], values["server_url"], values["username"],
            values["password"], values["epg_url"], values["file_path"], values["account"],
        )
        self.store.update(playlist.id, auto_sync=1 if values["auto_sync"] else 0)
        self.refresh()
        self.changed.emit()

    def edit(self):
        playlist_id = self._selected_id()
        if playlist_id is None:
            return
        playlist = self.store.get(playlist_id)
        editor = PlaylistEditor(self, playlist)
        if editor.exec() != QDialog.Accepted:
            return
        values = editor.values()
        credentials_changed = editor.credentials_changed()
        self.store.update(
            playlist_id,
            password=values["password"],
            name=values["name"], type=values["type"],
            server_url=values["server_url"], username=values["username"],
            epg_url=values["epg_url"], file_path=values["file_path"],
            auto_sync=1 if values["auto_sync"] else 0,
        )
        self.refresh()
        self.changed.emit()
        # Only a source change invalidates the cached catalog; renaming does not.
        if credentials_changed:
            answer = QMessageBox.question(
                self, "Update now?",
                "The server or credentials changed. Refresh this playlist's catalog now?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self.parent().start_sync(self.store.get(playlist_id))

    def delete(self):
        playlist_id = self._selected_id()
        if playlist_id is None:
            return
        playlist = self.store.get(playlist_id)
        downloads = self.store.db.scalar(
            "SELECT COUNT(*) FROM downloads WHERE playlist_id=? AND status='done'",
            (playlist_id,), 0,
        )

        box = QMessageBox(self)
        box.setWindowTitle("Delete playlist")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"Delete “{playlist.name}”?")
        detail = ("Its cached catalog, favourites and watch history will be removed.")
        if downloads:
            detail += (f"\n\n{downloads} downloaded file(s) will be KEPT on disk "
                       "unless you tick the box below.")
        box.setInformativeText(detail)
        checkbox = None
        if downloads:
            checkbox = QCheckBox("Also delete the downloaded files")
            box.setCheckBox(checkbox)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Cancel)
        if box.exec() != QMessageBox.Yes:
            return

        remove_files = bool(checkbox and checkbox.isChecked())
        paths = self.store.delete(playlist_id, delete_downloads=remove_files)
        if remove_files:
            from pathlib import Path

            for path in paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
        self.refresh()
        self.changed.emit()

    def make_active(self):
        playlist_id = self._selected_id()
        if playlist_id is None:
            return
        self.store.set_active(playlist_id)
        self.refresh()
        self.changed.emit()
