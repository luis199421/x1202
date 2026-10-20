#!/usr/bin/env python3
"""
install_gui.py - install Pi 5 Energy Plan without a terminal.

Double-click "Install Pi 5 Energy Plan" (or this file, then "Execute") in the
file manager. It asks for your password once, runs install.sh with pkexec,
shows the progress, then adds the menu entry and panel battery icon for you.
"""
import os
import queue
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALL_SH = os.path.join(HERE, "install.sh")
INSTALLED = "/usr/local/bin/pi5_energyplan.py"
TITLE = "Install Pi 5 Energy Plan"
BG, CARD, FG, MUTED, GREEN, RED = "#1e1f24", "#2a2c33", "#e8e8e8", "#9aa0a6", "#66bb6a", "#ef5350"


def root_cmd(args):
    """Run as root: pkexec shows the desktop's password window."""
    if os.geteuid() == 0:
        return args
    if shutil.which("pkexec"):
        return ["pkexec"] + args
    return ["sudo", "-n"] + args


def ensure_tk():
    """The window needs tkinter. Raspberry Pi OS desktop has it; if not, install it
    first (the password window appears, no terminal needed)."""
    try:
        import tkinter  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("PI5EP_TK_TRIED"):
        sys.exit("tkinter could not be installed: sudo apt install python3-tk")
    subprocess.run(root_cmd(["apt-get", "-o", "DPkg::Lock::Timeout=300", "install", "-y",
                             "python3-tk"]), stdin=subprocess.DEVNULL)
    os.environ["PI5EP_TK_TRIED"] = "1"
    os.execv(sys.executable, [sys.executable] + sys.argv)


try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:  # installed by ensure_tk() when the window starts
    tk = messagebox = None


