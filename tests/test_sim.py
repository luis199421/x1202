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
              "/run/pi5-energyplan", STATE):
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
        self.assertIn("# plan: Cool\n[all]\ndtparam=fan_temp0=40000\n", block)
        self.assertIn("dtparam=fan_temp3_speed=255\narm_freq=2400\n# <<<", block)
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
        self.assertIn("arm_freq=2800\nover_voltage_delta=50000\n", cfg)
        cli("--apply", "OC 3.0 GHz")
        self.assertIn("arm_freq=3000\nover_voltage_delta=50000\ngpu_freq=1000\n",
                      self.config())

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
        self.ep.time.sleep = lambda s: None
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
        self.assertIn("Saved: charge to 80%, safe shutdown at 20%, or after a 5 minutes "
                      "outage.", out)
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
                       [paused, resumed, saver_on, not_yet, not_yet, shutdown_started,
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
        cc.observe(False, 1, 70.0, 0)
        self.assertTrue(cc.observe(False, 1, 71.2, 60))     # rose while "paused"
        self.assertEqual(cc.off_level, "low")
        self.assertEqual(cc.pin_args(False), ["op", "dl"])
        cc.observe(False, 1, 72.0, 100)
        self.assertTrue(cc.observe(False, 1, 73.5, 200))    # still rising
        self.assertEqual(cc.status, "unsupported")
        self.assertEqual(cc.pin_args(False), ["ip", "pd"])  # pin released

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
        self.assertIn("arm_freq=2600\n# <<<", self.tryboot())

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

    def install(self, *args, user=None):
        env = dict(os.environ)
        env.pop("SUDO_USER", None)
        if user:
            env["SUDO_USER"] = user
        r = subprocess.run(["sh", self.INSTALL, *args], capture_output=True, text=True,
                           env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def apt_installed(self):
        line = [c for c in calls() if c.startswith("apt-get install")][-1]
        return line.split()[3:]

    def test_trixie_packages_and_i2c(self):
        hw(i2c=1, apt_missing=["policykit-1", "libraspberrypi-bin"])
        out = self.install()
        self.assertEqual(self.apt_installed(), [
            "python3", "python3-tk", "python3-smbus", "i2c-tools", "raspi-utils",
            "python3-gi", "gir1.2-gdkpixbuf-2.0", "librsvg2-common", "pkexec",
            "stress-ng", "python3-libgpiod"])
        self.assertIn("raspi-config nonint do_i2c 0", calls())
        self.assertIn("reboot once so the battery can be read", out)
        self.assertTrue(os.access("/usr/local/bin/pi5_energyplan.py", os.X_OK))
        self.assertIn("Battery     : 76.5%", out)          # --status ran at the end
        self.assertIn("skipped the menu entry", out)

    def test_older_package_names_used_when_needed(self):
        hw(i2c=0, apt_missing=["pkexec", "raspi-utils", "python3-smbus"])
        out = self.install()
        pk = self.apt_installed()
        for p in ("policykit-1", "libraspberrypi-bin", "python3-smbus2"):
            self.assertIn(p, pk)
        self.assertNotIn("raspi-config nonint do_i2c 0", calls())
        self.assertNotIn("reboot once", out)

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
        for p in ("/usr/local/bin/pi5_energyplan.py",
                  "/etc/systemd/system/pi5-energyplan.service",
                  "/etc/systemd/system/pi5-battery-guard.service"):
            self.assertFalse(os.path.exists(p), p)

    @unittest.skipUnless(os.environ.get("INSTALL_TEST_USER"),
                         "set INSTALL_TEST_USER to a throwaway desktop user")
    def test_desktop_user_part(self):
        user = os.environ["INSTALL_TEST_USER"]
        home = os.path.expanduser("~" + user)
        self.install(user=user)
        launcher = get(home + "/.local/share/applications/pi5-energyplan.desktop")
        self.assertIn("Exec=/usr/bin/python3 /usr/local/bin/pi5_energyplan.py", launcher)
        tray = get(home + "/.config/autostart/pi5-battery-tray.desktop")
        self.assertIn("/usr/local/bin/pi5_energyplan.py --tray", tray)
        self.install("--uninstall", user=user)
        self.assertFalse(os.path.exists(home + "/.config/autostart/pi5-battery-tray.desktop"))


HAVE_TK = bool(importlib.util.find_spec("tkinter") and os.environ.get("DISPLAY"))


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
            self.assertEqual(app.stat["Governor"].cget("text"), "schedutil")
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
