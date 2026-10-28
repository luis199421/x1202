"""Simulation tests for pi5_energyplan.py. Run them with run_sim_tests.sh,
which sets up the fake Pi 5 (sysfs, config.txt, fuel gauge, GPIO tools)."""
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "pi5_energyplan.py")
STATE = os.environ.get("FAKE_STATE")
# Safety: only ever run against the fakes, never on a real Pi
if not STATE or not (shutil.which("pinctrl") or "").startswith(os.path.join(HERE, "fakes")):
    sys.exit("Run this through tests/run_sim_tests.sh")

CPUFREQ = "/sys/devices/system/cpu/cpufreq/policy0"
ZONE = "/sys/class/thermal/thermal_zone0"

STOCK_CONFIG = """\
# For more options and information see
# http://rptl.io/configtxt
dtparam=i2c_arm=on
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
arm_64bit=1
disable_overscan=1
arm_boost=1
arm_freq=2600
dtparam=fan_temp0=45000

[pi4]
arm_freq=1800

[cm4]
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[pi5]
over_voltage_delta=10000

[all]
"""

# Raspberry Pi OS's own config.txt, unchanged: note it ends with [cm5] then [all]
PI_OS_CONFIG = """\
# For more options and information see
# http://rptl.io/configtxt
# Some settings may impact device functionality. See link above for details

# Uncomment some or all of these to enable the optional hardware interfaces
#dtparam=i2c_arm=on
#dtparam=i2s=on
#dtparam=spi=on

# Enable audio (loads snd_bcm2835)
dtparam=audio=on

# Automatically load overlays for detected cameras
camera_auto_detect=1

# Automatically load overlays for detected DSI displays
display_auto_detect=1

# Automatically load initramfs files, if found
auto_initramfs=1

# Enable DRM VC4 V3D driver
dtoverlay=vc4-kms-v3d
max_framebuffers=2

# Don't have the firmware create an initial video= setting in cmdline.txt.
# Use the kernel's default instead.
disable_fw_kms_setup=1

# Run in 64-bit mode
arm_64bit=1

# Disable compensation for displays with overscan
disable_overscan=1

# Run as fast as firmware / board allows
arm_boost=1

[cm4]
# Enable host mode on the 2711 built-in XHCI USB controller.
# This line should be removed if the legacy DWC2 controller is required
# (e.g. for USB device mode) or if USB support is not required.
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[all]
"""


def section_of(text, wanted):
    """The [section] a config.txt line is in, as the firmware reads it."""
    section = "all"
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            section = st[1:-1].lower()
        elif st == wanted:
            return section
    return None


def put(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(value) + "\n")


def get(path):
    with open(path) as f:
        return f.read().strip()


def clear(d):
    for p in glob.glob(d + "/*") + glob.glob(d + "/.*"):
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)


def make_pi(governors="conservative ondemand userspace powersave performance schedutil"):
    """A fresh Raspberry Pi 5 with Active Cooler and an X1202."""
    for d in ("/sys/class", "/sys/devices/system/cpu", "/boot", "/etc/systemd/system",
              "/usr/local/bin", "/etc/pi5-energyplan", "/var/lib/pi5-energyplan",
              "/run/pi5-energyplan", "/sys/firmware", "/usr/share/pi5-energyplan",
              "/sys/block", "/etc/sysctl.d",
              "/usr/share/doc/pi5-energyplan", "/usr/share/keyrings",
              "/etc/apt/sources.list.d", "/var/cache/pi5-energyplan", STATE):
        clear(d)
    put(CPUFREQ + "/scaling_available_governors", governors)
    put(CPUFREQ + "/scaling_governor", "ondemand")
    put(CPUFREQ + "/scaling_max_freq", 2400000)
    put(CPUFREQ + "/scaling_cur_freq", 1500000)
    put(CPUFREQ + "/cpuinfo_max_freq", 2400000)
    put(CPUFREQ + "/cpuinfo_min_freq", 1500000)
    os.makedirs("/sys/devices/system/cpu/cpu0")
    os.symlink(CPUFREQ, "/sys/devices/system/cpu/cpu0/cpufreq")
    put(ZONE + "/temp", 52300)
    # Pi 5: four active trips for the fan + one critical
    for i, (kind, t) in enumerate((("active", 50000), ("active", 60000),
                                   ("active", 67500), ("active", 75000),
                                   ("critical", 110000))):
        put(f"{ZONE}/trip_point_{i}_type", kind)
        put(f"{ZONE}/trip_point_{i}_temp", t)
        put(f"{ZONE}/trip_point_{i}_hyst", 5000)
    put("/sys/class/thermal/cooling_device0/type", "pwm-fan")
    put("/sys/class/thermal/cooling_device0/cur_state", 1)
    put("/sys/class/thermal/cooling_device0/max_state", 4)
    put("/sys/class/hwmon/hwmon2/name", "pwmfan")
    put("/sys/class/hwmon/hwmon2/pwm1", 75)
    put("/sys/class/hwmon/hwmon2/fan1_input", 2500)
    os.makedirs("/boot/firmware")
    put("/boot/firmware/config.txt", STOCK_CONFIG.rstrip("\n"))
    put("/sys/firmware/devicetree/base/model", "Raspberry Pi 5 Model B Rev 1.0\x00")
    hw(soc=76.5, volt=3.95, pld=1, pin16="ip pd", throttled="0x0", crate=0, ups=True)


def hw(**changes):
    path = os.path.join(STATE, "hw.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError:
        data = {}
    data.update(changes)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def calls():
    try:
        with open(os.path.join(STATE, "calls.log")) as f:
            return f.read().splitlines()
    except OSError:
        return []


def cli(*args, check=True):
    r = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                       timeout=60)
    if check and r.returncode != 0:
        raise AssertionError(f"{args} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


def load_module():
    spec = importlib.util.spec_from_file_location("ep", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Stop(BaseException):
    pass


class Clock:
    """Stands in for the `time` module so the guard runs hours in milliseconds."""
    def __init__(self, on_sleep):
        self.t, self.on_sleep = 1_000_000.0, on_sleep
        self.strftime = time.strftime

    def time(self):
        return self.t

    monotonic = time

    def sleep(self, s):
        self.t += s
        self.on_sleep()


class Base(unittest.TestCase):
    def setUp(self):
        make_pi()
        self.ep = load_module()

    def no_sleep(self):
        """Skip the boot task's waits. `ep.time` is the shared time module, so undo it
        after the test (other tests need real pauses)."""
        real = time.sleep
        time.sleep = lambda s: None
        self.addCleanup(setattr, time, "sleep", real)

    def config(self):
        return get("/boot/firmware/config.txt")


class TestCli(Base):
    def test_help_list_status(self):
        self.assertIn("--apply", cli("--help").stdout)
        out = cli("--list").stdout
        for name in ("Underclock", "Eco", "Balanced", "Cool", "Max Cool", "Performance",
                     "OC 2.6 GHz", "OC 2.8 GHz", "OC 3.0 GHz"):
            self.assertIn(name, out)
        st = cli("--status").stdout
        self.assertIn("Temperature : 52.3 °C", st)
        self.assertIn("Fan         : level 1/4 · 29% · 2500 rpm", st)
        self.assertIn("CPU         : 1.50 GHz (max 2.4)", st)
        self.assertIn("Power       : OK", st)
        self.assertIn("Saved plan  : none", st)
        self.assertIn("Battery     : 76.5% · Plugged in · 3.95 V", st)

    def test_unknown_plan(self):
        r = cli("--apply", "nope", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Unknown plan", r.stderr)

    def test_find_plan_loose_names(self):
        f = self.ep.find_plan
        self.assertEqual(f("oc28"), "OC 2.8 GHz")
        self.assertEqual(f("OC 3.0"), "OC 3.0 GHz")
        self.assertEqual(f("maxcool"), "Max Cool")
        self.assertEqual(f("cool"), "Cool")
        self.assertEqual(f("bal"), "Balanced")
        self.assertIsNone(f("oc"))      # ambiguous
        self.assertIsNone(f(""))

    def test_throttle_text(self):
        hw(throttled="0x50005")
        self.assertEqual(self.ep.throttle_text(), "NOW: under-voltage, throttled")
        hw(throttled="0x50000")
        self.assertEqual(self.ep.throttle_text(), "OK (issues earlier since boot)")

    def test_pmic(self):
        pm = self.ep.read_pmic()
        self.assertAlmostEqual(pm["ext5v"], 5.13996)
        self.assertAlmostEqual(pm["core_v"], 0.8)
        self.assertGreater(pm["watts"], 0.8)


class TestPlans(Base):
    def test_apply_cool(self):
        out = cli("--apply", "Cool").stdout
        self.assertIn("schedutil governor, max 2400 MHz (applied now)", out)
        self.assertIn("Fan temps: applied now", out)
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "schedutil")
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "2400000")
        trips = [get(f"{ZONE}/trip_point_{i}_temp") for i in range(5)]
        self.assertEqual(trips, ["40000", "50000", "58000", "65000", "110000"])
        self.assertEqual(get(f"{ZONE}/trip_point_0_hyst"), "3000")

        cfg = self.config()
        self.assertTrue(os.path.exists("/boot/firmware/config.txt.energyplan.bak"))
        self.assertEqual(get("/boot/firmware/config.txt.energyplan.bak"),
                         STOCK_CONFIG.rstrip("\n"))
        block = cfg[cfg.index("# >>> pi5-energyplan"):]
        self.assertIn("# plan: Cool\n[pi5]\ndtparam=fan_temp0=40000\n", block)
        self.assertIn("dtparam=fan_temp3_speed=255\narm_freq=2400\n[all]\n# <<<", block)
        # old values in [all]/[pi5] are commented out, [pi4] is left alone
        self.assertIn("#energyplan-disabled# arm_freq=2600", cfg)
        self.assertIn("#energyplan-disabled# dtparam=fan_temp0=45000", cfg)
        self.assertIn("#energyplan-disabled# over_voltage_delta=10000", cfg)
        self.assertIn("[pi4]\narm_freq=1800", cfg)
        self.assertIn("[cm4]\notg_mode=1", cfg)
        # the empty [all] at the end of the stock file is tidied away
        self.assertNotIn("[all]\n\n# >>>", cfg)

        self.assertTrue(os.path.exists("/usr/local/bin/pi5_energyplan.py"))
        unit = get("/etc/systemd/system/pi5-energyplan.service")
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/bin/pi5_energyplan.py --boot",
                      unit)
        self.assertIn("systemctl enable pi5-energyplan.service", calls())
        self.assertIn("Saved plan  : Cool", cli("--status").stdout)

    def test_config_rewrite_is_idempotent(self):
        cli("--apply", "Eco")
        first = self.config()
        cli("--apply", "Eco")
        self.assertEqual(self.config(), first)
        cli("--apply", "Balanced")
        cfg = self.config()
        self.assertEqual(cfg.count("# >>> pi5-energyplan"), 1)
        self.assertEqual(cfg.count("#energyplan-disabled# arm_freq=2600"), 1)
        self.assertIn("# plan: Balanced", cfg)
        # the backup is the original file, not overwritten later
        self.assertEqual(get("/boot/firmware/config.txt.energyplan.bak"),
                         STOCK_CONFIG.rstrip("\n"))

    def test_no_config(self):
        cli("--apply", "Underclock", "--no-config")
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "powersave")
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "1500000")
        self.assertEqual(self.config(), STOCK_CONFIG.rstrip("\n"))
        self.assertFalse(os.path.exists("/etc/systemd/system/pi5-energyplan.service"))

    def test_overclock_needs_reboot(self):
        out = cli("--apply", "oc28").stdout
        self.assertIn("Overclock: OC 2.8 GHz", out)
        self.assertIn("2800 MHz unlocks after reboot", out)
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "2400000")
        cfg = self.config()
        self.assertIn("arm_freq=2800\nover_voltage_delta=50000\nv3d_freq=1050\n", cfg)
        cli("--apply", "OC 3.0 GHz")
        self.assertIn("arm_freq=3000\nover_voltage_delta=50000\nv3d_freq=1100\n",
                      self.config())
        self.assertNotIn("\ngpu_freq=", self.config())

    def test_gpu_overclock_steps_up_with_the_cpu(self):
        gpu = {}
        for plan in ("OC 2.6 GHz", "OC 2.8 GHz", "OC 3.0 GHz"):
            cli("--apply", plan)
            block = self.config().split("# >>> pi5-energyplan")[1]
            gpu[plan] = int(block.split("v3d_freq=")[1].split()[0])
            self.assertEqual(block.count("v3d_freq="), 1)
        self.assertEqual(gpu, {"OC 2.6 GHz": 1000, "OC 2.8 GHz": 1050, "OC 3.0 GHz": 1100})
        self.assertTrue(all(v > 960 for v in gpu.values()))   # above the Pi 5 default
        cli("--apply", "Cool")                                  # no GPU overclock
        self.assertNotIn("v3d_freq=", self.config().split("# >>> pi5-energyplan")[1])
        # a hand-made GPU overclock elsewhere in config.txt can't override the plan
        put("/boot/firmware/config.txt", self.config().replace("[pi5]", "[pi5]\nv3d_freq=1200"))
        cli("--apply", "oc26")
        self.assertIn("#energyplan-disabled# v3d_freq=1200", self.config())
        self.assertIn("GPU (3D)    : 960 MHz", cli("--status").stdout)

    def test_governor_fallback(self):
        put(CPUFREQ + "/scaling_available_governors", "ondemand performance")
        cli("--apply", "Cool", "--no-config")
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "ondemand")

    def test_live_fan_not_writable(self):
        # root ignores file modes, so make the trip files unwritable another way
        for i in range(4):
            os.remove(f"{ZONE}/trip_point_{i}_temp")
            os.mkdir(f"{ZONE}/trip_point_{i}_temp")
        out = cli("--apply", "Cool").stdout
        self.assertIn("kernel doesn't allow live change", out)

    def test_set_governor_and_fan(self):
        cli("--apply", "Eco")
        out = cli("--set-governor", "Eco", "performance").stdout
        self.assertIn("Eco will use the performance governor (applied now)", out)
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "performance")
        out = cli("--set-fan", "eco", "Max").stdout
        self.assertIn("Eco will use the Max fan profile", out)
        self.assertIn("dtparam=fan_temp0=30000", self.config())
        lst = cli("--list").stdout
        self.assertRegex(lst, r"Eco\s+performance\s+fan Max")
        self.assertEqual(json.loads(get("/etc/pi5-energyplan/governors.json")),
                         {"Eco": "performance"})
        r = cli("--set-fan", "Eco", "Turbo", check=False)
        self.assertIn("Unknown fan profile", r.stderr)

    def test_boot_service(self):
        cli("--apply", "Underclock")
        put(CPUFREQ + "/scaling_governor", "ondemand")   # what Linux does on boot
        put(CPUFREQ + "/scaling_max_freq", 2400000)
        self.no_sleep()
        self.ep.boot_apply()
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "powersave")
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "1500000")

    def test_update_is_idempotent(self):
        cli("--apply", "Cool")
        cli("--ups-config", '{"enabled": true}')
        out = cli("--update").stdout
        self.assertIn("System part already up to date", out)
        before = self.config()
        cli("--update")
        self.assertEqual(self.config(), before)


