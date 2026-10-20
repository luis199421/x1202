#!/bin/sh
# Installs Pi 5 Energy Plan on Raspberry Pi OS (Trixie first, Bookworm too).
# Without a terminal: double-click "Install Pi 5 Energy Plan" in the file manager
# instead (install_gui.py).
#
#   sudo sh install.sh               install, or update from a newer download
#   sudo sh install.sh --uninstall   remove it again
#
# It builds the pi5-energyplan package from the files next to it and installs it
# with apt (which also installs what it needs). From then on, new versions
# published on GitHub show up in the Pi's updater and in `sudo apt upgrade`.
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
#   --from-package     (used by the package itself) only the system part
#
# Running it again never adds anything twice.
set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/pi5_energyplan.py"
DEST=/usr/local/bin/pi5_energyplan.py
CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
STATE=/var/lib/pi5-energyplan
CACHE=/var/cache/pi5-energyplan
REBOOT_MARK=/run/pi5-energyplan/reboot-needed
APT_WAIT="-o DPkg::Lock::Timeout=300"

UPS_RECOMMENDED='{"enabled": true, "max": 80, "min": 20, "outage_minutes": 0, "warn_minutes": 1, "saver_enabled": true, "saver_below": 50, "saver_plan": "Underclock"}'

say() { printf '\n==> %s\n' "$*"; }

UNINSTALL="" EEPROM=1 UPS="" DESKTOP=1 FORCE="" PACKAGE=""
for arg in "$@"; do
    case "$arg" in
        --uninstall) UNINSTALL=1 ;;
        --no-eeprom) EEPROM="" ;;
        --ups-recommended) UPS=1 ;;
        --skip-desktop) DESKTOP="" ;;
        --force) FORCE=1 ;;
        --from-package) PACKAGE=1 ;;
        *) echo "Unknown option: $arg"; exit 2 ;;
    esac
done

# --force reaches the package's install step through apt / dpkg like this
[ -n "${PI5EP_FORCE:-}" ] && FORCE=1
export PI5EP_FORCE="$FORCE"

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
    # as_user USER CMD...: run as a desktop user, inside their session if they are
    # logged in, so the panel icon can (re)start right away
    u=$1
    shift
    rt=/run/user/$(id -u "$u")
    if [ -S "$rt/bus" ]; then
        sudo -H -u "$u" env XDG_RUNTIME_DIR="$rt" DBUS_SESSION_BUS_ADDRESS="unix:path=$rt/bus" "$@"
    else
        sudo -H -u "$u" "$@"
    fi
}

desktop_users() {
    # users who have the menu entry or panel icon
    getent passwd | while IFS=: read -r name _ uid _ _ home _; do
        [ "$uid" -ge 1000 ] 2>/dev/null && [ "$uid" -lt 65534 ] || continue
        if [ -f "$home/.local/share/applications/pi5-energyplan.desktop" ] ||
                [ -f "$home/.config/autostart/pi5-battery-tray.desktop" ]; then
            echo "$name"
        fi
    done
}

package_installed() {
    dpkg-query -W -f='${Status}' pi5-energyplan 2>/dev/null | grep -q "install ok installed"
}

ver() { sed -n 's/^VERSION = "\([0-9.]*\)".*/\1/p' "$1" 2>/dev/null | head -n 1; }

remove_desktop() {
    home=$(getent passwd "$1" | cut -d: -f6)
    rm -f "$home/.local/share/applications/pi5-energyplan.desktop" \
          "$home/.config/autostart/pi5-battery-tray.desktop"
    pkill -u "$1" -f "pi5_energyplan.py --tray" || true
}

