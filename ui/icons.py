"""Painted transport icons.

The obvious way to build a control bar is Unicode glyphs (▶ ⏮ 🔊). That was the
first attempt and it does not survive contact with three platforms: several of
the transport codepoints live in the emoji block, so macOS and Windows render
them in *colour*, at their own size, ignoring the stylesheet — which defeats the
whole point of matching this app's palette. The rest depend on fonts that are
not guaranteed to be installed on Linux.

So the icons are drawn. Every shape fits a 100×100 logical box and is scaled to
the requested size, and each icon carries three pixmaps — normal, hover
(accent), disabled — so Qt swaps them without any stylesheet involvement.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)

FG = "#d7dcf0"
ACCENT = "#f5d90a"
DIM = "#4d5480"
RECORD = "#e0424f"

_CACHE: dict = {}


# --------------------------------------------------------------------------
# shapes, all on a 100x100 canvas
# --------------------------------------------------------------------------


def _fill(painter: QPainter, colour: QColor):
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(colour))


def _stroke(painter: QPainter, colour: QColor, width: float = 9.0,
            cap=Qt.RoundCap):
    pen = QPen(colour, width)
    pen.setCapStyle(cap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)


def _triangle(painter: QPainter, points):
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    path.closeSubpath()
    painter.drawPath(path)


def _arrow_head(painter: QPainter, tip, back, spread=24.0):
    """A small filled triangle pointing from `back` towards `tip`."""
    import math

    angle = math.atan2(tip[1] - back[1], tip[0] - back[0])
    length = 22.0
    left = (tip[0] - length * math.cos(angle - math.radians(spread)),
            tip[1] - length * math.sin(angle - math.radians(spread)))
    right = (tip[0] - length * math.cos(angle + math.radians(spread)),
             tip[1] - length * math.sin(angle + math.radians(spread)))
    _triangle(painter, [tip, left, right])


def _play(p, c):
    _fill(p, c)
    _triangle(p, [(26, 14), (86, 50), (26, 86)])


def _pause(p, c):
    _fill(p, c)
    p.drawRoundedRect(QRectF(26, 16, 16, 68), 4, 4)
    p.drawRoundedRect(QRectF(58, 16, 16, 68), 4, 4)


def _stop(p, c):
    _fill(p, c)
    p.drawRoundedRect(QRectF(22, 22, 56, 56), 5, 5)


def _previous(p, c):
    _fill(p, c)
    p.drawRoundedRect(QRectF(18, 18, 12, 64), 3, 3)
    _triangle(p, [(86, 18), (38, 50), (86, 82)])


def _next(p, c):
    _fill(p, c)
    _triangle(p, [(14, 18), (62, 50), (14, 82)])
    p.drawRoundedRect(QRectF(70, 18, 12, 64), 3, 3)


def _fullscreen(p, c):
    _stroke(p, c, 10)
    arm, inset = 24, 16
    for x_dir, y_dir, x0, y0 in ((1, 1, inset, inset), (-1, 1, 100 - inset, inset),
                                 (1, -1, inset, 100 - inset), (-1, -1, 100 - inset, 100 - inset)):
        p.drawLine(QPointF(x0, y0), QPointF(x0 + arm * x_dir, y0))
        p.drawLine(QPointF(x0, y0), QPointF(x0, y0 + arm * y_dir))


def _effects(p, c):
    """An equaliser, which is what VLC's extended-settings button shows."""
    _stroke(p, c, 8)
    for y in (26, 50, 74):
        p.drawLine(QPointF(14, y), QPointF(86, y))
    _fill(p, c)
    for x, y in ((66, 26), (32, 50), (72, 74)):
        p.drawEllipse(QPointF(x, y), 11, 11)


def _playlist(p, c):
    _stroke(p, c, 10)
    for y in (28, 50, 72):
        p.drawLine(QPointF(16, y), QPointF(84, y))


def _loop(p, c):
    # Arc from 60 degrees counter-clockwise by 300, leaving the gap on the
    # right where the arrow head goes.
    _stroke(p, c, 9, Qt.FlatCap)
    p.drawArc(QRectF(20, 24, 60, 52), 60 * 16, 300 * 16)
    _fill(p, c)
    _arrow_head(p, (80, 38), (80, 62))


def _loop_one(p, c):
    _loop(p, c)
    _stroke(p, c, 8, Qt.FlatCap)
    p.drawLine(QPointF(50, 38), QPointF(50, 64))
    p.drawLine(QPointF(41, 46), QPointF(50, 38))