class TestUps(Base):
    def test_ups_config(self):
        out = cli("--ups-config", json.dumps({"enabled": True, "max": 80, "min": 20,
                                             "outage_minutes": 5, "warn_minutes": 2})).stdout
        self.assertIn("Saved: charge to 80%, safe shutdown at 20%, or after 5 minutes "
                      "without power.", out)
        cfg = json.loads(get("/etc/pi5-energyplan/battery.json"))
        self.assertEqual(cfg["outage_minutes"], 5)
        self.assertIn("ExecStart=/usr/bin/python3 /usr/local/bin/pi5_energyplan.py "
                      "--battery-guard", get("/etc/systemd/system/pi5-battery-guard.service"))
        self.assertIn("systemctl restart pi5-battery-guard.service", calls())

    def test_ups_config_rejects_bad_values(self):
        for bad in ({"enabled": True, "max": 60, "min": 50},
                    {"outage_minutes": 7}, {"warn_minutes": 10}, {"saver_plan": "Cool"}):
            r = cli("--ups-config", json.dumps(bad), check=False)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("Error:", r.stderr)

    def test_battery_reading(self):
        b = self.ep.Battery().read()
        self.assertTrue(b["ok"])
        self.assertAlmostEqual(b["soc"], 76.5, places=1)
        self.assertAlmostEqual(b["volt"], 3.95, places=3)
        self.assertEqual(b["ac"], 1)
        self.assertEqual(b["state"], "Plugged in")
        hw(soc=103.2)
        self.assertEqual(self.ep.Battery().read()["soc"], 100.0)   # clamped
        hw(ups=False)
        b = self.ep.Battery().read()
        self.assertFalse(b["ok"])
        self.assertIn("not answering", b["error"])

    def test_battery_time_estimates(self):
        ep = self.ep
        clock = Clock(lambda: None)
        ep.time = clock
        bat = ep.Battery()
        soc = 50.0
        for _ in range(40):                    # charging at 20 %/h
            hw(soc=soc)
            b = bat.read()
            clock.t += 3
            soc += 20 / 3600 * 3
        self.assertEqual(b["state"], "Charging")
        self.assertTrue(b["charging"])
        self.assertRegex(b["eta"], r"Full in ≈ 2 h 2\d min")
        hw(pld=0)
        for _ in range(40):                    # unplugged, losing 30 %/h
            hw(soc=soc)
            b = bat.read()
            clock.t += 3
            soc -= 30 / 3600 * 3
        self.assertEqual(b["state"], "On battery")
        self.assertRegex(b["eta"], r"≈ 1 h [34]\d min left")

    def test_crate_used_before_history(self):
        hw(crate=-30.0, pld=0)
        b = self.ep.Battery().read()
        self.assertEqual(b["state"], "On battery")
        self.assertRegex(b["eta"], r"≈ 2 h 3\d min left")

    def run_guard(self, cfg, steps):
        """steps[i] runs after guard loop i; it can check and change the hardware."""
        ep = self.ep
        os.makedirs("/etc/pi5-energyplan", exist_ok=True)
        ep.write_json_atomic(ep.BATTERY_CFG, ep.validate_battery_cfg(cfg))
        tick = [0]

        def on_sleep():
            i = tick[0]
            tick[0] += 1
            if i >= len(steps):
                raise Stop
            if steps[i]:
                steps[i]()
        ep.time = Clock(on_sleep)
        with self.assertRaises(Stop):
            ep.battery_guard()
        return json.loads(get(ep.GUARD_STATUS))

    def status(self):
        return json.loads(get(self.ep.GUARD_STATUS))

    def test_guard_charge_limit_saver_and_shutdown(self):
        ep = self.ep
        ep.write_config("Cool")
        ep.apply_live("Cool")
        hw(soc=85, volt=4.05)
        S = {}

        def paused():
            self.assertEqual(hw()["pin16"], "op dh")       # charging off
            self.assertIs(self.status()["charging_allowed"], False)
            hw(soc=74)

        def resumed():
            self.assertEqual(hw()["pin16"], "ip pd")       # Pi default = charging on
            hw(pld=0, soc=45, volt=3.70)                   # power cut

        def saver_on():
            self.assertEqual(self.status()["saver_active"], "Underclock")
            self.assertEqual(get(CPUFREQ + "/scaling_governor"), "powersave")
            self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "1500000")
            hw(soc=19, volt=3.55)

        def not_yet():
            self.assertFalse(any(c.startswith("shutdown -h") for c in calls()))

        def shutdown_started():
            sd = [c for c in calls() if c.startswith("shutdown -h")]
            self.assertEqual(len(sd), 1)
            self.assertTrue(sd[0].startswith("shutdown -h +1 Battery guard: battery at 19% "
                                              "(limit 20%)"), sd[0])
            self.assertTrue(self.status()["shutdown_pending"])
            S["n"] = len(calls())

        def still_one():
            self.assertEqual(sum(c.startswith("shutdown -h") for c in calls()), 1)
            hw(pld=1)                                      # power is back

        def cancelled():
            self.assertIn("shutdown -c Power restored: shutdown cancelled", calls())
            st = self.status()
            self.assertFalse(st["shutdown_pending"])
            self.assertIsNone(st["saver_active"])
            self.assertEqual(get(CPUFREQ + "/scaling_governor"), "schedutil")
            self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "2400000")

        self.run_guard({"enabled": True, "max": 80, "min": 20, "saver_enabled": True,
                        "saver_below": 50, "saver_plan": "Underclock"},
                       [paused, resumed, None, saver_on, not_yet, not_yet, shutdown_started,
                        still_one, cancelled])
        self.assertEqual(hw()["pin16"], "ip pd")   # guard always leaves charging on

    def test_guard_outage_timer(self):
        hw(pld=0, soc=90, volt=4.0)
        steps = [None] * 19 + [lambda: self.assertFalse(
            any(c.startswith("shutdown -h") for c in calls()))] + [None]
        self.run_guard({"enabled": True, "outage_minutes": 1, "warn_minutes": 5}, steps)
        sd = [c for c in calls() if c.startswith("shutdown -h")]
        self.assertEqual(len(sd), 1)
        self.assertIn("+5 Battery guard: power out for 1 min", sd[0])

    def test_guard_low_voltage_floor(self):
        hw(pld=0, soc=60, volt=3.25)        # % still looks fine, the cells don't
        self.run_guard({"enabled": True}, [None] * 4)
        sd = [c for c in calls() if c.startswith("shutdown -h")]
        self.assertEqual(len(sd), 1)
        self.assertIn("battery voltage low (3.25 V)", sd[0])

    def test_guard_never_shuts_down_on_mains(self):
        hw(pld=1, soc=5, volt=3.25)
        self.run_guard({"enabled": True}, [None] * 6)
        self.assertFalse(any(c.startswith("shutdown") for c in calls()))

    def test_guard_disabled_keeps_charging_on(self):
        hw(soc=95, pin16="op dh")
        self.run_guard({"enabled": False}, [None] * 3)
        self.assertEqual(hw()["pin16"], "ip pd")
        self.assertFalse(any(c.startswith("shutdown") for c in calls()))

    def test_charge_check_learns_inverted_pin(self):
        cc = self.ep.ChargeCheck(log=lambda m: None)
        self.assertEqual(cc.pin_args(False), ["op", "dh"])
        self.assertEqual(cc.pin_args(True), ["ip", "pd"])
        cc.observe(False, 1, 70.0, 0, 3.95)
        self.assertTrue(cc.observe(False, 1, 71.2, 60, 3.98))   # % and volts rose: charging
        self.assertEqual(cc.off_level, "low")
        self.assertEqual(cc.pin_args(False), ["op", "dl"])
        cc.observe(False, 1, 72.0, 100, 3.99)
        self.assertTrue(cc.observe(False, 1, 73.5, 200, 4.02))  # still charging
        self.assertEqual(cc.status, "unsupported")
        self.assertEqual(cc.pin_args(False), ["ip", "pd"])  # pin released

    def test_gauge_creeping_up_after_a_pause_is_not_charging(self):
        """The LED goes off, the % keeps rising for a while, the voltage falls:
        that's the fuel gauge catching up, so the pin must not be changed."""
        cc = self.ep.ChargeCheck(log=lambda m: None)
        cc.observe(False, 1, 80.0, 0, 4.10)
        for i, (soc, volt) in enumerate([(80.4, 4.06), (80.9, 4.04), (81.5, 4.03),
                                         (82.0, 4.02), (82.3, 4.02)], start=1):
            self.assertFalse(cc.observe(False, 1, soc, i * 60, volt))
        self.assertEqual((cc.off_level, cc.status), ("high", "unknown"))
        self.assertEqual(cc.pin_args(False), ["op", "dh"])   # still pausing

    def test_charge_check_probe_is_undone_if_useless(self):
        cc = self.ep.ChargeCheck(log=lambda m: None)
        w = cc.WINDOW
        cc.observe(True, 1, 50.0, 0)
        self.assertTrue(cc.observe(True, 1, 50.0, w))        # flat 20 min: probe
        self.assertEqual(cc.pin_args(True), ["op", "dh"])    # the other "on" state
        cc.observe(True, 1, 50.0, w + 1)
        self.assertTrue(cc.observe(True, 1, 50.1, 2 * w + 2))  # probe didn't help
        self.assertEqual(cc.pin_args(True), ["ip", "pd"])    # back to normal
        self.assertEqual(cc.off_level, "high")
        self.assertIn("isn't the cause", cc.warn)


class TestAutotest(Base):
    def setUp(self):
        super().setUp()
        ep = self.ep
        ep.is_pi5 = lambda: True
        ep.BOOT_DELAY = 0
        self.flag = [None]
        ep.booted_with_tryboot = lambda: self.flag[0]
        self.results = []
        ep.run_stress = lambda seconds, max_temp: self.results.pop(0)
        ep.write_config("Balanced")
        self.normal = self.config()

    def tryboot(self):
        return get("/boot/firmware/tryboot.txt")

    def boot(self, tryboot):
        self.flag[0] = tryboot
        self.ep.boot_apply()

    def test_finds_sweet_spot(self):
        ep = self.ep
        ep.start_autotest(1, 80)
        self.assertEqual(calls()[-1], "reboot 0 tryboot")
        self.assertEqual(self.config(), self.normal)     # config.txt never touched
        self.assertIn("arm_freq=2600\n[all]\n# <<<", self.tryboot())

        self.results = [(True, "stable, ran at 2600 MHz", 61.0)]
        self.boot(True)                                   # 2600 +0 passes
        self.assertIn("arm_freq=2800\nover_voltage_delta=25000", self.tryboot())

        self.results = [(False, "unstable", 70.0)]
        self.boot(True)                                   # 2800 +25 mV unstable
        self.assertIn("arm_freq=2800\nover_voltage_delta=50000", self.tryboot())

        self.boot(False)                                  # 2800 +50 mV didn't boot
        st = ep.load_state()
        self.assertEqual(st["phase"], "done")
        self.assertEqual(st["best"], [2600, 0])
        self.assertFalse(os.path.exists("/boot/firmware/tryboot.txt"))
        self.assertEqual(calls()[-1], "reboot ")
        self.assertIn("# plan: Sweet Spot", self.config())
        self.assertIn("arm_freq=2600", self.config())
        custom = json.loads(get("/etc/pi5-energyplan/custom.json"))
        self.assertEqual(custom["Sweet Spot"]["max_freq_mhz"], 2600)
        self.assertIn("Sweet Spot", cli("--list").stdout)
        self.assertIn("2600 MHz +0 mV → plan 'Sweet Spot'", cli("--status").stdout)

    def test_crash_moves_to_more_voltage(self):
        ep = self.ep
        ep.start_autotest(1, 80)
        st = ep.load_state()
        st["phase"] = "running"                           # froze mid stress test
        ep.save_state(st)
        self.boot(False)
        st = ep.load_state()
        self.assertEqual(st["index"], 1)
        self.assertEqual(st["results"][0]["reason"], "crashed")

    def test_old_bootloader_is_reported(self):
        self.ep.start_autotest(1, 80)
        self.boot(False)                                  # first test boot ignored
        self.assertEqual(self.ep.load_state()["phase"], "error")
        self.assertIn("rpi-eeprom-update", cli("--autotest-status").stdout)

    def test_cancel(self):
        self.ep.start_autotest(1, 80)
        out = cli("--autotest-cancel").stdout
        self.assertIn("cancelled", out)
        self.assertFalse(os.path.exists("/boot/firmware/tryboot.txt"))
        self.assertEqual(self.ep.load_state()["phase"], "cancelled")
        self.assertEqual(self.config(), self.normal)


class TestUninstall(Base):
    def test_uninstall_service_keeps_battery_guard_working(self):
        cli("--apply", "Cool")
        cli("--ups-config", '{"enabled": true}')
        cli("--uninstall-service")
        self.assertFalse(os.path.exists("/etc/systemd/system/pi5-energyplan.service"))
        # the battery guard still runs from the system copy
        self.assertTrue(os.path.exists("/usr/local/bin/pi5_energyplan.py"))

    def test_uninstall_everything(self):
        cli("--apply", "Cool")
        cli("--ups-config", '{"enabled": true}')
        cli("--uninstall-guard")
        cli("--uninstall-service")
        for p in ("/etc/systemd/system/pi5-energyplan.service",
                  "/etc/systemd/system/pi5-battery-guard.service",
                  "/usr/local/bin/pi5_energyplan.py"):
            self.assertFalse(os.path.exists(p), p)


