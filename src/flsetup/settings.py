"""FL Studio Settings: compatibility tweaks in a Windows-style property sheet."""
import queue
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import config as C
from . import tweaks

GREY = "#f0f0f0"


def fl_running(prefix: Path):
    key = b"\0WINEPREFIX=" + str(prefix).encode() + b"\0"
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if key not in b"\0" + (d / "environ").read_bytes() + b"\0":
                continue
            argv0 = (d / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
        except OSError:
            continue
        if argv0.replace("\\", "/").rsplit("/", 1)[-1].lower() in ("fl64.exe", "fl.exe"):
            return True
    return False


class SettingsWindow:
    def __init__(self, win, owner=None):
        from .ui import ASSETS, Wizard, open_url, clean_env  # noqa: F401
        self.open_url = open_url
        self.win = win
        if owner is None:
            import types
            ns = types.SimpleNamespace(root=win)
            Wizard._setup_style(ns)
            self.f_bold, self.f_small = ns.f_bold, ns.f_small
        else:
            self.f_bold, self.f_small = owner.f_bold, owner.f_small
            win.transient(owner.root)
        win.title("FL Studio Settings")
        win.configure(bg=GREY)
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"640x620+{(sw - 640) // 2}+{(sh - 620) // 3}")
        win.minsize(600, 520)
        try:
            self.icon = tk.PhotoImage(file=str(ASSETS / "icon.png"))
            win.iconphoto(False, self.icon)
        except tk.TclError:
            pass
        self.w = tweaks.Wine(log=lambda m: self.q.put(("log", m)))
        self.t = tweaks.get_tweaks()
        self.q = queue.Queue()
        self.busy = False

        foot = tk.Frame(win, bg=GREY)
        foot.pack(side="bottom", fill="x", padx=10, pady=10)
        self.b_close = ttk.Button(foot, text="Close", command=self.close)
        self.b_close.pack(side="right")
        self.b_apply = ttk.Button(foot, text="Apply", command=self.apply)
        self.b_apply.pack(side="right", padx=(0, 8))
        self.pb = ttk.Progressbar(foot, mode="indeterminate", length=120)
        self.status = ttk.Label(foot, text="", style="Small.TLabel")
        self.status.pack(side="left")

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self.nb = nb
        self._tab_graphics()
        self._tab_audio()
        self._tab_advanced()
        win.protocol("WM_DELETE_WINDOW", self.close)
        if not self.w.ok:
            messagebox.showerror("FL Studio Settings", "FL Studio is not installed yet.", parent=win)
            win.after(10, win.destroy)

    # ------------------------------------------------------------- helpers
    def _desc(self, parent, text, pad=(24, 0)):
        ttk.Label(parent, text=text, style="Small.TLabel", wraplength=540, justify="left").pack(
            anchor="w", padx=pad, pady=(0, 10))

    def _group(self, parent, title):
        g = ttk.LabelFrame(parent, text=f" {title} ", padding=(12, 8))
        g.pack(fill="x", padx=12, pady=(10, 0))
        return g

    def run_task(self, label, fn, *args, close_fl=True, after=None):
        if self.busy:
            return
        if close_fl and fl_running(self.w.prefix):
            if not messagebox.askyesno("FL Studio Settings", "FL Studio is running. It needs to be closed "
                                       "to apply this change.\n\nClose FL Studio now? Save your work first.",
                                       icon="warning", parent=self.win):
                return
            self.w.close_all()
        self.busy = True
        self.status.configure(text=label + "…")
        self.pb.pack(side="left", padx=(10, 0))
        self.pb.start(12)
        for b in (self.b_apply, self.b_close):
            b.state(["disabled"])

        def work():
            try:
                res = fn(*args)
                self.q.put(("done", (True, res)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("done", (False, str(e))))
        threading.Thread(target=work, daemon=True).start()
        self._after = after
        self.win.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    if not val.startswith("$"):
                        self.status.configure(text=val[:90])
                    with open(C.LOG_FILE, "a") as f:
                        f.write("[settings] " + val + "\n")
                else:
                    ok, res = val
                    self.busy = False
                    self.pb.stop()
                    self.pb.pack_forget()
                    for b in (self.b_apply, self.b_close):
                        b.state(["!disabled"])
                    if ok:
                        self.status.configure(text="Done.")
                        if isinstance(res, str) and res:
                            messagebox.showinfo("FL Studio Settings", res, parent=self.win)
                    else:
                        self.status.configure(text="Failed.")
                        messagebox.showerror("FL Studio Settings", res, parent=self.win)
                    if self._after:
                        self._after()
                    return
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    # ------------------------------------------------------------- graphics
    def _tab_graphics(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  Graphics & Plugins  ")
        g = self._group(f, "Plugin windows")
        self.v_dxvk = tk.BooleanVar(value=self.t["dxvk"])
        ttk.Checkbutton(g, text="Use DXVK (Direct3D on Vulkan)", variable=self.v_dxvk).pack(anchor="w")
        self._desc(g, "Fixes blank, black or glitchy windows in many plugins, especially ones built "
                      "with JUCE 7. It can cause glitches in a few others; if a plugin gets worse, "
                      "turn it off again. Needs a Vulkan-capable graphics driver.")
        self.v_d2d = tk.BooleanVar(value=self.t["d2d1_off"])
        ttk.Checkbutton(g, text="Turn off Direct2D (recommended for FL Studio)", variable=self.v_d2d).pack(anchor="w")
        self._desc(g, "FL Studio's own interface draws more reliably with Direct2D off. If a specific "
                      "plugin won't open at all, try turning Direct2D on. Plugins built with JUCE 8 need a "
                      "newer Direct2D than Wine has and may not display either way.")

        r = self._group(f, "Windows components")
        row = ttk.Frame(r)
        row.pack(fill="x")
        ttk.Label(row, text="Microsoft Visual C++ runtimes (2015-2022)").pack(side="left")
        self.l_vc = ttk.Label(row, text="", style="Small.TLabel")
        self.l_vc.pack(side="left", padx=8)
        self.b_vc = ttk.Button(row, text="Install", command=self._vcrun)
        self.b_vc.pack(side="right")
        self._desc(r, "Many plugins quietly need these and fail to load without them. Downloads the "
                      "official installers from Microsoft (about 40 MB).", pad=(0, 0))
        row2 = ttk.Frame(r)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="Windows core fonts").pack(side="left")
        self.l_fonts = ttk.Label(row2, text="", style="Small.TLabel")
        self.l_fonts.pack(side="left", padx=8)
        self.b_fonts = ttk.Button(row2, text="Install", command=self._fonts)
        self.b_fonts.pack(side="right")
        self._desc(r, "Arial, Verdana, Times New Roman and more, for plugins that expect them "
                      "(about 10 MB).", pad=(0, 0))
        row3 = ttk.Frame(r)
        row3.pack(fill="x", pady=(6, 0))
        ttk.Label(row3, text="Microsoft GDI+").pack(side="left")
        self.l_gdi = ttk.Label(row3, text="", style="Small.TLabel")
        self.l_gdi.pack(side="left", padx=8)
        self.b_gdi = ttk.Button(row3, text="Install", command=self._gdiplus)
        self.b_gdi.pack(side="right")
        self._desc(r, "Only if a plugin draws incorrectly: Wine's own GDI+ is usually enough. "
                      "Large download (about 1 GB) from Microsoft.", pad=(0, 0))
        self._refresh_vc()

    def _refresh_vc(self):
        done = self.t.get("vcrun")
        self.l_vc.configure(text="Installed" if done else "Not installed")
        self.b_vc.configure(text="Reinstall" if done else "Install")
        f = tweaks.fonts_installed(self.w)
        self.l_fonts.configure(text="Installed" if f else "Not installed")
        self.b_fonts.configure(text="Reinstall" if f else "Install")
        g = tweaks.gdiplus_installed(self.w)
        self.l_gdi.configure(text="Installed" if g else "Using Wine's built-in")
        self.b_gdi.configure(text="Reinstall" if g else "Install")

    def _gdiplus(self):
        if not messagebox.askyesno("Microsoft GDI+", "This downloads about 1 GB from Microsoft and can "
                                   "take a while. Continue?", parent=self.win):
            return

        def fn():
            tweaks.install_gdiplus(self.w)
            return "Microsoft GDI+ is installed. Restart FL Studio to use it."
        self.run_task("Installing Microsoft GDI+", fn, after=self._refresh_vc)

    def _fonts(self):
        def fn():
            tweaks.install_fonts(self.w)
            return "Windows core fonts are installed. Restart FL Studio to use them."
        self.run_task("Installing Windows core fonts", fn, after=self._refresh_vc)

    def _vcrun(self):
        def fn():
            tweaks.install_vcrun(self.w)
            self.t["vcrun"] = True
            tweaks.save_tweaks(self.t)
            return "Visual C++ runtimes installed. Rescan plugins in FL Studio if some were missing."
        self.run_task("Installing Visual C++ runtimes", fn, after=self._refresh_vc)

    # --------------------------------------------------------------- audio
    def _tab_audio(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  Audio & MIDI  ")
        s = self._group(f, "This computer")
        self.l_midi = ttk.Label(s, text="")
        self.l_midi.pack(anchor="w")
        self.l_ntsync = ttk.Label(s, text="")
        self.l_ntsync.pack(anchor="w", pady=(4, 0))
        self.b_fix = ttk.Button(s, text="Fix (asks for your password)", command=self._sysfix)
        self.b_fix.pack(anchor="w", pady=(8, 0))
        self._desc(s, "Loads the Linux modules for MIDI (keyboards/controllers, fixes the 'MIDI Mapper' "
                      "error) and ntsync (faster Wine), and keeps them loaded after restarts.", pad=(0, 0))
        self._refresh_sys()

        a = self._group(f, "Low-latency audio (ASIO)")
        ttk.Label(a, text="FL Studio ASIO works out of the box. For lower latency, install PipeASIO "
                          "(recommended on PipeWire) or WineASIO with your package manager, then enable "
                          "it here.", style="Small.TLabel", wraplength=540, justify="left").pack(anchor="w")
        self.asio_rows = ttk.Frame(a)
        self.asio_rows.pack(fill="x", pady=(6, 0))
        self._refresh_asio()

        p = self._group(f, "Performance")
        self.v_gm = tk.BooleanVar(value=self.t.get("gamemode", False))
        cb = ttk.Checkbutton(p, text="Start FL Studio with GameMode", variable=self.v_gm)
        cb.pack(anchor="w")
        if not shutil.which("gamemoderun"):
            cb.state(["disabled"])
            self._desc(p, "GameMode is not installed (package: gamemode).")
        else:
            self._desc(p, "Asks the system to prioritise FL Studio (CPU governor, process priority).")

    def _refresh_sys(self):
        st = tweaks.system_status()
        self.l_midi.configure(text=("✔  MIDI support is available" if st["midi"] else
                                    "✖  MIDI support is not loaded: MIDI keyboards won't work"))
        if st["ntsync"]:
            nt = "✔  ntsync is active (faster Wine)"
        elif st["ntsync_possible"]:
            nt = "✖  ntsync is available but not enabled"
        else:
            nt = "–  ntsync is not supported by this kernel (needs Linux 6.14+)"
        self.l_ntsync.configure(text=nt)
        need = tweaks.system_fix_script() is not None
        self.b_fix.state(["!disabled"] if need else ["disabled"])

    def _sysfix(self):
        def fn():
            ok, msg = tweaks.run_system_fix()
            if not ok:
                raise RuntimeError(msg)
            return msg
        self.run_task("Waiting for your password", fn, close_fl=False, after=self._refresh_sys)

    def _refresh_asio(self):
        for c in self.asio_rows.winfo_children():
            c.destroy()
        reg = " ".join(tweaks.registered_asio(self.w)).lower()
        hints = {"PipeASIO": "paru -S pipeasio", "WineASIO": "your distro's 'wineasio' package"}
        for name in tweaks.ASIO_DRIVERS:
            row = ttk.Frame(self.asio_rows)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=name, width=12).pack(side="left")
            if name.lower() in reg:
                ttk.Label(row, text="Enabled: pick it in FL Studio under Options > Audio settings").pack(side="left")
            elif tweaks.find_asio_driver(name):
                ttk.Label(row, text="Installed on this computer").pack(side="left")
                ttk.Button(row, text="Enable", command=lambda n=name: self._asio(n)).pack(side="right")
            else:
                ttk.Label(row, text=f"Not installed ({hints[name]})", style="Small.TLabel").pack(side="left")

    def _asio(self, name):
        def fn():
            tweaks.install_asio(self.w, name)
            self.t["asio"] = name
            tweaks.save_tweaks(self.t)
            return (f"{name} is enabled. In FL Studio open Options > Audio settings and choose {name} "
                    "as the device. If you hear crackles, turn off 'Mix in buffer switch'.")
        self.run_task(f"Enabling {name}", fn, after=self._refresh_asio)

    # ------------------------------------------------------------ advanced
    def _tab_advanced(self):
        f = ttk.Frame(self.nb)
        self.nb.add(f, text="  Advanced  ")
        g = self._group(f, "Windows tools")
        grid = ttk.Frame(g)
        grid.pack(fill="x")
        items = [("Wine configuration", "--winecfg"), ("Registry editor", "--regedit"),
                 ("Task manager", "--taskmgr")]
        for i, (label, arg) in enumerate(items):
            ttk.Button(grid, text=label, command=lambda a=arg: self._launch(a)).grid(
                row=0, column=i, padx=(0, 8), sticky="w")
        self._desc(g, "Task manager can end a stuck plugin bridge (ilbridge.exe) if FL Studio hangs "
                      "while opening an old 32-bit plugin.", pad=(0, 0))
        t = self._group(f, "Troubleshooting")
        row = ttk.Frame(t)
        row.pack(fill="x")
        ttk.Button(row, text="Force-close FL Studio", command=self._kill).pack(side="left")
        ttk.Button(row, text="Open setup log", command=lambda: self.open_url(C.LOG_FILE.as_uri())).pack(
            side="left", padx=8)
        ttk.Button(row, text="Edit launcher settings", command=self._env).pack(side="left")
        self._desc(t, "Launcher settings is a small text file where you can add environment variables "
                      "for FL Studio (one 'export NAME=value' per line).", pad=(0, 0))
        i = self._group(f, "Installation")
        ttk.Label(i, text=f"Windows environment: {self.w.prefix}", style="Small.TLabel",
                  wraplength=560).pack(anchor="w")
        ttk.Label(i, text=f"Wine: {self.w.wine.parent.parent.name}", style="Small.TLabel").pack(anchor="w")

    def _launch(self, arg):
        from .ui import launch_fl
        launch_fl([arg])

    def _kill(self):
        if messagebox.askyesno("FL Studio Settings", "Force-close FL Studio and everything running in its "
                               "Windows environment? Unsaved work will be lost.", icon="warning",
                               parent=self.win):
            self.w.close_all()
            self.status.configure(text="FL Studio was closed.")

    def _env(self):
        tweaks.write_launcher_env(self.t)
        self.open_url(tweaks.ENV_FILE.as_uri())

    # --------------------------------------------------------------- apply
    def apply(self):
        new = dict(self.t, dxvk=self.v_dxvk.get(), d2d1_off=self.v_d2d.get(), gamemode=self.v_gm.get())
        jobs = []
        if new["dxvk"] != self.t["dxvk"]:
            jobs.append(tweaks.install_dxvk if new["dxvk"] else tweaks.remove_dxvk)
        if new["d2d1_off"] != self.t["d2d1_off"]:
            jobs.append(lambda w: tweaks.set_d2d1(w, new["d2d1_off"]))
        gm_changed = new["gamemode"] != self.t.get("gamemode")
        if not jobs and not gm_changed:
            self.status.configure(text="No changes.")
            return

        def fn():
            for j in jobs:
                j(self.w)
            self.t.update(new)
            tweaks.save_tweaks(self.t)
            tweaks.write_launcher_env(self.t)
            return "Settings applied. They take effect the next time you start FL Studio."
        self.run_task("Applying settings", fn, close_fl=bool(jobs))

    def close(self):
        if self.busy:
            return
        self.win.destroy()


def settings_mode():
    root = tk.Tk(className="FL Studio Setup")
    SettingsWindow(root)
    root.mainloop()
