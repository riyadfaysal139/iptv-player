#!/usr/bin/env python3
"""Generate the app icon in every format the installers need.

Produces assets/icon.png (1024), icon.icns (macOS) and icon.ico (Windows).
Run from the project root:  .venv/bin/python packaging/make_icons.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
SIZE = 1024

NAVY_TOP = "#141d4e"
NAVY_BOTTOM = "#05081a"
YELLOW = "#f5d90a"
RED = "#d81f2a"


def draw(size: int = SIZE) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    p = QPainter(pixmap)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    s = size / 1024.0

    # rounded background
    gradient = QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0.0, QColor(NAVY_TOP))
    gradient.setColorAt(1.0, QColor(NAVY_BOTTOM))
    body = QPainterPath()
    body.addRoundedRect(QRectF(0, 0, size, size), 224 * s, 224 * s)
    p.fillPath(body, QBrush(gradient))

    # subtle screen panel
    screen = QRectF(150 * s, 232 * s, 724 * s, 470 * s)
    panel = QPainterPath()
    panel.addRoundedRect(screen, 40 * s, 40 * s)
    p.fillPath(panel, QColor("#0a1030"))
    p.setPen(QPen(QColor("#2b3873"), 8 * s))
    p.drawPath(panel)

    # play triangle
    play = QPainterPath()
    cx, cy, r = size / 2.0, screen.center().y(), 132 * s
    play.moveTo(QPointF(cx - r * 0.55, cy - r))
    play.lineTo(QPointF(cx + r * 0.95, cy))
    play.lineTo(QPointF(cx - r * 0.55, cy + r))
    play.closeSubpath()
    p.fillPath(play, QColor(YELLOW))

    # stand
    p.setPen(QPen(QColor("#2b3873"), 26 * s, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(cx, screen.bottom()), QPointF(cx, screen.bottom() + 58 * s))
    p.drawLine(
        QPointF(cx - 130 * s, screen.bottom() + 58 * s),
        QPointF(cx + 130 * s, screen.bottom() + 58 * s),
    )

    # LIVE tag
    tag = QRectF(150 * s, 118 * s, 214 * s, 74 * s)
    tag_path = QPainterPath()
    tag_path.addRoundedRect(tag, 12 * s, 12 * s)
    p.fillPath(tag_path, QColor(RED))
    font = QFont()
    font.setBold(True)
    font.setPixelSize(int(46 * s))
    font.setLetterSpacing(QFont.PercentageSpacing, 112)
    p.setFont(font)
    p.setPen(QPen(QColor("#ffffff")))
    p.drawText(tag, Qt.AlignCenter, "LIVE")
    p.end()
    return pixmap


def main() -> int:
    app = QApplication(sys.argv)  # noqa: F841 - required for QPixmap
    ASSETS.mkdir(exist_ok=True)

    master = draw()
    png = ASSETS / "icon.png"
    master.save(str(png), "PNG")
    print(f"wrote {png.relative_to(ROOT)}")

    # ---- Windows .ico (multi-resolution)
    try:
        from PIL import Image

        with Image.open(png) as source:
            source.save(
                ASSETS / "icon.ico",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )
        print("wrote assets/icon.ico")
    except ImportError:
        print("Pillow not installed - skipping icon.ico", file=sys.stderr)

    # ---- macOS .icns
    if sys.platform == "darwin" and shutil.which("iconutil"):
        iconset = ASSETS / "icon.iconset"
        if iconset.exists():
            shutil.rmtree(iconset)
        iconset.mkdir()
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = base * scale
                name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
                draw(px).save(str(iconset / name), "PNG")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")],
            check=True,
        )
        shutil.rmtree(iconset)
        print("wrote assets/icon.icns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