class TestInstaller(Base):
    INSTALL = os.path.join(os.path.dirname(HERE), "install.sh")
    run_guard = TestUps.run_guard

    def install(self, *args, user=None):
        env = dict(os.environ)
        env.pop("SUDO_USER", None)
        if user:
            env["SUDO_USER"] = user
        r = subprocess.run(["sh", self.INSTALL, *args], capture_output=True, text=True,
                           env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def deb_field(self, field):
        debs = glob.glob("/var/cache/pi5-energyplan/*.deb")
        self.assertEqual(len(debs), 1, debs)
        return subprocess.run(["dpkg-deb", "-f", debs[0], field], capture_output=True,
                              text=True, check=True).stdout.strip()

    def test_installs_as_a_package_with_i2c_and_bootloader(self):
        hw(i2c=1)
        out = self.install()
        ver = self.ep.VERSION
        deb = f"/var/cache/pi5-energyplan/pi5-energyplan_{ver}_all.deb"
        self.assertIn("apt-get -o DPkg::Lock::Timeout=300 update", calls())
        self.assertIn(f"apt-get -o DPkg::Lock::Timeout=300 install -y --reinstall {deb}",
                      calls())
        self.assertEqual(hw()["dpkg"]["pi5-energyplan"]["version"], ver)
        self.assertEqual(get("/usr/share/pi5-energyplan/pi5_energyplan.py"), get(SCRIPT))
        self.assertIn("raspi-config nonint do_i2c 0", calls())
        self.assertIn("REBOOT_NEEDED=1", out)
        # Geekworm's bootloader settings, other lines kept
        self.assertEqual(hw()["eeprom"], "[all]\nBOOT_UART=1\nPOWER_OFF_ON_HALT=1\n"
                                         "BOOT_ORDER=0xf41\nPSU_MAX_CURRENT=5000\n")
        self.assertFalse(os.path.exists("/etc/pi5-energyplan/battery.json"))
        self.assertTrue(os.access("/usr/local/bin/pi5_energyplan.py", os.X_OK))
        self.assertIn("Battery     : 76.5%", out)          # --status ran at the end
        self.assertIn("skipped the menu entry", out)

    def test_package_needs_trixie_or_bookworm_names(self):
        self.install()
        deps = self.deb_field("Depends")
        # Trixie name first, Bookworm's as the alternative: apt picks what exists
        for want in ("python3-tk", "python3-smbus2 | python3-smbus", "i2c-tools",
                     "raspi-utils | libraspberrypi-bin", "python3-gi",
                     "gir1.2-gdkpixbuf-2.0", "pkexec | policykit-1"):
            self.assertIn(want, deps)
        recs = self.deb_field("Recommends")
        for want in ("librsvg2-common", "python3-libgpiod", "gpiod", "rpi-eeprom",
                     "stress-ng"):
            self.assertIn(want, recs)
        self.assertEqual(self.deb_field("Architecture"), "all")

    def test_bootloader_already_set(self):
        hw(i2c=0, eeprom="[all]\nPOWER_OFF_ON_HALT=1\nPSU_MAX_CURRENT=5000\n")
        out = self.install()
        self.assertNotIn("raspi-config nonint do_i2c 0", calls())
        self.assertIn("Bootloader already set", out)
        self.assertNotIn("REBOOT_NEEDED", out)

    def test_no_eeprom_and_recommended_ups(self):
        out = self.install("--no-eeprom", "--ups-recommended")
        self.assertNotIn("Bootloader", out)
        self.assertFalse(any(c.startswith("rpi-eeprom-config") for c in calls()))
        cfg = json.loads(get("/etc/pi5-energyplan/battery.json"))
        self.assertEqual((cfg["enabled"], cfg["max"], cfg["min"], cfg["saver_enabled"],
                          cfg["saver_below"]), (True, 80, 20, True, 50))
        self.assertTrue(os.path.exists("/etc/systemd/system/pi5-battery-guard.service"))
        cli("--ups-config", '{"enabled": true, "max": 90}')
        out = self.install("--ups-recommended")       # keeps the user's own settings
        self.assertIn("already saved, left as they are", out)
        self.assertEqual(json.loads(get("/etc/pi5-energyplan/battery.json"))["max"], 90)

    def test_warns_about_geekworm_scripts(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                              "/home/pi/x120x/merged-trixie.py"])
        try:
            out = self.install()
        finally:
            p.kill()
            p.wait()
        self.assertIn("WARNING: Geekworm UPS scripts are running", out)
        self.assertIn("merged-trixie.py", out)

    def test_not_a_raspberry_pi(self):
        os.remove("/sys/firmware/devicetree/base/model")
        out = self.install()
        self.assertIn("doesn't look like a Raspberry Pi 5", out)
        self.assertIn("Board: unknown (not a Raspberry Pi?)", out)
        self.assertFalse(any(c.startswith(("rpi-eeprom-config", "raspi-config nonint do_i2c"))
                             for c in calls()))
        self.assertIn("Then pick a plan.", out)

    def test_rerun_updates_services(self):
        self.install()
        cli("--apply", "Cool")
        cli("--ups-config", '{"enabled": true}')
        before = self.config()
        out = self.install()
        self.assertIn("System part already up to date", out)
        self.assertIn("systemctl try-restart pi5-battery-guard.service", calls())
        self.assertEqual(self.config(), before)

    def test_uninstall(self):
        self.install()
        subprocess.run([sys.executable, "/usr/local/bin/pi5_energyplan.py", "--apply",
                        "Cool"], check=True, capture_output=True)
        cli("--ups-config", '{"enabled": true}')
        self.install("--uninstall")
        self.assertIn("apt-get -o DPkg::Lock::Timeout=300 remove -y pi5-energyplan", calls())
        self.assertNotIn("pi5-energyplan", hw()["dpkg"])
        for p in ("/usr/local/bin/pi5_energyplan.py",
                  "/etc/systemd/system/pi5-energyplan.service",
                  "/etc/systemd/system/pi5-battery-guard.service",
                  "/usr/share/pi5-energyplan/pi5_energyplan.py"):
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(hw()["pin16"], "ip pd")             # charging left on

    @unittest.skipUnless(os.environ.get("INSTALL_TEST_USER"),
                         "set INSTALL_TEST_USER to a throwaway desktop user")
    def test_desktop_user_part(self):
        user = os.environ["INSTALL_TEST_USER"]
        home = os.path.expanduser("~" + user)
        out = self.install(user=user)
        launcher = get(home + "/.local/share/applications/pi5-energyplan.desktop")
        self.assertIn("Exec=/usr/bin/python3 /usr/local/bin/pi5_energyplan.py", launcher)
        # sudo drops the simulator's fake I2C library, so as that user no UPS is found:
        # then there's no battery icon (on a Pi, the installed library finds the UPS)
        self.assertIn("Battery icon not added: Missing I2C library", out)
        self.assertFalse(os.path.exists(home + "/.config/autostart/pi5-battery-tray.desktop"))
        self.install("--uninstall", user=user)
        self.assertFalse(os.path.exists(home + "/.local/share/applications/"
                                        "pi5-energyplan.desktop"))


class TestGeekwormFunctions(Base):
    """The functions of Geekworm's example scripts, done the safe way."""
    run_guard = TestUps.run_guard
    status = TestUps.status

    def test_shutdown_15_s_after_power_cut(self):
        hw(pld=0, soc=90, volt=4.0)
        steps = [None] * 4 + [lambda: self.assertFalse(
            any(c.startswith("shutdown -h") for c in calls()))] + [None]
        self.run_guard({"enabled": True, "outage_minutes": 0.25}, steps)
        sd = [c for c in calls() if c.startswith("shutdown -h")]
        self.assertEqual(len(sd), 1)
        self.assertIn("+1 Battery guard: power out for 15 seconds", sd[0])

    def test_short_glitch_does_not_shut_down(self):
        hw(pld=0)
        self.run_guard({"enabled": True, "outage_minutes": 0.25},
                       [None, None, lambda: hw(pld=1), None, lambda: hw(pld=0), None, None])
        self.assertFalse(any(c.startswith("shutdown") for c in calls()))

    def test_gauge_lost_on_battery(self):
        hw(pld=0)                     # on battery; the gauge answers once, then goes silent
        steps = [lambda: hw(ups=False)] + [None] * 99 + [lambda: self.assertFalse(
            any(c.startswith("shutdown -h") for c in calls()))] + [None]
        st = self.run_guard({"enabled": True}, steps)
        self.assertFalse(st["gauge_ok"])
        self.assertEqual(st["ac"], 0)
        sd = [c for c in calls() if c.startswith("shutdown -h")]
        self.assertEqual(len(sd), 1)
        self.assertIn("battery level can't be read", sd[0])

    def test_gauge_lost_outage_timer_still_works(self):
        hw(pld=0)
        self.run_guard({"enabled": True, "outage_minutes": 1},
                       [lambda: hw(ups=False)] + [None] * 20)
        sd = [c for c in calls() if c.startswith("shutdown -h")]
        self.assertEqual(len(sd), 1)
        self.assertIn("power out for 1 minute", sd[0])

    def test_pi5_without_the_ups_is_left_alone(self):
        """No X1202: the gauge never answers and GPIO 6 may read low. The guard must
        not shut down and must not touch GPIO 16."""
        hw(ups=False, pld=0, pin16="op dl")       # GPIO 16 used by something else
        self.run_guard({"enabled": True, "outage_minutes": 0.25, "saver_enabled": True},
                       [None] * 150)
        self.assertFalse(any(c.startswith(("shutdown", "pinctrl set")) for c in calls()))
        self.assertEqual(hw()["pin16"], "op dl")

    def test_gauge_lost_on_mains_is_fine(self):
        hw(ups=False, pld=1)
        self.run_guard({"enabled": True}, [None] * 120)
        self.assertFalse(any(c.startswith("shutdown") for c in calls()))

    def test_charge_full_once(self):
        ep = self.ep
        hw(soc=85, pld=1)

        def paused():
            self.assertEqual(hw()["pin16"], "op dh")
            put(ep.TOPUP_FILE, int(time.time()))        # "Charge to 100 % once"

        def charging():
            self.assertEqual(hw()["pin16"], "ip pd")
            st = self.status()
            self.assertTrue(st["topup"])
            self.assertEqual((st["max"], st["limit"]), (100, 80))
            hw(soc=99.7)

        def back_to_limit():
            self.assertFalse(os.path.exists(ep.TOPUP_FILE))
            self.assertEqual(hw()["pin16"], "op dh")
            self.assertFalse(self.status()["topup"])
        self.run_guard({"enabled": True, "max": 80}, [paused, charging, back_to_limit])

    def test_charge_full_cli(self):
        cli("--ups-config", '{"enabled": true, "max": 80}')
        r = cli("--charge-full", "on", check=False)
        self.assertIn("battery guard isn't running", r.stderr)
        hw(active=["pi5-battery-guard.service"])
        self.assertIn("Charging to 100 % once", cli("--charge-full", "on").stdout)
        self.assertTrue(os.path.exists(self.ep.TOPUP_FILE))
        self.assertIn("Back to the normal 80% charge limit",
                      cli("--charge-full", "off").stdout)
        self.assertFalse(os.path.exists(self.ep.TOPUP_FILE))
        cli("--ups-config", '{"enabled": true, "max": 100, "min": 20}')
        self.assertIn("already charges to 100 %", cli("--charge-full", "on").stdout)

    def test_monitor_report(self):
        ep = self.ep
        hw(pld=0, soc=40, volt=3.62)
        txt = ep.monitor_report(ep.Battery())
        for want in ("Battery      : 40.0 % (Medium) · 3.620 V per cell",
                     "Mains power  : LOST, running on battery",
                     "Charging     : board default (battery guard not running)",
                     "Protection   : off (no auto shutdown)",
                     "WARNING      : Battery approaching critical",
                     "Pi power     : input 5.14 V · uses",
                     "CPU core 0.800 V 1.00 A",
                     "CPU          : 52.3 °C · 1.50 GHz (max 2.4) · ondemand",
                     "Fan          : level 1/4 · 29% · 2500 rpm"):
            self.assertIn(want, txt)
        hw(soc=20)
        self.assertIn("Battery critical", ep.monitor_report(ep.Battery()))
        self.assertIn("CPU core 0.800 V 1.00 A", cli("--status").stdout)

    def test_eeprom_setup(self):
        hw(eeprom="[all]\nBOOT_UART=1\nPSU_MAX_CURRENT=3000\n\n[gpio4=1]\nBOOT_ORDER=0x1\n")
        out = cli("--eeprom-setup").stdout
        self.assertIn("take effect after a reboot", out)
        self.assertEqual(hw()["eeprom"], "[all]\nBOOT_UART=1\nPSU_MAX_CURRENT=5000\n"
                                         "POWER_OFF_ON_HALT=1\n\n[gpio4=1]\nBOOT_ORDER=0x1\n")
        self.assertIn("PSU_MAX_CURRENT=3000", get("/var/lib/pi5-energyplan/eeprom-before.conf"))
        self.assertIn("already set", cli("--eeprom-setup").stdout)
        self.assertEqual(sum(c.startswith("rpi-eeprom-config --apply") for c in calls()), 1)


