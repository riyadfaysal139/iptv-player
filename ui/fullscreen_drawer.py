"""The fullscreen "more" drawer: other episodes of the show, or similar films.

Reached from the chevron under the floating control bar, or by rolling the
wheel down over the bar - YouTube's pull-up sheet, on a desktop. It is a
separate top-level window for the same reason the bar is: nothing in the main
window can be painted over libVLC's native video view, only another window
can sit on top of it.

Only ever built in fullscreen. The windowed layout has no use for it and is
left exactly as it was.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QStyle, QStyleOption,
    QVBoxLayout, QWidget,
)

from ui import icons

DRAWER_HEIGHT = 292
CARD_W, CARD_H = 236, 132          # landscape stills for episodes
POSTER_W, POSTER_H = 124, 186      # portrait posters for films
CAPTION_H = 42


class DrawerCard(QFrame):
    """One tile: a picture, a caption, and the payload to play when clicked."""

    chosen = Signal(object)

    def __init__(self, payload, label: str, image_url: str, portrait: bool,
                 current: bool, parent=None):
        super().__init__(parent)
        self.payload = payload
        self.image_url = image_url
        self._portrait = portrait
        self.setObjectName("drawerCard")
        self.setProperty("current", bool(current))
        self.setCursor(Qt.PointingHandCursor)
        self._w, self._h = (POSTER_W, POSTER_H) if portrait else (CARD_W, CARD_H)
        self.setFixedSize(self._w, self._h + CAPTION_H)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        self.image = QLabel()
        self.image.setObjectName("drawerImage")
        self.image.setFixedSize(self._w, self._h)
        self.image.setAlignment(Qt.AlignCenter)
        self.caption = QLabel(label)
        self.caption.setObjectName("drawerCaption")
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        self.caption.setFixedHeight(CAPTION_H)
        box.addWidget(self.image)
        box.addWidget(self.caption)

    def set_pixmap(self, pixmap: QPixmap | None):
        if pixmap is None or pixmap.isNull():
            return
        if self._portrait:
            scaled = pixmap.scaled(self._w, self._h, Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
            # Centre-crop a poster; its subject is in the middle.
            x = max(0, (scaled.width() - self._w) // 2)
            scaled = scaled.copy(x, 0, self._w, self._h)
        else:
            scaled = pixmap.scaledToWidth(self._w, Qt.SmoothTransformation)
            if scaled.height() > self._h:
                # Keep the top: series posters carry their title there.
                scaled = scaled.copy(0, 0, self._w, self._h)
        self.image.setPixmap(scaled)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.property("current"):
            painter = QPainter(self)
            painter.fillRect(QRect(0, self._h - 4, self.width(), 4),
                             QColor(icons.ACCENT))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.chosen.emit(self.payload)
        super().mousePressEvent(event)


class _RailScroll(QScrollArea):
    """A horizontal rail that takes the vertical wheel, as Netflix's rows do."""

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        drawer = self.parent()
        while drawer is not None and not isinstance(drawer, FullscreenDrawer):
            drawer = drawer.parent()
        if drawer is not None:
            drawer.wheel(delta)
        event.accept()


