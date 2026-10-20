#!/usr/bin/env python3
"""
pi5_energyplan.py - One-click energy plans for the Raspberry Pi 5 + Active Cooler.

Install (packages, I2C, menu entry, panel icon):  sudo sh install.sh

GUI:   python3 pi5_energyplan.py
CLI:   python3 pi5_energyplan.py --list
       python3 pi5_energyplan.py --status
       sudo python3 pi5_energyplan.py --apply Cool
       python3 pi5_energyplan.py --install-launcher   (adds a menu entry)
       sudo python3 pi5_energyplan.py --uninstall-service (stop restoring at boot)
       sudo python3 pi5_energyplan.py --uninstall-guard   (remove the UPS battery guard)
       sudo python3 pi5_energyplan.py --autotest          (find overclock sweet spot)
       python3 pi5_energyplan.py --autotest-status        (progress / results)
       sudo python3 pi5_energyplan.py --autotest-cancel
       python3 pi5_energyplan.py --install-tray           (battery % in the panel)
       sudo python3 pi5_energyplan.py --eeprom-setup      (Geekworm bootloader settings)
       sudo python3 pi5_energyplan.py --usb-5v-when-off on|off  (5 V outputs after shutdown)
       python3 pi5_energyplan.py --battery                (live battery in terminal)
       python3 pi5_energyplan.py --monitor                (full UPS + Pi report, live)
       sudo python3 pi5_energyplan.py --charge-full on    (charge to 100 % once)

UPS / battery (Geekworm X1200/X1201/X1202): open "UPS settings…" in the GUI to set
  * Stop charging at X %        (charge limit, resumes 5 % below)
  * Safe shutdown at Y %        (on battery only; cancelled if power returns)
  * Shutdown after a power outage of 15 s to 1 h, with a 1-5 minute warning
  * Charge to 100 % once (e.g. before a trip), then back to the limit
  * Battery saver below Z %     (switches to Underclock/Eco on battery, back when plugged in)
  These run in the background as pi5-battery-guard.service, even with the GUI closed.

What a plan changes
  * Right away:    CPU governor, CPU max frequency, fan trip temperatures
                   (the trip temperatures only if the kernel allows writing them).
  * After reboot:  full fan curve (temperatures + speeds) and arm_freq, written to a
                   managed block at the end of /boot/firmware/config.txt.
                   A backup is made the first time: config.txt.energyplan.bak
  * Every boot:    a systemd service (pi5-energyplan.service) re-applies the saved
                   plan's CPU governor and max frequency, which Linux resets on boot.
                   It is installed automatically the first time you save a plan.

Edit PLANS below to make your own plans.
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:  # GUI optional; CLI still works
    tk = None

# --------------------------------------------------------------------------- #
#  PLANS - edit freely
#  fan: 4 points of (start temp °C, hysteresis °C, speed 0-255), rising temps
# --------------------------------------------------------------------------- #
# Fan profiles: 4 points of (start temp °C, hysteresis °C, speed 0-255), rising temps
FAN_PROFILES = {
    "Silent":  [(60, 5, 75), (67, 5, 125), (73, 5, 175), (80, 5, 250)],
    "Quiet":   [(55, 5, 75), (62, 5, 125), (70, 5, 175), (78, 5, 250)],
    "Default": [(50, 5, 75), (60, 5, 125), (67.5, 5, 175), (75, 5, 250)],
    "Cool":    [(40, 3, 100), (50, 3, 150), (58, 3, 200), (65, 3, 255)],
    "Strong":  [(35, 3, 150), (45, 3, 200), (52, 3, 230), (60, 3, 255)],
    "Max":     [(30, 2, 200), (40, 2, 230), (50, 2, 255), (55, 2, 255)],
}


def fan_profile_info(name):
    c = FAN_PROFILES[name]
    return f"starts at {c[0][0]:g} °C, full speed at {c[-1][0]:g} °C"


def fan_profile_of(curve):
    curve = [tuple(x) for x in curve]
    return next((n for n, c in FAN_PROFILES.items() if c == curve), "Custom")


PLANS = {
    "Underclock": {
        "desc": "Coolest & quietest. Max 1.5 GHz.",
        "governor": "powersave",
        "max_freq_mhz": 1500,
        "fan": list(FAN_PROFILES["Silent"]),
        "color": "#78909c",
    },
    "Eco": {
        "desc": "Quiet & low power. Max 1.8 GHz.",
        "governor": "conservative",
        "max_freq_mhz": 1800,
        "fan": list(FAN_PROFILES["Quiet"]),
        "color": "#26a69a",
    },
    "Balanced": {
        "desc": "Raspberry Pi defaults. 2.4 GHz.",
        "governor": "ondemand",
        "max_freq_mhz": 2400,
        "fan": list(FAN_PROFILES["Default"]),
        "color": "#42a5f5",
    },
    "Cool": {
        "desc": "Full 2.4 GHz, kept cool.",
        "governor": "schedutil",
        "max_freq_mhz": 2400,
        "fan": list(FAN_PROFILES["Cool"]),
        "color": "#29b6f6",
    },
    "Max Cool": {
        "desc": "Coolest at speed. Max 2.0 GHz.",
        "governor": "conservative",
        "max_freq_mhz": 2000,
        "fan": list(FAN_PROFILES["Max"]),
        "color": "#ab47bc",
    },
    "Performance": {
        "desc": "Full 2.4 GHz, always top speed.",
        "governor": "performance",
        "max_freq_mhz": 2400,
        "fan": list(FAN_PROFILES["Cool"]),
        "color": "#ff7043",
    },
    # ---- Overclock: need a good power supply (official 27 W) and the Active Cooler.
    # over_voltage_delta is extra CPU voltage in microvolts (50000 = +0.05 V).
    # v3d_freq is the 3D GPU clock (Pi 5 default 960 MHz). Only the 3D block is
    # raised, as the Raspberry Pi docs recommend: gpu_freq would also overclock the
    # core / ISP / video clocks, and gpu_freq=1000 at 3.0 GHz freezes some Pi 5s.
    "OC 2.6 GHz": {
        "desc": "Mild overclock + GPU 1.0 GHz, works on most Pi 5s.",
        "governor": "ondemand",
        "max_freq_mhz": 2600,
        "over_voltage_delta": 25000,
        "v3d_freq": 1000,
        "fan": list(FAN_PROFILES["Strong"]),
        "color": "#ffca28",
        "warn": True,
    },
    "OC 2.8 GHz": {
        "desc": "Medium overclock, +0.05 V, GPU 1.05 GHz.",
        "governor": "ondemand",
        "max_freq_mhz": 2800,
        "over_voltage_delta": 50000,
        "v3d_freq": 1050,
        "fan": list(FAN_PROFILES["Strong"]),
        "color": "#ffa000",
        "warn": True,
    },
    "OC 3.0 GHz": {
        "desc": "Max overclock + GPU 1.1 GHz. Not every chip is stable.",
        "governor": "ondemand",
        "max_freq_mhz": 3000,
        "over_voltage_delta": 50000,
        "v3d_freq": 1100,
        "fan": list(FAN_PROFILES["Max"]),
        "color": "#e53935",
        "warn": True,
    },
}

# Raise on every release. An older copy never replaces a newer installed one
# (copies without this line, like the first versions, count as version 0).
VERSION = "2.0.0"

CONFIG_PATHS = ["/boot/firmware/config.txt", "/boot/config.txt"]
BLOCK_START = "# >>> pi5-energyplan (managed by pi5_energyplan.py) >>>"
BLOCK_END = "# <<< pi5-energyplan <<<"
CONFLICT_RE = re.compile(
    r"^\s*(dtparam=fan_temp\d|arm_freq\s*=|over_voltage(_delta)?\s*=|gpu_freq\s*=|"
    r"v3d_freq\s*=)")
THERMAL_ZONE = "/sys/class/thermal/thermal_zone0"
CPU0 = "/sys/devices/system/cpu/cpu0/cpufreq"
# If a kernel lacks a governor, use the closest one that exists.
GOVERNOR_FALLBACK = {"powersave": ["conservative", "ondemand"],
                     "conservative": ["ondemand", "schedutil"],
                     "schedutil": ["ondemand"], "ondemand": ["schedutil"],
                     "performance": ["ondemand"]}
REFRESH_MS = 3000          # GUI update interval
UPS_I2C_ADDR = 0x36        # Geekworm X120x fuel gauge (MAX1704x type)
PLD_GPIO = 6               # X120x power-loss-detect pin: high = adapter OK
CHARGE_GPIO = 16           # X120x charge control: high = charging off, low = on
BATTERY_CFG = "/etc/pi5-energyplan/battery.json"
GUARD_STATUS = "/run/pi5-energyplan/battery-guard.json"
TOPUP_FILE = "/run/pi5-energyplan/charge-full-once"  # in /run: forgotten on reboot
TOPUP_MAX_H = 12           # give up on "charge to 100 % once" after this long
GUARD_SERVICE = "pi5-battery-guard.service"
GUARD_UNIT_PATH = "/etc/systemd/system/" + GUARD_SERVICE
RESUME_GAP = 5             # resume charging this many % below the max limit
VOLT_FLOOR = 3.30          # also shut down below this cell voltage (on battery)
BLIND_MINUTES = 5          # on battery with the fuel gauge silent this long: shut down
SERVICE_NAME = "pi5-energyplan.service"
SERVICE_PATH = "/etc/systemd/system/" + SERVICE_NAME
INSTALLED_SCRIPT = "/usr/local/bin/pi5_energyplan.py"

# ---- Sweet spot auto test ---------------------------------------------------
STATE_DIR = "/var/lib/pi5-energyplan"
AUTOTEST_STATE = STATE_DIR + "/autotest.json"
AUTOTEST_LOG = STATE_DIR + "/autotest.log"
CUSTOM_PLANS = "/etc/pi5-energyplan/custom.json"
SWEET_SPOT = "Sweet Spot"
SWEET_FAN = list(FAN_PROFILES["Strong"])
TEST_FAN = [(30, 2, 220), (40, 2, 255), (50, 2, 255), (55, 2, 255)]
BOOT_DELAY = 45  # seconds after boot before each test (time to cancel / settle)
# (MHz, over_voltage_delta µV), tried in order. For each speed the lowest
# voltage is tried first; if it fails, the next voltage for that speed is tried.
AUTOTEST_STEPS = [(2600, 0), (2600, 25000), (2800, 25000), (2800, 50000), (3000, 50000)]


def load_custom_plans():
    """Add plans saved by the auto test (e.g. 'Sweet Spot') to PLANS."""
    try:
        with open(CUSTOM_PLANS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for name, p in data.items():
        if isinstance(p, dict) and "max_freq_mhz" in p:
            p.setdefault("governor", "ondemand")
            p.setdefault("desc", "Custom plan")
            p.setdefault("color", "#66bb6a")
            p["fan"] = [tuple(x) for x in p.get("fan", SWEET_FAN)]
            PLANS[name] = p


load_custom_plans()

# ---- Governor chosen per plan in the GUI --------------------------------------
GOV_FILE = "/etc/pi5-energyplan/governors.json"
GOV_INFO = {"powersave": "always slowest, least power",
            "conservative": "speeds up slowly, saves power",
            "ondemand": "jumps up fast when busy (Pi default)",
            "schedutil": "follows the load, efficient",
            "performance": "always full speed"}


def available_governors():
    avail = (read(CPU0 + "/scaling_available_governors") or "").split()
    return [g for g in GOV_INFO if g in avail] or list(GOV_INFO)


def load_governor_overrides():
    try:
        with open(GOV_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for name, gov in data.items():
        if name in PLANS and gov in GOV_INFO:
            PLANS[name]["governor"] = gov


FAN_FILE = "/etc/pi5-energyplan/fans.json"


def load_fan_overrides():
    try:
        with open(FAN_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for name, prof in data.items():
        if name in PLANS and prof in FAN_PROFILES:
            PLANS[name]["fan"] = list(FAN_PROFILES[prof])


def reload_plan_settings():
    """Reset plans to built-in governor/fan, then apply the saved choices."""
    for p in PLANS.values():
        p["governor"] = p.get("_default_gov", p["governor"])
        p["fan"] = list(p.get("_default_fan", p["fan"]))
    load_governor_overrides()
    load_fan_overrides()


for _p in PLANS.values():
    _p["_default_gov"] = _p["governor"]
    _p["_default_fan"] = list(_p["fan"])
reload_plan_settings()


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #
def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def find_plan(name):
    """Match plan names loosely: 'oc28', 'OC 2.8', 'maxcool' all work."""
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())
    key = norm(name)
    if not key:
        return None
    for plan in PLANS:
        if norm(plan) == key:
            return plan
    partial = [plan for plan in PLANS if norm(plan).startswith(key)]
    return partial[0] if len(partial) == 1 else None


def config_path():
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            return p
    return CONFIG_PATHS[0]


# --------------------------------------------------------------------------- #
#  Reading status (no root needed)
# --------------------------------------------------------------------------- #
def fan_cooling_device():
    for d in glob.glob("/sys/class/thermal/cooling_device*"):
        if read(d + "/type") == "pwm-fan":
            return d
    return None


def fan_hwmon():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        if read(d + "/name") == "pwmfan":
            return d
    return None


def throttle_bits():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"0x[0-9a-fA-F]+", out)
    return int(m.group(0), 16) if m else None


def gpu_clock_mhz():
    """Current 3D GPU (V3D) clock, from the firmware."""
    try:
        out = subprocess.run(["vcgencmd", "measure_clock", "v3d"], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"=(\d+)", out)
    return round(int(m.group(1)) / 1e6) if m and int(m.group(1)) else None


def cpu_temp_c():
    t = as_int(read(THERMAL_ZONE + "/temp"))
    return t / 1000 if t is not None else None


def throttle_text():
    v = throttle_bits()
    if v is None:
        return None
    now = [label for bit, label in ((0x1, "under-voltage"), (0x2, "freq capped"),
                                    (0x4, "throttled"), (0x8, "soft temp limit")) if v & bit]
    if now:
        return "NOW: " + ", ".join(now)
    if v & 0xF0000:
        return "OK (issues earlier since boot)"
    return "OK"


def saved_plan():
    text = read(config_path(), "")
    m = re.search(re.escape(BLOCK_START) + r"\s*\n# plan: (.+)", text)
    return m.group(1).strip() if m else None


def get_status():
    t = as_int(read(THERMAL_ZONE + "/temp"))
    fd = fan_cooling_device()
    hw = fan_hwmon()
    return {
        "temp": t / 1000 if t is not None else None,
        "fan_level": read(fd + "/cur_state") if fd else None,
        "fan_max": read(fd + "/max_state") if fd else None,
        "fan_pwm": as_int(read(hw + "/pwm1")) if hw else None,
        "fan_rpm": read(hw + "/fan1_input") if hw else None,
        "freq": as_int(read(CPU0 + "/scaling_cur_freq")),
        "max_freq": as_int(read(CPU0 + "/scaling_max_freq")),
        "governor": read(CPU0 + "/scaling_governor"),
        "throttled": throttle_text(),
        "plan": saved_plan(),
    }


def fmt_fan(s):
    parts = []
    if s["fan_level"] is not None:
        parts.append(f"level {s['fan_level']}/{s['fan_max']}")
    if s["fan_pwm"] is not None:
        parts.append(f"{round(s['fan_pwm'] * 100 / 255)}%")
    if s["fan_rpm"]:
        parts.append(f"{s['fan_rpm']} rpm")
    return " · ".join(parts) if parts else "not found"


def fmt_freq(s):
    if s["freq"] is None:
        return "?"
    txt = f"{s['freq'] / 1e6:.2f} GHz"
    if s["max_freq"]:
        txt += f" (max {s['max_freq'] / 1e6:.1f})"
    return txt


# --------------------------------------------------------------------------- #
#  Battery: Geekworm X1200 / X1201 / X1202 UPS
#
#  Everything Geekworm's example scripts do (wiki "X120X Software", scripts from
#  github.com/suptronics/x120x 6bbfaa7) is done here, without their problems:
#   * GPIO: pld.py / merged.py open 'gpiochip4' with the gpiod v1 API, and the
#     -trixie.py versions hard-code 'gpiochip0' with the v2 API, so each one
#     only works on some kernels. Here GPIO 6 is read with `pinctrl` (no chip
#     name needed), with a gpiod v1/v2 fallback that finds the RP1 chip by name.
#   * bat.py shuts down on ONE reading under 3.2 V, even on mains. merged.py
#     shuts down below 20 % even while charging (a boot loop after an outage),
#     exits after one good check (Loop=False), and always shuts down after a
#     ~2 minute power cut (no setting). merged-trixie.py and
#     pld*.py have their shutdown commented out, and merged-trixie.py turns an
#     I2C error into "0 %" and hides crashes with `except: pass`.
#     Here: shutdown only on battery, after 3 readings in a row, with a warning
#     and cancelled when power returns; a power cut has its own timer (15 s to 1 h).
#   * qtx120x.py (Geekworm copy) re-runs `shutdown +5` every 30 s below 15 %, which
#     keeps pushing the shutdown back so it never happens. Its charge control
#     stops at a fixed 90 % with no gap, so it can switch on and off every 30 s,
#     and it makes a new GPIO device each time. Here the limit is yours, with
#     a 5 % gap, and the pin is checked to really pause / resume charging.
#   * If the fuel gauge stops answering, the scripts crash or read 0. Here GPIO 6
#     keeps working: the outage timer still runs, and on battery with no
#     reading for 5 minutes the Pi shuts down safely.
#   * Battery % isn't clamped (can read 103 %); "== 100" checks on a float.
#   * None of them can show time left: the board's fuel gauge has no current
#     sensor. Here the time is estimated from how fast the % changes.
# --------------------------------------------------------------------------- #
_pld_line = None


def read_pld():
    """Power-loss-detect pin. 1 = adapter OK, 0 = running on battery, None = unknown."""
    if shutil.which("pinctrl"):
        try:
            out = subprocess.run(["pinctrl", "get", str(PLD_GPIO)], capture_output=True,
                                 text=True, timeout=2).stdout
            m = re.search(r"\|\s*(hi|lo)\b", out)
            if m:
                return 1 if m.group(1) == "hi" else 0
        except (OSError, subprocess.SubprocessError):
            pass
    return _read_pld_gpiod()


def _read_pld_gpiod():
    global _pld_line
    try:
        import gpiod
    except ImportError:
        return None
    try:
        if _pld_line is None:
            for path in sorted(glob.glob("/dev/gpiochip*")):
                if hasattr(gpiod, "request_lines"):  # libgpiod v2
                    with gpiod.Chip(path) as chip:
                        if "rp1" not in chip.get_info().label:
                            continue
                    from gpiod.line import Direction
                    req = gpiod.request_lines(path, consumer="pi5-energyplan", config={
                        PLD_GPIO: gpiod.LineSettings(direction=Direction.INPUT)})
                    _pld_line = ("v2", req)
                else:  # libgpiod v1
                    chip = gpiod.Chip(path)
                    if "rp1" not in chip.label():
                        chip.close()
                        continue
                    line = chip.get_line(PLD_GPIO)
                    line.request(consumer="pi5-energyplan", type=gpiod.LINE_REQ_DIR_IN)
                    _pld_line = ("v1", line)
                break
        if _pld_line is None:
            return None
        kind, obj = _pld_line
        if kind == "v2":
            from gpiod.line import Value
            return 1 if obj.get_value(PLD_GPIO) == Value.ACTIVE else 0
        return obj.get_value()
    except Exception:  # noqa: BLE001
        _pld_line = None
        return None


def fmt_hours(h):
    if h > 48:
        return "more than 48 h"
    mins = max(1, round(h * 60))
    return f"{mins} min" if mins < 60 else f"{mins // 60} h {mins % 60:02d} min"


class Battery:
    """Reads the X120x fuel gauge and estimates time left / time to full."""
    WINDOW = 300      # seconds of history used for the rate estimate
    MIN_SPAN = 90     # need this much history before showing a time
    DEADBAND = 0.3    # %/h treated as "not changing"

    def __init__(self):
        self.bus = None
        self.err = None
        self.hist = deque()
        self.last_ac = "unset"
        self.has_crate = False

    def _word(self, reg):
        w = self.bus.read_word_data(UPS_I2C_ADDR, reg)
        return ((w & 0xFF) << 8) | (w >> 8)  # chip sends MSB first

    def _connect(self):
        if self.bus:
            return True
        if not os.path.exists("/dev/i2c-1"):
            self.err = "I2C is off: sudo raspi-config nonint do_i2c 0, then reboot"
            return False
        mod = None
        for name in ("smbus2", "smbus"):
            try:
                mod = __import__(name)
                break
            except ImportError:
                pass
        if mod is None:
            self.err = "Missing I2C library: sudo apt install python3-smbus"
            return False
        try:
            self.bus = mod.SMBus(1)
        except OSError as e:
            self.err = f"Can't open I2C: {e}"
            return False
        try:  # MAX17048/49 report their own charge rate (CRATE register)
            self.has_crate = (self._word(0x08) & 0xFFF0) == 0x0010
        except OSError:
            self.has_crate = False
        return True

    def read(self):
        if not self._connect():
            return {"ok": False, "error": self.err}
        try:
            volt = self._word(0x02) * 1.25 / 16 / 1000
            soc_raw = self._word(0x04) / 256
            crate = None
            if self.has_crate:
                c = self._word(0x16)
                crate = (c - 65536 if c & 0x8000 else c) * 0.208  # %/h
                if abs(crate) > 200:
                    crate = None
        except OSError:
            try:
                self.bus.close()
            except Exception:  # noqa: BLE001
                pass
            self.bus = None
            return {"ok": False, "error": "UPS not answering on I2C 0x36 (is the X1202 seated?)"}

        ac = read_pld()
        now = time.monotonic()
        if ac != self.last_ac:  # plugged / unplugged: old trend no longer valid
            self.hist.clear()
            self.last_ac = ac
        self.hist.append((now, soc_raw))
        while self.hist and now - self.hist[0][0] > self.WINDOW:
            self.hist.popleft()

        rate = self._slope()
        if rate is None and crate is not None and abs(crate) > self.DEADBAND:
            rate = crate
        soc = min(max(soc_raw, 0.0), 100.0)
        info = {"ok": True, "soc": soc, "volt": volt, "ac": ac, "rate": rate}
        info.update(self._describe(soc, ac, rate))
        self._apply_guard(info, read_guard_status())
        return info

    @staticmethod
    def _apply_guard(info, g):
        """Adjust the text when the battery guard is limiting charge or shutting down."""
        if g and g.get("saver_active"):
            info["state"] += f" · saver on ({g['saver_active']})"
        if not g or not g.get("enabled"):
            return
        soc, rate = info["soc"], info["rate"]
        if g.get("shutdown_pending"):
            info["eta"] = f"Shutting down: {g.get('shutdown_reason', 'battery guard')}"
        elif info["on_battery"] and rate is not None and rate < -Battery.DEADBAND:
            left = max(0.0, soc - g["min"]) / -rate
            info["eta"] = f"≈ {fmt_hours(left)} until safe shutdown at {g['min']}%"
        elif not info["on_battery"] and g.get("charging_allowed") is False \
                and g.get("charge_check") != "unsupported":
            info["state"], info["charging"] = f"Charge limit {g['max']}% reached", False
            info["eta"] = f"Charging paused (resumes below {g['max'] - RESUME_GAP}%)"
            if rate is not None and rate > Battery.DEADBAND:
                info["eta"] += " · the % can creep up a little after a pause: that's the " \
                               "gauge catching up, not charging"
        elif info["charging"] and rate:
            info["eta"] = f"{g['max']}% limit in ≈ {fmt_hours(max(0.0, g['max'] - soc) / rate)}"

    def _slope(self):
        """Least-squares slope of battery % over the history window, in %/hour."""
        n = len(self.hist)
        if n < 20 or self.hist[-1][0] - self.hist[0][0] < self.MIN_SPAN:
            return None
        t0 = self.hist[0][0]
        xs = [t - t0 for t, _ in self.hist]
        ys = [s for _, s in self.hist]
        mx, my = sum(xs) / n, sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if not den:
            return None
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den * 3600

    def _describe(self, soc, ac, rate):
        d = self.DEADBAND
        on_battery = ac == 0 or (ac is None and rate is not None and rate < -d)
        charging = False
        if on_battery:
            state = "On battery"
            eta = (f"≈ {fmt_hours(soc / -rate)} left" if rate is not None and rate < -d
                   else "Time left: estimating…")
        elif soc >= 99.5:
            state, eta = "Full", ""
        elif rate is None:
            state, eta = "Plugged in", "Estimating…"
        elif rate > d:
            state, charging = "Charging", True
            eta = f"Full in ≈ {fmt_hours((100 - soc) / rate)}"
        elif rate < -d:
            state = "Plugged in but draining"
            eta = f"Adapter too weak? ≈ {fmt_hours(soc / -rate)} left"
        else:
            state, eta = "Plugged in (not charging)", ""
        return {"state": state, "eta": eta, "charging": charging, "on_battery": on_battery}


def battery_line(b):
    if not b["ok"]:
        return b["error"]
    line = f"{b['soc']:.1f}% · {b['state']} · {b['volt']:.2f} V"
    return line + (f" · {b['eta']}" if b["eta"] else "")


def battery_color(soc):
    return "#66bb6a" if soc >= 50 else "#ffa726" if soc >= 20 else "#ef5350"


def watch_battery():
    bat = Battery()
    try:
        while True:
            print(time.strftime("%H:%M:%S  ") + battery_line(bat.read()), flush=True)
            time.sleep(REFRESH_MS / 1000)
    except KeyboardInterrupt:
        pass


def battery_health(volt):
    """Same voltage levels as Geekworm/Suptronics merged.py."""
    if volt >= 3.87:
        return "Full"
    if volt >= 3.7:
        return "High"
    if volt >= 3.55:
        return "Medium"
    if volt >= 3.4:
        return "Low"
    return "Critical"


def read_pmic():
    """Pi 5 power readings (like qtx120x.py): input voltage and total Pi watts."""
    try:
        out = subprocess.run(["vcgencmd", "pmic_read_adc"], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    vals = {(n, u): float(v) for n, u, v in
            re.findall(r"(\w+)_([AV])\s+\w+\(\d+\)=([\d.]+)", out)}
    watts = sum(a * vals[(n, "V")] for (n, u), a in vals.items()
                if u == "A" and (n, "V") in vals)
    return {"ext5v": vals.get(("EXT5V", "V")), "watts": watts or None,
            "core_v": vals.get(("VDD_CORE", "V")), "core_a": vals.get(("VDD_CORE", "A"))}


# --------------------------------------------------------------------------- #
#  Battery guard (root service): charge limit + safe shutdown
#  Settings come from the GUI's "UPS settings" window, saved in BATTERY_CFG.
# --------------------------------------------------------------------------- #
DEFAULT_BATTERY_CFG = {"enabled": False, "max": 80, "min": 20,
                       "outage_minutes": 0, "warn_minutes": 1,
                       "saver_enabled": False, "saver_below": 50, "saver_plan": "Underclock",
                       "usb_5v_when_off": False}
SAVER_PLANS = [("Underclock", "Underclock · 1.5 GHz · powersave"),
               ("Eco", "Eco · 1.8 GHz · conservative")]
# 0.25 = 15 s: "shut down when the power goes", with a short debounce for glitches
OUTAGE_CHOICES = [(0, "Never"), (0.25, "15 seconds"), (1, "1 minute"),
                  (5, "5 minutes"), (15, "15 minutes"), (30, "30 minutes"), (60, "1 hour")]
WARN_CHOICES = [(1, "1 minute"), (2, "2 minutes"), (5, "5 minutes")]


def load_battery_cfg():
    cfg = dict(DEFAULT_BATTERY_CFG)
    try:
        with open(BATTERY_CFG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in cfg})
    except (OSError, ValueError):
        pass
    return cfg


def validate_battery_cfg(cfg):
    c = dict(DEFAULT_BATTERY_CFG)
    c.update(cfg)
    c["enabled"] = bool(c["enabled"])
    c["max"] = int(min(100, max(50, c["max"])))
    c["min"] = int(min(50, max(5, c["min"])))
    if c["max"] - c["min"] < 20:
        raise ValueError("Keep at least 20 % between the low and high limits")
    if c["outage_minutes"] not in [m for m, _ in OUTAGE_CHOICES]:
        raise ValueError("Unknown outage time")
    if c["warn_minutes"] not in [m for m, _ in WARN_CHOICES]:
        raise ValueError("Unknown warning time")
    c["saver_enabled"] = bool(c["saver_enabled"])
    c["saver_below"] = int(min(100, max(15, c["saver_below"])))
    if c["saver_plan"] not in [p for p, _ in SAVER_PLANS]:
        raise ValueError("Unknown battery saver plan")
    c["usb_5v_when_off"] = bool(c["usb_5v_when_off"])
    return c


def set_plan_fan(name, profile):
    """Save the fan profile for one plan; apply it now if that plan is active."""
    plan = find_plan(name)
    if not plan:
        raise ValueError(f"Unknown plan '{name}'")
    if profile not in FAN_PROFILES:
        raise ValueError(f"Unknown fan profile '{profile}'. Options: {', '.join(FAN_PROFILES)}")
    try:
        with open(FAN_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[plan] = profile
    write_json_atomic(FAN_FILE, data)
    PLANS[plan]["fan"] = list(FAN_PROFILES[profile])
    msg = f"{plan} will use the {profile} fan profile"
    if find_plan(saved_plan() or "") == plan:
        live = set_trip_points(PLANS[plan]["fan"])
        write_config(plan)
        msg += (" (temperatures applied now, " if live else " (") + "fan speeds after reboot)"
    subprocess.run(["systemctl", "try-restart", GUARD_SERVICE], capture_output=True)
    sync_installed_copy()
    return msg


def sync_installed_copy():
    """Keep the system copy used by the boot service and battery guard up to date."""
    return os.path.exists(INSTALLED_SCRIPT) and install_copy()


def version_of(path):
    """(2, 0, 0) from a copy's VERSION line; (0,) for copies without one."""
    m = re.search(r'^VERSION = "([\d.]+)"', read(path, "") or "", re.M)
    return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)


