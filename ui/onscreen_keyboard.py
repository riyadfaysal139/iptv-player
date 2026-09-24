"""A floating on-screen keyboard of our own: drag it, resize it, type with it.

Why not the desktop's? On this box the app runs through XWayland (libVLC
needs an X11 window id, see main.py), and an X11 client cannot tell a Wayland
compositor that a text field has focus, so KDE's keyboard never appears on
its own - and when summoned by hand it is pinned to the bottom edge at full
width, with no way to move or shrink it, covering half the page. Windows'
and macOS' keyboards have the same "it goes where it goes" problem over a
fullscreen window. A keyboard that is part of the app has none of that: it
is a tool window transient for whichever of our windows holds the text
field, so it floats above fullscreen video the same way the transport bar
does; it never takes focus, so the field keeps the caret; and it delivers
plain QKeyEvents, so nothing between us and the widget needs to cooperate.

It appears when focus lands on a text-entry widget and goes away when focus
moves on to something that is not one. Its geometry is remembered across
runs, so once it is parked where the user wants it, it stays there.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QSizeGrip, QSizePolicy, QStyle, QStyleOption,
    QTextEdit, QVBoxLayout, QWidget,
)

MIN_SIZE = QSize(420, 170)
DEFAULT_WIDTH_FRACTION = 0.62   # of the screen, first time out
ASPECT = 0.34                   # height / width, first time out
HEADER_H = 26
SETTING_KEY = "osk_geometry"
FLOATING_SOCKET = "floating-keyboard-{uid}"   # keyboard.py's single-instance socket

# Layouts. Each row is a list of keys; a key is (label, what, span) where
# `what` is the text to type, or a Qt.Key for the specials, or one of the
# layer switches below. `span` is its width in grid units.
SHIFT, SYMBOLS, LETTERS, CLOSE = "shift", "symbols", "letters", "close"

LETTER_ROWS = [
    [("1", "1", 2), ("2", "2", 2), ("3", "3", 2), ("4", "4", 2), ("5", "5", 2),
     ("6", "6", 2), ("7", "7", 2), ("8", "8", 2), ("9", "9", 2), ("0", "0", 2),
     ("⌫", Qt.Key_Backspace, 3)],
    [("q", "q", 2), ("w", "w", 2), ("e", "e", 2), ("r", "r", 2), ("t", "t", 2),
     ("y", "y", 2), ("u", "u", 2), ("i", "i", 2), ("o", "o", 2), ("p", "p", 2),
     ("-", "-", 3)],
    [("a", "a", 2), ("s", "s", 2), ("d", "d", 2), ("f", "f", 2), ("g", "g", 2),
     ("h", "h", 2), ("j", "j", 2), ("k", "k", 2), ("l", "l", 2), ("'", "'", 2),
     ("↵", Qt.Key_Return, 3)],
    [("⇧", SHIFT, 3), ("z", "z", 2), ("x", "x", 2), ("c", "c", 2), ("v", "v", 2),
     ("b", "b", 2), ("n", "n", 2), ("m", "m", 2), (",", ",", 2), (".", ".", 2),
     ("?", "?", 2)],
    [("?123", SYMBOLS, 3), ("←", Qt.Key_Left, 2), ("space", " ", 11),
     ("→", Qt.Key_Right, 2), ("✕", CLOSE, 5)],
]

SYMBOL_ROWS = [
    [("!", "!", 2), ("@", "@", 2), ("#", "#", 2), ("$", "$", 2), ("%", "%", 2),
     ("^", "^", 2), ("&", "&", 2), ("*", "*", 2), ("(", "(", 2), (")", ")", 2),
     ("⌫", Qt.Key_Backspace, 3)],
    [("~", "~", 2), ("`", "`", 2), ("|", "|", 2), ("•", "•", 2), ("√", "√", 2),
     ("π", "π", 2), ("÷", "÷", 2), ("×", "×", 2), ("{", "{", 2), ("}", "}", 2),
     ("_", "_", 3)],
    [("=", "=", 2), ("+", "+", 2), ("[", "[", 2), ("]", "]", 2), ("<", "<", 2),
     (">", ">", 2), ("\\", "\\", 2), ("/", "/", 2), (";", ";", 2), (":", ":", 2),
     ("↵", Qt.Key_Return, 3)],
    [("abc", LETTERS, 3), ("€", "€", 2), ("£", "£", 2), ("¥", "¥", 2), ("°", "°", 2),
     ("\"", "\"", 2), ("…", "…", 2), ("§", "§", 2), (",", ",", 2), (".", ".", 2),
     ("?", "?", 2)],
    [("abc", LETTERS, 3), ("←", Qt.Key_Left, 2), ("space", " ", 11),
     ("→", Qt.Key_Right, 2), ("✕", CLOSE, 5)],
]

# Held down, these keep going the way a physical key would.
REPEATING = {Qt.Key_Backspace, Qt.Key_Left, Qt.Key_Right, " "}


def is_text_entry(widget: QWidget | None) -> bool:
    """Does this widget take typed text, so that a keyboard is worth showing?"""
    if widget is None:
        return False
    if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox)):
        return not widget.isReadOnly()
    if isinstance(widget, QComboBox):
        return widget.isEditable()
    return False


class _Key(QPushButton):
    def __init__(self, label: str, what, span: int):
        super().__init__(label)
        self.what = what
        self.span = span
        self.setObjectName("oskKey")
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(1, 1)
        if what in REPEATING:
            self.setAutoRepeat(True)
            self.setAutoRepeatDelay(400)
            self.setAutoRepeatInterval(45)


class _Handle(QWidget):
    """The strip along the top: grab it to drag the keyboard around."""

    def __init__(self, keyboard):
        super().__init__(keyboard)
        self.setObjectName("oskHandle")
        self.setFixedHeight(HEADER_H)
        self.setCursor(Qt.SizeAllCursor)
        self._grab: QPoint | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 0, 4, 0)
        row.setSpacing(6)
        self.title = QLabel("⋯  Keyboard")
        self.title.setObjectName("oskTitle")
        row.addWidget(self.title, 1)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        # Let the window system do the drag: on Wayland a client cannot
        # position its own window at all, and everywhere else this is what
        # gives the native snapping and edge behaviour. Moving the window
        # by hand is the fallback for platforms without it.
        handle = self.window().windowHandle()
        if handle is not None and handle.startSystemMove():
            return
        self._grab = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._grab is not None:
            self.window().move(event.globalPosition().toPoint() - self._grab)

    def mouseReleaseEvent(self, event):
        self._grab = None


class OnScreenKeyboard(QWidget):
    """The floating keyboard window. One per app, re-homed as needed."""

    closed = Signal()
    geometryChanged = Signal(QRect)

    def __init__(self, owner: QWidget | None = None, rows=None):
        super().__init__(owner, self._flags())
        self._rows = rows or (LETTER_ROWS, SYMBOL_ROWS)
        self.setObjectName("onScreenKeyboard")
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMinimumSize(MIN_SIZE)
        self._target: QWidget | None = None
        self._shift = False
        self._layer = LETTERS

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 0, 1, 1)
        root.setSpacing(0)
        self.handle = _Handle(self)
        root.addWidget(self.handle)

        self._keys_host = QWidget(self)
        self._keys_host.setObjectName("oskKeys")
        self._grid = QGridLayout(self._keys_host)
        self._grid.setContentsMargins(6, 4, 6, 2)
        self._grid.setSpacing(4)
        root.addWidget(self._keys_host, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addStretch(1)
        grip = QSizeGrip(self)
        grip.setObjectName("oskGrip")
        grip.setFixedSize(18, 18)
        grip.setCursor(Qt.SizeFDiagCursor)
        foot.addWidget(grip, 0, Qt.AlignBottom | Qt.AlignRight)
        root.addLayout(foot)

        self._keys: list[_Key] = []
        self._build(self._rows[0])

    @staticmethod
    def _flags():
        # Transient for its owner, above it, and never the focus window: the
        # same recipe as the fullscreen transport bar, which is what keeps
        # it over an active fullscreen window and the caret in the field.
        return (Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                | Qt.WindowDoesNotAcceptFocus)

    # -- keys -------------------------------------------------------------

    def _build(self, rows):
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._keys = []
        for r, row in enumerate(rows):
            col = 0
            for label, what, span in row:
                key = _Key(label, what, span)
                key.clicked.connect(lambda _=False, k=key: self._pressed(k))
                self._grid.addWidget(key, r, col, 1, span)
                self._keys.append(key)
                col += span
            self._grid.setRowStretch(r, 1)
        for c in range(self._grid.columnCount()):
            self._grid.setColumnStretch(c, 1)
        self._relabel()
        self._refit()

    def _relabel(self):
        for key in self._keys:
            if isinstance(key.what, str) and len(key.what) == 1 and key.what.isalpha():
                key.setText(key.what.upper() if self._shift else key.what)
            elif key.what == SHIFT:
                key.setProperty("active", self._shift)
                key.style().unpolish(key)
                key.style().polish(key)

    def _pressed(self, key: _Key):
        what = key.what
        if what == SHIFT:
            self._shift = not self._shift
            self._relabel()
            return
        if what in (SYMBOLS, LETTERS):
            self._layer = what
            self._shift = False
            self._build(self._rows[1] if what == SYMBOLS else self._rows[0])
            return
        if what == CLOSE:
            self.hide()
            self.closed.emit()
            return
        if isinstance(what, str):
            text = what.upper() if self._shift else what
            code = Qt.Key(ord(text.upper())) if len(text) == 1 and text.isascii() else Qt.Key_unknown
            mods = Qt.ShiftModifier if self._shift and text != what else Qt.NoModifier
            if self._shift and text != what:
                # One-shot shift, like a phone: the next letter is lower again.
                self._shift = False
                self._relabel()
        else:
            code, text, mods = what, "", Qt.NoModifier
            if what == Qt.Key_Return:
                text = "\r"
        self.deliver(code, mods, text)

    def deliver(self, code, mods, text: str):
        """Hand one keystroke to whatever is being typed into.

        Here that is the focused widget of our own app, as a pair of key
        events. A system-wide keyboard (keyboard.py) overrides this to feed
        a virtual input device instead, so any app receives it.
        """
        target = self.target()
        if target is None:
            return
        QApplication.sendEvent(target, QKeyEvent(QEvent.KeyPress, code, mods, text))
        QApplication.sendEvent(target, QKeyEvent(QEvent.KeyRelease, code, mods, text))

    # -- target ------------------------------------------------------------

    def set_target(self, widget: QWidget | None):
        self._target = widget
        # Follow the field into whichever window holds it: a tool window is
        # only usable over a modal dialog when it is that dialog's child,
        # and only stays above a fullscreen window when it is its transient.
        owner = widget.window() if widget is not None else None
        if owner is not None and owner is not self.parentWidget() and owner is not self:
            geometry = self.geometry()
            shown = self.isVisible()
            self.setParent(owner, self._flags())
            self.setGeometry(geometry)
            if shown:
                self.show()

    def target(self) -> QWidget | None:
        focused = QApplication.focusWidget()
        if is_text_entry(focused):
            return focused
        return self._target if self._target is not None and is_text_entry(self._target) else None

    # -- geometry ----------------------------------------------------------

    def place_default(self, screen_rect: QRect):
        width = max(MIN_SIZE.width(), int(screen_rect.width() * DEFAULT_WIDTH_FRACTION))
        height = max(MIN_SIZE.height(), int(width * ASPECT))
        self.setGeometry(screen_rect.left() + (screen_rect.width() - width) // 2,
                         screen_rect.bottom() - height - 12, width, height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refit()
        self.geometryChanged.emit(self.geometry())

    def moveEvent(self, event):
        super().moveEvent(event)
        self.geometryChanged.emit(self.geometry())

    def _refit(self):
        # Keys grow with the window; the label size follows the key height.
        rows = max(1, self._grid.rowCount())
        key_h = max(8, (self.height() - HEADER_H - 30) / rows)
        font = QFont(self.font())
        font.setPixelSize(max(11, int(key_h * 0.42)))
        for key in self._keys:
            key.setFont(font)

    def paintEvent(self, event):
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PE_Widget, option, painter, self)


def floating_keyboard_running() -> bool:
    """Is the stand-alone Floating Keyboard (keyboard.py) already serving?"""
    import os

    if not hasattr(os, "getuid"):
        return False
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(FLOATING_SOCKET.format(uid=os.getuid()))
    running = sock.waitForConnected(50)
    sock.abort()
    return running


class KeyboardController(QObject):
    """Brings the keyboard out for text fields and puts it away after."""

    def __init__(self, app: QApplication, db=None):
        super().__init__(app)
        self.app = app
        self.db = db
        self.keyboard: OnScreenKeyboard | None = None
        self._dismissed_for: QWidget | None = None
        # A drag or a grip resize streams geometry; write it once it settles.
        self._pending: QRect | None = None
        self._persist = QTimer(self)
        self._persist.setSingleShot(True)
        self._persist.setInterval(500)
        self._persist.timeout.connect(self._flush)
        app.focusChanged.connect(self._focus_changed)
        app.applicationStateChanged.connect(self._app_state_changed)
        app.installEventFilter(self)

    def _ensure(self, owner: QWidget) -> OnScreenKeyboard:
        if self.keyboard is None:
            self.keyboard = OnScreenKeyboard(owner)
            self.keyboard.closed.connect(self._closed)
            self.keyboard.geometryChanged.connect(self._remember)
            saved = self._saved_geometry()
            if saved is not None:
                self.keyboard.setGeometry(saved)
            else:
                screen = owner.screen() or QApplication.primaryScreen()
                self.keyboard.place_default(screen.availableGeometry())
        return self.keyboard

    def show_for(self, widget: QWidget):
        if floating_keyboard_running():
            # The system-wide copy of this keyboard is up (keyboard.py); a
            # second one under it would only cover more of the page.
            return
        keyboard = self._ensure(widget.window())
        keyboard.set_target(widget)
        # Keep it on a screen: a saved spot from a bigger monitor is no use.
        screen = widget.window().screen() or QApplication.primaryScreen()
        if screen is not None and not screen.availableGeometry().intersects(keyboard.geometry()):
            keyboard.place_default(screen.availableGeometry())
        keyboard.show()
        keyboard.raise_()

    def hide(self):
        if self.keyboard is not None:
            self.keyboard.hide()

    # -- focus tracking ---------------------------------------------------

    def _focus_changed(self, old: QWidget | None, new: QWidget | None):
        if new is None:
            # Not the user leaving the field: another app came to the front
            # (which _app_state_changed handles) or the window is closing.
            return
        if is_text_entry(new):
            if new is not self._dismissed_for:
                self.show_for(new)
        else:
            self._dismissed_for = None
            self.hide()

    def eventFilter(self, watched, event):
        # Focus does not change when the field that already has it is tapped
        # again; after the keyboard's ✕, that tap is how it is asked back.
        if (event.type() == QEvent.MouseButtonPress and isinstance(watched, QWidget)
                and is_text_entry(watched) and watched.hasFocus()):
            self._dismissed_for = None
            self.show_for(watched)
        return False

    def _app_state_changed(self, state):
        # A stay-on-top window would otherwise hang over whatever app the
        # user switched to; it belongs to ours alone. It comes back with
        # the app if the field still has the caret.
        if state != Qt.ApplicationActive:
            self.hide()
            return
        focused = QApplication.focusWidget()
        if is_text_entry(focused) and focused is not self._dismissed_for:
            self.show_for(focused)

    def _closed(self):
        # ✕ means "not for this field": it comes back for the next one.
        self._dismissed_for = QApplication.focusWidget()

    # -- persistence --------------------------------------------------------

    def _saved_geometry(self) -> QRect | None:
        if self.db is None:
            return None
        raw = self.db.get_setting(SETTING_KEY, "") or ""
        try:
            x, y, w, h = (int(v) for v in raw.split(","))
        except ValueError:
            return None
        if w < MIN_SIZE.width() or h < MIN_SIZE.height():
            return None
        return QRect(x, y, w, h)

    def _remember(self, rect: QRect):
        self._pending = QRect(rect)
        self._persist.start()

    def _flush(self):
        rect, self._pending = self._pending, None
        if self.db is not None and rect is not None:
            self.db.set_setting(SETTING_KEY, f"{rect.x()},{rect.y()},{rect.width()},{rect.height()}")


def install(app: QApplication, db=None) -> KeyboardController:
    return KeyboardController(app, db)
