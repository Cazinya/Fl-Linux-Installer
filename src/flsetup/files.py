"""FL Studio Files: an Explorer-style browser for sample packs, presets, projects and plugins."""
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from urllib.parse import unquote, urlparse

from . import config as C
from . import plugins

GREY = "#f0f0f0"
AUDIO = {".wav", ".mp3", ".ogg", ".flac", ".aif", ".aiff", ".mid", ".midi"}
ARCHIVE = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".xz", ".bz2"}


# ------------------------------------------------------------------ locations

def _user_docs(prefix: Path) -> Path:
    users = prefix / "drive_c" / "users"
    if users.is_dir():
        for u in sorted(users.iterdir()):
            if u.name.lower() != "public" and (u / "Documents").exists():
                return (u / "Documents").resolve()
    return C.HOME / "Documents"


def locations():
    """(label, path, tip) for every place FL Studio content lives."""
    st = C.load_state() or {}
    prefix = Path(st.get("prefix") or C.DEFAULT_PREFIX)
    fl = Path(st.get("fl_exe", ""))
    install = fl.parent if fl.name else None
    docs = _user_docs(prefix)
    il = docs / "Image-Line"
    out = []
    if install and (install / "Data" / "Patches").is_dir():
        out.append(("Sample packs", install / "Data" / "Patches" / "Packs",
                    "Folders here show up in FL Studio's Browser under 'Packs'. Import a sample "
                    "pack folder or .zip and it's ready to use."))
    out += [
        ("My presets", il / "FL Studio" / "Presets" / "Plugin presets",
         "Presets for FL Studio's own plugins. Put them in Generators\\<plugin> or "
         "Effects\\<plugin>, e.g. Generators\\Sakura."),
        ("Projects", il / "FL Studio" / "Projects", "Your FL Studio projects (.flp)."),
        ("Image-Line user data", il, "Everything FL Studio saves for you: projects, recordings, "
         "presets, settings. Kept if you uninstall."),
    ]
    if install:
        out += [("Factory content", install / "Data" / "Patches",
                 "Presets, packs and templates that came with FL Studio."),
                ("FL Studio program", install, "The FL Studio installation folder.")]
    f = plugins.folders()
    out += [("VST3 plugins", f["VST3"], "VST3 plugins (.vst3)."),
            ("VST2 plugins", f["VST2"], "Older VST plugins (.dll)."),
            ("CLAP plugins", f["CLAP"], "CLAP plugins (.clap)."),
            ("Windows drive (C:)", prefix / "drive_c", "FL Studio's private Windows drive."),
            ("Home folder", C.HOME, "Your Linux home folder.")]
    return out, prefix


def windows_path(p: Path, prefix: Path):
    try:
        rel = p.resolve().relative_to((prefix / "drive_c").resolve())
        return "C:\\" + str(rel).replace("/", "\\") if str(rel) != "." else "C:\\"
    except ValueError:
        return None


def unique(dest: Path):
    if not dest.exists():
        return dest
    stem, suf = (dest.stem, dest.suffix) if dest.is_file() else (dest.name, "")
    n = 2
    while (dest.parent / f"{stem} ({n}){suf}").exists():
        n += 1
    return dest.parent / f"{stem} ({n}){suf}"


def is_archive(p: Path):
    n = p.name.lower()
    return p.is_file() and (p.suffix.lower() in ARCHIVE or n.endswith((".tar.gz", ".tar.xz", ".tar.bz2")))


def archive_stem(p: Path):
    n = p.name
    for ext in (".tar.gz", ".tar.xz", ".tar.bz2"):
        if n.lower().endswith(ext):
            return n[: -len(ext)]
    return p.stem


def import_item(src: Path, dest_dir: Path, extract: bool, log):
    """Copy a file/folder into dest_dir; unpack archives into a folder of their own."""
    if extract and is_archive(src):
        tmp = Path(tempfile.mkdtemp(prefix="flimport-"))
        try:
            log(f"Unpacking {src.name}…")
            plugins._extract(src, tmp, log)
            items = [p for p in tmp.iterdir() if not p.name.startswith((".", "__MACOSX"))]
            # A zip that already contains one top folder: use that folder directly.
            source = items[0] if len(items) == 1 and items[0].is_dir() else tmp
            target = unique(dest_dir / (items[0].name if source is not tmp else archive_stem(src)))
            shutil.copytree(source, target)
            return target
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    target = unique(dest_dir / src.name)
    log(f"Copying {src.name}…")
    if src.is_dir():
        shutil.copytree(src, target, symlinks=True)
    else:
        shutil.copy2(src, target)
    return target


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# --------------------------------------------------------------------- window