def installed_is_newer():
    me = os.path.abspath(__file__)
    return me != INSTALLED_SCRIPT and os.path.exists(INSTALLED_SCRIPT) and \
        version_of(INSTALLED_SCRIPT) > version_of(me)


def install_copy():
    """Copy this file to INSTALLED_SCRIPT, the copy the services run. Never replaces
    a newer version with an older one. Returns True when it copied."""
    me = os.path.abspath(__file__)
    if me == INSTALLED_SCRIPT or file_sha(me) == file_sha(INSTALLED_SCRIPT) or \
            installed_is_newer():
        return False
    shutil.copy2(me, INSTALLED_SCRIPT)
    os.chmod(INSTALLED_SCRIPT, 0o755)
    return True


def file_sha(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def set_plan_governor(name, gov):
    """Save the governor for one plan; apply it now if that plan is active."""
    plan = find_plan(name)
    if not plan:
        raise ValueError(f"Unknown plan '{name}'")
    if gov not in GOV_INFO:
        raise ValueError(f"Unknown governor '{gov}'. Options: {', '.join(GOV_INFO)}")
    try:
        with open(GOV_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[plan] = gov
    write_json_atomic(GOV_FILE, data)
    PLANS[plan]["governor"] = gov
    msg = f"{plan} will use the {gov} governor"
    if find_plan(saved_plan() or "") == plan:
        apply_live(plan)
        msg += " (applied now)"
    # the battery guard keeps plans in memory: restart it so the saver uses this too
    subprocess.run(["systemctl", "try-restart", GUARD_SERVICE], capture_output=True)
    sync_installed_copy()
    return msg


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_guard_status():
    """Latest status from the running guard, or None if it isn't running."""
    try:
        with open(GUARD_STATUS) as f:
            g = json.load(f)
    except (OSError, ValueError):
        return None
    return g if time.time() - g.get("updated", 0) < 30 else None


CHARGE_PIN_FILE = "/etc/pi5-energyplan/charge_pin_v3.json"
# What older versions learned, forgotten on update: v1 had a charging bug, and v2
# could mistake the fuel gauge catching up after a pause for "pause not working".
OLD_CHARGE_PIN_FILES = ("/etc/pi5-energyplan/charge_pin.json",
                        "/etc/pi5-energyplan/charge_pin_v2.json")
VOLT_MAX = 4.25  # a full 18650 sits at ~4.2 V; the X1202 charger stops at 4.23 V


class ChargeCheck:
    """Controls the X120x charge pin and checks it really works.

    Rule #1: when the battery should charge, it must charge. "Charging on" uses
    the Pi's own default pin state (input, pull-down), the same state the board
    charges with when no script is running (and what Geekworm's script uses).

    * PAUSED check (on mains, below 98 %): the battery must not keep charging. Only
      when the % rises 1 % AND the cell voltage rises too, the "off" level isn't
      working: try the opposite level once, then give up (pin released, board
      charges normally). The % alone isn't enough: the fuel gauge estimates it from
      the voltage with a lag, so it often creeps up 1-2 % after charging has
      stopped, while the voltage falls (no current flows into the cells).
    * ON check (on mains, below 95 %): if the % stays flat for 20 min, try the
      other pin state for 20 min as a PROBE. If that charges, use it from now
      on; if not, go back. A probe is never saved unless it made the battery
      charge, so a weak adapter can't leave charging switched off.
    Nothing is "verified" at 98 % or more, where the % can't rise anyway."""
    WINDOW = 20 * 60
    PAUSE_RISE, ON_RISE = 1.0, 0.5
    PAUSE_VRISE = 0.015   # V above the lowest reading since the pause = really charging

    def __init__(self, log=print):
        self.log = log
        try:
            with open(CHARGE_PIN_FILE) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.off_level = d.get("off_level", "high")  # Geekworm: high = charging off
        self.status = d.get("status", "unknown")       # unknown / ok / unsupported
        self.tried_flip = d.get("tried_flip", False)
        self.note = d.get("note", "")
        self.warn = ""          # shown in the GUI (e.g. adapter too weak)
        self.probe = None       # start time while testing the other "on" state
        self.no_probe_until = 0  # after a failed probe, wait before trying again
        self.base = None

    def save(self):
        write_json_atomic(CHARGE_PIN_FILE, {"off_level": self.off_level,
                                            "status": self.status,
                                            "tried_flip": self.tried_flip,
                                            "note": self.note})

    def _on_args(self, off_level):
        # "on" = the level opposite to "off". For the normal board that is low,
        # done the gentle way: input with pull-down = Pi default = board charges.
        return ["ip", "pd"] if off_level == "high" else ["op", "dh"]

    def pin_args(self, enabled):
        if self.status == "unsupported":
            return ["ip", "pd"]
        if enabled:
            lvl = self.off_level
            if self.probe is not None:
                lvl = "low" if lvl == "high" else "high"
            return self._on_args(lvl)
        return ["op", "dh"] if self.off_level == "high" else ["op", "dl"]

    def observe(self, charging, ac, soc, now, volt=None):
        """Feed one reading. Returns True when the pin must be set again."""
        if ac != 1 or charging is None or self.status == "unsupported":
            self.base = None
            if self.probe is not None:          # left mains during a probe: undo it
                self.probe = None
                return True
            return False
        mode = "on" if charging else "paused"
        if mode == "paused" and self.probe is not None:
            self.probe = None
        top = 98 if mode == "paused" else 95
        if soc >= top:
            self.base = None
            return False
        if not self.base or self.base["mode"] != mode:
            self.base = {"mode": mode, "t0": now, "s0": soc, "smin": soc, "vmin": volt}
            return False
        b = self.base
        b["smin"] = min(b["smin"], soc)
        if volt is not None:
            b["vmin"] = volt if b["vmin"] is None else min(b["vmin"], volt)
        if mode == "paused":
            return self._paused(b, soc, now, volt)
        return self._on(b, soc, now)

    def _paused(self, b, soc, now, volt=None):
        volt_up = volt is None or b["vmin"] is None or volt - b["vmin"] >= self.PAUSE_VRISE
        if soc - b["smin"] >= self.PAUSE_RISE and volt_up:
            self.base = None
            self.log(f"Charge pause not working: battery rose {b['smin']:.1f}% → {soc:.1f}%"
                     + (f", {b['vmin']:.3f} → {volt:.3f} V" if volt is not None and
                        b["vmin"] is not None else ""))
            if not self.tried_flip:
                self.off_level = "low" if self.off_level == "high" else "high"
                self.tried_flip, self.status = True, "unknown"
                self.note = "Trying the opposite pin setting to pause charging"
            else:
                self.status = "unsupported"
                self.note = ("This board can't pause charging, so the max limit has no "
                             "effect. Charging is left to the board.")
            self.log(self.note)
            self.save()
            return True
        if now - b["t0"] >= self.WINDOW and self.status != "ok":
            self.status, self.note = "ok", "Verified: charging pauses at your limit"
            self.log("Charge pause verified")
            self.save()
            self.base = None
        return False

    def _on(self, b, soc, now):
        if soc - b["s0"] >= self.ON_RISE:                  # it charges
            if self.probe is not None:                     # ...thanks to the probe
                self.off_level = "low" if self.off_level == "high" else "high"
                self.probe, self.status = None, "unknown"
                self.note = "Charging needed the opposite pin setting; using it now"
                self.log(self.note)
                self.save()
            self.warn, self.base, self.no_probe_until = "", None, 0
            return False
        if soc < b["s0"] - 0.5:                            # draining on mains
            self.warn = ("Plugged in but the battery is draining: the adapter can't "
                         "power the Pi and charge at the same time.")
            self.base = None
            if self.probe is not None:
                self.probe = None
                return True
            return False
        if now - b["t0"] < self.WINDOW:
            return False
        self.base = None
        if self.probe is None and now < self.no_probe_until:
            return False  # already know the pin isn't the cause; keep the warning
        if self.probe is None:
            self.probe = now
            self.log(f"Battery not charging at {soc:.0f}% for {self.WINDOW // 60} min: "
                     "trying the other pin setting for a while")
            return True
        self.probe = None
        self.no_probe_until = now + 6 * 3600
        self.warn = ("Plugged in but the battery isn't charging. The charge pin isn't "
                     "the cause: check the adapter and that the cells are seated.")
        self.log("Probe didn't help either: back to the normal setting. " + self.warn)
        return True


_charge_check = None


def set_charging(enabled):
    """Drive the X120x charge-control pin. Returns True on success."""
    if not shutil.which("pinctrl"):
        print("pinctrl not found (sudo apt install raspi-utils)", flush=True)
        return False
    args = _charge_check.pin_args(enabled) if _charge_check else \
        ["op", "dl" if enabled else "dh"]
    r = subprocess.run(["pinctrl", "set", str(CHARGE_GPIO)] + args,
                       capture_output=True, timeout=3)
    return r.returncode == 0


def battery_guard():
    def log(msg):
        print(time.strftime("%H:%M:%S  ") + msg, flush=True)

    global _charge_check
    _charge_check = ChargeCheck(log)
    bat = Battery()
    charging, low_count, pending, outage_since = None, 0, None, None
    blind_since, gauge_ok, ac = None, True, None
    saver = None  # name of the saver plan while it's active
    volt_high = False
    tick = 0
    for old in OLD_CHARGE_PIN_FILES:  # forget what the older, faulty checks learned
        try:
            os.remove(old)
            log(f"Removed charge-pin data learned by an older version: {old}")
        except OSError:
            pass
    set_charging(True)  # start from a charging state; the limit logic decides next
    if os.path.exists("/run/systemd/shutdown/scheduled"):  # survived a restart
        pending = {"reason": "shutdown already scheduled", "at": None}

    def restore_plan():
        name = find_plan(saved_plan() or "") or "Balanced"
        apply_live(name)
        return name

    log("Battery guard started")
    try:
        while True:
            cfg = load_battery_cfg()
            b = bat.read()
            now = time.time()

            # Battery saver: switch to a low-power plan on battery below the limit;
            # stay in it until the power comes back (no flip-flopping).
            if b["ok"]:
                want_saver = (cfg["saver_enabled"] and b["ac"] == 0 and
                              b["soc"] <= cfg["saver_below"])
                if want_saver and not saver:
                    apply_live(cfg["saver_plan"])
                    saver = cfg["saver_plan"]
                    log(f"Battery saver ON at {b['soc']:.0f}%: switched to {saver}")
                elif saver and (b["ac"] == 1 or not cfg["saver_enabled"]):
                    log(f"Battery saver OFF: back to {restore_plan()}")
                    saver = None

            # Overcharge watch (always on): a full cell should never pass ~4.23 V
            if b["ok"]:
                if b["volt"] > VOLT_MAX and not volt_high:
                    log(f"WARNING: battery at {b['volt']:.2f} V: charger is not stopping")
                volt_high = b["ok"] and b["volt"] > VOLT_MAX

            # "Charge to 100 % once": lift the limit until the battery is full
            topup = as_int(read(TOPUP_FILE))
            if topup is not None and (not cfg["enabled"] or cfg["max"] >= 100 or
                                      (b["ok"] and b["soc"] >= 99.5) or
                                      now - topup > TOPUP_MAX_H * 3600):
                if cfg["enabled"] and cfg["max"] < 100:
                    log(f"Charge to 100 % once: done, back to the {cfg['max']}% limit")
                try:
                    os.remove(TOPUP_FILE)
                except OSError:
                    pass
                topup = None
            limit = 100 if topup is not None else cfg["max"]

            # GPIO 6 still works when the fuel gauge doesn't answer on I2C
            ac = b["ac"] if b["ok"] else read_pld()
            soc, volt = (b["soc"], b["volt"]) if b["ok"] else (None, None)
            if b["ok"] != gauge_ok:
                log("UPS fuel gauge answering again" if b["ok"] else
                    f"UPS fuel gauge not answering: {b['error']}")
                gauge_ok = b["ok"]

            if not cfg["enabled"]:
                tick += 1
                if (charging is not True or tick % 10 == 0) and set_charging(True):
                    if charging is False:
                        log("Protection off: charging allowed again")
                    charging = True

            if cfg["enabled"] and b["ok"]:
                # 1) charge limit with a gap so it doesn't toggle constantly
                if limit >= 100 or soc <= limit - RESUME_GAP:
                    want = True
                elif soc >= limit:
                    want = False
                else:
                    want = True if charging is None else charging
                if want != charging and set_charging(want):
                    charging = want
                    log(f"Charging {'ON' if want else 'PAUSED'} at {soc:.0f}% "
                        f"(limit {limit}%)")

                # 1b) check that charging really stops / starts; fix the pin if not.
                #     Also set the pin again every 30 s, in case anything else
                #     (another UPS script, a crash) changed it.
                reassert = _charge_check.observe(charging, ac, soc, now, volt)
                tick += 1
                if charging is not None and (reassert or tick % 10 == 0):
                    set_charging(charging)

            if cfg["enabled"]:
                # 2) shutdown conditions (only when really on battery)
                on_batt = ac == 0
                outage_since = (outage_since or now) if on_batt else None
                # on battery with no battery reading: can't see the level, so after a
                # while shut down safely rather than risk the power just being cut
                blind_since = (blind_since or now) if on_batt and not b["ok"] else None
                low = b["ok"] and ((on_batt and (soc <= cfg["min"] or volt < VOLT_FLOOR)) or
                                   (ac is None and volt < VOLT_FLOOR))
                low_count = low_count + 1 if low else 0   # 3 readings in a row
                reason = None
                if low_count >= 3:
                    reason = (f"battery at {soc:.0f}% (limit {cfg['min']}%)"
                              if soc <= cfg["min"] else f"battery voltage low ({volt:.2f} V)")
                elif on_batt and cfg["outage_minutes"] and \
                        now - outage_since >= cfg["outage_minutes"] * 60:
                    reason = f"power out for {dict(OUTAGE_CHOICES)[cfg['outage_minutes']]}"
                elif blind_since and now - blind_since >= BLIND_MINUTES * 60:
                    reason = "on battery and the battery level can't be read (I2C)"

                if reason and not pending:
                    warn = cfg["warn_minutes"]
                    subprocess.run(["shutdown", "-h", f"+{warn}",
                                    f"Battery guard: {reason}. Plug in power to cancel."])
                    pending = {"reason": reason, "at": now + warn * 60}
                    log(f"Shutdown in {warn} min: {reason}")
                elif pending and ac == 1:
                    subprocess.run(["shutdown", "-c", "Power restored: shutdown cancelled"])
                    log("Power restored: shutdown cancelled")
                    pending, low_count = None, 0

            write_json_atomic(GUARD_STATUS, {
                "updated": now, "enabled": cfg["enabled"], "min": cfg["min"],
                "max": limit, "limit": cfg["max"], "topup": topup is not None,
                "outage_minutes": cfg["outage_minutes"],
                "charging_allowed": charging, "on_battery_since": outage_since,
                "shutdown_pending": bool(pending),
                "shutdown_reason": pending["reason"] if pending else None,
                "shutdown_at": pending["at"] if pending else None,
                "saver_enabled": cfg["saver_enabled"], "saver_below": cfg["saver_below"],
                "saver_active": saver,
                "charge_check": _charge_check.status, "charge_note": _charge_check.note,
                "charge_warn": _charge_check.warn,
                "charge_probe": _charge_check.probe is not None,
                "volt_high": volt_high, "volt": b.get("volt"), "gauge_ok": b["ok"],
                "ac": ac})
            time.sleep(REFRESH_MS / 1000)
    finally:
        _charge_check.probe = None
        set_charging(True)  # never leave charging switched off when the guard stops
        if saver:
            restore_plan()


GUARD_UNIT = f"""[Unit]
Description=Battery guard for Geekworm X120x UPS (charge limit and safe shutdown)
After=sysinit.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {INSTALLED_SCRIPT} --battery-guard
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def apply_ups_config(new):
    old = load_battery_cfg()
    cfg = validate_battery_cfg({**old, **new})   # keys left out keep their value
    write_json_atomic(BATTERY_CFG, cfg)
    halt = ""
    # only touch the bootloader when this choice really changed (the window always
    # sends it, and someone who skipped the bootloader settings shouldn't get them)
    if "usb_5v_when_off" in new and cfg["usb_5v_when_off"] != old["usb_5v_when_off"]:
        try:
            halt = " " + eeprom_setup(halt_only=True)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            halt = f" (5 V setting not saved: {e})"
    install_copy()
    if read(GUARD_UNIT_PATH) != GUARD_UNIT.strip():
        write(GUARD_UNIT_PATH, GUARD_UNIT)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", GUARD_SERVICE], capture_output=True, check=True)
    subprocess.run(["systemctl", "restart", GUARD_SERVICE], check=True)
    saver = ("" if not cfg["saver_enabled"] else
             f" Battery saver ({cfg['saver_plan']}) "
             + ("when power goes out." if cfg["saver_below"] >= 100
                else f"below {cfg['saver_below']}% on battery."))
    if not cfg["enabled"]:
        return "Battery protection is off (charging always on, no auto shutdown)." + saver \
            + halt
    outage = dict(OUTAGE_CHOICES)[cfg["outage_minutes"]]
    return (f"Saved: charge to {cfg['max']}%, safe shutdown at {cfg['min']}%"
            + ("" if not cfg["outage_minutes"] else f", or after {outage} without power")
            + "." + saver + halt)


def set_topup(on):
    """Start or stop "charge to 100 % once" (needs root; the guard does the work)."""
    cfg = load_battery_cfg()
    if not on:
        try:
            os.remove(TOPUP_FILE)
        except OSError:
            pass
        return f"Back to the normal {cfg['max']}% charge limit."
    if not cfg["enabled"] or cfg["max"] >= 100:
        return "There is no charge limit set, so the battery already charges to 100 %."
    if subprocess.run(["systemctl", "is-active", "--quiet", GUARD_SERVICE]).returncode:
        raise RuntimeError("the battery guard isn't running (save UPS settings first)")
    os.makedirs(os.path.dirname(TOPUP_FILE), exist_ok=True)
    write(TOPUP_FILE, int(time.time()))
    return (f"Charging to 100 % once. The {cfg['max']}% limit comes back when the battery "
            f"is full (or after {TOPUP_MAX_H} h, or a reboot).")


# Geekworm's X120x setup (wiki "Configure EEPROM"): tell the firmware the UPS can
# supply 5 A (otherwise USB is limited to 600 mA), and POWER_OFF_ON_HALT, which
# decides the Pi's 5 V outputs (USB, GPIO 5 V) once it is shut down:
#   1 = switched off (saves battery; Geekworm's setting), 0 = stay on.
# The choice is "usb_5v_when_off" in the UPS settings.
EEPROM_SETTINGS = {"POWER_OFF_ON_HALT": "1", "PSU_MAX_CURRENT": "5000"}
EEPROM_BACKUP = STATE_DIR + "/eeprom-before.conf"


def eeprom_wanted(halt_only=False):
    halt = "0" if load_battery_cfg()["usb_5v_when_off"] else "1"
    if halt_only:
        return {"POWER_OFF_ON_HALT": halt}
    return dict(EEPROM_SETTINGS, POWER_OFF_ON_HALT=halt)


def eeprom_with_settings(text, settings=None):
    """Return the bootloader config with `settings` set in its [all] part.
    Every other line is kept as it is."""
    settings = settings or EEPROM_SETTINGS
    out, seen, section, insert_at = [], set(), "all", None
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            section = st[1:-1].strip().lower()
            if section != "all" and insert_at is None:
                insert_at = len(out)   # new keys go before the first filter section
        key = st.split("=", 1)[0].strip() if "=" in st and not st.startswith("#") else None
        if key in settings and section == "all":
            if key in seen:
                continue               # a duplicate: the first one is set below
            seen.add(key)
            line = f"{key}={settings[key]}"
        out.append(line)
    missing = [f"{k}={v}" for k, v in settings.items() if k not in seen]
    if insert_at is None:
        insert_at = len(out)
    while insert_at and not out[insert_at - 1].strip():
        insert_at -= 1                 # right after the last setting, not after a gap
    out[insert_at:insert_at] = missing
    if not any(line.strip().startswith("[") for line in out):
        out.insert(0, "[all]")
    return "\n".join(out).strip("\n") + "\n"


def eeprom_setup(halt_only=False):
    """Apply the bootloader settings (needs root; takes effect after a reboot).
    Writes nothing when they are already set."""
    if not is_pi5():
        raise RuntimeError("these bootloader settings are for the Raspberry Pi 5")
    if not shutil.which("rpi-eeprom-config"):
        raise RuntimeError("rpi-eeprom-config not found: sudo apt install rpi-eeprom")
    cur = subprocess.run(["rpi-eeprom-config"], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    if not cur.strip():
        raise RuntimeError("couldn't read the current bootloader settings")
    settings = eeprom_wanted(halt_only)
    new = eeprom_with_settings(cur, settings)
    wanted = ", ".join(f"{k}={v}" for k, v in settings.items())
    if new.strip() == cur.strip():
        return f"Bootloader already set: {wanted}"
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(EEPROM_BACKUP):
        write(EEPROM_BACKUP, cur)
    tmp = STATE_DIR + "/eeprom-new.conf"
    write(tmp, new)
    r = subprocess.run(["rpi-eeprom-config", "--apply", tmp], capture_output=True,
                       text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("rpi-eeprom-config --apply failed: " +
                           (r.stderr or r.stdout).strip()[-300:])
    return (f"Bootloader settings saved ({wanted}); they take effect after a reboot. "
            f"Old settings: {EEPROM_BACKUP}")


def single_instance(name):
    """Return False if another copy (same name) is already running."""
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind("\0" + name)
    except OSError:
        return False
    globals().setdefault("_instance_socks", []).append(s)
    return True


# --------------------------------------------------------------------------- #
#  Panel (taskbar) battery icon
#  wf-panel-pi shows StatusNotifierItem tray icons. We publish one over D-Bus:
#  the icon is the battery % with a level bar (⚡ when charging), the tooltip
#  has the full details, and a left click opens the Energy Plan window.
# --------------------------------------------------------------------------- #
SNI_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewToolTip"/>
</interface></node>"""
ICON_SIZE = 64
AUTOSTART = os.path.expanduser("~/.config/autostart/pi5-battery-tray.desktop")


def battery_icon_svg(b):
    s = ICON_SIZE
    if not b["ok"]:
        text, soc, col, bolt = "?", 0, "#9aa0a6", False
    else:
        soc = b["soc"]
        text, col, bolt = f"{soc:.0f}", battery_color(soc), b["charging"]
    fs, ls = (46, 0) if len(text) < 3 else (34, -3)
    bolt_svg = ('<path d="M52 0 L44 16 H50 L46 30 L60 11 H53 L58 0 Z" fill="#ffd54f" '
                'stroke="#000" stroke-width="1.5"/>' if bolt else "")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}" viewBox="0 0 64 64">
<text x="32" y="46" font-family="DejaVu Sans" font-weight="bold" letter-spacing="{ls}"
 font-size="{fs}" text-anchor="middle" fill="#ffffff" stroke="#000000" stroke-width="2.5"
 paint-order="stroke" stroke-linejoin="round">{text}</text>
{bolt_svg}
<rect x="3" y="50" width="58" height="12" rx="3" fill="#000000" fill-opacity="0.55"
 stroke="#ffffff" stroke-width="2"/>
<rect x="6" y="53" width="{max(0.0, 52 * soc / 100):.1f}" height="6" rx="1" fill="{col}"/>
</svg>"""


def render_argb(svg):
    """SVG -> ARGB32 bytes (big-endian), the pixel format tray icons use."""
    from gi.repository import GdkPixbuf
    s = ICON_SIZE
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.write(svg.encode())
        loader.close()
        pb = loader.get_pixbuf()
        if pb.get_width() != s or pb.get_height() != s:
            pb = pb.scale_simple(s, s, GdkPixbuf.InterpType.BILINEAR)
        if not pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0)
    except Exception:  # noqa: BLE001  (no SVG support: plain level bar)
        m = re.search(r'width="([\d.]+)" height="6" rx="1" fill="#(\w{6})"', svg)
        w = int(float(m.group(1)) * s / 64) if m else 0
        r, g, bl = (int(m.group(2)[i:i + 2], 16) for i in (0, 2, 4)) if m else (150,) * 3
        out = bytearray(s * s * 4)
        for y in range(s // 3, s - s // 3):
            for x in range(4, 4 + w):
                o = (y * s + x) * 4
                out[o:o + 4] = bytes((255, r, g, bl))
        return bytes(out)

    w, h, rs = pb.get_width(), pb.get_height(), pb.get_rowstride()
    px = pb.get_pixels()
    out = bytearray(w * h * 4)
    for y in range(h):
        row = px[y * rs: y * rs + w * 4]
        seg = bytearray(w * 4)
        seg[0::4], seg[1::4], seg[2::4], seg[3::4] = row[3::4], row[0::4], row[1::4], row[2::4]
        out[y * w * 4:(y + 1) * w * 4] = seg
    return bytes(out)


def run_tray():
    try:
        from gi.repository import Gio, GLib
    except ImportError:
        sys.exit("Needs PyGObject: sudo apt install python3-gi gir1.2-gdkpixbuf-2.0")
    st = load_state()
    if st and st.get("phase") in ("pending", "running"):  # show the test's progress
        subprocess.Popen([sys.executable, os.path.abspath(__file__)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    bat = Battery()
    st = {"pixmap": b"", "title": "Battery", "text": "Reading UPS…", "key": None}

    def esc(t):
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def get_prop(conn, sender, path, iface, prop):
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "pi5-battery"),
            "Title": GLib.Variant("s", "Battery"),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": GLib.Variant("a(iiay)", [(ICON_SIZE, ICON_SIZE, st["pixmap"])]),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], esc(st["title"]),
                                                    esc(st["text"]))),
            "ItemIsMenu": GLib.Variant("b", False),
        }
        return values.get(prop)

    def method(conn, sender, path, iface, name, params, invocation):
        if name == "Activate":
            subprocess.Popen([sys.executable, os.path.abspath(__file__)],
                             start_new_session=True)
        invocation.return_value(None)

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
    bus.register_object("/StatusNotifierItem", node.interfaces[0], method, get_prop, None)

    def emit(sig):
        try:
            bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", sig, None)
        except GLib.Error:
            pass

    def update():
        b = bat.read()
        if b["ok"]:
            title = f"Battery {b['soc']:.0f}% · {b['state']}"
            text = "\n".join(x for x in (b["eta"], f"{b['volt']:.2f} V per cell",
                                         "Click: open Energy Plan") if x)
            key = (round(b["soc"]), b["charging"], True)
        else:
            title, text, key = "Battery: UPS not found", b["error"], (None, False, False)
        if key != st["key"]:
            st["key"], st["pixmap"] = key, render_argb(battery_icon_svg(b))
            emit("NewIcon")
        if (title, text) != (st["title"], st["text"]):
            st["title"], st["text"] = title, text
            emit("NewToolTip")
        return True

    def register(*_):
        try:
            bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                          "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                          GLib.Variant("(s)", ("/StatusNotifierItem",)), None,
                          Gio.DBusCallFlags.NONE, 3000, None)
        except GLib.Error as e:
            print(f"Panel tray not available yet: {e.message}", flush=True)

    update()
    # (re)register whenever the panel's tray appears, e.g. after a panel restart
    Gio.bus_watch_name_on_connection(bus, "org.kde.StatusNotifierWatcher",
                                     Gio.BusNameWatcherFlags.NONE, register, None)
    GLib.timeout_add(REFRESH_MS, update)
    GLib.MainLoop().run()


def install_tray():
    os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
    with open(AUTOSTART, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Pi 5 Battery\n"
                "Comment=Battery level in the panel (Geekworm X120x)\n"
                f"Exec={sys.executable} {os.path.abspath(__file__)} --tray\n"
                "Terminal=false\nX-GNOME-Autostart-enabled=true\n")
    restart_tray()
    print("Battery icon added to the panel and set to start at login.\n"
          f"Remove it: rm {AUTOSTART}  (and log out/in)")


def restart_tray():
    """Stop any running battery icon (maybe an older version) and start this one."""
    subprocess.run(["pkill", "-u", str(os.getuid()), "-f", "pi5_energyplan.py --tray"],
                   capture_output=True)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "--tray"],
                     start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- #
#  Applying a plan (root needed)
# --------------------------------------------------------------------------- #
def build_block(name, p=None):
    p = p or PLANS[name]
    lines = [BLOCK_START, f"# plan: {name}", "[all]"]
    for i, (temp, hyst, speed) in enumerate(p["fan"]):
        lines += [
            f"dtparam=fan_temp{i}={int(temp * 1000)}",
            f"dtparam=fan_temp{i}_hyst={int(hyst * 1000)}",
            f"dtparam=fan_temp{i}_speed={int(speed)}",
        ]
    lines.append(f"arm_freq={p['max_freq_mhz']}")
    if p.get("over_voltage_delta"):
        lines.append(f"over_voltage_delta={int(p['over_voltage_delta'])}")
    for key in ("gpu_freq", "v3d_freq"):   # gpu_freq: older custom plans may use it
        if p.get(key):
            lines.append(f"{key}={int(p[key])}")
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def write_config(name, p=None, target=None):
    """Put the plan block into config.txt (or into `target`, e.g. tryboot.txt)."""
    CONFIG_NOTES.clear()
    path = config_path()
    target = target or path
    if not os.path.exists(path):
        raise RuntimeError(f"{path} not found")
    with open(path) as f:
        text = f.read()

    backup = path + ".energyplan.bak"
    if target == path and not os.path.exists(backup):
        shutil.copy2(path, backup)

    # Drop our old block. Lines someone added after it (audio HAT guides say "add
    # this at the end of config.txt", raspi-config appends there too) were under
    # the block's [all]: an [all] in its place keeps them there. Without it they'd
    # end up in the section above (on a stock file [cm5]), which a Pi 5 ignores.
    def old_block(m):
        return "[all]\n" if text[m.end():].strip() else ""
    text = re.sub(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END) + r"\n?",
                  old_block, text, flags=re.S)
    if target == path:
        text = repair_moved_lines(text, read(backup, "") or "", path)

    # Comment out fan/arm_freq lines elsewhere in [all]/[pi5] so ours win cleanly
    out, section, seen_disabled = [], "all", set()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
        if CONFLICT_RE.match(line) and section in ("all", "pi5"):
            line = "#energyplan-disabled# " + line.strip()
        if line.startswith("#energyplan-disabled#"):
            if (section, line) in seen_disabled:   # same old line twice: keep one
                continue
            seen_disabled.add((section, line))
        out.append(line)

    # drop a section header that has nothing under it (e.g. left by a double paste)
    tidy = []
    for i, line in enumerate(out):
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            nxt = next((x.strip() for x in out[i + 1:] if x.strip()), None)
            if nxt is None or (nxt.startswith("[") and nxt.endswith("]")):
                continue
        tidy.append(line)
    text = "\n".join(tidy).rstrip("\n") + "\n\n" + build_block(name, p)

    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return target


CONFIG_NOTES = []   # messages from write_config for the caller to show


def sections(lines):
    """[(header or None, [lines])] in file order."""
    out = [(None, [])]
    for line in lines:
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            out.append((st.lower(), []))
        else:
            out[-1][1].append(line)
    return out


def repair_moved_lines(text, backup, path):
    """Versions before 2.0.0 moved lines that were added after the plan block into
    the section above it when they rewrote the block (e.g. under [cm5], which a
    Pi 5 ignores: an audio overlay added at the end stopped working). Find lines
    appended to that last section since the backup was made and put them back
    under [all]. Lines already there before the first change are never moved."""
    secs = sections(text.splitlines())
    header, body = secs[-1]
    if header in (None, "[all]", "[pi5]"):
        return text

    def content(lines):
        lines = list(lines)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines
    cur = content(body)
    before = [b for h, b in sections(backup.splitlines()) if h == header]
    if not before:
        return text
    orig = content(before[-1])
    if len(cur) <= len(orig) or cur[:len(orig)] != orig:
        return text
    k = len(orig)
    while k < len(cur) and not cur[k].strip():
        k += 1
    moved = [x.strip() for x in cur[k:] if x.strip() and not x.strip().startswith("#")]
    if not moved:
        return text
    lines = text.splitlines()
    lines.insert(len(lines) - len(body) + k, "[all]")   # body = the file's last lines
    if not os.path.exists(path + ".before-repair"):
        shutil.copy2(path, path + ".before-repair")      # the file as it was
    CONFIG_NOTES.append(f"config.txt repaired: {', '.join(moved)} moved back under [all] "
                        f"(an older version had put {'it' if len(moved) == 1 else 'them'} "
                        f"under {header}, where a Pi 5 ignores it). Reboot to apply.")
    return "\n".join(lines) + "\n"


def set_trip_points(fan):
    """Move the fan's active trip temps live. Returns True if the kernel accepted it."""
    trips = []
    for f in glob.glob(THERMAL_ZONE + "/trip_point_*_type"):
        if read(f) == "active":
            idx = int(re.search(r"trip_point_(\d+)_type", f).group(1))
            trips.append(idx)
    if not trips:
        return False
    trips.sort(key=lambda i: as_int(read(f"{THERMAL_ZONE}/trip_point_{i}_temp"), 0))

    pairs = list(zip(trips, fan))
    for _ in range(2):  # 2nd pass catches writes rejected due to ordering
        failed = 0
        for idx, (temp, hyst, _speed) in pairs:
            try:
                write(f"{THERMAL_ZONE}/trip_point_{idx}_temp", int(temp * 1000))
            except OSError:
                failed += 1
            try:
                write(f"{THERMAL_ZONE}/trip_point_{idx}_hyst", int(hyst * 1000))
            except OSError:
                pass
        if not failed:
            return True
    return False


def apply_live(name):
    """Settings that take effect immediately but are lost on reboot."""
    p = PLANS[name]
    msgs = []
    needs_reboot = False
    used_gov = "unchanged"

    for pol in glob.glob("/sys/devices/system/cpu/cpufreq/policy*"):
        avail = (read(pol + "/scaling_available_governors") or "").split()
        gov = next((g for g in [p["governor"]] + GOVERNOR_FALLBACK.get(p["governor"], [])
                    if g in avail), None)
        if gov:
            write(pol + "/scaling_governor", gov)
            used_gov = gov
        hw_max = as_int(read(pol + "/cpuinfo_max_freq"), 0)
        hw_min = as_int(read(pol + "/cpuinfo_min_freq"), 0)
        target = p["max_freq_mhz"] * 1000
        if hw_max:
            needs_reboot |= target > hw_max
            target = min(target, hw_max)
        target = max(target, hw_min)
        write(pol + "/scaling_max_freq", target)
    if needs_reboot:
        msgs.append(f"CPU: {used_gov} governor now; {p['max_freq_mhz']} MHz unlocks after reboot")
    else:
        msgs.append(f"CPU: {used_gov} governor, max {p['max_freq_mhz']} MHz (applied now)")

    if set_trip_points(p["fan"]):
        msgs.append("Fan temps: applied now (new speeds after reboot)")
    else:
        msgs.append("Fan temps: kernel doesn't allow live change, applies after reboot")
    return msgs


def apply_plan(name, save_config=True):
    msgs = apply_live(name)
    if save_config:
        path = write_config(name)
        msgs.append(f"Saved to {path} (reboot for full fan curve)")
        msgs += CONFIG_NOTES
        try:
            install_service()
            msgs.append("Boot service on: this plan is restored at every boot")
        except Exception as e:  # noqa: BLE001
            msgs.append(f"Warning: boot service not installed ({e})")
    return msgs


# --------------------------------------------------------------------------- #
#  Keep the plan across reboots (systemd service)
# --------------------------------------------------------------------------- #
SERVICE_UNIT = f"""[Unit]
Description=Restore Pi 5 energy plan (CPU governor and max frequency)
After=sysinit.target raspi-config.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 {INSTALLED_SCRIPT} --boot

[Install]
WantedBy=multi-user.target
"""


def install_service():
    """Copy this script to /usr/local/bin and enable the boot service."""
    install_copy()
    if read(SERVICE_PATH) != SERVICE_UNIT.strip():
        write(SERVICE_PATH, SERVICE_UNIT)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", SERVICE_NAME], check=True,
                   capture_output=True)


