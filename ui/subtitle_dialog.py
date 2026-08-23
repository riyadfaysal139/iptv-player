"""VLSub, rebuilt natively.

The real VLSub is a Lua extension that only the full VLC *application* can load
— embedded libVLC has no Lua interpreter. So its window is rebuilt here, with
the same two buttons it has (search by hash, search by name), talking to the
same keyless XML-RPC endpoint. No API key, nothing to register.

Config holds an optional opensubtitles.org login, which only raises the daily
download quota, and the REST backend for anyone who would rather use a key.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QSpinBox, QVBoxLayout,
)

from core.db import app_dir
from core.playlists import get_secret, set_secret
from core.subtitles import (
    BACKEND_REST, BACKEND_VLSUB, LANGUAGES, SITE_URL, OpenSubtitlesClient,
    SubtitleError, SubtitleResult, hash_any,
)
from core.vlsub import SIGN_UP_URL, VLSubClient

SECRET_USER = "opensubtitles-user"
SECRET_PASSWORD = "opensubtitles-password"


def make_client(db):
    """The backend the user selected — VLSub unless they chose otherwise."""
    if (db.get_setting("subtitle_backend", BACKEND_VLSUB) or BACKEND_VLSUB) == BACKEND_REST:
        return OpenSubtitlesClient(db.get_setting("opensubtitles_key", "") or "")
    namespace = db.instance_id
    return VLSubClient(get_secret(SECRET_USER, namespace),
                       get_secret(SECRET_PASSWORD, namespace))


class _SearchWorker(QObject):
    """One search. `by_hash` picks which of VLSub's two buttons was pressed."""

    done = Signal(object, str)

    def __init__(self, client, languages, by_hash, url, query, season, episode):
        super().__init__()
        self.client = client
        self.languages = languages
        self.by_hash = by_hash
        self.url = url
        self.query = query
        self.season = season
        self.episode = episode

    def run(self):
        try:
            if self.by_hash:
                if not self.url:
                    self.done.emit(None, "Nothing is playing, so there is no file to fingerprint.")
                    return
                moviehash, size = hash_any(self.url)
                results = self.client.search_hash(moviehash, size, self.languages)
                note = "" if results else (
                    "No exact match for this file. Providers usually re-encode "
                    "their streams, so the fingerprint is not in the index — "
                    "Search by name instead."
                )
            else:
                results = self.client.search_name(
                    self.query, self.season, self.episode, self.languages)
                note = ""
            self.done.emit(results, note)
        except SubtitleError as exc:
            self.done.emit(None, str(exc))
        except Exception as exc:
            self.done.emit(None, f"{type(exc).__name__}: {exc}")


class _DownloadWorker(QObject):
    done = Signal(str, str)

    def __init__(self, client, result, dest_dir, base_name):
        super().__init__()
        self.client = client
        self.result = result
        self.dest_dir = dest_dir
        self.base_name = base_name

    def run(self):
        try:
            path = self.client.download(self.result, self.dest_dir, self.base_name)
            self.done.emit(str(path), "")
        except SubtitleError as exc:
            self.done.emit("", str(exc))
        except Exception as exc:
            self.done.emit("", f"{type(exc).__name__}: {exc}")


