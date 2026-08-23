"""Main window: three panes (categories | items | player) with TV/Movies/Series tabs."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStackedWidget,
    QTabBar, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core import sync as sync_mod
from core import vlc_setup
from core.db import Database
from core.downloads import DownloadManager, STATUS_DONE
from core.playlists import PlaylistStore, TYPE_XTREAM
from ui.category_tree import CategoryTree, build_groups
from ui.downloads_panel import DownloadsPanel
from ui.effects_dialog import EffectsDialog, load_saved_effects
from ui.models import (
    ROLE_ITEM, CatalogModel, ChannelDelegate, ImageCache, PosterDelegate,
)
from ui.player_widget import PlayerWidget
from ui.playlist_dialog import PlaylistEditor, PlaylistManager
from ui.subtitle_dialog import SubtitleDialog

TABS = [("live", "TV"), ("movie", "MOVIES"), ("series", "SERIES")]
EXPIRY_WARN_DAYS = 7
EPG_CACHE_SECONDS = 300        # now/next only moves every half hour
RESOLVED_TTL_SECONDS = 45      # CDN tokens verified good at 25 s; stay well inside
PREFETCH_DELAY_MS = 180        # let arrow-key scrolling settle before resolving
SEEK_STEP_SECONDS = 10         # VLC's short jump, on the left/right arrows
VOLUME_STEP = 5                # matches the wheel step over the volume slider


class SyncWorker(QObject):
    progress = Signal(str, int)
    finished = Signal(object)

    def __init__(self, db_path, playlist):
        super().__init__()
        self.db_path = db_path
        self.playlist = playlist
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        # A worker thread needs its own connection.
        db = Database(self.db_path)
        syncer = sync_mod.Syncer(
            db,
            progress=lambda m, p: self.progress.emit(m, p),
            should_cancel=lambda: self._cancel,
        )
        result = syncer.run(self.playlist)
        db.close()
        self.finished.emit(result)


class EpisodeWorker(QObject):
    finished = Signal(object, str)

    def __init__(self, db_path, playlist, series_id):
        super().__init__()
        self.db_path = db_path
        self.playlist = playlist
        self.series_id = series_id

    def run(self):
        db = Database(self.db_path)
        try:
            sync_mod.fetch_episodes(db, self.playlist, self.series_id)
            self.finished.emit(self.series_id, "")
        except Exception as exc:
            self.finished.emit(self.series_id, str(exc))
        finally:
            db.close()


class _BackgroundJob(QObject):
    """Runs one callable on a worker thread and emits the result.

    Used for the two things that used to block the GUI on a channel click: the
    EPG fetch (measured at 525 ms) and the portal redirect resolve (~700 ms).
    """

    done = Signal(object, object)   # (token, result) - result is None on failure

    def __init__(self, token, fn):
        super().__init__()
        self.token = token
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception:
            result = None
        self.done.emit(self.token, result)


class ThreadedTask:
    """Owns a QThread + worker pair and keeps them alive until finished.

    Without holding these references Python garbage-collects the QThread
    mid-flight and Qt aborts the process.
    """

    _live: set = set()

    @classmethod
    def run(cls, parent, token, fn, on_done):
        thread = QThread(parent)
        worker = _BackgroundJob(token, fn)
        worker.moveToThread(thread)
        holder = (thread, worker)
        cls._live.add(holder)
        thread.started.connect(worker.run)
        worker.done.connect(on_done)
        worker.done.connect(thread.quit)
        thread.finished.connect(lambda: cls._live.discard(holder))
        thread.start()
        return holder

    @classmethod
    def drain(cls, timeout_ms: int = 1500):
        """Let in-flight jobs finish before the app tears down.

        Qt aborts on a QThread destroyed while still running. Prev/Next can
        leave several EPG and redirect-resolve jobs in the air at once, which
        made that abort easy to hit on quit.
        """
        for thread, _worker in list(cls._live):
            try:
                thread.quit()
                thread.wait(timeout_ms)
            except RuntimeError:
                pass
        cls._live.clear()


class MainWindow(QMainWindow):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.store = PlaylistStore(db)
        self.playlist = self.store.active()
        self.kind = "live"
        self.images = ImageCache(self)
        self._sync_thread = None
        self._episode_thread = None
        self._current_item = None
        self._current_series = None
        self._counts_cache = {}
        self._client_cache = None
        self._epg_cache = {}
        self._epg_token = None
        self._resolved = {}
        self._fs_state = None
        self._fs_idle_ms = 0
        self._fs_last_cursor = None
        self._cursor_hidden = False
        self._effects_dialog = None
        self._browser_sizes = None
        self._fs_overlay = None

        self.downloads = DownloadManager(db)
        self.downloads.start()

        self.setWindowTitle("IPTV Player")
        self.resize(1500, 880)
        self._build_ui()
        self._build_menu()

        self.images.loaded.connect(lambda _: self.list_view.viewport().update())

        if self.playlist is None:
            QTimer.singleShot(200, self.first_run)
        else:
            self.reload_catalog()
            QTimer.singleShot(600, self.maybe_auto_sync)

        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(60_000)
        self._schedule_timer.timeout.connect(self._check_schedule)
        self._schedule_timer.start()
        self._last_scheduled_day = None

        # Resolving the portal redirect ahead of the click is what makes
        # playback start fast; debounced so arrow-key scrolling doesn't spam it.
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(PREFETCH_DELAY_MS)
        self._prefetch_timer.timeout.connect(self._prefetch_selected)
        sel = self.list_view.selectionModel()
        if sel is not None:
            sel.currentChanged.connect(lambda *_: self._prefetch_timer.start())

        # Polls the pointer while fullscreen so the controls can auto-hide.
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(400)
        self._fs_timer.timeout.connect(self._fs_tick)

        # Player keys are filtered at the application level so a focused list,
        # tree or search box cannot swallow them first. See eventFilter().
        QApplication.instance().installEventFilter(self)

    # ------------------------------------------------------------------ ui

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # top bar: playlist switcher + tabs
        top = QWidget()
        self._top_bar = top
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(10, 6, 10, 0)
        self.playlist_button = QPushButton("No playlist")
        self.playlist_button.clicked.connect(self.open_playlists)
        self.playlist_button.setFlat(True)
        top_layout.addWidget(self.playlist_button)

        self.account_label = QLabel("")
        self.account_label.setObjectName("statusNote")
        top_layout.addWidget(self.account_label)
        top_layout.addStretch(1)

        self.tab_bar = QTabBar()
        for _, label in TABS:
            self.tab_bar.addTab(label)
        self.tab_bar.currentChanged.connect(self._tab_changed)
        top_layout.addWidget(self.tab_bar)
        outer.addWidget(top)

        splitter = QSplitter(Qt.Horizontal)
        self._splitter = splitter
        outer.addWidget(splitter, 1)

        # ---- left: categories
        left = QWidget()
        self._left_pane = left
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 6, 0, 0)
        left_layout.setSpacing(0)
        self.category_search = QLineEdit()
        self.category_search.setPlaceholderText("Search Category")
        self.category_search.setClearButtonEnabled(True)
        self.category_search.textChanged.connect(self._category_filter)
        left_layout.addWidget(self.category_search)
        self.tree = CategoryTree()
        self.tree.nodeSelected.connect(self.on_node_selected)
        left_layout.addWidget(self.tree, 1)
        splitter.addWidget(left)

        # ---- middle: items (stacked with downloads + episodes)
        middle = QWidget()
        self._middle_pane = middle
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 6, 0, 0)
        middle_layout.setSpacing(0)
        self.item_search = QLineEdit()
        self.item_search.setPlaceholderText("Search Channels")
        self.item_search.setClearButtonEnabled(True)
        self.item_search.textChanged.connect(self._schedule_item_filter)
        middle_layout.addWidget(self.item_search)

        self.stack = QStackedWidget()
        self.model = CatalogModel(self)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.list_view.setMouseTracking(True)
        self.list_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._item_menu)
        self.list_view.doubleClicked.connect(self._activate_index)
        self.list_view.clicked.connect(self._clicked_index)
        self.channel_delegate = ChannelDelegate(self.images, lambda: self.model, self)
        self.poster_delegate = PosterDelegate(self.images, lambda: self.model, self)
        self.list_view.setItemDelegate(self.channel_delegate)
        self.stack.addWidget(self.list_view)

        self.episode_tree = QTreeWidget()
        self.episode_tree.setHeaderHidden(True)
        self.episode_tree.itemDoubleClicked.connect(self._play_episode)
        self.episode_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.episode_tree.customContextMenuRequested.connect(self._episode_menu)
        self.stack.addWidget(self.episode_tree)

        self.downloads_panel = DownloadsPanel(self.downloads)
        self.downloads_panel.playRequested.connect(self.play_local)
        self.stack.addWidget(self.downloads_panel)

        middle_layout.addWidget(self.stack, 1)
        splitter.addWidget(middle)

        # ---- right: player + EPG
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.player = PlayerWidget()
        self.player.errorOccurred.connect(self._on_player_error)
        self.player.playbackStarted.connect(lambda: self.downloads.set_playback_active(True))
        self.player.playbackStopped.connect(lambda: self.downloads.set_playback_active(False))
        self.player.endReached.connect(self._on_end_reached)
        self.player.positionChanged.connect(self._remember_position)
        self.player.fullscreenToggled.connect(self.toggle_fullscreen)
        self._connect_transport(self.player.bar)
        right_layout.addWidget(self.player, 3)

        info = QWidget()
        self._info_panel = info
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(12, 10, 12, 10)
        badge_row = QHBoxLayout()
        self.live_badge = QLabel("LIVE TV")
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.hide()
        self.now_title = QLabel("")
        self.now_title.setObjectName("nowPlayingTitle")
        self.now_title.setWordWrap(True)
        badge_row.addWidget(self.live_badge)
        badge_row.addWidget(self.now_title, 1)
        info_layout.addLayout(badge_row)

        buttons = QHBoxLayout()
        self.subs_button = QPushButton("Subtitles…")
        self.subs_button.clicked.connect(self.open_subtitles)
        self.download_button = QPushButton("⤓ Download")
        self.download_button.clicked.connect(self.download_current)
        self.vlc_button = QPushButton("Open in VLC")
        self.vlc_button.clicked.connect(self.open_in_vlc)
        self.vlc_button.setToolTip(
            "Open this stream in the full VLC application, where the original "
            "VLSub extension is available."
        )
        for button in (self.subs_button, self.download_button, self.vlc_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        info_layout.addLayout(buttons)

        self.epg_box = QVBoxLayout()
        info_layout.addLayout(self.epg_box)
        info_layout.addStretch(1)
        self._right_layout = right_layout
        right_layout.addWidget(info, 2)
        splitter.addWidget(right)

        splitter.setSizes([330, 660, 510])
        splitter.setStretchFactor(1, 1)

        self.status_progress = QProgressBar()
        self.status_progress.setMaximumWidth(180)
        self.status_progress.hide()
        self.statusBar().addPermanentWidget(self.status_progress)
        self.statusBar().showMessage("Ready")

        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(250)
        self._filter_timer.timeout.connect(self.apply_item_filter)

    def _connect_transport(self, bar):
        """Hook up the parts of the control bar the application owns.

        Everything libVLC-shaped the bar does itself; what arrives here is the
        handful of actions that need to know about the catalog, the panes or
        the download queue.
        """
        bar.previousRequested.connect(lambda: self.step_item(-1))
        bar.nextRequested.connect(lambda: self.step_item(1))
        bar.recordRequested.connect(self.download_current)
        bar.playlistToggled.connect(self.toggle_browser)
        bar.effectsRequested.connect(self.open_effects)
        bar.subtitleSearchRequested.connect(self.open_subtitles)
        bar.subtitleFileRequested.connect(self.open_subtitle_file)
        bar.message.connect(lambda text: self.statusBar().showMessage(text, 5000))
        # These run after the bar's own handlers, so the state is already
        # cycled by the time the value is read back for storing.
        bar.btn_loop.clicked.connect(
            lambda: self.db.set_setting("loop_mode", bar.loop_mode))
        bar.btn_shuffle.clicked.connect(
            lambda: self.db.set_setting("shuffle", "1" if bar.shuffle else "0"))
        bar.volume.sliderReleased.connect(
            lambda: self.db.set_setting("volume", str(bar.volume.value())))

    def _restore_player_state(self):
        """Bar state and saved effects, applied once the engine exists."""
        self.player.bar.restore(
            loop_mode=self.db.get_setting("loop_mode", "off") or "off",
            shuffle=self.db.get_bool("shuffle", False),
            advanced=self.db.get_bool("advanced_controls", True),
            volume=int(self.db.get_setting("volume", "85") or 85),
        )
        self.player.snapshot_dir = str(Path(self.downloads.root_dir()) / "Snapshots")
        if self.player.available:
            load_saved_effects(self.db, self.player)

    def _build_menu(self):
        bar = self.menuBar()

        playlist_menu = bar.addMenu("&Playlist")
        playlist_menu.addAction("Playlists…", self.open_playlists)
        playlist_menu.addAction("Add playlist…", self.add_playlist)
        playlist_menu.addSeparator()
        refresh = QAction("Refresh now", self)
        refresh.setShortcut(QKeySequence("Ctrl+R"))
        refresh.triggered.connect(lambda: self.start_sync(self.playlist))
        playlist_menu.addAction(refresh)
        playlist_menu.addSeparator()
        playlist_menu.addAction("Quit", self.close)

        view_menu = bar.addMenu("&View")
        self.flat_action = QAction("Flat category list", self, checkable=True)
        self.flat_action.setChecked(self.db.get_bool("flat_categories", False))
        self.flat_action.toggled.connect(self._toggle_flat)
        view_menu.addAction(self.flat_action)
        view_menu.addAction("Downloads", lambda: self.stack.setCurrentIndex(2))
        view_menu.addSeparator()

        # VLC hides the record/snapshot/A-B row behind the same menu item.
        self.advanced_action = QAction("Advanced controls", self, checkable=True)
        self.advanced_action.setChecked(self.db.get_bool("advanced_controls", True))
        self.advanced_action.toggled.connect(self._toggle_advanced)
        view_menu.addAction(self.advanced_action)

        browser = QAction("Show browser", self, checkable=True)
        browser.setChecked(True)
        browser.setShortcut(QKeySequence("Ctrl+L"))
        browser.toggled.connect(self.set_browser_visible)
        self.browser_action = browser
        view_menu.addAction(browser)

        effects = QAction("Adjustments and effects…", self)
        effects.triggered.connect(self.open_effects)
        view_menu.addAction(effects)

        fullscreen = QAction("Fullscreen video", self)
        fullscreen.setShortcut(QKeySequence("F"))
        fullscreen.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(fullscreen)

        settings_menu = bar.addMenu("&Settings")
        self.hw_action = QAction("Hardware decoding", self, checkable=True)
        self.hw_action.setChecked(self.db.get_bool("hardware_decoding", True))
        self.hw_action.toggled.connect(self._toggle_hw)
        settings_menu.addAction(self.hw_action)

        self.dl_action = QAction("Allow downloads while playing", self, checkable=True)
        self.dl_action.setChecked(self.db.get_bool("download_while_playing", False))
        self.dl_action.setToolTip(
            "Your account allows one connection; downloading while watching "
            "usually fails."
        )
        self.dl_action.toggled.connect(self.downloads.set_allow_while_playing)
        settings_menu.addAction(self.dl_action)

        self.autosync_action = QAction("Update catalog daily", self, checkable=True)
        self.autosync_action.setChecked(True)
        self.autosync_action.toggled.connect(self._toggle_autosync)
        settings_menu.addAction(self.autosync_action)

        settings_menu.addAction("Download folder…", self.choose_download_dir)
        settings_menu.addAction("Subtitles…", self.open_subtitles)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction("About", self.show_about)

        self.player.set_hardware_decoding(self.db.get_bool("hardware_decoding", True))
        self._restore_player_state()

    # ------------------------------------------------------- playlist setup

    def first_run(self):
        QMessageBox.information(
            self, "Welcome",
            "Add your IPTV provider to get started.\n\n"
            "You can paste a portal address or a full M3U link — the details "
            "fill in automatically.",
        )
        self.add_playlist()

    def add_playlist(self):
        editor = PlaylistEditor(self)
        if editor.exec() != QDialog.Accepted:
            return
        values = editor.values()
        playlist = self.store.add(
            values["name"], values["type"], values["server_url"], values["username"],
            values["password"], values["epg_url"], values["file_path"], values["account"],
        )
        self.store.update(playlist.id, auto_sync=1 if values["auto_sync"] else 0)
        self.playlist = self.store.active()
        self.reload_catalog()
        self.start_sync(self.playlist)

    def open_playlists(self):
        dialog = PlaylistManager(self.store, self)
        dialog.changed.connect(self._playlists_changed)
        dialog.exec()

    def _playlists_changed(self):
        self.invalidate_counts()
        self.invalidate_client()
        self.playlist = self.store.active()
        self.reload_catalog()

    # ------------------------------------------------------------- syncing

    def maybe_auto_sync(self):
        if self.playlist and self.store.due_for_sync(self.playlist):
            self.start_sync(self.playlist)
        else:
            self._warn_if_expiring()

    def _check_schedule(self):
        """Fire the daily refresh at the configured local time."""
        if not self.playlist or not self.playlist.auto_sync:
            return
        now = time.localtime()
        today = (now.tm_year, now.tm_yday)
        target = self.playlist.sync_at_time or "04:00"
        try:
            hour, minute = (int(x) for x in target.split(":"))
        except ValueError:
            hour, minute = 4, 0
        if now.tm_hour == hour and now.tm_min == minute and self._last_scheduled_day != today:
            self._last_scheduled_day = today
            self.start_sync(self.playlist)

    def start_sync(self, playlist):
        if playlist is None or (self._sync_thread and self._sync_thread.isRunning()):
            return
        if playlist.type == TYPE_XTREAM and not playlist.password:
            QMessageBox.warning(
                self, "Missing password",
                f"No stored password for “{playlist.name}”. Edit the playlist to set it.",
            )
            return
        self.status_progress.show()
        self.status_progress.setValue(0)
        self.statusBar().showMessage("Updating catalog…")

        self._sync_thread = QThread(self)
        self._sync_worker = SyncWorker(self.db.path, playlist)
        self._sync_worker.moveToThread(self._sync_thread)
        self._sync_thread.started.connect(self._sync_worker.run)
        self._sync_worker.progress.connect(self._sync_progress)
        self._sync_worker.finished.connect(self._sync_finished)
        self._sync_worker.finished.connect(self._sync_thread.quit)
        self._sync_thread.start()

    def _sync_progress(self, message, percent):
        self.statusBar().showMessage(message)
        if percent >= 0:
            self.status_progress.setValue(percent)

    def _sync_finished(self, result):
        self.invalidate_counts()
        self.invalidate_client()
        self.status_progress.hide()
        if result.error:
            # A background refresh should not throw dialogs at the user.
            self.statusBar().showMessage(f"Update failed — {result.error}", 12000)
        else:
            self.statusBar().showMessage(result.summary(), 10000)
        self.playlist = self.store.get(self.playlist.id) if self.playlist else None
        self.reload_catalog()
        self._warn_if_expiring()

    def _warn_if_expiring(self):
        if not self.playlist or not self.playlist.exp_date:
            return
        days = (self.playlist.exp_date - time.time()) / 86400
        if 0 < days <= EXPIRY_WARN_DAYS:
            self.account_label.setText(
                f"⚠ Subscription expires in {int(days)} day(s)"
            )
            self.account_label.setObjectName("warningNote")
        elif days <= 0:
            self.account_label.setText("⚠ Subscription expired")
            self.account_label.setObjectName("warningNote")
        self.account_label.style().unpolish(self.account_label)
        self.account_label.style().polish(self.account_label)

    # ------------------------------------------------------------ catalogue

    def reload_catalog(self):
        if self.playlist is None:
            self.playlist_button.setText("Add a playlist…")
            self.tree.load([], {})
            self.model.set_rows([], self.kind)
            return

        self.playlist_button.setText(f"▾ {self.playlist.name}")
        if self.playlist.last_sync_at:
            stamp = time.strftime("%d %b %H:%M", time.localtime(self.playlist.last_sync_at))
            self.account_label.setText(f"updated {stamp}")
        else:
            self.account_label.setText("never updated")
        self.autosync_action.setChecked(self.playlist.auto_sync)

        rows = self.db.query(
            "SELECT category_id, name, group_name, sub_name, item_count FROM categories "
            "WHERE playlist_id=? AND kind=? ORDER BY group_name, sub_name",
            (self.playlist.id, self.kind),
        )
        groups = build_groups(rows)

        totals = self._sidebar_counts()
        orphans = totals.pop("_orphans", 0)
        if orphans:
            groups.append(("Uncategorized", orphans, [("__orphans__", "Uncategorized", orphans)]))

        self.tree.set_flat(self.db.get_bool("flat_categories", False))
        self.tree.load(groups, totals)
        self.tree.select_first()

    def _sidebar_counts(self) -> dict:
        """Pinned-row counts, cached per (playlist, kind).

        These were six separate aggregate queries costing ~88 ms on every tab
        switch, for numbers that only move when the catalog syncs or the user
        favourites/downloads something. `invalidate_counts()` clears them.
        """
        key = (self.playlist.id, self.kind)
        cached = self._counts_cache.get(key)
        if cached is not None:
            return dict(cached)

        pid, kind = key
        cutoff = int(time.time()) - 14 * 86400
        totals = {
            "all": self.db.scalar(
                "SELECT COUNT(*) FROM streams WHERE playlist_id=? AND kind=? AND available=1",
                (pid, kind), 0),
            "favourites": self.db.scalar(
                "SELECT COUNT(*) FROM favourites WHERE playlist_id=? AND kind=?",
                (pid, kind), 0),
            "continue": self.db.scalar(
                "SELECT COUNT(*) FROM history WHERE playlist_id=? AND kind=?",
                (pid, kind), 0),
            "recent": self.db.scalar(
                "SELECT COUNT(*) FROM streams WHERE playlist_id=? AND kind=? "
                "AND available=1 AND added > ?", (pid, kind, cutoff), 0),
            "downloads": self.db.scalar(
                "SELECT COUNT(*) FROM downloads WHERE playlist_id=? AND status='done'",
                (pid,), 0),
            # Items whose category is missing from the provider's own category
            # list; they must stay reachable via an Uncategorized node.
            "_orphans": self.db.scalar(
                "SELECT COUNT(*) FROM streams s WHERE s.playlist_id=? AND s.kind=? "
                "AND s.available=1 AND NOT EXISTS(SELECT 1 FROM categories c "
                "WHERE c.playlist_id=s.playlist_id AND c.kind=s.kind "
                "AND c.category_id=s.category_id)", (pid, kind), 0),
        }
        self._counts_cache[key] = dict(totals)
        return totals

    def invalidate_counts(self):
        self._counts_cache.clear()

    def client(self):
        """Cached Xtream client for the active playlist.

        Building one reads the password from the OS keychain, which costs
        ~12 ms; it was being rebuilt on every EPG load, prefetch and play.
        Cleared whenever the active playlist changes.
        """
        if self._client_cache is None and self.playlist is not None:
            self._client_cache = self.playlist.client()
        return self._client_cache

    def invalidate_client(self):
        self._client_cache = None

    def _tab_changed(self, index):
        self.kind = TABS[index][0]
        self.item_search.setPlaceholderText(
            "Search Channels" if self.kind == "live" else "Search Titles"
        )
        self.list_view.setItemDelegate(
            self.channel_delegate if self.kind == "live" else self.poster_delegate
        )
        self.list_view.setViewMode(
            QListView.ListMode if self.kind == "live" else QListView.IconMode
        )
        self.list_view.setObjectName("" if self.kind == "live" else "posterGrid")
        if self.kind != "live":
            self.list_view.setResizeMode(QListView.Adjust)
            self.list_view.setSpacing(4)
        else:
            self.list_view.setSpacing(0)
        self.stack.setCurrentIndex(0)
        self.reload_catalog()

    def _category_filter(self, text):
        self.tree.set_filter(text)

    def _toggle_flat(self, flat):
        self.db.set_setting("flat_categories", "1" if flat else "0")
        self.tree.set_flat(flat)

    def on_node_selected(self, node_type, payload):
        if self.playlist is None:
            return
        if node_type == "downloads":
            self.stack.setCurrentIndex(2)
            self.downloads_panel.refresh()
            return
        self.stack.setCurrentIndex(0)
        self._current_filter = (node_type, payload)
        self.apply_item_filter()

    def _schedule_item_filter(self):
        self._filter_timer.start()

    def apply_item_filter(self):
        if self.playlist is None:
            return
        node_type, payload = getattr(self, "_current_filter", ("all", None))
        search = self.item_search.text().strip().lower()
        params = [self.playlist.id, self.kind]
        where = ["s.playlist_id=?", "s.kind=?"]
        joins = ""
        order = "s.name_folded"

        if node_type == "category":
            if payload == "__orphans__":
                where.append(
                    "NOT EXISTS(SELECT 1 FROM categories c WHERE c.playlist_id=s.playlist_id "
                    "AND c.kind=s.kind AND c.category_id=s.category_id)"
                )
            else:
                where.append("s.category_id=?")
                params.append(payload)
        elif node_type == "group":
            where.append(
                "s.category_id IN (SELECT category_id FROM categories WHERE "
                "playlist_id=? AND kind=? AND group_name=?)"
            )
            params.extend([self.playlist.id, self.kind, payload])
        elif node_type == "favourites":
            joins = ("JOIN favourites f ON f.playlist_id=s.playlist_id "
                     "AND f.kind=s.kind AND f.stream_id=s.stream_id")
        elif node_type == "continue":
            joins = ("JOIN history h ON h.playlist_id=s.playlist_id "
                     "AND h.kind=s.kind AND h.stream_id=s.stream_id")
            order = "h.watched_at DESC"
        elif node_type == "recent":
            where.append("s.added > ?")
            params.append(int(time.time()) - 14 * 86400)
            order = "s.added DESC"

        if node_type not in ("favourites",):
            where.append("s.available=1")
        # The provider ships a few nameless records; they render as blank cells.
        where.append("s.name <> ''")
        if search:
            where.append("s.name_folded LIKE ?")
            params.append(f"%{search}%")

        # Live channels have a provider-assigned running order; keep it.
        if self.kind == "live" and order == "s.name_folded":
            order = "s.num, s.name_folded"

        columns = ("s.stream_id, s.name, s.icon, s.rating, s.container_extension, "
                   "s.num, s.available, s.epg_channel_id, s.added")
        base = f"SELECT {columns} FROM streams s {joins} WHERE {' AND '.join(where)}"

        # In aggregate views the same title appears once per category it belongs
        # to (55,724 movie entries cover only 33,916 distinct titles), so show
        # one representative — the best-rated. Category views need no dedupe.
        if node_type in ("all", "recent") or (search and node_type not in ("category", "group")):
            # dup_rank is computed once per sync (see core/sync.py), so this is
            # an ordered index scan instead of a ROW_NUMBER() over 55k rows.
            where.append("s.dup_rank=1")
            sql = (
                f"SELECT {columns} FROM streams s {joins} "
                f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 60000"
            )
        else:
            sql = f"{base} ORDER BY {order} LIMIT 60000"
        rows = [tuple(r) for r in self.db.query(sql, tuple(params))]
        favourites = {
            r["stream_id"] for r in self.db.query(
                "SELECT stream_id FROM favourites WHERE playlist_id=? AND kind=?",
                (self.playlist.id, self.kind),
            )
        }
        self.model.set_rows(rows, self.kind, favourites)
        self.statusBar().showMessage(f"{len(rows):,} items")

    # ------------------------------------------------------------ playback

    def _clicked_index(self, index):
        # Toggle favourite when the heart is hit; otherwise select.
        if self.kind != "live":
            return
        rect = self.list_view.visualRect(index)
        position = self.list_view.viewport().mapFromGlobal(self.cursor().pos())
        if ChannelDelegate.heart_rect(rect).contains(position):
            self.toggle_favourite(index)

    def _activate_index(self, index):
        row = index.data(ROLE_ITEM)
        if row is None:
            return
        if self.kind == "series":
            self.open_series(row)
        else:
            self.play_item(row)

    def play_item(self, row):
        if not self.playlist or not self.playlist.is_xtream:
            QMessageBox.information(
                self, "Playback",
                "This playlist type does not provide direct stream URLs.",
            )
            return
        self._current_item = row
        client = self.client()
        stream_id, name, _, _, ext = row[0], row[1], row[2], row[3], row[4]
        is_live = self.kind == "live"
        url = client.url_for("live" if is_live else "movie", stream_id, ext or None)

        resume = 0
        if not is_live:
            record = self.db.one(
                "SELECT position_secs, duration_secs FROM history WHERE playlist_id=? "
                "AND kind=? AND stream_id=? AND episode_id=''",
                (self.playlist.id, self.kind, stream_id),
            )
            if record and record["position_secs"] > 60:
                minutes = record["position_secs"] // 60
                answer = QMessageBox.question(
                    self, "Resume?",
                    f"Continue “{name}” from {minutes} min?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if answer == QMessageBox.Yes:
                    resume = record["position_secs"]

        self.now_title.setText(name)
        self.live_badge.setVisible(is_live)
        self._play_original_url = url
        # Start the stream first; the guide fills in asynchronously behind it.
        self.player.play(self._playable_url(url), name, is_live=is_live,
                         resume_secs=resume, immediate=True)
        self._load_epg(row)

    def play_local(self, path: str, title: str):
        """Downloaded files play from disk and use no connection."""
        self._current_item = None
        self.now_title.setText(title)
        self.live_badge.hide()
        self.player.play(Path(path).absolute().as_uri(), title, is_live=False)
        self._clear_epg()

    def _on_player_error(self, message):
        self.statusBar().showMessage(message, 8000)

    def _on_end_reached(self):
        """Honour the bar's loop and random settings, as VLC does."""
        bar = self.player.bar
        # endReached arrives from a libVLC thread through a queued signal, and
        # libVLC must not be re-entered from its own callback — the extra
        # event-loop hop guarantees it is well clear.
        if bar.loop_mode == "one":
            QTimer.singleShot(0, self.replay_current)
        elif bar.loop_mode == "all" or bar.shuffle:
            QTimer.singleShot(0, lambda: self.step_item(1))
        else:
            self.player.stop()

    def replay_current(self):
        if self._current_item is not None and self.stack.currentIndex() != 1:
            self.play_item(self._current_item)
        elif self.player.current_url:
            self.player.play(self.player.current_url, self.player.current_title,
                             self.player.is_live)

    # --------------------------------------------------------- prev / next

    def step_item(self, delta: int):
        """The bar's ⏮ / ⏭, over whichever list the user is looking at."""
        if self.stack.currentIndex() == 1:
            self._step_episode(delta)
            return
        count = self.model.rowCount()
        if not count:
            return
        current = self.list_view.currentIndex()
        row = current.row() if current.isValid() else -1
        if self.player.bar.shuffle and count > 1:
            import random

            choice = row
            while choice == row:
                choice = random.randrange(count)
            row = choice
        else:
            row = (row + delta) % count

        index = self.model.index(row, 0)
        self.list_view.setCurrentIndex(index)
        self.list_view.scrollTo(index, QListView.PositionAtCenter)
        item = index.data(ROLE_ITEM)
        if item is None:
            return
        if self.kind == "series":
            self.open_series(item)
        else:
            self.play_item(item)

    def _step_episode(self, delta: int):
        """Within a series, step across season boundaries rather than stopping."""
        tree = self.episode_tree
        episodes = [
            tree.topLevelItem(season).child(index)
            for season in range(tree.topLevelItemCount())
            for index in range(tree.topLevelItem(season).childCount())
        ]
        if not episodes:
            return
        current = tree.currentItem()
        position = episodes.index(current) if current in episodes else -1
        if self.player.bar.shuffle and len(episodes) > 1:
            import random

            choice = position
            while choice == position:
                choice = random.randrange(len(episodes))
            position = choice
        else:
            position = (position + delta) % len(episodes)
        target = episodes[position]
        tree.setCurrentItem(target)
        self._play_episode(target)

    # ------------------------------------------------------------- browser

    def toggle_browser(self):
        self.browser_action.setChecked(not self.browser_action.isChecked())

    def set_browser_visible(self, visible: bool):
        """The bar's ☰ button — VLC's "show playlist"."""
        if self._fs_state:
            return          # fullscreen owns pane visibility
        if not visible and self._left_pane.isVisible():
            self._browser_sizes = self._splitter.sizes()
        self._left_pane.setVisible(visible)
        self._middle_pane.setVisible(visible)
        if visible and self._browser_sizes:
            self._splitter.setSizes(self._browser_sizes)

    def _toggle_advanced(self, enabled: bool):
        self.db.set_setting("advanced_controls", "1" if enabled else "0")
        self.player.bar.set_advanced_visible(enabled)

    def open_effects(self):
        if not self.player.available:
            self.statusBar().showMessage("VLC is not available", 4000)
            return
        if self._effects_dialog is None:
            self._effects_dialog = EffectsDialog(self, self.db, self.player)
        self._effects_dialog.show()
        self._effects_dialog.raise_()
        self._effects_dialog.activateWindow()

    def _remember_position(self, fraction, position, duration):
        if not self.playlist or not self._current_item or self.kind == "live":
            return
        if duration <= 0 or position <= 0:
            return
        episode_id = self._current_episode_id() or ""
        self.db.execute(
            "INSERT INTO history(playlist_id, kind, stream_id, episode_id, position_secs,"
            " duration_secs, watched_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(playlist_id, kind, stream_id, episode_id) DO UPDATE SET "
            "position_secs=excluded.position_secs, duration_secs=excluded.duration_secs,"
            " watched_at=excluded.watched_at",
            (self.playlist.id, self.kind, str(self._current_item[0]), episode_id,
             int(position), int(duration), int(time.time())),
        )

    def _current_episode_id(self):
        item = self.episode_tree.currentItem()
        if item and item.data(0, Qt.UserRole):
            return str(item.data(0, Qt.UserRole).get("episode_id", ""))
        return ""

    # --------------------------------------------------------- fullscreen

    def toggle_fullscreen(self):
        if self._fs_state:
            self.exit_fullscreen()
        else:
            self.enter_fullscreen()

    def enter_fullscreen(self):
        """Give the whole screen to the video.

        The video surface is deliberately NOT reparented into a new window:
        libVLC is bound to this widget's native handle, and moving it mid-
        playback loses the drawable and leaves a black screen. Instead the
        surrounding chrome is hidden and the window goes fullscreen, so the
        surface keeps its handle and simply grows to fill the display.
        """
        if self._fs_state or not self.player.available:
            return
        self._fs_state = {
            "sizes": self._splitter.sizes(),
            "left": self._left_pane.isVisible(),
            "middle": self._middle_pane.isVisible(),
            "info": self._info_panel.isVisible(),
            "top": self._top_bar.isVisible(),
            # On macOS the menu bar is native (not a child widget), so it has
            # no visibility to toggle; the OS hides it in fullscreen itself.
            "menubar": None if self.menuBar().isNativeMenuBar()
                       else self.menuBar().isVisible(),
            "statusbar": self.statusBar().isVisible(),
            "maximized": self.isMaximized(),
            "geometry": self.saveGeometry(),
        }
        for widget in (self._left_pane, self._middle_pane, self._info_panel,
                       self._top_bar):
            widget.hide()
        if not self.menuBar().isNativeMenuBar():
            self.menuBar().setVisible(False)
        self.statusBar().setVisible(False)
        self.showFullScreen()
        self._float_bar()
        # The overlay is a separate top-level and takes activation with it, so
        # claim it back and put focus on the video — otherwise nothing holds
        # focus at all and the keyboard has no obvious owner.
        self.activateWindow()
        self.raise_()
        self.player.surface.setFocus(Qt.OtherFocusReason)

        self._fs_idle_ms = 0
        self._fs_last_cursor = QCursor.pos()
        self._fs_timer.start()

    def _float_bar(self):
        """Put the controls over the video, the way VLC's fullscreen does.

        A separate top-level window is the only thing that reliably draws above
        libVLC's native video view — a sibling widget in the same window does
        not, because the video is a native child window. Safe to do because the
        bar owns no video handle. Set `fullscreen_floating_bar` to 0 to keep the
        old docked behaviour if a window manager will not honour stay-on-top.
        """
        if not self.db.get_bool("fullscreen_floating_bar", True):
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return

        area = screen.geometry()
        width = min(1100, area.width() - 80)
        left = area.x() + (area.width() - width) // 2

        overlay = QWidget(None, Qt.Tool | Qt.FramelessWindowHint
                          | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        overlay.setObjectName("fullscreenBar")
        overlay.setAttribute(Qt.WA_ShowWithoutActivating, True)
        overlay.setWindowOpacity(0.96)
        # Geometry before children: adding the bar first lets the layout size
        # the window to the bar's current (fullscreen-wide) hint, and Qt then
        # creates the native window off-screen on a multi-monitor desktop.
        overlay.setGeometry(left, area.y() + area.height() - 140, width, 112)

        layout = QVBoxLayout(overlay)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.player.detach_bar())
        overlay.setFixedWidth(width)

        overlay.show()
        overlay.adjustSize()
        overlay.move(left, area.y() + area.height() - overlay.height() - 28)
        self._fs_overlay = overlay

    def _dock_bar(self):
        if self._fs_overlay is None:
            return
        self._fs_overlay.layout().removeWidget(self.player.bar)
        self.player.attach_bar()
        self._fs_overlay.close()
        self._fs_overlay.deleteLater()
        self._fs_overlay = None

    def _set_controls_visible(self, visible: bool):
        if self._fs_overlay is not None:
            self._fs_overlay.setVisible(visible)
        else:
            self.player.set_chrome_visible(visible)

    def exit_fullscreen(self):
        state = self._fs_state
        if not state:
            return
        self._fs_timer.stop()
        self._restore_cursor()
        self._dock_bar()
        self.player.set_chrome_visible(True)

        self._left_pane.setVisible(state["left"])
        self._middle_pane.setVisible(state["middle"])
        self._info_panel.setVisible(state["info"])
        self._top_bar.setVisible(state["top"])
        if state["menubar"] is not None:
            self.menuBar().setVisible(state["menubar"])
        self.statusBar().setVisible(state["statusbar"])

        self.showNormal()
        self.restoreGeometry(state["geometry"])
        if state["maximized"]:
            self.showMaximized()
        self._splitter.setSizes(state["sizes"])
        self._fs_state = None
        self.activateWindow()
        self.player.surface.setFocus(Qt.OtherFocusReason)

    def _fs_tick(self):
        """Hide the controls and pointer once the mouse goes still.

        Cursor position is polled rather than tracked through mouse events:
        libVLC's own view sits on top of the surface and can swallow them, so
        an event-driven approach would leave the bar stuck hidden.
        """
        if not self._fs_state:
            return
        position = QCursor.pos()
        if position != self._fs_last_cursor:
            self._fs_last_cursor = position
            self._fs_idle_ms = 0
            self._set_controls_visible(True)
            self._restore_cursor()
            return

        # Leave the bar up while the pointer is over it, or it vanishes under
        # the cursor mid-drag of the seek slider.
        if self._fs_overlay is not None and \
                self._fs_overlay.geometry().contains(position):
            self._fs_idle_ms = 0
            return

        self._fs_idle_ms += self._fs_timer.interval()
        if self._fs_idle_ms >= 2500:
            self._set_controls_visible(False)
            if not self._cursor_hidden:
                QApplication.setOverrideCursor(Qt.BlankCursor)
                self._cursor_hidden = True

    def _restore_cursor(self):
        if self._cursor_hidden:
            QApplication.restoreOverrideCursor()
            self._cursor_hidden = False

    # ------------------------------------------------------------- keyboard

    def eventFilter(self, obj, event):
        """VLC's keys, applied before a focused list can swallow them.

        `keyPressEvent` on the window is not enough: it only runs once no child
        has consumed the event, and QListView, QTreeWidget and QLineEdit all
        consume Space. Filtering at the application level is what makes the
        shortcuts work no matter where focus happens to be.
        """
        if event.type() == QEvent.KeyPress and self._handle_player_key(event):
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _is_typing() -> bool:
        """True when a text field has focus, in which case typing wins.

        Space has to insert a space in the search box, and the arrows have to
        move the caret, so the player must keep its hands off entirely.
        """
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
            return True
        return isinstance(focused, QComboBox) and focused.isEditable()

    def _arrows_go_to_player(self) -> bool:
        """Who owns the arrow keys right now.

        Fullscreen has no channel list, so playback always wins there. Windowed,
        the video takes them only while it holds focus - which it is given the
        moment playback starts - leaving arrow channel-surfing intact whenever
        the list is what you are actually looking at.
        """
        if self._fs_state:
            return True
        focused = QApplication.focusWidget()
        if focused is None:
            return False
        return focused is self.player.surface or self.player.isAncestorOf(focused)

    def _handle_player_key(self, event) -> bool:
        """Returns True when the key was consumed as a player command."""
        if event.isAutoRepeat() and event.key() in (Qt.Key_Space, Qt.Key_M):
            return False
        key = event.key()

        if key == Qt.Key_Escape and self._fs_state:
            self.exit_fullscreen()
            return True
        # Dialogs (VLSub, Effects) keep their own keyboard entirely — but the
        # test is "does another window own the focus", not "is this window
        # active". Going fullscreen leaves focusWidget() None and the window
        # reporting inactive, because the floating control bar is a separate
        # top-level; an activeWindow() test silently swallowed every key there.
        focused = QApplication.focusWidget()
        if focused is not None:
            owner = focused.window()
            if owner is not self and owner is not self._fs_overlay:
                return False
        elif QApplication.activeModalWidget() is not None:
            return False
        if not self.player.available or self._is_typing():
            return False

        if key == Qt.Key_Space:
            self.player.toggle_pause()
            self._key_feedback("Pause" if not self.player.is_playing() else "Play")
            return True
        if key == Qt.Key_M:
            muted = self.player.bar.toggle_mute()
            self._key_feedback("Muted" if muted else "Unmuted")
            return True
        if key in (Qt.Key_BracketRight, Qt.Key_BracketLeft, Qt.Key_Equal):
            self._step_rate(key)
            return True

        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            if not self._arrows_go_to_player():
                return False
            if key in (Qt.Key_Left, Qt.Key_Right):
                step = SEEK_STEP_SECONDS if key == Qt.Key_Right else -SEEK_STEP_SECONDS
                if self.player.seek_relative(step):
                    self._key_feedback(f"Seek {step:+d}s")
                else:
                    self._key_feedback("Live stream — cannot seek")
            else:
                step = VOLUME_STEP if key == Qt.Key_Up else -VOLUME_STEP
                self._key_feedback(f"Volume {self.player.bar.nudge_volume(step)}%")
                self.db.set_setting("volume", str(self.player.bar.volume.value()))
            return True
        return False

    def _step_rate(self, key):
        rates = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 3.0, 4.0]
        if key == Qt.Key_Equal:
            rate = 1.0
        else:
            current = min(range(len(rates)),
                          key=lambda i: abs(rates[i] - self.player.rate()))
            step = 1 if key == Qt.Key_BracketRight else -1
            rate = rates[max(0, min(len(rates) - 1, current + step))]
        self.player.bar.apply_rate(rate)
        self._key_feedback(f"Speed {rate:.2f}x")

    def _key_feedback(self, message: str):
        """Say what the key did — otherwise there is no telling it worked.

        Fullscreen hides the status bar, so the floating control bar is popped
        back up instead: it already shows the position and volume the key just
        changed, and fades out again on its own.
        """
        if self._fs_state:
            self._fs_idle_ms = 0
            self._set_controls_visible(True)
            self._restore_cursor()
        else:
            self.statusBar().showMessage(message, 1500)

    # -------------------------------------------------------------- series

    def open_series(self, row):
        self._current_item = row
        self._current_series = row
        self.episode_tree.clear()
        self.episode_tree.addTopLevelItem(QTreeWidgetItem(["Loading episodes…"]))
        self.stack.setCurrentIndex(1)
        self.now_title.setText(row[1])

        cached = self.db.query(
            "SELECT season, episode_num, episode_id, title, container_extension "
            "FROM series_episodes WHERE playlist_id=? AND series_id=? "
            "ORDER BY season, episode_num",
            (self.playlist.id, str(row[0])),
        )
        if cached:
            self._render_episodes(cached)
            return

        self._episode_thread = QThread(self)
        self._episode_worker = EpisodeWorker(self.db.path, self.playlist, row[0])
        self._episode_worker.moveToThread(self._episode_thread)
        self._episode_thread.started.connect(self._episode_worker.run)
        self._episode_worker.finished.connect(self._episodes_ready)
        self._episode_worker.finished.connect(self._episode_thread.quit)
        self._episode_thread.start()

    def _episodes_ready(self, series_id, error):
        if error:
            self.episode_tree.clear()
            self.episode_tree.addTopLevelItem(
                QTreeWidgetItem([f"Could not load episodes: {error}"])
            )
            return
        rows = self.db.query(
            "SELECT season, episode_num, episode_id, title, container_extension "
            "FROM series_episodes WHERE playlist_id=? AND series_id=? "
            "ORDER BY season, episode_num",
            (self.playlist.id, str(series_id)),
        )
        self._render_episodes(rows)

    def _render_episodes(self, rows):
        self.episode_tree.clear()
        seasons: dict[int, QTreeWidgetItem] = {}
        for row in rows:
            season = row["season"]
            if season not in seasons:
                node = QTreeWidgetItem([f"Season {season}"])
                self.episode_tree.addTopLevelItem(node)
                seasons[season] = node
            label = row["title"] or f"Episode {row['episode_num']}"
            child = QTreeWidgetItem([f"{row['episode_num']:>2}.  {label}"])
            child.setData(0, Qt.UserRole, {
                "episode_id": row["episode_id"],
                "ext": row["container_extension"] or "mp4",
                "title": label,
                "season": season,
                "episode": row["episode_num"],
            })
            seasons[season].addChild(child)
        for node in seasons.values():
            node.setExpanded(True)
        if not rows:
            self.episode_tree.addTopLevelItem(QTreeWidgetItem(["No episodes listed"]))

    def _play_episode(self, item, _column=0):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        client = self.client()
        url = client.episode_url(data["episode_id"], data["ext"])
        show = self._current_series[1] if self._current_series else ""
        title = f"{show} — S{data['season']:02d}E{data['episode']:02d}"
        self.now_title.setText(title)
        self.live_badge.hide()
        self.player.play(url, title, is_live=False)

    def _episode_menu(self, point):
        item = self.episode_tree.itemAt(point)
        if item is None or not item.data(0, Qt.UserRole):
            return
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play_episode(item))
        menu.addAction("⤓ Download episode", lambda: self._download_episode(item))
        parent = item.parent()
        if parent:
            menu.addAction(
                "⤓ Download all episodes in season",
                lambda: [self._download_episode(parent.child(i))
                         for i in range(parent.childCount())],
            )
        menu.exec(self.episode_tree.viewport().mapToGlobal(point))

    def _download_episode(self, item):
        data = item.data(0, Qt.UserRole)
        if not data or not self._current_series:
            return
        client = self.client()
        url = client.episode_url(data["episode_id"], data["ext"])
        self.downloads.enqueue(
            self.playlist.id, "series", self._current_series[0], data["title"], url,
            episode_id=str(data["episode_id"]), show=self._current_series[1],
            season=data["season"], episode=data["episode"], ext=data["ext"],
        )
        self.statusBar().showMessage(f"Queued {data['title']}", 4000)

    # ------------------------------------------------------------------ EPG

    def _clear_epg(self):
        while self.epg_box.count():
            widget = self.epg_box.takeAt(0).widget()
            if widget:
                widget.deleteLater()

    def _load_epg(self, row):
        """Fetch the now/next listing without blocking the GUI.

        This used to be a synchronous request. Because `player.play()` only
        arms a timer, a blocking call here delayed the stream by its own
        duration: measured 525 ms of EPG before a single frame could start.
        """
        self._clear_epg()
        if self.kind != "live" or not self.playlist or not self.playlist.is_xtream:
            return
        stream_id = str(row[0])
        self._epg_token = stream_id

        cached = self._epg_cache.get(stream_id)
        if cached and (time.time() - cached[0]) < EPG_CACHE_SECONDS:
            self._render_epg(stream_id, cached[1])
            return

        label = QLabel("Loading guide…")
        label.setObjectName("epgEmpty")
        self.epg_box.addWidget(label)

        client = self.client()
        ThreadedTask.run(
            self, stream_id,
            lambda: client.short_epg(stream_id, limit=5),
            self._epg_ready,
        )

    def _epg_ready(self, token, listings):
        # The user may have moved on while this was in flight.
        if token != getattr(self, "_epg_token", None):
            return
        self._epg_cache[token] = (time.time(), listings or [])
        self._render_epg(token, listings or [])

    def _render_epg(self, token, listings):
        self._clear_epg()
        if not listings:
            label = QLabel("NO EPG FOUND")
            label.setObjectName("epgEmpty")
            self.epg_box.addWidget(label)
            return
        for entry in listings:
            start = (entry.get("start") or "")[11:16]
            end = (entry.get("end") or "")[11:16]
            label = QLabel(f"{start} – {end}   {entry['title']}")
            label.setWordWrap(True)
            self.epg_box.addWidget(label)

    # ------------------------------------------------- redirect prefetching

    def _prefetch_selected(self):
        index = self.list_view.currentIndex()
        if index.isValid():
            row = index.data(ROLE_ITEM)
            if row:
                self._prefetch_url(row)

    def _prefetch_url(self, row):
        """Resolve the portal's 302 while the user is still deciding.

        Doing it at play time gains nothing (the resolve itself costs ~700 ms);
        doing it on selection means VLC gets the CDN URL directly and starts in
        0.92 s instead of 1.75 s. Verified not to consume the account's single
        connection.
        """
        if not self.playlist or not self.playlist.is_xtream or self.kind == "series":
            return
        try:
            url = self.client().url_for(self.kind, row[0], row[4] or None)
        except Exception:
            return
        hit = self._resolved.get(url)
        if hit and (time.time() - hit[0]) < RESOLVED_TTL_SECONDS:
            return
        client = self.client()
        ThreadedTask.run(
            self, url,
            lambda: client.resolve_stream_url(url),
            self._prefetch_ready,
        )

    def _prefetch_ready(self, url, resolved):
        if resolved:
            self._resolved[url] = (time.time(), resolved)

    def _playable_url(self, url: str) -> str:
        """Use a freshly resolved CDN URL when we have one."""
        hit = self._resolved.get(url)
        if hit and (time.time() - hit[0]) < RESOLVED_TTL_SECONDS:
            return hit[1]
        return url

    # -------------------------------------------------------------- actions

    def toggle_favourite(self, index):
        row = index.data(ROLE_ITEM)
        if row is None or not self.playlist:
            return
        stream_id = str(row[0])
        if self.model.is_favourite(stream_id):
            self.db.execute(
                "DELETE FROM favourites WHERE playlist_id=? AND kind=? AND stream_id=?",
                (self.playlist.id, self.kind, stream_id),
            )
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO favourites(playlist_id, kind, stream_id, added_at)"
                " VALUES(?,?,?,?)",
                (self.playlist.id, self.kind, stream_id, int(time.time())),
            )
        favourites = {
            r["stream_id"] for r in self.db.query(
                "SELECT stream_id FROM favourites WHERE playlist_id=? AND kind=?",
                (self.playlist.id, self.kind),
            )
        }
        self.model.set_favourites(favourites)
        self.invalidate_counts()

    def _item_menu(self, point):
        index = self.list_view.indexAt(point)
        if not index.isValid():
            return
        row = index.data(ROLE_ITEM)
        menu = QMenu(self)
        if self.kind == "series":
            menu.addAction("Open episodes", lambda: self.open_series(row))
        else:
            menu.addAction("Play", lambda: self.play_item(row))
        menu.addAction(
            "Remove from favourites" if self.model.is_favourite(row[0])
            else "Add to favourites",
            lambda: self.toggle_favourite(index),
        )
        if self.kind == "movie":
            menu.addAction("⤓ Download", lambda: self.download_row(row))
        if self.kind == "live":
            menu.addAction("● Record…", lambda: self.record_row(row))
        menu.exec(self.list_view.viewport().mapToGlobal(point))

    def download_current(self):
        if self._current_item is None:
            self.statusBar().showMessage("Select something to download first", 4000)
            return
        if self.kind == "live":
            self.record_row(self._current_item)
        else:
            self.download_row(self._current_item)

    def download_row(self, row):
        if not self.playlist or not self.playlist.is_xtream:
            return
        client = self.client()
        ext = row[4] or "mp4"
        url = client.movie_url(row[0], ext)
        self.downloads.enqueue(
            self.playlist.id, "movie", row[0], row[1], url, ext=ext
        )
        note = ""
        if self.downloads.blocked_by_playback:
            note = " — will start when playback stops (one connection)"
        self.statusBar().showMessage(f"Queued {row[1]}{note}", 6000)

    def record_row(self, row):
        minutes, ok = QInputDialog.getInt(
            self, "Record live channel",
            "A live channel has no end, so recording is time-limited.\n\nMinutes:",
            60, 1, 720,
        )
        if not ok:
            return
        client = self.client()
        url = client.live_url(row[0], "ts")
        self.downloads.record_live(
            self.playlist.id, row[0], row[1], url, minutes * 60, ext="ts"
        )
        self.statusBar().showMessage(f"Recording {row[1]} for {minutes} min", 6000)

    def open_subtitles(self):
        title = self.now_title.text() or ""
        # The resolved CDN URL is what is actually playing, so it is the one
        # worth fingerprinting.
        url = self.player.current_url
        season = episode = 0
        item = self.episode_tree.currentItem()
        if item and item.data(0, Qt.UserRole):
            data = item.data(0, Qt.UserRole)
            season, episode = data["season"], data["episode"]
        dialog = SubtitleDialog(self, self.db, title, url, season, episode)
        dialog.subtitleChosen.connect(self._apply_subtitle)
        dialog.exec()

    def open_subtitle_file(self):
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a subtitle file", "",
            "Subtitles (*.srt *.ass *.ssa *.sub *.vtt);;All files (*)",
        )
        if path:
            self._apply_subtitle(path)

    def _apply_subtitle(self, path):
        if self.player.add_subtitle_file(path):
            self.statusBar().showMessage(f"Subtitle applied: {Path(path).name}", 5000)
        else:
            self.statusBar().showMessage("Could not apply that subtitle", 5000)

    def open_in_vlc(self):
        url = self.player.current_url
        if not url:
            self.statusBar().showMessage("Nothing is playing", 4000)
            return
        binary = vlc_setup.vlc_app_binary()
        if binary is None:
            QMessageBox.information(
                self, "VLC not found",
                "The full VLC application was not found on this computer.",
            )
            return
        try:
            subprocess.Popen([str(binary), url])
            self.statusBar().showMessage("Opened in VLC", 4000)
        except Exception as exc:
            self.statusBar().showMessage(f"Could not launch VLC: {exc}", 6000)

    def choose_download_dir(self):
        from PySide6.QtWidgets import QFileDialog

        current = str(self.downloads.root_dir())
        path = QFileDialog.getExistingDirectory(self, "Download folder", current)
        if path:
            self.db.set_setting("download_dir", path)
            self.player.snapshot_dir = str(Path(path) / "Snapshots")
            self.statusBar().showMessage(f"Downloads will go to {path}", 5000)

    def _toggle_hw(self, enabled):
        self.db.set_setting("hardware_decoding", "1" if enabled else "0")
        self.player.set_hardware_decoding(enabled)
        self.statusBar().showMessage(
            "Hardware decoding " + ("enabled" if enabled else "disabled"), 4000
        )

    def _toggle_autosync(self, enabled):
        if self.playlist:
            self.store.update(self.playlist.id, auto_sync=1 if enabled else 0)
            self.playlist = self.store.get(self.playlist.id)

    def show_about(self):
        QMessageBox.about(
            self, "About IPTV Player",
            "<b>IPTV Player</b><br><br>"
            "Xtream Codes and M3U client with embedded VLC playback.<br><br>"
            f"libVLC: {'found' if self.player.available else 'not found'}<br>"
            "Subtitles come from OpenSubtitles over the same keyless protocol "
            "VLSub uses — no API key needed.<br><br>"
            "Video playback is powered by VLC (VideoLAN).",
        )

    # ------------------------------------------------------------- shutdown

    def closeEvent(self, event):
        # Order matters: stop background producers before the objects they
        # signal into are destroyed.
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self._fs_state:
            self.exit_fullscreen()
        if self._effects_dialog is not None:
            self._effects_dialog.close()
        ThreadedTask.drain()
        try:
            self.images.shutdown()
        except Exception:
            pass
        try:
            self.player.shutdown()
        except Exception:
            pass
        try:
            self.downloads.shutdown()
        except Exception:
            pass
        if self._sync_thread and self._sync_thread.isRunning():
            self._sync_worker.cancel()
            self._sync_thread.quit()
            self._sync_thread.wait(3000)
        super().closeEvent(event)
