"""Main window: categories | items | video, with TV/Movies/Series tabs.

The video pane is not there at first. Browsing is what the window is for until
you double-click something, so the catalog opens across the whole width and the
player slides in beside it on play, then folds away again when playback stops.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QObject, QRect, QSize, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import QAction, QCursor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QDialog, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListView, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStackedWidget,
    QTabBar, QTextEdit, QVBoxLayout, QWidget,
)

from core import sync as sync_mod
from core import vlc_setup
from core.db import Database, fold
from core.downloads import DownloadManager, STATUS_DONE
from core.playlists import PlaylistStore, TYPE_XTREAM
from ui import icons
from ui.category_tree import CategoryTree, build_groups
from ui.downloads_panel import DownloadsPanel
from ui.effects_dialog import EffectsDialog, load_saved_effects
from ui.home_page import (
    HomePage, home_sections, is_pinned, pin_keys, pin_rail, unpin_rail,
)
from ui.models import (
    ROLE_ITEM, CatalogModel, ChannelDelegate, ImageCache, PosterDelegate,
)
from ui.player_widget import PlayerWidget
from ui.playlist_dialog import PlaylistEditor, PlaylistManager
from ui.search_page import SearchPage, search_catalog
from ui.series_page import SeriesPage, episode_caption, next_episode
from ui.subtitle_dialog import SubtitleDialog

TABS = [("live", "TV"), ("movie", "MOVIES"), ("series", "SERIES")]
EXPIRY_WARN_DAYS = 7
EPG_CACHE_SECONDS = 300        # now/next only moves every half hour
RESOLVED_TTL_SECONDS = 45      # CDN tokens verified good at 25 s; stay well inside
PREFETCH_DELAY_MS = 180        # let arrow-key scrolling settle before resolving
SEEK_STEP_SECONDS = 10         # VLC's short jump, on the left/right arrows
VOLUME_STEP = 5                # matches the wheel step over the volume slider
PIP_WIDTH = 560                # the main row of the bar needs ~490px to fit
PIP_MARGIN = 24
# What the splitter opens the video pane at. The docked transport bar's minimum
# is 522px and it lives in this pane, so anything below that is silently widened
# by Qt — measured, which is why this is not the 510 the layout used to ask for.
PLAYER_PANE_WIDTH = 540
MIN_MIDDLE_WIDTH = 260         # a poster column plus its scrollbar
MIN_LEFT_WIDTH = 180           # enough for a category name
PLAYER_HIDE_DELAY_MS = 400     # past SWITCH_DEBOUNCE_MS, so a queued switch wins
UP_NEXT_SECONDS = 10           # Netflix's countdown, near enough


def pip_rect(screen, width: int = PIP_WIDTH, margin: int = PIP_MARGIN):
    """Bottom-right 16:9 box inside `screen`, as (x, y, w, h).

    `screen` is (x, y, w, h) of the available desktop area. Kept pure and free
    of Qt so the corner arithmetic can be tested without a display, the same
    way `clamp_seek` is.
    """
    sx, sy, sw, sh = (int(v) for v in screen)
    width = max(160, min(int(width), sw))
    height = int(round(width * 9 / 16))
    if height > sh:                       # a very short screen wins over 16:9
        height = sh
        width = min(width, int(round(height * 16 / 9))) or width
    margin = max(0, int(margin))
    # Shrink the margin rather than push the window off the screen edge.
    margin_x = min(margin, max(0, sw - width))
    margin_y = min(margin, max(0, sh - height))
    return (sx + sw - width - margin_x, sy + sh - height - margin_y, width, height)


def reveal_sizes(current, want: int = PLAYER_PANE_WIDTH,
                 min_middle: int = MIN_MIDDLE_WIDTH,
                 min_left: int = MIN_LEFT_WIDTH):
    """Splitter widths for [categories, items, video] when the video comes back.

    Hiding a splitter child is not symmetric: its width goes to the items pane
    and does not return on its own. `current` is what the splitter reports while
    the video is collapsed, so its total is all the width there is — the video's
    share is taken out of the items pane, the one that absorbed it, and only out
    of the categories pane once items has given all it can spare.

    Pure and free of Qt so the arithmetic can be tested without a display, the
    same way `pip_rect` is.
    """
    left, middle = (max(0, int(v)) for v in current[:2])
    want = max(0, int(want))
    from_middle = min(want, max(0, middle - min_middle))
    from_left = min(want - from_middle, max(0, left - min_left))
    # A window with nothing to spare gets whatever is left rather than a clamp
    # failure: every width stays >= 0 and the total is preserved.
    return [left - from_left, middle - from_middle, from_middle + from_left]


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
        self._player_width = PLAYER_PANE_WIDTH
        self._fs_overlay = None
        self._pip_state = None
        self._pip_hold_ms = 0
        self._current_episode = None
        # The episode list the current episode was played from, captured at
        # play time: the series page may since have been pointed at another
        # show, and next/previous must follow what is playing, not what is
        # being browsed.
        self._episode_queue = []
        self._playing_series = None      # the show the current episode is from
        self._playing_kind = None        # the kind of what is playing, not of the tab
        self._up_next = None
        self._up_next_url = ""

        self.downloads = DownloadManager(db)
        self.downloads.start()

        # Owned by the window rather than a bare QTimer.singleShot: it must die
        # with the window, since a stop arrives during close as well. Built
        # before the UI, which connects the player's stop signal to it.
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(PLAYER_HIDE_DELAY_MS)
        self._idle_timer.timeout.connect(self._hide_player_if_idle)

        self.setWindowTitle("IPTV Player")
        self.resize(1500, 880)
        self._build_ui()
        self._build_menu()

        self.images.loaded.connect(lambda _: self.list_view.viewport().update())
        self.images.loaded.connect(self._up_next_image)

        if self.playlist is None:
            QTimer.singleShot(200, self.first_run)
        else:
            self.reload_catalog()
            # The front door. After reload_catalog, because building the tab
            # puts the channel list in the pane first.
            self.open_home()
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
        # The series page and the master search render into the items pane
        # rather than over the whole window, so the video can sit beside them
        # and stay there. A sibling in the splitter never covers libVLC's native
        # view; only something painting *over* it does, which is still why the
        # fullscreen control bar is a window of its own.
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

        self.home_button = QPushButton()
        self.home_button.setObjectName("topBarButton")
        self.home_button.setIcon(icons.icon("home", 19))
        self.home_button.setIconSize(QSize(19, 19))
        self.home_button.setFixedSize(34, 30)
        self.home_button.setCursor(Qt.PointingHandCursor)
        self.home_button.setFocusPolicy(Qt.NoFocus)
        self.home_button.setToolTip("Home")
        self.home_button.clicked.connect(self.open_home)
        top_layout.addWidget(self.home_button)

        self.search_button = QPushButton()
        self.search_button.setObjectName("masterSearchButton")
        self.search_button.setIcon(icons.icon("search", 19))
        self.search_button.setIconSize(QSize(19, 19))
        self.search_button.setFixedSize(34, 30)
        self.search_button.setCursor(Qt.PointingHandCursor)
        self.search_button.setFocusPolicy(Qt.NoFocus)
        self.search_button.setToolTip("Search everything  (Ctrl+F)")
        self.search_button.clicked.connect(self.open_search)
        top_layout.addWidget(self.search_button)

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
        self.tree.pinToggled.connect(self.toggle_home_pin)
        self.tree.menuRequested.connect(self._category_menu)
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

        self.downloads_panel = DownloadsPanel(self.downloads)
        self.downloads_panel.playRequested.connect(self.play_local)
        self.stack.addWidget(self.downloads_panel)

        middle_layout.addWidget(self.stack, 1)
        splitter.addWidget(middle)

        # ---- right: player + EPG
        right = QWidget()
        self._right_pane = right
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.player = PlayerWidget()
        self.player.errorOccurred.connect(self._on_player_error)
        self.player.playbackStarted.connect(lambda: self.downloads.set_playback_active(True))
        self.player.playbackStopped.connect(lambda: self.downloads.set_playback_active(False))
        self.player.playbackStopped.connect(self._idle_timer.start)
        self.player.endReached.connect(self._on_end_reached)
        self.player.positionChanged.connect(self._remember_position)
        self.player.fullscreenToggled.connect(self.toggle_fullscreen)
        self.player.videoDoubleClicked.connect(self._video_double_clicked)
        self.player.upNextRequested.connect(self._play_up_next)
        self.player.upNextDismissed.connect(self._dismiss_up_next)
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
        self._build_series_page()
        self._build_search_page()
        self._build_home_page()

        self.epg_box = QVBoxLayout()
        info_layout.addLayout(self.epg_box)
        info_layout.addStretch(1)
        self._right_layout = right_layout
        right_layout.addWidget(info, 2)
        splitter.addWidget(right)

        splitter.setSizes([330, 660, PLAYER_PANE_WIDTH])
        splitter.setStretchFactor(1, 1)
        # Nothing is playing yet, so the catalog gets the whole window. Hidden
        # after setSizes so the splitter still knows the width to open it at.
        right.hide()

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
        bar.pipRequested.connect(self.toggle_pip)
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
        view_menu.addAction("Downloads",
                            lambda: self._show_middle(self.downloads_panel))
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

        # Off until something plays: the catalog owns the window while you are
        # browsing, which is what it is for most of the time.
        player_pane = QAction("Video pane", self, checkable=True)
        player_pane.setChecked(self._right_pane.isVisible())
        player_pane.setShortcut(QKeySequence("Ctrl+Shift+V"))
        player_pane.toggled.connect(self.set_player_visible)
        self.player_action = player_pane
        view_menu.addAction(player_pane)

        effects = QAction("Adjustments and effects…", self)
        effects.triggered.connect(self.open_effects)
        view_menu.addAction(effects)

        search = QAction("Search everything…", self)
        search.setShortcut(QKeySequence.Find)
        search.triggered.connect(self.open_search)
        view_menu.addAction(search)

        fullscreen = QAction("Fullscreen video", self)
        fullscreen.setShortcut(QKeySequence("F"))
        fullscreen.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(fullscreen)

        pip = QAction("Picture-in-Picture", self)
        pip.setShortcut(QKeySequence("P"))
        pip.triggered.connect(self.toggle_pip)
        view_menu.addAction(pip)

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

        self.autoplay_action = QAction("Autoplay next episode", self, checkable=True)
        self.autoplay_action.setChecked(self.db.get_bool("autoplay_next", True))
        self.autoplay_action.setToolTip(
            "When an episode ends, count down and play the next one. Off, the "
            "card still appears — it just waits for you."
        )
        self.autoplay_action.toggled.connect(
            lambda on: self.db.set_setting("autoplay_next", "1" if on else "0"))
        settings_menu.addAction(self.autoplay_action)

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
        # Before load(), which is what rebuilds the rows carrying the glyph.
        self.tree.set_home_pins(pin_keys(self.db, self.playlist.id), self.kind,
                                rebuild=False)
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
            # Counted the way the list is built, so the badge cannot disagree
            # with what you can see: one row per show (history holds a row per
            # *episode*), and only shows still in the catalog — a watched title
            # the provider has since dropped is not shown, so it is not counted.
            "continue": self.db.scalar(
                "SELECT COUNT(*) FROM (SELECT DISTINCT h.stream_id FROM history h "
                "JOIN streams s ON s.playlist_id=h.playlist_id AND s.kind=h.kind "
                "AND s.stream_id=h.stream_id AND s.available=1 AND s.name <> '' "
                "WHERE h.playlist_id=? AND h.kind=?)",
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
        # Changing tab leaves the series page or the search results behind, the
        # same way picking a category does.
        self._show_middle(self.list_view)
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
            self._show_middle(self.downloads_panel)
            self.downloads_panel.refresh()
            return
        self._show_middle(self.list_view)
        self._current_filter = (node_type, payload)
        self.apply_item_filter()

    def _show_middle(self, widget):
        """Swap what the items pane shows.

        Four things live in that pane now — the catalog list, the downloads, the
        series page and the master search. The last two bring their own header
        (a back arrow, and the search field itself), so the pane's own filter box
        is hidden for them rather than sitting above a page it cannot filter.
        """
        self.stack.setCurrentWidget(widget)
        self.item_search.setVisible(
            widget not in (self.series_page, self.search_page, self.home_page))

    def _schedule_item_filter(self):
        self._filter_timer.start()

    def apply_item_filter(self):
        if self.playlist is None:
            return
        node_type, payload = getattr(self, "_current_filter", ("all", None))
        # fold(), not lower(): name_folded strips accents as well as case, so a
        # merely lowercased term misses every accented title - lower('café')
        # matched nothing at all.
        search = fold(self.item_search.text().strip())
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
            # Grouped, not a plain join: history is keyed per *episode*, so a
            # series with eight watched episodes joined straight through and
            # appeared eight times in the list.
            joins = ("JOIN (SELECT playlist_id, kind, stream_id,"
                     " MAX(watched_at) AS watched_at FROM history"
                     " GROUP BY playlist_id, kind, stream_id) h"
                     " ON h.playlist_id=s.playlist_id AND h.kind=s.kind"
                     " AND h.stream_id=s.stream_id")
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

    def play_item(self, row, kind: str | None = None):
        """`kind` is passed in when the row did not come from the open tab.

        The homepage mixes all three, so reading self.kind there would request a
        film as a live stream and file it under whatever tab happened to be
        selected. Everywhere else the open tab *is* the kind, and the default
        keeps that unchanged.
        """
        if not self.playlist or not self.playlist.is_xtream:
            QMessageBox.information(
                self, "Playback",
                "This playlist type does not provide direct stream URLs.",
            )
            return
        self._idle_timer.stop()
        self.set_player_visible(True)
        kind = kind or self.kind
        self._current_item = row
        self._current_episode = None
        self._playing_series = None
        self._playing_kind = kind
        client = self.client()
        stream_id, name, _, _, ext = row[0], row[1], row[2], row[3], row[4]
        is_live = kind == "live"
        url = client.url_for("live" if is_live else "movie", stream_id, ext or None)

        resume = 0
        if not is_live:
            record = self.db.one(
                "SELECT position_secs, duration_secs FROM history WHERE playlist_id=? "
                "AND kind=? AND stream_id=? AND episode_id=''",
                (self.playlist.id, kind, stream_id),
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
        self._load_epg(row, kind)

    def play_local(self, path: str, title: str):
        """Downloaded files play from disk and use no connection."""
        self._idle_timer.stop()
        self.set_player_visible(True)
        self._current_item = None
        self._current_episode = None
        self._playing_series = None
        self._playing_kind = None
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
        elif self._current_episode is not None:
            QTimer.singleShot(0, self._episode_ended)
        else:
            self.player.stop()

    def _episode_ended(self):
        """Offer the next episode instead of just stopping.

        Order matters twice over. The player is stopped first, because the
        account allows one connection and the finished stream should not hold
        it while a card sits on screen; and the idle timer is stopped *after*,
        because stop() is what emits playbackStopped and starts it.
        """
        episode = self._current_episode
        following = next_episode(self._episode_queue, episode["episode_id"])
        # The show being *watched*, which is not necessarily the one on screen.
        series = self._playing_series
        show = series[1] if series else ""
        episode_series = series[0] if series else ""

        self.player.stop()
        self._idle_timer.stop()         # the pane stays: there is something to see

        if following is None:
            # The show's own cover, which is also what the episode cards fall
            # back to — the provider ships no per-episode stills.
            self._up_next_url = self._series_info(episode_series).get("cover") or ""
            self.player.show_finished(show or "this show",
                                      self.images.get(self._up_next_url))
            return

        self._up_next = following
        title, subtitle = episode_caption(following, show)
        seconds = UP_NEXT_SECONDS if self.db.get_bool("autoplay_next", True) else 0
        self._up_next_url = following.get("image_url") or ""
        self.player.show_up_next(
            title, subtitle, self.images.get(self._up_next_url), seconds)

    def _play_up_next(self):
        """The card was taken: Space, the play button, a click, or the clock."""
        episode = self._up_next
        self._up_next = None
        if episode is not None:
            self._play_episode(episode)

    def _dismiss_up_next(self):
        """The end-of-show card's way out: back to the page, pane folded away."""
        self.player.hide_up_next()
        if self._playing_series is not None:
            self.open_series(self._playing_series)
        self.set_player_visible(False)

    def _up_next_image(self, url: str):
        """The thumbnail arriving after the card was already drawn."""
        if url and url == self._up_next_url and self.player.up_next_showing:
            self.player.up_next.set_still(self.images.get(url))

    def replay_current(self):
        # The episode first: open_series() leaves _current_item pointing at the
        # *series* row, so replaying that would ask for a VOD URL built from a
        # series id and get nothing back.
        if self._current_episode is not None:
            self._play_episode(self._current_episode)
        elif self._current_item is not None:
            # With the kind it was played as: a film started from the homepage
            # while the TV tab was open would otherwise repeat as a live stream.
            self.play_item(self._current_item, kind=self._playing_kind)
        elif self.player.current_url:
            self.player.play(self.player.current_url, self.player.current_title,
                             self.player.is_live)

    # --------------------------------------------------------- prev / next

    def step_item(self, delta: int):
        """The bar's ⏮ / ⏭, over whatever is playing.

        Keyed off the episode rather than off what the items pane happens to be
        showing: "next" means the next episode for as long as an episode is what
        you are watching, whether or not you have since walked back to the list.
        """
        if self._current_episode is not None:
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
        """Within a series, step across season boundaries rather than stopping.

        The list holds every episode of the show in order, so crossing from the
        last episode of one season into the first of the next needs no special
        case - it is just the next entry. Unlike the up-next card, ⏭ wraps: it
        is a deliberate press, not something that happens on its own.

        The list is the one captured when the episode started, not whatever the
        page is showing now — open a second show while watching a first and the
        two disagree.
        """
        episodes = self._episode_queue
        if not episodes:
            return
        current = getattr(self, "_current_episode", None)
        position = -1
        if current is not None:
            for index, episode in enumerate(episodes):
                if str(episode["episode_id"]) == str(current["episode_id"]):
                    position = index
                    break
        if self.player.bar.shuffle and len(episodes) > 1:
            import random

            choice = position
            while choice == position:
                choice = random.randrange(len(episodes))
            position = choice
        else:
            position = (position + delta) % len(episodes)
        self._play_episode(episodes[position])

    # ------------------------------------------------------------- browser

    def toggle_browser(self):
        self.browser_action.setChecked(not self.browser_action.isChecked())

    def set_browser_visible(self, visible: bool):
        """The bar's ☰ button — VLC's "show playlist"."""
        if self._fs_state or self._pip_state:
            return          # fullscreen and PiP own pane visibility
        if not visible and not self.player_visible:
            # Hiding the browser with no video pane leaves an empty window.
            self.browser_action.setChecked(True)
            self.statusBar().showMessage("Nothing is playing", 4000)
            return
        if not visible and self._left_pane.isVisible():
            self._browser_sizes = self._splitter.sizes()
        self._left_pane.setVisible(visible)
        self._middle_pane.setVisible(visible)
        if visible and self._browser_sizes:
            self._splitter.setSizes(self._browser_sizes)

    def _reveal_browser(self):
        """Make sure the items pane is actually on screen before rendering into it.

        The series page and the master search live in that pane now, so three
        states hide it from underneath them: fullscreen, Picture-in-Picture, and
        Ctrl+L. Opening either page has to undo whichever is in force, or the
        page renders somewhere nobody can see.
        """
        if self._fs_state:
            self.exit_fullscreen()
        if self._pip_state:
            self.exit_pip()
        if not self._middle_pane.isVisible():
            self.browser_action.setChecked(True)    # drives set_browser_visible

    # -------------------------------------------------------- video pane

    @property
    def player_visible(self) -> bool:
        return self._right_pane.isVisible()

    def set_player_visible(self, visible: bool):
        """Bring the video pane in, or fold it away again.

        Called on every play, on the View menu item, and 400ms after playback
        stops. Closing it by hand does not stop the stream — the audio carries
        on, the way it does when a video window is minimised.
        """
        if self._fs_state or self._pip_state:
            # Fullscreen and PiP own pane visibility; put the tick back where
            # the panes actually are rather than where the menu was clicked.
            self.player_action.setChecked(self.player_visible)
            return
        if visible == self.player_visible:
            self.player_action.setChecked(visible)
            return
        if not visible:
            # Remember the width it was actually dragged to, not the default.
            self._player_width = self._splitter.sizes()[2] or self._player_width
        self._right_pane.setVisible(visible)
        if visible:
            self._splitter.setSizes(
                reveal_sizes(self._splitter.sizes(), self._player_width))
            # Give the surface real geometry *before* libVLC draws into it: a
            # layout applies its sizes on a posted LayoutRequest, which would
            # otherwise not be delivered until after play() has started.
            QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self.player_action.setChecked(visible)

    def _return_focus(self):
        """Hand the keyboard back to whatever is actually on screen.

        The video surface is the right owner while it is showing — it is what
        the arrow keys act on. With the pane closed it is hidden, and focusing a
        hidden widget leaves the window with no focus widget at all: the list
        then ignores the arrows entirely, which is exactly what happened after
        closing the search page in a browse-only window.
        """
        target = self.player.surface if self.player_visible else self.list_view
        target.setFocus(Qt.OtherFocusReason)

    def _hide_player_if_idle(self):
        """Fold the pane away once playback really has stopped.

        Delayed because restart_after_error() stops and immediately replays,
        and a queued switch waits out the player's own debounce — neither is
        "nothing is playing", and blinking the pane away for them looks broken.
        """
        if self.player.current_url or self.player.pending:
            return
        if self.player.up_next_showing:
            return          # the episode ended, but there is an offer on screen
        self.set_player_visible(False)

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
        if not self.playlist or duration <= 0 or position <= 0:
            return
        target = self._history_target()
        if target is None:
            return
        kind, stream_id, episode_id = target
        self.db.execute(
            "INSERT INTO history(playlist_id, kind, stream_id, episode_id, position_secs,"
            " duration_secs, watched_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(playlist_id, kind, stream_id, episode_id) DO UPDATE SET "
            "position_secs=excluded.position_secs, duration_secs=excluded.duration_secs,"
            " watched_at=excluded.watched_at",
            (self.playlist.id, kind, stream_id, episode_id,
             int(position), int(duration), int(time.time())),
        )

    def _history_target(self):
        """(kind, stream_id, episode_id) the position belongs to, or None.

        Taken from what is *playing*, never from the tab that is open or the
        show being browsed: switch to Movies mid-episode, or open a second show
        while a first one plays, and both of those have moved on while the
        resume position still belongs where it started.
        """
        if self._current_episode is not None:
            if not self._playing_series:
                return None
            return ("series", str(self._playing_series[0]),
                    str(self._current_episode["episode_id"]))
        kind = self._playing_kind or self.kind
        if not self._current_item or kind == "live":
            return None         # live has nothing to resume
        return (kind, str(self._current_item[0]), "")

    # ------------------------------------------------- fullscreen and PiP

    def _save_chrome(self) -> dict:
        """Everything a window mode has to put back when it ends."""
        return {
            "sizes": self._splitter.sizes(),
            "left": self._left_pane.isVisible(),
            "middle": self._middle_pane.isVisible(),
            "right": self._right_pane.isVisible(),
            "info": self._info_panel.isVisible(),
            "top": self._top_bar.isVisible(),
            # On macOS the menu bar is native (not a child widget), so it has
            # no visibility to toggle; the OS hides it in fullscreen itself.
            "menubar": None if self.menuBar().isNativeMenuBar()
                       else self.menuBar().isVisible(),
            "statusbar": self.statusBar().isVisible(),
            "maximized": self.isMaximized(),
            "geometry": self.saveGeometry(),
            "advanced": self.player.bar.advanced_row.isVisible(),
        }

    def _hide_chrome(self):
        for widget in (self._left_pane, self._middle_pane, self._info_panel,
                       self._top_bar):
            widget.hide()
        if not self.menuBar().isNativeMenuBar():
            self.menuBar().setVisible(False)
        self.statusBar().setVisible(False)

    def _restore_chrome(self, state: dict):
        self._left_pane.setVisible(state["left"])
        self._middle_pane.setVisible(state["middle"])
        # Leaving fullscreen with nothing playing goes back to a browse-only
        # window, not to an idle black pane.
        self._right_pane.setVisible(state["right"])
        self.player_action.setChecked(state["right"])
        self._info_panel.setVisible(state["info"])
        self._top_bar.setVisible(state["top"])
        if state["menubar"] is not None:
            self.menuBar().setVisible(state["menubar"])
        self.statusBar().setVisible(state["statusbar"])
        self.player.bar.set_advanced_visible(state["advanced"])

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
        if self._pip_state:
            self.exit_pip()
        # The series page and the search are inside the items pane, which
        # _hide_chrome hides wholesale — leaving fullscreen puts you back on
        # whichever of them you were reading.
        self._fs_state = self._save_chrome()
        self._right_pane.show()     # the video lives in it; saved state puts it back
        self._hide_chrome()
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

    def _float_bar(self, area=None, inset: int = 80, gap: int = 28,
                   max_width: int = 1100):
        """Put the controls over the video, the way VLC's fullscreen does.

        A separate top-level window is the only thing that reliably draws above
        libVLC's native video view — a sibling widget in the same window does
        not, because the video is a native child window. Safe to do because the
        bar owns no video handle. Set `fullscreen_floating_bar` to 0 to keep the
        old docked behaviour if a window manager will not honour stay-on-top.

        `area` is the rectangle to sit at the bottom of: the whole screen for
        fullscreen, the mini window's frame for Picture-in-Picture.
        """
        if not self.db.get_bool("fullscreen_floating_bar", True):
            return
        if area is None:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            area = screen.geometry()

        width = max(200, min(max_width, area.width() - inset))
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
        overlay.move(left, area.y() + area.height() - overlay.height() - gap)
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
        self._restore_chrome(state)

        self.showNormal()
        self.restoreGeometry(state["geometry"])
        if state["maximized"]:
            self.showMaximized()
        self._splitter.setSizes(state["sizes"])
        self._fs_state = None
        self.activateWindow()
        self._return_focus()

    # ------------------------------------------------ picture-in-picture

    def _video_double_clicked(self):
        """In PiP a double-click means "give me the window back", not "now go
        fullscreen" — which is what it means everywhere else."""
        if self._pip_state:
            self.exit_pip()
        else:
            self.toggle_fullscreen()

    def toggle_pip(self):
        if self._pip_state:
            self.exit_pip()
        else:
            self.enter_pip()

    def enter_pip(self):
        """Shrink the window to a small player that floats over other apps.

        Built the same way as fullscreen, in reverse, and for the same reason:
        the video surface is never reparented, so libVLC keeps its drawable.
        The one structural change is the stay-on-top flag — Qt restacks the
        native window in place rather than recreating it, which was measured
        before this was written (the handle and the picture both survive). The
        re-attach below is insurance for platforms where that is not true.
        """
        if self._pip_state or not self.player.available:
            return
        if self._fs_state:
            self.exit_fullscreen()
        # As in fullscreen: the pages live in the items pane, which _hide_chrome
        # takes away and _restore_chrome gives back exactly as it was.

        state = self._save_chrome()
        state["flags"] = self.windowFlags()
        state["handle"] = int(self.player.surface.winId())
        self._pip_state = state

        self._right_pane.show()     # the video lives in it; saved state puts it back
        self._hide_chrome()
        self.player.bar.set_advanced_visible(False)

        rect = pip_rect(self._screen_area(), self._pip_width())
        # The bar must come out of the layout *before* the window is resized:
        # docked, its 522px minimum width is the window's minimum too, and the
        # resize is silently clamped to it. Measured, not guessed.
        self._float_bar(QRect(*rect), inset=0, gap=6, max_width=rect[2])
        self._set_controls_visible(False)
        self._relayout()

        if self.isMaximized():
            self.showNormal()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setGeometry(*rect)
        self.show()
        if int(self.player.surface.winId()) != state["handle"]:
            self.player.reattach_surface()

        self._reposition_pip_bar()
        self.player.bar.set_pip(True)

        self._pip_hold_ms = 0
        self._fs_timer.start()
        self.activateWindow()
        self.raise_()
        self.player.surface.setFocus(Qt.OtherFocusReason)

    def exit_pip(self):
        state = self._pip_state
        if not state:
            return
        self._fs_timer.stop()
        self._pip_state = None          # before _dock_bar, so the tick is inert
        self.db.set_setting("pip_width", str(self.width()))

        self._dock_bar()
        self.player.set_chrome_visible(True)
        self._restore_chrome(state)

        self.setWindowFlags(state["flags"])
        self.show()
        self.restoreGeometry(state["geometry"])
        if state["maximized"]:
            self.showMaximized()
        self._splitter.setSizes(state["sizes"])
        self.player.bar.set_pip(False)
        self.activateWindow()
        self.raise_()
        self._return_focus()

    def _relayout(self):
        """Recompute the window's minimum size *now*, not on the next tick.

        A layout applies its minimum by calling setMinimumSize on the widget,
        and only recomputes when a posted LayoutRequest is delivered. Hiding the
        panes and detaching the bar therefore leaves the old, much larger
        minimum in place for the rest of the current call — which silently
        clamped the resize into PiP to the size the window had with the bar
        still docked (996x441 instead of 560x315).
        """
        for widget in (self.player, self._splitter, self.centralWidget(), self):
            if widget is None:
                continue
            layout = widget.layout()
            if layout is not None:
                layout.invalidate()
            widget.updateGeometry()
        # A QSplitter has no QLayout to invalidate — it recomputes when its
        # posted LayoutRequest is delivered, so deliver those now. Only that
        # event type, so no input or paint work happens here.
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        self.setMinimumSize(0, 0)
        layout = self.layout()
        if layout is not None:
            layout.activate()

    def _screen_area(self):
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else self.geometry()
        return (area.x(), area.y(), area.width(), area.height())

    def _pip_width(self) -> int:
        try:
            return int(self.db.get_setting("pip_width", str(PIP_WIDTH)) or PIP_WIDTH)
        except (TypeError, ValueError):
            return PIP_WIDTH

    def _pip_tick(self):
        """Controls follow the pointer: up while it is over the mini player.

        Deliberately not the fullscreen rule of "any mouse movement shows the
        bar" — in PiP you are working in another application, and the bar
        appearing every time the mouse twitched would be noise.
        """
        position = QCursor.pos()
        over = self.frameGeometry().contains(position)
        if not over and self._fs_overlay is not None:
            over = self._fs_overlay.geometry().contains(position)
        if self._pip_hold_ms > 0:
            self._pip_hold_ms -= self._fs_timer.interval()
            over = True
        self._set_controls_visible(over)

    def _reposition_pip_bar(self):
        """Keep the floating bar under the mini window as it is moved/resized."""
        if not self._pip_state or self._fs_overlay is None:
            return
        frame = self.frameGeometry()
        overlay = self._fs_overlay
        overlay.setFixedWidth(max(200, frame.width()))
        overlay.adjustSize()
        overlay.move(frame.x() + (frame.width() - overlay.width()) // 2,
                     frame.y() + frame.height() - overlay.height() - 6)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._reposition_pip_bar()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_pip_bar()

    def _fs_tick(self):
        """Hide the controls and pointer once the mouse goes still.

        Cursor position is polled rather than tracked through mouse events:
        libVLC's own view sits on top of the surface and can swallow them, so
        an event-driven approach would leave the bar stuck hidden.
        """
        if self._pip_state:
            self._pip_tick()
            return
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

        Fullscreen and Picture-in-Picture have no channel list, so playback
        always wins there. Windowed, the video takes them only while it holds
        focus - which it is given the moment playback starts - leaving arrow
        channel-surfing intact whenever the list is what you are looking at.
        """
        if self._fs_state or self._pip_state:
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
        if key == Qt.Key_Escape and self._pip_state:
            self.exit_pip()
            return True
        if key == Qt.Key_Escape and self.series_open:
            self.close_series()
            return True
        if key == Qt.Key_Escape and self.search_open:
            self.close_search()
            return True
        if key == Qt.Key_Escape and self.home_open:
            self.close_home()
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

        Fullscreen and PiP hide the status bar, so the floating control bar is
        popped back up instead: it already shows the position and volume the key
        just changed, and fades out again on its own.
        """
        if self._pip_state:
            self._pip_hold_ms = 1600
            self._set_controls_visible(True)
        elif self._fs_state:
            self._fs_idle_ms = 0
            self._set_controls_visible(True)
            self._restore_cursor()
        else:
            self.statusBar().showMessage(message, 1500)

    # -------------------------------------------------------------- series

    def _build_home_page(self):
        self.home_page = HomePage(self.images)
        self.home_page.itemActivated.connect(self._activate_home)
        self.home_page.seeAllRequested.connect(self._see_all_rail)
        self.home_page.unpinRequested.connect(self._unpin_rail)
        self.stack.addWidget(self.home_page)

    @property
    def home_open(self) -> bool:
        return self.stack.currentWidget() is self.home_page

    def open_home(self):
        """The house icon, and where the app lands."""
        self._reveal_browser()
        self._show_middle(self.home_page)
        self.refresh_home()

    def refresh_home(self):
        """Rebuilt on every visit: Continue Watching moves as you watch."""
        if self.playlist is None:
            self.home_page.show_sections([])
            return
        self.home_page.show_sections(home_sections(self.db, self.playlist.id))

    def close_home(self):
        self._show_middle(self.list_view)
        self._return_focus()

    def _activate_home(self, kind: str, row):
        """Play from the wall without being thrown off it.

        The tab is deliberately *not* switched, unlike a search result: the
        homepage is somewhere you stay. play_item() is told which kind this is
        instead of reading it off the open tab, which is the only reason that
        is safe - otherwise a film played from here would be requested as a
        live stream and remembered under the wrong kind.
        """
        if kind == "series":
            self._select_tab("series")
            self.open_series(row)
            return
        self.play_item(row, kind=kind)

    def toggle_home_pin(self, node_type: str, payload, title: str):
        """Put a sidebar group or category on the homepage, or take it off.

        The pin lives in three places at once — the table, the sidebar glyph and
        the wall — so every route in comes through here rather than each caller
        remembering to update the other two.
        """
        if self.playlist is None or node_type not in ("group", "category"):
            return
        pinned = is_pinned(self.db, self.playlist.id, self.kind, node_type, payload)
        if pinned:
            unpin_rail(self.db, self.playlist.id, self.kind, node_type, payload)
        else:
            pin_rail(self.db, self.playlist.id, self.kind, node_type, payload,
                     title or str(payload))
        self._refresh_home_pins()
        self.statusBar().showMessage(
            f"{'Removed' if pinned else 'Added'} “{title}” "
            f"{'from' if pinned else 'to'} the homepage", 4000)

    def _unpin_rail(self, target):
        """The ✕ on a pinned rail, which knows its own kind."""
        kind, node_type, payload = target
        if self.playlist is None:
            return
        unpin_rail(self.db, self.playlist.id, kind, node_type, payload)
        self._refresh_home_pins()

    def _refresh_home_pins(self):
        """Put the sidebar glyph and the wall back in step with the table."""
        if self.playlist is None:
            return
        self.tree.set_home_pins(pin_keys(self.db, self.playlist.id), self.kind)
        if self.home_open:
            self.refresh_home()

    def _category_menu(self, node_type: str, payload, title: str, position):
        menu = QMenu(self)
        menu.addAction("Show", lambda: self.on_node_selected(node_type, payload))
        if node_type in ("group", "category") and self.playlist is not None:
            pinned = is_pinned(self.db, self.playlist.id, self.kind,
                               node_type, payload)
            menu.addSeparator()
            menu.addAction(
                "Remove from homepage" if pinned else "Add to homepage",
                lambda: self.toggle_home_pin(node_type, payload, title),
            )
        menu.exec(position)

    def _see_all_rail(self, target):
        """A rail's "See all", handed to the sidebar node it corresponds to.

        Every rail below the personal two is one kind and one existing node, so
        this reuses on_node_selected rather than growing a second filter path.
        """
        kind, node_type, payload = target
        self._select_tab(kind)
        self.on_node_selected(node_type, payload)

    def _build_search_page(self):
        self.search_page = SearchPage(self.images)
        self.search_page.backRequested.connect(self.close_search)
        self.search_page.resultActivated.connect(self._activate_result)
        self.search_page.seeAllRequested.connect(self._see_all)
        self.search_page.searchRequested.connect(self._schedule_master_search)
        self.stack.addWidget(self.search_page)

        self._master_timer = QTimer(self)
        self._master_timer.setSingleShot(True)
        self._master_timer.setInterval(250)
        self._master_timer.timeout.connect(self._run_master_search)

    @property
    def search_open(self) -> bool:
        return self.stack.currentWidget() is self.search_page

    def open_search(self):
        """The magnifier and Ctrl+F, from wherever you happen to be."""
        if self.search_open:
            self.search_page.focus_field()
            return
        self._reveal_browser()
        self._show_middle(self.search_page)
        self.search_page.focus_field()
        self._run_master_search()

    def close_search(self):
        self._master_timer.stop()
        self._show_middle(self.list_view)
        self._return_focus()

    def _schedule_master_search(self, _term=""):
        self._master_timer.start()

    def _run_master_search(self):
        if self.playlist is None:
            return
        term = self.search_page.term()
        results = search_catalog(self.db, self.playlist.id, term)
        self.search_page.show_results(results, term)

    def _activate_result(self, kind: str, row):
        """Play or open a result found in a section other than the current tab.

        The tab is switched FIRST and deliberately: play_item() builds its URL
        and its history record from self.kind, so a film opened while the TV tab
        was selected would be requested as a live stream and remembered under
        the wrong kind.
        """
        if kind != self.kind:
            self._select_tab(kind)
        if kind == "series":
            self.open_series(row)
        else:
            self.close_search()
            self.play_item(row)

    def _select_tab(self, kind: str):
        for index, (value, _label) in enumerate(TABS):
            if value != kind:
                continue
            if self.tab_bar.currentIndex() != index:
                self.tab_bar.setCurrentIndex(index)     # drives _tab_changed
            elif self.kind != kind:
                # Already on the tab but self.kind disagrees. Nothing should
                # make them diverge, but self.kind is what playback builds its
                # URL from, so re-sync rather than trust the invariant.
                self._tab_changed(index)
            return

    def _see_all(self, kind: str, term: str):
        """Hand the term to the tab's own filter rather than duplicating it."""
        self._select_tab(kind)
        self.close_search()
        self.item_search.setText(term)
        self.apply_item_filter()

    def _build_series_page(self):
        self.series_page = SeriesPage(self.images)
        self.series_page.backRequested.connect(self.close_series)
        self.series_page.episodeActivated.connect(self._play_episode)
        self.series_page.episodeMenuRequested.connect(self._episode_menu)
        self.stack.addWidget(self.series_page)

    @property
    def series_open(self) -> bool:
        return self.stack.currentWidget() is self.series_page

    def close_series(self):
        self._show_middle(self.list_view)
        self._return_focus()

    EPISODE_COLUMNS = ("season, episode_num, episode_id, title, "
                       "container_extension, image, duration_secs")

    def _episode_rows(self, series_id) -> list:
        rows = self.db.query(
            f"SELECT {self.EPISODE_COLUMNS} FROM series_episodes "
            "WHERE playlist_id=? AND series_id=? ORDER BY season, episode_num",
            (self.playlist.id, str(series_id)),
        )
        cover = self._series_info(series_id).get("cover") or ""
        out = []
        for row in rows:
            out.append({
                "episode_id": row["episode_id"],
                "ext": row["container_extension"] or "mp4",
                "title": row["title"] or f"Episode {row['episode_num']}",
                "season": row["season"],
                "episode": row["episode_num"],
                # Most providers ship no per-episode still; the series cover is
                # what the reference screenshot falls back to as well.
                "image_url": row["image"] or cover,
            })
        return out

    def _series_info(self, series_id) -> dict:
        row = self.db.one(
            "SELECT cover, backdrop, plot, cast_list, director, genre,"
            " release_date, rating FROM series_info WHERE playlist_id=? AND series_id=?",
            (self.playlist.id, str(series_id)),
        )
        return dict(row) if row else {}

    def _series_history(self, series_id) -> dict:
        rows = self.db.query(
            "SELECT episode_id, position_secs, duration_secs, watched_at FROM history"
            " WHERE playlist_id=? AND kind='series' AND stream_id=? AND episode_id <> ''",
            (self.playlist.id, str(series_id)),
        )
        return {str(r["episode_id"]): (r["position_secs"], r["duration_secs"],
                                       r["watched_at"]) for r in rows}

    def open_series(self, row):
        self._current_item = row
        self._current_series = row
        self._reveal_browser()
        self.series_page.set_loading(row[1])
        self._show_middle(self.series_page)
        self.now_title.setText(row[1])

        episodes = self._episode_rows(row[0])
        # Both halves must be present: databases written before the series page
        # existed have episodes cached but no info row, and would otherwise show
        # six blank metadata lines forever.
        if episodes and self._series_info(row[0]):
            self._show_series(row[0])
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
            # Cached episodes beat an error page. The account allows a single
            # connection, so a refetch fails outright whenever something is
            # playing - and the episodes are usually already on disk, only the
            # metadata is missing.
            if self._episode_rows(series_id):
                self._show_series(series_id)
                self.series_page.set_error(f"Showing what was already saved — {error}")
            else:
                self.series_page.set_error(f"Could not load episodes: {error}")
            return
        self._show_series(series_id)

    def _show_series(self, series_id):
        show = self._current_series[1] if self._current_series else ""
        info = self._series_info(series_id)
        if not info.get("cover") and self._current_series is not None:
            # Fall back to the catalog row's icon when the provider sent no
            # per-series cover with get_series_info.
            info["cover"] = self._current_series[2] or ""
        self.series_page.set_series(show, info, self._episode_rows(series_id),
                                    self._series_history(series_id))

    def _play_episode(self, episode, resume_secs: int = 0):
        if not episode:
            return
        client = self.client()
        url = client.episode_url(episode["episode_id"], episode["ext"])
        self._current_episode = episode
        # Adopt the page's show and episode list only when the episode actually
        # came from that page. _current_series is whatever is being *browsed*,
        # and browsing a second show while a first one plays must not rename
        # what is playing, file its resume position under the wrong id, or swap
        # the queue out from under it.
        showing = self.series_page.episodes()
        if any(str(item["episode_id"]) == str(episode["episode_id"])
               for item in showing):
            self._episode_queue = showing
            self._playing_series = self._current_series
        show = self._playing_series[1] if self._playing_series else ""
        title = f"{show} — S{episode['season']:02d}E{episode['episode']:02d}"
        self.series_page.note_played(episode)
        self.now_title.setText(title)
        self.live_badge.hide()
        # The page stays. The video docks beside it, so playing episode four no
        # longer throws you back to the grid of every show you own.
        self._idle_timer.stop()
        self.set_player_visible(True)
        self.player.play(url, title, is_live=False, resume_secs=int(resume_secs or 0))

    def _episode_menu(self, episode, position):
        menu = QMenu(self)
        menu.addAction("Play", lambda: self._play_episode(episode))
        menu.addAction("⤓ Download episode", lambda: self._download_episode(episode))
        season = episode["season"]
        menu.addAction(
            "⤓ Download all episodes in season",
            lambda: [self._download_episode(item)
                     for item in self.series_page.episodes()
                     if item["season"] == season],
        )
        menu.exec(position)

    def _download_episode(self, episode):
        if not episode or not self._current_series:
            return
        client = self.client()
        url = client.episode_url(episode["episode_id"], episode["ext"])
        self.downloads.enqueue(
            self.playlist.id, "series", self._current_series[0], episode["title"], url,
            episode_id=str(episode["episode_id"]), show=self._current_series[1],
            season=episode["season"], episode=episode["episode"], ext=episode["ext"],
        )
        self.statusBar().showMessage(f"Queued {episode['title']}", 4000)

    # ------------------------------------------------------------------ EPG

    def _clear_epg(self):
        while self.epg_box.count():
            widget = self.epg_box.takeAt(0).widget()
            if widget:
                widget.deleteLater()

    def _load_epg(self, row, kind: str | None = None):
        """Fetch the now/next listing without blocking the GUI.

        This used to be a synchronous request. Because `player.play()` only
        arms a timer, a blocking call here delayed the stream by its own
        duration: measured 525 ms of EPG before a single frame could start.
        """
        self._clear_epg()
        kind = kind or self.kind
        if kind != "live" or not self.playlist or not self.playlist.is_xtream:
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
        playing = getattr(self, "_current_episode", None)
        if playing:
            season, episode = playing["season"], playing["episode"]
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
        if self._pip_state:
            self.exit_pip()
        if self.search_open:
            self.close_search()
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
