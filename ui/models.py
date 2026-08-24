"""Models and delegates for the catalog views.

Memory is the governing constraint: one category can hold 55,700 items. Qt's
model/view only renders the visible rows, so the model stores compact tuples
and never builds per-item widgets. Posters are fetched by a bounded thread pool
into an LRU pixmap cache with a disk cache behind it, so a long scroll cannot
grow without limit.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QRect, QRunnable, QSize, Qt,
    QThreadPool, Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from core.db import app_dir
from core.http import IMAGE_TIMEOUT, make_session

# Roles carrying our own payload.
ROLE_ITEM = Qt.UserRole + 1
ROLE_STREAM_ID = Qt.UserRole + 2
ROLE_KIND = Qt.UserRole + 3

POSTER_CACHE = 220              # pixmaps held in RAM
FAILURE_TTL = 7 * 24 * 3600     # re-try a dead image URL a week later
POSTER_W, POSTER_H = 132, 198
ROW_HEIGHT = 44


# --------------------------------------------------------------------------
# image loading
# --------------------------------------------------------------------------


class ImageCache(QObject):
    """Bounded LRU pixmap cache with an on-disk backing store."""

    loaded = Signal(str)
    # Emitted from worker threads; the queued connection hops the payload back
    # to the GUI thread, because QPixmap must not be touched off it.
    _fetched = Signal(str, bytes, str)

    def __init__(self, parent=None, capacity: int = POSTER_CACHE):
        super().__init__(parent)
        self._fetched.connect(self._on_fetched_main, Qt.QueuedConnection)
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._capacity = capacity
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._dir = app_dir() / "images"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pool = QThreadPool(self)
        # A sampled 32% of channel logos are dead (404/403/timeout) and a few
        # hosts hang until the timeout, so a small pool gets starved by them.
        self._pool.setMaxThreadCount(12)
        self._session = make_session()
        self._closing = False
        self._failed_path = self._dir / "failed.txt"
        self._load_failures()

    def shutdown(self):
        """Stop accepting work and drain in-flight fetches before teardown.

        Without this, a task finishing after the cache is gone emits on a
        deleted object and takes the process down on exit.
        """
        self._save_failures()
        self._closing = True
        self._pool.clear()
        self._pool.waitForDone(3000)
        self._cache.clear()

    def _disk_path(self, url: str) -> Path:
        return self._dir / hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]

    # -- negative cache ----------------------------------------------------
    #
    # Roughly a third of provider logo URLs are permanently dead. Remembering
    # that across runs stops the app re-requesting them on every launch.

    def _load_failures(self):
        try:
            raw = self._failed_path.read_text(encoding="utf-8")
        except OSError:
            return
        cutoff = time.time() - FAILURE_TTL
        for line in raw.splitlines():
            stamp, _, url = line.partition(" ")
            try:
                if float(stamp) > cutoff:
                    self._failed.add(url)
            except ValueError:
                continue

    def _save_failures(self):
        try:
            now = time.time()
            self._failed_path.write_text(
                "\n".join(f"{now} {u}" for u in list(self._failed)[:8000]),
                encoding="utf-8",
            )
        except OSError:
            pass

    def get(self, url: str) -> QPixmap | None:
        if not url or url in self._failed or self._closing:
            return None
        pixmap = self._cache.get(url)
        if pixmap is not None:
            self._cache.move_to_end(url)
            return pixmap
        if url in self._pending:
            return None

        path = self._disk_path(url)
        if path.exists():
            pixmap = QPixmap()
            if pixmap.load(str(path)):
                self._store(url, pixmap)
                return pixmap
            try:
                path.unlink()
            except OSError:
                pass

        self._pending.add(url)
        self._pool.start(_FetchTask(self, url, path))
        return None

    def _store(self, url: str, pixmap: QPixmap):
        self._cache[url] = pixmap
        self._cache.move_to_end(url)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)

    def _on_fetched_main(self, url: str, data: bytes, path_str: str):
        self._on_fetched(url, data, Path(path_str))

    def _on_fetched(self, url: str, data: bytes, path: Path):
        self._pending.discard(url)
        if not data:
            self._failed.add(url)
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self._failed.add(url)
            return
        scaled = pixmap.scaled(
            POSTER_W * 2, POSTER_H * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        try:
            scaled.save(str(path), "PNG")
        except Exception:
            pass
        self._store(url, scaled)
        self.loaded.emit(url)

    def clear(self):
        self._cache.clear()


class _FetchTask(QRunnable):
    def __init__(self, cache: ImageCache, url: str, path: Path):
        super().__init__()
        self.cache = cache
        self.url = url
        self.path = path

    def run(self):
        if self.cache._closing:
            return
        data = b""
        try:
            resp = self.cache._session.get(self.url, timeout=IMAGE_TIMEOUT)
            if resp.status_code == 200 and resp.content:
                data = resp.content
        except Exception:
            data = b""
        # Hand off via a queued signal; QPixmap is built on the GUI thread.
        # The cache may be torn down while this task was in flight.
        try:
            if not self.cache._closing:
                self.cache._fetched.emit(self.url, data, str(self.path))
        except RuntimeError:
            pass


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


class CatalogModel(QAbstractListModel):
    """Holds compact tuples; the view renders only what is visible."""

    FIELDS = (
        "stream_id", "name", "icon", "rating", "container_extension",
        "num", "available", "epg_channel_id", "added",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[tuple] = []
        self._kind = "live"
        self._kinds: list[str] | None = None
        self._favourites: set[str] = set()

    def set_rows(self, rows, kind: str, favourites: set[str] | None = None,
                 kinds=None):
        """`kinds` is a per-row kind, for lists that mix them.

        The homepage puts a film and a show in the same row, so one kind for
        the whole model is not enough there. Everywhere else there is exactly
        one kind and `kinds` stays None, which is what ROLE_KIND falls back to.
        """
        self.beginResetModel()
        self._rows = rows
        self._kind = kind
        self._kinds = list(kinds) if kinds is not None else None
        self._favourites = favourites or set()
        self.endResetModel()

    def kind(self) -> str:
        return self._kind

    def kind_at(self, row: int) -> str:
        """This row's own kind, or the model's when the list is not mixed."""
        if self._kinds is not None and 0 <= row < len(self._kinds):
            return self._kinds[row]
        return self._kind

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = self._rows[index.row()]
        if role == Qt.DisplayRole:
            return row[1]
        if role == ROLE_ITEM:
            return row
        if role == ROLE_STREAM_ID:
            return row[0]
        if role == ROLE_KIND:
            return self.kind_at(index.row())
        if role == Qt.ToolTipRole:
            if len(row) > 6 and not row[6]:
                return f"{row[1]}\n(No longer offered by the provider)"
            return row[1]
        return None

    def item_at(self, row: int):
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def is_favourite(self, stream_id: str) -> bool:
        return stream_id in self._favourites

    def set_favourites(self, favourites: set[str]):
        self._favourites = favourites
        if self._rows:
            top = self.index(0)
            bottom = self.index(len(self._rows) - 1)
            self.dataChanged.emit(top, bottom, [Qt.DecorationRole])


# --------------------------------------------------------------------------
# delegates
# --------------------------------------------------------------------------


class ChannelDelegate(QStyledItemDelegate):
    """Row layout for live TV: number, logo, name, favourite heart."""

    def __init__(self, images: ImageCache, model_getter, parent=None):
        super().__init__(parent)
        self.images = images
        self._model = model_getter

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        rect = option.rect
        row = index.data(ROLE_ITEM)
        selected = bool(option.state & QStyle.State_Selected)

        if selected:
            painter.fillRect(rect, QColor("#16204f"))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(rect, QColor("#101841"))

        available = row[6] if len(row) > 6 else 1
        text_colour = QColor("#f5d90a") if selected else QColor("#d7dcf0")
        if not available:
            text_colour = QColor("#5f6795")

        # index number
        painter.setPen(QPen(QColor("#5f6795")))
        num = row[5] if len(row) > 5 and row[5] else index.row() + 1
        painter.drawText(
            QRect(rect.left() + 6, rect.top(), 42, rect.height()),
            Qt.AlignVCenter | Qt.AlignRight, str(num),
        )

        # logo
        logo_rect = QRect(rect.left() + 58, rect.top() + 7, 46, ROW_HEIGHT - 14)
        pixmap = self.images.get(row[2]) if row[2] else None
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                logo_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            target = QRect(logo_rect)
            target.setSize(scaled.size())
            target.moveCenter(logo_rect.center())
            painter.drawPixmap(target, scaled)
        else:
            painter.setPen(QPen(QColor("#1b2350")))
            painter.drawRect(logo_rect)

        # name
        painter.setPen(QPen(text_colour))
        name_rect = QRect(rect.left() + 116, rect.top(), rect.width() - 160, rect.height())
        text = painter.fontMetrics().elidedText(
            row[1], Qt.ElideRight, name_rect.width()
        )
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, text)

        # favourite heart
        model = self._model()
        is_fav = model.is_favourite(row[0]) if model else False
        painter.setPen(QPen(QColor("#f5d90a") if is_fav else QColor("#414c85")))
        painter.drawText(
            QRect(rect.right() - 34, rect.top(), 26, rect.height()),
            Qt.AlignCenter, "♥" if is_fav else "♡",
        )
        painter.restore()

    @staticmethod
    def heart_rect(option_rect: QRect) -> QRect:
        return QRect(option_rect.right() - 34, option_rect.top(), 26, option_rect.height())


class PosterDelegate(QStyledItemDelegate):
    """Poster grid for movies and series, with a rating badge."""

    def __init__(self, images: ImageCache, model_getter, parent=None):
        super().__init__(parent)
        self.images = images
        self._model = model_getter

    def sizeHint(self, option, index) -> QSize:
        return QSize(POSTER_W + 14, POSTER_H + 40)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        row = index.data(ROLE_ITEM)
        selected = bool(option.state & QStyle.State_Selected)

        cell = option.rect.adjusted(5, 5, -5, -5)
        art = QRect(cell.left(), cell.top(), cell.width(), POSTER_H)

        if selected:
            painter.fillRect(option.rect, QColor("#16204f"))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, QColor("#0e1533"))

        pixmap = self.images.get(row[2]) if row[2] else None
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(art.size(), Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
            painter.save()
            painter.setClipRect(art)
            target = QRect(art)
            target.setSize(scaled.size())
            target.moveCenter(art.center())
            painter.drawPixmap(target, scaled)
            painter.restore()
        else:
            painter.fillRect(art, QColor("#0d1330"))
            painter.setPen(QPen(QColor("#3a4272")))
            painter.drawText(art, Qt.AlignCenter | Qt.TextWordWrap, row[1][:40])

        # rating badge
        rating = row[3] if len(row) > 3 else None
        if rating:
            try:
                value = float(rating)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                badge = QRect(art.left(), art.top(), 34, 19)
                painter.fillRect(badge, QColor("#c81e2a"))
                painter.setPen(QPen(QColor("#ffffff")))
                font = QFont(painter.font())
                font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(badge, Qt.AlignCenter, f"{value:.1f}")

        # favourite marker
        model = self._model()
        if model and model.is_favourite(row[0]):
            painter.setPen(QPen(QColor("#f5d90a")))
            painter.drawText(QRect(art.right() - 24, art.top() + 2, 20, 20),
                             Qt.AlignCenter, "♥")

        # title
        painter.setFont(QFont())
        painter.setPen(QPen(QColor("#f5d90a") if selected else QColor("#c6cdea")))
        title = QRect(cell.left(), art.bottom() + 4, cell.width(), 32)
        text = painter.fontMetrics().elidedText(row[1], Qt.ElideRight, title.width() * 2)
        painter.drawText(title, Qt.AlignTop | Qt.AlignHCenter | Qt.TextWordWrap, text)
        painter.restore()
