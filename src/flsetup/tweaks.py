"""Compatibility tweaks for an installed FL Studio: graphics, runtimes, audio, system.

Each tweak works on the existing Wine prefix recorded in install.json and can be
applied or undone from the Settings window (or re-applied by Repair).
"""
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from . import config as C

DXVK_VERSION = os.environ.get("FLSETUP_DXVK_VERSION", "3.1.1")
DXVK_URL = f"https://github.com/doitsujin/dxvk/releases/download/v{DXVK_VERSION}/dxvk-{DXVK_VERSION}.tar.gz"
DXVK_DLLS = ["d3d8", "d3d9", "d3d10core", "d3d11", "dxgi"]
VCREDIST = {
    "x64": "https://aka.ms/vs/17/release/vc_redist.x64.exe",
    "x86": "https://aka.ms/vs/17/release/vc_redist.x86.exe",
}
VC_DLLS = ["concrt140", "msvcp140", "msvcp140_1", "msvcp140_2", "msvcp140_atomic_wait",
           "msvcp140_codecvt_ids", "vcamp140", "vccorlib140", "vcomp140", "vcruntime140",
           "vcruntime140_1"]
ENV_FILE = C.DATA_DIR / "launcher.env"     # sourced by the launcher; user-editable
DEFAULTS = {"dxvk": False, "d2d1_off": True, "vcrun": False, "gamemode": False, "asio": ""}

# Fonts that contain the sharp/flat signs FL Studio draws with "MS Gothic".
SYMBOL_FONTS = ["Noto Sans CJK JP", "Noto Sans JP", "Source Han Sans JP", "DejaVu Sans",
                "Noto Sans Symbols2", "Noto Sans"]


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ("TCL_LIBRARY", "TK_LIBRARY", "TKPATH", "SSL_CERT_FILE",
                         "PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH")}


