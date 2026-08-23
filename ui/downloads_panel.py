"""Downloads panel: progress, pause/resume/cancel, and offline playback.

Downloads yield to playback because the account allows a single connection, so
the panel says so plainly rather than looking stalled.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from core.downloads import (
    STATUS_ACTIVE, STATUS_DONE, STATUS_FAILED, STATUS_PAUSED, STATUS_QUEUED,
    DownloadManager, human_size,
)

STATUS_LABELS = {
    STATUS_QUEUED: "Queued",
    STATUS_ACTIVE: "Downloading",
    STATUS_PAUSED: "Paused",
    STATUS_DONE: "Completed",
    STATUS_FAILED: "Failed",
}


class DownloadsPanel(QWidget):
    playRequested = Signal(str, str)   # path, title

    def __init__(self, manager: DownloadManager, parent=None):
        super().__init__(parent)
        self.manager = manager

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        header = QHBoxLayout()
        title = QLabel("DOWNLOADS")
        title.setObjectName("sectionLabel")
        self.note = QLabel("")
        self.note.setObjectName("statusNote")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.note)
        layout.addLayout(header)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Title", "Progress", "Size", "Status"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(self._play_item)
        header_view = self.tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree, 1)

        buttons = QHBoxLayout()
        for label, slot in (
            ("Pause", self._pause), ("Resume", self._resume),
            ("Remove", self._remove), ("Open folder", self._open_folder),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._bars: dict[int, QProgressBar] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(900)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    # ------------------------------------------------------------- display

    def refresh(self):
        jobs = self.manager.jobs()
        existing = {}
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            existing[item.data(0, Qt.UserRole)] = item

        seen = set()
        for job in jobs:
            seen.add(job.id)
            item = existing.get(job.id)
            if item is None:
                item = QTreeWidgetItem(["", "", "", ""])
                item.setData(0, Qt.UserRole, job.id)
                self.tree.addTopLevelItem(item)
                bar = QProgressBar()
                bar.setTextVisible(False)
                bar.setFixedWidth(150)
                self.tree.setItemWidget(item, 1, bar)
                self._bars[job.id] = bar

            item.setText(0, job.title)
            bar = self._bars.get(job.id)
            if bar is not None:
                if job.total_bytes:
                    bar.setRange(0, 1000)
                    bar.setValue(int(1000 * job.done_bytes / job.total_bytes))
                else:
                    bar.setRange(0, 0) if job.status == STATUS_ACTIVE else bar.setRange(0, 1)

            if job.total_bytes:
                item.setText(2, f"{human_size(job.done_bytes)} / {human_size(job.total_bytes)}")
            else:
                item.setText(2, human_size(job.done_bytes))

            status = STATUS_LABELS.get(job.status, job.status)
            if job.status == STATUS_QUEUED and self.manager.blocked_by_playback:
                status = "Waiting for playback to finish"
            if job.error:
                status = job.error[:70]
            item.setText(3, status)
            item.setToolTip(3, job.error or job.dest_path)

        for job_id, item in existing.items():
            if job_id not in seen:
                index = self.tree.indexOfTopLevelItem(item)
                if index >= 0:
                    self.tree.takeTopLevelItem(index)
                self._bars.pop(job_id, None)

        if self.manager.blocked_by_playback:
            self.note.setText("Paused — playback is using your single connection")
        else:
            active = sum(1 for j in jobs if j.status in (STATUS_ACTIVE, STATUS_QUEUED))
            self.note.setText(f"{active} in queue" if active else "")

    # -------------------------------------------------------------- actions

    def _selected_job_id(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _pause(self):
        job_id = self._selected_job_id()
        if job_id is not None:
            self.manager.pause(job_id)
            self.refresh()

    def _resume(self):
        job_id = self._selected_job_id()
        if job_id is not None:
            self.manager.resume(job_id)
            self.refresh()

    def _remove(self):
        job_id = self._selected_job_id()
        if job_id is None:
            return
        job = next((j for j in self.manager.jobs() if j.id == job_id), None)
        if job is None:
            return
        if job.status == STATUS_DONE:
            answer = QMessageBox.question(
                self, "Remove download",
                f"Remove “{job.title}” from the list?\n\n"
                "Choose Yes to also delete the file from disk, or No to keep it.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.No,
            )
            if answer == QMessageBox.Cancel:
                return
            self.manager.cancel(job_id, delete_file=(answer == QMessageBox.Yes))
        else:
            self.manager.cancel(job_id, delete_file=True)
        self.refresh()

    def _play_item(self, item):
        job_id = item.data(0, Qt.UserRole)
        job = next((j for j in self.manager.jobs() if j.id == job_id), None)
        if job and job.status == STATUS_DONE and Path(job.dest_path).exists():
            self.playRequested.emit(job.dest_path, job.title)

    def _open_folder(self):
        import subprocess
        import sys

        path = self.manager.root_dir()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception:
            pass

    def _menu(self, point):
        item = self.tree.itemAt(point)
        if item is None:
            return
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play_item(item))
        menu.addAction("Pause", self._pause)
        menu.addAction("Resume", self._resume)
        menu.addSeparator()
        menu.addAction("Remove…", self._remove)
        menu.exec(self.tree.viewport().mapToGlobal(point))