class TestNothingTwice(Base):
    """Reinstalling, updating, re-applying and rebooting must never add anything twice."""
    INSTALL = TestInstaller.INSTALL

    def snapshot(self, home):
        files = {}
        for pattern in ("/boot/firmware/*", "/etc/systemd/system/*", "/etc/pi5-energyplan/*",
                        "/usr/local/bin/*", "/var/lib/pi5-energyplan/eeprom-before.conf",
                        home + "/.local/share/applications/*",
                        home + "/.config/autostart/*"):
            for f in sorted(glob.glob(pattern)):
                files[f] = get(f)
        files["eeprom"] = hw()["eeprom"]
        return files

    def test_reinstall_update_boot_cycle(self):
        import tempfile
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home)
        ep = self.ep
        self.no_sleep()

        def sh(*args):
            r = subprocess.run(["sh", self.INSTALL, *args], capture_output=True, text=True,
                               env=env, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        def me(*args):
            r = subprocess.run([sys.executable, "/usr/local/bin/pi5_energyplan.py", *args],
                               capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        try:
            sh("--ups-recommended")
            me("--install-launcher")
            me("--install-tray")
            me("--apply", "Cool")
            me("--set-fan", "Cool", "Strong")
            first = self.snapshot(home)

            for _ in range(3):                      # reinstall / update / reboot, again and again
                sh("--ups-recommended")
                sh()
                me("--install-launcher")
                me("--install-tray")
                me("--update")
                me("--update-root")
                me("--eeprom-setup")
                me("--apply", "Cool")
                ep.boot_apply()                     # what runs at every boot
                ep.boot_apply()
            again = self.snapshot(home)
        finally:
            subprocess.run(["pkill", "-f", "pi5_energyplan.py --tray"])
            shutil.rmtree(home, ignore_errors=True)

        self.assertEqual(sorted(again), sorted(first))   # no new files
        for f in first:
            self.assertEqual(again[f], first[f], f)       # and nothing changed inside
        cfg = again["/boot/firmware/config.txt"]
        self.assertEqual(cfg.count("# >>> pi5-energyplan"), 1)
        self.assertEqual(cfg.count("# <<< pi5-energyplan <<<"), 1)
        self.assertEqual(cfg.count("dtparam=fan_temp0="), 2)    # ours + the old one, disabled
        self.assertEqual(cfg.count("#energyplan-disabled#"), 3)
        self.assertEqual(again["eeprom"].count("POWER_OFF_ON_HALT"), 1)
        self.assertEqual(again["eeprom"].count("PSU_MAX_CURRENT"), 1)
        self.assertEqual(sum(c.startswith("rpi-eeprom-config --apply") for c in calls()), 1)
        units = [f for f in again if f.startswith("/etc/systemd/system/")]
        self.assertEqual(sorted(units), ["/etc/systemd/system/pi5-battery-guard.service",
                                         "/etc/systemd/system/pi5-energyplan.service"])
        autostart = [f for f in again if "/autostart/" in f]
        self.assertEqual(len(autostart), 1)

    def test_old_manual_install_is_replaced_not_doubled(self):
        """Someone ran the script from ~/pi5-energyplan before using install.sh."""
        import tempfile
        home = tempfile.mkdtemp()
        old = os.path.join(home, "pi5-energyplan")
        os.makedirs(old)
        shutil.copy2(SCRIPT, old)
        env = dict(os.environ, HOME=home)
        try:
            for args in (["--install-launcher"], ["--install-tray"]):
                subprocess.run([sys.executable, os.path.join(old, "pi5_energyplan.py"), *args],
                               check=True, capture_output=True, env=env)
            subprocess.run(["sh", TestInstaller.INSTALL], check=True, capture_output=True,
                           env=env)
            for args in (["--install-launcher"], ["--install-tray"]):
                subprocess.run([sys.executable, "/usr/local/bin/pi5_energyplan.py", *args],
                               check=True, capture_output=True, env=env)
            apps = glob.glob(home + "/.local/share/applications/*")
            auto = glob.glob(home + "/.config/autostart/*")
            self.assertEqual(len(apps), 1)
            self.assertEqual(len(auto), 1)
            self.assertIn("/usr/local/bin/pi5_energyplan.py", get(apps[0]))
            self.assertIn("/usr/local/bin/pi5_energyplan.py --tray", get(auto[0]))
        finally:
            subprocess.run(["pkill", "-f", "pi5_energyplan.py --tray"])
            shutil.rmtree(home, ignore_errors=True)

    def second_copy_refused(self, args, message):
        first = subprocess.Popen([sys.executable, SCRIPT, *args],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.5)
            self.assertIsNone(first.poll(), "first copy should still be running")
            r = cli(*args, check=False)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(message, r.stderr)
        finally:
            first.terminate()
            first.wait()

    def test_only_one_battery_guard(self):
        self.second_copy_refused(["--battery-guard"], "already running")

    def test_only_one_boot_task(self):
        cli("--apply", "Cool")
        self.second_copy_refused(["--boot"], "already running")

    def test_usb_5v_when_off_choice(self):
        stock = "[all]\nBOOT_UART=1\nPOWER_OFF_ON_HALT=0\nBOOT_ORDER=0xf41\n"
        hw(eeprom=stock)
        out = cli("--usb-5v-when-off", "off").stdout
        self.assertIn("POWER_OFF_ON_HALT=1", out)
        self.assertEqual(hw()["eeprom"], stock.replace("HALT=0", "HALT=1"))  # only that key
        self.assertIn("already set", cli("--usb-5v-when-off", "off").stdout)
        cli("--usb-5v-when-off", "on")
        self.assertIn("POWER_OFF_ON_HALT=0", hw()["eeprom"])
        self.assertTrue(json.loads(get("/etc/pi5-energyplan/battery.json"))["usb_5v_when_off"])

        # a reinstall adds PSU_MAX_CURRENT but keeps the "stay on" choice
        r = subprocess.run(["sh", TestInstaller.INSTALL], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(hw()["eeprom"], "[all]\nBOOT_UART=1\nPOWER_OFF_ON_HALT=0\n"
                                         "BOOT_ORDER=0xf41\nPSU_MAX_CURRENT=5000\n")

    def test_ups_window_choice_changes_only_when_needed(self):
        hw(eeprom="[all]\nPOWER_OFF_ON_HALT=1\n")
        cli("--ups-config", '{"enabled": true, "usb_5v_when_off": false}')
        self.assertFalse(any(c.startswith("rpi-eeprom-config --apply") for c in calls()))
        out = cli("--ups-config", '{"enabled": true, "usb_5v_when_off": true}').stdout
        self.assertIn("POWER_OFF_ON_HALT=0", out)
        self.assertEqual(hw()["eeprom"], "[all]\nPOWER_OFF_ON_HALT=0\n")
        cli("--ups-config", '{"max": 90}')                # other keys keep their values
        cfg = json.loads(get("/etc/pi5-energyplan/battery.json"))
        self.assertEqual((cfg["max"], cfg["enabled"], cfg["usb_5v_when_off"]), (90, True, True))
        self.assertEqual(sum(c.startswith("rpi-eeprom-config --apply") for c in calls()), 1)


class TestBootGovernor(Base):
    """The Raspberry Pi kernel boots in 'powersave' (1.5 GHz). The boot service must
    always leave the Pi on its saved plan, or on Performance."""
    INSTALL = os.path.join(os.path.dirname(HERE), "install.sh")

    def setUp(self):
        super().setUp()
        put(CPUFREQ + "/scaling_governor", "powersave")      # how the kernel starts
        put(CPUFREQ + "/scaling_max_freq", 1500000)
        self.no_sleep()

    def test_status_shows_nobody_set_it(self):
        st = cli("--status").stdout
        self.assertIn("Governor    : powersave (not set by Pi 5 Energy Plan since boot: "
                      "the kernel starts in powersave", st)
        self.assertIn("Saved plan  : none (Performance is used at boot)", st)

    def test_no_saved_plan_boots_in_performance(self):
        self.ep.boot_apply()
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "performance")
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "2400000")
        self.assertIn("Governor    : performance (Performance, set at boot)",
                      cli("--status").stdout)
        # the same when the power comes back after the battery saver
        self.ep.apply_live("Underclock", by="battery saver")
        hw(pld=0, soc=40)
        TestUps.run_guard(self, {"saver_enabled": True, "saver_below": 50},
                          [None, None, lambda: hw(pld=1), None])
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "performance")

    def test_saved_plan_boots_with_it(self):
        cli("--apply", "Cool")
        put(CPUFREQ + "/scaling_governor", "powersave")
        self.ep.boot_apply()
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "schedutil")
        self.assertIn("(Cool, set at boot)", cli("--status").stdout)

    def test_boot_leaves_the_battery_saver_alone(self):
        cli("--apply", "Performance")
        self.ep.apply_live("Underclock", by="battery saver")
        self.ep.write_json_atomic(self.ep.GUARD_STATUS, {"updated": time.time(),
                                                         "saver_active": "Underclock"})
        self.ep.boot_apply()
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "powersave")
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "1500000")
        self.assertIn("(Underclock, battery saver on)", cli("--status").stdout)
        # editing the plan in use doesn't undo the saver either
        out = cli("--set-governor", "Performance", "ondemand").stdout
        self.assertIn("battery saver is on: it applies when the power is back", out)
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "powersave")

    def test_saver_ignores_a_single_glitch(self):
        cli("--apply", "Cool")
        hw(pld=1, soc=40)
        st = TestUps.run_guard(self, {"enabled": True, "saver_enabled": True,
                                      "saver_below": 50},
                               [lambda: hw(pld=0), lambda: hw(pld=1), None,
                                lambda: hw(pld=0), None, None])
        self.assertEqual(st["saver_active"], "Underclock")    # 2 readings in a row: on
        self.assertEqual(calls().count("systemctl try-restart pi5-battery-guard.service"), 0)

    def test_saver_glitch_does_not_switch(self):
        cli("--apply", "Cool")
        hw(pld=1, soc=40)
        TestUps.run_guard(self, {"enabled": True, "saver_enabled": True, "saver_below": 50},
                          [lambda: hw(pld=0), lambda: hw(pld=1), None, None])
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "schedutil")

    def test_install_adds_the_boot_service_but_respects_removing_it(self):
        TestInstaller.install(self)
        self.assertTrue(os.path.exists("/etc/systemd/system/pi5-energyplan.service"))
        self.assertIn("systemctl enable pi5-energyplan.service", calls())
        cli("--uninstall-service")
        out = TestInstaller.install(self)                     # an update
        self.assertIn("Boot service not added: it was removed with --uninstall-service", out)
        self.assertFalse(os.path.exists("/etc/systemd/system/pi5-energyplan.service"))
        cli("--apply", "Eco")                                 # keep-after-reboot adds it again
        self.assertTrue(os.path.exists("/etc/systemd/system/pi5-energyplan.service"))


class TestChargeTest(Base):
    """--charge-test: does the board really stop charging when GPIO 16 is high?"""

    def run_test(self, readings, minutes=2, unplug_at=None):
        ep = self.ep
        it = iter(readings)
        n = [0]

        def on_sleep():
            n[0] += 1
            if unplug_at is not None and n[0] == unplug_at:
                hw(pld=0)
            try:
                soc, volt = next(it)
                hw(soc=soc, volt=volt)
            except StopIteration:
                pass
        ep.time = Clock(on_sleep)
        return ep.charge_test(minutes=minutes, interval=15)

    def test_board_that_keeps_charging(self):
        hw(soc=70, volt=3.95, active=["pi5-battery-guard.service"])
        rising = [(70 + i * 0.1, 3.95 + i * 0.003) for i in range(1, 10)]
        out = self.run_test(rising)
        self.assertIn("STILL CHARGING with GPIO 16 high", out)
        self.assertIn("op dh", " ".join(c for c in calls() if c.startswith("pinctrl set 16")))
        self.assertEqual(hw()["pin16"], "ip pd")                 # always back on
        self.assertIn("systemctl stop pi5-battery-guard.service", calls())
        self.assertIn("systemctl start pi5-battery-guard.service", calls())
        self.assertIn("STILL CHARGING", get("/var/lib/pi5-energyplan/charge-test.log"))

    def test_board_that_stops_while_the_gauge_catches_up(self):
        hw(soc=80, volt=4.08)
        # LED off, % still creeping up, but the voltage falls: no current flows in
        creeping = [(80.3, 4.05), (80.6, 4.04), (80.9, 4.035), (81.2, 4.03), (81.4, 4.03),
                    (81.6, 4.028), (81.7, 4.027), (81.8, 4.027), (81.8, 4.026)]
        out = self.run_test(creeping)
        self.assertIn("Charging STOPS correctly", out)
        self.assertIn("fuel gauge's estimate catches up", out)
        self.assertEqual(hw()["pin16"], "ip pd")
        self.assertIn("Last charge test:", subprocess.run(
            [sys.executable, SCRIPT, "--charge-debug"], capture_output=True, text=True).stdout)

    def test_unplugging_stops_the_test_and_charging_comes_back(self):
        hw(soc=60, volt=3.9)
        with self.assertRaises(RuntimeError):
            self.run_test([(60, 3.9)] * 9, unplug_at=3)
        self.assertEqual(hw()["pin16"], "ip pd")

    def test_full_battery_is_not_tested(self):
        """At 100 % the charger has stopped by itself: "stops" would prove nothing."""
        hw(soc=100, volt=4.19, active=["pi5-battery-guard.service"])
        r = cli("--charge-test", "2", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the battery is full (100 %, 4.19 V)", r.stderr)
        self.assertIn("below 85 %", r.stderr)
        self.assertFalse([c for c in calls() if c.startswith("pinctrl set 16")])
        self.assertNotIn("systemctl stop pi5-battery-guard.service", calls())

    def test_needs_the_adapter(self):
        hw(pld=0)
        r = cli("--charge-test", "2", check=False)
        self.assertIn("plug in the adapter first", r.stderr)
        self.assertEqual(hw()["pin16"], "ip pd")


def as_board(model, top_khz, low_khz, fan=False, ups=False):
    """Turn the simulated Pi 5 into another model."""
    put("/sys/firmware/devicetree/base/model", model + "\x00")
    put(CPUFREQ + "/cpuinfo_max_freq", top_khz)
    put(CPUFREQ + "/cpuinfo_min_freq", low_khz)
    put(CPUFREQ + "/scaling_max_freq", top_khz)
    if not fan:
        shutil.rmtree("/sys/class/thermal/cooling_device0")
        shutil.rmtree("/sys/class/hwmon/hwmon2")
    hw(ups=ups)


class TestOtherBoards(Base):
    INSTALL = os.path.join(os.path.dirname(HERE), "install.sh")

    def board(self, *a, **kw):
        as_board(*a, **kw)
        self.ep = load_module()
        return self.ep

    def block(self):
        return self.config().split("# >>> pi5-energyplan")[1]

    def test_pi4(self):
        ep = self.board("Raspberry Pi 4 Model B Rev 1.4", 1800000, 600000)
        self.assertEqual((ep.BOARD["family"], ep.BOARD["top"]), ("pi4", 1800))
        plans = {n: p["max_freq_mhz"] for n, p in ep.PLANS.items()}
        self.assertEqual(plans, {"Underclock": 1100, "Eco": 1400, "Balanced": 1800,
                                 "Cool": 1800, "Max Cool": 1500, "Performance": 1800,
                                 "OC 2.0 GHz": 2000, "Custom": 1800})
        self.assertRegex(cli("--list").stdout, r"Balanced\s+ondemand\s+fan -\s+")
        out = cli("--apply", "Cool").stdout
        self.assertIn("no fan control", out)
        self.assertIn("# plan: Cool\n[pi4]\narm_freq=1800\n[all]\n# <<<", self.block())
        self.assertNotIn("fan_temp0=", self.block())
        cli("--apply", "OC 2.0")
        self.assertIn("[pi4]\narm_freq=2000\nover_voltage=6\n[all]", self.block())
        r = cli("--set-fan", "Cool", "Max", check=False)
        self.assertIn("no fan control", r.stderr)
        st = cli("--status").stdout
        self.assertIn("Board       : Raspberry Pi 4 Model B Rev 1.4 · no fan control · no UPS",
                      st)
        self.assertIn("Battery     : no X120x UPS: it fits only the Raspberry Pi 5", st)
        self.assertNotIn("Sweet spot", st)
        self.assertNotIn("GPU (3D)", st)
        r = cli("--autotest", "--yes", check=False)
        self.assertIn("only for the Raspberry Pi 5", r.stderr)

    def test_older_pi4_gets_a_milder_overclock_too(self):
        ep = self.board("Raspberry Pi 4 Model B Rev 1.2", 1500000, 600000)
        self.assertEqual(ep.PLANS["OC 1.8 GHz"]["over_voltage"], 2)
        self.assertEqual(ep.PLANS["Balanced"]["max_freq_mhz"], 1500)

    def test_zero_2_w(self):
        ep = self.board("Raspberry Pi Zero 2 W Rev 1.0", 1000000, 600000)
        self.assertEqual(list(ep.PLANS), ["Underclock", "Eco", "Balanced", "Cool",
                                          "Max Cool", "Performance", "Custom"])   # no OC
        self.assertEqual(ep.PLANS["Underclock"]["max_freq_mhz"], 600)
        cli("--apply", "Underclock")
        self.assertIn("[pi02]\narm_freq=600\n[all]", self.block())
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "600000")
        # the Custom plan can't overclock it, and has no voltage / GPU / fan settings
        for bad, why in (({"max_freq_mhz": 1200}, "CPU speed: 600-1000 MHz"),
                         ({"over_voltage": 2}, "Extra voltage"),
                         ({"over_voltage_delta": 25000}, "Extra voltage"),
                         ({"v3d_freq": 1000}, "only the Raspberry Pi 5")):
            r = cli("--set-custom", json.dumps(bad), check=False)
            self.assertIn(why, r.stderr, bad)
        out = cli("--set-custom", '{"max_freq_mhz": 900, "governor": "schedutil"}',
                  "--apply", "Custom").stdout
        self.assertIn("Custom plan saved: CPU 900 MHz, schedutil governor", out)
        self.assertIn("[pi02]\narm_freq=900\n[all]", self.block())

    def test_zero_w(self):
        ep = self.board("Raspberry Pi Zero W Rev 1.1", 1000000, 700000)
        self.assertEqual(ep.PLANS["Underclock"]["max_freq_mhz"], 700)
        cli("--apply", "Balanced")
        self.assertIn("[pi0]\narm_freq=1000\n[all]", self.block())

    def test_sd_card_moved_from_a_pi5_to_a_pi4(self):
        cli("--apply", "OC 2.8")                      # on the Pi 5
        self.assertIn("[pi5]", self.block())
        self.board("Raspberry Pi 4 Model B Rev 1.4", 1800000, 600000)
        put(CPUFREQ + "/scaling_governor", "powersave")
        self.no_sleep()
        self.ep.boot_apply()                           # the Pi 5 plan doesn't exist here
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "performance")
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "1800000")
        cli("--apply", "Balanced")
        cfg = self.config()
        self.assertEqual(cfg.count("# >>> pi5-energyplan"), 1)
        self.assertIn("[pi4]\narm_freq=1800", cfg)
        self.assertNotIn("arm_freq=2800", cfg)

    def test_guard_stays_idle_on_other_boards(self):
        ep = self.board("Raspberry Pi 4 Model B Rev 1.4", 1800000, 600000)
        ep.write_json_atomic(ep.BATTERY_CFG, ep.validate_battery_cfg(
            {"enabled": True, "saver_enabled": True}))
        n = [0]

        def on_sleep():
            n[0] += 1
            if n[0] > 3:
                raise Stop
        ep.time = Clock(on_sleep)
        with self.assertRaises(Stop):
            ep.battery_guard()
        self.assertFalse(any(c.startswith(("pinctrl set", "shutdown")) for c in calls()))
        self.assertFalse(os.path.exists(ep.GUARD_STATUS))

    def test_pi5_without_the_x1202(self):
        hw(ups=False)
        self.ep = load_module()
        r = cli("--ups-config", '{"enabled": true}', check=False)
        self.assertIn("No X120x UPS found", r.stderr)
        r = cli("--eeprom-setup", check=False)
        self.assertIn("PSU_MAX_CURRENT=5000 is only for a 5 A supply", r.stderr)
        self.assertFalse(any(c.startswith("rpi-eeprom-config --apply") for c in calls()))
        out = TestInstaller.install(self, "--ups-recommended")
        self.assertIn("No X120x UPS found: bootloader settings skipped", out)
        self.assertIn("No X120x UPS found: battery protection not turned on", out)
        self.assertFalse(os.path.exists("/etc/systemd/system/pi5-battery-guard.service"))
        self.assertIn("UPS not answering", cli("--status").stdout)