def _shuffle(p, c):
    _stroke(p, c, 9, Qt.FlatCap)
    p.drawLine(QPointF(12, 30), QPointF(70, 70))
    p.drawLine(QPointF(12, 70), QPointF(70, 30))
    _fill(p, c)
    _arrow_head(p, (88, 82), (70, 70))
    _arrow_head(p, (88, 18), (70, 30))


def _record(p, c):
    _fill(p, c)
    p.drawEllipse(QPointF(50, 50), 27, 27)


def _snapshot(p, c):
    _stroke(p, c, 8)
    p.drawRoundedRect(QRectF(12, 30, 76, 52), 9, 9)
    p.drawEllipse(QPointF(50, 56), 16, 16)
    _fill(p, c)
    p.drawRoundedRect(QRectF(36, 18, 28, 14), 4, 4)


def _frame_step(p, c):
    _stroke(p, c, 8)
    p.drawRoundedRect(QRectF(12, 26, 76, 48), 8, 8)
    _fill(p, c)
    _triangle(p, [(40, 33), (70, 50), (40, 67)])


def _volume(p, c):
    _fill(p, c)
    _triangle(p, [(12, 38), (30, 38), (52, 18), (52, 82), (30, 62), (12, 62)])
    _stroke(p, c, 8)
    for radius in (16, 28):
        p.drawArc(QRectF(52 - radius, 50 - radius, radius * 2, radius * 2),
                  -50 * 16, 100 * 16)


def _volume_muted(p, c):
    _fill(p, c)
    _triangle(p, [(12, 38), (30, 38), (52, 18), (52, 82), (30, 62), (12, 62)])
    _stroke(p, c, 9)
    p.drawLine(QPointF(64, 36), QPointF(90, 64))
    p.drawLine(QPointF(90, 36), QPointF(64, 64))


def _back(p, c):
    _stroke(p, c, 10, Qt.FlatCap)
    p.drawLine(QPointF(24, 50), QPointF(84, 50))
    _fill(p, c)
    _arrow_head(p, (16, 50), (46, 50), spread=30.0)


def _search(p, c):
    _stroke(p, c, 10)
    p.drawEllipse(QPointF(44, 42), 26, 26)
    _stroke(p, c, 12, Qt.RoundCap)
    p.drawLine(QPointF(63, 61), QPointF(86, 84))


def _home(p, c):
    """A house: roof drawn as a stroked V so it reads at 19px, walls below."""
    _stroke(p, c, 10)
    p.drawLine(QPointF(10, 48), QPointF(50, 16))
    p.drawLine(QPointF(50, 16), QPointF(90, 48))
    _stroke(p, c, 9)
    p.drawLine(QPointF(22, 46), QPointF(22, 86))
    p.drawLine(QPointF(78, 46), QPointF(78, 86))
    p.drawLine(QPointF(22, 86), QPointF(78, 86))
    # The doorway, left open at the bottom so it is not a solid block.
    p.drawLine(QPointF(42, 86), QPointF(42, 62))
    p.drawLine(QPointF(42, 62), QPointF(58, 62))
    p.drawLine(QPointF(58, 62), QPointF(58, 86))


def _director(p, c):
    """A megaphone — the reference's glyph for the director row."""
    _fill(p, c)
    _triangle(p, [(20, 40), (54, 22), (54, 78), (20, 60)])
    p.drawRoundedRect(QRectF(10, 40, 12, 20), 4, 4)
    _stroke(p, c, 7)
    for radius in (14, 26):
        p.drawArc(QRectF(54 - radius, 50 - radius, radius * 2, radius * 2),
                  -48 * 16, 96 * 16)


def _calendar(p, c):
    _stroke(p, c, 8)
    p.drawRoundedRect(QRectF(12, 24, 76, 62), 8, 8)
    p.drawLine(QPointF(12, 44), QPointF(88, 44))
    _stroke(p, c, 8, Qt.RoundCap)
    p.drawLine(QPointF(32, 14), QPointF(32, 30))
    p.drawLine(QPointF(68, 14), QPointF(68, 30))
    _fill(p, c)
    for x in (28, 46, 64):
        for y in (56, 72):
            p.drawEllipse(QPointF(x, y), 5, 5)


def _genre(p, c):
    """A record with a note, for the genre row."""
    _stroke(p, c, 8)
    p.drawEllipse(QPointF(46, 54), 34, 34)
    _fill(p, c)
    p.drawEllipse(QPointF(46, 54), 7, 7)
    _stroke(p, c, 7, Qt.FlatCap)
    p.drawLine(QPointF(62, 26), QPointF(62, 56))
    p.drawLine(QPointF(62, 26), QPointF(86, 18))
    _fill(p, c)
    p.drawEllipse(QPointF(56, 58), 9, 7)


