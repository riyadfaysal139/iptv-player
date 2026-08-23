#!/bin/bash
# Build the macOS installer (.dmg).
#
#   ./packaging/build_macos.sh
#
# Produces dist/IPTV-Player-<version>-macOS-<arch>.dmg containing the app and a
# drag-to-Applications shortcut.
#
# Optional signing/notarisation (skipped when the variables are unset):
#   SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE="my-notary-profile"      # from: xcrun notarytool store-credentials
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

APP_NAME="IPTV Player"
VERSION="1.0.0"
ARCH="$(uname -m)"
APP="dist/${APP_NAME}.app"
DMG="dist/IPTV-Player-${VERSION}-macOS-${ARCH}.dmg"
STAGE="build/dmg"

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

echo "==> Generating icons"
"$PY" packaging/make_icons.py >/dev/null 2>&1 || echo "    (icon step skipped)"

echo "==> Building app bundle"
rm -rf build/IPTVPlayer "$APP" dist/IPTVPlayer
"$PY" -m PyInstaller packaging/iptvplayer.spec --noconfirm --log-level WARN 2>&1 \
  | grep -viE "libvlc.*not found" || true

[ -d "$APP" ] || { echo "ERROR: $APP was not produced"; exit 1; }

echo "==> Signing"
if [ -n "${SIGN_IDENTITY:-}" ]; then
  codesign --force --deep --options runtime --timestamp \
           --sign "$SIGN_IDENTITY" "$APP"
  echo "    signed with Developer ID"
else
  # An ad-hoc signature is required for arm64 binaries to run at all. It does
  # not satisfy Gatekeeper, so first launch still needs right-click > Open.
  codesign --force --deep --sign - "$APP"
  echo "    ad-hoc signed (no Developer ID set)"
fi
codesign --verify --deep --strict "$APP" && echo "    signature verifies"

echo "==> Staging disk image"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

cat > "$STAGE/READ ME FIRST.txt" <<'EOF'
IPTV Player
===========

1. Drag "IPTV Player" onto the Applications folder shown here.

2. VLC is required.
   This player uses VLC's engine to decode video. If you do not already
   have VLC, install it from https://www.videolan.org/vlc/ first.

3. First launch:
   macOS blocks apps from unidentified developers. Right-click (or
   Control-click) "IPTV Player" in Applications and choose "Open", then
   confirm. You only need to do this once.

4. On first run the app asks for your IPTV playlist. Paste your provider's
   portal address or a full M3U link - the fields fill in automatically.
EOF

echo "==> Creating $DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" \
               -ov -format UDZO -imagekey zlib-level=9 "$DMG" >/dev/null
rm -rf "$STAGE"

if [ -n "${NOTARY_PROFILE:-}" ]; then
  echo "==> Notarising"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi

echo
echo "Done: $DMG  ($(du -h "$DMG" | cut -f1))"
