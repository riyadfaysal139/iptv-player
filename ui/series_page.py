"""The series page: backdrop, metadata, seasons, episode cards.

Rendered into the items pane rather than over the window, so the video can sit
beside it and playing an episode does not take the show away.

Laid out like the reference: a darkened still behind everything, the poster and
a column of icon-labelled metadata rows, a row of season buttons, and a wrapping
grid of episode cards.

Only what the provider sends is available, and providers are inconsistent, so
every field degrades: backdrop falls back to the cover and then to the flat
theme colour, and an episode with no still of its own borrows the series cover.
Two of the six metadata rows are empty even in the reference screenshot.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ui import icons

POSTER_WIDTH = 260
CARD_WIDTH = 268
CARD_IMAGE_HEIGHT = 150
WATCHED_FRACTION = 0.95     # past this an episode counts as finished, not resumable


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------


def pick_resume(episodes, history):
    """Which episode the Continue button should offer, and from where.

    `episodes` is the season/episode-ordered list of dicts the page renders.
    `history` maps episode_id -> (position_secs, duration_secs, watched_at).

    Returns (episode, resume_secs) or (None, 0) when there is nothing to play.
    Netflix's rule: pick up the most recently watched episode where it was left,
    unless it was finished - then offer the next one from its start. Kept pure
    so the boundaries can be tested without a database or a display.
    """
    if not episodes:
        return None, 0
    by_id = {str(ep["episode_id"]): index for index, ep in enumerate(episodes)}

    latest_index, latest = None, None
    for episode_id, record in (history or {}).items():
        index = by_id.get(str(episode_id))
        if index is None:
            continue
        if latest is None or (record[2] or 0) > (latest[2] or 0):
            latest_index, latest = index, record

    if latest is None:
        return episodes[0], 0

    position, duration, _ = latest
    if duration > 0 and position / duration >= WATCHED_FRACTION:
        nxt = latest_index + 1
        if nxt < len(episodes):
            return episodes[nxt], 0
        return episodes[latest_index], 0      # end of the show: offer a rewatch
    return episodes[latest_index], int(max(0, position))


def watched_fraction(record) -> float:
    if not record:
        return 0.0
    position, duration = record[0], record[1]
    if not duration or duration <= 0:
        return 0.0
    return max(0.0, min(1.0, position / duration))


def describe_resume(episode, resume_secs: int) -> str:
    label = f"S{episode['season']:02d}E{episode['episode']:02d}"
    if resume_secs > 30:
        return f"Continue {label} · {resume_secs // 60} min in"
    return f"Play {label}"


def episode_caption(episode, show: str = ""):
    """Two lines naming an episode: (what it is, what show it is from).

    Provider episode titles are inconsistent. Some are a real name; many just
    repeat the show and the season/episode label straight back at you, which on
    a card reads as "S01E02 · Trailer Park Boys - S01E02". The label is always
    shown; the provider's name only earns its place when it says something the
    label does not.
    """
    label = f"S{episode.get('season', 0):02d}E{episode.get('episode', 0):02d}"
    name = (episode.get("title") or "").strip()
    for junk in (show or "", label):
        if junk:
            name = name.replace(junk, "").replace(junk.lower(), "")
    # Whatever punctuation the provider used to join them is now dangling.
    name = name.strip(" -–—·:|,")
    if not name or name.lower() == label.lower():
        return label, show
    return f"{label} · {name}", show


def next_episode(episodes, current_id):
    """The episode after `current_id`, or None at the end of the show.

    Deliberately does *not* wrap, unlike the ⏭ button: the show running out is
    the whole point of the end-of-show card, and a countdown that silently
    looped back to episode one would be worse than no countdown at all. Season
    boundaries need no special case — the list is already season/episode
    ordered, so the next entry is the next episode.
    """
    if not episodes:
        return None
    for index, episode in enumerate(episodes):
        if str(episode.get("episode_id")) != str(current_id):
            continue
        return episodes[index + 1] if index + 1 < len(episodes) else None
    return None                     # not in this show's list: offer nothing


# --------------------------------------------------------------------------
# a wrapping layout, which Qt does not ship
# --------------------------------------------------------------------------


class FlowLayout(QLayout):
    """Left-to-right layout that wraps onto a new line when it runs out of room."""

    def __init__(self, parent=None, margin=0, spacing=12):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(),
                             -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > area.right() and line_height > 0:
                x = area.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


# --------------------------------------------------------------------------
# pieces
# --------------------------------------------------------------------------


class MetaRow(QWidget):
    """One icon-labelled metadata line, as in the reference."""

    def __init__(self, glyph: str, parent=None):
        super().__init__(parent)
        self.setObjectName("metaRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        badge = QLabel()
        badge.setObjectName("metaBadge")
        badge.setPixmap(icons.pixmap(glyph, "#ffffff", 22))
        badge.setFixedWidth(26)
        badge.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        colon = QLabel(":")
        colon.setObjectName("metaColon")
        colon.setAlignment(Qt.AlignTop)

        self.value = QLabel("")
        self.value.setObjectName("metaValue")
        self.value.setWordWrap(True)
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.value.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        row.addWidget(badge)
        row.addWidget(colon)
        row.addWidget(self.value, 1)

    def set_text(self, text: str):
        self.value.setText(text or "")


class SeasonButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("seasonButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setProperty("on", False)

    def set_on(self, on: bool):
        if self.property("on") == bool(on):
            return
        self.setProperty("on", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)


class EpisodeCard(QFrame):
    """Poster tile for one episode, with a progress bar when partly watched."""

    activated = Signal(object)
    menuRequested = Signal(object, object)      # episode, global position

    def __init__(self, episode: dict, label: str, parent=None):
        super().__init__(parent)
        self.episode = episode
        self.setObjectName("episodeCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(CARD_WIDTH)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda point: self.menuRequested.emit(self.episode,
                                                  self.mapToGlobal(point)))
        self._pixmap = None
        self._progress = 0.0

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        self.image = QLabel()
        self.image.setFixedHeight(CARD_IMAGE_HEIGHT)
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setObjectName("episodeImage")

        self.caption = QLabel(label)
        self.caption.setObjectName("episodeCaption")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)

        box.addWidget(self.image)
        box.addWidget(self.caption)

    def set_pixmap(self, pixmap: QPixmap | None):
        self._pixmap = pixmap
        if pixmap is None or pixmap.isNull():
            self.image.setText("")
            return
        # Fill the width, then keep the TOP of the image rather than the middle.
        # Most episodes fall back to the series poster, whose title sits at the
        # top - a centre crop throws away the one part that identifies it.
        wide = pixmap.scaledToWidth(CARD_WIDTH, Qt.SmoothTransformation)
        if wide.height() > CARD_IMAGE_HEIGHT:
            wide = wide.copy(0, 0, CARD_WIDTH, CARD_IMAGE_HEIGHT)
        self.image.setPixmap(wide)

    def set_progress(self, fraction: float):
        self._progress = max(0.0, min(1.0, float(fraction or 0.0)))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._progress <= 0:
            return
        painter = QPainter(self)
        bar = QRect(0, CARD_IMAGE_HEIGHT - 5, self.width(), 5)
        painter.fillRect(bar, QColor("#1b2350"))
        painter.fillRect(QRect(bar.x(), bar.y(),
                               int(bar.width() * self._progress), bar.height()),
                         QColor(icons.ACCENT))

    def mouseDoubleClickEvent(self, event):
        self.activated.emit(self.episode)
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit(self.episode)
        super().mouseReleaseEvent(event)


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


class SeriesPage(QWidget):
    """Full-window series detail view."""

    backRequested = Signal()
    episodeActivated = Signal(object, int)       # episode dict, resume seconds
    episodeMenuRequested = Signal(object, object)

    def __init__(self, images, parent=None):
        super().__init__(parent)
        self.images = images
        self.setObjectName("seriesPage")
        self._backdrop_url = ""
        self._cover_url = ""
        self._episodes = []
        self._history = {}
        self._season_buttons = {}
        self._cards = []
        self._show = ""
        self._season = None

        images.loaded.connect(self._image_arrived)
        self._build()

    # ------------------------------------------------------------------ ui

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("seriesScroll")
        body = QWidget()
        body.setObjectName("seriesBody")
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        root = QVBoxLayout(body)
        root.setContentsMargins(28, 24, 28, 28)
        root.setSpacing(18)

        # header ---------------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(12)
        self.back_button = QPushButton()
        self.back_button.setObjectName("backButton")
        self.back_button.setIcon(icons.icon("back", 24, "#ffffff"))
        self.back_button.setIconSize(QSize(24, 24))
        self.back_button.setFixedSize(40, 36)
        self.back_button.setCursor(Qt.PointingHandCursor)
        self.back_button.setToolTip("Back to the catalog  (Esc)")
        self.back_button.clicked.connect(self.backRequested)
        self.title_label = QLabel("")
        self.title_label.setObjectName("seriesTitle")
        header.addWidget(self.back_button)
        header.addWidget(self.title_label, 1)
        root.addLayout(header)

        # poster + metadata ----------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(26)
        self.poster = QLabel()
        self.poster.setObjectName("seriesPoster")
        self.poster.setFixedWidth(POSTER_WIDTH)
        self.poster.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        top.addWidget(self.poster, 0, Qt.AlignTop)

        meta = QVBoxLayout()
        meta.setSpacing(9)
        self.rows = {}
        for key, glyph in (("director", "director"), ("release", "calendar"),
                           ("genre", "genre"), ("cast", "cast"),
                           ("rating", "star"), ("plot", "info")):
            row = MetaRow(glyph)
            self.rows[key] = row
            meta.addWidget(row)

        self.continue_button = QPushButton("")
        self.continue_button.setObjectName("continueButton")
        self.continue_button.setIcon(icons.icon("play", 18, "#06091c", "#06091c"))
        self.continue_button.setIconSize(QSize(18, 18))
        self.continue_button.setCursor(Qt.PointingHandCursor)
        self.continue_button.clicked.connect(self._continue_clicked)
        self.continue_button.hide()
        continue_row = QHBoxLayout()
        continue_row.addWidget(self.continue_button)
        continue_row.addStretch(1)
        meta.addSpacing(6)
        meta.addLayout(continue_row)
        meta.addStretch(1)
        top.addLayout(meta, 1)
        root.addLayout(top)

        # seasons ---------------------------------------------------------
        self.season_holder = QWidget()
        self.season_holder.setObjectName("seasonHolder")
        self.season_row = FlowLayout(self.season_holder, margin=0, spacing=8)
        root.addWidget(self.season_holder)

        # episodes --------------------------------------------------------
        self.episode_holder = QWidget()
        self.episode_holder.setObjectName("episodeHolder")
        self.episode_grid = FlowLayout(self.episode_holder, margin=0, spacing=12)
        root.addWidget(self.episode_holder)

        self.status = QLabel("")
        self.status.setObjectName("seriesStatus")
        root.addWidget(self.status)
        root.addStretch(1)

    # --------------------------------------------------------------- paint

    def paintEvent(self, event):
        """The darkened still behind everything.

        KeepAspectRatioByExpanding then centre-crop, so a wide backdrop fills a
        tall window without letterboxing; the veil keeps the text readable over
        whatever the provider happened to send.
        """
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#070b22"))
        pixmap = self._backdrop_pixmap()
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
            painter.drawPixmap(
                (self.width() - scaled.width()) // 2,
                (self.height() - scaled.height()) // 2, scaled)
            painter.fillRect(self.rect(), QColor(4, 6, 18, 214))
        super().paintEvent(event)

    def _backdrop_pixmap(self):
        for url in (self._backdrop_url, self._cover_url):
            if url:
                pixmap = self.images.get(url)
                if pixmap is not None and not pixmap.isNull():
                    return pixmap
        return None

    def _image_arrived(self, url: str):
        if url in (self._backdrop_url, self._cover_url):
            self.update()
            if url == self._cover_url:
                self._apply_poster()
        for card in self._cards:
            if card.episode.get("image_url") == url:
                card.set_pixmap(self.images.get(url))

    def _apply_poster(self):
        pixmap = self.images.get(self._cover_url) if self._cover_url else None
        if pixmap is not None and not pixmap.isNull():
            self.poster.setPixmap(pixmap.scaledToWidth(
                POSTER_WIDTH, Qt.SmoothTransformation))
        else:
            self.poster.setText("")

    # ---------------------------------------------------------------- data

    def set_loading(self, show: str):
        self._show = show
        self.title_label.setText(show)
        self.status.setText("Loading episodes…")
        self._clear(self.episode_grid)
        self._cards = []

    def set_error(self, message: str):
        self.status.setText(message)

    def set_series(self, show: str, info: dict, episodes: list, history: dict):
        """Populate everything. `info` may be empty - most fields then blank."""
        self._show = show
        self._episodes = list(episodes)
        self._history = dict(history or {})
        self.title_label.setText(show)

        self._cover_url = (info.get("cover") or "").strip()
        self._backdrop_url = (info.get("backdrop") or "").strip()
        self._apply_poster()
        self.update()

        rating = info.get("rating")
        self.rows["director"].set_text(info.get("director") or "")
        self.rows["release"].set_text(info.get("release_date") or "")
        self.rows["genre"].set_text(info.get("genre") or "")
        self.rows["cast"].set_text(info.get("cast_list") or "")
        self.rows["rating"].set_text(
            "" if rating in (None, "") else f"{float(rating):g}")
        self.rows["plot"].set_text(info.get("plot") or "")

        episode, resume = pick_resume(self._episodes, self._history)
        if episode is not None:
            self.continue_button.setText("  " + describe_resume(episode, resume))
            self.continue_button.show()
            self._resume_target = (episode, resume)
        else:
            self.continue_button.hide()
            self._resume_target = None

        self._build_seasons()
        # Open on the season the Continue button points at, not always season 1.
        seasons = self.seasons()
        self.show_season(episode["season"] if episode is not None
                         else (seasons[0] if seasons else 0))
        self.status.setText("" if self._episodes else "No episodes listed")

    def seasons(self) -> list:
        seen = []
        for episode in self._episodes:
            if episode["season"] not in seen:
                seen.append(episode["season"])
        return seen

    def _build_seasons(self):
        self._clear(self.season_row)
        self._season_buttons = {}
        for season in self.seasons():
            button = SeasonButton(f"Season {season}")
            button.clicked.connect(lambda _=False, s=season: self.show_season(s))
            self.season_row.addWidget(button)
            self._season_buttons[season] = button

    def show_season(self, season: int):
        self._season = season
        for value, button in self._season_buttons.items():
            button.set_on(value == season)

        self._clear(self.episode_grid)
        self._cards = []
        for episode in self._episodes:
            if episode["season"] != season:
                continue
            label = (f"{self._show} - S{episode['season']:02d}"
                     f"E{episode['episode']:02d}")
            card = EpisodeCard(episode, label)
            card.activated.connect(self._card_activated)
            card.menuRequested.connect(self.episodeMenuRequested)
            url = episode.get("image_url") or ""
            if url:
                card.set_pixmap(self.images.get(url))
            card.set_progress(watched_fraction(
                self._history.get(str(episode["episode_id"]))))
            self.episode_grid.addWidget(card)
            self._cards.append(card)
        self.episode_holder.updateGeometry()

    @staticmethod
    def _clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # ------------------------------------------------------------- actions

    def _card_activated(self, episode):
        record = self._history.get(str(episode["episode_id"]))
        fraction = watched_fraction(record)
        # Clicking a part-watched episode resumes it; a finished one restarts.
        resume = int(record[0]) if record and 0 < fraction < WATCHED_FRACTION else 0
        self.episodeActivated.emit(episode, resume)

    def _continue_clicked(self):
        if getattr(self, "_resume_target", None) is None:
            return
        episode, resume = self._resume_target
        self.episodeActivated.emit(episode, resume)

    # -------------------------------------------------- episode navigation

    def episodes(self) -> list:
        """Every episode across all seasons, in order - what next/previous walks."""
        return list(self._episodes)

    def index_of(self, episode_id) -> int:
        for index, episode in enumerate(self._episodes):
            if str(episode["episode_id"]) == str(episode_id):
                return index
        return -1

    def note_played(self, episode: dict):
        """Follow the played episode: select its season so the page matches."""
        if episode and episode.get("season") != self._season:
            self.show_season(episode["season"])