def _cast(p, c):
    """Two theatre masks.

    The back mask is only a crescent - the part that is not hidden behind the
    front one. Drawing two full overlapping ellipses merged into a single blob
    at 22px, which is the size this is actually used at.
    """
    _stroke(p, c, 7, Qt.FlatCap)
    p.drawArc(QRectF(40, 16, 52, 62), 300 * 16, 130 * 16)
    _stroke(p, c, 8)
    p.drawEllipse(QRectF(10, 20, 54, 62))
    _fill(p, c)
    p.drawEllipse(QPointF(27, 44), 5, 6)
    p.drawEllipse(QPointF(47, 44), 5, 6)
    _stroke(p, c, 7, Qt.RoundCap)
    p.drawArc(QRectF(24, 50, 26, 22), 200 * 16, 140 * 16)


def _star(p, c):
    import math

    _fill(p, c)
    points = []
    for index in range(10):
        radius = 40.0 if index % 2 == 0 else 17.0
        angle = math.radians(-90 + index * 36)
        points.append((50 + radius * math.cos(angle), 52 + radius * math.sin(angle)))
    _triangle(p, points)


def _info(p, c):
    _stroke(p, c, 8)
    p.drawEllipse(QPointF(50, 50), 36, 36)
    _fill(p, c)
    p.drawEllipse(QPointF(50, 30), 6, 6)
    p.drawRoundedRect(QRectF(44, 42, 12, 32), 5, 5)


def _pip(p, c):
    """A screen with a smaller picture inset at the bottom right."""
    _stroke(p, c, 8)
    p.drawRoundedRect(QRectF(12, 22, 76, 56), 9, 9)
    _fill(p, c)
    p.drawRoundedRect(QRectF(48, 46, 32, 24), 4, 4)


def _subtitles(p, c):
    _stroke(p, c, 8)
    p.drawRoundedRect(QRectF(10, 22, 80, 56), 9, 9)
    _fill(p, c)
    for y, (x0, x1) in ((50, (22, 50)), (50, (58, 78)), (64, (22, 42)), (64, (50, 78))):
        p.drawRoundedRect(QRectF(x0, y, x1 - x0, 7), 3, 3)


def _audio(p, c):
    _stroke(p, c, 9, Qt.FlatCap)
    p.drawLine(QPointF(38, 22), QPointF(38, 70))
    p.drawLine(QPointF(78, 14), QPointF(78, 62))
    p.drawLine(QPointF(38, 22), QPointF(78, 14))
    _fill(p, c)
    p.drawEllipse(QPointF(28, 70), 14, 12)
    p.drawEllipse(QPointF(68, 62), 14, 12)


SHAPES = {
    "play": _play,
    "pause": _pause,
    "stop": _stop,
    "previous": _previous,
    "next": _next,
    "fullscreen": _fullscreen,
    "pip": _pip,
    "effects": _effects,
    "playlist": _playlist,
    "loop": _loop,
    "loop_one": _loop_one,
    "shuffle": _shuffle,
    "record": _record,
    "snapshot": _snapshot,
    "frame_step": _frame_step,
    "volume": _volume,
    "volume_muted": _volume_muted,
    "subtitles": _subtitles,
    "audio": _audio,
    "back": _back,
    "search": _search,
    "home": _home,
    "director": _director,
    "calendar": _calendar,
    "genre": _genre,
    "cast": _cast,
    "star": _star,
    "info": _info,
}


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------


def pixmap(name: str, colour: str = FG, size: int = 20, ratio: float = 2.0) -> QPixmap:
    key = (name, colour, size, ratio)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    canvas = QPixmap(int(size * ratio), int(size * ratio))
    canvas.setDevicePixelRatio(ratio)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    # setDevicePixelRatio means the painter already works in *logical* units,
    # so the shapes scale to `size`, not to the backing-store pixel count.
    painter.scale(size / 100.0, size / 100.0)
    SHAPES.get(name, _play)(painter, QColor(colour))
    painter.end()

    _CACHE[key] = canvas
    return canvas


def icon(name: str, size: int = 20, colour: str = FG,
         hover: str = ACCENT, disabled: str = DIM) -> QIcon:
    """One icon carrying its own normal / hover / disabled artwork.

    Qt picks Active on hover and Disabled when the widget is disabled, so the
    bar gets its accent-on-hover behaviour without a stylesheet rule for icons
    (stylesheets cannot recolour a pixmap).
    """
    result = QIcon()
    result.addPixmap(pixmap(name, colour, size), QIcon.Normal)
    result.addPixmap(pixmap(name, hover, size), QIcon.Active)
    result.addPixmap(pixmap(name, disabled, size), QIcon.Disabled)
    return result
