"""Locate libVLC before `import vlc` happens.

python-vlc resolves the shared library at import time, so the search paths must
be set up first. The app deliberately does not bundle libVLC — it uses the
user's VLC install and explains how to get one when it is missing.
"""

from __future__ import annotations

import ctypes
import os
import platform
import struct
import sys
from pathlib import Path

DOWNLOAD_URL = "https://www.videolan.org/vlc/"

# Mach-O cpu_type_t values, from <mach/machine.h>. CPU_ARCH_ABI64 (0x01000000)
# is or-ed into the base type for the 64-bit variants. Note arm64e shares
# CPU_TYPE_ARM64 and is distinguished only by its cpusubtype, so it reads back
# as "arm64" here — which is the safe direction: it can only ever suppress a
# mismatch warning, never invent one.
_CPU_TYPES = {0x0100000C: "arm64", 0x01000007: "x86_64", 0x00000007: "i386"}
_ARCH_LABELS = {"arm64": "Apple Silicon", "x86_64": "Intel"}

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


def macho_arches(path) -> set[str]:
    """Architectures inside a Mach-O file, read straight from its header.

    An Apple Silicon app cannot load an Intel-only libVLC, and dyld reports that
    as an opaque "incompatible architecture" string. Reading the header lets the
    app say which build of VLC is installed and which one is needed.

    Anything unreadable or unrecognised gives an empty set, so a parsing failure
    degrades to the plain error message rather than to a confident wrong claim.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
            if len(head) < 8:
                return set()
            magic = head[:4]
            # Thin 64-bit image: MH_MAGIC_64/MH_CIGAM_64, cputype follows.
            if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
                order = "<" if magic == b"\xcf\xfa\xed\xfe" else ">"
                (cpu,) = struct.unpack(order + "I", head[4:8])
                name = _CPU_TYPES.get(cpu)
                return {name} if name else set()
            # Universal ("fat") binary: a big-endian count, then one 20-byte
            # fat_arch per slice with cputype first.
            if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
                order = ">" if magic == b"\xca\xfe\xba\xbe" else "<"
                (count,) = struct.unpack(order + "I", head[4:8])
                if not 0 < count <= 32:  # a sane cap; a real fat file has a few
                    return set()
                table = handle.read(20 * count)
                if len(table) < 20 * count:
                    return set()
                found = set()
                for index in range(count):
                    (cpu,) = struct.unpack(order + "I", table[20 * index:20 * index + 4])
                    name = _CPU_TYPES.get(cpu)
                    if name:
                        found.add(name)
                return found
    except OSError:
        pass
    return set()


def arch_label(arch: str) -> str:
    """'arm64' -> 'Apple Silicon (arm64)', for messages aimed at users."""
    friendly = _ARCH_LABELS.get(arch)
    return f"{friendly} ({arch})" if friendly else arch


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
    library_path = None  # the file we tried, whether or not it actually loaded
    for name in _libnames():
        target = lib_dir / name
        if target.exists():
            library_path = library_path or target
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
        elif sys.platform == "darwin":
            # The macOS equivalent: an Apple Silicon app cannot load an Intel
            # VLC. Only claim a mismatch when the header parsed and genuinely
            # disagrees — otherwise dyld's own message stands unaltered.
            want = platform.machine()
            have = macho_arches(library_path) if library_path else set()
            if have and want not in have:
                theirs = " or ".join(arch_label(a) for a in sorted(have))
                hint = (
                    f"\n\nThis app is {arch_label(want)}, but the VLC installed "
                    f"at {library_path} is {theirs} only. The two cannot be "
                    "mixed, even though VLC is present.\n\nDownload the "
                    f"{arch_label(want)} build of VLC from {DOWNLOAD_URL} and "
                    "restart."
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