uninstall() {
    if [ -z "$PACKAGE" ] && package_installed; then
        # the package's own removal step (prerm) does the rest, for every user
        say "Removing the pi5-energyplan package"
        # shellcheck disable=SC2086
        apt-get $APT_WAIT remove -y pi5-energyplan
    else
        if [ -f "$DEST" ]; then
            say "Removing the background services"
            python3 "$DEST" --uninstall-service || true   # keeps the copy while the guard needs it
            [ -f "$DEST" ] && { python3 "$DEST" --uninstall-guard || true; }
        fi
        rm -f "$DEST"
        users=$(desktop_users)
        [ -n "$DESK_USER" ] && users="$users $DESK_USER"
        for u in $users; do
            remove_desktop "$u"
        done
    fi
    [ -n "$PACKAGE" ] && return
    say "Uninstalled"
    echo "Kept: your settings in /etc/pi5-energyplan, the plan block in $CONFIG and the"
    echo "bootloader settings. For Pi defaults, delete the lines from '# >>> pi5-energyplan'"
    echo "to '# <<< pi5-energyplan <<<' in $CONFIG (or restore config.txt.energyplan.bak)."
    echo "The other apt packages are left installed. Settings too: sudo apt purge pi5-energyplan"
}

if [ -n "$UNINSTALL" ]; then
    uninstall
    exit 0
fi

if [ ! -f "$SRC" ]; then
    echo "pi5_energyplan.py not found next to install.sh ($HERE)"
    exit 1
fi
if [ -z "$PACKAGE" ] && [ ! -f "$HERE/packaging/build-deb.sh" ]; then
    echo "The packaging folder is missing next to install.sh ($HERE)."
    echo "Download or clone the whole project, not only these files."
    exit 1
fi

# version check: never put an older version over a newer one by accident
NEW_VER=$(ver "$SRC"); NEW_VER=${NEW_VER:-0}
OLD_VER=""
KEEP_NEWER=""
if [ -f "$DEST" ]; then
    OLD_VER=$(ver "$DEST"); OLD_VER=${OLD_VER:-0}
    newest=$(printf '%s\n%s\n' "$OLD_VER" "$NEW_VER" | sort -V | tail -n 1)
    if [ "$OLD_VER" != "$NEW_VER" ] && [ "$newest" = "$OLD_VER" ] && [ -z "$FORCE" ]; then
        if [ -n "$PACKAGE" ]; then
            KEEP_NEWER=1      # e.g. a newer copy installed by hand: keep it
        else
            echo "Version $OLD_VER is installed, which is newer than this one ($NEW_VER)."
            echo "Nothing was changed. To install this older version anyway: --force"
            exit 1
        fi
    fi
fi

# ----------------------------------------------------------------------------
# System part. Runs inside the package's install step, so it's the same for
# install.sh, the click installer, `apt upgrade` and the Pi's updater.
# ----------------------------------------------------------------------------
system_part() {
    mkdir -p "$(dirname "$REBOOT_MARK")"

    # I2C for the X1202 fuel gauge
    say "Turning on I2C"
    if command -v raspi-config >/dev/null 2>&1; then
        if [ "$(raspi-config nonint get_i2c)" != "0" ]; then
            raspi-config nonint do_i2c 0
            touch "$REBOOT_MARK"
        else
            echo "  already on"
        fi
    elif ! grep -q "^dtparam=i2c_arm=on" "$CONFIG" 2>/dev/null; then
        echo "  raspi-config not found: add 'dtparam=i2c_arm=on' to $CONFIG and reboot"
    fi

    # the program (the replaced version is kept, for going back)
    if [ -n "$KEEP_NEWER" ]; then
        say "Keeping the installed version $OLD_VER (newer than this package, $NEW_VER)"
    else
        if [ -z "$OLD_VER" ]; then
            say "Installing version $NEW_VER to $DEST"
        elif cmp -s "$SRC" "$DEST"; then
            say "Version $NEW_VER is already installed: refreshing the rest"
        else
            [ "$OLD_VER" = 0 ] && OLD_VER="an older one without a number"
            say "Updating from version $OLD_VER to $NEW_VER"
            mkdir -p "$STATE"
            cp -p "$DEST" "$STATE/previous-version.py"
            echo "  previous version kept as $STATE/previous-version.py"
        fi
        install -m 755 "$SRC" "$DEST"
    fi
    if [ -f /etc/systemd/system/pi5-energyplan.service ] || \
            [ -f /etc/systemd/system/pi5-battery-guard.service ]; then
        # refresh service files, tidy config.txt, run the new version
        python3 "$DEST" --update-root
        systemctl try-restart pi5-battery-guard.service || true
    fi

    # menu entry / panel icon of everyone who has them: point at the new version
    for u in $(desktop_users); do
        echo "  refreshing the menu entry / panel icon of $u"
        as_user "$u" python3 "$DEST" --refresh-desktop || true
    done

    # only one program may drive the UPS pins
    others=$(pgrep -af 'x120x|qtx120x|merged(-trixie)?\.py|pld(-trixie)?\.py|[^a-z_]bat\.py' \
             2>/dev/null | grep -v pi5_energyplan || true)
    if [ -n "$others" ]; then
        say "WARNING: Geekworm UPS scripts are running"
        echo "$others" | sed 's/^/  /'
        echo "  Stop them and remove them from autostart / cron / rc.local: they control"
        echo "  the same GPIO pins and shutdown as this program."
    fi
}

