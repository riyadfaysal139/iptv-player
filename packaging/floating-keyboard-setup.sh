#!/bin/sh
# One-time setup for the system-wide Floating Keyboard (keyboard.py) on KDE.
#
#  1. /dev/uinput access for the logged-in user (udev "uaccess" tag, plus an
#     ACL right now so no re-login is needed).
#  2. A KWin window rule: the keyboard never takes focus, sits in the overlay
#     layer above fullscreen windows, stays out of the taskbar and alt-tab,
#     and KWin remembers where it was left.
#  3. A launcher (relaunching toggles the keyboard) and an autostart entry
#     that brings it up hidden in the tray at login.
set -eu

HERE=$(cd "$(dirname "$0")/.." && pwd)
PY="$HERE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
EXEC="$PY $HERE/keyboard.py"

# -- 1. uinput --------------------------------------------------------------
# The only step that needs root. SKIP_UINPUT=1 does the rest without it.
RULE=/etc/udev/rules.d/70-floating-keyboard-uinput.rules
if [ "${SKIP_UINPUT:-}" = 1 ]; then
    echo "uinput: skipped (SKIP_UINPUT=1)"
elif [ ! -f "$RULE" ]; then
    printf '%s\n' 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"' \
        | sudo tee "$RULE" >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=uinput
fi
if [ "${SKIP_UINPUT:-}" != 1 ]; then
    sudo setfacl -m "u:$(id -un):rw" /dev/uinput
    echo "uinput: $(getfacl -p /dev/uinput 2>/dev/null | grep "^user:$(id -un)" || echo 'no acl?')"
fi

# -- 2. KWin rule -----------------------------------------------------------
RULES="$HOME/.config/kwinrulesrc"
ID=floating-keyboard
if ! grep -q "^\[$ID\]" "$RULES" 2>/dev/null; then
    if grep -q '^rules=' "$RULES" 2>/dev/null; then
        sed -i "s/^rules=.*/&,$ID/" "$RULES"
        sed -i "s/^count=\([0-9]*\)/count=$(( $(grep '^count=' "$RULES" | cut -d= -f2) + 1 ))/" "$RULES"
    else
        printf '[General]\ncount=1\nrules=%s\n' "$ID" >> "$RULES"
    fi
    cat >> "$RULES" <<EOF

[$ID]
Description=Floating Keyboard: never focused, above everything
wmclass=$ID
wmclassmatch=1
wmclasscomplete=false
acceptfocus=false
acceptfocusrule=2
layer=overlay
layerrule=2
above=true
aboverule=2
skiptaskbar=true
skiptaskbarrule=2
skipswitcher=true
skipswitcherrule=2
skippager=true
skippagerrule=2
position=243,411
positionrule=4
size=793,269
sizerule=4
EOF
    busctl --user call org.kde.KWin /KWin org.kde.KWin reconfigure >/dev/null 2>&1 || true
fi
echo "kwin rule: installed"

# -- 3. launcher + autostart ------------------------------------------------
APPS="$HOME/.local/share/applications"; mkdir -p "$APPS" "$HOME/.config/autostart"
cat > "$APPS/$ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Floating Keyboard
Comment=Small draggable on-screen keyboard; run again to show or hide it
Exec=$EXEC
Icon=input-keyboard-virtual
Terminal=false
Categories=Utility;Accessibility;
Keywords=keyboard;osk;touch;virtual;
StartupNotify=false
EOF
cat > "$HOME/.config/autostart/$ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Floating Keyboard
Exec=$EXEC --hidden
Icon=input-keyboard-virtual
Terminal=false
X-KDE-autostart-phase=2
EOF
update-desktop-database "$APPS" 2>/dev/null || true
echo "launcher: $APPS/$ID.desktop (autostart hidden in tray at login)"
