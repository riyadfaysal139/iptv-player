"""What the screen edges offer while the whole app is fullscreen.

With the frame gone there is no title bar to minimise or close from and no
panel to switch apps with. Hovering the top edge slides in the three window
buttons; hovering the bottom edge offers the taskbar - which, under a
fullscreen window, can only be shown by stepping the window down to
maximised, so that is what the button does.

Both are separate top-level tool windows, like the fullscreen control bar,
so they can float over the video's native view as well as over the pages.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QStyle, QStyleOption, QWidget

EDGE_PX = 3            # how close to the edge the pointer must come
STRIP_H = 40
LINGER_PX = 24         # how far back the pointer may drift before the strip goes


class _Strip(QWidget):
    def __init__(self, owner=None):
        # Transient for the main window: a window manager keeps a transient
        # above its owner even when the owner is an *active fullscreen*
        # window - which it puts in its own top layer, above plain
        # stay-on-top windows, the moment it is clicked.
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setObjectName("edgeStrip")

    def paintEvent(self, event):
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, option, painter, self)

    @staticmethod
    def _button(text: str, tip: str, name: str = "edgeButton") -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        return button


class TopStrip(_Strip):
    """Minimise, leave fullscreen, close - the title bar's three, at the top right."""

    minimizeRequested = Signal()
    restoreRequested = Signal()
    closeRequested = Signal()

    def __init__(self, owner=None):
        super().__init__(owner)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(4)
        for text, tip, signal in (("—", "Minimise", self.minimizeRequested),
                                  ("❐", "Leave full screen  (Esc / F11)", self.restoreRequested),
                                  ("✕", "Quit", self.closeRequested)):
            button = self._button(text, tip, "edgeButton" if text != "✕" else "edgeClose")
            button.clicked.connect(signal)
            row.addWidget(button)

    def place(self, area: QRect):
        self.adjustSize()
        self.move(area.right() - self.width() - 8, area.top())


class BottomStrip(_Strip):
    """One pill: step down to a maximised window so the panel shows."""

    taskbarRequested = Signal()

    def __init__(self, owner=None):
        super().__init__(owner)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        button = self._button("⌄  Show taskbar", "Leave full screen so the taskbar is reachable")
        button.clicked.connect(self.taskbarRequested)
        row.addWidget(button)

    def place(self, area: QRect):
        self.adjustSize()
        self.move(area.left() + (area.width() - self.width()) // 2,
                  area.bottom() - self.height() + 1)


class EdgeStrips:
    """Owns both strips and decides, from the pointer, which is showing."""

    def __init__(self, owner=None):
        self.top = TopStrip(owner)
        self.bottom = BottomStrip(owner)

    def poll(self, position, area: QRect, available: QRect):
        """Called on a timer with the pointer's global position.

        `area` is the whole screen (the trigger edges); `available` is the
        screen minus the panel's strut, which the window manager still
        enforces on a tool window even over a fullscreen app - a strip placed
        inside the strut would be bounced to the centre of the screen.
        """
        near_top = position.y() <= area.top() + EDGE_PX
        near_bottom = position.y() >= area.bottom() - EDGE_PX
        self._toggle(self.top, near_top,
                     position.y() <= area.top() + STRIP_H + LINGER_PX, area)
        self._toggle(self.bottom, near_bottom,
                     position.y() >= available.bottom() - STRIP_H - LINGER_PX, available)

    def _toggle(self, strip, trigger: bool, linger: bool, area: QRect):
        if trigger and not strip.isVisible():
            strip.place(area)
            strip.show()
            strip.raise_()
        elif strip.isVisible() and not linger and not strip.underMouse():
            strip.hide()
        elif strip.isVisible():
            # Re-assert every tick while it should be showing: a click on the
            # fullscreen app re-raises it, and the strip must not sink under.
            strip.raise_()

    def hide(self):
        self.top.hide()
        self.bottom.hide()

    def close(self):
        for strip in (self.top, self.bottom):
            strip.close()
            strip.deleteLater()