class FullscreenDrawer(QWidget):
    """A translucent sheet along the bottom of the screen."""

    itemChosen = Signal(object)
    seasonChosen = Signal(int)
    closeRequested = Signal()

    def __init__(self, images, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setObjectName("fullscreenDrawer")
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.images = images
        self._cards: list[DrawerCard] = []
        images.loaded.connect(self._image_loaded)

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 14, 40, 16)
        root.setSpacing(8)

        head = QHBoxLayout()
        self.title = QLabel("")
        self.title.setObjectName("drawerTitle")
        head.addWidget(self.title, 0)
        # Season chips, for a show: the row lists one season at a time.
        self.seasons = QWidget()
        self.seasons.setObjectName("drawerSeasons")
        self.seasons_layout = QHBoxLayout(self.seasons)
        self.seasons_layout.setContentsMargins(18, 0, 0, 0)
        self.seasons_layout.setSpacing(6)
        self._season_buttons: list[QPushButton] = []
        head.addWidget(self.seasons, 1, Qt.AlignLeft)
        self.close_button = QPushButton("⌃  Close")
        self.close_button.setObjectName("drawerClose")
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.clicked.connect(self.closeRequested)
        head.addWidget(self.close_button, 0)
        root.addLayout(head)

        self.scroll = _RailScroll()
        self.scroll.setObjectName("drawerScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.rail = QWidget()
        self.rail.setObjectName("drawerRail")
        self.rail_layout = QHBoxLayout(self.rail)
        self.rail_layout.setContentsMargins(0, 0, 0, 0)
        self.rail_layout.setSpacing(12)
        self.rail_layout.addStretch(1)
        self.scroll.setWidget(self.rail)
        root.addWidget(self.scroll, 1)

    def set_seasons(self, seasons, current):
        """Chips for each season; `seasons` empty hides the row (films)."""
        for button in self._season_buttons:
            self.seasons_layout.removeWidget(button)
            button.deleteLater()
        self._season_buttons = []
        for number in seasons:
            button = QPushButton(f"S{int(number)}")
            button.setObjectName("drawerSeason")
            button.setProperty("on", int(number) == int(current) if current is not None else False)
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda _=False, n=int(number): self.seasonChosen.emit(n))
            self.seasons_layout.addWidget(button)
            self._season_buttons.append(button)
        self.seasons.setVisible(bool(seasons))

    def scroll_rail(self, delta: int) -> bool:
        """A wheel notch moves the row. False when it was already at the end."""
        bar = self.scroll.horizontalScrollBar()
        before = bar.value()
        bar.setValue(before - delta)
        return bar.value() != before

    def wheel(self, delta: int):
        """Scrolling back past the start of the row puts the sheet away:
        the same gesture that opened it, run in reverse, closes it."""
        if delta > 0 and not self.scroll_rail(delta):
            self.closeRequested.emit()
        elif delta < 0:
            self.scroll_rail(delta)

    def set_items(self, title: str, items: list[dict], portrait: bool):
        """`items`: dicts with payload, label, image_url and an optional current flag."""
        self.title.setText(title)
        # A scroll area reports no content height of its own, so pin it to
        # the cards plus the scrollbar - otherwise the rail is clipped while
        # the sheet is sized from a hint that ignored it.
        card_h = (POSTER_H if portrait else CARD_H) + CAPTION_H
        self.scroll.setFixedHeight(card_h + self.scroll.horizontalScrollBar().sizeHint().height() + 4)
        for card in self._cards:
            self.rail_layout.removeWidget(card)
            card.deleteLater()
        self._cards = []
        current_card = None
        for index, item in enumerate(items):
            card = DrawerCard(item["payload"], item["label"], item.get("image_url") or "",
                              portrait, bool(item.get("current")))
            card.chosen.connect(self.itemChosen)
            card.set_pixmap(self.images.get(card.image_url))
            self.rail_layout.insertWidget(index, card)
            self._cards.append(card)
            if item.get("current"):
                current_card = card
        if current_card is not None:
            # Land with the playing item in view, not at the start of the row.
            self.scroll.ensureWidgetVisible(current_card, 200, 0)
        else:
            self.scroll.horizontalScrollBar().setValue(0)

    def _image_loaded(self, url: str):
        if not self.isVisible():
            return
        for card in self._cards:
            if card.image_url == url:
                card.set_pixmap(self.images.get(url))

    def paintEvent(self, event):
        # Subclassed QWidgets must paint their own stylesheet background.
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, option, painter, self)

    def wheelEvent(self, event):
        # Anywhere on the sheet the wheel moves the row - never the volume.
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.wheel(delta)
        event.accept()

    def place(self, area: QRect):
        # Anchored to the bottom edge; never shorter than its contents need,
        # or the layout would grow the window downwards off the screen.
        self.layout().activate()
        height = max(DRAWER_HEIGHT, self.sizeHint().height())
        self.setGeometry(area.x(), area.y() + area.height() - height,
                         area.width(), height)
