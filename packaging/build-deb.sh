#!/bin/sh
# Builds pi5-energyplan_<VERSION>_all.deb from a checkout or extracted ZIP.
#
#   sh packaging/build-deb.sh [SOURCE_DIR] [OUTPUT_DIR]
#
# The version comes from VERSION in pi5_energyplan.py. If packaging/apt-repo-key.asc
# exists, the package also adds the project's apt repository (GitHub Pages), so the
# Pi's updater and `apt upgrade` offer new versions. APT_URL overrides its address.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=$(cd "${1:-$HERE/..}" && pwd)
OUT=${2:-$SRC/dist}
VERSION=$(sed -n 's/^VERSION = "\([0-9.]*\)".*/\1/p' "$SRC/pi5_energyplan.py" | head -n 1)
[ -n "$VERSION" ] || { echo "No VERSION line in $SRC/pi5_energyplan.py"; exit 1; }

# where the apt repository is published: https://OWNER.github.io/REPO/apt
if [ -z "${APT_URL:-}" ]; then
    repo=${GITHUB_REPOSITORY:-}
    if [ -z "$repo" ]; then
        repo=$(git -C "$SRC" remote get-url origin 2>/dev/null |
               sed -n 's#.*github\.com[:/]\([^/]*/[^/]*\)$#\1#p' | sed 's/\.git$//')
    fi
    repo=${repo:-luis199421/x1202}
    APT_URL="https://${repo%%/*}.github.io/${repo#*/}/apt"
fi

ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT
chmod 755 "$ROOT"
SHARE=$ROOT/usr/share/pi5-energyplan
install -d "$SHARE" "$ROOT/DEBIAN" "$ROOT/usr/share/doc/pi5-energyplan"
install -m 755 "$SRC/pi5_energyplan.py" "$SRC/install.sh" "$SRC/install_gui.py" "$SHARE/"
install -m 644 "$SRC/README.md" "$ROOT/usr/share/doc/pi5-energyplan/"

KEY=$SRC/packaging/apt-repo-key.asc
if [ -f "$KEY" ]; then
    install -D -m 644 "$KEY" "$ROOT/usr/share/keyrings/pi5-energyplan-archive-keyring.asc"
    install -d "$ROOT/etc/apt/sources.list.d"
    # not a conffile on purpose: dpkg would stop and ask on upgrades if it changed
    cat > "$ROOT/etc/apt/sources.list.d/pi5-energyplan.sources" <<SOURCES
Types: deb
URIs: $APT_URL
Suites: stable
Components: main
Signed-By: /usr/share/keyrings/pi5-energyplan-archive-keyring.asc
SOURCES
else
    echo "Note: packaging/apt-repo-key.asc not found: this package won't add the update" \
         "source (see packaging/README.md)" >&2
fi

for f in postinst prerm postrm; do
    install -m 755 "$HERE/debian/$f" "$ROOT/DEBIAN/$f"
done
SIZE=$(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)
cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: pi5-energyplan
Version: $VERSION
Architecture: all
Maintainer: luis199421 <luis199421@users.noreply.github.com>
Installed-Size: $SIZE
Depends: python3, python3-tk, python3-smbus2 | python3-smbus, i2c-tools, raspi-utils | libraspberrypi-bin, python3-gi, gir1.2-gdkpixbuf-2.0, pkexec | policykit-1
Recommends: librsvg2-common, python3-libgpiod, gpiod, rpi-eeprom, stress-ng
Section: utils
Priority: optional
Homepage: https://github.com/${repo:-luis199421/x1202}
Description: Energy plans for the Raspberry Pi 5 and Geekworm X1202 UPS manager
 One-click energy plans (CPU governor, max speed, fan curve, overclock with
 GPU) for the Raspberry Pi 5 + Active Cooler, and battery management for the
 Geekworm X1200/X1201/X1202 UPS: charge limit, safe shutdown, battery saver,
 a battery icon in the panel and a sweet spot overclock test.
CONTROL

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$ROOT" "$OUT/pi5-energyplan_${VERSION}_all.deb" >/dev/null
echo "$OUT/pi5-energyplan_${VERSION}_all.deb"
