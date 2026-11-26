"""Hold the display awake while something is playing, on macOS.

Why this exists: on Linux the desktop shell notices a fullscreen video window
on its own and inhibits the screensaver, so the app gets the behaviour for
free. macOS has no equivalent - the power manager only suppresses display
sleep for Apple's own playback paths (AVFoundation, Safari). A libVLC surface
inside a Qt window looks like any other application, so the screen dims and
locks mid-film while the audio keeps playing.

The fix is an IOPM assertion: `IOPMAssertionCreateWithName` with
`kIOPMAssertionTypeNoDisplaySleep` asks the power manager to keep the display
lit until the matching `IOPMAssertionRelease`. It is what `caffeinate -d` wraps.

Notes on the implementation:

* ctypes rather than pyobjc, to avoid a new dependency for two calls.
* The assertion is per-process and tied to the returned id, not to a live
  object, so it must be released explicitly - a crashed process has its
  assertions reaped by the kernel, but a clean quit should not rely on that.
* Idempotent: `hold()` while already held does nothing, so callers can be
  liberal. `release()` while not held does nothing too.
* Every platform that is not macOS is a no-op, so the call sites need no
  branching.

Best-effort throughout: a machine where the framework cannot be loaded must
still play video, so a failure here silently degrades to the old behaviour
instead of taking playback down.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys

# IOKit constants (IOKit/pwr_mgt/IOPMLib.h). The type is a CFString, so the
# value below is the string's *content*, not an enum.
_NO_DISPLAY_SLEEP = b"NoDisplaySleepAssertion"
_LEVEL_ON = 255
_UTF8 = 0x08000100  # kCFStringEncodingUTF8

_REASON = b"Video playback in progress"


class _MacAssertion:
    """The ctypes bindings, resolved once on first use."""

    def __init__(self):
        self._cf = None
        self._iokit = None
        self._create = None
        self._release = None
        self._type = None
        self._reason = None

    def load(self) -> bool:
        if self._create is not None:
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
            self._cf.CFRelease.argtypes = [ctypes.c_void_p]

            # (CFStringRef type, IOPMAssertionLevel, CFStringRef reason,
            #  IOPMAssertionID *out) -> IOReturn
            self._create = self._iokit.IOPMAssertionCreateWithName
            self._create.restype = ctypes.c_int
            self._create.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            self._release = self._iokit.IOPMAssertionRelease
            self._release.restype = ctypes.c_int
            self._release.argtypes = [ctypes.c_uint32]

            self._type = self._cf.CFStringCreateWithCString(None, _NO_DISPLAY_SLEEP, _UTF8)
            self._reason = self._cf.CFStringCreateWithCString(None, _REASON, _UTF8)
            if not self._type or not self._reason:
                raise OSError("CFStringCreateWithCString failed")
            return True
        except Exception:
            self._create = None
            return False


class ScreenAwake:
    """Keeps the display on between `hold()` and `release()` on macOS."""

    def __init__(self) -> None:
        self._id = None
        self._bindings = _MacAssertion() if sys.platform == "darwin" else None

    @property
    def held(self) -> bool:
        return self._id is not None

    def hold(self) -> bool:
        """Ask the power manager to keep the display lit. True if now held."""
        if self._id is not None:
            return True
        if self._bindings is None or not self._bindings.load():
            return False
        try:
            out = ctypes.c_uint32(0)
            status = self._bindings._create(
                self._bindings._type, _LEVEL_ON, self._bindings._reason, ctypes.byref(out))
            if status != 0:  # kIOReturnSuccess
                return False
            self._id = out.value
            return True
        except Exception:
            self._id = None
            return False

    def release(self) -> None:
        """Drop the assertion. Safe to call when nothing is held."""
        if self._id is None:
            return
        identifier, self._id = self._id, None
        try:
            self._bindings._release(identifier)
        except Exception:
            pass
