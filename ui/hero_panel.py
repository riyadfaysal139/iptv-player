"""The homepage's billboard: the title under the cursor, large, on its backdrop.

A TV-app front page. The right two thirds are the artwork - a landscape
backdrop when the provider has one, otherwise the poster blown up behind a
dark veil - fading into the page on the left, where the title, a line of
facts (rating, year, genre, seasons) and the synopsis sit.

Homepage only: the catalog grids and the series page keep their own layouts.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

HERO_HEIGHT = 224
TEXT_WIDTH_FRACTION = 0.46
PLOT_FOLD_CHARS = 190
PAGE_COLOUR = "#070b22"


def fold_plot(text: str, limit: int = PLOT_FOLD_CHARS) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > 0 else limit].rstrip(" ,.;:") + "…"


def facts_line(info: dict) -> str:
    """"★ 8.0   2020   Comedy / Drama   4 seasons" from whatever is known."""
    parts = []
    rating = info.get("rating")
    try:
        if rating is not None and float(rating) > 0:
            parts.append(f"★ {float(rating):.1f}")
    except (TypeError, ValueError):
        pass
    release = (info.get("release_date") or "").strip()
    if release:
        parts.append(release[:4] if release[:4].isdigit() else release)
    genre = (info.get("genre") or "").strip()
    if genre:
        parts.append(genre)
    seasons = info.get("seasons")
    if seasons:
        parts.append(f"{seasons} season" + ("s" if int(seasons) != 1 else ""))
    duration = (info.get("duration") or "").strip()
    if duration:
        parts.append(duration)
    if info.get("kind") == "live":
        parts.append("Live TV")
    return "   ".join(parts)


class HeroPanel(QWidget):
    """Backdrop painted by the widget; the words are plain labels over it."""

    def __init__(self, images, parent=None):
        super().__init__(parent)
        self.images = images
        self.setObjectName("heroPanel")
        self.setFixedHeight(HERO_HEIGHT)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._art_url = ""
        self._poster_url = ""
        self._scaled = None          # (url, width, height) -> the pixmap drawn last time
        images.loaded.connect(self._image_loaded)

        column = QVBoxLayout(self)
        column.setContentsMargins(36, 18, 0, 14)
        column.setSpacing(6)
        self.title = QLabel("")
        self.title.setObjectName("heroTitle")
        self.title.setWordWrap(True)
        self.facts = QLabel("")
        self.facts.setObjectName("heroFacts")
        self.plot = QLabel("")
        self.plot.setObjectName("heroPlot")
        self.plot.setWordWrap(True)
        self.plot.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.hint = QLabel("")
        self.hint.setObjectName("heroHint")
        column.addWidget(self.title)
        column.addWidget(self.facts)
        column.addWidget(self.plot, 1)
        column.addWidget(self.hint)
        column.addStretch(0)
        self.hide()

    # --------------------------------------------------------------- data

    def set_content(self, info: dict):
        """`info`: title, kind, art (backdrop url), poster, plot, rating,
        release_date, genre, seasons, duration, hint."""
        if not info:
            self.hide()
            return
        self.title.setText(info.get("title") or "")
        self.facts.setText(facts_line(info))
        self.facts.setVisible(bool(self.facts.text()))
        self.plot.setText(fold_plot(info.get("plot") or ""))
        self.plot.setVisible(bool(self.plot.text()))
        self.hint.setText(info.get("hint") or "")
        self.hint.setVisible(bool(self.hint.text()))
        self._art_url = info.get("art") or ""
        self._poster_url = info.get("poster") or ""
        self._scaled = None
        self.show()
        self.update()

    def _image_loaded(self, url: str):
        if url and url in (self._art_url, self._poster_url):
            self._scaled = None
            self.update()

    # -------------------------------------------------------------- paint

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scaled = None
        width = int(self.width() * TEXT_WIDTH_FRACTION)
        for label in (self.title, self.facts, self.plot, self.hint):
            label.setMaximumWidth(max(200, width))

    def _artwork(self):
        """The backdrop if it has arrived, else the poster, else nothing."""
        for url in (self._art_url, self._poster_url):
            if not url:
                continue
            pixmap = self.images.get(url)
            if pixmap is not None and not pixmap.isNull():
                return url, pixmap
        return "", None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        rect = self.rect()
        painter.fillRect(rect, QColor(PAGE_COLOUR))

        url, pixmap = self._artwork()
        if pixmap is not None:
            # The picture takes the right ~70%; a portrait poster is blown up
            # to cover it, which reads as a backdrop once the veil is on.
            art_rect = QRect(int(rect.width() * 0.30), 0, int(rect.width() * 0.70), rect.height())
            key = (url, art_rect.width(), art_rect.height())
            if self._scaled is None or self._scaled[0] != key:
                scaled = pixmap.scaled(art_rect.size(), Qt.KeepAspectRatioByExpanding,
                                       Qt.SmoothTransformation)
                self._scaled = (key, scaled)
            scaled = self._scaled[1]
            portrait = pixmap.height() > pixmap.width()
            # A poster's subject is near the top; a backdrop's is central.
            y = art_rect.top() - (0 if portrait else (scaled.height() - art_rect.height()) // 2)
            x = art_rect.left() - (scaled.width() - art_rect.width()) // 2
            painter.drawPixmap(x, y, scaled)
            if portrait:
                painter.fillRect(art_rect, QColor(0, 0, 0, 70))

            # Fade into the page on the left, where the text is, and along
            # the bottom, where the rails begin.
            fade = QLinearGradient(art_rect.left(), 0, art_rect.left() + art_rect.width() * 0.55, 0)
            fade.setColorAt(0.0, QColor(PAGE_COLOUR))
            fade.setColorAt(1.0, QColor(7, 11, 34, 0))
            painter.fillRect(art_rect, fade)
            bottom = QLinearGradient(0, rect.height() - 70, 0, rect.height())
            bottom.setColorAt(0.0, QColor(7, 11, 34, 0))
            bottom.setColorAt(1.0, QColor(PAGE_COLOUR))
            painter.fillRect(QRect(0, rect.height() - 70, rect.width(), 70), bottom)
