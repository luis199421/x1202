#!/bin/sh
# Pi 5 energy saving that doesn't change how the Pi feels to use.
#   sudo sh energy-save.sh          turn it on (running it again changes nothing)
#   sudo sh energy-save.sh undo     back to the Raspberry Pi OS defaults
# What it does:
#   * PCIe link (NVMe): deepest idle states (pcie_aspm.policy=powersupersave in
#     cmdline.txt). The drive wakes up in microseconds; it saves power while idle.
#   * Wi-Fi: power saving on (NetworkManager), the radio sleeps between packets.
#   * Disk: dirty data is written every 15 s instead of every 5 s, so the drive
#     sleeps more (the X1202 keeps the Pi running through power cuts).
# Each step is undone by "undo". Needs a reboot for the PCIe part.
set -e
[ "$(id -u)" = 0 ] || { echo "Run it with sudo"; exit 1; }
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
NM=/etc/NetworkManager/conf.d/90-energy-save-wifi.conf
SYSCTL=/etc/sysctl.d/91-energy-save.conf

if [ "$1" = undo ]; then
    sed -i 's/ pcie_aspm.policy=[a-z]*//g' "$CMDLINE"
    rm -f "$NM" "$SYSCTL"
    sysctl -q -w vm.dirty_writeback_centisecs=500 || true
    command -v iw >/dev/null && iw dev wlan0 set power_save off 2>/dev/null || true
    echo "Energy saving removed (the PCIe part after a reboot)."
    exit 0
fi

cp -n "$CMDLINE" "$CMDLINE.before-energy-save" 2>/dev/null || true
# one line only: drop older values and the "power saving off" switches, add ours once
sed -i -e 's/ pcie_aspm=off//g; s/ nvme_core.default_ps_max_latency_us=0//g' \
       -e 's/ pcie_aspm.policy=[a-z]*//g' -e '1 s/$/ pcie_aspm.policy=powersupersave/' "$CMDLINE"

if [ -d /etc/NetworkManager ]; then
    mkdir -p /etc/NetworkManager/conf.d
    printf '[connection]\n# 3 = Wi-Fi power saving on (energy-save.sh)\nwifi.powersave = 3\n' > "$NM"
fi
command -v iw >/dev/null && iw dev wlan0 set power_save on 2>/dev/null || true

printf '# energy-save.sh: write to disk every 15 s instead of 5 s\nvm.dirty_writeback_centisecs = 1500\n' > "$SYSCTL"
sysctl -q -p "$SYSCTL" || true

echo "Energy saving on. Reboot for the PCIe part. Check after the reboot:"
echo "  cat /sys/module/pcie_aspm/parameters/policy     -> [powersupersave]"
echo "  iw dev wlan0 get power_save                      -> Power save: on"
echo "Undo: sudo sh $(readlink -f "$0") undo"