class FileBrowser:
    def __init__(self, win, owner=None, start=None):
        from .ui import ASSETS, Wizard, open_url
        self.open_url = open_url
        self.win = win
        if owner is None:
            import types
            ns = types.SimpleNamespace(root=win)
            Wizard._setup_style(ns)
            self.f_bold, self.f_small = ns.f_bold, ns.f_small
        else:
            self.f_bold, self.f_small = owner.f_bold, owner.f_small
        win.title("FL Studio Files")
        win.configure(bg=GREY)
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        ww, wh = min(980, sw - 80), min(640, sh - 80)
        win.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 3}")
        win.minsize(720, 460)
        try:
            self.icon = tk.PhotoImage(file=str(ASSETS / "icon.png"))
            win.iconphoto(False, self.icon)
        except tk.TclError:
            pass
        self._make_icons()
        self.locs, self.prefix = locations()
        self.cwd = None
        self.history = []
        self.q = queue.Queue()
        self.busy = False
        self.v_extract = tk.BooleanVar(value=True)

        self._build()
        start = start or self.locs[0][1]
        self.go(Path(start))
        win.bind("<Control-v>", lambda e: self.paste())
        win.bind("<F5>", lambda e: self.refresh())
        win.bind("<Alt-Left>", lambda e: self.back())
        win.bind("<Alt-Up>", lambda e: self.up())
        win.bind("<BackSpace>", lambda e: self.up() if not isinstance(e.widget, ttk.Entry) else None)

    # -------------------------------------------------------------- building
    def _make_icons(self):
        def img(draw):
            im = tk.PhotoImage(width=16, height=16)
            for color, (x0, y0, x1, y1) in draw:
                im.put(color, to=(x0, y0, x1, y1))
            return im
        self.i_folder = img([("#c7922f", (1, 3, 7, 5)), ("#e8b14a", (1, 5, 15, 14)),
                             ("#f5c86a", (2, 6, 14, 13))])
        self.i_file = img([("#8a8a8a", (3, 1, 13, 15)), ("#ffffff", (4, 2, 12, 14))])
        self.i_audio = img([("#2f6fb7", (3, 1, 13, 15)), ("#dbe9fb", (4, 2, 12, 14)),
                            ("#2f6fb7", (6, 5, 8, 12)), ("#2f6fb7", (8, 5, 11, 7)),
                            ("#2f6fb7", (5, 10, 8, 13))])
        self.i_zip = img([("#7a5a2a", (3, 1, 13, 15)), ("#e9d3a8", (4, 2, 12, 14)),
                          ("#7a5a2a", (7, 2, 9, 9))])
        self.i_plugin = img([("#5b3fa0", (2, 2, 14, 14)), ("#b9a6ea", (3, 3, 13, 13)),
                             ("#5b3fa0", (5, 5, 11, 11))])

    def _build(self):
        w = self.win
        ttk.Style(w).configure("Tool.TButton", padding=(8, 3))
        B = lambda parent, **kw: ttk.Button(parent, style="Tool.TButton", **kw)  # noqa: E731
        # toolbar
        tb = tk.Frame(w, bg=GREY)
        tb.pack(fill="x", padx=8, pady=(8, 4))
        self.b_back = B(tb, text="◀", width=2, command=self.back)
        self.b_back.pack(side="left")
        B(tb, text="▲", width=2, command=self.up).pack(side="left", padx=(4, 0))
        B(tb, text="⟳", width=2, command=self.refresh).pack(side="left", padx=(4, 8))
        self.v_path = tk.StringVar()
        e = ttk.Entry(tb, textvariable=self.v_path)
        e.pack(side="left", fill="x", expand=True, ipady=2)
        e.bind("<Return>", lambda ev: self.go(Path(os.path.expanduser(self.v_path.get()))))

        # action bar
        ab = tk.Frame(w, bg=GREY)
        ab.pack(fill="x", padx=8, pady=(0, 6))
        B(ab, text="Import Files…", command=self.import_files).pack(side="left")
        B(ab, text="Import Folder…", command=self.import_folder).pack(side="left", padx=4)
        B(ab, text="Paste", command=self.paste).pack(side="left")
        ttk.Separator(ab, orient="vertical").pack(side="left", fill="y", padx=8)
        B(ab, text="New Folder", command=self.new_folder).pack(side="left")
        self.b_rename = B(ab, text="Rename", command=self.rename)
        self.b_rename.pack(side="left", padx=4)
        self.b_delete = B(ab, text="Delete", command=self.delete)
        self.b_delete.pack(side="left")
        B(ab, text="Open in File Manager", command=self.open_external).pack(side="right")

        # status bar
        sb = tk.Frame(w, bg=GREY)
        sb.pack(side="bottom", fill="x", padx=8, pady=(4, 6))
        self.status = ttk.Label(sb, text="", style="Small.TLabel")
        self.status.pack(side="left")
        self.pb = ttk.Progressbar(sb, mode="indeterminate", length=140)
        self.l_free = ttk.Label(sb, text="", style="Small.TLabel")
        self.l_free.pack(side="right")
        ttk.Checkbutton(sb, text="Unpack .zip files when importing", variable=self.v_extract).pack(
            side="right", padx=14)

        pane = ttk.PanedWindow(w, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8)
        # sidebar
        side = ttk.Frame(pane)
        self.side = ttk.Treeview(side, show="tree", selectmode="browse")
        self.side.pack(fill="both", expand=True)
        for i, (label, path, _tip) in enumerate(self.locs):
            self.side.insert("", "end", iid=str(i), text="  " + label, image=self.i_folder)
        self.side.bind("<<TreeviewSelect>>", self._side_pick)
        pane.add(side, weight=1)

        # main list
        main = ttk.Frame(pane)
        self.tip = ttk.Label(main, text="", style="Small.TLabel", wraplength=640, justify="left")
        self.tip.pack(fill="x", pady=(0, 4))
        self.winpath = ttk.Label(main, text="", style="Small.TLabel")
        self.winpath.pack(anchor="w")
        lf = ttk.Frame(main)
        lf.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(lf, columns=("size", "mod", "kind"), selectmode="extended")
        self.tree.heading("#0", text="Name", command=lambda: self._sort("name"))
        self.tree.heading("size", text="Size", command=lambda: self._sort("size"))
        self.tree.heading("mod", text="Date modified", command=lambda: self._sort("mod"))
        self.tree.heading("kind", text="Type", command=lambda: self._sort("kind"))
        self.tree.column("#0", width=320)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("mod", width=140, stretch=False)
        self.tree.column("kind", width=110, stretch=False)
        vs = ttk.Scrollbar(lf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<Delete>", lambda e: self.delete())
        self.tree.bind("<F2>", lambda e: self.rename())
        self.tree.bind("<Button-3>", self._context)
        pane.add(main, weight=4)

        self.menu = tk.Menu(w, tearoff=False)
        self.menu.add_command(label="Open", command=self.open_selected)
        self.menu.add_command(label="Rename", command=self.rename)
        self.menu.add_command(label="Delete", command=self.delete)
        self.menu.add_separator()
        self.menu.add_command(label="Copy path", command=self.copy_path)
        self.menu.add_command(label="Show in File Manager", command=self.open_external)
        self.sort_key, self.sort_rev = "name", False
        self.paths = {}

    # ----------------------------------------------------------- navigation
    def _side_pick(self, _e):
        sel = self.side.selection()
        if sel:
            path = self.locs[int(sel[0])][1]
            self.go(path)

    def go(self, path: Path, remember=True):
        path = Path(path)
        if not path.exists():
            # Known FL locations may not exist yet; create them on first visit.
            if any(path == p for _, p, _ in self.locs):
                try:
                    path.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    messagebox.showerror("FL Studio Files", str(e), parent=self.win)
                    return
            else:
                messagebox.showerror("FL Studio Files", f"Folder not found:\n{path}", parent=self.win)
                return
        if not path.is_dir():
            return
        if remember and self.cwd and self.cwd != path:
            self.history.append(self.cwd)
        self.cwd = path
        self.v_path.set(str(path))
        wp = windows_path(path, self.prefix)
        self.winpath.configure(text=f"In FL Studio: {wp}" if wp else "")
        tip = next((t for _, p, t in self.locs if p == path), "")
        self.tip.configure(text=tip)
        self.refresh()

    def back(self):
        if self.history:
            self.go(self.history.pop(), remember=False)

    def up(self):
        if self.cwd and self.cwd.parent != self.cwd:
            self.go(self.cwd.parent)

    def _kind(self, p: Path):
        if p.is_dir():
            if p.suffix.lower() == ".vst3":
                return "VST3 plugin", self.i_plugin
            return "Folder", self.i_folder
        s = p.suffix.lower()
        if s in AUDIO:
            return f"{s[1:].upper()} audio" if s not in (".mid", ".midi") else "MIDI", self.i_audio
        if is_archive(p):
            return "Archive", self.i_zip
        if s in (".dll", ".clap", ".vst3"):
            return "Plugin", self.i_plugin
        names = {".fst": "FL preset", ".flp": "FL project", ".nmsv": "Sytrus preset",
                 ".exe": "Program", ".txt": "Text"}
        return names.get(s, (s[1:].upper() + " file") if s else "File"), self.i_file

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.paths.clear()
        try:
            entries = [p for p in self.cwd.iterdir() if not p.name.startswith(".")]
        except OSError as e:
            self.status.configure(text=f"Can't open this folder: {e}")
            return
        rows = []
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue
            kind, icon = self._kind(p)
            rows.append((p, st, kind, icon))
        key = {"name": lambda r: r[0].name.lower(), "size": lambda r: r[1].st_size,
               "mod": lambda r: r[1].st_mtime, "kind": lambda r: r[2]}[self.sort_key]
        rows.sort(key=key, reverse=self.sort_rev)
        rows.sort(key=lambda r: not r[0].is_dir())  # folders first, like Explorer
        for p, st, kind, icon in rows:
            size = "" if p.is_dir() else human(st.st_size)
            mod = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            iid = self.tree.insert("", "end", text=" " + p.name, image=icon, values=(size, mod, kind))
            self.paths[iid] = p
        try:
            free = shutil.disk_usage(self.cwd).free
            self.l_free.configure(text=f"{human(free)} free")
        except OSError:
            pass
        if not self.busy:
            self.status.configure(text=f"{len(rows)} item(s)")

    def _sort(self, key):
        self.sort_rev = (not self.sort_rev) if self.sort_key == key else False
        self.sort_key = key
        self.refresh()

    def selected(self):
        return [self.paths[i] for i in self.tree.selection() if i in self.paths]

    def open_selected(self):
        sel = self.selected()
        if not sel:
            return
        p = sel[0]
        if p.is_dir() and p.suffix.lower() != ".vst3":
            self.go(p)
        else:
            self.open_url(p.as_uri())  # preview samples etc. with the desktop's default app

    def _context(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.menu.tk_popup(e.x_root, e.y_root)

    # ------------------------------------------------------------- actions
    def import_files(self):
        from .ui import native_dialog
        dl = C.HOME / "Downloads"
        files = native_dialog("any_files", "Choose files to import (samples, presets, .zip packs)",
                              dl if dl.is_dir() else C.HOME, parent=self.win)
        if files:
            self.do_import([Path(f) for f in files])

    def import_folder(self):
        from .ui import native_dialog
        dl = C.HOME / "Downloads"
        d = native_dialog("dir", "Choose a folder to import (e.g. a sample pack)",
                          dl if dl.is_dir() else C.HOME, parent=self.win)
        if d:
            self.do_import([Path(d)])

    def paste(self):
        """Paste files copied in the desktop file manager (Ctrl+C in Files/Dolphin/Thunar)."""
        paths = []
        for typ in ("x-special/gnome-copied-files", "text/uri-list", "STRING", "UTF8_STRING"):
            try:
                data = self.win.clipboard_get(type=typ)
            except tk.TclError:
                continue
            for ln in data.splitlines():
                ln = ln.strip()
                if ln.startswith("file://"):
                    paths.append(Path(unquote(urlparse(ln).path)))
                elif ln.startswith("/") and Path(ln).exists():
                    paths.append(Path(ln))
            if paths:
                break
        paths = [p for p in dict.fromkeys(paths) if p.exists()]
        if not paths:
            messagebox.showinfo("FL Studio Files", "Nothing to paste.\n\nCopy files or folders in your "
                                "file manager (Ctrl+C), then press Ctrl+V here.", parent=self.win)
            return
        self.do_import(paths)

    def do_import(self, paths):
        if self.busy:
            return
        dest = self.cwd
        inside = [p for p in paths if dest == p or dest.is_relative_to(p)]
        if inside:
            messagebox.showerror("FL Studio Files", "Can't copy a folder into itself.", parent=self.win)
            return
        self.busy = True
        self.pb.pack(side="left", padx=10)
        self.pb.start(12)
        extract = self.v_extract.get()
        log = lambda m: self.q.put(("log", m))  # noqa: E731

        def work():
            done, errors = [], []
            for p in paths:
                try:
                    done.append(import_item(p, dest, extract, log))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{p.name}: {e}")
            self.q.put(("done", (done, errors)))
        threading.Thread(target=work, daemon=True).start()
        self.win.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.status.configure(text=val)
                else:
                    done, errors = val
                    self.busy = False
                    self.pb.stop()
                    self.pb.pack_forget()
                    self.refresh()
                    self.status.configure(text=f"Imported {len(done)} item(s).")
                    for iid, p in self.paths.items():
                        if p in done:
                            self.tree.selection_add(iid)
                            self.tree.see(iid)
                    if errors:
                        messagebox.showerror("FL Studio Files", "Some items could not be imported:\n\n"
                                             + "\n".join(errors[:10]), parent=self.win)
                    elif done and windows_path(self.cwd, self.prefix):
                        self.status.configure(text=f"Imported {len(done)} item(s). In FL Studio, "
                                                   "refresh the Browser (right-click > Refresh) to see them.")
                    return
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    def new_folder(self):
        name = simpledialog.askstring("New Folder", "Folder name:", initialvalue="New folder",
                                      parent=self.win)
        if not name:
            return
        if "/" in name or name in (".", ".."):
            messagebox.showerror("FL Studio Files", "That name can't be used.", parent=self.win)
            return
        p = unique(self.cwd / name)
        try:
            p.mkdir()
        except OSError as e:
            messagebox.showerror("FL Studio Files", str(e), parent=self.win)
        self.refresh()

    def rename(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        p = sel[0]
        name = simpledialog.askstring("Rename", "New name:", initialvalue=p.name, parent=self.win)
        if not name or name == p.name:
            return
        if "/" in name or (p.parent / name).exists():
            messagebox.showerror("FL Studio Files", "That name can't be used or already exists.",
                                 parent=self.win)
            return
        try:
            p.rename(p.parent / name)
        except OSError as e:
            messagebox.showerror("FL Studio Files", str(e), parent=self.win)
        self.refresh()

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        trash = shutil.which("gio")
        what = sel[0].name if len(sel) == 1 else f"these {len(sel)} items"
        msg = (f"Move {what} to the Trash?" if trash else
               f"Permanently delete {what}? This can't be undone.")
        if not messagebox.askyesno("Delete", msg, icon="warning", parent=self.win):
            return
        for p in sel:
            try:
                if trash and subprocess.run(["gio", "trash", str(p)], capture_output=True).returncode == 0:
                    continue
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError as e:
                messagebox.showerror("FL Studio Files", f"{p.name}: {e}", parent=self.win)
        self.refresh()

    def copy_path(self):
        sel = self.selected()
        if sel:
            self.win.clipboard_clear()
            self.win.clipboard_append(str(sel[0]))

    def open_external(self):
        sel = self.selected()
        target = sel[0].parent if sel else self.cwd
        self.open_url(target.as_uri())


def files_mode(start=None):
    root = tk.Tk(className="FL Studio Setup")
    if not C.load_state():
        root.withdraw()
        messagebox.showerror("FL Studio", "FL Studio is not installed yet. Run FL Studio Setup first.")
        return
    FileBrowser(root, start=start)
    root.mainloop()