class TestStorage(Base):
    """Slow storage + RAM to spare: keep more in RAM. Never on a low-RAM board."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.ep.MEMINFO = os.path.join(self.tmp, "meminfo")
        self.ep.SWAPS = os.path.join(self.tmp, "swaps")
        clear("/sys/block")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def setup(self, disk="mmcblk0", kind="SD", ram_mb=8000, swap="/var/swap", tmp="ext4",
              usb_speed=None):
        part = disk + ("p2" if disk[-1].isdigit() else "2")
        real = f"/sys/class/fakedev/{'usb1/1-1/1-1:1.0/host0' if usb_speed else 'mmc'}/{disk}"
        os.makedirs(f"{real}/{part}")
        put(f"{real}/{part}/partition", 2)
        os.symlink(f"{real}/{part}", f"/sys/class/block/{part}") if os.path.isdir(
            "/sys/class/block") else (os.makedirs("/sys/class/block"),
                                      os.symlink(f"{real}/{part}", f"/sys/class/block/{part}"))
        os.makedirs(f"/sys/block/{disk}")
        if disk.startswith("mmcblk"):
            put(f"/sys/block/{disk}/device/type", kind)
        elif usb_speed:
            put("/sys/class/fakedev/usb1/1-1/speed", usb_speed)
            os.symlink(f"{real}", f"/sys/block/{disk}/device")
        put(self.ep.MEMINFO, f"MemTotal:       {ram_mb * 1024} kB\nMemFree: 1 kB")
        swaps = "Filename\tType\tSize\tUsed\tPriority\n"
        if swap:
            swaps += f"{swap}\tfile\t1\t0\t-2\n"
        put(self.ep.SWAPS, swaps)
        hw(mounts={"/": {"SOURCE": f"/dev/{part}"}, "/tmp": {"FSTYPE": tmp}})

    def test_sd_card_with_ram_to_spare(self):
        self.setup()
        out = self.ep.storage_tune("auto")
        self.assertIn("SD card is slow and there are 8000 MB of RAM: file cache kept in RAM "
                      "longer, swap only when really needed", out)
        self.assertIn("/tmp in RAM from the next boot", out)
        conf = get(self.ep.SYSCTL_FILE)
        self.assertIn("vm.vfs_cache_pressure = 50", conf)
        self.assertIn("vm.swappiness = 10", conf)
        self.assertIn("Where=/tmp", get("/etc/systemd/system/tmp.mount"))
        self.assertIn("systemctl enable tmp.mount", calls())
        self.assertIn(f"sysctl -p {self.ep.SYSCTL_FILE}", calls())
        n = len(calls())
        self.ep.storage_tune("auto")                        # again: nothing twice
        self.assertEqual(len([c for c in calls()[n:] if "enable tmp.mount" in c]), 0)
        self.assertIn("SD card (slow) · 7.8 GB RAM · swap: file · kept in RAM: file cache, "
                      "/tmp, swap only when needed", self.ep.storage_line())

    def test_trixie_already_keeps_swap_and_tmp_in_ram(self):
        self.setup(swap="/dev/zram0", tmp="tmpfs")
        out = self.ep.storage_tune("auto")
        self.assertIn("(swap is zram: left as it is)", out[0])
        self.assertIn("/tmp is already in RAM", out)
        self.assertNotIn("swappiness", get(self.ep.SYSCTL_FILE))
        self.assertFalse(os.path.exists("/etc/systemd/system/tmp.mount"))

    def test_low_ram_board_is_left_alone(self):
        self.setup(ram_mb=427)                              # Zero 2 W: 512 MB
        out = self.ep.storage_tune("auto")
        self.assertIn("with only 427 MB of RAM keeping more in RAM would make it slower", out[0])
        self.assertFalse(os.path.exists(self.ep.SYSCTL_FILE))
        self.assertFalse(os.path.exists("/etc/systemd/system/tmp.mount"))

    def test_fast_storage_is_left_alone(self):
        for disk, kind, speed, what in (("nvme0n1", None, None, "NVMe SSD"),
                                        ("mmcblk0", "MMC", None, "eMMC"),
                                        ("sda", None, 5000, "USB 3 drive")):
            make_pi()
            self.setup(disk=disk, kind=kind, usb_speed=speed)
            self.assertEqual(self.ep.root_storage(), (what, False))
            self.assertIn("fast enough", self.ep.storage_tune("auto")[0])
            self.assertFalse(os.path.exists(self.ep.SYSCTL_FILE))

    def test_usb2_drive_is_slow(self):
        self.setup(disk="sda", usb_speed=480)
        self.assertEqual(self.ep.root_storage(), ("USB 2 drive", True))

    def test_off_undoes_and_is_remembered(self):
        self.setup()
        self.ep.storage_tune("auto")
        self.assertIn("Storage tuning removed", self.ep.storage_tune("off")[0])
        self.assertFalse(os.path.exists(self.ep.SYSCTL_FILE))
        self.assertFalse(os.path.exists("/etc/systemd/system/tmp.mount"))
        self.assertIn("sysctl --system", calls())
        self.assertIn("is off", self.ep.storage_tune("auto")[0])   # updates respect it
        self.assertFalse(os.path.exists(self.ep.SYSCTL_FILE))
        self.ep.storage_tune("on")
        self.assertTrue(os.path.exists(self.ep.SYSCTL_FILE))

    def test_someone_elses_tmp_mount_is_kept(self):
        self.setup()
        put("/etc/systemd/system/tmp.mount", "[Mount]\nWhat=tmpfs\nWhere=/tmp\n")
        out = self.ep.storage_tune("auto")
        self.assertIn("/tmp: there's already a tmp.mount of your own, left as it is", out)
        self.ep.storage_tune("off")
        self.assertTrue(os.path.exists("/etc/systemd/system/tmp.mount"))


class TestCustomPlan(Base):
    MINE = {"max_freq_mhz": 2700, "v3d_freq": 1020, "over_voltage_delta": 25000,
            "fan_profile": "Strong", "governor": "conservative"}

    def block(self):
        return self.config().split("# >>> pi5-energyplan")[1]

    def test_starts_empty_and_cant_be_applied(self):
        self.assertEqual(list(self.ep.PLANS)[-1], "Custom")
        self.assertTrue(self.ep.PLANS["Custom"]["empty"])
        self.assertRegex(cli("--list").stdout, r"Custom\s+\(empty: fill it in the window")
        r = cli("--apply", "Custom", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("The Custom plan is empty", r.stderr)
        self.assertEqual(self.config(), STOCK_CONFIG.rstrip("\n"))

    def test_fill_in_and_use(self):
        out = cli("--set-custom", json.dumps(self.MINE)).stdout
        self.assertIn("Custom plan saved: CPU 2700 MHz +0.025 V, GPU 1020 MHz, Strong fan, "
                      "conservative governor", out)
        self.assertRegex(cli("--list").stdout,
                         r"Custom\s+conservative\s+fan Strong\s+Yours: CPU 2.7 GHz \+0.025 V, "
                         r"GPU 1020 MHz.")
        out = cli("--apply", "Custom").stdout
        self.assertIn("Overclock: Custom", out)
        self.assertIn("2700 MHz unlocks after reboot", out)
        block = self.block()
        self.assertIn("# plan: Custom", block)
        self.assertIn("dtparam=fan_temp0=35000", block)          # Strong profile
        self.assertIn("arm_freq=2700\nover_voltage_delta=25000\nv3d_freq=1020\n", block)
        self.assertEqual(get(CPUFREQ + "/scaling_governor"), "conservative")
        self.assertIn("Saved plan  : Custom", cli("--status").stdout)

    def test_set_and_use_in_one_step(self):
        out = cli("--set-custom", json.dumps(self.MINE), "--apply", "Custom").stdout
        self.assertEqual(out.count("Saved to /boot/firmware/config.txt"), 1)   # applied once
        self.assertIn("# plan: Custom", self.config())

    def test_defaults_write_nothing_extra(self):
        cli("--set-custom", '{"max_freq_mhz": 2000, "v3d_freq": 960, "fan_profile": "Quiet",'
                            ' "governor": "powersave"}', "--apply", "Custom")
        block = self.block()
        self.assertIn("arm_freq=2000\n[all]\n# <<<", block)
        self.assertNotIn("v3d_freq", block)
        self.assertNotIn("over_voltage", block)
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "2000000")
        self.assertFalse(self.ep.PLANS["Custom"].get("warn"))
        self.ep.reload_plan_settings()
        self.assertFalse(self.ep.PLANS["Custom"].get("warn"))
        self.assertNotIn("Overclock", cli("--apply", "Custom").stdout)

    def test_changing_it_while_in_use_applies_again(self):
        cli("--set-custom", json.dumps(self.MINE), "--apply", "Custom")
        out = cli("--set-custom", json.dumps(dict(self.MINE, max_freq_mhz=2500,
                                                  over_voltage_delta=0))).stdout
        self.assertIn("Saved to /boot/firmware/config.txt", out)
        self.assertIn("arm_freq=2500\nv3d_freq=1020\n", self.block())

    def test_lowest_speeds(self):
        out = cli("--set-custom", json.dumps(dict(self.MINE, min_freq_mhz=2000,
                                                  v3d_freq_min=800)), "--apply", "Custom").stdout
        self.assertIn("CPU 2700 MHz +0.025 V (min 2000 MHz), GPU 1020 MHz (min 800 MHz)", out)
        self.assertIn("min 2000 MHz", out)
        self.assertEqual(get(CPUFREQ + "/scaling_min_freq"), "2000000")    # right away
        self.assertIn("v3d_freq=1020\nv3d_freq_min=800\n", self.block())  # after reboot
        self.assertRegex(cli("--list").stdout, r"Yours: CPU 2.7 GHz \+0.025 V \(min 2.0 GHz\), "
                                               r"GPU 1020 MHz \(min 800\)")
        self.assertIn("min 2.0)", cli("--status").stdout)
        # the boot service puts the minimum back after a reboot
        put(CPUFREQ + "/scaling_min_freq", 1500000)
        self.no_sleep()
        self.ep.load_custom_plans()
        self.ep.boot_apply()
        self.assertEqual(get(CPUFREQ + "/scaling_min_freq"), "2000000")
        # another plan goes back to the normal lowest speed and drops the GPU line
        cli("--apply", "Eco")
        self.assertEqual(get(CPUFREQ + "/scaling_min_freq"), "1500000")
        self.assertNotIn("v3d_freq_min", self.block())
        # an overclock unlocks after the reboot: until then the min is capped at 2.4 GHz
        cli("--set-custom", json.dumps({"max_freq_mhz": 2700, "min_freq_mhz": 2700}),
            "--apply", "Custom")
        self.assertEqual(get(CPUFREQ + "/scaling_max_freq"), "2400000")
        self.assertEqual(get(CPUFREQ + "/scaling_min_freq"), "2400000")

    def test_lowest_speed_left_out_changes_nothing(self):
        cli("--set-custom", json.dumps(self.MINE), "--apply", "Custom")
        self.assertEqual(get(CPUFREQ + "/scaling_min_freq"), "1500000")
        self.assertNotIn("min", self.ep.PLANS["Custom"]["desc"] +
                         json.dumps(json.loads(get("/etc/pi5-energyplan/custom.json"))))
        self.assertNotIn("v3d_freq_min", self.block())

    def test_bad_values_are_refused(self):
        for bad, why in (({"max_freq_mhz": 3200}, "CPU speed: 1500-3000 MHz"),
                         ({"max_freq_mhz": 2650}, "in steps of 100"),
                         ({"v3d_freq": 1300}, "GPU speed: 600-1100 MHz"),
                         ({"max_freq_mhz": 2000, "min_freq_mhz": 2100},
                          "CPU min speed: at most the max speed (2000 MHz)"),
                         ({"min_freq_mhz": 1000}, "CPU min speed: 1500-3000 MHz"),
                         ({"v3d_freq_min": 1000}, "GPU min speed: at most the GPU speed"),
                         ({"v3d_freq_min": 505}, "GPU min speed: 600-1100 MHz"),
                         ({"over_voltage_delta": 75000}, "Extra voltage"),
                         ({"fan_profile": "Turbo"}, "Fan profile"),
                         ({"governor": "fast"}, "Governor"),
                         ([1, 2], "expected settings")):
            r = cli("--set-custom", json.dumps(bad), check=False)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn(why, r.stderr)
        self.assertTrue(self.ep.PLANS["Custom"]["empty"])
        self.assertFalse(os.path.exists("/etc/pi5-energyplan/custom.json"))

    def test_editor_wins_over_the_row_pickers_and_sweet_spot_stays(self):
        self.ep.save_custom_plan(2600, 0)                   # a Sweet Spot from the test
        cli("--set-custom", json.dumps(self.MINE))
        cli("--set-fan", "Custom", "Max")
        cli("--set-governor", "Custom", "performance")
        self.assertRegex(cli("--list").stdout, r"Custom\s+performance\s+fan Max")
        cli("--set-custom", json.dumps(dict(self.MINE, fan_profile="Quiet")))
        lst = cli("--list").stdout
        self.assertRegex(lst, r"Custom\s+conservative\s+fan Quiet")
        self.assertIn("Sweet Spot", lst)
        self.assertEqual(lst.strip().splitlines()[-1].split()[0], "Custom")   # still last
        self.assertNotIn("Custom", json.loads(get("/etc/pi5-energyplan/fans.json")))


class TestConfigLinesKeepTheirSection(Base):
    """A line added at the end of config.txt (e.g. an audio HAT overlay) must keep
    working after plan changes and updates."""
    AUDIO = "dtoverlay=hifiberry-dacplus"
    CFG = "/boot/firmware/config.txt"

    def setUp(self):
        super().setUp()
        put(self.CFG, PI_OS_CONFIG.rstrip("\n"))

    def add_at_the_end(self, line):
        with open(self.CFG, "a") as f:
            f.write(line + "\n")

    def test_lines_added_after_the_block_stay_under_all(self):
        cli("--apply", "Cool")
        self.add_at_the_end(self.AUDIO)
        self.assertEqual(section_of(self.config(), self.AUDIO), "all")
        for plan in ("Eco", "OC 2.6 GHz", "Balanced"):
            cli("--apply", plan)
            self.assertEqual(section_of(self.config(), self.AUDIO), "all", plan)
        cli("--update-root")
        self.assertEqual(section_of(self.config(), self.AUDIO), "all")
        self.assertEqual(self.config().count(self.AUDIO), 1)
        before = self.config()
        cli("--apply", "Balanced")                       # and nothing piles up
        self.assertEqual(self.config(), before)

    def test_update_repairs_what_an_older_version_moved(self):
        old = os.path.join(PREVIOUS, "pi5_energyplan_original.py")
        run = lambda *a: subprocess.run([sys.executable, old, *a], check=True,
                                        capture_output=True)
        run("--apply", "Cool")
        self.add_at_the_end(self.AUDIO)
        run("--apply", "Eco")                            # the old bug
        self.assertEqual(section_of(self.config(), self.AUDIO), "cm5")   # sound gone
        broken = self.config()

        out = cli("--update-root").stdout                # what an update runs
        self.assertIn(f"config.txt repaired: {self.AUDIO} moved back under [all]", out)
        self.assertIn("under [cm5]", out)
        self.assertEqual(section_of(self.config(), self.AUDIO), "all")
        self.assertEqual(section_of(self.config(), "dtoverlay=dwc2,dr_mode=host"), "cm5")
        self.assertEqual(get(self.CFG + ".before-repair"), broken.rstrip("\n"))
        self.assertIn("# plan: Eco", self.config())
        fixed = self.config()
        self.assertNotIn("repaired", cli("--update-root").stdout)   # only once
        self.assertEqual(self.config(), fixed)

    def test_lines_that_were_in_cm5_from_the_start_are_left_alone(self):
        put(self.CFG, PI_OS_CONFIG.replace("dtoverlay=dwc2,dr_mode=host",
                                           "dtoverlay=dwc2,dr_mode=host\ndtparam=pciex1"))
        cli("--apply", "Cool")
        cli("--apply", "Eco")
        self.assertEqual(section_of(self.config(), "dtparam=pciex1"), "cm5")
        self.assertFalse(os.path.exists(self.CFG + ".before-repair"))


PREVIOUS = os.path.join(HERE, "previous")


class TestUpgrade(Base):
    """Updating over an installed previous version, with the command line."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=self.home)
        self.old_dir = os.path.join(self.home, "pi5-energyplan")

    def tearDown(self):
        subprocess.run(["pkill", "-f", "pi5_energyplan.py --tr" + "ay"])
        shutil.rmtree(self.home, ignore_errors=True)

    def run_py(self, script, *args, check=True):
        r = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                           env=self.env, timeout=120)
        if check:
            self.assertEqual(r.returncode, 0, f"{args}: {r.stdout}{r.stderr}")
        return r

    def install_sh(self, *args, src_dir=None, check=True):
        script = os.path.join(src_dir or os.path.dirname(HERE), "install.sh")
        r = subprocess.run(["sh", script, *args], capture_output=True, text=True,
                           env=self.env, timeout=180)
        if check:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def set_up_previous(self, name):
        """Install a previous version the way its users did: copy in ~/pi5-energyplan,
        menu entry, panel icon, a plan, UPS settings, a fan choice."""
        os.makedirs(self.old_dir)
        old = os.path.join(self.old_dir, "pi5_energyplan.py")
        shutil.copy2(os.path.join(PREVIOUS, name), old)
        for args in (["--install-launcher"], ["--install-tray"], ["--apply", "Cool"],
                     ["--ups-config", '{"enabled": true, "max": 85, "min": 25}'],
                     ["--set-fan", "Eco", "Strong"]):
            self.run_py(old, *args)
        put("/etc/pi5-energyplan/charge_pin.json", '{"off_level": "low"}')  # old faulty data
        put("/etc/pi5-energyplan/charge_pin_v2.json", '{"off_level": "low", "status": '
            '"unknown", "tried_flip": true}')      # possibly learned from the gauge lag
        self.assertEqual(get("/usr/local/bin/pi5_energyplan.py"), get(old))
        return old

    def check_updated(self, old, previous_name):
        new = get(SCRIPT)
        self.assertEqual(get("/usr/local/bin/pi5_energyplan.py"), new)
        self.assertEqual(get("/var/lib/pi5-energyplan/previous-version.py"),
                         get(os.path.join(PREVIOUS, previous_name)))
        # settings from the old version are kept, new ones get their defaults
        cfg = json.loads(get("/etc/pi5-energyplan/battery.json"))
        self.assertEqual((cfg["enabled"], cfg["max"], cfg["min"]), (True, 85, 25))
        self.assertEqual(json.loads(get("/etc/pi5-energyplan/fans.json")), {"Eco": "Strong"})
        self.assertFalse(os.path.exists("/etc/pi5-energyplan/charge_pin.json"))
        self.assertFalse(os.path.exists("/etc/pi5-energyplan/charge_pin_v2.json"))
        conf = self.config()
        self.assertEqual(conf.count("# >>> pi5-energyplan"), 1)
        self.assertIn("# plan: Cool", conf)
        self.assertIn("systemctl try-restart pi5-battery-guard.service", calls())
        units = sorted(glob.glob("/etc/systemd/system/*"))
        self.assertEqual(units, ["/etc/systemd/system/pi5-battery-guard.service",
                                 "/etc/systemd/system/pi5-energyplan.service"])
        st = self.run_py("/usr/local/bin/pi5_energyplan.py", "--status").stdout
        self.assertIn("Saved plan  : Cool", st)
        self.assertEqual(self.run_py("/usr/local/bin/pi5_energyplan.py", "--version")
                         .stdout.strip(), self.ep.VERSION)

        # the user part: the old copy the menu / panel used is brought up to date
        out = self.run_py("/usr/local/bin/pi5_energyplan.py", "--install-desktop").stdout
        self.assertIn(f"Older copy updated to version {self.ep.VERSION}: {old}", out)
        self.assertEqual(get(old), new)
        self.assertEqual(get(old + ".previous"), get(os.path.join(PREVIOUS, previous_name)))
        apps = glob.glob(self.home + "/.local/share/applications/*")
        auto = glob.glob(self.home + "/.config/autostart/*")
        self.assertEqual(len(apps), 1)
        self.assertEqual(len(auto), 1)
        self.assertIn("/usr/local/bin/pi5_energyplan.py\n", get(apps[0]))
        self.assertIn("/usr/local/bin/pi5_energyplan.py --tray", get(auto[0]))

    def test_update_original_version_with_install_sh(self):
        old = self.set_up_previous("pi5_energyplan_original.py")
        out = self.install_sh().stdout
        self.assertIn("Updating from version an older one without a number to "
                      f"{self.ep.VERSION}", out)
        self.check_updated(old, "pi5_energyplan_original.py")
        before = {f: get(f) for f in glob.glob("/etc/pi5-energyplan/*") +
                  glob.glob("/etc/systemd/system/*") + ["/boot/firmware/config.txt"]}
        out = self.install_sh().stdout                     # running it again
        self.assertIn(f"Version {self.ep.VERSION} is already installed", out)
        self.assertEqual(get("/var/lib/pi5-energyplan/previous-version.py"),
                         get(os.path.join(PREVIOUS, "pi5_energyplan_original.py")))
        self.assertEqual({f: get(f) for f in before}, before)
        self.assertEqual(self.run_py("/usr/local/bin/pi5_energyplan.py", "--install-desktop")
                         .stdout.strip(), "Menu entry created: " + self.home +
                         "/.local/share/applications/pi5-energyplan.desktop (look under "
                         "Preferences/System Tools)\nBattery icon added to the panel and "
                         "set to start at login.\nRemove it: rm " + self.home +
                         "/.config/autostart/pi5-battery-tray.desktop  (and log out/in)")

    def test_update_first_release_with_install_sh(self):
        old = self.set_up_previous("pi5_energyplan_first_release.py")
        out = self.install_sh().stdout
        self.assertIn(f"to {self.ep.VERSION}", out)
        self.check_updated(old, "pi5_energyplan_first_release.py")

    def test_update_with_the_scripts_own_update_command(self):
        """Old way: put the new file in ~/pi5-energyplan, then run --update there."""
        old = self.set_up_previous("pi5_energyplan_original.py")
        shutil.copy2(SCRIPT, old)
        out = self.run_py(old, "--update").stdout
        self.assertIn(f"System copy updated to version {self.ep.VERSION}", out)
        self.assertIn("Old charge-pin data removed", out)
        self.assertEqual(get("/usr/local/bin/pi5_energyplan.py"), get(SCRIPT))
        self.assertIn("systemctl try-restart pi5-battery-guard.service", calls())
        self.assertFalse(os.path.exists("/etc/pi5-energyplan/charge_pin.json"))
        self.assertEqual(len(glob.glob(self.home + "/.config/autostart/*")), 1)
        self.assertIn("System part already up to date", self.run_py(old, "--update").stdout)

    def test_never_goes_back_to_an_older_version(self):
        self.install_sh()
        new = get("/usr/local/bin/pi5_energyplan.py")
        # an older download: install.sh refuses unless --force
        older = os.path.join(self.home, "old-download")
        os.makedirs(older)
        shutil.copy2(os.path.join(PREVIOUS, "pi5_energyplan_original.py"),
                     os.path.join(older, "pi5_energyplan.py"))
        shutil.copy2(os.path.join(os.path.dirname(HERE), "install.sh"), older)
        r = self.install_sh(src_dir=older, check=False)
        self.assertIn("packaging folder is missing", r.stdout)       # only some files
        shutil.copytree(os.path.join(os.path.dirname(HERE), "packaging"),
                        os.path.join(older, "packaging"))
        for f in ("install_gui.py", "README.md"):                # a whole download
            shutil.copy2(os.path.join(os.path.dirname(HERE), f), older)
        r = self.install_sh(src_dir=older, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"Version {self.ep.VERSION} is installed, which is newer", r.stdout)
        self.assertEqual(get("/usr/local/bin/pi5_energyplan.py"), new)
        # an older numbered copy that applies a plan or saves settings doesn't copy itself
        lower = os.path.join(older, "pi5_energyplan.py")
        put(lower, new.replace(f'VERSION = "{self.ep.VERSION}"', 'VERSION = "1.9.0"'))
        self.run_py(lower, "--apply", "Cool")
        self.run_py(lower, "--ups-config", '{"enabled": true}')
        self.run_py(lower, "--set-fan", "Cool", "Max")
        self.assertIn("A newer version (" + self.ep.VERSION,
                      self.run_py(lower, "--update").stdout)
        self.assertEqual(get("/usr/local/bin/pi5_energyplan.py"), new)
        # --force does it on purpose
        self.install_sh("--force", src_dir=older)
        self.assertIn('VERSION = "1.9.0"', get("/usr/local/bin/pi5_energyplan.py"))