class Wine:
    """The installed Wine runtime + prefix, as recorded by Setup."""

    def __init__(self, state=None, log=print):
        self.state = state or C.load_state() or {}
        self.prefix = Path(self.state.get("prefix") or C.DEFAULT_PREFIX)
        self.wine = Path(self.state.get("wine") or "wine")
        self.log = log

    @property
    def ok(self):
        return (self.prefix / "system.reg").exists() and self.wine.exists()

    @property
    def libdir(self):  # .../lib/wine of the bundled runtime
        return self.wine.parent.parent / "lib" / "wine"

    def env(self):
        env = clean_env()
        tools = Path(__file__).parent / "bin"
        env.update(WINEPREFIX=str(self.prefix), WINEDEBUG="-all", WINEARCH="win64",
                   WINEDLLOVERRIDES="winemenubuilder.exe=d;mscoree=d;mshtml=d",
                   WINE=str(self.wine), WINESERVER=str(self.wine.parent / "wineserver"),
                   PATH=os.pathsep.join([str(tools), str(self.wine.parent), env.get("PATH", "")]))
        return env

    def run(self, *args, timeout=600, cwd=None):
        self.log("$ wine " + " ".join(str(a) for a in args))
        r = subprocess.run([str(self.wine), *map(str, args)], env=self.env(), cwd=cwd,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           errors="replace", timeout=timeout)
        return r.returncode

    def wineserver(self, *args, timeout=60):
        ws = self.wine.parent / "wineserver"
        try:
            subprocess.run([str(ws) if ws.exists() else "wineserver", *args], env=self.env(),
                           timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            subprocess.run([str(ws) if ws.exists() else "wineserver", "-k"], env=self.env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close_all(self):
        self.wineserver("-k")

    def regedit(self, content: str):
        """Import a REGEDIT4 snippet into the prefix."""
        f = self.prefix / "drive_c" / "fl-tweak.reg"
        f.write_text("REGEDIT4\n\n" + content)
        try:
            self.run("regedit", "/S", "C:\\fl-tweak.reg", timeout=180)
            self.wineserver("-w", timeout=30)
        finally:
            f.unlink(missing_ok=True)

    def sysdir(self, arch):
        return self.prefix / "drive_c" / "windows" / ("system32" if arch == "x64" else "syswow64")


# --------------------------------------------------------------------- state

def get_tweaks(state=None):
    st = state or C.load_state() or {}
    t = dict(DEFAULTS)
    t.update(st.get("tweaks", {}))
    return t


def save_tweaks(t):
    st = C.load_state() or {}
    st["tweaks"] = t
    C.save_state(st)


def download(url, dest: Path, log, min_size=1024):
    from .engine import ssl_context, UA  # reuse the robust TLS setup
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size >= min_size:
        return dest
    log(f"Downloading {dest.name}…")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 17)
    if tmp.stat().st_size < min_size:
        raise RuntimeError(f"Download of {dest.name} failed")
    tmp.rename(dest)
    return dest


def _overrides(dlls, value):
    lines = "".join(f'"{d}"={value}\n' for d in dlls)
    return f"[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]\n{lines}\n"


# ------------------------------------------------------------------- graphics

def set_d2d1(w: Wine, off: bool):
    """Direct2D off = FL's interface draws reliably (Lutris default); on = plugins that
    need Direct2D can load."""
    w.regedit(_overrides(["d2d1"], '""' if off else "-"))
    w.log("Direct2D is now " + ("off" if off else "on (Wine's built-in)"))


def install_dxvk(w: Wine):
    arc = download(DXVK_URL, C.CACHE_DIR / Path(DXVK_URL).name, w.log, 1024 * 1024)
    backup = C.DATA_DIR / "dxvk-backup"
    tmp = Path(tempfile.mkdtemp(prefix="dxvk-"))
    try:
        with tarfile.open(arc) as tf:
            tf.extractall(tmp, filter="data")
        root = next(tmp.iterdir())
        for arch, sub in (("x64", "x64"), ("x86", "x32")):
            dst = w.sysdir(arch)
            if not dst.is_dir():
                continue
            for dll in DXVK_DLLS:
                src = root / sub / f"{dll}.dll"
                if not src.exists():
                    continue
                orig = dst / f"{dll}.dll"
                bk = backup / arch / f"{dll}.dll"
                if orig.exists() and not bk.exists():
                    bk.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(orig, bk)
                shutil.copy2(src, orig)
        w.regedit(_overrides(DXVK_DLLS, '"native,builtin"'))
        w.log(f"DXVK {DXVK_VERSION} installed (Direct3D now runs on Vulkan).")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remove_dxvk(w: Wine):
    backup = C.DATA_DIR / "dxvk-backup"
    for arch in ("x64", "x86"):
        for dll in DXVK_DLLS:
            bk = backup / arch / f"{dll}.dll"
            if bk.exists():
                shutil.copy2(bk, w.sysdir(arch) / f"{dll}.dll")
    w.regedit(_overrides(DXVK_DLLS, "-"))
    w.run("wineboot", "-u", timeout=600)   # puts back any missing built-in DLLs
    w.wineserver("-w", timeout=60)
    shutil.rmtree(backup, ignore_errors=True)
    w.log("DXVK removed; Wine's built-in Direct3D is back.")


# ------------------------------------------------------------------- runtimes

def install_vcrun(w: Wine):
    for arch, url in VCREDIST.items():
        exe = download(url, C.CACHE_DIR / f"vc_redist.{arch}.exe", w.log, 1024 * 1024)
        w.log(f"Installing Microsoft Visual C++ runtime ({arch})…")
        rc = w.run(exe, "/install", "/quiet", "/norestart", timeout=1200)
        w.wineserver("-w", timeout=120)
        if rc not in (0, 1638, 3010):  # ok / newer already installed / reboot needed
            w.log(f"vc_redist {arch} returned {rc}")
    w.regedit(_overrides(VC_DLLS, '"native,builtin"'))
    w.log("Visual C++ runtimes installed.")


# ---------------------------------------------------------------------- fonts

def fonts_installed(w: Wine):
    return (w.prefix / "drive_c" / "windows" / "Fonts" / "arial.ttf").exists()


def gdiplus_installed(w: Wine):
    st = get_tweaks()
    return bool(st.get("gdiplus"))


def winetricks(w: Wine, verb, timeout=1800):
    """Run one winetricks verb (each on its own, so one failure can't block the others)."""
    wt = download(C.WINETRICKS_URL, C.CACHE_DIR / "winetricks", w.log, 10 * 1024)
    wt.chmod(0o755)
    r = subprocess.run(["bash", str(wt), "-q", "--unattended", verb], env=w.env(),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                       timeout=timeout)
    with open(C.LOG_FILE, "a") as f:
        f.write(f"--- winetricks {verb} ---\n" + r.stdout[-20000:])
    w.wineserver("-w", timeout=60)
    if r.returncode != 0:
        lines = [ln for ln in r.stdout.strip().splitlines() if "fixme" not in ln and "warning" not in ln.lower()]
        raise RuntimeError(f"winetricks {verb} failed (code {r.returncode}):\n" + "\n".join(lines[-3:]))


def install_fonts(w: Wine):
    """Microsoft core fonts (Arial, Verdana, …, about 10 MB) via winetricks."""
    w.log("Installing Windows core fonts…")
    winetricks(w, "corefonts")
    w.log("Windows core fonts installed.")


def install_gdiplus(w: Wine):
    """Microsoft's native GDI+. winetricks extracts it from a large Windows update download."""
    w.log("Installing Microsoft GDI+ (large download, this can take a while)…")
    winetricks(w, "gdiplus", timeout=3600)
    t = get_tweaks()
    t["gdiplus"] = True
    save_tweaks(t)
    w.log("Microsoft GDI+ installed.")


def symbol_font():
    if not shutil.which("fc-list"):
        return None
    try:
        out = subprocess.run(["fc-list", ":charset=266d 266f", "family"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fams = {f.strip() for line in out.splitlines() for f in line.split(",")}
    for want in SYMBOL_FONTS:
        if want in fams:
            return want
    return sorted(fams)[0] if fams else None


def font_reg():
    """Registry text mapping MS Gothic (used for ♯/♭ note names) to a Linux font."""
    f = symbol_font()
    if not f:
        return ""
    lines = "".join(f'"{n}"="{f}"\n' for n in ("MS Gothic", "MS PGothic", "MS UI Gothic"))
    return f"[HKEY_CURRENT_USER\\Software\\Wine\\Fonts\\Replacements]\n{lines}\n"


# ----------------------------------------------------------------- launcher env

def write_launcher_env(t):
    lines = ["# Extra settings for the FL Studio launcher (written by FL Studio Setup).",
             "# You can add your own 'export NAME=value' lines below."]
    if t.get("gamemode") and shutil.which("gamemoderun"):
        lines.append("FL_WRAPPER=gamemoderun")
    keep = []
    if ENV_FILE.exists():  # keep the user's own lines
        mark = False
        for ln in ENV_FILE.read_text().splitlines():
            if ln.startswith("# --- your settings ---"):
                mark = True
                continue
            if mark:
                keep.append(ln)
    lines += ["# --- your settings ---"] + keep
    C.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(lines) + "\n")


# ------------------------------------------------------------------- ASIO drivers

ASIO_DRIVERS = {"PipeASIO": "pipeasio64", "WineASIO": "wineasio64"}
HOST_WINE_LIBS = ["/usr/lib/wine", "/usr/lib64/wine", "/usr/lib/x86_64-linux-gnu/wine",
                  "/usr/local/lib/wine", str(C.HOME / ".local/lib/wine")]


def find_asio_driver(name):
    """Locate an ASIO driver installed on the host (e.g. `paru -S pipeasio`)."""
    stem = ASIO_DRIVERS[name]
    for base in HOST_WINE_LIBS + [str(p) for p in Path("/opt").glob("wine*/lib/wine")]:
        b = Path(base)
        pe = b / "x86_64-windows" / f"{stem}.dll"
        if pe.exists():
            unix = next((u for u in (b / "x86_64-unix" / f"{stem}.so",
                                     b / "x86_64-unix" / f"{stem}.dll.so") if u.exists()), None)
            return pe, unix
    return None


def registered_asio(w: Wine):
    try:
        txt = (w.prefix / "system.reg").read_text(errors="replace")
    except OSError:
        return []
    names = []
    for ln in txt.splitlines():
        if ln.startswith("[Software\\\\ASIO\\\\"):
            names.append(ln.split("\\\\")[2].split("]")[0])
    return sorted(set(names))


def install_asio(w: Wine, name):
    found = find_asio_driver(name)
    if not found:
        raise RuntimeError(f"{name} is not installed on this computer.")
    pe, unix = found
    for src, sub in ((pe, "x86_64-windows"), (unix, "x86_64-unix")):
        if src:
            dst = w.libdir / sub / src.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    shutil.copy2(pe, w.sysdir("x64") / pe.name)
    rc = w.run("regsvr32", "/s", pe.name, timeout=120)
    w.wineserver("-w", timeout=30)
    if name not in " ".join(registered_asio(w)) and rc != 0:
        raise RuntimeError(f"{name} could not be registered (regsvr32 returned {rc}). "
                           "It may have been built for a different Wine version.")
    w.log(f"{name} registered. In FL Studio choose it under Options > Audio settings > Device.")


# ----------------------------------------------------------------- host system

def system_status():
    """What the computer itself provides (needs root to change)."""
    ntsync_avail = Path("/dev/ntsync").exists() or _module_exists("ntsync")
    return {
        "midi": Path("/dev/snd/seq").exists(),
        "ntsync": Path("/dev/ntsync").exists() and os.access("/dev/ntsync", os.R_OK),
        "ntsync_possible": ntsync_avail,
    }


def _module_exists(mod):
    if Path(f"/sys/module/{mod}").exists():
        return True
    if shutil.which("modinfo"):
        try:
            return subprocess.run(["modinfo", mod], capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def system_fix_script():
    st = system_status()
    mods = []
    if not st["midi"]:
        mods += ["snd-seq", "snd-seq-dummy"]
    if st["ntsync_possible"] and not st["ntsync"]:
        mods.append("ntsync")
    if not mods:
        return None
    lines = ["set -e"]
    lines += [f"modprobe {m} || true" for m in mods]
    lines.append("printf '%s\\n' " + " ".join(mods) + " > /etc/modules-load.d/fl-studio-linux.conf")
    if "ntsync" in mods:
        # Wine runs as your user, so it must be able to open /dev/ntsync.
        lines.append("if [ -e /dev/ntsync ] && [ \"$(stat -c %a /dev/ntsync)\" != 666 ]; then "
                     "printf 'KERNEL==\"ntsync\", MODE=\"0666\"\\n' > /etc/udev/rules.d/60-fl-studio-ntsync.rules; "
                     "chmod 0666 /dev/ntsync; fi")
    return "\n".join(lines)


def run_system_fix():
    """Ask for the admin password (polkit) and enable MIDI + ntsync. Returns (ok, message)."""
    script = system_fix_script()
    if not script:
        return True, "Nothing to fix."
    if not shutil.which("pkexec"):
        return False, ("Run this in a terminal:\n\nsudo sh -c '" + script.replace("'", "'\\''") + "'")
    r = subprocess.run(["pkexec", "sh", "-c", script], env=clean_env(),
                       capture_output=True, text=True)
    time.sleep(1)
    if r.returncode != 0:
        return False, ("The change was cancelled or failed.\n" + (r.stderr or "").strip())
    return True, "Done. Restart FL Studio for it to take effect."
