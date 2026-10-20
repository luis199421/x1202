#!/bin/sh
# Runs pi5_energyplan.py against a simulated Raspberry Pi 5 + Geekworm X1202.
#
# Needs Linux, root and `unshare` (it is for development machines, NOT for the
# Pi itself). Everything happens in a private mount namespace: fake sysfs,
# config.txt, /etc and systemd folders are tmpfs mounts that vanish when the
# test ends, and pinctrl / vcgencmd / systemctl / shutdown / reboot are fakes
# that only record how they were called.
#
#   sudo sh tests/run_sim_tests.sh            (Python 3 from PATH)
#   sudo PY=python3.11 sh tests/run_sim_tests.sh
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
PY=${PY:-python3}
PYBIN=$(command -v "$PY")
exec unshare -m sh -ec '
  mount --make-rprivate /
  for d in /sys/class /sys/devices/system/cpu /boot /etc/systemd/system /usr/local/bin; do
    mount -t tmpfs tmpfs "$d"
  done
  for d in /etc/pi5-energyplan /var/lib/pi5-energyplan /run/pi5-energyplan; do
    mkdir -p "$d"; mount -t tmpfs tmpfs "$d"
  done
  [ -e /dev/i2c-1 ] || { touch /dev/i2c-1; trap "rm -f /dev/i2c-1" EXIT; }
  export FAKE_STATE=$(mktemp -d)
  export PATH="'"$HERE"'/fakes/bin:$PATH"
  export PYTHONPATH="'"$HERE"'/fakes/py"
  "'"$PYBIN"'" "'"$HERE"'/test_sim.py" -v
'