REAL_APT = "/usr/bin/apt-get"


@unittest.skipUnless(shutil.which("apt-ftparchive") and shutil.which("gpg") and
                     os.path.exists(REAL_APT), "needs apt-ftparchive (apt-utils) and gpg")
class TestAptRepository(Base):
    """The GitHub release path, end to end with the real apt: what the Pi's updater
    (PackageKit) and `apt upgrade` see."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "src")
        repo = os.path.dirname(HERE)
        os.makedirs(self.src)
        for f in ("pi5_energyplan.py", "install.sh", "install_gui.py", "README.md"):
            shutil.copy2(os.path.join(repo, f), self.src)
        shutil.copytree(os.path.join(repo, "packaging"), os.path.join(self.src, "packaging"))
        for f in glob.glob(os.path.join(self.src, "packaging", "apt-repo-key.asc")):
            os.remove(f)                                 # make a fresh test key
        self.site = os.path.join(self.tmp, "site", "apt")
        apt = os.path.join(self.tmp, "apt")
        for d in ("state/lists/partial", "cache/archives/partial"):
            os.makedirs(os.path.join(apt, d))
        self.status = os.path.join(apt, "status")
        self.apt_opts = ["-o", f"Dir::State={apt}/state", "-o", f"Dir::State::status={self.status}",
                         "-o", f"Dir::Cache={apt}/cache", "-o", "Dir::Etc::sourcelist=/dev/null",
                         "-o", "Dir::Etc::sourceparts=/etc/apt/sources.list.d",
                         "-o", "APT::Sandbox::User=root", "-o", "Debug::NoLocking=1"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, *args, env=None):
        r = subprocess.run(args, capture_output=True, text=True, timeout=180,
                           env=dict(os.environ, **(env or {})))
        self.assertEqual(r.returncode, 0, f"{args}: {r.stdout}{r.stderr}")
        return r.stdout

    def make_key(self):
        out = self.sh("sh", os.path.join(self.src, "packaging", "make-signing-key.sh"),
                      env={"TMPDIR": self.tmp})
        secret = [w for w in out.split() if "pi5-energyplan-signing-key" in w][0]
        self.secret = get(secret)
        self.assertIn("BEGIN PGP PRIVATE KEY BLOCK", self.secret)
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK",
                      get(os.path.join(self.src, "packaging", "apt-repo-key.asc")))

    def release(self, version, key=None):
        """What the GitHub workflow does for tag v<version>."""
        script = os.path.join(self.src, "pi5_energyplan.py")
        put(script, get(script).replace(f'VERSION = "{self.ep.VERSION}"',
                                        f'VERSION = "{version}"', 1).replace(
            f'VERSION = "{getattr(self, "last", self.ep.VERSION)}"', f'VERSION = "{version}"', 1))
        self.last = version
        dist = os.path.join(self.tmp, "dist-" + version)
        deb = self.sh("sh", os.path.join(self.src, "packaging", "build-deb.sh"), self.src, dist,
                      env={"APT_URL": "file://" + self.site}).strip().splitlines()[-1]
        self.sh("sh", os.path.join(self.src, "packaging", "publish-apt-repo.sh"), dist, self.site,
                env={"APT_SIGNING_KEY": key or self.secret})
        return deb

    def set_installed(self, deb):
        """dpkg's record of an installed package: its control fields + Status."""
        control = subprocess.run(["dpkg-deb", "-f", deb], capture_output=True, text=True,
                                 check=True).stdout
        put(self.status, control.replace("Version:", "Status: install ok installed\nVersion:",
                                         1).rstrip("\n") + "\n")

    def fresh_lists(self):
        shutil.rmtree(os.path.join(self.tmp, "apt/state/lists"))
        os.makedirs(os.path.join(self.tmp, "apt/state/lists/partial"))

    def apt(self, *args, check=True):
        r = subprocess.run([REAL_APT, *self.apt_opts, *args], capture_output=True, text=True,
                           timeout=120)
        if check:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def upgradable(self):
        r = subprocess.run(["/usr/bin/apt", *self.apt_opts, "list", "--upgradable"],
                           capture_output=True, text=True, timeout=60)
        return [line for line in r.stdout.splitlines() if line.startswith("pi5-energyplan/")]

    def test_new_version_reaches_the_pi_updater(self):
        self.make_key()
        deb = self.release("2.0.0")
        # install 2.0.0 like install.sh does: the package adds the signed update source
        self.sh("apt-get", "install", "-y", deb)
        src = get("/etc/apt/sources.list.d/pi5-energyplan.sources")
        self.assertIn(f"URIs: file://{self.site}", src)
        self.assertIn("Signed-By: /usr/share/keyrings/pi5-energyplan-archive-keyring.asc", src)
        self.assertIn('VERSION = "2.0.0"', get("/usr/local/bin/pi5_energyplan.py"))
        self.set_installed(deb)
        self.apt("update")
        self.assertEqual(self.upgradable(), [])            # nothing new yet

        # a new release on GitHub: the updater now offers it
        self.release("2.0.1")
        self.apt("update")
        self.assertEqual(self.upgradable(),
                         ["pi5-energyplan/stable 2.0.1 all [upgradable from: 2.0.0]"])
        policy = subprocess.run(["/usr/bin/apt-cache", *self.apt_opts, "policy",
                                 "pi5-energyplan"], capture_output=True, text=True).stdout
        self.assertIn("Installed: 2.0.0", policy)
        self.assertIn("Candidate: 2.0.1", policy)

        # clicking "Install" in the updater: apt downloads (checks signature + hashes)
        # and dpkg runs the package's install step
        dl = os.path.join(self.tmp, "download")
        os.makedirs(dl)
        r = subprocess.run([REAL_APT, *self.apt_opts, "download", "pi5-energyplan"], cwd=dl,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        new_deb = glob.glob(dl + "/*.deb")[0]
        self.assertTrue(new_deb.endswith("pi5-energyplan_2.0.1_all.deb"))
        cli("--apply", "Cool")                              # so there are services to refresh
        out = self.sh("apt-get", "install", "-y", new_deb)
        self.assertIn("Updating from version 2.0.0 to 2.0.1", out)
        self.assertIn('VERSION = "2.0.1"', get("/usr/local/bin/pi5_energyplan.py"))
        self.assertIn('VERSION = "2.0.0"', get("/var/lib/pi5-energyplan/previous-version.py"))
        self.assertIn("systemctl try-restart pi5-battery-guard.service", calls())
        self.set_installed(new_deb)
        self.apt("update")
        self.assertEqual(self.upgradable(), [])

    def test_tampered_or_wrongly_signed_repository_is_refused(self):
        self.make_key()
        deb = self.release("2.0.0")
        self.sh("apt-get", "install", "-y", deb)
        self.set_installed(deb)
        self.apt("update")
        # someone changes the index after it was signed
        pk = os.path.join(self.site, "dists/stable/main/binary-amd64/Packages")
        put(pk, get(pk).replace("Version: 2.0.0", "Version: 9.9.9"))
        os.remove(pk + ".gz")
        self.fresh_lists()
        r = self.apt("update", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Hash Sum mismatch", r.stdout + r.stderr)
        # a repository signed with another key
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.sh("sh", "-c", f"GNUPGHOME={other} gpg --batch --passphrase '' --quick-gen-key "
                            "evil ed25519 sign never")
        evil = self.sh("sh", "-c", f"GNUPGHOME={other} gpg --batch --armor --export-secret-keys")
        self.release("2.0.2", key=evil)
        self.fresh_lists()
        r = self.apt("update", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout + r.stderr, "NO_PUBKEY|not signed|signatures couldn't be verified")
        self.assertEqual(self.upgradable(), [])


SCREEN_CHECK = r"""
import importlib.util, json, sys, time
sys.argv = ["x"]
sys.path.insert(0, %(here)r)
import test_sim as t
res = {}
def info(win, name):
    for _ in range(8):
        win.update()
    res[name] = dict(w=win.winfo_width(), h=win.winfo_height(), x=win.winfo_rootx(),
                     y=win.winfo_rooty(), sw=win.winfo_screenwidth(),
                     sh=win.winfo_screenheight(), scrolls=win.scrolls)
ep = t.load_module()
app = ep.App()
info(app.root, "main")
app.open_ups_settings()
info(app.ups_win, "ups")
app.open_custom_editor()
info(app.custom_win, "custom")
app.root.destroy()
spec = importlib.util.spec_from_file_location("ig", %(gui)r)
ig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ig)
inst = ig.Installer()
info(inst.root, "installer")
print(json.dumps(res))
"""


@unittest.skipUnless(shutil.which("Xvfb") and importlib.util.find_spec("tkinter"),
                     "needs Xvfb and tkinter")
class TestSmallScreens(Base):
    def measure(self, w, h, n):
        xvfb = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", f"{w}x{h}x24",
                                 "-nolisten", "tcp"], stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                    break
                time.sleep(0.1)
            code = SCREEN_CHECK % {"here": HERE,
                                   "gui": os.path.join(os.path.dirname(HERE), "install_gui.py")}
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                               env=dict(os.environ, DISPLAY=f":{n}"), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout.strip().splitlines()[-1])
        finally:
            xvfb.kill()
            xvfb.wait()

    def test_windows_fit_every_screen(self):
        cli("--ups-config", '{"enabled": true, "saver_enabled": true}')
        sizes = [(800, 480), (1024, 600), (1280, 720), (1920, 1080)]
        big = None
        for i, (w, h) in enumerate(sizes):
            res = self.measure(w, h, 90 + i)
            for name, r in res.items():
                where = f"{name} at {w}x{h}: {r}"
                # inside the screen, leaving room for the taskbar and title bar
                self.assertLessEqual(r["x"] + r["w"], w, where)
                self.assertLessEqual(r["y"] + r["h"], h - 60, where)
            if (w, h) == (800, 480):
                self.assertTrue(res["main"]["scrolls"][0], "main window should scroll")
                self.assertTrue(res["ups"]["scrolls"][0], "UPS settings should scroll")
            if (w, h) == (1920, 1080):
                big = res
        # on a big screen nothing scrolls: the windows look as they always did
        self.assertEqual(big["main"]["scrolls"], [False, False])
        self.assertEqual(big["ups"]["scrolls"], [False, False])
        self.assertFalse(big["installer"]["scrolls"])