def uninstall_service():
    subprocess.run(["systemctl", "disable", SERVICE_NAME], capture_output=True)
    # the battery guard runs from the same system copy: keep it while the guard is there
    paths = [SERVICE_PATH] + ([] if os.path.exists(GUARD_UNIT_PATH) else [INSTALLED_SCRIPT])
    for path in paths:
        if os.path.exists(path):
            os.remove(path)
    subprocess.run(["systemctl", "daemon-reload"])
    print("Boot service removed. Fan curve in config.txt is kept; "
          "pick 'Balanced' first if you want defaults back.")


def uninstall_guard():
    # stopping the guard switches charging back on (see battery_guard's finally)
    subprocess.run(["systemctl", "disable", "--now", GUARD_SERVICE], capture_output=True)
    paths = [GUARD_UNIT_PATH, GUARD_STATUS] + \
        ([] if os.path.exists(SERVICE_PATH) else [INSTALLED_SCRIPT])
    for path in paths:
        if os.path.exists(path):
            os.remove(path)
    subprocess.run(["systemctl", "daemon-reload"])
    print("Battery guard removed: no charge limit, no auto shutdown, no battery saver. "
          f"Your settings stay in {BATTERY_CFG}.")


def boot_apply():
    """Run by the service at boot: re-apply the saved plan's live settings."""
    st = load_state()
    if st and st.get("phase") in ("pending", "running"):
        autotest_boot(st)
        return
    name = find_plan(saved_plan() or "")
    if not name:
        print("No saved plan in config.txt, nothing to do")
        return
    for line in apply_live(name):
        print(f"[{name}] {line}")
    # Raspberry Pi OS may switch the governor back to 'ondemand' shortly after
    # boot, so apply once more after things have settled.
    time.sleep(60)
    for line in apply_live(name):
        print(f"[{name}] re-check: {line}")


