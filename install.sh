#!/bin/sh
# Installs pi5_energyplan.py and everything it needs on Raspberry Pi OS
# (Trixie first, Bookworm too). Without a terminal: double-click
# "Install Pi 5 Energy Plan" in the file manager instead (install_gui.py).
#
#   sudo sh install.sh               install, or update after replacing the script
#   sudo sh install.sh --uninstall   remove it again
#
# Options:
#   --no-eeprom        don't set Geekworm's bootloader settings
#                      (PSU_MAX_CURRENT=5000, and POWER_OFF_ON_HALT=1: the Pi's 5 V
#                      outputs switch off after shutdown; "UPS settings…" can keep
#                      them on instead, and a reinstall keeps that choice)
#   --ups-recommended  turn on battery protection with the recommended settings
#                      (charge to 80 %, safe shutdown at 20 %, battery saver at 50 %)
#                      unless UPS settings were already saved
#   --skip-desktop     don't add the menu entry / panel icon (install_gui.py does it)
#   --force            install even if the installed version is newer
#
# Install = apt packages, I2C on, bootloader settings, script in /usr/local/bin,
# menu entry and panel battery icon for the user who ran sudo/pkexec.
# Running it again never adds anything twice.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/pi5_energyplan.py"
DEST=/usr/local/bin/pi5_energyplan.py
CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt

# Each entry: the package names to try, first one that apt knows wins.
# This is the list from Geekworm's X120X Software guide plus what the window,
# panel icon and overclock test need.
PACKAGES="python3 python3-tk python3-smbus2|python3-smbus i2c-tools
raspi-utils|libraspberrypi-bin python3-libgpiod gpiod rpi-eeprom
python3-gi gir1.2-gdkpixbuf-2.0 librsvg2-common pkexec|policykit-1 stress-ng"

UPS_RECOMMENDED='{"enabled": true, "max": 80, "min": 20, "outage_minutes": 0, "warn_minutes": 1, "saver_enabled": true, "saver_below": 50, "saver_plan": "Underclock"}'

say() { printf '\n==> %s\n' "$*"; }

UNINSTALL="" EEPROM=1 UPS="" DESKTOP=1 FORCE=""
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        --no-eeprom) EEPROM="" ;;
        --ups-recommended) UPS=1 ;;
        --skip-desktop) DESKTOP="" ;;
        --force) FORCE=1 ;;
        *) echo "Unknown option: $arg"; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: sudo sh $0 $*"
    exit 1
fi

# The desktop user (for the menu entry and panel icon): who ran sudo or pkexec
DESK_USER=${SUDO_USER:-}
if [ -z "$DESK_USER" ] && [ -n "${PKEXEC_UID:-}" ]; then
    DESK_USER=$(id -nu "$PKEXEC_UID" 2>/dev/null || true)
fi
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
    echo "Kept: your settings in /etc/pi5-energyplan, the plan block in $CONFIG and the"
    echo "bootloader settings. For Pi defaults, delete the lines from '# >>> pi5-energyplan'"
    echo "to '# <<< pi5-energyplan <<<' in $CONFIG (or restore config.txt.energyplan.bak)."
    echo "The apt packages are left installed."
}

if [ -n "$UNINSTALL" ]; then
    uninstall
    exit 0
fi

if [ ! -f "$SRC" ]; then
    echo "pi5_energyplan.py not found next to install.sh ($HERE)"
    exit 1
