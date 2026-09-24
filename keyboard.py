#!/usr/bin/env python3
"""Floating Keyboard - the player's on-screen keyboard, for the whole desktop.

The keyboard that pops up inside the IPTV player (ui/onscreen_keyboard.py)
is small, dark, and goes where it is dragged; the desktop's own is none of
those. This runs the same widget as a stand-alone window and types through a
virtual input device (core/uinput.py), so whatever the compositor has in
focus receives the keys - any app, Wayland or X11.

The window itself must never take focus, or the app being typed into would
lose it on every tap; and it must sit above fullscreen windows. Neither is
something a Wayland client can ask for, so setup.sh installs a KWin window
rule for the window class `floating-keyboard` that forces both, and KWin
remembers where the window was left. A tray icon shows and hides it; running
this script again while it is up toggles it, which is what the launcher and
a global shortcut can be bound to.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon  # noqa: E402

from core import uinput  # noqa: E402
from ui.onscreen_keyboard import (  # noqa: E402
    CLOSE, FLOATING_SOCKET, LETTERS, LETTER_ROWS, OnScreenKeyboard,
)

APP_ID = "floating-keyboard"
SOCKET = FLOATING_SOCKET.format(uid=os.getuid())

# The symbol layer, with the keys a US keymap cannot produce (€ • π ...)
# traded for the navigation keys a real keyboard has and a phone's lacks.
SYSTEM_SYMBOL_ROWS = [
    [("!", "!", 2), ("@", "@", 2), ("#", "#", 2), ("$", "$", 2), ("%", "%", 2),
     ("^", "^", 2), ("&", "&", 2), ("*", "*", 2), ("(", "(", 2), (")", ")", 2),
     ("⌫", Qt.Key_Backspace, 3)],
    [("~", "~", 2), ("`", "`", 2), ("|", "|", 2), ("⇥", Qt.Key_Tab, 2), ("Del", Qt.Key_Delete, 2),
     ("Home", Qt.Key_Home, 2), ("End", Qt.Key_End, 2), ("↑", Qt.Key_Up, 2), ("{", "{", 2),
     ("}", "}", 2), ("_", "_", 3)],
    [("=", "=", 2), ("+", "+", 2), ("[", "[", 2), ("]", "]", 2), ("<", "<", 2),
     (">", ">", 2), ("\\", "\\", 2), ("/", "/", 2), (";", ";", 2), (":", ":", 2),
     ("↵", Qt.Key_Return, 3)],
    [("abc", LETTERS, 3), ("Esc", Qt.Key_Escape, 2), ("PgUp", Qt.Key_PageUp, 2),
     ("PgDn", Qt.Key_PageDown, 2), ("↓", Qt.Key_Down, 2), ("\"", "\"", 2),
     ("Ins", Qt.Key_Insert, 2), (",", ",", 2), (".", ".", 2), ("?", "?", 2)],
    [("abc", LETTERS, 3), ("←", Qt.Key_Left, 2), ("space", " ", 11),
     ("→", Qt.Key_Right, 2), ("✕", CLOSE, 5)],
]


class SystemKeyboard(OnScreenKeyboard):
    """The widget, typing into the compositor's focus instead of our own."""

    def __init__(self, device: uinput.VirtualKeyboard):
        super().__init__(None, rows=(LETTER_ROWS, SYSTEM_SYMBOL_ROWS))
        self.device = device
        self.setWindowTitle("Floating Keyboard")
        self.handle.title.setText("⋯  Floating Keyboard")

    def deliver(self, code, mods, text: str):
        if text and text not in ("\r", "\n"):
            self.device.type_char(text)
        else:
            self.device.press_special(code)


def load_theme(app: QApplication):
    qss = Path(__file__).resolve().parent / "ui" / "theme.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))


def poke_running_instance() -> bool:
    """Tell an instance already up to toggle itself. True if there was one."""
    sock = QLocalSocket()
    sock.connectToServer(SOCKET)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"toggle")
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Floating Keyboard")
    app.setOrganizationName("IPTVPlayer")
    app.setDesktopFileName(APP_ID)     # what KWin's window rule matches on
    app.setQuitOnLastWindowClosed(False)

    if poke_running_instance():
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(SOCKET)   # a stale socket from a crash
    server.listen(SOCKET)

    load_theme(app)
    try:
        device = uinput.VirtualKeyboard()
    except OSError as exc:
        QMessageBox.critical(
            None, "Floating Keyboard",
            f"Cannot open /dev/uinput ({exc.strerror}).\n\n"
            "Run setup.sh once to grant access, then start the keyboard again.")
        return 1

    keyboard = SystemKeyboard(device)
    settings = QSettings()
    size = settings.value("size")
    if size is not None:
        keyboard.resize(size)
    else:
        screen = app.primaryScreen()
        keyboard.place_default(screen.availableGeometry())
    keyboard.geometryChanged.connect(lambda rect: settings.setValue("size", rect.size()))

    def toggle():
        if keyboard.isVisible():
            keyboard.hide()
        else:
            keyboard.show()
            keyboard.raise_()

    def on_connection():
        sock = server.nextPendingConnection()
        sock.readyRead.connect(lambda: (sock.readAll(), toggle()))
        sock.disconnected.connect(sock.deleteLater)

    server.newConnection.connect(on_connection)

    tray = QSystemTrayIcon(QIcon.fromTheme("input-keyboard-virtual", QIcon.fromTheme("input-keyboard")), app)
    tray.setToolTip("Floating Keyboard")
    menu = QMenu()
    menu.addAction("Show / hide keyboard", toggle)
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: toggle() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()

    if "--hidden" not in sys.argv:
        keyboard.show()
    try:
        return app.exec()
    finally:
        device.close()


if __name__ == "__main__":
    sys.exit(main())
