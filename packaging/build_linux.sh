#!/bin/bash
# Build the Linux artifacts: an AppImage and a plain tarball.
#
#   ./packaging/build_linux.sh
#
# Produces:
#   dist/IPTV_Player-<version>-x86_64.AppImage    single-file, double-click
#   dist/IPTV-Player-<version>-linux-<arch>.tar.gz   extract-and-run fallback
#
# The tarball is not redundant: AppImages need FUSE, and Ubuntu 22.04+ and
# Fedora no longer install libfuse2 by default, so a share of users cannot run
# one at all without installing it first.
#
# Build on the OLDEST distro you intend to support. A binary linked against
# glibc 2.39 (Ubuntu 24.04) will not start on Ubuntu 22.04 or Debian 12, and
# nothing about the build will warn you.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

VERSION="1.0.0"
ARCH="$(uname -m)"
APPDIR="build/AppDir"
APPIMAGE="dist/IPTV_Player-${VERSION}-${ARCH}.AppImage"
TARBALL="dist/IPTV-Player-${VERSION}-linux-${ARCH}.tar.gz"

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

echo "==> Generating icons"
"$PY" packaging/make_icons.py >/dev/null 2>&1 || echo "    (icon step skipped)"

echo "==> Building executable"
rm -rf build/IPTVPlayer dist/IPTVPlayer "$APPDIR"
"$PY" -m PyInstaller packaging/iptvplayer.spec --noconfirm --log-level WARN 2>&1 \
  | grep -viE "libvlc.*not found" || true

[ -d dist/IPTVPlayer ] || { echo "ERROR: dist/IPTVPlayer was not produced"; exit 1; }

echo "==> Assembling AppDir"
mkdir -p "$APPDIR/usr/bin" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/256x256/apps" \
         "$APPDIR/usr/share/metainfo"
cp -a dist/IPTVPlayer/. "$APPDIR/usr/bin/"

# Qt 6's xcb platform plugin links libxcb-cursor, PySide6 does not ship it, and
# plenty of distros do not install it. Without this the app dies at startup with
# "could not load the Qt platform plugin xcb" and no other clue.
for lib in libxcb-cursor.so.0 libxcb-util.so.1; do
  found="$(ldconfig -p 2>/dev/null | awk -v l="$lib" '$1 == l {print $NF; exit}')" || true
  if [ -n "${found:-}" ] && [ -f "$found" ]; then
    cp -L "$found" "$APPDIR/usr/bin/"
    echo "    bundled $lib"
  else
    echo "    WARNING: $lib not found on the build host — the AppImage may not start" >&2
  fi
done

cp packaging/iptvplayer.desktop "$APPDIR/iptvplayer.desktop"
cp packaging/iptvplayer.desktop "$APPDIR/usr/share/applications/"
cp packaging/iptvplayer.appdata.xml "$APPDIR/usr/share/metainfo/com.iptvplayer.app.appdata.xml"

# appimagetool wants a 256px icon; the master is 1024.
ICON_SRC="assets/icon.png"
ICON_DST="$APPDIR/usr/share/icons/hicolor/256x256/apps/iptvplayer.png"
if command -v convert >/dev/null 2>&1; then
  convert "$ICON_SRC" -resize 256x256 "$ICON_DST"
else
  "$PY" - "$ICON_SRC" "$ICON_DST" <<'PYEOF' || cp "$ICON_SRC" "$ICON_DST"
import sys
from PIL import Image

with Image.open(sys.argv[1]) as image:
    image.resize((256, 256), Image.LANCZOS).save(sys.argv[2])
PYEOF
fi
cp "$ICON_DST" "$APPDIR/iptvplayer.png"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
# AppImage entry point. $APPDIR is set by the runtime; resolve it ourselves too
# so the AppDir also works when extracted and run directly.
HERE="$(dirname "$(readlink -f "$0")")"
export APPDIR="${APPDIR:-$HERE}"
export LD_LIBRARY_PATH="$APPDIR/usr/bin:${LD_LIBRARY_PATH:-}"
exec "$APPDIR/usr/bin/IPTVPlayer" "$@"
EOF
chmod +x "$APPDIR/AppRun"

echo "==> Creating $APPIMAGE"
mkdir -p dist
rm -f "$APPIMAGE"

APPIMAGETOOL="${APPIMAGETOOL:-$(command -v appimagetool || true)}"
if [ -z "$APPIMAGETOOL" ]; then
  echo "ERROR: appimagetool not found." >&2
  echo "  Get it from https://github.com/AppImage/AppImageKit/releases" >&2
  echo "  then: chmod +x appimagetool-x86_64.AppImage && export APPIMAGETOOL=\$PWD/appimagetool-x86_64.AppImage" >&2
  echo "  (dist/IPTVPlayer/ is built and runnable regardless.)" >&2
  exit 1
fi

# appimagetool is itself an AppImage, so it needs FUSE to run — which CI
# containers do not have. Extracting it sidesteps that entirely.
ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run --no-appstream \
    "$APPDIR" "$APPIMAGE"
chmod +x "$APPIMAGE"

echo "==> Creating $TARBALL"
rm -f "$TARBALL"
tar -czf "$TARBALL" -C dist IPTVPlayer

echo
echo "Done: $APPIMAGE  ($(du -h "$APPIMAGE" | cut -f1))"
echo "      $TARBALL  ($(du -h "$TARBALL" | cut -f1))"
