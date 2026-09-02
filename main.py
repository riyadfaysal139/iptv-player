#!/usr/bin/env python3
"""IPTV Player — entry point."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import vlc_setup  # noqa: E402
from core.db import Database  # noqa: E402


def load_theme(app: QApplication):
    qss = Path(__file__).resolve().parent / "ui" / "theme.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))


def selftest() -> int:
    """Verify a frozen build can import everything and find VLC, then exit.

    Packaging is where missing hidden imports show up, and a GUI app gives no
    feedback when it fails to start. `IPTVPlayer --selftest` makes that visible
    in a build script or CI job.
    """
    import importlib

    # A windowed Windows build has no stdout, so print() would vanish when the
    # self-test is run from cmd. Attach to the parent console so the report is
    # actually readable; the exit code alone is not enough to debug a build.
    original_stdout, original_stderr = sys.stdout, sys.stderr
    console_attached = False
    if sys.platform.startswith("win"):
        try:
            import ctypes

            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", buffering=1)
                sys.stderr = open("CONOUT$", "w", buffering=1)
                console_attached = True
        except Exception:
            pass

    # Attaching is best-effort and has been observed to fail outright under
    # PowerShell 7's ConPTY (as opposed to a classic console, where it works)
    # - and a frozen windowed build otherwise has no stdout/stderr at all
    # (they are None, not merely redirected). Every print() below would crash
    # the whole process on the first line with no visible error, since stderr
    # is None too. A do-nothing stream keeps print() harmless either way; the
    # disk report a few lines down is the real, always-reliable output.
    import io

    if sys.stdout is None:
        sys.stdout = io.StringIO()
    if sys.stderr is None:
        sys.stderr = io.StringIO()

    try:
        return _run_selftest(importlib)
    finally:
        # Interpreter shutdown flushes and closes sys.stdout/sys.stderr. A
        # windowed build has no message pump, and closing a CONOUT$ handle in
        # that state has been observed (specifically under PowerShell 7's
        # ConPTY, not a classic console) to turn a failure there into a
        # nonzero process exit code - even on a run that computed and printed
        # SELFTEST: PASS. Restoring the original streams (None, for a frozen
        # windowed build) means shutdown has nothing of ours left to touch.
        if console_attached:
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                    stream.close()
                except Exception:
                    pass
            sys.stdout, sys.stderr = original_stdout, original_stderr


def _run_selftest(importlib) -> int:
    modules = [
        "core.api", "core.classify", "core.db", "core.downloads", "core.m3u",
        "core.playlists", "core.subtitles", "core.sync", "core.vlc_setup",
        "core.vlsub",
        "ui.category_tree", "ui.downloads_panel", "ui.effects_dialog",
        "ui.icons", "ui.main_window", "ui.models", "ui.player_widget",
        "ui.playlist_dialog", "ui.subtitle_dialog", "ui.transport_bar",
    ]
    failures = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    for name in ("PySide6.QtWidgets", "shiboken6", "keyring", "requests"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    try:
        import keyring

        keyring.get_keyring()
    except Exception as exc:
        failures.append(f"keyring backend: {exc}")

    qss = Path(__file__).resolve().parent / "ui" / "theme.qss"
    if not qss.exists():
        failures.append(f"missing data file: {qss}")

    print(f"modules      : {len(modules) - len(failures)}/{len(modules)} imported")

    # Subtitles go over TLS to a host the stdlib's certificate store cannot
    # always verify inside a frozen bundle, so an anonymous login is the one
    # honest way to know the shipped build can actually fetch subtitles.
    # Informational only: no network in CI is not a broken build.
    try:
        from core.vlsub import VLSubClient

        client = VLSubClient()
        client.log_in()
        # A real search too: it exercises response decoding, not just TLS.
        found = client.search_name("Casablanca", languages=["eng"])
        print(f"opensubtitles: anonymous login ok, search returned {len(found)}")
    except Exception as exc:
        print(f"opensubtitles: unreachable ({type(exc).__name__}) — offline?")

    import platform

    lib_dir, plugin_dir = vlc_setup.find_vlc()

    # On macOS an architecture mismatch is the failure that looks like a broken
    # build but is not one, so name both architectures outright. It also tells
    # the arm64 and x86_64 CI jobs apart, which are otherwise identical.
    if sys.platform == "darwin":
        mine = platform.machine()
        theirs = set()
        for name in ("libvlc.dylib", "libvlc.5.dylib"):
            theirs = vlc_setup.macho_arches(lib_dir / name) if lib_dir else set()
            if theirs:
                break
        if not lib_dir:
            print(f"architecture : app {mine}, VLC absent")
        elif theirs:
            verdict = "match" if mine in theirs else "MISMATCH"
            print(f"architecture : app {mine}, VLC {'/'.join(sorted(theirs))} — {verdict}")
        else:
            print(f"architecture : app {mine}, VLC unreadable")

    if lib_dir:
        print(f"libVLC       : {lib_dir}")
        print(f"loads        : {vlc_setup.ensure_vlc()}")
    else:
        # Not a build failure: VLC is the user's to install.
        print("libVLC       : not installed on this machine (app will prompt)")

    for line in failures:
        print(f"FAIL {line}", file=sys.stderr)
    verdict = "FAIL" if failures else "PASS"
    print("SELFTEST:", verdict)

    # Also leave a file behind: if the console trick fails on some Windows
    # host, this is what tells you why a build is broken.
    try:
        from core.db import app_dir

        report = app_dir() / "selftest.txt"
        report.write_text(
            f"SELFTEST: {verdict}\nlibVLC: {vlc_setup.find_vlc()[0]}\n"
            + "\n".join(failures),
            encoding="utf-8",
        )
        print(f"report       : {report}")
    except Exception:
        pass
    return 1 if failures else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()

    QGuiApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    # Windows at a fractional scale (125%, 150%...) is where this actually
    # matters: Qt's older default rounds the scale factor to the nearest
    # whole number before computing window geometry, which then disagrees by
    # a pixel or two with what the OS itself reports for the monitor - a
    # fullscreen window sized from the rounded figure comes out a sliver
    # smaller than the real screen, leaving a hairline gap the desktop shows
    # through at the edges. PassThrough uses the exact factor Windows
    # reports instead of rounding it, which is what removes the mismatch at
    # its source. Harmless everywhere else: macOS reports only whole device
    # pixel ratios, so there is nothing here to round in the first place.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("IPTV Player")
    app.setOrganizationName("IPTVPlayer")
    load_theme(app)

    # Report a missing VLC clearly instead of failing later inside playback.
    if not vlc_setup.ensure_vlc():
        QMessageBox.warning(None, "VLC required", vlc_setup.error_message() or "")

    db = Database()

    from ui.main_window import MainWindow

    window = MainWindow(db)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