HAVE_TK = bool(importlib.util.find_spec("tkinter") and os.environ.get("DISPLAY"))


def widget_texts(widget):
    """Every text shown in a Tk window, to check what's there."""
    out = []
    for w in widget.winfo_children():
        try:
            out.append(w.cget("text"))
        except Exception:  # noqa: BLE001
            pass
        out += widget_texts(w)
    return out
KEEP_TK = []


class TestInstallLauncher(unittest.TestCase):
    def test_double_click_launcher_finds_the_installer(self):
        """Run the .desktop Exec line the way PCManFM (libfm) does: %k = the folder
        of the .desktop file, unquoted, then split like a shell would."""
        import shlex
        import tempfile
        repo = os.path.dirname(HERE)
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "my downloads", "x1202-main")
            os.makedirs(folder)
            for f in ("install_gui.py", "install.sh", "Install Pi 5 Energy Plan.desktop"):
                shutil.copy2(os.path.join(repo, f), folder)
            with open(os.path.join(folder, "Install Pi 5 Energy Plan.desktop")) as f:
                exec_line = [x for x in f.read().splitlines() if x.startswith("Exec=")][0]
            argv = shlex.split(exec_line[5:].replace("%k", folder))
            env = {k: v for k, v in os.environ.items()
                   if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
            r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
        # it got into the folder and started the installer, which then stops
        # because this test has no desktop
        self.assertIn("No desktop found", r.stderr)


@unittest.skipUnless(HAVE_TK, "needs tkinter and a display (use xvfb-run)")
class TestInstallGui(Base):
    def test_no_ups_options_on_other_boards(self):
        as_board("Raspberry Pi Zero 2 W Rev 1.0", 1000000, 600000)
        spec = importlib.util.spec_from_file_location(
            "ig", os.path.join(os.path.dirname(HERE), "install_gui.py"))
        ig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ig)
        app = ig.Installer()
        try:
            app.root.update()
            texts = widget_texts(app.root)
            self.assertFalse(any("Battery protection" in t or "bootloader" in t for t in texts))
            self.assertIn("Menu entry", texts)
            self.assertTrue(any(t.startswith("Board: Raspberry Pi Zero 2 W") for t in texts))
            self.assertFalse(app.opt_ups.get() or app.opt_eeprom.get())
        finally:
            app.root.destroy()
            KEEP_TK.append(app)

    def test_button_says_update_reinstall_or_go_back(self):
        spec = importlib.util.spec_from_file_location(
            "ig", os.path.join(os.path.dirname(HERE), "install_gui.py"))
        ig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ig)
        ver = self.ep.VERSION
        cases = [(None, "Install", False),
                 (get(os.path.join(PREVIOUS, "pi5_energyplan_original.py")),
                  f"Update to {ver}", False),
                 (get(SCRIPT), "Install again", False),
                 (get(SCRIPT).replace(f'VERSION = "{ver}"', 'VERSION = "9.0.0"'),
                  f"Go back to {ver}", True)]
        for content, label, force in cases:
            if content is None:
                if os.path.exists(ig.INSTALLED):
                    os.remove(ig.INSTALLED)
            else:
                put(ig.INSTALLED, content)
            app = ig.Installer()
            try:
                app.root.update()
                buttons = [w.cget("text") for w in app.btns.winfo_children()]
                self.assertIn(label, buttons)
                self.assertEqual(app.force, force)
            finally:
                app.root.destroy()
                KEEP_TK.append(app)

    def test_install_and_uninstall_from_the_window(self):
        import tempfile
        home = tempfile.mkdtemp()
        os.environ["HOME"] = home
        spec = importlib.util.spec_from_file_location(
            "ig", os.path.join(os.path.dirname(HERE), "install_gui.py"))
        ig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ig)
        ig.os.geteuid = lambda: 1000        # behave like the desktop user: use pkexec
        hw(i2c=1)
        app = ig.Installer()
        try:
            self.assertTrue(app.opt_ups.get() and app.opt_eeprom.get())
            app.start_install()
            end = time.time() + 120
            while time.time() < end and not app.step.cget("text").startswith(("✔", "✘")):
                app.root.update()
                time.sleep(0.02)
            self.assertTrue(app.step.cget("text").startswith("✔ Installed"),
                            app.step.cget("text") + app.log.get("1.0", "end")[-2000:])
            self.assertTrue(app.reboot_needed)
            labels = sorted(b.cget("text") for b in app.btns.winfo_children())
            self.assertEqual(labels, ["Close", "Open Energy Plan", "Reboot now"])
            pk = [c for c in calls() if c.startswith("pkexec")]
            self.assertEqual(len(pk), 1)                 # one password prompt
            self.assertIn("--skip-desktop --ups-recommended", pk[0])
            self.assertTrue(os.path.exists("/usr/local/bin/pi5_energyplan.py"))
            self.assertTrue(os.path.exists("/etc/pi5-energyplan/battery.json"))
            self.assertTrue(os.path.exists(
                home + "/.local/share/applications/pi5-energyplan.desktop"))
            self.assertTrue(os.path.exists(home + "/.config/autostart/pi5-battery-tray.desktop"))

            ig.messagebox.askyesno = lambda *a: True
            app.start_uninstall()
            end = time.time() + 60
            while time.time() < end and not app.step.cget("text").startswith(
                    ("✔ Uninstalled", "✘")):
                app.root.update()
                time.sleep(0.02)
            self.assertEqual(app.step.cget("text"), "✔ Uninstalled.")
            self.assertFalse(os.path.exists("/usr/local/bin/pi5_energyplan.py"))
        finally:
            subprocess.run(["pkill", "-f", "pi5_energyplan.py --tray"])
            app.root.destroy()
            KEEP_TK.append(app)   # freed at exit on the main thread (Tk aborts otherwise)
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(HAVE_TK, "needs tkinter and a display (use xvfb-run)")
class TestGuiOtherBoards(Base):
    def texts(self, widget):
        return widget_texts(widget)

    def test_zero_2_w_window(self):
        as_board("Raspberry Pi Zero 2 W Rev 1.0", 1000000, 600000)
        ep = load_module()
        app = ep.App()
        try:
            for _ in range(5):
                app.root.update()
            t = self.texts(app.root)
            self.assertIn("Raspberry Pi Zero 2 W Rev 1.0  ·  no fan control  ·  no UPS", t)
            for gone in ("UPS settings…", "Find sweet spot…", "Battery in panel", "Fan",
                         "Sweet spot", "OC 2.6 GHz"):
                self.assertNotIn(gone, t)
            self.assertEqual(app.fan_vars, {})
            app.open_custom_editor()
            app.root.update()
            e = self.texts(app.custom_win)
            self.assertIn("CPU max speed", e)
            for gone in ("GPU (3D) speed", "Fan profile", "Extra CPU voltage"):
                self.assertNotIn(gone, e)
            self.assertEqual(app.custom_controls["cpu"].get(), 1000)
        finally:
            app.root.destroy()
            KEEP_TK.append(app)

    def test_pi5_without_ups_says_why(self):
        hw(ups=False)
        ep = load_module()
        app = ep.App()
        try:
            app.root.update()
            t = self.texts(app.root)
            self.assertTrue(any(x.startswith("No X120x UPS found: UPS not answering")
                                for x in t), t)
            self.assertNotIn("UPS settings…", t)
            self.assertIn("Find sweet spot…", t)              # still a Pi 5
        finally:
            app.root.destroy()
            KEEP_TK.append(app)