class SubtitleDialog(QDialog):
    subtitleChosen = Signal(str)

    def __init__(self, parent, db, title: str, url: str,
                 season: int = 0, episode: int = 0):
        super().__init__(parent)
        self.setWindowTitle("VLSub — subtitles")
        self.setMinimumSize(700, 540)
        self.db = db
        self.media_url = url
        self._results: list[SubtitleResult] = []
        self._thread = None
        self._worker = None

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.query = QLineEdit(title)
        self.query.setCursorPosition(0)     # show the start of a long title
        form.addRow("Title", self.query)

        numbers = QHBoxLayout()
        self.season = QSpinBox()
        self.season.setRange(0, 99)
        self.season.setValue(int(season or 0))
        self.season.setSpecialValueText("—")
        self.episode = QSpinBox()
        self.episode.setRange(0, 999)
        self.episode.setValue(int(episode or 0))
        self.episode.setSpecialValueText("—")
        numbers.addWidget(QLabel("Season"))
        numbers.addWidget(self.season)
        numbers.addSpacing(12)
        numbers.addWidget(QLabel("Episode"))
        numbers.addWidget(self.episode)
        numbers.addStretch(1)
        form.addRow("", numbers)

        languages = QHBoxLayout()
        self.language = QComboBox()
        self.language2 = QComboBox()
        self.language2.addItem("None", "")
        for label, iso3, iso2 in LANGUAGES:
            self.language.addItem(label, (iso3, iso2))
            self.language2.addItem(label, (iso3, iso2))
        self.language.setCurrentIndex(
            max(0, self.language.findText(db.get_setting("subtitle_language", "English") or "English")))
        self.language2.setCurrentIndex(
            max(0, self.language2.findText(db.get_setting("subtitle_language2", "None") or "None")))
        languages.addWidget(self.language, 1)
        languages.addWidget(QLabel("also"))
        languages.addWidget(self.language2, 1)
        form.addRow("Language", languages)
        layout.addLayout(form)

        # VLSub's two buttons, kept separate on purpose: which one found the
        # result is the single most useful thing to know about a subtitle.
        search_row = QHBoxLayout()
        self.hash_button = QPushButton("Search by hash")
        self.hash_button.setObjectName("primaryButton")
        self.hash_button.setToolTip(
            "Fingerprints the playing file and asks for subtitles matching it "
            "exactly — right timing, no guessing.\n\n"
            "Works best on downloaded files. A provider's own re-encode of a "
            "stream is often not in OpenSubtitles' index, in which case this "
            "finds nothing and Search by name is the one to use."
        )
        self.hash_button.clicked.connect(lambda: self.search(by_hash=True))
        self.name_button = QPushButton("Search by name")
        self.name_button.clicked.connect(lambda: self.search(by_hash=False))
        search_row.addWidget(self.hash_button)
        search_row.addWidget(self.name_button)
        search_row.addStretch(1)
        layout.addLayout(search_row)

        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda _: self.download_selected())
        layout.addWidget(self.results, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("statusNote")
        layout.addWidget(self.status)

        self.config_box = self._build_config()
        self.config_box.hide()
        layout.addWidget(self.config_box)

        bottom = QHBoxLayout()
        self.apply_button = QPushButton("Download selection")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self.download_selected)
        local = QPushButton("Load local file…")
        local.clicked.connect(self.load_local)
        config = QPushButton("Config")
        config.setCheckable(True)
        config.toggled.connect(self.config_box.setVisible)
        bottom.addWidget(self.apply_button)
        bottom.addWidget(local)
        bottom.addWidget(config)
        bottom.addSpacing(16)
        bottom.addWidget(QLabel("Delay"))
        self.delay = QSpinBox()
        self.delay.setRange(-60000, 60000)
        self.delay.setSingleStep(100)
        self.delay.setSuffix(" ms")
        self.delay.valueChanged.connect(
            lambda value: parent.player.set_subtitle_delay(value)
            if hasattr(parent, "player") else None
        )
        bottom.addWidget(self.delay)
        bottom.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        layout.addLayout(bottom)

        self.hash_button.setEnabled(bool(url))
        if not url:
            self.status.setText(
                "Play something first to search by its fingerprint — "
                "Search by name works either way."
            )

    # -------------------------------------------------------------- config

    def _build_config(self) -> QGroupBox:
        box = QGroupBox("Config")
        form = QFormLayout(box)
        namespace = self.db.instance_id

        self.backend = QComboBox()
        self.backend.addItem("VLSub — no API key needed", BACKEND_VLSUB)
        self.backend.addItem("OpenSubtitles REST API (needs a key)", BACKEND_REST)
        current = self.db.get_setting("subtitle_backend", BACKEND_VLSUB) or BACKEND_VLSUB
        self.backend.setCurrentIndex(1 if current == BACKEND_REST else 0)
        self.backend.currentIndexChanged.connect(self._backend_changed)
        form.addRow("Source", self.backend)

        self.username = QLineEdit(get_secret(SECRET_USER, namespace))
        self.username.setPlaceholderText("optional — raises the daily limit")
        self.password = QLineEdit(get_secret(SECRET_PASSWORD, namespace))
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("opensubtitles.org user", self.username)
        form.addRow("Password", self.password)

        account_note = QLabel(
            f"Searching needs no account at all. A free one from {SIGN_UP_URL} "
            "only raises how many subtitles you may download per day."
        )
        account_note.setWordWrap(True)
        account_note.setObjectName("statusNote")
        form.addRow("", account_note)

        self.api_key = QLineEdit(self.db.get_setting("opensubtitles_key", "") or "")
        self.api_key.setPlaceholderText(f"only for the REST source — {SITE_URL}")
        form.addRow("API key", self.api_key)

        self.remember_language = QCheckBox("Remember the chosen languages")
        self.remember_language.setChecked(True)
        form.addRow("", self.remember_language)

        save = QPushButton("Save")
        save.clicked.connect(self._save_config)
        form.addRow("", save)

        self._backend_changed()
        return box

    def _backend_changed(self, *_):
        rest = self.backend.currentData() == BACKEND_REST
        self.api_key.setEnabled(rest)
        for widget in (self.username, self.password):
            widget.setEnabled(not rest)

    def _save_config(self):
        namespace = self.db.instance_id
        self.db.set_setting("subtitle_backend", self.backend.currentData())
        self.db.set_setting("opensubtitles_key", self.api_key.text().strip())
        # Credentials go to the OS keychain, never into the database file.
        set_secret(SECRET_USER, self.username.text().strip(), namespace)
        set_secret(SECRET_PASSWORD, self.password.text(), namespace)
        if self.remember_language.isChecked():
            self.db.set_setting("subtitle_language", self.language.currentText())
            self.db.set_setting("subtitle_language2", self.language2.currentText())
        self.status.setText("Settings saved.")

    # -------------------------------------------------------------- search

    def _languages(self) -> list[str]:
        rest = (self.db.get_setting("subtitle_backend", BACKEND_VLSUB)
                or BACKEND_VLSUB) == BACKEND_REST
        index = 1 if rest else 0          # REST wants ISO 639-1, VLSub 639-2/B
        codes = [self.language.currentData()[index]]
        second = self.language2.currentData()
        if second:
            codes.append(second[index])
        return [code for code in codes if code]

    def search(self, by_hash: bool):
        if self._thread is not None and self._thread.isRunning():
            return
        self.results.clear()
        self._results = []
        self.status.setText("Fingerprinting the file…" if by_hash else "Searching…")
        self._set_busy(True)

        self._thread = QThread(self)
        self._worker = _SearchWorker(
            make_client(self.db), self._languages(), by_hash, self.media_url,
            self.query.text().strip(), self.season.value(), self.episode.value(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._search_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _set_busy(self, busy: bool):
        for button in (self.hash_button, self.name_button, self.apply_button):
            button.setEnabled(not busy)
        if not busy:
            self.hash_button.setEnabled(bool(self.media_url))

    def _search_done(self, results, message):
        self._set_busy(False)
        if results is None:
            self.status.setText(message)
            return
        self._results = results
        for result in results:
            item = QListWidgetItem(result.label)
            tips = []
            if result.from_hash:
                tips.append("Exact match for this file")
            if result.downloads:
                tips.append(f"{result.downloads:,} downloads")
            if result.file_name:
                tips.append(result.file_name)
            item.setToolTip("\n".join(tips))
            self.results.addItem(item)
        if not results:
            self.status.setText(message or "No subtitles found.")
        else:
            exact = sum(1 for result in results if result.from_hash)
            summary = f"{len(results)} result(s)"
            if exact:
                summary += f", {exact} matching this exact file"
            self.status.setText(f"{message} {summary}.".strip())
            self.results.setCurrentRow(0)

    # ------------------------------------------------------------ download

    def download_selected(self):
        row = self.results.currentRow()
        if row < 0 or row >= len(self._results):
            return
        if self._thread is not None and self._thread.isRunning():
            return
        result = self._results[row]
        self.status.setText("Downloading subtitle…")
        self._set_busy(True)

        self._thread = QThread(self)
        self._worker = _DownloadWorker(
            make_client(self.db), result, app_dir() / "subtitles",
            _safe(self.query.text() or result.file_name),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.done.connect(self._download_done)
        self._worker.done.connect(self._thread.quit)
        self._thread.start()

    def _download_done(self, path, error):
        self._set_busy(False)
        if not path:
            self.status.setText(error)
            return
        self.subtitleChosen.emit(path)
        self.status.setText(f"Applied {Path(path).name}")

    def load_local(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a subtitle file", "",
            "Subtitles (*.srt *.ass *.ssa *.sub *.vtt);;All files (*)",
        )
        if path:
            self.subtitleChosen.emit(path)
            self.status.setText(f"Applied {Path(path).name}")

    def closeEvent(self, event):
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)


def _safe(text: str) -> str:
    import re

    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text or "subtitle")[:120]