# --------------------------------------------------------------------------- #
#  Sweet spot auto test
#
#  How it stays safe: each test overclock is written to tryboot.txt, NOT
#  config.txt, and the Pi is rebooted with "reboot '0 tryboot'". The firmware
#  uses tryboot.txt for that ONE boot only. If the test freezes, the hardware
#  watchdog (or you, by unplugging power) reboots it, and it comes back with
#  your normal config.txt. The boot service then marks that step as failed and
#  continues. Only the winning setting is written to config.txt at the end.
# --------------------------------------------------------------------------- #
def load_state():
    try:
        with open(AUTOTEST_STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = AUTOTEST_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, AUTOTEST_STATE)
    os.sync()


def at_log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S  ") + msg
    print(line, flush=True)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(AUTOTEST_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def tryboot_path():
    return os.path.join(os.path.dirname(config_path()), "tryboot.txt")


def booted_with_tryboot():
    """True/False from the firmware flag, or None if the flag isn't exposed."""
    try:
        with open("/proc/device-tree/chosen/bootloader/tryboot", "rb") as f:
            return int.from_bytes(f.read(4), "big") == 1
    except OSError:
        return None


def is_pi5():
    # /proc/device-tree is a link to /sys/firmware/devicetree/base
    model = read("/proc/device-tree/model") or \
        read("/sys/firmware/devicetree/base/model", "") or ""
    return any(m in model for m in ("Raspberry Pi 5", "Raspberry Pi 500",
                                    "Compute Module 5"))


def step_label(freq, ovd):
    return f"{freq} MHz +{ovd // 1000} mV"


def autotest_summary():
    st = load_state()
    if not st:
        return "not tested"
    phase = st.get("phase")
    if phase in ("pending", "running"):
        f, v = AUTOTEST_STEPS[st["index"]]
        text = f"TESTING {step_label(f, v)} (step {len(st['results']) + 1})"
        left = (st.get("stage_ends") or 0) - time.time()
        if left > 0:
            what = {"starting": "stress test starts in", "stress": "stress test",
                    "continuing": "next step in"}.get(st.get("stage"), "")
            text += f" · {what} {int(left) // 60}:{int(left) % 60:02d}" + \
                (" left" if st.get("stage") == "stress" else "")
        else:
            text += " · waiting for the test reboot"
        return text
    if phase == "done":
        if st.get("best"):
            return f"{step_label(*st['best'])} → plan '{SWEET_SPOT}'"
        return "done, no overclock was stable"
    if phase == "error":
        last = (read(AUTOTEST_LOG, "") or "").strip().splitlines()
        return "error: " + (last[-1][21:] if last else "see --autotest-status")
    return phase or "?"


def autotest_stuck():
    """True if a test is in progress but nothing is driving it (no boot service
    running for a few minutes after boot)."""
    st = load_state()
    if not st or st.get("phase") not in ("pending", "running"):
        return False
    try:
        uptime = float((read("/proc/uptime", "0") or "0").split()[0])
    except ValueError:
        uptime = 0
    if uptime < 240 or (st.get("stage_ends") or 0) > time.time() - 120:
        return False
    r = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE_NAME])
    return r.returncode != 0