FAKE_WINDOW = """\
import os, socket, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind("\\0pi5-energyplan-gui")
if os.environ.get("FAKE_HOW") == "stuck":
    s.listen(1)          # takes the call but never answers, like a frozen window
print("ready", flush=True)
time.sleep(600)
"""


def gui_lock_taken():
    with open("/proc/net/unix") as f:
        return any(line.rstrip().endswith("@pi5-energyplan-gui") for line in f)


@unittest.skipUnless(HAVE_TK, "needs tkinter and a display (use xvfb-run)")
class TestWindowLaunch(Base):
    """Opening Energy Plan again (menu or panel icon) while it is already open."""
    def setUp(self):
        super().setUp()
        import tempfile
        self.home = tempfile.mkdtemp()
        self.env = dict(os.environ, HOME=self.home)
        self.procs = []
        self.addCleanup(self.stop_all)
        self.addCleanup(shutil.rmtree, self.home, True)

    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            if p.stdout:
                p.stdout.close()
        for _ in range(50):
            if not gui_lock_taken():
                break
            time.sleep(0.1)

    def start(self, argv):
        p = subprocess.Popen(argv, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.procs.append(p)
        return p

    def wait_open(self, proc):
        for _ in range(300):
            if gui_lock_taken() and proc.poll() is None:
                return
            self.assertIsNone(proc.poll(), "window didn't stay open")
            time.sleep(0.1)
        self.fail("window never opened")

    def log(self):
        try:
            with open(os.path.join(self.home, ".cache/pi5-energyplan/window.log")) as f:
                return f.read()
        except OSError:
            return ""

    def fake_window(self, how):
        folder = os.path.join(self.home, "old")
        os.makedirs(folder)
        path = os.path.join(folder, "pi5_energyplan.py")
        with open(path, "w") as f:
            f.write(FAKE_WINDOW)
        p = subprocess.Popen([sys.executable, path], env=dict(self.env, FAKE_HOW=how),
                             stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        self.assertEqual(p.stdout.readline().strip(), "ready")
        return p

    def test_second_launch_brings_the_open_window_back(self):
        a = self.start([sys.executable, SCRIPT])
        self.wait_open(a)
        b = subprocess.run([sys.executable, SCRIPT], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertIn("brought it to the front", b.stdout)
        self.assertIsNone(a.poll(), "the open window must stay open")
        self.assertIn("brought to the front", self.log())
        self.assertIn("window opened (version", self.log())

    def test_frozen_window_is_replaced(self):
        old = self.fake_window("stuck")
        b = self.start([sys.executable, SCRIPT])
        self.wait_open(b)
        for _ in range(300):
            if old.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(old.poll(), "the frozen window should be closed")
        time.sleep(1)
        self.assertIsNone(b.poll(), "the new window should be open")

    def test_old_version_window_is_replaced(self):
        old = self.fake_window("old")      # holds the lock but can't be asked anything
        b = self.start([sys.executable, SCRIPT])
        self.wait_open(b)
        old.wait(timeout=30)
        time.sleep(1)
        self.assertIsNone(b.poll(), "the new window should be open")

    def test_minimised_window_comes_back(self):
        app = self.ep.App()
        try:
            app.root.update()
            app.root.iconify()
            app.root.update()
            app.show_window()
            app.root.update()
            self.assertEqual(app.root.state(), "normal")
        finally:
            app.root.destroy()
            KEEP_TK.append(app)

    def test_an_error_doesnt_stop_the_updates(self):
        app = self.ep.App()
        try:
            app._show_error = lambda *e: None
            app.root.report_callback_exception = app._show_error
            app.root.update()
            real, app._refresh = app._refresh, lambda: 1 / 0
            with self.assertRaises(ZeroDivisionError):
                app.refresh()
            app._refresh = real
            pending = app.root.tk.call("after", "info")
            names = [app.root.tk.call("after", "info", i)[0] for i in pending]
            self.assertTrue(any("refresh" in str(n) for n in names), names)
        finally:
            app.root.destroy()
            KEEP_TK.append(app)

    def test_only_windows_are_replaced(self):
        folder = os.path.join(self.home, "x")
        os.makedirs(folder)
        path = os.path.join(folder, "pi5_energyplan.py")
        with open(path, "w") as f:
            f.write("import time\ntime.sleep(600)\n")
        window = self.start([sys.executable, path])
        tray = self.start([sys.executable, path, "--tray"])
        time.sleep(0.3)
        found = self.ep.open_windows()
        self.assertIn(window.pid, found)
        self.assertNotIn(tray.pid, found)
        self.assertNotIn(os.getpid(), found)


@unittest.skipUnless(HAVE_TK, "needs tkinter and a display (use xvfb-run)")
class TestGui(Base):
    def test_window_builds_and_refreshes(self):
        ep = self.ep
        cli("--apply", "Cool")
        cli("--ups-config", json.dumps({"enabled": True, "saver_enabled": True}))
        app = ep.App()
        try:
            errors = []
            app._show_error = lambda *e: errors.append(e)
            app.root.report_callback_exception = app._show_error
            for _ in range(5):
                app.root.update()
            self.assertEqual(app.stat["Saved plan"].cget("text"), "Cool")
            self.assertEqual(app.stat["Governor"].cget("text"), "schedutil · Cool")
            self.assertIn("52.3", app.temp_lbl.cget("text"))
            self.assertEqual(app.bat_pct.cget("text"), "76%")
            self.assertEqual(app.buttons["Cool"].cget("bg"), ep.PLANS["Cool"]["color"])
            app.open_ups_settings()
            app.root.update()
            self.assertTrue(app.ups_win.winfo_exists())
            app.ups_win.destroy()
            picked = []
            win = app.choose("Test", [("a", "A"), ("b", "B")], picked.append, current="a")
            app.root.update()
            win.destroy()
            # the Custom plan: empty → editor → Save and use
            self.assertIn("Empty", app.desc_labels["Custom"].cget("text"))
            app.on_plan("Custom")                           # empty: opens the editor
            app.root.update()
            self.assertTrue(app.custom_win.winfo_exists())
            ep.messagebox.askyesno = lambda *a: True
            ctl = app.custom_controls
            self.assertEqual((ctl["cpu"].get(), ctl["gpu"].get(), ctl["fan"].get()),
                             (2400, 960, "Default"))         # starts at the Pi's defaults
            self.assertEqual((ctl["cpu_min"].get(), ctl["gpu_min"].get()), (1500, "Default"))
            ctl["cpu"].set(2600)
            ctl["cpu_min"].set(2800)
            ctl["gpu_min"].set("900")
            app.root.update()
            self.assertEqual(ctl["cpu_min"].get(), 2600)      # never above the max
            ctl["cpu_min"].set(1800)
            ctl["volt"].set("+0.025 V")                       # triggers the warning text
            app.root.update()
            self.assertIn("Overclock", ctl["note"].cget("text"))
            ctl["Save and use"].invoke()
            end = time.time() + 60
            while app.busy and time.time() < end:
                app.root.update()
                time.sleep(0.05)
            self.assertIn("# plan: Custom", self.config())
            self.assertIn("arm_freq=2600", self.config())
            self.assertIn("v3d_freq_min=900", self.config())
            self.assertEqual(get(CPUFREQ + "/scaling_min_freq"), "1800000")
            self.assertIn("Yours: CPU 2.6 GHz +0.025 V (min 1.8 GHz), GPU 960 MHz (default) "
                          "(min 900)", app.desc_labels["Custom"].cget("text"))
            # banners for the guard states
            b = {"on_battery": True, "soc": 18}
            g = {"enabled": True, "min": 20, "outage_minutes": 0}
            self.assertIn("critical", app.battery_banner(b, g)[0])
            g2 = dict(g, shutdown_pending=True, shutdown_at=time.time() + 90,
                      shutdown_reason="x")
            self.assertIn("Shutting down in 1:", app.battery_banner(b, g2)[0])
            self.assertEqual(errors, [])
        finally:
            app.root.destroy()
            KEEP_TK.append(app)   # freed at exit on the main thread (Tk aborts otherwise)


@unittest.skipUnless(shutil.which("dbus-daemon"), "needs dbus-daemon")
class TestTray(Base):
    def test_tray_icon_registers_and_reports(self):
        try:
            import gi
            gi.require_version("GdkPixbuf", "2.0")
            from gi.repository import Gio, GLib
        except (ImportError, ValueError):
            self.skipTest("needs PyGObject")
        d = subprocess.run(["dbus-daemon", "--session", "--fork", "--print-address=1",
                            "--print-pid=1"], capture_output=True, text=True, check=True)
        addr, pid = d.stdout.split()[:2]
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=addr)
        tray = None
        try:
            bus = Gio.DBusConnection.new_for_address_sync(
                addr, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
                Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
            got = []
            xml = ("<node><interface name='org.kde.StatusNotifierWatcher'>"
                   "<method name='RegisterStatusNotifierItem'><arg type='s' direction='in'/>"
                   "</method></interface></node>")

            def method(conn, sender, path, iface, name, params, inv):
                got.append((sender, params.unpack()[0]))
                inv.return_value(None)
            bus.register_object("/StatusNotifierWatcher",
                                Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0],
                                method, None, None)
            bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", "RequestName",
                          GLib.Variant("(su)", ("org.kde.StatusNotifierWatcher", 0)),
                          None, Gio.DBusCallFlags.NONE, -1, None)
            tray = subprocess.Popen([sys.executable, SCRIPT, "--tray"], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ctx = GLib.MainContext.default()
            end = time.time() + 15
            while not got and time.time() < end and tray.poll() is None:
                ctx.iteration(False)
                time.sleep(0.02)
            self.assertTrue(got, tray.stderr.read().decode() if tray.poll() is not None
                            else "tray never registered")
            sender, path = got[0]
            self.assertEqual(path, "/StatusNotifierItem")

            def prop(name):
                r = bus.call_sync(sender, path, "org.freedesktop.DBus.Properties", "Get",
                                  GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", name)),
                                  None, Gio.DBusCallFlags.NONE, 3000, None)
                return r.unpack()[0]
            tip = prop("ToolTip")
            self.assertEqual(tip[2], "Battery 76% · Plugged in")
            icon = prop("IconPixmap")
            self.assertEqual(icon[0][:2], (64, 64))
            self.assertEqual(len(icon[0][2]), 64 * 64 * 4)
            self.assertTrue(any(icon[0][2]))
        finally:
            if tray:
                tray.kill()
                tray.wait()
            os.kill(int(pid), 15)


if __name__ == "__main__":
    unittest.main()
