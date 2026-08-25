# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds the macOS .app and the Windows .exe.

Build from the project root:
    pyinstaller packaging/iptvplayer.spec --noconfirm

libVLC is deliberately NOT bundled. The app detects the user's VLC install and
shows an install prompt when it is missing, which keeps the download small and
avoids redistributing VideoLAN's binaries.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is injected by PyInstaller; it is the folder holding this file.
ROOT = Path(SPECPATH).parent
APP_NAME = "IPTV Player"
EXE_NAME = "IPTVPlayer"
VERSION = "1.3.0"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

icon = None
if IS_MAC and (ROOT / "assets/icon.icns").exists():
    icon = str(ROOT / "assets/icon.icns")
elif IS_WIN and (ROOT / "assets/icon.ico").exists():
    icon = str(ROOT / "assets/icon.ico")

datas = [(str(ROOT / "ui/theme.qss"), "ui")]
if (ROOT / "assets/icon.png").exists():
    datas.append((str(ROOT / "assets/icon.png"), "assets"))

# keyring resolves its backend at runtime, so the backends must be collected.
hiddenimports = collect_submodules("keyring.backends")

# VLSub's endpoint is XML-RPC. The module is only imported inside functions, so
# it is spelled out here rather than trusted to static analysis.
hiddenimports += ["xmlrpc.client", "gzip"]
if IS_MAC:
    hiddenimports += ["keyring.backends.macOS"]
elif IS_WIN:
    # keyring's Windows backend rides on pywin32-ctypes (pulled in by keyring
    # itself), not full pywin32 - so no win32timezone here.
    hiddenimports += ["keyring.backends.Windows", "win32ctypes.core"]
else:
    hiddenimports += ["keyring.backends.SecretService", "jeepney"]

# PySide6 ships every Qt module; this app uses only Widgets/Gui/Core. Dropping
# the rest takes the build from roughly a gigabyte to a manageable size.
excludes = [
    "tkinter", "unittest", "pydoc_data", "test", "distutils",
    "matplotlib", "numpy", "pandas", "scipy", "PIL", "setuptools", "pip",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtQml", "PySide6.QtQmlModels", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtOpenGLFunctions", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtHttpServer",
    "PySide6.QtGraphs", "PySide6.QtStateMachine",
    # Never exclude shiboken6: it is PySide6's binding core, not an optional
    # Qt module, and dropping it stops the app from importing PySide6 at all.
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Drop the Qt libraries/plugins the excludes above cannot reach (PySide6's hook
# copies some binaries regardless of the Python-level excludes).
_DROP = (
    "QtWebEngine", "QtQuick", "QtQml", "Qt3D", "QtCharts", "QtDataVisualization",
    "QtMultimedia", "QtBluetooth", "QtNfc", "QtPositioning", "QtLocation",
    "QtSensors", "QtSerial", "QtRemoteObjects", "QtScxml", "QtDesigner",
    "QtHelp", "QtUiTools", "QtPdf", "QtSpatialAudio", "QtTextToSpeech",
    "QtHttpServer", "QtGraphs", "QtWebSockets", "QtWebChannel", "QtSql",
    "QtTest", "qtwebengine", "qml",
)


def _keep(entry):
    name = entry[0].replace("\\", "/")
    return not any(token.lower() in name.lower() for token in _DROP)


a.binaries = TOC([b for b in a.binaries if _keep(b)])
a.datas = TOC([d for d in a.datas if _keep(d)])

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app: no console window on Windows
    disable_windowed_traceback=False,
    argv_emulation=False,   # keep off; the app takes no file arguments
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    version=str(ROOT / "packaging/win_version.txt") if IS_WIN else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=EXE_NAME,
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="com.iptvplayer.app",
        version=VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.video",
            "NSHumanReadableCopyright": "Playback powered by VLC (VideoLAN).",
            # The app talks to IPTV portals over plain HTTP, which App
            # Transport Security blocks by default.
            "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
        },
    )