def resume_autotest():
    st = load_state()
    if not st or st.get("phase") not in ("pending", "running", "error"):
        raise RuntimeError("No sweet spot test to continue")
    install_service()
    st["phase"] = "pending"
    save_state(st)
    at_log("Test continued by user")
    tryboot_step(st)


def start_autotest(minutes, max_temp):
    if not is_pi5():
        raise RuntimeError("The sweet spot test is only for the Raspberry Pi 5")
    st = load_state()
    if st and st.get("phase") in ("pending", "running"):
        raise RuntimeError("A test is already running (stop it with --autotest-cancel)")
    if not shutil.which("stress-ng"):
        print("Installing stress-ng for a stronger test…", flush=True)
        subprocess.run(["apt-get", "install", "-y", "stress-ng"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    install_service()
    os.makedirs(STATE_DIR, exist_ok=True)
    open(AUTOTEST_LOG, "w").close()
    state = {"phase": "pending", "index": 0, "minutes": minutes, "max_temp": max_temp,
             "results": [], "best": None, "started": time.strftime("%Y-%m-%d %H:%M")}
    save_state(state)
    at_log(f"Sweet spot test started: {minutes} min per step, fail above {max_temp} °C")
    tryboot_step(state)


def tryboot_step(state):
    freq, ovd = AUTOTEST_STEPS[state["index"]]
    plan = {"governor": "ondemand", "max_freq_mhz": freq,
            "over_voltage_delta": ovd, "fan": TEST_FAN}
    write_config(f"Autotest {step_label(freq, ovd)}", plan, target=tryboot_path())
    state["stage"], state["stage_ends"] = "rebooting", None
    save_state(state)
    at_log(f"Rebooting into test {step_label(freq, ovd)} (one-time tryboot)")
    os.sync()
    subprocess.run(["reboot", "0 tryboot"])


def cpu_full_speed_for_test(freq_mhz):
    """Make the stress test really run at the test speed: performance governor and
    the max frequency unlocked. Without this the CPU can sit at 1.5 GHz (powersave)
    and every step would 'pass' without being tested."""
    got = []
    for pol in glob.glob("/sys/devices/system/cpu/cpufreq/policy*"):
        avail = (read(pol + "/scaling_available_governors") or "").split()
        for gov in ("performance", "ondemand", "schedutil"):
            if gov in avail:
                try:
                    write(pol + "/scaling_governor", gov)
                except OSError:
                    continue
                break
        hw_max = as_int(read(pol + "/cpuinfo_max_freq"), 0)
        try:
            write(pol + "/scaling_max_freq", hw_max or freq_mhz * 1000)
        except OSError:
            pass
        got.append(f"{read(pol + '/scaling_governor')}, max {hw_max // 1000} MHz")
    return "; ".join(got) or "cpufreq not found"


def set_stage(state, stage, seconds):
    """Remember what the test is doing right now, so the GUI can show a countdown."""
    state["stage"], state["stage_ends"] = stage, time.time() + seconds
    save_state(state)


def autotest_boot(state):
    """Called at boot while a test is in progress."""
    freq, ovd = AUTOTEST_STEPS[state["index"]]
    cancel_hint = f"cancel: sudo python3 {INSTALLED_SCRIPT} --autotest-cancel"

    if state["phase"] == "running":
        at_log(f"{step_label(freq, ovd)} froze or reset during the stress test")
        at_log(f"Continuing in {BOOT_DELAY}s ({cancel_hint})")
        set_stage(state, "continuing", BOOT_DELAY)
        time.sleep(BOOT_DELAY)
        if (load_state() or {}).get("phase") == "running":
            finish_step(load_state(), False, "crashed", None)
        return

    flag = booted_with_tryboot()
    in_test = flag if flag is not None else \
        as_int(read(CPU0 + "/cpuinfo_max_freq"), 0) == freq * 1000
    if not in_test:
        if not state["results"]:
            at_log("The test settings were not used on boot. Update the bootloader "
                   "(sudo rpi-eeprom-update -a, then reboot) and try again.")
            state["phase"] = "error"
            save_state(state)
            remove_tryboot()
            return
        at_log(f"{step_label(freq, ovd)} did not boot")
        set_stage(state, "continuing", BOOT_DELAY)
        time.sleep(BOOT_DELAY)
        if (load_state() or {}).get("phase") == "pending":
            finish_step(load_state(), False, "no boot", None)
        return

    at_log(f"Booted {step_label(freq, ovd)}. Stress test in {BOOT_DELAY}s ({cancel_hint})")
    set_stage(state, "starting", BOOT_DELAY)
    time.sleep(BOOT_DELAY)
    state = load_state()
    if not state or state.get("phase") != "pending":
        return
    at_log("CPU set for the test: " + cpu_full_speed_for_test(freq))
    state["phase"] = "running"
    set_stage(state, "stress", state["minutes"] * 60)
    result = run_stress(state["minutes"] * 60, state["max_temp"])
    if result is None:
        at_log("Cancelled. Reboot to return to your normal settings.")
        return
    finish_step(load_state() or state, *result)


def finish_step(state, passed, reason, peak):
    i = state["index"]
    freq, ovd = AUTOTEST_STEPS[i]
    state["results"].append({"freq": freq, "ovd": ovd, "passed": passed,
                             "reason": reason, "peak_temp": peak})
    peak_txt = f", peak {peak:.1f} °C" if peak else ""
    at_log(f"{step_label(freq, ovd)}: {'PASS' if passed else 'FAIL'} ({reason}{peak_txt})")

    if passed:
        state["best"] = [freq, ovd]
        nxt = next((j for j in range(i + 1, len(AUTOTEST_STEPS))
                    if AUTOTEST_STEPS[j][0] > freq), None)
    elif reason in ("unstable", "crashed", "no boot"):
        # same speed, more voltage
        j = i + 1
        nxt = j if j < len(AUTOTEST_STEPS) and AUTOTEST_STEPS[j][0] == freq else None
    else:
        nxt = None  # too hot / power problem: going higher won't help

    if nxt is None:
        finish_autotest(state)
    else:
        state["index"], state["phase"] = nxt, "pending"
        save_state(state)
        tryboot_step(state)


def save_custom_plan(freq, ovd):
    plan = {"desc": f"Found by auto test: {freq / 1000:.1f} GHz, +{ovd // 1000} mV.",
            "governor": "ondemand", "max_freq_mhz": freq, "over_voltage_delta": ovd,
            "fan": SWEET_FAN, "color": "#66bb6a", "warn": True}
    os.makedirs(os.path.dirname(CUSTOM_PLANS), exist_ok=True)
    try:
        with open(CUSTOM_PLANS) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data[SWEET_SPOT] = plan
    with open(CUSTOM_PLANS, "w") as f:
        json.dump(data, f, indent=1)
    PLANS[SWEET_SPOT] = dict(plan, fan=[tuple(x) for x in plan["fan"]])


def remove_tryboot():
    try:
        os.remove(tryboot_path())
    except OSError:
        pass


def finish_autotest(state):
    state["phase"] = "done"
    state["finished"] = time.strftime("%Y-%m-%d %H:%M")
    remove_tryboot()
    if state.get("best"):
        freq, ovd = state["best"]
        save_custom_plan(freq, ovd)
        write_config(SWEET_SPOT)
        at_log(f"SWEET SPOT: {step_label(freq, ovd)}. Saved as plan '{SWEET_SPOT}' "
               "and set as your plan.")
    else:
        at_log("No overclock was stable. Your current plan was kept.")
    save_state(state)
    at_log("Rebooting into your normal settings")
    os.sync()
    subprocess.run(["reboot"])


def _burn_worker(seconds, q):
    """Fallback load when stress-ng isn't installed: repeat a fixed calculation
    and report if the answer ever changes (a sign of an unstable CPU)."""
    import hashlib
    import math
    try:
        os.nice(10)
    except OSError:
        pass

    def block():
        h = b"pi5-energyplan"
        for _ in range(20000):
            h = hashlib.sha256(h).digest()
        x = 0.0
        for i in range(1, 30000):
            x += math.sqrt(i) * math.sin(i)
        return h.hex() + repr(x)

    ref = block()
    q.put(("ref", ref))
    end = time.time() + seconds
    while time.time() < end:
        if block() != ref:
            q.put(("err", None))
            return


def run_stress(seconds, max_temp):
    """Load every core. Returns (passed, reason, peak_temp), or None if cancelled."""
    wd = None
    try:
        wd = open("/dev/watchdog", "wb", buffering=0)  # resets the Pi if we freeze
    except OSError:
        at_log("Hardware watchdog not available: if it freezes, unplug and replug power")

    peak, ng, procs, q, refs = 0.0, None, [], None, set()
    use_ng = shutil.which("stress-ng") is not None
    try:
        if use_ng:
            ng = subprocess.Popen(["nice", "-n", "10", "stress-ng", "--cpu", "0",
                                   "--cpu-method", "all", "--verify",
                                   "--timeout", f"{seconds}s"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            import multiprocessing as mp
            q = mp.Queue()
            procs = [mp.Process(target=_burn_worker, args=(seconds, q), daemon=True)
                     for _ in range(os.cpu_count() or 4)]
            for p in procs:
                p.start()

        end = time.time() + seconds
        while True:
            time.sleep(2)
            if wd:
                wd.write(b"1")
            t = cpu_temp_c()
            if t is not None:
                peak = max(peak, t)
            bits = throttle_bits() or 0
            if bits & 0x10001:
                return False, "power", peak
            if (t is not None and t >= max_temp) or bits & 0xE:
                return False, "too hot", peak
            if q is not None:
                while not q.empty():
                    kind, val = q.get_nowait()
                    if kind == "err":
                        return False, "unstable", peak
                    refs.add(val)
                if len(refs) > 1:
                    return False, "unstable", peak
            if (load_state() or {}).get("phase") != "running":
                return None
            if use_ng:
                if ng.poll() is not None:
                    if ng.returncode != 0:
                        return False, "unstable", peak
                    break
                if time.time() > end + 120:
                    break
            elif time.time() >= end:
                break
        cur = as_int(read(CPU0 + "/scaling_cur_freq"), 0) // 1000
        return True, f"stable, ran at {cur} MHz", peak
    finally:
        if ng and ng.poll() is None:
            ng.terminate()
        for p in procs:
            if p.is_alive():
                p.terminate()
        if wd:
            try:
                wd.write(b"V")  # "magic close": stop the watchdog cleanly
                wd.close()
            except OSError:
                pass


def cancel_autotest():
    remove_tryboot()
    st = load_state()
    if st and st.get("phase") in ("pending", "running"):
        st["phase"] = "cancelled"
        save_state(st)
        at_log("Test cancelled by user")
    print("Sweet spot test cancelled. config.txt was never changed by the test.\n"
          "If the Pi is in a test boot right now, reboot to return to normal.")


# --------------------------------------------------------------------------- #
#  Privilege handling for the GUI
# --------------------------------------------------------------------------- #
def run_privileged(name, save_config):
    """Apply a plan, asking for the password with pkexec if needed. Returns (ok, text)."""
    if os.geteuid() == 0:
        try:
            return True, "\n".join(apply_plan(name, save_config))
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    extra = ["--apply", name] + ([] if save_config else ["--no-config"])
    return run_root(extra)


def run_root(extra, timeout=180):
    """Run this script with root rights (pkexec password prompt). Returns (ok, text)."""
    args = [sys.executable, os.path.abspath(__file__)] + extra
    if os.geteuid() == 0:
        cmd = args
    elif shutil.which("pkexec"):
        cmd = ["pkexec"] + args
    else:
        cmd = ["sudo", "-n"] + args

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "No answer in time (was the password window closed or hidden?)"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode == 0:
        return True, r.stdout.strip()
    if r.returncode in (126, 127) and cmd[0] == "pkexec":
        return False, "Password prompt cancelled or not authorized."
    return False, (r.stderr or r.stdout).strip() or f"Failed (code {r.returncode})"


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
BG, CARD, FG, MUTED = "#1e1f24", "#2a2c33", "#e8e8e8", "#9aa0a6"


SCROLLBAR = 18     # wide enough to drag with a finger on a touch screen


def screen_room(win):
    """Usable screen size for a window: minus the taskbar and the title bar."""
    return win.winfo_screenwidth() - 16, win.winfo_screenheight() - 80


def make_scrollable(win, footer=False, **pad):
    """Build a window's contents in the frame this returns. If the screen is too small
    for it (e.g. the 800x480 Touch Display) the window is sized to the screen and gets
    scroll bars; on a big screen it looks exactly as without.
    footer=True also returns a frame pinned below the scrolling part, for the
    buttons, so they are always visible."""
    bg = win["bg"]
    canvas = tk.Canvas(win, bg=bg, highlightthickness=0, bd=0)
    bar = {"width": SCROLLBAR, "bg": CARD, "troughcolor": bg, "activebackground": "#555",
           "highlightthickness": 0, "bd": 0, "relief": "flat"}
    vbar = tk.Scrollbar(win, orient="vertical", command=canvas.yview, **bar)
    hbar = tk.Scrollbar(win, orient="horizontal", command=canvas.xview, **bar)
    canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    win.grid_rowconfigure(0, weight=1)
    win.grid_columnconfigure(0, weight=1)
    inner = tk.Frame(canvas, bg=bg, **pad)
    canvas.create_window(0, 0, window=inner, anchor="nw")
    foot = None
    if footer:
        foot = tk.Frame(win, bg=bg, padx=pad.get("padx", 0))
        foot.grid(row=2, column=0, columnspan=2, sticky="ew")

    def fit(_=None):
        w, h = inner.winfo_reqwidth(), inner.winfo_reqheight()
        room_w, room_h = screen_room(win)
        if foot is not None:
            room_h -= foot.winfo_reqheight()
        need_v = h > room_h
        need_h = w > room_w - (SCROLLBAR if need_v else 0)
        need_v = need_v or h > room_h - (SCROLLBAR if need_h else 0)
        canvas.config(width=min(w, room_w - (SCROLLBAR if need_v else 0)),
                      height=min(h, room_h - (SCROLLBAR if need_h else 0)),
                      scrollregion=(0, 0, w, h))
        if need_v:
            vbar.grid(row=0, column=1, sticky="ns")
        else:
            vbar.grid_remove()
            canvas.yview_moveto(0)
        if need_h:
            hbar.grid(row=1, column=0, sticky="ew")
        else:
            hbar.grid_remove()
            canvas.xview_moveto(0)
        win.scrolls = (need_v, need_h)
    inner.bind("<Configure>", fit)
    if foot is not None:
        foot.bind("<Configure>", fit)

    def wheel(e):
        try:
            if e.widget.winfo_toplevel() is not win or vbar.winfo_ismapped() == 0:
                return
        except (AttributeError, tk.TclError):
            return
        step = -1 if getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0 else 1
        canvas.yview_scroll(step * 3, "units")
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        win.bind_all(seq, wheel, add="+")
    win.scrolls = (False, False)
    return (inner, foot) if footer else inner


def keep_on_screen(win):
    """Move a window back inside the screen if part of it is outside."""
    win.update_idletasks()
    room_w, room_h = screen_room(win)
    x = max(0, min(win.winfo_rootx(), room_w - win.winfo_reqwidth()))
    y = max(0, min(win.winfo_rooty(), room_h - win.winfo_reqheight()))
    if (x, y) != (win.winfo_rootx(), win.winfo_rooty()):
        win.geometry(f"+{x}+{y}")


def temp_color(t):
    if t is None:
        return MUTED
    if t < 50:
        return "#66bb6a"
    if t < 65:
        return "#ffa726"
    return "#ef5350"


class App:
    HISTORY = 90  # samples (3 s each = 4.5 min)

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Pi 5 Energy Plan")
        self.root.configure(bg=BG)
        self.history = []
        self.buttons = {}
        self.busy = False
        self.busy_task, self.busy_since = "", time.time()
        self.result = None
        self.save_var = tk.BooleanVar(value=True)
        self.battery = Battery()
        self.root.report_callback_exception = self._show_error
        self._build()
        self.refresh()
        self.root.after(800, self.check_update)

    def check_update(self):
        me = os.path.abspath(__file__)
        if me == INSTALLED_SCRIPT or not os.path.exists(INSTALLED_SCRIPT) or \
                file_sha(me) == file_sha(INSTALLED_SCRIPT):
            return
        if installed_is_newer():
            self.msg.config(text=f"This is an older copy (version {VERSION}). Version "
                                 f"{fmt_version(version_of(INSTALLED_SCRIPT))} is installed: "
                                 "open Pi 5 Energy Plan from the menu instead.", fg="#ffa726")
            return
        self.choose("Update", [("yes", "Update the background services now"),
                               ("no", "Not now")], self.do_update,
                    text=f"This copy (version {VERSION}) differs from the one the background "
                         f"services use (version {fmt_version(version_of(INSTALLED_SCRIPT))})"
                         ". Nothing will be added twice.")

    def do_update(self, answer):
        if answer == "yes" and not self.blocked("the update", lock_during_test=False):
            self.set_busy("updating the background services")
            self.msg.config(text="Updating… (enter your password if asked)", fg=MUTED)

            def work():
                refresh_old_copies()
                if os.path.exists(AUTOSTART):
                    restart_tray()
                if os.path.exists(LAUNCHER):
                    install_launcher()
                self.result = ("Update", False) + run_root(["--update-root"], timeout=300)
            threading.Thread(target=work, daemon=True).start()
            self.root.after(200, self.poll_result)

    def choose(self, title, options, callback, anchor=None, current=None, text=None):
        """Small picker window. Used instead of Tk drop-down menus, which often
        don't open on the Raspberry Pi's Wayland desktop."""
        old = getattr(self, "chooser", None)
        if old is not None and old.winfo_exists():
            old.destroy()
        win = self.chooser = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG, padx=10, pady=8)
        win.resizable(False, False)
        win.transient(self.root)
        self._label(win, title, 11, bold=True).pack(anchor="w")
        if text:
            self._label(win, text, 9, MUTED, wraplength=340, justify="left") \
                .pack(anchor="w", pady=(2, 0))
        box = tk.Frame(win, bg=BG)
        box.pack(fill="x", pady=(6, 0))

        def pick(value):
            win.destroy()
            callback(value)
        for value, label in options:
            sel = value == current
            tk.Button(box, text=("●  " if sel else "○  ") + label, anchor="w",
                      justify="left", relief="flat", bd=0, padx=10, pady=6,
                      bg="#3a5a7a" if sel else CARD, fg=FG, activebackground="#4a6a8a",
                      activeforeground=FG, font=("DejaVu Sans", 10),
                      command=lambda v=value: pick(v)).pack(fill="x", pady=1)
        tk.Button(win, text="Close", command=win.destroy, relief="flat", bg=CARD, fg=FG,
                  activebackground="#555", activeforeground=FG,
                  font=("DejaVu Sans", 9)).pack(anchor="e", pady=(8, 0), ipadx=8)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.update_idletasks()
        if anchor is not None:
            x = anchor.winfo_rootx()
            y = anchor.winfo_rooty() + anchor.winfo_height()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x = max(0, min(x, sw - win.winfo_reqwidth() - 8))
            y = max(0, min(y, sh - win.winfo_reqheight() - 40))
            win.geometry(f"+{x}+{y}")
        win.lift()
        win.focus_force()
        return win

    def picker_button(self, parent, var, width, on_click):
        """A button that shows the current value with a ▾ and opens a picker."""
        b = tk.Button(parent, text=f"{var.get()}  ▾", width=width, anchor="w",
                      relief="flat", bd=0, padx=6, pady=3, bg=CARD, fg=FG,
                      activebackground="#3a3d45", activeforeground=FG,
                      font=("DejaVu Sans", 9))
        b.config(command=lambda: on_click(b))
        var.trace_add("write", lambda *_: b.config(text=f"{var.get()}  ▾"))
        return b

    def _label(self, parent, text="", size=10, color=FG, bold=False, **kw):
        font = ("DejaVu Sans", size, "bold" if bold else "normal")
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color, font=font, **kw)

    def _build(self):
        pad = {"padx": 14, "pady": 6}
        body, foot = make_scrollable(self.root, footer=True)
        self._label(body, "Pi 5 Energy Plan", 15, bold=True).pack(anchor="w", **pad)

        # Live stats card
        card = tk.Frame(body, bg=CARD, padx=12, pady=10)
        card.pack(fill="x", padx=14)
        self.temp_lbl = self._label(card, "--.- °C", 30, bold=True)
        self.temp_lbl.grid(row=0, column=0, rowspan=6, sticky="w", padx=(0, 18))

        self.stat = {}
        for r, key in enumerate(("Fan", "CPU", "Governor", "Power", "Saved plan",
                                 "Sweet spot")):
            self._label(card, key, 9, MUTED).grid(row=r, column=1, sticky="w")
            v = self._label(card, "…", 10)
            v.grid(row=r, column=2, sticky="w", padx=(8, 0))
            self.stat[key] = v

        # Battery card (Geekworm X120x UPS)
        bcard = tk.Frame(body, bg=CARD, padx=12, pady=8)
        bcard.pack(fill="x", padx=14, pady=(6, 0))
        self.bat_pct = self._label(bcard, "--%", 22, bold=True)
        self.bat_pct.grid(row=0, column=0, rowspan=3, sticky="w", padx=(0, 14))
        self.bat_bar = tk.Canvas(bcard, width=250, height=12, bg="#3a3d45",
                                 highlightthickness=0)
        self.bat_bar.grid(row=0, column=1, sticky="w", pady=(2, 4))
        self.bat_state = self._label(bcard, "Reading UPS…", 10, wraplength=290,
                                     justify="left")
        self.bat_state.grid(row=1, column=1, sticky="w")
        self.bat_eta = self._label(bcard, "", 10, MUTED)
        self.bat_eta.grid(row=2, column=1, sticky="w")
        self.bat_info = self._label(bcard, "", 8, MUTED, wraplength=420, justify="left")
        self.bat_info.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tk.Button(bcard, text="UPS settings…", command=self.open_ups_settings,
                  relief="flat", bg="#3a3d45", fg=FG, activebackground="#4a4d55",
                  activeforeground=FG, font=("DejaVu Sans", 9)) \
            .grid(row=0, column=2, rowspan=2, sticky="ne", padx=(8, 0))
        self.bat_banner = tk.Label(bcard, text="", fg="#000", font=("DejaVu Sans", 9, "bold"),
                                   wraplength=410, justify="left", padx=8, pady=4)
        self.bat_banner.grid(row=4, column=0, columnspan=3, sticky="we", pady=(6, 0))
        self.bat_banner.grid_remove()

        self.canvas = tk.Canvas(body, width=440, height=70, bg=CARD,
                                highlightthickness=0)
        self.canvas.pack(padx=14, pady=(6, 4))

        # Plan buttons
        plans = tk.Frame(body, bg=BG)
        plans.pack(fill="x", padx=14, pady=4)
        for c, head in enumerate(("Plan", "Governor", "Fan", "")):
            self._label(plans, head, 8, MUTED).grid(row=0, column=c, sticky="w",
                                                    padx=(0 if c == 0 else 8, 0))
        self.gov_vars, self.fan_vars = {}, {}
        govs = available_governors()
        for r, name in enumerate(PLANS, start=1):
            p = PLANS[name]
            b = tk.Button(plans, text=name, width=12, relief="flat", bd=0,
                          bg=CARD, fg=FG, activebackground=p["color"],
                          activeforeground="#000", font=("DejaVu Sans", 10, "bold"),
                          command=lambda n=name: self.on_plan(n))
            b.grid(row=r, column=0, sticky="w", pady=3, ipady=4)
            self.buttons[name] = b

            var = tk.StringVar(value=p["governor"])
            self.picker_button(plans, var, 12, lambda btn, n=name: self.choose(
                f"{n}: CPU governor",
                [(g, f"{g}  ·  {GOV_INFO[g]}") for g in govs],
                lambda g, n=n: self.on_governor(n, g), anchor=btn,
                current=PLANS[n]["governor"])).grid(row=r, column=1, sticky="w", padx=8)
            self.gov_vars[name] = var

            fvar = tk.StringVar(value=fan_profile_of(p["fan"]))
            self.picker_button(plans, fvar, 8, lambda btn, n=name: self.choose(
                f"{n}: fan profile",
                [(f, f"{f}  ·  {fan_profile_info(f)}") for f in FAN_PROFILES],
                lambda f, n=n: self.on_fan(n, f), anchor=btn,
                current=fan_profile_of(PLANS[n]["fan"]))).grid(row=r, column=2, sticky="w")
            self.fan_vars[name] = fvar

            self._label(plans, p["desc"], 9, MUTED, wraplength=170, justify="left") \
                .grid(row=r, column=3, sticky="w", padx=(8, 0))

        tk.Checkbutton(body, text="Keep this plan after reboot (config.txt + boot service)",
                       variable=self.save_var, bg=BG, fg=FG, selectcolor=CARD,
                       activebackground=BG, activeforeground=FG,
                       font=("DejaVu Sans", 9)).pack(anchor="w", padx=12)

        self.msg = self._label(foot, "Pick a plan.", 9, MUTED, wraplength=440,
                               justify="left")
        self.msg.pack(anchor="w", padx=14, pady=(4, 6))

        bottom = tk.Frame(foot, bg=BG)
        bottom.pack(fill="x", padx=14, pady=(0, 12))
        for text, cmd in (("Quit", self.root.destroy), ("Reboot now", self.on_reboot),
                          ("Find sweet spot…", self.on_autotest),
                          ("Battery in panel", self.on_tray)):
            tk.Button(bottom, text=text, command=cmd, relief="flat", bg=CARD, fg=FG,
                      activebackground="#444", activeforeground=FG,
                      font=("DejaVu Sans", 9)).pack(side="right", padx=(6, 0), ipadx=6)

    # ----- live refresh -----
    def refresh(self):
        s = get_status()
        t = s["temp"]
        self.temp_lbl.config(text=f"{t:.1f} °C" if t is not None else "--.- °C",
                             fg=temp_color(t))
        self.stat["Fan"].config(text=fmt_fan(s))
        self.stat["CPU"].config(text=fmt_freq(s))
        self.stat["Governor"].config(text=s["governor"] or "?")
        thr = s["throttled"] or "?"
        self.stat["Power"].config(text=thr, fg="#ef5350" if thr.startswith("NOW") else FG)
        self.stat["Saved plan"].config(text=s["plan"] or "none")
        sweet = autotest_summary()
        self.stat["Sweet spot"].config(text=sweet, fg="#ffa726" if sweet.startswith(
            "TESTING") else "#ef5350" if sweet.startswith("error") else FG)
        if sweet.startswith("TESTING") and not getattr(self, "stuck_asked", False) \
                and not self.busy and autotest_stuck():
            self.stuck_asked = True
            self.choose("Sweet spot test",
                        [("resume", "Continue the test (the Pi reboots)"),
                         ("cancel", "Cancel the test"), ("later", "Leave it for now")],
                        self.on_stuck_answer,
                        text="The sweet spot test seems to have stopped.")

        for name, b in self.buttons.items():
            active = name == s["plan"]
            b.config(bg=PLANS[name]["color"] if active else CARD,
                     fg="#000" if active else FG)

        self.update_battery()

        if t is not None:
            self.history = (self.history + [t])[-self.HISTORY:]
        self.draw_graph()
        self.root.after(REFRESH_MS, self.refresh)

    def on_stuck_answer(self, ans):
        if ans == "later" or self.blocked("the test", lock_during_test=False):
            return
        self.set_busy("continuing the sweet spot test" if ans == "resume"
                      else "cancelling the sweet spot test")
        cmd = ["--autotest-resume"] if ans == "resume" else ["--autotest-cancel"]
        threading.Thread(target=lambda: setattr(
            self, "result", ("Sweet spot test", False) + run_root(cmd)),
            daemon=True).start()
        self.root.after(200, self.poll_result)

    def update_battery(self):
        b = self.battery.read()
        self.bat_bar.delete("all")
        if not b["ok"]:
            self.bat_pct.config(text="--%", fg=MUTED)
            self.bat_state.config(text=b["error"], fg=MUTED)
            self.bat_eta.config(text="")
            self.bat_info.config(text="")
            self.bat_banner.grid_remove()
            return
        g = read_guard_status()
        col = battery_color(b["soc"])
        self.bat_pct.config(text=f"{b['soc']:.0f}%", fg=col)
        self.bat_bar.create_rectangle(0, 0, 250 * b["soc"] / 100, 12, fill=col, width=0)
        if g and g.get("enabled") and g["max"] < 100:   # mark the charge limit
            x = 250 * g["max"] / 100
            self.bat_bar.create_line(x, 0, x, 12, fill="#ffffff", width=2)
        icon = "⚡ " if b["charging"] else ""
        self.bat_state.config(text=f"{icon}{b['state']}",
                              fg="#ffa726" if b["on_battery"] else FG)
        self.bat_eta.config(text=b["eta"])

        pm = read_pmic()
        info = [f"Cell {b['volt']:.2f} V ({battery_health(b['volt'])})"]
        if pm.get("ext5v"):
            info.append(f"Pi input {pm['ext5v']:.2f} V")
        if pm.get("watts"):
            info.append(f"Pi uses {pm['watts']:.1f} W")
        if pm.get("core_v") is not None and pm.get("core_a") is not None:
            info.append(f"CPU core {pm['core_v']:.2f} V {pm['core_a']:.2f} A")
        if g and g.get("topup"):
            info.append("Charging to 100 % once")
        info.append("Protection ON" if g and g.get("enabled") else "Protection off")
        if g and g.get("enabled") and g.get("max", 100) < 100:
            info.append({"ok": "Charge stop ✓ verified", "unsupported": "Charge stop ✗",
                         }.get(g.get("charge_check"), "Charge stop: checking…"))
        if g and g.get("saver_enabled"):
            info.append("Saver ON" if g.get("saver_active") else f"Saver at {g['saver_below']} %")
        self.bat_info.config(text="  ·  ".join(info))

        text, bg = self.battery_banner(b, g)
        if text:
            self.bat_banner.config(text=text, bg=bg)
            self.bat_banner.grid()
        else:
            self.bat_banner.grid_remove()

    @staticmethod
    def battery_banner(b, g):
        """Warning levels while on battery (like qtx120x.py), plus shutdown countdown."""
        red, orange = "#ef5350", "#ffa726"
        if g and g.get("volt_high"):
            return (f"⚠ Battery at {g.get('volt', 0):.2f} V: the charger isn't stopping "
                    "when full. Unplug the adapter and check the board/cells.", red)
        if g and g.get("charge_warn") and not b["on_battery"]:
            return "⚠ " + g["charge_warn"], orange
        if g and g.get("enabled") and g.get("charge_check") == "unsupported" \
                and not b["on_battery"]:
            return ("Charging can't be paused on this board, so the max limit has no "
                    "effect. The board still stops by itself at full (4.2 V).", orange)
        if g and g.get("shutdown_pending"):
            at = g.get("shutdown_at")
            left = f" in {int(max(0, at - time.time())) // 60}:" \
                   f"{int(max(0, at - time.time())) % 60:02d}" if at else ""
            return (f"⚠ Shutting down{left}: {g['shutdown_reason']}. "
                    "Plug in power to cancel.", red)
        if not b["on_battery"]:
            return "", None
        soc = b["soc"]
        limit = g["min"] if g and g.get("enabled") else 15
        if soc <= max(24, limit + 5):
            text, bg = "⚠ Battery critical: plug in power now", red
        elif soc <= 50:
            text, bg = "Battery approaching critical: running on backup power", orange
        else:
            text, bg = "Running on backup battery power", orange
        if g and g.get("enabled") and g.get("outage_minutes") and g.get("on_battery_since"):
            left = g["outage_minutes"] * 60 - (time.time() - g["on_battery_since"])
            if left > 0:
                when = f"{int(left)} s" if left < 60 else fmt_hours(left / 3600)
                text += f" · auto shutdown in {when} if power stays off"
        elif not (g and g.get("enabled")):
            text += " · no auto shutdown set (UPS settings)"
        if g and g.get("saver_active"):
            text += f" · Battery saver ON ({g['saver_active']})"
        elif g and g.get("saver_enabled"):
            text += f" · battery saver starts at {g['saver_below']} %"
        return text, bg

    # ----- UPS settings window -----
    def open_ups_settings(self):
        old = getattr(self, "ups_win", None)
        if old is not None and old.winfo_exists():
            old.destroy()  # Wayland often can't raise a hidden window: open a fresh one
        try:
            self._build_ups_settings()
        except Exception as e:  # noqa: BLE001 - show it instead of failing silently
            if getattr(self, "ups_win", None) is not None and self.ups_win.winfo_exists():
                self.ups_win.destroy()
            self.msg.config(text=f"✘ Couldn't open UPS settings: {type(e).__name__}: {e}",
                            fg="#ef5350")

    def _build_ups_settings(self):
        cfg = load_battery_cfg()
        top = self.ups_win = tk.Toplevel(self.root)
        top.geometry(f"+{self.root.winfo_rootx() + 30}+{max(0, self.root.winfo_rooty() - 10)}")
        top.title("UPS & battery settings")
        top.configure(bg=BG)
        top.transient(self.root)
        win, foot = make_scrollable(top, footer=True, padx=16, pady=12)  # widgets go here

        enabled = tk.BooleanVar(value=cfg["enabled"] or not os.path.exists(BATTERY_CFG))
        def step5(v, lo, hi, default):
            try:
                return int(min(hi, max(lo, round(float(v) / 5) * 5)))
            except (TypeError, ValueError):
                return default
        vmax = tk.IntVar(value=step5(cfg["max"], 50, 100, 80))
        vmin = tk.IntVar(value=step5(cfg["min"], 5, 50, 20))
        outage_lbl = dict(OUTAGE_CHOICES)
        warn_lbl = dict(WARN_CHOICES)
        def nearest(choices, value):
            keys = [k for k, _ in choices]
            v = value if isinstance(value, (int, float)) else keys[0]
            return min(keys, key=lambda k: abs(k - v))
        vout = tk.StringVar(value=outage_lbl[nearest(OUTAGE_CHOICES, cfg["outage_minutes"])])
        vwarn = tk.StringVar(value=warn_lbl[nearest(WARN_CHOICES, cfg["warn_minutes"])])

        self._label(win, "Battery protection", 13, bold=True).pack(anchor="w")
        self._label(win, "Keeping the battery between about 20 % and 80 % gives it many more "
                         "charge cycles. These settings keep working in the background, "
                         "even when this window is closed.", 9, MUTED, wraplength=380,
                    justify="left").pack(anchor="w", pady=(2, 8))
        tk.Checkbutton(win, text="Turn on battery protection", variable=enabled, bg=BG,
                       fg=FG, selectcolor=CARD, activebackground=BG, activeforeground=FG,
                       font=("DejaVu Sans", 10, "bold")).pack(anchor="w")

        def slider(title, var, lo, hi, hint):
            box = tk.Frame(win, bg=CARD, padx=10, pady=6)
            box.pack(fill="x", pady=(8, 0))
            top = tk.Frame(box, bg=CARD)
            top.pack(fill="x")
            self._label(top, title, 10).pack(side="left")
            val = self._label(top, "", 12, bold=True)
            val.pack(side="right")
            scale = tk.Scale(box, from_=lo, to=hi, resolution=5, orient="horizontal",
                             variable=var, showvalue=False, length=360, bg=CARD,
                             troughcolor="#3a3d45", highlightthickness=0, bd=0,
                             activebackground="#90caf9")
            scale.pack(fill="x")
            self._label(box, hint, 8, MUTED, wraplength=360, justify="left").pack(anchor="w")
            return val, scale

        max_val, max_scale = slider("Stop charging at", vmax, 50, 100,
                                    "80 % = longest battery life · 100 % = no limit")
        min_val, min_scale = slider("Safe shutdown when battery drops to", vmin, 5, 50,
                                    "Only when running on battery. 20 % is a good choice.")

        def menu(title, var, choices):
            row = tk.Frame(win, bg=BG)
            row.pack(fill="x", pady=(8, 0))
            self._label(row, title, 10).pack(side="left")
            labels = [label for _, label in choices]
            self.picker_button(row, var, max(len(x) for x in labels) + 1,
                               lambda btn: self.choose(
                                   title, [(x, x) for x in labels], var.set,
                                   anchor=btn, current=var.get())).pack(side="right")

        menu("If the power goes out, shut down after", vout, OUTAGE_CHOICES)
        menu("Warning before shutting down", vwarn, WARN_CHOICES)

        # POWER_OFF_ON_HALT: the Pi's 5 V outputs after it has shut down
        usb_choices = [(False, "Turn off · saves battery"), (True, "Stay on · powers USB")]
        usb_lbl = dict(usb_choices)
        vusb = tk.StringVar(value=usb_lbl[bool(cfg["usb_5v_when_off"])])
        menu("After shutdown, the Pi's 5 V outputs", vusb, usb_choices)
        self._label(win, "USB ports and GPIO 5 V pins. 'Turn off' is Geekworm's setting "
                         "(POWER_OFF_ON_HALT=1); 'Stay on' keeps USB devices powered from "
                         "the battery. Takes effect after a reboot.", 8, MUTED,
                    wraplength=380, justify="left").pack(anchor="w")

        # Battery saver
        vsaver_on = tk.BooleanVar(value=cfg["saver_enabled"] or not os.path.exists(BATTERY_CFG))
        vsaver = tk.IntVar(value=step5(cfg["saver_below"], 15, 100, 50))
        saver_choices = [(n, f"{n} · {PLANS[n]['max_freq_mhz'] / 1000:.1f} GHz · "
                             f"{PLANS[n]['governor']}") for n, _ in SAVER_PLANS]
        saver_lbl = dict(saver_choices)
        vsplan = tk.StringVar(value=saver_lbl.get(cfg["saver_plan"], saver_lbl["Underclock"]))
        self._label(win, "Battery saver", 12, bold=True).pack(anchor="w", pady=(14, 0))
        tk.Checkbutton(win, text="Save power on battery so it lasts the day", variable=vsaver_on, bg=BG, fg=FG,
                       selectcolor=CARD, activebackground=BG, activeforeground=FG,
                       font=("DejaVu Sans", 10)).pack(anchor="w")
        saver_val, saver_scale = slider("Start battery saver at", vsaver, 15, 100,
                                        "100 % = as soon as the power goes out. Off again when plugged in.")
        menu("Saver mode", vsplan, saver_choices)
        self._label(win, "Underclock gives the longest runtime; Eco is a bit faster.", 8,
                    MUTED).pack(anchor="w")

        summary = self._label(win, "", 9, "#90caf9", wraplength=380, justify="left")
        summary.pack(anchor="w", pady=(10, 4))

        def refresh_summary(*_):
            # keep at least 20 % between the limits, pushing the other slider
            if vmax.get() - vmin.get() < 20:
                if refresh_summary.last == "min":
                    vmax.set(min(100, vmin.get() + 20))
                else:
                    vmin.set(max(5, vmax.get() - 20))
            max_val.config(text="no limit" if vmax.get() >= 100 else f"{vmax.get()} %")
            min_val.config(text=f"{vmin.get()} %")
            if vsaver.get() <= vmin.get():  # saver must start before the shutdown level
                vsaver.set(min(100, vmin.get() + 5))
            saver_val.config(text="when power goes out" if vsaver.get() >= 100
                             else f"{vsaver.get()} %")
            saver_scale.config(state="normal" if vsaver_on.get() else "disabled")
            plan_short = vsplan.get().split(" ·")[0]
            saver_txt = ("" if not vsaver_on.get() else
                         f" Battery saver ({plan_short}) starts "
                         + ("as soon as the power goes out." if vsaver.get() >= 100
                            else f"at {vsaver.get()} % on battery."))
            on = enabled.get()
            for w in (max_scale, min_scale):
                w.config(state="normal" if on else "disabled")
            if not on:
                summary.config(text="Protection off: charges to 100 % and never shuts down "
                                    "by itself." + saver_txt + usb_txt())
                return
            parts = [("Charges to 100 %." if vmax.get() >= 100 else
                      f"Charges to {vmax.get()} %, starts again below "
                      f"{vmax.get() - RESUME_GAP} %."),
                     f"On battery, shuts down safely at {vmin.get()} %"]
            o = [m for m, l in OUTAGE_CHOICES if l == vout.get()][0]
            if o:
                parts[-1] += f" or after {vout.get()} without power"
            parts[-1] += f", with a {vwarn.get()} warning. Plugging in cancels it."
            summary.config(text=" ".join(parts) + saver_txt + usb_txt())
        refresh_summary.last = "max"

        def touched(which):
            def cb(*_):
                refresh_summary.last = which
                refresh_summary()
            return cb
        max_scale.config(command=touched("max"))
        min_scale.config(command=touched("min"))
        saver_scale.config(command=touched("saver"))
        def usb_txt():
            return (" After shutdown, USB / 5 V stay powered." if vusb.get() == usb_lbl[True]
                    else "")
        for v in (vout, vwarn, enabled, vsaver_on, vsplan, vusb):
            v.trace_add("write", refresh_summary)
        refresh_summary()

        def recommended():
            enabled.set(True)
            vmax.set(80)
            vmin.set(20)
            vsaver_on.set(True)
            vsaver.set(50)
            vsplan.set(saver_lbl["Underclock"])
            refresh_summary()

        def save():
            new = {"enabled": enabled.get(), "max": vmax.get(), "min": vmin.get(),
                   "outage_minutes": [m for m, l in OUTAGE_CHOICES if l == vout.get()][0],
                   "warn_minutes": [m for m, l in WARN_CHOICES if l == vwarn.get()][0],
                   "saver_enabled": vsaver_on.get(), "saver_below": vsaver.get(),
                   "saver_plan": [p for p, l in saver_choices if l == vsplan.get()][0],
                   "usb_5v_when_off": vusb.get() == usb_lbl[True]}
            if self.blocked("UPS settings", lock_during_test=False):
                return
            self.set_busy("saving UPS settings")
            self.msg.config(text="Saving UPS settings… (enter your password if asked)",
                            fg=MUTED)
            threading.Thread(target=lambda: setattr(
                self, "result", ("UPS settings", False) +
                run_root(["--ups-config", json.dumps(new)])), daemon=True).start()
            self.root.after(200, self.poll_result)
            top.destroy()

        g = read_guard_status()
        topup_on = bool(g and g.get("topup"))

        def topup():
            if self.blocked("charging", lock_during_test=False):
                return
            self.set_busy("changing charging")
            threading.Thread(target=lambda: setattr(
                self, "result", ("Charging", False) +
                run_root(["--charge-full", "off" if topup_on else "on"])),
                daemon=True).start()
            self.root.after(200, self.poll_result)
            top.destroy()
        if cfg["enabled"] and cfg["max"] < 100:
            row = tk.Frame(win, bg=BG)
            row.pack(fill="x", pady=(10, 0))
            tk.Button(row, text=("Stop charging to 100 %" if topup_on
                                 else "Charge to 100 % once"), command=topup,
                      relief="flat", bg=CARD, fg=FG, activebackground="#555",
                      activeforeground=FG, font=("DejaVu Sans", 9)).pack(side="left", ipadx=6)
            self._label(row, "e.g. before a trip; the limit comes back when it's full", 8,
                        MUTED).pack(side="left", padx=(8, 0))

        btns = tk.Frame(foot, bg=BG)
        btns.pack(fill="x", pady=(4, 12))
        for text, cmd, bg in (("Save", save, "#43a047"), ("Cancel", top.destroy, CARD),
                              ("Recommended (20–80 %)", recommended, CARD)):
            tk.Button(btns, text=text, command=cmd, relief="flat", bg=bg, fg=FG,
                      activebackground="#555", activeforeground=FG,
                      font=("DejaVu Sans", 9, "bold" if text == "Save" else "normal")) \
                .pack(side="right", padx=(6, 0), ipadx=6, ipady=2)
        keep_on_screen(top)

    def draw_graph(self):
        c = self.canvas
        c.delete("all")
        w, h, lo, hi = 440, 70, 30.0, 85.0

        def y(v):
            return h - 4 - (min(max(v, lo), hi) - lo) / (hi - lo) * (h - 8)

        for mark in (50, 70):
            c.create_line(0, y(mark), w, y(mark), fill="#3a3d45", dash=(2, 3))
            c.create_text(4, y(mark) - 7, text=f"{mark}°", fill=MUTED, anchor="w",
                          font=("DejaVu Sans", 7))
        if len(self.history) > 1:
            step = w / (self.HISTORY - 1)
            x0 = w - step * (len(self.history) - 1)
            pts = []
            for i, v in enumerate(self.history):
                pts += [x0 + i * step, y(v)]
            c.create_line(*pts, fill=temp_color(self.history[-1]), width=2, smooth=True)

    # ----- actions -----
    def _show_error(self, exc_type, exc, tb):
        import traceback
        traceback.print_exception(exc_type, exc, tb)
        try:
            self.msg.config(text=f"✘ Something went wrong: {exc_type.__name__}: {exc}",
                            fg="#ef5350")
        except Exception:  # noqa: BLE001
            pass

    def set_busy(self, task):
        self.busy, self.busy_task, self.busy_since = True, task, time.time()

    def blocked(self, what, lock_during_test=True):
        """Return True (and say why) if this click can't be done right now."""
        if self.busy:
            self.msg.config(text=f"⏳ Still waiting for: {self.busy_task}. If a password "
                                 "window is open, it may be hidden behind this window.",
                            fg="#ffa726")
            self.root.bell()
            self.sync_governors()
            return True
        st = load_state()
        if lock_during_test and st and st.get("phase") in ("pending", "running"):
            self.msg.config(text=f"Locked: {what} can't change while the sweet spot test is "
                                 "running, or it would spoil the test. Click 'Find sweet "
                                 "spot…' to cancel the test first.", fg="#ffa726")
            self.root.bell()
            self.sync_governors()
            return True
        return False

    def on_plan(self, name):
        if self.blocked("the plan"):
            return
        if PLANS[name].get("warn") and not messagebox.askyesno(
                "Overclock",
                f"{name} raises CPU speed and voltage.\n\n"
                "• Use the official 27 W power supply\n"
                "• If the Pi freezes or won't boot, put the SD card in a PC and\n"
                "  delete the 'pi5-energyplan' block at the end of config.txt\n\n"
                "Continue?"):
            return
        self.set_busy(f"applying {name}")
        for b in self.buttons.values():
            b.config(state="disabled")
        self.msg.config(text=f"Applying {name}… (enter your password if asked)", fg=MUTED)
        save = self.save_var.get()
        threading.Thread(target=lambda: setattr(self, "result",
                         (name, save) + run_privileged(name, save)), daemon=True).start()
        self.root.after(200, self.poll_result)

    def on_governor(self, name, gov):
        if self.blocked("the governor"):
            return
        if gov == PLANS[name]["governor"]:
            self.msg.config(text=f"{name} already uses {gov}.", fg=MUTED)
            return
        self.gov_vars[name].set(gov)  # show the choice while saving
        self.set_busy(f"saving the {name} governor")
        self.msg.config(text=f"Setting {name} to {gov}… (enter your password if asked)",
                        fg=MUTED)
        threading.Thread(target=lambda: setattr(
            self, "result", (f"{name}: {gov}", False) +
            run_root(["--set-governor", name, gov])), daemon=True).start()
        self.root.after(200, self.poll_result)

    def on_fan(self, name, profile):
        if self.blocked("the fan profile"):
            return
        if profile == fan_profile_of(PLANS[name]["fan"]):
            self.msg.config(text=f"{name} already uses the {profile} fan.", fg=MUTED)
            return
        self.fan_vars[name].set(profile)
        self.set_busy(f"saving the {name} fan profile")
        self.msg.config(text=f"Setting {name} fan to {profile}… (enter your password if "
                             "asked)", fg=MUTED)
        threading.Thread(target=lambda: setattr(
            self, "result", (f"{name} fan: {profile}", False) +
            run_root(["--set-fan", name, profile])), daemon=True).start()
        self.root.after(200, self.poll_result)

    def sync_governors(self):
        """Show what's really saved (reverts a dropdown if saving failed)."""
        reload_plan_settings()
        for name, var in getattr(self, "gov_vars", {}).items():
            var.set(PLANS[name]["governor"])
        for name, var in getattr(self, "fan_vars", {}).items():
            var.set(fan_profile_of(PLANS[name]["fan"]))

    def poll_result(self):
        if self.result is None:
            waited = time.time() - getattr(self, "busy_since", time.time())
            if waited > 6:
                self.msg.config(text=f"⏳ {self.busy_task[:1].upper()}{self.busy_task[1:]}… "
                                     f"{int(waited) // 60}:{int(waited) % 60:02d}. Enter your "
                                     "password in the password window (it may be behind "
                                     "this window).", fg="#ffa726")
            self.root.after(200, self.poll_result)
            return
        name, saved, ok, text = self.result
        self.result, self.busy = None, False
        self.sync_governors()
        for b in self.buttons.values():
            b.config(state="normal")
        self.msg.config(text=(f"✔ {name}\n" if ok else "✘ ") + text,
                        fg="#66bb6a" if ok else "#ef5350")
        if ok and saved and messagebox.askyesno(
                "Reboot?", f"{name} saved.\nReboot now to apply the full fan curve?"):
            self.on_reboot(confirm=False)

    def on_autotest(self):
        if self.blocked("the sweet spot test", lock_during_test=False):
            return
        st = load_state()
        if st and st.get("phase") in ("pending", "running"):
            if messagebox.askyesno("Sweet spot test",
                                   f"A test is running: {autotest_summary()}\n\n"
                                   "Cancel it? Your normal settings come back on reboot."):
                self.set_busy("cancelling the sweet spot test")
                threading.Thread(target=lambda: setattr(
                    self, "result", ("Sweet spot test cancelled", False) +
                    run_root(["--autotest-cancel"])), daemon=True).start()
                self.root.after(200, self.poll_result)
            return
        if not messagebox.askyesno(
                "Find sweet spot",
                "The Pi will reboot several times and stress-test each overclock\n"
                "(2.6 → 2.8 → 3.0 GHz). It takes about 20–40 minutes.\n\n"
                "• Save your work and don't use the Pi during the test\n"
                "• Use the official 27 W power supply\n"
                "• If it freezes for more than a minute, unplug and replug power.\n"
                "  It always comes back with your normal settings.\n\n"
                "At the end the best stable setting is saved as the\n"
                f"'{SWEET_SPOT}' plan and turned on.\n\nStart now?"):
            return
        self.set_busy("starting the sweet spot test")
        self.msg.config(text="Starting sweet spot test… the Pi will reboot shortly.",
                        fg=MUTED)
        threading.Thread(target=lambda: setattr(
            self, "result", ("Sweet spot test", False) +
            run_root(["--autotest", "--yes"], timeout=900)), daemon=True).start()
        self.root.after(200, self.poll_result)

    def on_tray(self):
        install_tray()
        self.msg.config(text="✔ Battery icon added to the panel (starts at every login).",
                        fg="#66bb6a")

    def on_reboot(self, confirm=True):
        if confirm and not messagebox.askyesno("Reboot", "Reboot the Raspberry Pi now?"):
            return
        subprocess.Popen(["systemctl", "reboot"])

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------- #
#  Extras
# --------------------------------------------------------------------------- #
LAUNCHER = os.path.expanduser("~/.local/share/applications/pi5-energyplan.desktop")


def install_launcher():
    apps = os.path.dirname(LAUNCHER)
    os.makedirs(apps, exist_ok=True)
    path = LAUNCHER
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Pi 5 Energy Plan\n"
                "Comment=Switch fan and CPU energy plans\n"
                f"Exec={sys.executable} {os.path.abspath(__file__)}\n"
                "Icon=applications-system\nTerminal=false\n"
                "Categories=System;Settings;\n")
    print(f"Menu entry created: {path} (look under Preferences/System Tools)")


def fmt_version(v):
    return ".".join(map(str, v)) if v != (0,) else "an older one without a number"


def refresh_old_copies():
    """Bring older copies of this script that the menu entry / panel icon pointed at
    up to date (the old copy is kept next to it as .previous). Otherwise opening
    one later would run old code."""
    me = os.path.abspath(__file__)
    msgs = []
    for entry in (LAUNCHER, AUTOSTART):
        m = re.search(r"^Exec=.*?(\S*pi5_energyplan\.py)", read(entry, "") or "", re.M)
        old = os.path.abspath(m.group(1)) if m else None
        if not old or old in (me, INSTALLED_SCRIPT) or not os.path.isfile(old) or \
                BLOCK_START not in (read(old, "") or "") or \
                version_of(old) >= version_of(me) or not os.access(old, os.W_OK):
            continue
        shutil.copy2(old, old + ".previous")
        shutil.copy2(me, old)
        msgs.append(f"Older copy updated to version {VERSION}: {old}")
    return msgs


def install_desktop():
    """User part of an install or update: older copies updated, then the menu entry
    and panel icon pointing at this copy (each always the same single file)."""
    msgs = refresh_old_copies()
    install_launcher()
    install_tray()
    return msgs


def refresh_desktop():
    """User part of an update: older copies updated, and the menu entry / panel icon
    refreshed if this user has them (nothing is added that wasn't there)."""
    msgs = refresh_old_copies()
    if os.path.exists(LAUNCHER):
        install_launcher()
        msgs.append("Menu entry refreshed")
    if os.path.exists(AUTOSTART):
        install_tray()
        msgs.append("Battery icon restarted with the new version")
    return msgs


def update_root():
    """Root part of an update. Every step overwrites in place, so running it
    any number of times never adds anything twice."""
    msgs = []
    changed = False
    installed = [p for p in (SERVICE_PATH, GUARD_UNIT_PATH) if os.path.exists(p)]
    if installed and installed_is_newer():
        msgs.append(f"A newer version ({fmt_version(version_of(INSTALLED_SCRIPT))}) is "
                    f"installed: this older copy ({VERSION}) was not installed")
    elif installed and install_copy():
        msgs.append(f"System copy updated to version {VERSION}: {INSTALLED_SCRIPT}")
        changed = True
    reload_needed = False
    for path, unit in ((SERVICE_PATH, SERVICE_UNIT), (GUARD_UNIT_PATH, GUARD_UNIT)):
        if os.path.exists(path) and read(path) != unit.strip():
            write(path, unit)
            reload_needed = True
            msgs.append(f"Service file refreshed: {os.path.basename(path)}")
    if reload_needed:
        subprocess.run(["systemctl", "daemon-reload"])
    if os.path.exists(GUARD_UNIT_PATH) and (changed or reload_needed):
        subprocess.run(["systemctl", "try-restart", GUARD_SERVICE], capture_output=True)
        msgs.append("Battery guard restarted with the new version")
    old_pin = [f for f in OLD_CHARGE_PIN_FILES if os.path.exists(f)]
    if old_pin:
        for f in old_pin:
            os.remove(f)
        msgs.append("Old charge-pin data removed (an older version could learn it wrong); "
                    "it is checked again")
        changed = True
        if os.path.exists(GUARD_UNIT_PATH):
            subprocess.run(["systemctl", "try-restart", GUARD_SERVICE], capture_output=True)
    plan = find_plan(saved_plan() or "")
    st = load_state()
    if plan and not (st and st.get("phase") in ("pending", "running")):
        path = config_path()
        before = read(path, "")
        write_config(plan)
        msgs += CONFIG_NOTES
        if read(path, "") != before and not CONFIG_NOTES:
            msgs.append(f"config.txt tidied (one '{plan}' block, no duplicates)")
    return msgs or ["System part already up to date"]


def update_all():
    """User part (menu entry, panel icon) plus the root part when needed."""
    msgs = refresh_desktop()
    if any(os.path.exists(p) for p in (SERVICE_PATH, GUARD_UNIT_PATH)) or saved_plan():
        if os.geteuid() == 0:
            msgs += update_root()
        elif sys.stdin.isatty():
            r = subprocess.run(["sudo", sys.executable, os.path.abspath(__file__),
                                "--update-root"])
            if r.returncode != 0:
                msgs.append("System part not updated (sudo failed)")
        else:
            ok, text = run_root(["--update-root"])
            msgs.append(text if ok else f"System part not updated: {text}")
    return msgs or ["Nothing installed yet, nothing to update"]


def charge_debug():
    """Everything needed to see why the battery is or isn't charging."""
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError) as e:
            return f"({e})"
    b = Battery().read()
    print("Battery      :", battery_line(b))
    print("Adapter (GPIO6) :", run(["pinctrl", "get", str(PLD_GPIO)]) or "?")
    print("Charge pin (GPIO16):", run(["pinctrl", "get", str(CHARGE_GPIO)]) or "?")
    print("  normal board: 'pd | lo' or 'dl | lo' = charging allowed, 'dh | hi' = paused")
    print("  Paused but the % still rises? Watch the cell voltage for 15-20 min: falling or")
    print("  flat = charging really stopped (the gauge's % lags behind); rising = still charging")
    print("Guard service:", run(["systemctl", "is-active", GUARD_SERVICE]) or "?")
    g = read_guard_status()
    if g:
        keys = ("enabled", "max", "limit", "topup", "min", "charging_allowed", "charge_check",
                "charge_probe",
                "charge_warn", "charge_note", "volt_high")
        for k in keys:
            print(f"  {k:17}: {g.get(k)}")
    else:
        print("  (guard not running or no status yet)")
    print("Learned pin data:", read(CHARGE_PIN_FILE, "(none)"))
    print("Settings     :", read(BATTERY_CFG, "(none)"))
    pm = read_pmic()
    if pm.get("ext5v"):
        print(f"Pi input     : {pm['ext5v']:.2f} V, Pi uses {pm.get('watts') or 0:.1f} W")
    print("\nLast guard messages:")
    print(run(["journalctl", "-u", GUARD_SERVICE, "-n", "25", "--no-pager", "-o", "cat"])
          or "(none, try with sudo)")


