"""Windows-style setup wizard (Tk)."""
import os
import queue
import shutil
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import config as C
from .engine import Engine, Options, find_system_wine, human, uninstall

ASSETS = Path(__file__).parent / "assets"
W, H = 660, 500
BANNER_W = 180
GREY = "#f0f0f0"
ACCENT = "#ff8a00"
LINK = "#0066cc"


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ("TCL_LIBRARY", "TK_LIBRARY", "TKPATH", "SSL_CERT_FILE",
                         "PYTHONHOME", "PYTHONPATH")}


def open_url(url):
    for tool in ("xdg-open", "gio"):
        if shutil.which(tool):
            args = [tool, url] if tool == "xdg-open" else [tool, "open", url]
            subprocess.Popen(args, env=clean_env(), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
    webbrowser.open(url)


def native_dialog(kind, title, initial=None, filetypes=None, parent=None):
    """Use the desktop's own file chooser (zenity/kdialog) when present, else Tk's."""
    env = clean_env()
    initial = str(initial or C.HOME)
    try:
        if shutil.which("zenity"):
            cmd = ["zenity", "--file-selection", f"--title={title}", f"--filename={initial}/"]
            if kind == "dir":
                cmd.append("--directory")
            elif kind == "any_files":
                cmd += ["--multiple", "--separator=\n"]
            elif kind == "plugins":
                cmd += ["--multiple", "--separator=\n",
                        "--file-filter=Plugins, installers and archives | *.exe *.EXE *.msi *.MSI "
                        "*.vst3 *.VST3 *.dll *.DLL *.clap *.zip *.ZIP *.7z *.rar *.tar.gz *.tar.xz",
                        "--file-filter=All files | *"]
            else:
                cmd.append("--file-filter=Windows installers (*.exe *.msi) | *.exe *.EXE *.msi *.MSI")
                cmd.append("--file-filter=All files | *")
            r = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if kind in ("plugins", "any_files"):
                return [x for x in r.stdout.splitlines() if x.strip()] or None
            return r.stdout.strip() or None
        if shutil.which("kdialog"):
            if kind == "dir":
                cmd = ["kdialog", "--title", title, "--getexistingdirectory", initial]
            elif kind == "any_files":
                cmd = ["kdialog", "--title", title, "--multiple", "--separate-output", "--getopenfilename",
                       initial]
            elif kind == "plugins":
                cmd = ["kdialog", "--title", title, "--multiple", "--separate-output", "--getopenfilename",
                       initial, "*.exe *.msi *.vst3 *.dll *.clap *.zip *.7z *.rar|Plugins and installers"]
            else:
                cmd = ["kdialog", "--title", title, "--getopenfilename", initial,
                       "*.exe *.EXE *.msi *.MSI|Windows installers"]
            r = subprocess.run(cmd, env=env, capture_output=True, text=True)
            if kind in ("plugins", "any_files"):
                return [x for x in r.stdout.splitlines() if x.strip()] or None
            return r.stdout.strip() or None
    except OSError:
        pass
    if kind == "dir":
        return filedialog.askdirectory(title=title, initialdir=initial, parent=parent) or None
    if kind == "any_files":
        return list(filedialog.askopenfilenames(title=title, initialdir=initial, parent=parent)) or None
    if kind == "plugins":
        r = filedialog.askopenfilenames(title=title, initialdir=initial, parent=parent, filetypes=[
            ("Plugins, installers and archives", "*.exe *.msi *.vst3 *.dll *.clap *.zip *.7z *.rar *.gz *.xz"),
            ("All files", "*")])
        return list(r) or None
    return filedialog.askopenfilename(title=title, initialdir=initial, parent=parent,
                                      filetypes=filetypes or [("Windows installers", "*.exe *.msi"),
                                                              ("All files", "*")]) or None


def launch_fl(args=()):
    if C.LAUNCHER.exists():
        subprocess.Popen([str(C.LAUNCHER), *args], env=clean_env(), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    return False


class Wizard:
    def __init__(self, mode="auto"):
        self.root = tk.Tk(className="FL Studio Setup")
        self.root.title("FL Studio Setup")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self._setup_style()
        self.icon = self.icon_small = None
        try:
            self.icon = tk.PhotoImage(file=str(ASSETS / "icon.png"))
            f = max(1, self.icon.width() // 48)
            self.icon_small = self.icon.subsample(f, f)
            fb = max(1, self.icon.width() // 112)
            self.icon_banner = self.icon.subsample(fb, fb)
            self.root.iconphoto(True, self.icon)
        except tk.TclError:
            self.icon_banner = None
        self.root.geometry(f"{W}x{H}")
        self._center()

        self.state = C.load_state()
        self.installed = bool(self.state and Path(self.state.get("fl_exe", "")).exists())
        self.q = queue.Queue()
        self.engine = None
        self.worker = None
        self.busy = False
        self.result = None
        self.prefix_existed = True

        # option variables
        st = (self.state or {}).get("options", {})
        self.v_accept = tk.StringVar(value="no")
        self.v_prefix = tk.StringVar(value=(self.state or {}).get("prefix") or str(C.DEFAULT_PREFIX))
        self.v_desktop = tk.BooleanVar(value=st.get("desktop_shortcut", True))
        self.v_menu = tk.BooleanVar(value=st.get("menu", True))
        self.v_flp = tk.BooleanVar(value=st.get("flp_assoc", True))
        self.v_compat = tk.BooleanVar(value=st.get("compat", True))
        self.v_source = tk.StringVar(value="download")
        self.v_local = tk.StringVar()
        self.v_express = tk.StringVar(value="custom")
        self.v_wine = tk.StringVar(value="bundled")
        self.v_keep = tk.BooleanVar(value=False)
        self.v_dpi = tk.StringVar(value=self._dpi_label(st.get("dpi") or self._detect_dpi()))
        self.v_maint = tk.StringVar(value="remove" if mode == "uninstall" else "update")
        self.v_deldata = tk.BooleanVar(value=False)
        self.v_launch = tk.BooleanVar(value=True)

        self._build_chrome()
        if self.installed or mode in ("maintenance", "uninstall"):
            if not self.installed:
                messagebox.showinfo("FL Studio Setup", "FL Studio is not installed yet. "
                                    "Setup will now install it.", parent=self.root)
                self.pages = ["welcome", "license", "location", "tasks", "source", "ready", "progress", "finish"]
            else:
                self.pages = ["maint", "progress", "finish"]
        else:
            self.pages = ["welcome", "license", "location", "tasks", "source", "ready", "progress", "finish"]
        self.idx = 0
        self.show()

    # ---------------------------------------------------------------- styling
    def _setup_style(self):
        fams = set(tkfont.families(self.root))
        fam = next((f for f in ("Segoe UI", "Noto Sans", "Cantarell", "Ubuntu", "DejaVu Sans",
                                "Liberation Sans") if f in fams), "TkDefaultFont")
        size = 10
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=fam, size=size)
            except tk.TclError:
                pass
        self.f_base = tkfont.Font(family=fam, size=size)
        self.f_bold = tkfont.Font(family=fam, size=size, weight="bold")
        self.f_title = tkfont.Font(family=fam, size=15, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=size - 1)
        self.root.configure(bg=GREY)
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=GREY, font=self.f_base)
        s.configure("TButton", padding=(14, 4))
        for w in ("TCheckbutton", "TRadiobutton"):
            s.map(w, background=[("disabled", GREY), ("active", GREY)],
                  indicatorbackground=[("disabled", "#e6e6e6"), ("pressed", "#dcdcdc")])
        s.configure("White.TFrame", background="white")
        s.configure("White.TLabel", background="white")
        s.configure("White.TCheckbutton", background="white")
        s.map("White.TCheckbutton", background=[("active", "white")])
        s.configure("White.TRadiobutton", background="white")
        s.map("White.TRadiobutton", background=[("active", "white")])
        s.configure("Hdr.TLabel", background="white", font=self.f_bold)
        s.configure("Sub.TLabel", background="white")
        s.configure("Title.TLabel", background="white", font=self.f_title)
        s.configure("Dim.TLabel", foreground="#6d6d6d")
        s.configure("Small.TLabel", font=self.f_small, foreground="#555")
        s.configure("Link.TLabel", foreground=LINK)
        s.configure("Horizontal.TProgressbar", troughcolor="#e6e6e6", background="#06b025",
                    lightcolor="#4cd964", darkcolor="#06a020", bordercolor="#bcbcbc", thickness=18)

    def _center(self):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 3}")

    def _detect_dpi(self):
        try:
            dpi = int(round(self.root.winfo_fpixels("1i")))
        except tk.TclError:
            dpi = 96
        for d in (96, 120, 144, 192):
            if dpi <= d + 10:
                return d
        return 192

    @staticmethod
    def _dpi_label(dpi):
        return {96: "100% (normal)", 120: "125%", 144: "150%", 168: "175%", 192: "200% (HiDPI)"}.get(dpi, "100% (normal)")

    def _dpi_value(self):
        return {"100% (normal)": 96, "125%": 120, "150%": 144, "175%": 168, "200% (HiDPI)": 192}[self.v_dpi.get()]

    # ---------------------------------------------------------------- chrome
    def _build_chrome(self):
        r = self.root
        self.header = tk.Frame(r, bg="white", height=64)
        self.header.pack_propagate(False)
        inner = tk.Frame(self.header, bg="white")
        inner.pack(side="left", fill="both", expand=True, padx=(18, 0), pady=(10, 6))
        self.h_title = ttk.Label(inner, style="Hdr.TLabel")
        self.h_title.pack(anchor="w")
        self.h_sub = ttk.Label(inner, style="Sub.TLabel")
        self.h_sub.pack(anchor="w", padx=(14, 0), pady=(3, 0))
        if self.icon_small:
            tk.Label(self.header, image=self.icon_small, bg="white").pack(side="right", padx=12)
        self.h_sep = ttk.Separator(r)

        self.body = tk.Frame(r, bg=GREY)
        self.footer = tk.Frame(r, bg=GREY)
        self.footer.pack(side="bottom", fill="x")
        ttk.Separator(self.footer).pack(fill="x")
        row = tk.Frame(self.footer, bg=GREY)
        row.pack(fill="x", padx=10, pady=10)
        ttk.Label(row, text=f"{C.APP_NAME} {C.APP_VERSION}", style="Dim.TLabel").pack(side="left")
        self.b_cancel = ttk.Button(row, text="Cancel", command=self.on_cancel)
        self.b_cancel.pack(side="right")
        self.b_next = ttk.Button(row, text="Next >", command=self.on_next)
        self.b_next.pack(side="right", padx=(0, 10))
        self.b_back = ttk.Button(row, text="< Back", command=self.on_back)
        self.b_back.pack(side="right")
        self.root.bind("<Return>", lambda e: self.b_next.instate(["!disabled"]) and self.on_next())
        self.root.bind("<Escape>", lambda e: self.on_cancel())

    def set_header(self, title, sub):
        self.h_title.configure(text=title)
        self.h_sub.configure(text=sub)

    def show(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.body.pack_forget()
        self.header.pack_forget()
        self.h_sep.pack_forget()
        page = self.pages[self.idx]
        big = page in ("welcome", "finish")
        if not big:
            self.header.pack(side="top", fill="x")
            self.h_sep.pack(side="top", fill="x")
        self.body.configure(bg="white" if big else GREY)
        self.body.pack(side="top", fill="both", expand=True)
        self.b_back.configure(state="normal" if 0 < self.idx and page not in ("progress", "finish") else "disabled")
        self.b_next.configure(text="Next >", state="normal")
        self.b_cancel.configure(state="normal", text="Cancel")
        getattr(self, "page_" + page)()

    def content(self, pad=(30, 14)):
        f = ttk.Frame(self.body)
        f.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        return f

    def big_page(self, title):
        f = tk.Frame(self.body, bg="white")
        f.pack(fill="both", expand=True)
        cv = tk.Canvas(f, width=BANNER_W, highlightthickness=0, bd=0)
        cv.pack(side="left", fill="y")
        hh = H - 52
        top, bot = (0x2b, 0x2d, 0x42), (0xff, 0x8a, 0x00)
        steps = 64
        for i in range(steps):
            t = i / (steps - 1)
            t2 = t ** 1.6
            c = tuple(int(top[k] + (bot[k] - top[k]) * t2) for k in range(3))
            cv.create_rectangle(0, hh * i / steps, BANNER_W, hh * (i + 1) / steps + 1,
                                fill="#%02x%02x%02x" % c, outline="")
        for k in range(7):  # decorative equalizer bars
            x = 22 + k * 20
            hgt = [40, 90, 60, 120, 75, 100, 50][k]
            cv.create_rectangle(x, hh - 24 - hgt, x + 12, hh - 24, fill="#ffffff", outline="",
                                stipple="gray25")
        if self.icon_banner:
            cv.create_image(BANNER_W // 2, 96, image=self.icon_banner)
        right = tk.Frame(f, bg="white")
        right.pack(side="left", fill="both", expand=True, padx=(22, 22), pady=(22, 10))
        ttk.Label(right, text=title, style="Title.TLabel", wraplength=W - BANNER_W - 50,
                  justify="left").pack(anchor="w", pady=(0, 14))
        return right

    def para(self, parent, text, style="White.TLabel", pady=(0, 10), wrap=None):
        lbl = ttk.Label(parent, text=text, style=style, justify="left",
                        wraplength=wrap or (W - BANNER_W - 60))
        lbl.pack(anchor="w", pady=pady)
        return lbl

    # ----------------------------------------------------------------- pages
    def page_welcome(self):
        r = self.big_page("Welcome to the FL Studio Setup Wizard")
        self.para(r, "This will install FL Studio on your computer.")
        self.para(r, "Setup downloads the official FL Studio installer from Image-Line and sets up "
                     "everything FL Studio needs to run on Linux. No terminal, no manual configuration.")
        self.para(r, "It is recommended that you close all other applications before continuing.")
        self.para(r, "Click Next to continue, or Cancel to exit Setup.")

    def page_license(self):
        self.set_header("License Agreement", "Please read the following important information before continuing.")
        f = self.content()
        ttk.Label(f, text="Please read the following License Agreement. You must accept the terms of "
                          "this agreement before continuing with the installation.",
                  wraplength=W - 60, justify="left").pack(anchor="w", pady=(0, 8))
        box = tk.Frame(f, bd=1, relief="solid", bg="#bcbcbc")
        box.pack(fill="both", expand=True)
        t = tk.Text(box, wrap="word", height=10, bd=0, padx=10, pady=8, font=self.f_base,
                    highlightthickness=0, bg="white")
        sb = ttk.Scrollbar(box, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        t.pack(side="left", fill="both", expand=True)
        t.tag_configure("h", font=self.f_bold)
        t.tag_configure("link", foreground=LINK, underline=True)
        t.tag_bind("link", "<Button-1>", lambda e: open_url(C.FL_EULA_URL))
        t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor=""))
        t.insert("end", "IMPORTANT, PLEASE READ CAREFULLY\n\n", "h")
        t.insert("end", "FL Studio is proprietary software made by Image-Line Software. This setup "
                        "program is an independent, unofficial tool. It is not affiliated with, "
                        "endorsed by, or supported by Image-Line.\n\n")
        t.insert("end", "1. FL Studio license\n", "h")
        t.insert("end", "During setup, the official FL Studio installer is downloaded directly from "
                        "Image-Line's servers. By installing and using FL Studio you agree to Image-Line's "
                        "End User License Agreement:\n")
        t.insert("end", C.FL_EULA_URL, "link")
        t.insert("end", "\n\nFL Studio runs as a free trial until you sign in with your Image-Line account "
                        "(Help > About in FL Studio). Your license is not affected by this setup tool.\n\n")
        t.insert("end", "2. Third-party components\n", "h")
        t.insert("end", "Setup also downloads Wine, an open-source compatibility layer (LGPL) that lets "
                        "Windows applications run on Linux, and optionally winetricks (LGPL) with "
                        "Microsoft's freely distributed core fonts.\n\n")
        t.insert("end", "3. Compatibility\n", "h")
        t.insert("end", "Running FL Studio under Wine is not officially supported by Image-Line. Most "
                        "features work well, but some plugins, video features or hardware may not.\n\n")
        t.insert("end", "4. No warranty\n", "h")
        t.insert("end", "THIS SETUP TOOL IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR "
                        "IMPLIED. IN NO EVENT SHALL ITS AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR "
                        "OTHER LIABILITY ARISING FROM ITS USE.\n")
        t.configure(state="disabled")
        rb = ttk.Frame(f)
        rb.pack(anchor="w", pady=(10, 0))
        ttk.Radiobutton(rb, text="I accept the agreement", value="yes", variable=self.v_accept,
                        command=self._license_changed).pack(anchor="w")
        ttk.Radiobutton(rb, text="I do not accept the agreement", value="no", variable=self.v_accept,
                        command=self._license_changed).pack(anchor="w", pady=(2, 0))
        self._license_changed()

    def _license_changed(self):
        self.b_next.configure(state="normal" if self.v_accept.get() == "yes" else "disabled")

    def page_location(self):
        self.set_header("Select Destination Location", "Where should FL Studio be installed?")
        f = self.content((30, 18))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(0, 14))
        if self.icon_small:
            ttk.Label(row, image=self.icon_small).pack(side="left", padx=(0, 12))
        ttk.Label(row, text="Setup will install FL Studio into the following folder.").pack(side="left")
        ttk.Label(f, text="To continue, click Next. If you would like to select a different folder, "
                          "click Browse.", wraplength=W - 60).pack(anchor="w", pady=(0, 8))
        er = ttk.Frame(f)
        er.pack(fill="x")
        e = ttk.Entry(er, textvariable=self.v_prefix)
        e.pack(side="left", fill="x", expand=True, ipady=2)
        ttk.Button(er, text="Browse...", command=self._browse_prefix).pack(side="left", padx=(8, 0))
        ttk.Label(f, text="This folder will contain FL Studio and its own private Windows environment. "
                          "Your projects are saved in your Documents folder.",
                  style="Small.TLabel", wraplength=W - 60).pack(anchor="w", pady=(8, 0))
        self.l_space = ttk.Label(f, text="")
        self.l_space.pack(side="bottom", anchor="w")
        ttk.Label(f, text="At least 6.0 GB of free disk space is required.").pack(side="bottom", anchor="w")
        self.v_prefix.trace_add("write", lambda *a: self._update_space())
        self._update_space()

    def _update_space(self):
        try:
            p = Path(os.path.expanduser(self.v_prefix.get().strip() or "/"))
            while not p.exists():
                p = p.parent
            free = shutil.disk_usage(p).free
            self.l_space.configure(text=f"Space available: {human(free)}",
                                   style="TLabel" if free > 6 * 1024**3 else "Link.TLabel")
        except (OSError, tk.TclError):
            pass

    def _browse_prefix(self):
        cur = Path(os.path.expanduser(self.v_prefix.get()))
        start = cur if cur.exists() else cur.parent if cur.parent.exists() else C.HOME
        d = native_dialog("dir", "Select Destination Folder", start, parent=self.root)
        if d:
            p = Path(d)
            if p.exists() and any(p.iterdir()) and not (p / "system.reg").exists():
                p = p / "FL Studio"   # like Windows: install into a subfolder of a non-empty folder
            self.v_prefix.set(str(p))

    def page_tasks(self):
        self.set_header("Select Additional Tasks", "Which additional tasks should be performed?")
        f = self.content((30, 18))
        ttk.Label(f, text="Select the additional tasks you would like Setup to perform while installing "
                          "FL Studio, then click Next.", wraplength=W - 60).pack(anchor="w", pady=(0, 12))
        ttk.Label(f, text="Additional shortcuts:").pack(anchor="w")
        ttk.Checkbutton(f, text="Create a desktop shortcut", variable=self.v_desktop).pack(anchor="w", padx=18, pady=2)
        ttk.Checkbutton(f, text="Add FL Studio to the applications menu", variable=self.v_menu).pack(anchor="w", padx=18, pady=2)
        ttk.Label(f, text="File associations:").pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(f, text="Open FL Studio projects (.flp) with FL Studio", variable=self.v_flp).pack(anchor="w", padx=18, pady=2)
        ttk.Label(f, text="Compatibility:").pack(anchor="w", pady=(10, 0))
        ttk.Checkbutton(f, text="Install compatibility components (recommended)",
                        variable=self.v_compat).pack(anchor="w", padx=18, pady=2)
        ttk.Label(f, text="Microsoft core fonts (Arial, Verdana, …), about 10 MB.", style="Small.TLabel").pack(anchor="w", padx=40)
        dr = ttk.Frame(f)
        dr.pack(anchor="w", padx=18, pady=(8, 0))
        ttk.Label(dr, text="Display scaling:").pack(side="left")
        ttk.Combobox(dr, textvariable=self.v_dpi, state="readonly", width=14,
                     values=["100% (normal)", "125%", "150%", "175%", "200% (HiDPI)"]).pack(side="left", padx=8)

    def page_source(self):
        self.set_header("FL Studio Installer", "Where should Setup get FL Studio from?")
        f = self.content((30, 16))
        ttk.Radiobutton(f, text="Download the latest version from Image-Line (recommended, about 1 GB)",
                        value="download", variable=self.v_source, command=self._src_changed).pack(anchor="w")
        ttk.Radiobutton(f, text="Use an installer I already downloaded:", value="local",
                        variable=self.v_source, command=self._src_changed).pack(anchor="w", pady=(6, 2))
        lr = ttk.Frame(f)
        lr.pack(fill="x", padx=22)
        self.e_local = ttk.Entry(lr, textvariable=self.v_local)
        self.e_local.pack(side="left", fill="x", expand=True, ipady=2)
        self.b_local = ttk.Button(lr, text="Browse...", command=self._browse_local)
        self.b_local.pack(side="left", padx=(8, 0))
        self.c_keep = ttk.Checkbutton(f, text="Keep the downloaded installer after setup (for offline reinstall)",
                                      variable=self.v_keep)
        self.c_keep.pack(anchor="w", pady=(8, 0))

        adv = ttk.LabelFrame(f, text=" Advanced ", padding=(12, 8))
        adv.pack(fill="x", pady=(16, 0))
        ttk.Radiobutton(adv, text="Open the FL Studio installer and let me click through it (recommended)",
                        value="custom", variable=self.v_express).pack(anchor="w")
        ttk.Radiobutton(adv, text="Try a silent install (experimental; falls back to the installer)",
                        value="express", variable=self.v_express).pack(anchor="w", pady=(2, 8))
        sysw = find_system_wine()
        ttk.Radiobutton(adv, text=f"Use the included Wine {C.WINE_VERSION} (recommended)",
                        value="bundled", variable=self.v_wine).pack(anchor="w")
        rb = ttk.Radiobutton(adv, text="Use the Wine installed on this system" +
                             (f" ({sysw})" if sysw else " (not found)"),
                             value="system", variable=self.v_wine)
        rb.pack(anchor="w", pady=(2, 0))
        if not sysw:
            rb.state(["disabled"])
        self._src_changed()

    def _src_changed(self):
        local = self.v_source.get() == "local"
        for w in (self.e_local, self.b_local):
            w.state(["!disabled"] if local else ["disabled"])
        self.c_keep.state(["disabled"] if local else ["!disabled"])

    def _browse_local(self):
        dl = C.HOME / "Downloads"
        p = native_dialog("file", "Select the FL Studio installer", dl if dl.is_dir() else C.HOME,
                          parent=self.root)
        if p:
            self.v_local.set(p)

    def page_ready(self):
        self.set_header("Ready to Install", "Setup is now ready to begin installing FL Studio on your computer.")
        f = self.content((30, 14))
        ttk.Label(f, text="Click Install to continue with the installation, or click Back if you want "
                          "to review or change any settings.", wraplength=W - 60).pack(anchor="w", pady=(0, 8))
        box = tk.Frame(f, bd=1, relief="solid", bg="#bcbcbc")
        box.pack(fill="both", expand=True)
        t = tk.Text(box, wrap="word", bd=0, padx=10, pady=8, font=self.f_base, highlightthickness=0, bg="white")
        t.pack(fill="both", expand=True)
        lines = ["Destination location:", "      " + self._prefix().as_posix(), "",
                 "FL Studio installer:",
                 "      " + ("Download the latest version from Image-Line" if self.v_source.get() == "download"
                             else self.v_local.get()),
                 "      " + ("Silent install (experimental)" if self.v_express.get() == "express"
                             else "FL Studio's installer will open; click through it"),
                 "", "Additional tasks:"]
        tasks = [(self.v_desktop, "Create a desktop shortcut"), (self.v_menu, "Add to the applications menu"),
                 (self.v_flp, "Open .flp projects with FL Studio"),
                 (self.v_compat, "Install compatibility components")]
        lines += ["      " + txt for v, txt in tasks if v.get()]
        lines += ["      Display scaling: " + self.v_dpi.get(), "", "Windows compatibility layer:",
                  "      " + (f"Wine {C.WINE_VERSION} Staging (included)" if self.v_wine.get() == "bundled"
                              else f"System Wine ({find_system_wine()})")]
        t.insert("end", "\n".join(lines))
        t.configure(state="disabled")
        self.b_next.configure(text="Install")

    def page_maint(self):
        st = self.state
        fl = Path(st.get("fl_exe", ""))
        ver = fl.parent.name if fl.parent.name else "FL Studio"
        self.set_header("Modify FL Studio", "Update, repair or remove the installed copy of FL Studio.")
        f = self.content((30, 14))
        info = ttk.Frame(f)
        info.pack(fill="x", pady=(0, 6))
        if self.icon_small:
            ttk.Label(info, image=self.icon_small).pack(side="left", padx=(0, 12))
        txt = ttk.Frame(info)
        txt.pack(side="left", fill="x", expand=True)
        ttk.Label(txt, text=ver, font=self.f_bold).pack(anchor="w")
        ttk.Label(txt, text=f"Installed {st.get('installed', '')}", style="Small.TLabel").pack(anchor="w")
        ttk.Label(txt, text=st.get("prefix", ""), style="Small.TLabel", wraplength=W - 140).pack(anchor="w")
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(0, 4))
        ttk.Button(btns, text="Launch FL Studio",
                   command=lambda: (launch_fl(), self.root.destroy())).pack(side="right")
        ttk.Button(btns, text="Manage Plugins…", command=self._open_plugins).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Files…", command=self._open_files).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="Settings…", command=self._open_settings).pack(side="right", padx=(0, 8))
        ttk.Separator(f).pack(fill="x", pady=(4, 0))

        def opt(value, title, desc):
            ttk.Radiobutton(f, text=title, value=value, variable=self.v_maint,
                            command=self._maint_changed).pack(anchor="w", pady=(8, 0))
            ttk.Label(f, text=desc, style="Small.TLabel", wraplength=W - 100).pack(anchor="w", padx=24)
        opt("update", "Update", "Download and install the latest version of FL Studio from Image-Line. "
                                "Your projects, settings and plugins are kept.")
        opt("repair", "Repair", "Reinstall FL Studio and recreate its Windows environment settings, "
                                "shortcuts and file associations.")
        opt("remove", "Uninstall", "Remove FL Studio, its Windows environment and all shortcuts from this computer.")
        self.c_del = ttk.Checkbutton(f, text="Also delete my projects, presets and settings (Documents/Image-Line)",
                                     variable=self.v_deldata)
        self.c_del.pack(anchor="w", padx=24, pady=(4, 0))
        self._maint_changed()

    def _maint_changed(self):
        self.c_del.state(["!disabled"] if self.v_maint.get() == "remove" else ["disabled"])
        self.b_next.configure(text={"update": "Update", "repair": "Repair", "remove": "Uninstall"}[self.v_maint.get()])

    def page_progress(self):
        removing = self.pages[0] == "maint" and self.v_maint.get() == "remove"
        verb = {"update": "Updating", "repair": "Repairing", "remove": "Uninstalling"}.get(
            self.v_maint.get() if self.pages[0] == "maint" else "", "Installing")
        does = {"Installing": "installs", "Updating": "updates", "Repairing": "repairs",
                "Uninstalling": "removes"}[verb]
        self.set_header(verb, f"Please wait while Setup {does} FL Studio "
                              f"{'from' if removing else 'on'} your computer.")
        f = self.content((30, 18))
        self.l_status = ttk.Label(f, text="Starting…")
        self.l_status.pack(anchor="w")
        self.pb = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.pb.pack(fill="x", pady=(8, 4))
        self.l_detail = ttk.Label(f, text="", style="Small.TLabel")
        self.l_detail.pack(anchor="w")
        self.l_step = ttk.Label(f, text="", style="Small.TLabel")
        self.l_step.pack(anchor="w")
        self.logframe = tk.Frame(f, bd=1, relief="solid", bg="#bcbcbc")
        self.loglist = tk.Listbox(self.logframe, bd=0, highlightthickness=0, font=self.f_small,
                                  activestyle="none")
        lsb = ttk.Scrollbar(self.logframe, command=self.loglist.yview)
        self.loglist.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.loglist.pack(fill="both", expand=True)
        self.b_details = ttk.Button(f, text="Show details", command=self._toggle_details)
        self.b_details.pack(anchor="w", pady=(12, 0))
        self.b_next.configure(state="disabled")
        self.b_back.configure(state="disabled")
        self.busy = True
        if removing:
            self.l_status.configure(text="Removing FL Studio…")
            self.b_cancel.configure(state="disabled")
            self.worker = threading.Thread(target=self._run_uninstall, daemon=True)
        else:
            self.engine = Engine(self._options(), lambda kind, **kw: self.q.put((kind, kw)))
            self.worker = threading.Thread(target=self.engine.run, daemon=True)
        self.worker.start()
        self.root.after(80, self._poll)

    def _toggle_details(self):
        if self.logframe.winfo_ismapped():
            self.logframe.pack_forget()
            self.b_details.configure(text="Show details")
        else:
            self.logframe.pack(fill="both", expand=True, pady=(10, 0), before=self.b_details)
            self.b_details.configure(text="Hide details")

    def _run_uninstall(self):
        emit = lambda kind, **kw: self.q.put((kind, kw))  # noqa: E731
        try:
            emit("progress", value=0.1, detail="Closing FL Studio…")
            n = [0]

            def log(msg):
                n[0] += 1
                emit("log", text=msg)
                emit("progress", value=min(0.95, 0.15 + n[0] * 0.2), detail=msg)
            kept = uninstall(self.v_deldata.get(), log)
            emit("progress", value=1.0, detail="Completed")
            emit("done", ok=True, removed=True, kept=str(kept or ""))
        except Exception as e:  # noqa: BLE001
            emit("done", ok=False, error=str(e))

    def _poll(self):
        try:
            while True:
                kind, kw = self.q.get_nowait()
                if kind == "progress":
                    self.pb["value"] = int(kw["value"] * 1000)
                    if kw.get("detail"):
                        self.l_detail.configure(text=kw["detail"])
                elif kind == "step":
                    self.l_status.configure(text=kw["label"] + "…")
                    self.l_step.configure(text=f"Step {kw['index'] + 1} of {kw['count']}")
                elif kind == "log":
                    self.loglist.insert("end", kw["text"])
                    self.loglist.see("end")
                elif kind == "done":
                    self.busy = False
                    self.result = kw
                    if kw.get("cancelled"):
                        self._cleanup_after_cancel()
                        self.root.destroy()
                        return
                    self.idx = self.pages.index("finish")
                    self.show()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def page_finish(self):
        res = self.result or {}
        removed = res.get("removed")
        if res.get("ok") and removed:
            r = self.big_page("FL Studio was removed")
            self.para(r, "FL Studio was successfully removed from your computer.")
            if res.get("kept"):
                self.para(r, f"Your projects and settings were kept in:\n{res['kept']}")
            elif not self.v_deldata.get():
                self.para(r, "Your projects and settings in Documents/Image-Line were kept.")
        elif res.get("ok"):
            r = self.big_page("Completing the FL Studio Setup Wizard")
            self.para(r, "Setup has finished installing FL Studio on your computer. FL Studio can be "
                         "started from the installed shortcuts.")
            self.para(r, "To unlock your edition, open FL Studio, choose Help > About and sign in with "
                         "your Image-Line account.", style="White.TLabel", pady=(0, 4))
            lk = ttk.Label(r, text="Install VST / CLAP plugins now…", style="Link.TLabel",
                           background="white", cursor="hand2")
            lk.pack(anchor="w", pady=(0, 2))
            lk.bind("<Button-1>", lambda e: self._open_plugins())
            lk2 = ttk.Label(r, text="Add sample packs and presets…", style="Link.TLabel",
                            background="white", cursor="hand2")
            lk2.pack(anchor="w", pady=(0, 10))
            lk2.bind("<Button-1>", lambda e: self._open_files())
            if res.get("warnings"):
                w = tk.Text(r, height=4, wrap="word", bd=1, relief="solid", font=self.f_small,
                            bg="#fff8e5", highlightthickness=0, padx=6, pady=4)
                w.insert("end", "Notes:\n" + "\n".join("• " + x for x in res["warnings"]))
                w.configure(state="disabled")
                w.pack(fill="x", pady=(0, 8))
            if res.get("fl_running"):
                self.v_launch.set(False)
                self.para(r, "FL Studio is already open.", pady=(4, 0))
            else:
                ttk.Checkbutton(r, text="Launch FL Studio", variable=self.v_launch,
                                style="White.TCheckbutton").pack(anchor="w", pady=(4, 0))
        else:
            r = self.big_page("Setup was not completed")
            self.para(r, "FL Studio could not be set up because of this problem:")
            msg = tk.Text(r, height=6, wrap="word", bd=1, relief="solid", font=self.f_small,
                          bg="#fdecea", highlightthickness=0, padx=6, pady=4)
            msg.insert("end", res.get("error", "Unknown error"))
            msg.configure(state="disabled")
            msg.pack(fill="x", pady=(0, 10))
            self.para(r, "You can run Setup again; files that were already downloaded will be reused.")
            ttk.Label(r, text="Open the setup log", style="Link.TLabel", background="white",
                      cursor="hand2").pack(anchor="w")
            r.winfo_children()[-1].bind("<Button-1>", lambda e: open_url(C.LOG_FILE.as_uri()))
            self.v_launch.set(False)
        self.b_back.configure(state="disabled")
        self.b_cancel.configure(state="disabled")
        self.b_next.configure(text="Finish", state="normal")

    def _open_plugins(self):
        PluginManager(tk.Toplevel(self.root), self)

    def _open_files(self):
        from .files import FileBrowser
        FileBrowser(tk.Toplevel(self.root), self)

    def _open_settings(self):
        from .settings import SettingsWindow
        SettingsWindow(tk.Toplevel(self.root), self)

    # ------------------------------------------------------------ navigation
    def _prefix(self):
        return Path(os.path.expanduser(self.v_prefix.get().strip())).absolute()

    def _options(self):
        maint = self.pages[0] == "maint"
        mode = self.v_maint.get() if maint else "install"
        st = (self.state or {}).get("options", {})
        o = Options(mode=mode, prefix=self._prefix())
        if maint:
            o.system_wine = bool(self.state.get("system_wine"))
            o.menu, o.desktop_shortcut = st.get("menu", True), st.get("desktop_shortcut", True)
            o.flp_assoc, o.compat, o.dpi = st.get("flp_assoc", True), st.get("compat", True), st.get("dpi", 96)
            return o
        if self.v_source.get() == "local":
            o.installer_path = Path(self.v_local.get())
        o.express = self.v_express.get() == "express"
        o.system_wine = self.v_wine.get() == "system"
        o.compat, o.menu = self.v_compat.get(), self.v_menu.get()
        o.desktop_shortcut, o.flp_assoc = self.v_desktop.get(), self.v_flp.get()
        o.dpi, o.keep_installer = self._dpi_value(), self.v_keep.get()
        self.prefix_existed = o.prefix.exists() and any(o.prefix.iterdir())
        return o

    def validate(self):
        page = self.pages[self.idx]
        if page == "location":
            p = self._prefix()
            if not self.v_prefix.get().strip():
                messagebox.showerror("FL Studio Setup", "You must enter a folder path.", parent=self.root)
                return False
            if p.exists() and not p.is_dir():
                messagebox.showerror("FL Studio Setup", "The path you entered is a file, not a folder.",
                                     parent=self.root)
                return False
            probe = p
            while not probe.exists():
                probe = probe.parent
            if not os.access(probe, os.W_OK):
                messagebox.showerror("FL Studio Setup", f"You don't have permission to write to\n{probe}",
                                     parent=self.root)
                return False
            if p.exists() and any(p.iterdir()) and not (p / "system.reg").exists():
                if not messagebox.askyesno("Folder Exists", f"The folder:\n\n{p}\n\nalready exists and is not "
                                           "empty. Would you like to install into a subfolder named "
                                           "\"FL Studio\" instead?", parent=self.root):
                    return False
                self.v_prefix.set(str(p / "FL Studio"))
        if page == "source" and self.v_source.get() == "local":
            lp = Path(self.v_local.get())
            if not lp.is_file():
                messagebox.showerror("FL Studio Setup", "Please choose the FL Studio installer (.exe) file.",
                                     parent=self.root)
                return False
        if page == "maint" and self.v_maint.get() == "remove":
            extra = ("\n\nYour projects, presets and settings will also be DELETED."
                     if self.v_deldata.get() else "")
            if not messagebox.askyesno("FL Studio Uninstall", "Are you sure you want to completely remove "
                                       "FL Studio and all of its components?" + extra,
                                       icon="warning", parent=self.root):
                return False
        return True

    def on_next(self):
        page = self.pages[self.idx]
        if page == "finish":
            if self.v_launch.get() and (self.result or {}).get("ok") and not (self.result or {}).get("removed"):
                launch_fl()
            self.root.destroy()
            return
        if not self.validate():
            return
        self.idx += 1
        self.show()

    def on_back(self):
        if self.idx > 0:
            self.idx -= 1
            self.show()

    def on_cancel(self):
        page = self.pages[self.idx]
        if page == "finish":
            self.root.destroy()
            return
        if self.busy and not self.engine:
            return  # uninstall in progress cannot be interrupted
        if page == "maint":
            self.root.destroy()
            return
        if messagebox.askyesno("Exit Setup", "Setup is not complete. If you exit now, FL Studio will not "
                               "be installed.\n\nYou may run Setup again at another time to complete the "
                               "installation.\n\nExit Setup?", parent=self.root):
            if self.busy and self.engine:
                self.l_status.configure(text="Cancelling…")
                self.b_cancel.configure(state="disabled")
                threading.Thread(target=self.engine.cancel, daemon=True).start()
                return  # _poll closes the window when the engine stops
            self.root.destroy()

    def _cleanup_after_cancel(self):
        """Remove a half-created Windows environment from a fresh install."""
        if self.pages[0] != "maint" and not self.prefix_existed:
            p = self._prefix()
            if (p / "drive_c").is_dir() and not C.load_state():
                shutil.rmtree(p, ignore_errors=True)

    def run(self):
        self.root.mainloop()