class Installer:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.configure(bg=BG, padx=16, pady=12)
        self.root.minsize(560, 0)
        self.q = queue.Queue()
        self.reboot_needed = False
        self.installed = os.path.exists(INSTALLED)
        self.opt_ups = tk.BooleanVar(value=not os.path.exists(
            "/etc/pi5-energyplan/battery.json"))
        self.opt_eeprom = tk.BooleanVar(value=True)
        self.opt_desktop = tk.BooleanVar(value=True)
        self._build()

    def label(self, parent, text, size=10, color=FG, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color, justify="left",
                        font=("DejaVu Sans", size, "bold" if bold else "normal"), **kw)

    def check(self, parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var, bg=parent["bg"], fg=FG,
                              selectcolor=CARD, activebackground=parent["bg"],
                              activeforeground=FG, anchor="w", justify="left",
                              wraplength=500, font=("DejaVu Sans", 10))

    def button(self, parent, text, cmd, bg=CARD, bold=False):
        return tk.Button(parent, text=text, command=cmd, relief="flat", bg=bg, fg=FG,
                         activebackground="#555", activeforeground=FG, padx=10, pady=4,
                         font=("DejaVu Sans", 10, "bold" if bold else "normal"))

    def _build(self):
        r = self.root
        self.label(r, "Pi 5 Energy Plan", 15, bold=True).pack(anchor="w")
        self.label(r, "Energy plans for the Raspberry Pi 5 and battery management for the "
                      "Geekworm X1202 UPS (X1200 / X1201 too).", 9, MUTED,
                   wraplength=530).pack(anchor="w", pady=(2, 8))
        if self.installed:
            self.label(r, "Already installed: Install again to update it.", 9,
                       "#90caf9").pack(anchor="w", pady=(0, 6))

        box = tk.Frame(r, bg=CARD, padx=10, pady=8)
        box.pack(fill="x")
        self.label(box, "This installs the packages it needs, turns on I2C and puts the "
                        "program in /usr/local/bin. You'll be asked for your password once.",
                   9, MUTED, wraplength=510).pack(anchor="w", pady=(0, 4))
        self.check(box, "Battery protection with the recommended settings: charge to 80 %, "
                        "safe shutdown at 20 %, battery saver below 50 % "
                        "(change them later in UPS settings)", self.opt_ups).pack(fill="x")
        self.check(box, "Geekworm's bootloader settings: PSU_MAX_CURRENT=5000 (full 5 A "
                        "for USB) and POWER_OFF_ON_HALT=1 (5 V outputs off after shutdown, "
                        "saves battery; changeable in UPS settings). Reboot needed.",
                   self.opt_eeprom).pack(fill="x")
        self.check(box, "Menu entry and battery icon in the panel",
                   self.opt_desktop).pack(fill="x")

        self.step = self.label(r, "Ready.", 10, bold=True)
        self.step.pack(anchor="w", pady=(10, 2))
        self.bar = tk.Canvas(r, height=6, bg=CARD, highlightthickness=0)
        self.bar.pack(fill="x")
        self.bar_pos = 0

        logf = tk.Frame(r, bg=BG)
        logf.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(logf, height=12, bg="#141519", fg="#c8c8c8", relief="flat",
                           font=("DejaVu Sans Mono", 8), wrap="word", state="disabled")
        sb = tk.Scrollbar(logf, command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.btns = tk.Frame(r, bg=BG)
        self.btns.pack(fill="x", pady=(10, 0))
        self.show_buttons("start")

    def show_buttons(self, stage):
        for w in self.btns.winfo_children():
            w.destroy()
        b = []
        if stage == "start":
            b.append(("Install" if not self.installed else "Install again (update)",
                      self.start_install, "#43a047", True))
            if self.installed:
                b.append(("Uninstall", self.start_uninstall, CARD, False))
        elif stage == "done":
            if self.reboot_needed:
                b.append(("Reboot now", self.reboot, "#43a047", True))
            b.append(("Open Energy Plan", self.open_app,
                      CARD if self.reboot_needed else "#43a047", not self.reboot_needed))
        elif stage == "failed":
            b.append(("Try again", self.start_install, CARD, False))
        if stage != "busy":
            b.append(("Close", self.root.destroy, CARD, False))
        for text, cmd, bg, bold in reversed(b):
            self.button(self.btns, text, cmd, bg, bold).pack(side="right", padx=(6, 0))

    def write(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    # ----- running things in the background -----
    def run_bg(self, steps):
        """steps: list of (title, argv). Output goes to the log; stops at an error."""
        def work():
            for title, argv in steps:
                self.q.put(("step", title))
                try:
                    p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         text=True, bufsize=1)
                except OSError as e:
                    self.q.put(("end", (False, str(e))))
                    return
                for line in p.stdout:
                    self.q.put(("line", line))
                rc = p.wait()
                if rc != 0:
                    why = ("The password window was closed." if rc in (126, 127) and
                           argv[0] == "pkexec" else f"'{title}' failed (code {rc}).")
                    self.q.put(("end", (False, why)))
                    return
            self.q.put(("end", (True, "")))
        threading.Thread(target=work, daemon=True).start()
        self.show_buttons("busy")
        self.poll()

    def poll(self):
        done = None
        while True:
            try:
                kind, val = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "step":
                self.step.config(text=val + "…", fg=FG)
                self.write(f"\n### {val}\n")
            elif kind == "line":
                if val.startswith("==> "):
                    self.step.config(text=val[4:].strip() + "…")
                if val.strip() == "REBOOT_NEEDED=1":
                    self.reboot_needed = True
                    continue
                self.write(val)
            else:
                done = val
        self.animate()
        if done is None:
            self.root.after(100, self.poll)
        else:
            self.finished(*done)

    def animate(self):
        w = max(self.bar.winfo_width(), 100)
        self.bar.delete("all")
        self.bar_pos = (self.bar_pos + 8) % (w + 120)
        self.bar.create_rectangle(self.bar_pos - 120, 0, self.bar_pos, 6, fill="#42a5f5",
                                  width=0)

    def bar_full(self, color):
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, self.bar.winfo_width(), 6, fill=color, width=0)

    # ----- actions -----
    def start_install(self):
        args = ["/bin/sh", INSTALL_SH, "--skip-desktop"]
        if self.opt_ups.get():
            args.append("--ups-recommended")
        if not self.opt_eeprom.get():
            args.append("--no-eeprom")
        steps = [("Installing (enter your password)", root_cmd(args))]
        if self.opt_desktop.get():
            steps += [("Adding the menu entry", [sys.executable, INSTALLED,
                                                 "--install-launcher"]),
                      ("Adding the battery icon to the panel", [sys.executable, INSTALLED,
                                                                "--install-tray"])]
        self.action = "install"
        self.reboot_needed = False
        self.run_bg(steps)

    def start_uninstall(self):
        if not messagebox.askyesno(TITLE, "Remove Pi 5 Energy Plan, its background services, "
                                          "menu entry and panel icon?\n\nCharging goes back "
                                          "to normal. Your settings are kept."):
            return
        self.action = "uninstall"
        self.run_bg([("Uninstalling (enter your password)",
                      root_cmd(["/bin/sh", INSTALL_SH, "--uninstall"]))])

    def finished(self, ok, why):
        if not ok:
            self.bar_full(RED)
            self.step.config(text="✘ " + why + " Nothing else was changed after that step.",
                             fg=RED)
            self.show_buttons("failed")
            return
        self.bar_full(GREEN)
        if self.action == "uninstall":
            self.installed = False
            self.step.config(text="✔ Uninstalled.", fg=GREEN)
            self.show_buttons("closed")
            return
        self.installed = True
        msg = "✔ Installed."
        if self.reboot_needed:
            msg += " Reboot once so I2C / the bootloader settings take effect."
        self.step.config(text=msg, fg=GREEN)
        self.show_buttons("done")

    def open_app(self):
        subprocess.Popen([sys.executable, INSTALLED], start_new_session=True)
        self.root.destroy()

    def reboot(self):
        if messagebox.askyesno(TITLE, "Reboot the Raspberry Pi now?"):
            subprocess.Popen(["systemctl", "reboot"])

    def run(self):
        self.root.mainloop()


def main():
    if not os.path.exists(INSTALL_SH):
        sys.exit(f"install.sh not found next to {__file__}")
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        sys.exit("No desktop found. In a terminal use: sudo sh install.sh")
    ensure_tk()
    Installer().run()


if __name__ == "__main__":
    main()
