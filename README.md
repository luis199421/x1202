# Pi 5 Energy Plan

`pi5_energyplan.py` is a single Python script for a **Raspberry Pi 5** with the
**Active Cooler** and a **Geekworm X1202** UPS HAT (the X1200 and X1201 work too).
It gives you:

* **One-click energy plans**: CPU governor, maximum CPU speed and fan curve together,
  from "Underclock" (cool and silent) to "OC 3.0 GHz".
* **A UPS / battery manager for the X1202**: battery %, time left or time to full,
  a charge limit (for example stop at 80 %), safe shutdown on low battery or after a
  power cut, and a "battery saver" that slows the Pi down while it runs on battery.
* **A battery icon in the taskbar** (the panel), with % and charging state.
* **A "sweet spot" overclock test**: it finds the fastest overclock your own chip
  runs stably, using one-time test boots so a bad setting can't stop the Pi from booting.

It has a desktop window (GUI) and a full command line, so it also works over SSH.

![The Energy Plan window](docs/gui.png)

*(This screenshot was taken in the test simulator described under [Testing](#testing),
so the numbers come from fake hardware.)*

---

## Contents

1. [What it does on the Raspberry Pi 5](#1-what-it-does-on-the-raspberry-pi-5)
2. [What it does with the Geekworm X1202](#2-what-it-does-with-the-geekworm-x1202)
3. [Sweet spot overclock test](#3-sweet-spot-overclock-test)
4. [Installation](#4-installation)
5. [Using it](#5-using-it)
6. [Files it creates](#6-files-it-creates)
7. [Updating and uninstalling](#7-updating-and-uninstalling)
8. [Troubleshooting](#8-troubleshooting)
9. [Testing](#testing)

---

## 1. What it does on the Raspberry Pi 5

### The plans

| Plan | CPU governor | Max CPU | Fan profile | Notes |
|---|---|---|---|---|
| Underclock | powersave | 1.5 GHz | Silent | Coolest and quietest |
| Eco | conservative | 1.8 GHz | Quiet | Quiet, low power |
| Balanced | ondemand | 2.4 GHz | Default | Same as Raspberry Pi OS defaults |
| Cool | schedutil | 2.4 GHz | Cool | Full speed, kept cool |
| Max Cool | conservative | 2.0 GHz | Max | Coolest while still fairly fast |
| Performance | performance | 2.4 GHz | Cool | Always at top speed |
| OC 2.6 GHz | ondemand | 2.6 GHz, +0.025 V | Strong | Mild overclock |
| OC 2.8 GHz | ondemand | 2.8 GHz, +0.05 V | Strong | Medium overclock |
| OC 3.0 GHz | ondemand | 3.0 GHz, +0.05 V, GPU 1 GHz | Max | Not every chip is stable |
| Sweet Spot | ondemand | found by the test | Strong | Only appears after the [test](#3-sweet-spot-overclock-test) |

The window lets you change each plan's **governor** and **fan profile**. These choices
are saved in `/etc/pi5-energyplan/`. You can also edit the `PLANS` and `FAN_PROFILES`
tables at the top of the script.

Fan profiles set the Active Cooler's 4 steps as *(start temperature, hysteresis,
speed)*:

| Profile | Fan starts at | Full speed at |
|---|---|---|
| Silent | 60 °C | 80 °C |
| Quiet | 55 °C | 78 °C |
| Default | 50 °C | 75 °C (the Pi 5's own default) |
| Cool | 40 °C | 65 °C |
| Strong | 35 °C | 60 °C |
| Max | 30 °C | 55 °C |

### What changes when you pick a plan

| When | What | How |
|---|---|---|
| **Right away** | CPU governor and max frequency | writes `/sys/devices/system/cpu/cpufreq/policy*/scaling_governor` and `scaling_max_freq` |
| **Right away** (if the kernel allows it) | Fan start temperatures | writes the active trip points in `/sys/class/thermal/thermal_zone0` |
| **After a reboot** | Full fan curve (temperatures **and** speeds), `arm_freq`, `over_voltage_delta`, `gpu_freq` | a managed block at the end of `/boot/firmware/config.txt` |
| **At every boot** | Governor and max frequency again (Linux resets them on boot) | `pi5-energyplan.service` |

The block in `config.txt` looks like this. The script only ever changes the text between
the two markers:

```ini
# >>> pi5-energyplan (managed by pi5_energyplan.py) >>>
# plan: Cool
[all]
dtparam=fan_temp0=40000
dtparam=fan_temp0_hyst=3000
dtparam=fan_temp0_speed=100
...
arm_freq=2400
# <<< pi5-energyplan <<<
```

Safety details:

* The first time it edits `config.txt`, it saves the original as
  `config.txt.energyplan.bak`.
* Other `arm_freq`, `over_voltage`, `gpu_freq` or `dtparam=fan_temp…` lines in the
  `[all]` or `[pi5]` sections are commented out with `#energyplan-disabled#` so they
  can't override the plan. Lines under other sections such as `[pi4]` are left alone.
* Running the same plan again never adds a second block.
* Speeds above what the current boot allows (for example 2.8 GHz when the Pi booted
  at 2.4 GHz) are noted as "unlocks after reboot".

### Live status

The window and `--status` show the CPU temperature (with a 4.5-minute graph in the
window), fan level, PWM and RPM, CPU speed, governor, power warnings from
`vcgencmd get_throttled` (under-voltage, throttling), and the saved plan.

---

## 2. What it does with the Geekworm X1202

The X1202 holds four 18650 cells and powers the Pi 5 through pogo pins under the
board. The script talks to it through:

| Signal | Pi 5 pin | Used for |
|---|---|---|
| Fuel gauge (MAX1704x) | I2C bus 1, address `0x36` | Battery voltage, %, and charge rate (MAX17048) |
| Power-loss detect (PLD) | GPIO 6 | High = adapter connected, low = running on battery |
| Charge control | GPIO 16 | High = charging off, low = charging on |

### Battery display (no root needed)

* Battery %, cell voltage and a health label (Full / High / Medium / Low / Critical).
* Whether it's **plugged in, charging, full, or on battery**.
* **Time left on battery** or **time until full**. The board has no current sensor,
  so the script works this out from how fast the % changes over the last 5 minutes. It
  also uses the MAX17048's own charge-rate register when the chip has one. It shows
  "estimating…" for the first 1–2 minutes.
* The Pi's input voltage and total power use (from `vcgencmd pmic_read_adc`).
* Warning banners when running on battery, when the battery is low, or when a shutdown
  is counting down.

Three places show this:

* the Energy Plan window,
* the **panel icon** (`--install-tray`): a battery icon showing %, a level bar and ⚡
  while charging. Hover over it for details; click it to open the window.
* the terminal: `--battery` prints a new line every 3 seconds.

### Battery guard (background service, root)

When you save **UPS settings…** in the window (or run `--ups-config`), the script
installs `pi5-battery-guard.service`. It keeps running after you close the window and
starts at every boot. Every 3 seconds it does the following:

| Feature | What it does |
|---|---|
| **Charge limit** ("stop charging at X %") | Turns charging off at X % and back on 5 % below it. Keeping a Li-ion cell at 20–80 % gives it many more cycles. 100 % means no limit. |
| **Charge-pin self-check** | Checks that charging really pauses and resumes. If the battery keeps rising while it should be paused, the script tries the opposite pin level once. If that also fails, it gives up and leaves charging to the board. It never leaves charging switched off by mistake. |
| **Safe shutdown at Y %** | Only when **on battery**. It needs 3 readings in a row at or below Y % (or below 3.30 V per cell). Then it runs `shutdown -h +N` with a 1, 2 or 5-minute warning. **Plugging power back in cancels it.** |
| **Shutdown after an outage** | Optional: shut down after the power has been out for 1, 5, 15, 30 or 60 minutes. |
| **Battery saver** | On battery, below Z % (or as soon as the power goes out if Z is 100 %), switches to **Underclock** or **Eco**. When power returns, it switches back to your saved plan. |
| **Overcharge watch** | Warns in the window if a cell goes above 4.25 V. That means the board's charger isn't stopping. |
| **Never a boot loop** | It never shuts down while the adapter is plugged in. When the guard stops, charging is always switched back on. |

The script header also lists the problems in Geekworm's sample scripts
(github.com/geekworm-com/x120x) that this version avoids. For example, `gpiochip4`
no longer exists on newer kernels, and their scripts can shut down while charging or
keep postponing a shutdown.

> **Use only one UPS program.** If one of Geekworm's scripts (`bat.py`, `pld.py`,
> `merged.py`, `qtx120x.py`) runs at boot, remove it from cron, rc.local or autostart.
> Two programs would fight over GPIO 16 and the shutdown.

---

## 3. Sweet spot overclock test

**Find sweet spot…** in the window, or `sudo python3 pi5_energyplan.py --autotest`,
tries these steps in order:

`2.6 GHz +0 mV → 2.6 GHz +25 mV → 2.8 GHz +25 mV → 2.8 GHz +50 mV → 3.0 GHz +50 mV`

For each step:

1. The test settings go into **`tryboot.txt`**, not `config.txt`, and the Pi reboots
   with `reboot '0 tryboot'`. The firmware uses `tryboot.txt` for **that one boot
   only**.
2. After booting, it waits 45 seconds (time to cancel), then runs a stress test on all
   cores for 5 minutes. It uses `stress-ng --verify`, which the test installs if
   needed, or a built-in checksum test. The hardware watchdog is armed during the test.
3. A step **fails** if the CPU overheats (default 80 °C), throttles, reports
   under-voltage, or gives a wrong result. It also fails if the Pi freezes or resets:
   the next boot comes back with your normal `config.txt` and the test continues.
   After a crash or a wrong result, it tries the same speed with more voltage. If a
   step fails from heat or power, the test stops there.
4. At the end, the fastest step that passed is saved as the plan **"Sweet Spot"** and
   turned on. Then the Pi reboots normally.

The whole test takes about 20–40 minutes. Check progress with `--autotest-status`
and stop it with `sudo … --autotest-cancel`. If the Pi freezes for more than a minute,
unplug it and plug it back in: it always comes back with your normal settings.

Requirements: a Pi 5, an up-to-date bootloader (`sudo rpi-eeprom-update -a`), the
Active Cooler, and a strong power supply (the official 27 W one, or the X1202 on mains).

---

## 4. Installation

Made for **Raspberry Pi OS Trixie (Debian 13, 64-bit, desktop)**. It also works on
**Bookworm**. The command line also works on the Lite images.

| | Trixie (main target) | Bookworm |
|---|---|---|
| Python | 3.13 | 3.11 |
| `install.sh` | works as is | works as is (uses older package names where needed) |
| GPIO | `pinctrl`, with a libgpiod **v2** fallback | `pinctrl`, with a libgpiod v1 or v2 fallback |
| Desktop | labwc (Wayland) + wf-panel-pi | labwc or Wayfire (Wayland) + wf-panel-pi |

### Quick install (recommended)

On the Pi, in a terminal, as your normal desktop user:

```bash
git clone https://github.com/luis199421/x1202.git
cd x1202
sudo sh install.sh
sudo reboot          # only needed the first time, so I2C turns on
```

`install.sh` installs everything the script needs and can be run again at any time;
it never adds anything twice. It:

1. **Installs the packages** with apt. For each one it uses the Trixie package name,
   or the Bookworm name if that's the one apt knows:

   | Package | Needed for |
   |---|---|
   | `python3-tk` | The window (GUI) |
   | `python3-smbus` (or `python3-smbus2`) | Reading the X1202 battery over I2C |
   | `raspi-utils` (or `libraspberrypi-bin`) | `pinctrl` (power-loss and charge pins) and `vcgencmd` |
   | `python3-gi`, `gir1.2-gdkpixbuf-2.0`, `librsvg2-common` | The battery icon in the panel |
   | `pkexec` (or `policykit-1`) | The password prompt when the window changes settings |
   | `i2c-tools` | `i2cdetect`, to check the UPS |
   | `stress-ng` | The sweet spot overclock test |
   | `python3-libgpiod` | Fallback GPIO reading if `pinctrl` is missing (libgpiod v2 on Trixie) |

2. **Turns on I2C** with `raspi-config` (the X1202's fuel gauge is on I2C), and tells
   you if a reboot is needed.
3. **Installs the script** as `/usr/local/bin/pi5_energyplan.py`. That folder is on
   your PATH, so you can run `pi5_energyplan.py --status` from anywhere.
4. **Adds the menu entry** (Menu → Preferences → *Pi 5 Energy Plan*) and the **panel
   battery icon**, which starts at every login, for the user who ran `sudo`.
5. When run again after an update, it **refreshes the background services** so they
   run the new version.
6. At the end it prints `--status`, so you can check that it works.

Before installing, it's a good idea to update the system and the bootloader:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo rpi-eeprom-update -a
sudo reboot
```

### After installing: check the X1202 and set things up

With the X1202 mounted under the Pi:

```bash
i2cdetect -y 1             # "36" should appear in the table
pinctrl get 6              # "| hi" = adapter connected, "| lo" = on battery
pi5_energyplan.py --status # battery line, e.g. "76.5% · Charging · 3.95 V · Full in ≈ 2 h 10 min"
```

Then:

1. Open **Pi 5 Energy Plan** from the menu.
2. Click a plan, for example **Cool**. Keep *"Keep this plan after reboot"* ticked.
   Enter your password when asked. Reboot when it asks, so the full fan curve applies.
3. Click **UPS settings…** → **Recommended (20–80 %)** → **Save**. This turns on the
   battery guard service.

### Manual install (without install.sh)

```bash
sudo apt install -y python3-tk python3-smbus i2c-tools raspi-utils python3-gi \
                    gir1.2-gdkpixbuf-2.0 librsvg2-common pkexec stress-ng
sudo raspi-config nonint do_i2c 0 && sudo reboot
mkdir -p ~/pi5-energyplan && cp pi5_energyplan.py ~/pi5-energyplan/
cd ~/pi5-energyplan
python3 pi5_energyplan.py --install-launcher    # as your normal user, not sudo
python3 pi5_energyplan.py --install-tray
```

Applying a plan or saving UPS settings for the first time copies the script to
`/usr/local/bin/pi5_energyplan.py`. The two background services run that copy.

### Optional: let USB use the X1202's full 5 A

The Pi 5 can only detect a 5 A supply through USB-C, so on pogo-pin power it may limit
USB devices to 600 mA. If you use power-hungry USB devices (SSDs, for example), set
the supply limit in the bootloader:

```bash
sudo rpi-eeprom-config --edit
# add the line:  PSU_MAX_CURRENT=5000
```

Then reboot.

---

## 5. Using it

### The window

* **Plan buttons**: apply a plan. The plan in use is highlighted.
* **Governor / Fan pickers** (▾): change the governor or fan profile a plan uses.
* **UPS settings…**: charge limit, safe shutdown level, outage timer, warning time,
  battery saver.
* **Battery in panel**: adds the panel icon.
* **Find sweet spot…**: starts or cancels the overclock test.
* When you run a newer copy of the script, the window offers to **update the
  background services**.

### Command line

After `install.sh`, you can write `pi5_energyplan.py` instead of
`python3 pi5_energyplan.py`, from any folder.

| Command | What it does |
|---|---|
| `python3 pi5_energyplan.py` | Open the window |
| `python3 pi5_energyplan.py --list` | List plans with their governor and fan profile |
| `python3 pi5_energyplan.py --status` | Temperature, fan, CPU, power, plan, battery |
| `sudo python3 pi5_energyplan.py --apply Cool` | Apply a plan and keep it after reboot. Short names work: `oc28`, `maxcool`, `bal`. Quote names with spaces: `--apply "OC 2.8 GHz"`. |
| `sudo python3 pi5_energyplan.py --apply Eco --no-config` | Apply until the next reboot only (no `config.txt` change) |
| `sudo python3 pi5_energyplan.py --set-governor Eco performance` | Change the governor a plan uses |
| `sudo python3 pi5_energyplan.py --set-fan Eco Strong` | Change a plan's fan profile (Silent, Quiet, Default, Cool, Strong, Max) |
| `python3 pi5_energyplan.py --battery` | Live battery readout every 3 s (Ctrl+C to stop) |
| `python3 pi5_energyplan.py --charge-debug` | Everything about charging: pins, guard state, last log lines |
| `sudo python3 pi5_energyplan.py --ups-config '{"enabled":true,"max":80,"min":20}'` | Set the UPS settings without the window (see below) |
| `python3 pi5_energyplan.py --install-launcher` | Add the menu entry |
| `python3 pi5_energyplan.py --install-tray` | Add the panel battery icon |
| `sudo python3 pi5_energyplan.py --autotest [--minutes 5] [--max-temp 80]` | Start the sweet spot test |
| `python3 pi5_energyplan.py --autotest-status` | Test progress and log |
| `sudo python3 pi5_energyplan.py --autotest-cancel` | Stop the test |
| `python3 pi5_energyplan.py --update` | After replacing the script: refresh everything it installed |
| `sudo python3 pi5_energyplan.py --uninstall-service` | Stop restoring the plan at boot |
| `sudo python3 pi5_energyplan.py --uninstall-guard` | Remove the battery guard service |

`--ups-config` keys:

| Key | Values | Default |
|---|---|---|
| `enabled` | `true` / `false`: charge limit and auto shutdown on/off | `false` |
| `max` | 50–100: stop charging at this % | 80 |
| `min` | 5–50: safe shutdown at this % (at least 20 below `max`) | 20 |
| `outage_minutes` | 0 (never), 1, 5, 15, 30, 60 | 0 |
| `warn_minutes` | 1, 2, 5 | 1 |
| `saver_enabled` | `true` / `false` | `false` |
| `saver_below` | 15–100 (100 = as soon as power goes out) | 50 |
| `saver_plan` | `"Underclock"` or `"Eco"` | `"Underclock"` |

---

## 6. Files it creates

| Path | What |
|---|---|
| `/boot/firmware/config.txt` | The managed `pi5-energyplan` block at the end |
| `/boot/firmware/config.txt.energyplan.bak` | Your original `config.txt` (made once) |
| `/boot/firmware/tryboot.txt` | Only during the sweet spot test |
| `/usr/local/bin/pi5_energyplan.py` | The copy the services run |
| `/etc/systemd/system/pi5-energyplan.service` | Restores the plan at boot |
| `/etc/systemd/system/pi5-battery-guard.service` | Battery guard |
| `/etc/pi5-energyplan/battery.json` | UPS settings |
| `/etc/pi5-energyplan/governors.json`, `fans.json` | Your per-plan governor and fan choices |
| `/etc/pi5-energyplan/custom.json` | The "Sweet Spot" plan |
| `/etc/pi5-energyplan/charge_pin_v2.json` | What the charge-pin self-check learned |
| `/var/lib/pi5-energyplan/autotest.json`, `autotest.log` | Sweet spot test state and log |
| `/run/pi5-energyplan/battery-guard.json` | Live guard status, read by the window and the icon |
| `~/.local/share/applications/pi5-energyplan.desktop` | Menu entry |
| `~/.config/autostart/pi5-battery-tray.desktop` | Starts the panel icon at login |

---

## 7. Updating and uninstalling

**Update:** get the new version (`git pull`, or copy the new `pi5_energyplan.py` into
the folder), then run the installer again:

```bash
cd x1202 && git pull
sudo sh install.sh
```

This installs any new packages, replaces the system copy, refreshes both services,
the menu entry and the panel icon, and never adds anything twice. If you installed
manually, run `python3 pi5_energyplan.py --update` instead. The window also offers to
update when it notices a newer version.

**Uninstall:**

```bash
sudo pi5_energyplan.py --apply Balanced   # optional: put the Pi defaults back in config.txt
sudo sh install.sh --uninstall
```

This stops and removes both services (charging is switched back on), the system copy,
the menu entry and the panel icon. It keeps your settings in `/etc/pi5-energyplan`
and the installed apt packages.

To remove the managed block completely, delete the lines from
`# >>> pi5-energyplan` to `# <<< pi5-energyplan <<<` at the end of
`/boot/firmware/config.txt`, or restore `config.txt.energyplan.bak`. You can also
delete `/etc/pi5-energyplan` and `/var/lib/pi5-energyplan`.

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| **The Pi won't boot after choosing an overclock plan** | Put the SD card or SSD in another computer. In the boot partition, open `config.txt`, delete the `pi5-energyplan` block (or copy `config.txt.energyplan.bak` over it), and boot again. |
| Window says *"I2C is off"* | `sudo raspi-config nonint do_i2c 0`, then reboot |
| *"Missing I2C library"* | `sudo apt install python3-smbus` |
| *"UPS not answering on I2C 0x36"* | The X1202 isn't seated or its pogo pins don't touch the Pi. Check that `i2cdetect -y 1` shows `36`. |
| Battery shows "Plugged in" but never "Charging" | Run `python3 pi5_energyplan.py --charge-debug`. If it says the adapter is too weak, use a stronger supply: the Pi and the charger share it. |
| Charge limit "✗": board can't pause charging | Your board revision ignores GPIO 16. The guard leaves charging to the board, which stops by itself at full (4.2 V). |
| A shutdown is counting down and you want to stop it | Plug the adapter back in, or run `sudo shutdown -c`. |
| *"No desktop display found"* | You are on SSH: use `--status`, `--apply`, `--battery`. |
| Fan temperatures say "applies after reboot" | Your kernel doesn't allow live changes to trip points. The new curve applies after a reboot. |
| Plan's governor shows something else after boot | The boot service applies it again 60 s after boot. Check with `systemctl status pi5-energyplan`. |
| Sweet spot test says *"settings were not used on boot"* | Update the bootloader: `sudo rpi-eeprom-update -a`, reboot, then start the test again. |
| Guard log | `journalctl -u pi5-battery-guard -f` |

---

## Testing

There's no Raspberry Pi in CI, so `tests/` contains a **simulator**. It fakes a Pi 5
with Active Cooler and X1202 and runs the real script against it:

* fake sysfs (cpufreq, thermal trip points, fan) and a stock Raspberry Pi OS `config.txt`,
  in a private mount namespace,
* a fake MAX17048 fuel gauge (`tests/fakes/py/smbus2.py`),
* fake `pinctrl`, `vcgencmd`, `systemctl`, `shutdown` and `reboot`. These only record
  how they were called, so nothing on the test machine is changed or rebooted.

Run it on a Linux development machine, **not on the Pi**:

```bash
sudo PY=python3.13 sh tests/run_sim_tests.sh        # Python 3.13, as on Trixie
sudo PY=python3.11 sh tests/run_sim_tests.sh        # Python 3.11, as on Bookworm
sudo xvfb-run -a sh tests/run_sim_tests.sh          # also the GUI and the panel icon
```

The installer test for the menu entry and panel icon needs a throwaway user:
`sudo INSTALL_TEST_USER=someuser sh tests/run_sim_tests.sh`.

The 39 tests cover:

* **Plans:** applying each kind of plan, the live sysfs changes and governor fallback,
  `config.txt` editing (backup, commenting out conflicting lines, only `[all]`/`[pi5]`
  touched, idempotent), overclock blocks, `--no-config`, the boot service,
  `--set-governor`, `--set-fan` and `--update`.
* **Battery:** reading and clamping, time-left and time-to-full estimates, UPS-settings
  validation.
* **Battery guard:** a full scenario (charge pause at 80 % and resume at 74 %, saver on
  at 45 % on battery, shutdown at 19 % after 3 readings, cancel and plan restored when
  power returns), the outage timer, the low-voltage floor, no shutdown on mains, and
  the charge-pin self-check.
* **Sweet spot test:** a full run (pass, unstable, no boot → Sweet Spot saved, tryboot
  removed, `config.txt` untouched until the end), a crash during stress,
  old-bootloader detection, and cancel.
* **Installer:** Trixie and Bookworm package names, I2C switch-on, menu entry and
  panel icon for the sudo user, re-run as an update, `--uninstall`.
* **GUI and panel icon:** the window builds and shows live values; the panel icon
  registers on D-Bus and serves a 64×64 icon and its tooltip.
* **Uninstall:** `--uninstall-service` and `--uninstall-guard`.

These tests check the script's logic and its file and command output. They can't
check the real hardware: actual fan speeds, real overclock stability, or whether your
X1202 revision obeys GPIO 16. Do the first run on the Pi with `--status` and
`--charge-debug`, and watch the window for a few minutes.
