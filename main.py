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
    if sys.platform.startswith("win"):
        try:
            import ctypes

            if ctypes.windll.kernel32.AttachConsole(-1):
                sys.stdout = open("CONOUT$", "w", buffering=1)
                sys.stderr = open("CONOUT$", "w", buffering=1)
        except Exception:
            pass

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

    lib_dir, plugin_dir = vlc_setup.find_vlc()
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