def print_status():
    s = get_status()
    t = s["temp"]
    print(f"Temperature : {t:.1f} °C" if t is not None else "Temperature : ?")
    print(f"Fan         : {fmt_fan(s)}")
    print(f"CPU         : {fmt_freq(s)}")
    print(f"Governor    : {s['governor']}")
    gpu = gpu_clock_mhz()
    print(f"GPU (3D)    : {f'{gpu} MHz' if gpu else '?'}")
    print(f"Power       : {s['throttled']}")
    print(f"Saved plan  : {s['plan'] or 'none'}")
    print(f"Sweet spot  : {autotest_summary()}")
    print(f"Battery     : {battery_line(Battery().read())}")
    print(f"Pi power    : {fmt_pmic(read_pmic())}")


def fmt_pmic(pm):
    parts = []
    if pm.get("ext5v"):
        parts.append(f"input {pm['ext5v']:.2f} V")
    if pm.get("watts"):
        parts.append(f"uses {pm['watts']:.1f} W")
    if pm.get("core_v") is not None and pm.get("core_a") is not None:
        parts.append(f"CPU core {pm['core_v']:.3f} V {pm['core_a']:.2f} A")
    return " · ".join(parts) or "? (vcgencmd pmic_read_adc not available)"


def monitor_report(bat):
    """Full UPS + Pi report, like Geekworm's qtx120xTerminal.py but read-only:
    it never changes charging or schedules a shutdown (the battery guard does that)."""
    b, g, s = bat.read(), read_guard_status(), get_status()
    out = [time.strftime("==== X120x UPS ====================  %H:%M:%S")]
    if not b["ok"]:
        out.append(f"UPS          : {b['error']}")
    else:
        out.append(f"Battery      : {b['soc']:.1f} % ({battery_health(b['volt'])}) · "
                   f"{b['volt']:.3f} V per cell")
        out.append(f"State        : {b['state']}" + (f" · {b['eta']}" if b["eta"] else ""))
        out.append("Mains power  : " + {1: "OK (adapter connected)",
                                         0: "LOST, running on battery"}.get(
                                             b["ac"], "unknown (GPIO 6 not readable)"))
        if not g:
            charge = "board default (battery guard not running)"
        elif g.get("charging_allowed") is False:
            charge = f"paused at the {g['max']}% limit"
        else:
            charge = "allowed" + (" (charging to 100 % once)" if g.get("topup") else
                                  f", limit {g['max']}%" if g.get("enabled") and
                                  g["max"] < 100 else "")
        out.append(f"Charging     : {charge}")
        if g and g.get("enabled"):
            prot = f"on (shutdown at {g['min']}% on battery"
            if g.get("outage_minutes"):
                prot += f", or after {dict(OUTAGE_CHOICES).get(g['outage_minutes'], '?')}" \
                        " without power"
            out.append(f"Protection   : {prot})")
        else:
            out.append("Protection   : off (no auto shutdown)")
        warn = App.battery_banner(b, g)[0]
        if warn:
            out.append(f"WARNING      : {warn}")
    t = s["temp"]
    out += ["==== Raspberry Pi 5 ===============",
            f"Pi power     : {fmt_pmic(read_pmic())}",
            f"CPU          : {f'{t:.1f} °C' if t is not None else '? °C'} · {fmt_freq(s)} · "
            f"{s['governor'] or '?'}",
            f"GPU (3D)     : {gpu_clock_mhz() or '?'} MHz",
            f"Fan          : {fmt_fan(s)}",
            f"Power        : {s['throttled'] or '?'}",
            f"Plan         : {s['plan'] or 'none'}"]
    return "\n".join(out)


