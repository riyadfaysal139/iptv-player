"""Hold the display awake while something is playing.

Why this exists: every platform treats a libVLC surface differently, and none
of them infer "a film is playing" from it on their own.

* **macOS** only suppresses display sleep for Apple's own playback paths
  (AVFoundation, Safari). A video surface inside a Qt window looks like any
  other application, so the display dims and locks mid-film while the audio
  keeps playing. The cure is an IOPM assertion - `kIOPMAssertionTypeNoDisplaySleep`
  via `IOPMAssertionCreateWithName`, which is what `caffeinate -d` wraps.
* **Windows** behaves the same way: playback through libVLC sets no execution
  state, so the monitor powers off on the idle timeout. The cure is
  `SetThreadExecutionState` with `ES_DISPLAY_REQUIRED | ES_CONTINUOUS`.
* **Linux** is the odd one out and needs nothing here. The desktop shell
  notices a fullscreen video window and inhibits the screensaver itself, so
  the app already gets the behaviour for free. Adding a second inhibitor over
  D-Bus would duplicate what the shell is doing and, on some desktops, fight
  it. Left as a no-op deliberately rather than by omission.

Implementation notes:

* ctypes rather than pyobjc or pywin32 - two calls do not justify a dependency.
* Both assertions are process/thread-scoped and tied to an id or a flag, not a
  live object, so they must be released explicitly: a clean quit should not
  rely on the OS reaping them.
* Idempotent: `hold()` while already held does nothing, `release()` while not
  held does nothing. Callers can therefore be liberal.
* Best-effort throughout. A machine where the framework cannot be loaded must
  still play video, so any failure degrades silently to the old behaviour
  instead of taking playback down.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

# --- macOS -----------------------------------------------------------------

# IOKit constants (IOKit/pwr_mgt/IOPMLib.h). The assertion type is a CFString,
# so the value below is the string's content, not an enum.
_NO_DISPLAY_SLEEP = b"NoDisplaySleepAssertion"
_LEVEL_ON = 255
_UTF8 = 0x08000100  # kCFStringEncodingUTF8
_REASON = b"Video playback in progress"

# --- Windows ---------------------------------------------------------------

_ES_CONTINUOUS = 0x80000000
_ES_DISPLAY_REQUIRED = 0x00000002


class _MacBackend:
    """IOPMAssertion, resolved on first use."""

    def __init__(self) -> None:
        self._cf = None
        self._iokit = None
        self._create = None
        self._release_fn = None
        self._type = None
        self._reason = None
        self._loaded = False

    def load(self) -> bool:
        if self._loaded:
            return True
        try:
            cf_path = (ctypes.util.find_library("CoreFoundation")
                       or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            iokit_path = (ctypes.util.find_library("IOKit")
                          or "/System/Library/Frameworks/IOKit.framework/IOKit")
            self._cf = ctypes.CDLL(cf_path)
            self._iokit = ctypes.CDLL(iokit_path)

            self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            self._cf.CFStringCreateWithCString.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]

            # (CFStringRef type, IOPMAssertionLevel, CFStringRef reason,
            #  IOPMAssertionID *out) -> IOReturn
            self._create = self._iokit.IOPMAssertionCreateWithName
            self._create.restype = ctypes.c_int
            self._create.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            self._release_fn = self._iokit.IOPMAssertionRelease
            self._release_fn.restype = ctypes.c_int
            self._release_fn.argtypes = [ctypes.c_uint32]

            self._type = self._cf.CFStringCreateWithCString(None, _NO_DISPLAY_SLEEP, _UTF8)
            self._reason = self._cf.CFStringCreateWithCString(None, _REASON, _UTF8)
            if not self._type or not self._reason:
                raise OSError("CFStringCreateWithCString failed")
            self._loaded = True
            return True
        except Exception:
            self._create = None
            return False

    def hold(self) -> object | None:
        out = ctypes.c_uint32(0)
        status = self._create(self._type, _LEVEL_ON, self._reason, ctypes.byref(out))
        return out.value if status == 0 else None  # 0 is kIOReturnSuccess

    def release(self, token: object) -> None:
        self._release_fn(int(token))


class _WindowsBackend:
    """SetThreadExecutionState, resolved on first use.

    The state is per-thread, so it is set from whichever thread calls `hold()`
    and lasts as long as that thread keeps running - which for this app is the
    Qt main thread, for the lifetime of playback.
    """

    def __init__(self) -> None:
        self._set_state = None
        self._loaded = False

    def load(self) -> bool:
        if self._loaded:
            return True
        try:
            self._set_state = ctypes.windll.kernel32.SetThreadExecutionState
            self._set_state.restype = ctypes.c_uint32
            self._set_state.argtypes = [ctypes.c_uint32]
            self._loaded = True
            return True
        except Exception:
            self._set_state = None
            return False

    def hold(self) -> object | None:
        # A zero return means the call failed; anything else is the previous
        # state and simply confirms it took.
        return True if self._set_state(_ES_CONTINUOUS | _ES_DISPLAY_REQUIRED) else None

    def release(self, token: object) -> None:
        # ES_CONTINUOUS on its own clears the requirement.
        self._set_state(_ES_CONTINUOUS)


class ScreenAwake:
    """Keeps the display on between `hold()` and `release()`.

    No-op on Linux, where the desktop shell already does this for video
    windows (see the module docstring).
    """

    def __init__(self) -> None:
        if sys.platform == "darwin":
            self._backend = _MacBackend()
        elif sys.platform == "win32":
            self._backend = _WindowsBackend()
        else:
            self._backend = None
        self._token = None

    @property
    def held(self) -> bool:
        return self._token is not None

    def hold(self) -> bool:
        """Ask the OS to keep the display lit. True if now held."""
        if self._token is not None:
            return True
        if self._backend is None or not self._backend.load():
            return False
        try:
            token = self._backend.hold()
        except Exception:
            token = None
        if token is None:
            return False
        self._token = token
        return True

    def release(self) -> None:
        """Drop the request. Safe to call when nothing is held."""
        if self._token is None:
            return
        token, self._token = self._token, None
        try:
            self._backend.release(token)
        except Exception:
            pass
