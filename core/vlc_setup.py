"""Locate libVLC before `import vlc` happens.

python-vlc resolves the shared library at import time, so the search paths must
be set up first. The app deliberately does not bundle libVLC — it uses the
user's VLC install and explains how to get one when it is missing.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

DOWNLOAD_URL = "https://www.videolan.org/vlc/"

_state = {"loaded": False, "error": None, "lib_dir": None, "plugin_dir": None}


def _mac_candidates():
    return [
        Path("/Applications/VLC.app/Contents/MacOS"),
        Path.home() / "Applications/VLC.app/Contents/MacOS",
    ]


def _win_candidates():
    out = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(var)
        if base:
            out.append(Path(base) / "VideoLAN" / "VLC")
    try:  # the install location is authoritative when present
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\VideoLAN\VLC") as key:
                    path, _ = winreg.QueryValueEx(key, "InstallDir")
                    out.insert(0, Path(path))
            except OSError:
                continue
    except ImportError:
        pass
    return out


def _linux_candidates():
    return [
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib64"),
        Path("/usr/lib"),
        Path("/usr/local/lib"),
        Path("/var/lib/flatpak/app/org.videolan.VLC/current/active/files/lib"),
        Path.home() / ".local/share/flatpak/app/org.videolan.VLC/current/active/files/lib",
        Path("/snap/vlc/current/usr/lib"),
    ]


def _libnames():
    if sys.platform == "darwin":
        return ["libvlc.dylib", "libvlc.5.dylib"]
    if os.name == "nt":
        return ["libvlc.dll"]
    return ["libvlc.so.5", "libvlc.so"]


def _corenames():
    """libvlc links libvlccore via @rpath, so core must be resident first."""
    if sys.platform == "darwin":
        return ["libvlccore.9.dylib", "libvlccore.dylib"]
    if os.name == "nt":
        return ["libvlccore.dll"]
    return ["libvlccore.so.9", "libvlccore.so"]


def find_vlc() -> tuple[Path | None, Path | None]:
    """Return (lib_dir, plugin_dir), or (None, None) when VLC is absent."""
    if sys.platform == "darwin":
        bases = _mac_candidates()
    elif os.name == "nt":
        bases = _win_candidates()
    else:
        bases = _linux_candidates()

    env_dir = os.environ.get("IPTVPLAYER_VLC_DIR")
    if env_dir:
        bases.insert(0, Path(env_dir))

    for base in bases:
        if not base or not base.exists():
            continue
        # macOS keeps the dylibs in MacOS/lib; elsewhere they sit in base.
        for lib_dir in (base / "lib", base):
            if not lib_dir.exists():
                continue
            for name in _libnames():
                if (lib_dir / name).exists():
                    plugin_dir = None
                    for candidate in (
                        base / "plugins",
                        base / "vlc" / "plugins",
                        lib_dir / "vlc" / "plugins",
                        base.parent / "plugins",
                    ):
                        if candidate.exists():
                            plugin_dir = candidate
                            break
                    return lib_dir, plugin_dir
    return None, None


def ensure_vlc() -> bool:
    """Prepare the environment and import python-vlc. False if unavailable."""
    if _state["loaded"]:
        return True
    if _state["error"]:
        return False

    lib_dir, plugin_dir = find_vlc()
    if lib_dir is None:
        _state["error"] = (
            "VLC was not found on this computer.\n\n"
            "This player uses VLC's engine (libVLC) to play video, so VLC must "
            f"be installed.\n\nInstall it from {DOWNLOAD_URL} and restart."
        )
        return False

    if plugin_dir:
        os.environ["VLC_PLUGIN_PATH"] = str(plugin_dir)

    if os.name == "nt":
        try:
            os.add_dll_directory(str(lib_dir))
        except (AttributeError, OSError):
            pass
        os.environ["PATH"] = str(lib_dir) + os.pathsep + os.environ.get("PATH", "")
    else:
        var = "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        os.environ[var] = str(lib_dir) + os.pathsep + os.environ.get(var, "")

    # Load libvlccore first: libvlc references it via @rpath/$ORIGIN, which
    # cannot be resolved from an env var set after the process started. Once
    # core is resident, dyld reuses it and libvlc loads cleanly.
    for name in _corenames():
        target = lib_dir / name
        if target.exists():
            try:
                ctypes.CDLL(str(target), mode=getattr(ctypes, "RTLD_GLOBAL", 0))
                break
            except OSError:
                continue

    loaded_path = None
    for name in _libnames():
        target = lib_dir / name
        if target.exists():
            try:
                ctypes.CDLL(str(target), mode=getattr(ctypes, "RTLD_GLOBAL", 0))
                loaded_path = target
                break
            except OSError:
                continue
    if loaded_path:
        os.environ["PYTHON_VLC_LIB_PATH"] = str(loaded_path)

    try:
        import vlc  # noqa: F401
    except Exception as exc:
        hint = ""
        if os.name == "nt":
            bits = 64 if sys.maxsize > 2**32 else 32
            other = 32 if bits == 64 else 64
            # By far the most common Windows failure: VLC's architecture does
            # not match the app's, so the DLL exists but will never load.
            hint = (
                f"\n\nThis app is {bits}-bit, so it needs {bits}-bit VLC. "
                f"A {other}-bit VLC install will not work even though it is "
                f"present.\n\nInstall the {bits}-bit build from {DOWNLOAD_URL} "
                "(choose the Windows 64bit installer) and restart."
            )
        _state["error"] = f"VLC was found at {lib_dir} but could not be loaded:\n{exc}{hint}"
        return False

    _state.update(loaded=True, lib_dir=lib_dir, plugin_dir=plugin_dir)
    return True


def error_message() -> str | None:
    return _state["error"]


def vlc_app_binary() -> Path | None:
    """Path to the full VLC application, for the 'Open in VLC' fallback.

    The genuine VLSub extension only runs inside the VLC app, not libVLC.
    """
    if sys.platform == "darwin":
        for base in _mac_candidates():
            binary = base / "VLC"
            if binary.exists():
                return binary
    elif os.name == "nt":
        for base in _win_candidates():
            binary = base / "vlc.exe"
            if binary.exists():
                return binary
    else:
        for directory in ("/usr/bin", "/usr/local/bin", "/snap/bin"):
            binary = Path(directory) / "vlc"
            if binary.exists():
                return binary
    return None
