"""A QScrollArea that never hands an unclaimed scroll back to macOS.

At the top or bottom of the content, QAbstractScrollArea.wheelEvent() calls
event.ignore() once there is nothing left to scroll. On macOS, an ignored
wheel event is handed back to the native responder chain instead of being
consumed by the app - and a continuing two-finger scroll with nothing left to
absorb is exactly the input pattern AppKit's own "swipe between full-screen
applications" gesture watches for. Scrolling a page to its end could flip the
whole screen to whatever happens to be in the next Space.

Every wheel event is claimed here, whether or not it moved the scrollbar, so
macOS never sees one go unhandled.
"""

from __future__ import annotations

from PySide6.QtWidgets import QScrollArea


class BoundedScrollArea(QScrollArea):
    def wheelEvent(self, event):
        super().wheelEvent(event)
        event.accept()