class PluginManager:
    """'Add or remove plugins' window: installers, plugin files and archives."""

    def __init__(self, win, owner=None, files=None):
        self.win = win
        if owner is None:
            import types
            ns = types.SimpleNamespace(root=win)
            Wizard._setup_style(ns)
            self.f_bold, self.f_small = ns.f_bold, ns.f_small
        else:
            self.f_bold, self.f_small = owner.f_bold, owner.f_small
            win.transient(owner.root)
        win.title("FL Studio Plugins")
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"700x500+{(sw - 700) // 2}+{(sh - 500) // 3}")
        win.minsize(560, 400)
        win.configure(bg=GREY)
        self.icon = None
        try:
            self.icon = tk.PhotoImage(file=str(ASSETS / "icon.png"))
            f = max(1, self.icon.width() // 48)
            self.icon_small = self.icon.subsample(f, f)
            win.iconphoto(False, self.icon)
        except tk.TclError:
            self.icon_small = None
        self.q = queue.Queue()
        self.busy = False

        hdr = tk.Frame(win, bg="white", height=64)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        txt = tk.Frame(hdr, bg="white")
        txt.pack(side="left", padx=18, pady=(10, 6), fill="both", expand=True)
        ttk.Label(txt, text="Plugins", style="Hdr.TLabel").pack(anchor="w")
        ttk.Label(txt, text="Add or remove VST, VST3 and CLAP plugins for FL Studio.",
                  style="Sub.TLabel").pack(anchor="w", padx=(14, 0), pady=(3, 0))
        if self.icon_small:
            tk.Label(hdr, image=self.icon_small, bg="white").pack(side="right", padx=12)
        ttk.Separator(win).pack(fill="x")

        foot = tk.Frame(win, bg=GREY)
        foot.pack(side="bottom", fill="x")
        ttk.Separator(foot).pack(fill="x")
        fr = tk.Frame(foot, bg=GREY)
        fr.pack(fill="x", padx=10, pady=10)
        ttk.Button(fr, text="Close", command=self.close).pack(side="right")
        ttk.Button(fr, text="Launch FL Studio", command=lambda: launch_fl()).pack(side="right", padx=(0, 10))
        self.status = ttk.Label(fr, text="", style="Small.TLabel")
        self.status.pack(side="left")

        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=16, pady=12)
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(0, 8))
        self.b_add = ttk.Button(bar, text="Add Plugins…", command=self.add)
        self.b_add.pack(side="left")
        self.b_rm = ttk.Button(bar, text="Remove", command=self.remove)
        self.b_rm.pack(side="left", padx=6)
        ttk.Button(bar, text="Open Plugin Folder", command=self.open_folder).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="right")

        tf = ttk.Frame(body)
        tf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tf, columns=("type", "where"), show="headings", selectmode="extended")
        self.tree.heading("type", text="Type")
        self.tree.heading("where", text="Name")
        self.tree.column("type", width=70, stretch=False, anchor="center")
        self.tree["displaycolumns"] = ("where", "type")
        sb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._sel())
        self.tree.bind("<Delete>", lambda e: self.remove())

        self.pb = ttk.Progressbar(body, mode="indeterminate")
        tip = ("You can add plugin installers (.exe, .msi), plugin files (.vst3, .dll, .clap) or a .zip "
               "you downloaded. Afterwards, open FL Studio and choose Options > Manage plugins > "
               "Find installed plugins.")
        ttk.Label(body, text=tip, style="Small.TLabel", wraplength=640, justify="left").pack(
            anchor="w", pady=(8, 0))
        self.paths = {}
        self.refresh()
        win.protocol("WM_DELETE_WINDOW", self.close)
        if files:
            win.after(300, lambda: self.install_files(files))

    def _sel(self):
        self.b_rm.state(["!disabled"] if self.tree.selection() and not self.busy else ["disabled"])

    def refresh(self):
        from . import plugins
        self.tree.delete(*self.tree.get_children())
        self.paths.clear()
        items = plugins.list_plugins()
        for name, kind, path in items:
            iid = self.tree.insert("", "end", values=(kind, name))
            self.paths[iid] = path
        if not items:
            self.tree.insert("", "end", values=("", "No plugins installed yet. Click \"Add Plugins…\"."))
        if not self.busy:
            self.status.configure(text=f"{len(items)} plugin(s) installed")
        self._sel()

    def add(self):
        dl = C.HOME / "Downloads"
        files = native_dialog("plugins", "Choose plugins, plugin installers or archives",
                              dl if dl.is_dir() else C.HOME, parent=self.win)
        if files:
            self.install_files(files)

    def install_files(self, files):
        from . import plugins
        if self.busy:
            return
        self.busy = True
        self.b_add.state(["disabled"])
        self._sel()
        self.pb.pack(fill="x", pady=(8, 0), before=self.tree.master)
        self.pb.start(12)
        log = lambda m: self.q.put(("log", m))  # noqa: E731

        def work():
            n = 0
            for f in files:
                try:
                    n += plugins.install(Path(f), log)
                except Exception as e:  # noqa: BLE001
                    log(f"Could not install {Path(f).name}: {e}")
            self.q.put(("done", n))
        threading.Thread(target=work, daemon=True).start()
        self.win.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.status.configure(text=val)
                else:
                    self.busy = False
                    self.pb.stop()
                    self.pb.pack_forget()
                    self.b_add.state(["!disabled"])
                    self.refresh()
                    self.status.configure(text=f"Done. {val} item(s) installed.")
                    if val:
                        messagebox.showinfo("Plugins installed",
                                            "Plugins installed.\n\nIn FL Studio, choose Options > "
                                            "Manage plugins > Find installed plugins to load them.",
                                            parent=self.win)
                    return
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def remove(self):
        from . import plugins
        sel = [i for i in self.tree.selection() if i in self.paths]
        if not sel:
            return
        names = "\n".join("  " + self.tree.item(i, "values")[1] for i in sel[:8])
        if len(sel) > 8:
            names += f"\n  …and {len(sel) - 8} more"
        if not messagebox.askyesno("Remove plugins", f"Remove these plugins?\n\n{names}",
                                   icon="warning", parent=self.win):
            return
        for i in sel:
            try:
                plugins.remove(self.paths[i])
            except (OSError, ValueError) as e:
                messagebox.showerror("Remove plugins", str(e), parent=self.win)
        self.refresh()

    def open_folder(self):
        from . import plugins
        f = plugins.folders()
        sel = [i for i in self.tree.selection() if i in self.paths]
        target = self.paths[sel[0]].parent if sel else f["VST3"]
        target.mkdir(parents=True, exist_ok=True)
        from .files import FileBrowser
        FileBrowser(tk.Toplevel(self.win), start=target)

    def close(self):
        if self.busy and not messagebox.askyesno(
                "FL Studio Plugins", "A plugin is still being installed. Close anyway?", parent=self.win):
            return
        self.win.destroy()


def plugin_manager_mode(files=None):
    root = tk.Tk(className="FL Studio Setup")
    if not C.LAUNCHER.exists():
        root.withdraw()
        messagebox.showerror("FL Studio", "FL Studio is not installed yet. Run FL Studio Setup first.")
        return
    PluginManager(root, files=files)
    root.mainloop()