fi
# version check: never put an older version over a newer one by accident
ver() { sed -n 's/^VERSION = "\([0-9.]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1; }
NEW_VER=$(ver "$SRC"); NEW_VER=${NEW_VER:-0}
OLD_VER=""
if [ -f "$DEST" ]; then
    OLD_VER=$(ver "$DEST"); OLD_VER=${OLD_VER:-0}
    newest=$(printf '%s\n%s\n' "$OLD_VER" "$NEW_VER" | sort -V | tail -n 1)
    if [ "$OLD_VER" != "$NEW_VER" ] && [ "$newest" = "$OLD_VER" ] && [ -z "$FORCE" ]; then
        echo "Version $OLD_VER is installed, which is newer than this one ($NEW_VER)."
        echo "Nothing was changed. To install this older version anyway: --force"
        exit 1
    fi
fi

if ! cat /proc/device-tree/model /sys/firmware/devicetree/base/model 2>/dev/null | \
        grep -q "Raspberry Pi 5\|Raspberry Pi 500\|Compute Module 5"; then
    echo "Warning: this doesn't look like a Raspberry Pi 5. Installing anyway."
fi

# 1) packages (waits for another apt, e.g. automatic updates, instead of failing)
say "Installing the packages it needs"
APT_WAIT="-o DPkg::Lock::Timeout=300"
# shellcheck disable=SC2086
apt-get $APT_WAIT update
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
DEBIAN_FRONTEND=noninteractive apt-get $APT_WAIT install -y $want

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

# 3) the script (the replaced version is kept, for going back)
if [ -z "$OLD_VER" ]; then
    say "Installing version $NEW_VER to $DEST"
elif cmp -s "$SRC" "$DEST"; then
    say "Version $NEW_VER is already installed: refreshing the rest"
else
    [ "$OLD_VER" = 0 ] && OLD_VER="an older one without a number"
    say "Updating from version $OLD_VER to $NEW_VER"
    mkdir -p /var/lib/pi5-energyplan
    cp -p "$DEST" /var/lib/pi5-energyplan/previous-version.py
    echo "  previous version kept as /var/lib/pi5-energyplan/previous-version.py"
fi
install -m 755 "$SRC" "$DEST"
if [ -f /etc/systemd/system/pi5-energyplan.service ] || \
        [ -f /etc/systemd/system/pi5-battery-guard.service ]; then
    # update: refresh service files, tidy config.txt, run the new version
    python3 "$DEST" --update-root
    systemctl try-restart pi5-battery-guard.service || true
fi

# 4) Geekworm's bootloader settings
if [ -n "$EEPROM" ]; then
    say "Bootloader settings for the UPS (PSU_MAX_CURRENT, POWER_OFF_ON_HALT)"
    if out=$(python3 "$DEST" --eeprom-setup 2>&1); then
        echo "  $out"
        case "$out" in *"after a reboot"*) NEED_REBOOT=1 ;; esac
    else
        echo "  skipped: $out"
    fi
fi

# 5) battery protection with the recommended settings (only on a first install)
if [ -n "$UPS" ]; then
    say "Battery protection"
    if [ -f /etc/pi5-energyplan/battery.json ]; then
        echo "  UPS settings already saved, left as they are"
    else
        python3 "$DEST" --ups-config "$UPS_RECOMMENDED" | sed 's/^/  /'
    fi
fi

# 6) only one program may drive the UPS pins
others=$(pgrep -af 'x120x|qtx120x|merged(-trixie)?\.py|pld(-trixie)?\.py|[^a-z_]bat\.py' \
         2>/dev/null | grep -v pi5_energyplan || true)
if [ -n "$others" ]; then
    say "WARNING: Geekworm UPS scripts are running"
    echo "$others" | sed 's/^/  /'
    echo "  Stop them and remove them from autostart / cron / rc.local: they control"
    echo "  the same GPIO pins and shutdown as this program."
fi

# 7) menu entry and panel battery icon
if [ -z "$DESKTOP" ]; then
    :
elif [ -n "$DESK_USER" ]; then
    say "Adding the menu entry and panel battery icon for $DESK_USER"
    as_user python3 "$DEST" --install-desktop
else
    echo
    echo "Not run through sudo from a desktop user: skipped the menu entry and panel icon."
    echo "Add them later as your normal user:"
    echo "  python3 $DEST --install-desktop"
fi

say "Done"
python3 "$DEST" --status || true
echo
echo "Open 'Pi 5 Energy Plan' from the menu (Preferences), or run: python3 $DEST"
echo "Then pick a plan and set 'UPS settings…'."
if [ -n "$NEED_REBOOT" ]; then
    echo
    echo "Reboot once so the new settings take effect (I2C / bootloader): sudo reboot"
    echo "REBOOT_NEEDED=1"
fi
