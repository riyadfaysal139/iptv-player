"""A virtual keyboard device, for typing into any application.

Under Wayland there is no XTest: a client cannot send keystrokes to another
window, by design. What it can do - given permission on /dev/uinput - is be
a keyboard. The kernel presents this device to the compositor exactly like
a USB one, so every keystroke goes wherever the compositor's focus is: a
native Wayland app, an XWayland app, the lock screen, anything.

Only the kernel ABI is used, straight through ioctl(), so this needs no
extra Python packages. Characters are mapped for a US layout, which is what
the compositor turns the keycodes back into; that is the layout this box
runs, and the only one the system-wide keyboard draws.
"""

from __future__ import annotations

import fcntl
import os
import struct
import time

from PySide6.QtCore import Qt

# <linux/uinput.h>
_UI_SET_EVBIT = 0x40045564      # _IOW('U', 100, int)
_UI_SET_KEYBIT = 0x40045565     # _IOW('U', 101, int)
_UI_DEV_SETUP = 0x405C5503      # _IOW('U', 3, struct uinput_setup)   92 bytes
_UI_DEV_CREATE = 0x5501         # _IO('U', 1)
_UI_DEV_DESTROY = 0x5502        # _IO('U', 2)
_BUS_VIRTUAL = 0x06

# <linux/input-event-codes.h>
EV_SYN, EV_KEY = 0, 1
KEY_ESC, KEY_BACKSPACE, KEY_TAB, KEY_ENTER = 1, 14, 15, 28
KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_LEFTALT, KEY_SPACE = 29, 42, 56, 57
KEY_HOME, KEY_UP, KEY_PAGEUP, KEY_LEFT, KEY_RIGHT = 102, 103, 104, 105, 106
KEY_END, KEY_DOWN, KEY_PAGEDOWN, KEY_INSERT, KEY_DELETE = 107, 108, 109, 110, 111

_ROW_DIGITS = "1234567890"      # KEY_1 .. KEY_0 = 2..11
_ROW_TOP = "qwertyuiop"         # 16..25
_ROW_HOME = "asdfghjkl"         # 30..38
_ROW_BOTTOM = "zxcvbnm"         # 44..50

# character -> (keycode, needs shift), US layout
CHARS: dict[str, tuple[int, bool]] = {}
for _i, _c in enumerate(_ROW_DIGITS):
    CHARS[_c] = (2 + _i, False)
    CHARS["!@#$%^&*()"[_i]] = (2 + _i, True)
for _i, _c in enumerate(_ROW_TOP):
    CHARS[_c] = (16 + _i, False)
for _i, _c in enumerate(_ROW_HOME):
    CHARS[_c] = (30 + _i, False)
for _i, _c in enumerate(_ROW_BOTTOM):
    CHARS[_c] = (44 + _i, False)
for _c in _ROW_TOP + _ROW_HOME + _ROW_BOTTOM:
    CHARS[_c.upper()] = (CHARS[_c][0], True)
for _lower, _upper, _code in (("-", "_", 12), ("=", "+", 13), ("[", "{", 26),
                              ("]", "}", 27), (";", ":", 39), ("'", '"', 40),
                              ("`", "~", 41), ("\\", "|", 43), (",", "<", 51),
                              (".", ">", 52), ("/", "?", 53)):
    CHARS[_lower] = (_code, False)
    CHARS[_upper] = (_code, True)
CHARS[" "] = (KEY_SPACE, False)
CHARS["\t"] = (KEY_TAB, False)
CHARS["\r"] = (KEY_ENTER, False)
CHARS["\n"] = (KEY_ENTER, False)

SPECIALS = {
    Qt.Key_Backspace: KEY_BACKSPACE, Qt.Key_Return: KEY_ENTER, Qt.Key_Enter: KEY_ENTER,
    Qt.Key_Tab: KEY_TAB, Qt.Key_Escape: KEY_ESC, Qt.Key_Delete: KEY_DELETE,
    Qt.Key_Insert: KEY_INSERT, Qt.Key_Home: KEY_HOME, Qt.Key_End: KEY_END,
    Qt.Key_Left: KEY_LEFT, Qt.Key_Right: KEY_RIGHT, Qt.Key_Up: KEY_UP, Qt.Key_Down: KEY_DOWN,
    Qt.Key_PageUp: KEY_PAGEUP, Qt.Key_PageDown: KEY_PAGEDOWN,
}


def typeable(what) -> bool:
    """Can the device produce this key of the on-screen layout?"""
    if isinstance(what, str):
        return len(what) == 1 and what in CHARS
    return what in SPECIALS


class VirtualKeyboard:
    """One uinput keyboard. Create it once; the compositor sees it plug in."""

    def __init__(self, name: str = "Floating Keyboard"):
        self._fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._fd, _UI_SET_EVBIT, EV_KEY)
            for code in range(1, 128):
                fcntl.ioctl(self._fd, _UI_SET_KEYBIT, code)
            # struct uinput_setup { struct input_id {u16 bustype, vendor,
            # product, version}; char name[80]; u32 ff_effects_max; }
            setup = struct.pack("<HHHH80sI", _BUS_VIRTUAL, 0x1d6b, 0x0104, 1,
                                name.encode("utf-8")[:79], 0)
            fcntl.ioctl(self._fd, _UI_DEV_SETUP, setup)
            fcntl.ioctl(self._fd, _UI_DEV_CREATE)
        except OSError:
            os.close(self._fd)
            raise
        # The compositor needs a moment to pick the new device up; keys
        # written before that are lost.
        time.sleep(0.3)

    def close(self):
        if self._fd is not None:
            try:
                fcntl.ioctl(self._fd, _UI_DEV_DESTROY)
            finally:
                os.close(self._fd)
                self._fd = None

    def _emit(self, type_: int, code: int, value: int):
        # struct input_event { struct timeval; u16 type; u16 code; s32 value; }
        os.write(self._fd, struct.pack("llHHi", 0, 0, type_, code, value))

    def _sync(self):
        self._emit(EV_SYN, 0, 0)

    def tap(self, code: int, shift: bool = False, ctrl: bool = False):
        for mod, held in ((KEY_LEFTSHIFT, shift), (KEY_LEFTCTRL, ctrl)):
            if held:
                self._emit(EV_KEY, mod, 1)
        self._emit(EV_KEY, code, 1)
        self._sync()
        self._emit(EV_KEY, code, 0)
        for mod, held in ((KEY_LEFTCTRL, ctrl), (KEY_LEFTSHIFT, shift)):
            if held:
                self._emit(EV_KEY, mod, 0)
        self._sync()

    def type_char(self, char: str) -> bool:
        mapping = CHARS.get(char)
        if mapping is None:
            return False
        code, shift = mapping
        self.tap(code, shift=shift)
        return True

    def press_special(self, key) -> bool:
        code = SPECIALS.get(key)
        if code is None:
            return False
        self.tap(code)
        return True