if [ -n "$PACKAGE" ]; then
    system_part
    exit 0
fi

if ! cat /proc/device-tree/model /sys/firmware/devicetree/base/model 2>/dev/null | \
        grep -q "Raspberry Pi 5\|Raspberry Pi 500\|Compute Module 5"; then
    echo "Warning: this doesn't look like a Raspberry Pi 5. Installing anyway."
fi
rm -f "$REBOOT_MARK"

# 1) build the package from these files and install it with apt: apt installs what
#    it needs, and its install step runs the system part above
say "Building the pi5-energyplan $NEW_VER package"
mkdir -p "$CACHE"
rm -f "$CACHE"/pi5-energyplan_*_all.deb          # keep only this one
DEB=$(sh "$HERE/packaging/build-deb.sh" "$HERE" "$CACHE")
echo "  $DEB"

say "Installing it and the packages it needs"
# shellcheck disable=SC2086
apt-get $APT_WAIT update || echo "  (couldn't refresh the package lists: using the ones there are)"
DOWNGRADE=""
[ -n "$FORCE" ] && DOWNGRADE="--allow-downgrades"
# --reinstall: "Install again" also repairs the same version
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get $APT_WAIT install -y --reinstall $DOWNGRADE "$DEB"
NEED_REBOOT=""
[ -f "$REBOOT_MARK" ] && NEED_REBOOT=1

# 2) Geekworm's bootloader settings
if [ -n "$EEPROM" ]; then
    say "Bootloader settings for the UPS (PSU_MAX_CURRENT, POWER_OFF_ON_HALT)"
    if out=$(python3 "$DEST" --eeprom-setup 2>&1); then
        echo "  $out"
        case "$out" in *"after a reboot"*) NEED_REBOOT=1 ;; esac
    else
        echo "  skipped: $out"
    fi
fi

# 3) battery protection with the recommended settings (only on a first install)
if [ -n "$UPS" ]; then
    say "Battery protection"
    if [ -f /etc/pi5-energyplan/battery.json ]; then
        echo "  UPS settings already saved, left as they are"
    else
        python3 "$DEST" --ups-config "$UPS_RECOMMENDED" | sed 's/^/  /'
    fi
fi

# 4) menu entry and panel battery icon
if [ -z "$DESKTOP" ]; then
    :
elif [ -n "$DESK_USER" ]; then
    say "Adding the menu entry and panel battery icon for $DESK_USER"
    as_user "$DESK_USER" python3 "$DEST" --install-desktop
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
if [ -f /etc/apt/sources.list.d/pi5-energyplan.sources ]; then
    echo "New versions will show up in the Pi's updater (and in: sudo apt upgrade)."
fi
if [ -n "$NEED_REBOOT" ]; then
    echo
    echo "Reboot once so the new settings take effect (I2C / bootloader): sudo reboot"
    echo "REBOOT_NEEDED=1"
fi
