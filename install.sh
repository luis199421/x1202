#!/bin/sh
# Installs pi5_energyplan.py and everything it needs on Raspberry Pi OS
# (Trixie first, Bookworm too). Run it from the folder that holds the script:
#
#   sudo sh install.sh               install, or update after replacing the script
#   sudo sh install.sh --uninstall   remove it again
#
# Install = apt packages, I2C on, script in /usr/local/bin, menu entry and
# panel battery icon for the user who ran sudo. Running it again never adds
# anything twice.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/pi5_energyplan.py"
DEST=/usr/local/bin/pi5_energyplan.py
CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt

# Each entry: the package names to try, first one that apt knows wins
# (Trixie name first, older name after the "|").
PACKAGES="python3 python3-tk python3-smbus|python3-smbus2 i2c-tools
raspi-utils|libraspberrypi-bin python3-gi gir1.2-gdkpixbuf-2.0 librsvg2-common
pkexec|policykit-1 stress-ng python3-libgpiod"

say() { printf '\n==> %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: sudo sh $0 $*"
    exit 1
fi

# The desktop user (for the menu entry and panel icon), if run through sudo
DESK_USER=${SUDO_USER:-}
[ "$DESK_USER" = root ] && DESK_USER=

as_user() {
    # run as the desktop user, inside their session if they are logged in,
    # so the panel icon can start right away
    uid=$(id -u "$DESK_USER")
    rt=/run/user/$uid
    if [ -S "$rt/bus" ]; then
        sudo -H -u "$DESK_USER" env XDG_RUNTIME_DIR="$rt" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=$rt/bus" "$@"
    else
        sudo -H -u "$DESK_USER" "$@"
    fi
}

uninstall() {
    if [ -f "$DEST" ]; then
        say "Removing the background services"
        python3 "$DEST" --uninstall-service || true   # keeps the copy while the guard needs it
        [ -f "$DEST" ] && { python3 "$DEST" --uninstall-guard || true; }
    fi
    rm -f "$DEST"
    if [ -n "$DESK_USER" ]; then
        home=$(getent passwd "$DESK_USER" | cut -d: -f6)
        rm -f "$home/.local/share/applications/pi5-energyplan.desktop" \
              "$home/.config/autostart/pi5-battery-tray.desktop"
        pkill -u "$DESK_USER" -f "pi5_energyplan.py --tray" || true
    fi
    say "Uninstalled"
    echo "Kept: your settings in /etc/pi5-energyplan and the plan block in $CONFIG."
    echo "For Pi defaults, delete the lines from '# >>> pi5-energyplan' to"
    echo "'# <<< pi5-energyplan <<<' in $CONFIG (or restore config.txt.energyplan.bak)."
    echo "The apt packages are left installed."
}

if [ "$1" = "--uninstall" ]; then
    uninstall
    exit 0
fi

if [ ! -f "$SRC" ]; then
    echo "pi5_energyplan.py not found next to install.sh ($HERE)"
    exit 1
fi
if ! grep -q "Raspberry Pi 5\|Raspberry Pi 500\|Compute Module 5" \
        /proc/device-tree/model 2>/dev/null; then
    echo "Warning: this doesn't look like a Raspberry Pi 5. Installing anyway."
fi

# 1) packages
say "Installing the packages it needs"
apt-get update
want=""
for choice in $PACKAGES; do
    found=""
    for p in $(echo "$choice" | tr '|' ' '); do
        if apt-cache show "$p" >/dev/null 2>&1; then
            found=$p
            break
        fi
    done
    if [ -n "$found" ]; then
        want="$want $found"
    else
        echo "  not available, skipped: $choice"
    fi
done
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get install -y $want

# 2) I2C for the X1202 fuel gauge
say "Turning on I2C"
NEED_REBOOT=""
if command -v raspi-config >/dev/null 2>&1; then
    if [ "$(raspi-config nonint get_i2c)" != "0" ]; then
        raspi-config nonint do_i2c 0
        NEED_REBOOT=1
    else
        echo "  already on"
    fi
elif ! grep -q "^dtparam=i2c_arm=on" "$CONFIG" 2>/dev/null; then
    echo "  raspi-config not found: add 'dtparam=i2c_arm=on' to $CONFIG and reboot"
fi

# 3) the script
say "Installing the script to $DEST"
install -m 755 "$SRC" "$DEST"
if [ -f /etc/systemd/system/pi5-energyplan.service ] || \
        [ -f /etc/systemd/system/pi5-battery-guard.service ]; then
    # update: refresh service files, tidy config.txt, run the new version
    python3 "$DEST" --update-root
    systemctl try-restart pi5-battery-guard.service || true
fi

# 4) menu entry and panel battery icon
if [ -n "$DESK_USER" ]; then
    say "Adding the menu entry and panel battery icon for $DESK_USER"
    as_user python3 "$DEST" --install-launcher
    as_user python3 "$DEST" --install-tray
else
    echo
    echo "Not run through sudo from a desktop user: skipped the menu entry and panel icon."
    echo "Add them later as your normal user:"
    echo "  python3 $DEST --install-launcher && python3 $DEST --install-tray"
fi

say "Done"
python3 "$DEST" --status || true
echo
echo "Open 'Pi 5 Energy Plan' from the menu (Preferences), or run: python3 $DEST"
echo "Then pick a plan and set 'UPS settings…'."
if [ -n "$NEED_REBOOT" ]; then
    echo
    echo "I2C was just turned on: reboot once so the battery can be read (sudo reboot)."
fi
