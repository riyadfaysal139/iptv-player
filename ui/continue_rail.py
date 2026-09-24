"""The homepage's Continue Watching row, drawn the way a TV app draws it.

The first card is a large landscape still of what you were watching last; the
rest are posters. Every card carries a progress bar, and the one under the
cursor grows a little. Under the row, a caption names the episode the cursor
is on and how much of it is left.

It keeps the HomeRail interface the page's cursor logic drives - `model`,
`view.scrollTo`, `set_cursor`, `activate`, `rows` and the same signals - so the
page treats it as just another rail.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ui.models import HEART, ROLE_ITEM, ROLE_KIND, CatalogModel, POSTER_H, POSTER_W

# The first card is landscape, as tall as the posters beside it: the billboard
# above the rails is where the big picture lives now.
HERO_W, HERO_H = 352, POSTER_H
CARD_GAP = 16
FOCUS_GROW = 1.06                   # the cursor's card, a little bigger
HALO = 14                           # room for that growth around every card
RADIUS = 6
PROGRESS_H = 5
PROGRESS_COLOUR = "#e50914"
PROGRESS_TRACK = "#3a3a3a"
CURSOR_COLOUR = "#ffffff"


def minutes_left(position: int, duration: int) -> str:
    """"20m left", or "" when nothing useful is known."""
    if not duration or duration <= 0:
        return ""
    left = max(0, int(duration) - int(position or 0))
    if left < 60:
        return "Almost done"
    hours, minutes = divmod(left // 60, 60)
    return f"{hours}h {minutes}m left" if hours else f"{minutes}m left"


def _rounded(rect: QRect) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, RADIUS, RADIUS)
    return path


class _Strip(QWidget):
    """The cards themselves: painted by hand, one widget for the whole row."""

    pressed = Signal(int)
    activated = Signal(int)
    heartClicked = Signal(int)
    menuRequested = Signal(int, object)     # column, global position

    def __init__(self, rail, parent=None):
        super().__init__(parent)
        self.rail = rail
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = -1

    @staticmethod
    def heart_rect(card: QRect) -> QRect:
        return QRect(card.right() - HEART - 6, card.top() + 6, HEART, HEART)

    def scaled_rect(self, rect: QRect, card: QRect) -> QRect:
        """`rect` as the focused card's growth moves it (scale about centre)."""
        centre = card.center()
        left = centre.x() + (rect.left() - centre.x()) * FOCUS_GROW
        top = centre.y() + (rect.top() - centre.y()) * FOCUS_GROW
        return QRect(int(left), int(top), int(rect.width() * FOCUS_GROW),
                     int(rect.height() * FOCUS_GROW))

    def heart_at(self, point: QPoint) -> int:
        """The column whose heart is under `point`, or -1."""
        cursor = self.rail.cursor_column
        for column in range(self.rail.rows()):
            card = self.card_rect(column)
            heart = self.heart_rect(card)
            if column == cursor:
                heart = self.scaled_rect(heart, card)
            if heart.contains(point):
                return column
        return -1

    # ------------------------------------------------------------ layout

    def card_rect(self, column: int) -> QRect:
        """Where card `column` sits, at rest (before any growth)."""
        x = HALO
        bottom = HALO + HERO_H
        for index in range(column + 1):
            width = HERO_W if index == 0 else POSTER_W
            height = HERO_H if index == 0 else POSTER_H
            if index == column:
                return QRect(x, bottom - height, width, height)
            x += width + CARD_GAP
        return QRect()

    def sizeHint(self) -> QSize:
        count = self.rail.rows()
        if count == 0:
            return QSize(0, HERO_H + 2 * HALO)
        last = self.card_rect(count - 1)
        return QSize(last.right() + 1 + HALO, HERO_H + 2 * HALO)

    def column_at(self, point: QPoint) -> int:
        for column in range(self.rail.rows()):
            if self.card_rect(column).contains(point):
                return column
        return -1

    # ------------------------------------------------------------- paint

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        cursor = self.rail.cursor_column
        # The cursor's card is painted last so its growth overlaps its neighbours.
        order = [c for c in range(self.rail.rows()) if c != cursor]
        if cursor is not None and 0 <= cursor < self.rail.rows():
            order.append(cursor)
        for column in order:
            self._paint_card(painter, column, column == cursor)

    def _paint_card(self, painter: QPainter, column: int, focused: bool):
        rect = self.card_rect(column)
        row = self.rail.model.index(column, 0).data(ROLE_ITEM)
        if row is None:
            return
        meta = self.rail.meta_for(column)
        painter.save()
        if focused:
            centre = rect.center()
            painter.translate(centre)
            painter.scale(FOCUS_GROW, FOCUS_GROW)
            painter.translate(-centre)

        painter.setClipPath(_rounded(rect))
        painter.fillRect(rect, QColor("#101426"))
        pixmap = self.rail.pixmap_for(column, landscape=(column == 0))
        if pixmap is not None and not pixmap.isNull():
            self._draw_cover(painter, rect, pixmap)

        if column == 0:
            # The title sits on a fade at the foot of the still, TV-style.
            fade = QLinearGradient(rect.left(), rect.bottom() - 70, rect.left(), rect.bottom())
            fade.setColorAt(0.0, QColor(0, 0, 0, 0))
            fade.setColorAt(1.0, QColor(0, 0, 0, 200))
            painter.fillRect(QRect(rect.left(), rect.bottom() - 70, rect.width(), 70), fade)
            font = QFont(painter.font())
            font.setPointSize(13)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#ffffff"))
            text_rect = QRect(rect.left() + 12, rect.bottom() - 44, rect.width() - 24, 30)
            name = painter.fontMetrics().elidedText(str(row[1]), Qt.ElideRight, text_rect.width())
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, name)

        fraction = meta.get("fraction", 0.0)
        track = QRect(rect.left(), rect.bottom() - PROGRESS_H + 1, rect.width(), PROGRESS_H)
        painter.fillRect(track, QColor(PROGRESS_TRACK))
        painter.fillRect(QRect(track.left(), track.top(), int(track.width() * fraction), track.height()),
                         QColor(PROGRESS_COLOUR))
        painter.setClipping(False)

        if focused:
            painter.setPen(QPen(QColor(CURSOR_COLOUR), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(_rounded(rect.adjusted(1, 1, -1, -1)))

        # The favourite heart: lit when it is one, offered on hover/cursor.
        favourite = self.rail.is_favourite(column)
        if favourite or focused or column == self._hover:
            heart = self.heart_rect(rect)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawEllipse(heart)
            font = QFont(painter.font())
            font.setPointSizeF(11)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor("#f5d90a") if favourite else QColor("#ffffff"))
            painter.drawText(heart.adjusted(0, -1, 0, 0), Qt.AlignCenter, "♥" if favourite else "♡")
        painter.restore()

    @staticmethod
    def _draw_cover(painter: QPainter, rect: QRect, pixmap: QPixmap):
        """Fill the card. A landscape still is cropped to fit; a portrait poster
        in a landscape card gets a blurred copy of itself behind it."""
        landscape_card = rect.width() > rect.height()
        portrait_art = pixmap.height() > pixmap.width()
        if landscape_card and portrait_art:
            # The cheap blur: shrink hard, then stretch back up.
            tiny = pixmap.scaled(16, 9, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap(rect, tiny.scaled(rect.size(), Qt.IgnoreAspectRatio,
                                                 Qt.SmoothTransformation))
            painter.fillRect(rect, QColor(0, 0, 0, 90))
            sharp = pixmap.scaledToHeight(rect.height(), Qt.SmoothTransformation)
            painter.drawPixmap(rect.left() + (rect.width() - sharp.width()) // 2, rect.top(), sharp)
            return
        scaled = pixmap.scaled(rect.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        x = rect.left() - (scaled.width() - rect.width()) // 2
        y = rect.top() - (scaled.height() - rect.height()) // 2
        painter.drawPixmap(x, y, scaled)

    # ------------------------------------------------------------- mouse

    def mouseMoveEvent(self, event):
        column = self.column_at(event.position().toPoint())
        if column != self._hover:
            self._hover = column
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        point = event.position().toPoint()
        if event.button() == Qt.LeftButton and self.heart_at(point) >= 0:
            return                      # the release does the work
        column = self.column_at(point)
        if column >= 0 and event.button() == Qt.LeftButton:
            self.pressed.emit(column)
        elif column >= 0 and event.button() == Qt.RightButton:
            self.pressed.emit(column)
            self.menuRequested.emit(column, event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            column = self.heart_at(event.position().toPoint())
            if column >= 0:
                self.heartClicked.emit(column)
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        point = event.position().toPoint()
        if self.heart_at(point) >= 0:
            return
        column = self.column_at(point)
        if column >= 0 and event.button() == Qt.LeftButton:
            self.activated.emit(column)
        super().mouseDoubleClickEvent(event)


class _StripScroll(QScrollArea):
    """Sideways only; the vertical wheel belongs to the wall."""

    def scrollTo(self, index: QModelIndex, _hint=None):
        strip = self.widget()
        if strip is None or not index.isValid():
            return
        rect = strip.card_rect(index.row()).adjusted(-HALO, 0, HALO, 0)
        self.ensureVisible(rect.left(), 0, 0, 0)
        self.ensureVisible(rect.right(), 0, 0, 0)

    def wheelEvent(self, event):
        event.ignore()


class ContinueRail(QWidget):
    """Continue Watching, with the HomeRail surface the page expects."""

    activated = Signal(str, object)      # kind, row
    favouriteToggled = Signal(str, object)
    menuRequested = Signal(str, object, object)   # kind, row, global position
    seeAllRequested = Signal(object)
    unpinRequested = Signal(object)
    cursorRequested = Signal(str, int)
    moveRequested = Signal(str, int)

    def __init__(self, key: str, title: str, images, parent=None):
        super().__init__(parent)
        self.key = key
        self.target = None
        self.images = images
        self.setObjectName("homeRail")
        self.cursor_column = None
        self._meta = []             # per row: {"fraction", "caption", "still"}
        self.meta_provider = None   # callable(kind, row) -> dict, set by the page

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.heading = QLabel(title)
        self.heading.setObjectName("railHeading")
        header.addWidget(self.heading)
        # The same row controls every rail has, so Ctrl+arrows and the page's
        # button sync keep working; "See all" has nowhere to go for this rail.
        self.see_all = QPushButton("See all  →")
        self.see_all.setObjectName("seeAllButton")
        self.see_all.hide()
        self.unpin = QPushButton("✕ Remove")
        self.unpin.setObjectName("unpinButton")
        self.unpin.hide()
        self.move_up = self._move_button("▲", -1, "Move this row up  (Ctrl+↑)")
        self.move_down = self._move_button("▼", 1, "Move this row down  (Ctrl+↓)")
        header.addSpacing(4)
        header.addWidget(self.move_up)
        header.addWidget(self.move_down)
        header.addStretch(1)
        box.addLayout(header)

        self.model = CatalogModel(self)
        self.strip = _Strip(self)
        self.strip.pressed.connect(lambda column: self.cursorRequested.emit(self.key, column))
        self.strip.activated.connect(self.activate)
        self.strip.heartClicked.connect(self._heart_clicked)
        self.strip.menuRequested.connect(self._menu_requested)
        self.view = _StripScroll()
        self.view.setObjectName("continueScroll")
        self.view.setWidget(self.strip)
        self.view.setWidgetResizable(False)
        self.view.setFrameShape(QScrollArea.NoFrame)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setFixedHeight(HERO_H + 2 * HALO)
        self.view.setFocusPolicy(Qt.NoFocus)
        box.addWidget(self.view)

        self.caption = QLabel("")
        self.caption.setObjectName("continueCaption")
        self.caption.setContentsMargins(HALO, 0, 0, 0)
        self.left_label = QLabel("")
        self.left_label.setObjectName("continueLeft")
        self.left_label.setContentsMargins(HALO, 0, 0, 0)
        box.addWidget(self.caption)
        box.addWidget(self.left_label)

        images.loaded.connect(lambda _url: self.strip.update())

    def _move_button(self, glyph: str, delta: int, tip: str) -> QPushButton:
        button = QPushButton(glyph)
        button.setObjectName("moveRailButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        button.setToolTip(tip)
        button.clicked.connect(lambda: self.moveRequested.emit(self.key, delta))
        return button

    # ------------------------------------------------------------- rail API

    def set_target(self, target, pinned: bool = False):
        self.target = target

    def set_rows(self, rows, kinds):
        self.model.set_rows(rows, kinds[0] if kinds else "movie", set(), kinds)
        self._meta = []
        for column, row in enumerate(rows):
            kind = kinds[column] if column < len(kinds) else "movie"
            meta = {}
            if self.meta_provider is not None:
                try:
                    meta = self.meta_provider(kind, row) or {}
                except Exception:
                    meta = {}
            self._meta.append(meta)
        self.strip.resize(self.strip.sizeHint())
        self.strip.update()
        self._show_caption()
        self.setVisible(bool(rows))

    def rows(self) -> int:
        return self.model.rowCount()

    def meta_for(self, column: int) -> dict:
        return self._meta[column] if 0 <= column < len(self._meta) else {}

    def pixmap_for(self, column: int, landscape: bool):
        row = self.model.index(column, 0).data(ROLE_ITEM)
        if row is None:
            return None
        still = self.meta_for(column).get("still") if landscape else None
        if still:
            pixmap = self.images.get(still)
            if pixmap is not None and not pixmap.isNull():
                return pixmap
        return self.images.get(row[2] or "")

    def set_cursor(self, column):
        if column is None or not 0 <= column < self.rows():
            self.cursor_column = None
        else:
            self.cursor_column = int(column)
        self.strip.update()
        self._show_caption()
        return self.model.index(self.cursor_column, 0) if self.cursor_column is not None else None

    def activate(self, column: int):
        if 0 <= column < self.rows():
            index = self.model.index(column, 0)
            self.activated.emit(index.data(ROLE_KIND) or "movie", index.data(ROLE_ITEM))

    def is_favourite(self, column: int) -> bool:
        index = self.model.index(column, 0)
        row = index.data(ROLE_ITEM)
        return bool(row) and self.model.is_favourite(row[0], column)

    def _heart_clicked(self, column: int):
        index = self.model.index(column, 0)
        row = index.data(ROLE_ITEM)
        if row is not None:
            self.favouriteToggled.emit(index.data(ROLE_KIND) or "movie", row)
            self.strip.update()

    def _menu_requested(self, column: int, position):
        index = self.model.index(column, 0)
        row = index.data(ROLE_ITEM)
        if row is not None:
            self.menuRequested.emit(index.data(ROLE_KIND) or "movie", row, position)

    def repaint_cards(self):
        self.strip.update()

    def _show_caption(self):
        """The episode under the cursor - or the hero when nothing is."""
        column = self.cursor_column if self.cursor_column is not None else 0
        meta = self.meta_for(column)
        parts = [p for p in (meta.get("caption", ""), meta.get("left", "")) if p]
        self.caption.setText("   ·   ".join(parts))
        self.caption.setVisible(bool(parts))
        self.left_label.hide()