def monitor(interval):
    bat = Battery()
    tty = sys.stdout.isatty()
    try:
        while True:
            report = monitor_report(bat)
            print(("\033[H\033[2J" if tty else "") + report + "\n", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="Energy plans for Raspberry Pi 5 + Active Cooler")
    ap.add_argument("--apply", metavar="PLAN", help="apply a plan (needs sudo)")
    ap.add_argument("--no-config", action="store_true",
                    help="with --apply: change live settings only, don't edit config.txt")
    ap.add_argument("--list", action="store_true", help="list plans")
    ap.add_argument("--status", action="store_true", help="show temperature, fan, CPU")
    ap.add_argument("--install-launcher", action="store_true",
                    help="add a desktop menu entry for the GUI")
    ap.add_argument("--uninstall-service", action="store_true",
                    help="stop restoring the plan at boot (needs sudo)")
    ap.add_argument("--uninstall-guard", action="store_true",
                    help="remove the UPS battery guard service (needs sudo)")
    ap.add_argument("--autotest", action="store_true",
                    help="find the highest stable overclock (reboots several times)")
    ap.add_argument("--minutes", type=int, default=5,
                    help="with --autotest: stress test minutes per step (default 5)")
    ap.add_argument("--max-temp", type=int, default=80,
                    help="with --autotest: fail a step above this °C (default 80)")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--autotest-status", action="store_true",
                    help="show sweet spot test progress and results")
    ap.add_argument("--autotest-cancel", action="store_true",
                    help="stop a running sweet spot test")
    ap.add_argument("--autotest-resume", action="store_true",
                    help="continue a sweet spot test that stopped (reboots)")
    ap.add_argument("--charge-debug", action="store_true",
                    help="show why the battery is or isn't charging")
    ap.add_argument("--battery", action="store_true",
                    help="live battery readout in the terminal (every 3 s)")
    ap.add_argument("--monitor", nargs="?", type=float, const=3, metavar="SECONDS",
                    help="full UPS and Pi report in the terminal, refreshed every 3 s")
    ap.add_argument("--charge-full", choices=("on", "off"),
                    help="charge to 100 %% once, ignoring the limit until full (sudo)")
    ap.add_argument("--tray", action="store_true", help="run the panel battery icon")
    ap.add_argument("--install-tray", action="store_true",
                    help="add the battery icon to the panel and start it at login")
    ap.add_argument("--install-desktop", action="store_true",
                    help="menu entry + panel icon, and update older copies they used")
    ap.add_argument("--refresh-desktop", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="store_true", help="print the version")
    ap.add_argument("--ups-config", metavar="JSON",
                    help='battery limits, e.g. \'{"enabled":true,"min":20,"max":80}\' (sudo)')
    ap.add_argument("--set-governor", nargs=2, metavar=("PLAN", "GOVERNOR"),
                    help="choose the CPU governor a plan uses (sudo)")
    ap.add_argument("--set-fan", nargs=2, metavar=("PLAN", "PROFILE"),
                    help=f"choose a plan's fan profile: {', '.join(FAN_PROFILES)} (sudo)")
    ap.add_argument("--eeprom-setup", action="store_true",
                    help="set Geekworm's bootloader settings: PSU_MAX_CURRENT=5000 and "
                         "POWER_OFF_ON_HALT (from --usb-5v-when-off, default 1) (sudo)")
    ap.add_argument("--usb-5v-when-off", choices=("on", "off"),
                    help="keep the Pi's 5 V outputs (USB, GPIO 5 V) on when it is shut down, "
                         "or turn them off to save battery (sets POWER_OFF_ON_HALT; sudo)")
    ap.add_argument("--update", action="store_true",
                    help="after replacing this file: refresh everything it installed, "
                         "without adding anything twice")
    ap.add_argument("--update-root", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--battery-guard", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--boot", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.set_governor or args.set_fan or args.update_root:
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        try:
            if args.update_root:
                print("\n".join(update_root()))
            elif args.set_fan:
                print(set_plan_fan(*args.set_fan))
            else:
                print(set_plan_governor(*args.set_governor))
        except (ValueError, OSError, subprocess.CalledProcessError) as e:
            sys.exit(f"Error: {e}")
        return
    if args.update:
        for line in update_all():
            print(line)
        return

    if args.ups_config or args.battery_guard:
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        if args.battery_guard:
            # never two guards driving the charge pin / scheduling shutdowns
            if not single_instance("pi5-energyplan-guard"):
                sys.exit("The battery guard is already running")
            battery_guard()
            return
        try:
            print(apply_ups_config(json.loads(args.ups_config)))
        except (ValueError, OSError, subprocess.CalledProcessError) as e:
            sys.exit(f"Error: {e}")
        return

    if args.battery:
        watch_battery()
        return
    if args.monitor is not None:
        monitor(max(1.0, args.monitor))
        return
    if args.usb_5v_when_off:
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        cfg = load_battery_cfg()
        cfg["usb_5v_when_off"] = args.usb_5v_when_off == "on"
        write_json_atomic(BATTERY_CFG, validate_battery_cfg(cfg))
        try:
            print(eeprom_setup(halt_only=True))
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            sys.exit(f"Error: {e}")
        return
    if args.eeprom_setup:
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        try:
            print(eeprom_setup())
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            sys.exit(f"Error: {e}")
        return
    if args.charge_full:
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        try:
            print(set_topup(args.charge_full == "on"))
        except (OSError, RuntimeError) as e:
            sys.exit(f"Error: {e}")
        return
    if args.charge_debug:
        charge_debug()
        return
    if args.tray:
        for _ in range(10):  # an old icon may still be closing after an update
            if single_instance("pi5-energyplan-tray"):
                break
            time.sleep(0.5)
        else:
            sys.exit("Battery icon is already running")
        run_tray()
        return
    if args.install_tray:
        install_tray()
        return
    if args.install_desktop or args.refresh_desktop:
        for line in (install_desktop() if args.install_desktop else refresh_desktop()):
            print(line)
        return
    if args.version:
        print(VERSION)
        return

    if args.autotest_status:
        print(f"Sweet spot: {autotest_summary()}\n")
        print(read(AUTOTEST_LOG, "(no test log yet)"))
        return

    if args.autotest_resume:
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        try:
            resume_autotest()
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Error: {e}")
        return

    if args.boot or args.uninstall_service or args.uninstall_guard or args.autotest or \
            args.autotest_cancel:
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        # stop cleanly (and release the watchdog) on shutdown / systemctl stop
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        if args.boot:
            if not single_instance("pi5-energyplan-boot"):
                sys.exit("The boot task is already running")
            boot_apply()
        elif args.uninstall_service:
            uninstall_service()
        elif args.uninstall_guard:
            uninstall_guard()
        elif args.autotest_cancel:
            cancel_autotest()
        else:
            if not args.yes:
                print("The Pi will reboot several times to test 2.6 → 3.0 GHz "
                      f"({args.minutes} min each, ~20-40 min total).\n"
                      "If it freezes for over a minute, unplug and replug power; it "
                      "returns to your normal settings.")
                if input("Type 'yes' to start: ").strip().lower() != "yes":
                    sys.exit("Not started")
            try:
                start_autotest(max(1, args.minutes), args.max_temp)
            except Exception as e:  # noqa: BLE001
                sys.exit(f"Error: {e}")
        return

    if args.list:
        for name, p in PLANS.items():
            print(f"{name:12} {p['governor']:12} fan {fan_profile_of(p['fan']):8} {p['desc']}")
        return
    if args.status:
        print_status()
        return
    if args.install_launcher:
        install_launcher()
        return
    if args.apply:
        name = find_plan(args.apply)
        if not name:
            sys.exit(f"Unknown plan '{args.apply}'. Options: {', '.join(PLANS)}")
        if os.geteuid() != 0:
            sys.exit("Needs root: run with sudo")
        if PLANS[name].get("warn"):
            print(f"Overclock: {name}. Use the official 27 W supply. If it won't boot, "
                  "remove the pi5-energyplan block from config.txt on another PC.")
        try:
            for line in apply_plan(name, save_config=not args.no_config):
                print(line)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Error: {e}")
        return

    if tk is None:
        sys.exit("tkinter not found. Install it with: sudo apt install python3-tk")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        sys.exit("No desktop display found. Over SSH use --status / --apply PLAN instead.")
    if not single_instance("pi5-energyplan-gui"):
        sys.exit("Energy Plan is already open")
    App().run()


if __name__ == "__main__":
    main()
