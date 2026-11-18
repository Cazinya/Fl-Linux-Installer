"""The installation engine. Runs in a worker thread and reports through `emit`."""
import ctypes.util
import math
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C
from . import integration, peicon

ASSETS = Path(__file__).parent / "assets"
TOOLS_DIR = Path(__file__).parent / "bin"   # bundled cabextract for winetricks
UA = f"Mozilla/5.0 (X11; Linux x86_64) FLStudioSetupForLinux/{C.APP_VERSION}"


class Cancelled(Exception):
    pass


class SetupError(Exception):
    pass


@dataclass
class Options:
    mode: str = "install"                  # install | update | repair
    prefix: Path = C.DEFAULT_PREFIX
    installer_path: Path | None = None     # use a local FL Studio installer
    express: bool = False                  # silent FL Studio install (FL's installer may refuse /S)
    system_wine: bool = False
    compat: bool = True
    menu: bool = True
    desktop_shortcut: bool = True
    flp_assoc: bool = True
    dpi: int = 96
    keep_installer: bool = False
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------- helpers

def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


_SSL_CTX = None


def ssl_context():
    """Bundled CA certificates plus the system's own (covers corporate/proxy CAs)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        import ssl
        ctx = ssl.create_default_context()
        for f in (os.environ.get("SSL_CERT_FILE"), "/etc/ssl/certs/ca-certificates.crt",
                  "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
                  "/etc/ssl/cert.pem", os.environ.get("FLSETUP_EXTRA_CA")):
            if f and os.path.isfile(f):
                try:
                    ctx.load_verify_locations(cafile=f)
                except (ssl.SSLError, OSError):
                    pass
        _SSL_CTX = ctx
    return _SSL_CTX


def os_family():
    ids = ""
    try:
        for ln in Path("/etc/os-release").read_text().splitlines():
            if ln.startswith(("ID=", "ID_LIKE=")):
                ids += " " + ln.split("=", 1)[1].strip('"')
    except OSError:
        pass
    for fam, keys in (("apt", ("debian", "ubuntu")), ("dnf", ("fedora", "rhel", "centos")),
                      ("pacman", ("arch",)), ("zypper", ("suse",))):
        if any(k in ids for k in keys):
            return fam
    return None


HOST_LIBS = {  # soname stem -> (description, {family: package})
    "freetype": ("fonts", dict(apt="libfreetype6", dnf="freetype", pacman="freetype2", zypper="libfreetype6")),
    "fontconfig": ("fonts", dict(apt="libfontconfig1", dnf="fontconfig", pacman="fontconfig", zypper="fontconfig")),
    "X11": ("graphics", dict(apt="libx11-6", dnf="libX11", pacman="libx11", zypper="libX11-6")),
    "GL": ("graphics", dict(apt="libgl1", dnf="mesa-libGL", pacman="mesa", zypper="Mesa-libGL1")),
    "gnutls": ("sign-in / online features", dict(apt="libgnutls30", dnf="gnutls", pacman="gnutls", zypper="libgnutls30")),
    "asound": ("audio", dict(apt="libasound2", dnf="alsa-lib", pacman="alsa-lib", zypper="libasound2")),
    "pulse": ("audio", dict(apt="libpulse0", dnf="pulseaudio-libs", pacman="libpulse", zypper="libpulse0")),
}


def missing_host_libs():
    return [k for k in HOST_LIBS if not ctypes.util.find_library(k)]


def install_hint(libs):
    fam = os_family()
    if not libs:
        return ""
    if not fam:
        return "Install these libraries with your package manager: " + ", ".join(libs)
    pkgs = " ".join(HOST_LIBS[l][1][fam] for l in libs)
    cmd = {"apt": "sudo apt install", "dnf": "sudo dnf install",
           "pacman": "sudo pacman -S", "zypper": "sudo zypper install"}[fam]
    return f"{cmd} {pkgs}"


def find_system_wine():
    w = shutil.which("wine")
    return Path(w) if w else None


def find_fl_exe(prefix: Path):
    base = prefix / "drive_c" / "Program Files" / "Image-Line"
    hits = list(base.glob("*/FL64.exe")) if base.is_dir() else []
    if not hits:
        hits = list((prefix / "drive_c").glob("**/FL64.exe"))
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def dir_size(p: Path):
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------- engine

class Engine:
    STEPS_INSTALL = [
        ("check", "Checking your system", 2),
        ("wine", "Downloading the Windows compatibility layer (Wine)", 10),
        ("wine_x", "Unpacking Wine", 4),
        ("fl_dl", "Downloading FL Studio", 32),
        ("prefix", "Preparing the Windows environment", 8),
        ("config", "Configuring Windows settings", 2),
        ("compat", "Installing Windows core fonts", 8),
        ("fl_inst", "Installing FL Studio", 30),
        ("integrate", "Creating shortcuts and file associations", 3),
        ("cleanup", "Cleaning up", 1),
    ]

    def __init__(self, opts: Options, emit):
        self.o = opts
        self.emit = emit
        self.cancelled = threading.Event()
        self.proc = None
        self.wine = None
        self.warnings = []
        self.created = []
        self.fl_exe = None
        C.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.logf = open(C.LOG_FILE, "a", buffering=1)
        steps = self.STEPS_INSTALL
        if opts.installer_path:
            steps = [s for s in steps if s[0] != "fl_dl"]
        if opts.system_wine:
            steps = [s for s in steps if s[0] not in ("wine", "wine_x")]
        if not opts.compat:
            steps = [s for s in steps if s[0] != "compat"]
        if opts.mode == "update":
            steps = [s for s in steps if s[0] not in ("prefix", "compat")]
        self.steps = steps
        self.total_w = sum(w for *_, w in steps)
        self.done_w = 0
        self.cur_w = 0
        self._frac = 0.0
        self.fl_running = False

    # -- reporting
    def log(self, msg):
        line = time.strftime("[%H:%M:%S] ") + msg
        self.logf.write(line + "\n")
        self.emit("log", text=msg)

    def sub(self, frac, detail=None):
        frac = max(0.0, min(1.0, frac))
        self._frac = frac
        self.emit("progress", value=(self.done_w + self.cur_w * frac) / self.total_w, detail=detail)

    def check_cancel(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def cancel(self):
        self.cancelled.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        if self.wine:
            self.kill_wine()

    # -- main
    def run(self):
        try:
            if (self.o.prefix / "system.reg").exists():
                # Like a Windows installer closing the app first: FL Studio (or a stale
                # wineserver) must not be running while we modify its environment.
                self.ensure_wine()
                self.log("Closing FL Studio and other programs in its environment…")
                self.kill_wine()
            for i, (key, label, w) in enumerate(self.steps):
                self.check_cancel()
                self.cur_w = w
                self.emit("step", index=i, count=len(self.steps), label=label)
                self.log(f"== {label}")
                self.sub(0, label + "…")
                getattr(self, "step_" + key)()
                self.done_w += w
                self.sub(0)
            self.emit("progress", value=1.0, detail="Completed")
            self.emit("done", ok=True, warnings=self.warnings, fl_exe=str(self.fl_exe or ""),
                      fl_running=self.fl_running)
        except Cancelled:
            self.log("Setup was cancelled by the user.")
            self.emit("done", ok=False, cancelled=True)
        except Exception as e:  # noqa: BLE001 - show any failure to the user
            import traceback
            self.logf.write(traceback.format_exc())
            self.log(f"ERROR: {e}")
            self.emit("done", ok=False, error=str(e))
        finally:
            self.logf.close()

    # -- environment for wine
    def env(self, setup=True):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TCL_LIBRARY", "TK_LIBRARY", "TKPATH", "SSL_CERT_FILE",
                            "PYTHONHOME", "PYTHONPATH", "LD_LIBRARY_PATH")}
        env["WINEPREFIX"] = str(self.o.prefix)
        env["WINEARCH"] = "win64"
        env["WINEDEBUG"] = "-all"
        ov = "winemenubuilder.exe=d"
        if setup:
            ov += ";mscoree=d;mshtml=d"  # don't prompt for Mono/Gecko
        env["WINEDLLOVERRIDES"] = ov
        # Bundled helper tools (cabextract) come first, then Wine.
        env["PATH"] = os.pathsep.join([str(TOOLS_DIR), str(self.wine.parent), env.get("PATH", "")])
        env["WINE"] = str(self.wine)
        env["WINESERVER"] = str(self.wine.parent / "wineserver")
        return env

    def wineserver(self):
        ws = self.wine.parent / "wineserver"
        return str(ws) if ws.exists() else "wineserver"

    def kill_wine(self):
        try:
            subprocess.run([self.wineserver(), "-k"], env=self.env(), timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass

    def run_cmd(self, cmd, timeout=None, tick=None, cwd=None, env=None):
        self.log("$ " + " ".join(str(c) for c in cmd))
        self.proc = subprocess.Popen([str(c) for c in cmd], env=env or self.env(), cwd=cwd,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, errors="replace")
        out_lines = []

        def reader():
            for ln in self.proc.stdout:
                ln = ln.rstrip()
                if ln:
                    out_lines.append(ln)
                    self.logf.write("   " + ln + "\n")
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        start = time.time()
        while self.proc.poll() is None:
            if self.cancelled.is_set():
                self.proc.terminate()
                raise Cancelled()
            if timeout and time.time() - start > timeout:
                self.proc.kill()
                raise SetupError(f"'{Path(str(cmd[0])).name}' did not finish in time")
            if tick:
                tick(time.time() - start)
            time.sleep(0.25)
        t.join(2)
        return self.proc.returncode, out_lines

    # Wine's own background services; they exit by themselves once real programs are gone.
    SYSTEM_EXES = {"services.exe", "winedevice.exe", "plugplay.exe", "svchost.exe", "rpcss.exe",
                   "explorer.exe", "conhost.exe", "start.exe", "lsass.exe", "wineboot.exe",
                   "rundll32.exe", "tabtip.exe", "mscorsvw.exe", "winemenubuilder.exe"}
    # Programs an installer may leave open that are safe to close straight away.
    CLOSE_NOW = {"winedbg.exe"}
    # FL Studio itself, e.g. started from the installer's "Launch FL Studio" box: leave it open.
    APP_EXES = {"fl64.exe", "fl.exe", "ilbridge.exe"}

    def wine_processes(self):
        """Names of Windows programs currently running in our prefix (read from /proc)."""
        key = b"\0WINEPREFIX=" + str(self.o.prefix).encode() + b"\0"
        names = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                if key not in b"\0" + (d / "environ").read_bytes() + b"\0":
                    continue
                argv0 = (d / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
            except OSError:
                continue
            name = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if name.endswith((".exe", ".com")):
                names.append(name)
        return names

    def wait_wineserver(self, max_wait=90):
        """Wait for programs in the prefix to finish, but never hang: anything still
        running after `max_wait` seconds (or FL Studio itself, if an installer started
        it) is closed, just like a Windows installer closing apps when it's done."""
        start = time.time()
        while True:
            self.check_cancel()
            user = sorted({p for p in self.wine_processes() if p not in self.SYSTEM_EXES})
            if any(p in self.APP_EXES for p in user):
                self.log("FL Studio is running (started by its installer); not waiting for it.")
                self.fl_running = True
                return
            if any(p in self.CLOSE_NOW for p in user):
                self.log("Closing " + ", ".join(user) + " (started by the installer)")
                break
            if not user:
                try:  # only Wine's services are left; they normally stop within seconds
                    self.run_cmd([self.wineserver(), "-w"], timeout=20)
                    return
                except SetupError:
                    self.log("Wine services did not stop by themselves; stopping them.")
                    break
            waited = time.time() - start
            if waited > max_wait:
                self.log(f"Still running after {int(waited)}s: {', '.join(user)}. Closing them.")
                break
            self.sub(self._frac, f"Waiting for {', '.join(user)} to finish… ({int(max_wait - waited)}s)")
            time.sleep(1)
        self.kill_wine()

    # -- downloading
    def download(self, url, dest: Path, label, min_size=0):
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        if dest.exists() and dest.stat().st_size > min_size:
            self.log(f"Using cached {dest.name}")
            return dest
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        if have:
            req.add_header("Range", f"bytes={have}-")
        last_err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as r:
                    if have and r.status != 206:
                        have = 0  # server ignored the range request
                    total = r.length + have if r.length else 0
                    mode = "ab" if have else "wb"
                    got, t0, g0 = have, time.time(), have
                    with open(part, mode) as f:
                        while True:
                            self.check_cancel()
                            chunk = r.read(1 << 17)
                            if not chunk:
                                break
                            f.write(chunk)
                            got += len(chunk)
                            dt = time.time() - t0
                            speed = (got - g0) / dt if dt > 0.5 else 0
                            detail = f"{label}: {human(got)}" + (f" of {human(total)}" if total else "")
                            if speed:
                                detail += f"  ({human(speed)}/s"
                                if total:
                                    detail += f", {int((total - got) / speed // 60)} min {int((total - got) / speed % 60)} s left"
                                detail += ")"
                            self.sub(got / total if total else 0.5, detail)
                if part.stat().st_size < min_size:
                    raise SetupError(f"The download of {label} is incomplete or invalid.")
                part.rename(dest)
                self.log(f"Downloaded {dest.name} ({human(dest.stat().st_size)})")
                return dest
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.log(f"Download problem ({e}); retrying…")
                have = part.stat().st_size if part.exists() else 0
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                if have:
                    req.add_header("Range", f"bytes={have}-")
                time.sleep(2 + attempt * 3)
        raise SetupError(f"Could not download {label}. Check your internet connection.\n({last_err})")

    # ------------------------------------------------------------------ steps
    def step_check(self):
        o = self.o
        target = o.prefix
        probe = target
        while not probe.exists():
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        need = 6 * 1024**3 if o.mode == "install" else 2 * 1024**3
        self.log(f"Free space at {probe}: {human(free)}")
        if free < need:
            raise SetupError(f"Not enough disk space. Setup needs about {human(need)} free "
                             f"but only {human(free)} is available at {probe}.")
        if o.installer_path:
            p = Path(o.installer_path)
            with open(p, "rb") as f:
                if f.read(2) != b"MZ":
                    raise SetupError(f"{p.name} is not a Windows installer.")
        if o.system_wine:
            w = find_system_wine()
            if not w:
                raise SetupError("System Wine was selected but 'wine' was not found.")
            self.wine = w
        missing = missing_host_libs()
        if missing:
            descr = sorted({HOST_LIBS[m][0] for m in missing})
            msg = (f"Some system libraries are missing ({', '.join(missing)}); "
                   f"{', '.join(descr)} may not work. To fix: {install_hint(missing)}")
            self.log("WARNING: " + msg)
            self.warnings.append(msg)
        self.sub(1)

    def wine_archive(self):
        return C.CACHE_DIR / Path(C.WINE_URL).name

    def step_wine(self):
        target = C.WINE_DIR / f"{C.WINE_VERSION}-{C.WINE_FLAVOR}"
        if (target / "bin" / "wine").exists():
            self.log("Wine runtime already installed.")
            return
        self.download(C.WINE_URL, self.wine_archive(), f"Wine {C.WINE_VERSION}", 20 * 1024**2)

    def step_wine_x(self):
        target = C.WINE_DIR / f"{C.WINE_VERSION}-{C.WINE_FLAVOR}"
        if not (target / "bin" / "wine").exists():
            arc = self.wine_archive()
            tmp = Path(tempfile.mkdtemp(prefix=".unpack-", dir=C.DATA_DIR))
            try:
                with tarfile.open(arc, "r:*") as tf:
                    members = tf.getmembers()
                    for i, m in enumerate(members):
                        self.check_cancel()
                        tf.extract(m, tmp, filter="data")
                        if i % 50 == 0:
                            self.sub(i / len(members), f"Unpacking Wine… {i * 100 // len(members)}%")
                tops = [p for p in tmp.iterdir() if p.is_dir()]
                if len(tops) != 1 or not (tops[0] / "bin" / "wine").exists():
                    raise SetupError("The Wine download looks damaged. Please run Setup again.")
                if target.exists():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                tops[0].rename(target)
            except tarfile.TarError as e:
                arc.unlink(missing_ok=True)
                raise SetupError(f"The Wine download is damaged ({e}). Please run Setup again.")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            arc.unlink(missing_ok=True)
        self.wine = target / "bin" / "wine"
        rc, out = self.run_cmd([self.wine, "--version"], timeout=60)
        if rc != 0:
            raise SetupError("Wine could not start on this system:\n" + "\n".join(out[-5:]) +
                             "\n\n" + install_hint(missing_host_libs()))
        self.log("Wine version: " + (out[-1] if out else "?"))

    def ensure_wine(self):
        if self.wine:
            return
        if self.o.system_wine:
            self.wine = find_system_wine()
        else:
            self.wine = C.WINE_DIR / f"{C.WINE_VERSION}-{C.WINE_FLAVOR}" / "bin" / "wine"

    def fl_installer(self):
        if self.o.installer_path:
            return Path(self.o.installer_path)
        return C.CACHE_DIR / "flstudio_win64_installer.exe"

    def step_fl_dl(self):
        dest = self.fl_installer()
        if self.o.mode in ("update", "repair") and dest.exists():
            dest.unlink()  # always fetch the latest version
        self.download(C.FL_INSTALLER_URL, dest, "FL Studio", 100 * 1024**2)
        with open(dest, "rb") as f:
            if f.read(2) != b"MZ":
                dest.unlink()
                raise SetupError("Image-Line's server returned something that is not an installer. "
                                 "Try again later, or download the installer from image-line.com and "
                                 "choose it under 'Use an installer I already downloaded'.")

    def step_prefix(self):
        self.ensure_wine()
        self.o.prefix.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        self.run_cmd([self.wine, "wineboot", "-u"], timeout=900,
                     tick=lambda s: self.sub(1 - math.exp(-s / 25), "Creating the Windows environment…"))
        self.wait_wineserver()
        if not (self.o.prefix / "drive_c" / "windows").is_dir():
            raise SetupError("Wine could not create the Windows environment. See the log for details.")
        self.log(f"Prefix ready in {time.time() - t0:.0f}s")

    def restore_builtin_powershell(self):
        """Undo the PowerShell 7 wrapper installed by v1.0.0: under Wine, real PowerShell
        crashes (0xe0434352) when FL Studio's installer calls it. Wine's own built-in
        powershell.exe is a harmless stand-in that just returns success."""
        lib = self.wine.parent.parent / "lib" / "wine"
        win = self.o.prefix / "drive_c" / "windows"
        for sysdir, arch in (("system32", "x86_64-windows"), ("syswow64", "i386-windows")):
            src = lib / arch / "powershell.exe"
            for dst in (win / sysdir / "WindowsPowerShell" / "v1.0" / "powershell.exe",
                        win / sysdir / "powershell.exe"):
                try:
                    if not dst.exists():
                        continue
                    with open(dst, "rb") as f:
                        if b"Wine builtin" in f.read(4096):
                            continue
                    if src.exists():
                        shutil.copyfile(src, dst)
                        self.log(f"Restored Wine's built-in {sysdir}\\powershell.exe")
                    else:
                        dst.unlink()
                        self.log(f"Removed {dst}")
                except OSError as e:
                    self.log(f"Could not restore {dst}: {e}")
        pwsh = self.o.prefix / "drive_c" / "Program Files" / "Powershell"
        if pwsh.is_dir():
            shutil.rmtree(pwsh, ignore_errors=True)
            self.log("Removed PowerShell 7 (it crashes under Wine)")

    def step_config(self):
        self.ensure_wine()
        self.restore_builtin_powershell()
        reg = (
            "REGEDIT4\n\n"
            "[HKEY_CURRENT_USER\\Software\\Wine]\n\"Version\"=\"win10\"\n\n"
            "[HKEY_CURRENT_USER\\Software\\Wine\\DllOverrides]\n"
            "\"d2d1\"=\"\"\n\"winemenubuilder.exe\"=\"\"\n\n"
            "[HKEY_CURRENT_USER\\Control Panel\\Desktop]\n"
            f"\"LogPixels\"=dword:{self.o.dpi:08x}\n\n"
            "[HKEY_CURRENT_USER\\Software\\Wine\\WineDbg]\n\"ShowCrashDialog\"=dword:00000000\n\n"
            "[HKEY_CURRENT_USER\\Software\\Wine\\X11 Driver]\n\"Decorated\"=\"Y\"\n"
            "\"UseTakeFocus\"=\"N\"\n"
        )
        from . import tweaks
        t = tweaks.get_tweaks()
        if not t.get("d2d1_off", True):
            reg = reg.replace('"d2d1"=""\n', '"d2d1"=-\n')
        reg += "\n" + tweaks.font_reg()
        f = self.o.prefix / "drive_c" / "fl-setup.reg"
        f.write_text(reg)
        self.run_cmd([self.wine, "regedit", "/S", "C:\\fl-setup.reg"], timeout=180)
        self.wait_wineserver()
        f.unlink(missing_ok=True)
        self.sub(1)

    def step_compat(self):
        self.ensure_wine()
        # Microsoft core fonts via winetricks (uses our bundled cabextract). Small and safe.
        # (Native GDI+ is a ~1 GB Windows-update download, so it's optional in Settings.)
        from . import tweaks
        w = tweaks.Wine({"prefix": str(self.o.prefix), "wine": str(self.wine)}, log=self.log)
        if not shutil.which("cabextract", path=w.env()["PATH"]):
            self.warnings.append("Skipped Windows core fonts: 'cabextract' is missing.")
            return
        stop = threading.Event()

        def ticker():
            t0 = time.time()
            while not stop.wait(0.5):
                self.sub(1 - math.exp(-(time.time() - t0) / 40), "Installing Windows core fonts…")
        th = threading.Thread(target=ticker, daemon=True)
        th.start()
        try:
            tweaks.install_fonts(w)
        except Exception as e:  # noqa: BLE001
            self.warnings.append(f"Windows core fonts were not installed ({e}). FL Studio works "
                                 "without them; you can retry in FL Studio Setup > Settings.")
            self.log("WARNING: " + self.warnings[-1])
        finally:
            stop.set()

    def step_fl_inst(self):
        self.ensure_wine()
        exe = self.fl_installer()
        pf = self.o.prefix / "drive_c" / "Program Files" / "Image-Line"
        before = dir_size(pf) if pf.exists() else 0
        state = {"last": 0.0, "size": before}

        def tick(s):
            if s - state["last"] >= 2:
                state["last"] = s
                state["size"] = dir_size(pf) if pf.exists() else 0
            grown = max(0, state["size"] - before) if self.o.mode == "install" else s * 8e6
            frac = 1 - math.exp(-grown / (1.6 * 1024**3))
            if self.o.express:
                self.sub(min(frac, 0.97), f"Copying FL Studio files… {human(state['size'])}")
            else:
                self.sub(min(frac, 0.97), "Follow the steps in the FL Studio installer window. "
                                          "Keep the default install location.")

        # Wine maps the Windows desktop to the real one; FL's installer would leave
        # useless .lnk files there. Remember what's there so we can tidy up afterwards.
        desk = C.desktop_dir()
        lnk_before = set(desk.glob("*.lnk")) if desk.is_dir() else set()
        try:
            self._install_fl(exe, tick)
        finally:
            if desk.is_dir():
                for f in set(desk.glob("*.lnk")) - lnk_before:
                    f.unlink(missing_ok=True)

    def _install_fl(self, exe, tick):
        args = [self.wine, exe] + (["/S"] if self.o.express else [])
        rc, _ = self.run_cmd(args, tick=tick, cwd=str(exe.parent))
        self.log(f"FL Studio installer exited with code {rc}")
        self.sub(0.98, "Finishing FL Studio installation…")
        self.wait_wineserver(120)
        self.fl_exe = find_fl_exe(self.o.prefix)
        if self.o.express and (rc != 0 or not self.fl_exe):
            # FL's installer refused the silent mode: show the real installer instead.
            self.log("Silent install did not work; opening the FL Studio installer.")
            self.o.express = False
            rc, _ = self.run_cmd([self.wine, exe], tick=tick, cwd=str(exe.parent))
            self.log(f"FL Studio installer exited with code {rc}")
            self.wait_wineserver(120)
            self.fl_exe = find_fl_exe(self.o.prefix)
        if not self.fl_exe:
            raise SetupError("FL Studio was not installed (FL64.exe not found). "
                             "If you cancelled the FL Studio installer, run Setup again.")
        self.log(f"FL Studio installed at {self.fl_exe}")

    def reapply_tweaks(self):
        """Repair/Update: keep the user's Settings choices (DXVK, runtimes, launcher env)."""
        from . import tweaks
        t = tweaks.get_tweaks()
        w = tweaks.Wine({"prefix": str(self.o.prefix), "wine": str(self.wine)}, log=self.log)
        tweaks.write_launcher_env(t)
        for key, fn in (("dxvk", tweaks.install_dxvk), ("vcrun", tweaks.install_vcrun)):
            if t.get(key) and self.o.mode != "install":
                try:
                    fn(w)
                except Exception as e:  # noqa: BLE001
                    self.warnings.append(f"Could not re-apply {key}: {e}")
        if t.get("asio") and self.o.mode != "install":
            try:
                tweaks.install_asio(w, t["asio"])
            except Exception as e:  # noqa: BLE001
                self.warnings.append(f"Could not re-enable {t['asio']}: {e}")

    def step_integrate(self):
        self.ensure_wine()
        self.reapply_tweaks()
        if not self.fl_exe:
            self.fl_exe = find_fl_exe(self.o.prefix)
        created = [integration.write_launcher(self.o.prefix, self.wine, self.fl_exe)]
        icon = None
        try:
            icon = peicon.extract_icon(self.fl_exe, C.DATA_DIR / "fl-icon")
        except Exception as e:  # noqa: BLE001
            self.log(f"Could not read the FL Studio icon ({e}); using the default icon.")
        created += integration.install_icons(icon, ASSETS / "icon.png")
        setup_copy = self.copy_setup()
        created += integration.install_menu_entries(self.o.menu, setup_copy)
        if self.o.desktop_shortcut:
            d = integration.install_desktop_shortcut()
            if d:
                created.append(d)
        if self.o.flp_assoc:
            created += integration.install_flp_association()
        self.created = created
        prev = C.load_state() or {}
        files = sorted(set(prev.get("files", [])) | {str(p) for p in created})
        C.save_state({
            "version": C.APP_VERSION, "prefix": str(self.o.prefix), "wine": str(self.wine),
            "system_wine": self.o.system_wine, "fl_exe": str(self.fl_exe), "files": files,
            "tweaks": prev.get("tweaks", {}),
            "options": {"menu": self.o.menu, "desktop_shortcut": self.o.desktop_shortcut,
                        "flp_assoc": self.o.flp_assoc, "compat": self.o.compat, "dpi": self.o.dpi},
            "installed": prev.get("installed") or time.strftime("%Y-%m-%d %H:%M"),
            "updated": time.strftime("%Y-%m-%d %H:%M"),
        })
        self.sub(1)

    def copy_setup(self):
        src = os.environ.get("APPIMAGE")
        if not src or not Path(src).is_file():
            return C.SETUP_COPY if C.SETUP_COPY.exists() else None
        try:
            if Path(src).resolve() != C.SETUP_COPY.resolve():
                tmp = C.SETUP_COPY.with_suffix(".tmp")
                shutil.copyfile(src, tmp)
                tmp.chmod(0o755)
                tmp.replace(C.SETUP_COPY)
            return C.SETUP_COPY
        except OSError as e:
            self.log(f"Could not keep a copy of Setup ({e})")
            return None

    def step_cleanup(self):
        if not self.o.keep_installer and not self.o.installer_path:
            self.fl_installer().unlink(missing_ok=True)
        (C.CACHE_DIR / "install_pwshwrapper.exe").unlink(missing_ok=True)  # from v1.0.0
        self.sub(1)


# ------------------------------------------------------------------- uninstall

def uninstall(delete_user_data: bool, log=print):
    state = C.load_state() or {}
    prefix = Path(state.get("prefix") or C.DEFAULT_PREFIX)
    wine = state.get("wine")
    if wine and Path(wine).exists():
        env = dict(os.environ, WINEPREFIX=str(prefix))
        ws = Path(wine).parent / "wineserver"
        try:
            subprocess.run([str(ws) if ws.exists() else "wineserver", "-k"], env=env, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    kept = None
    users = prefix / "drive_c" / "users"
    if not delete_user_data and users.is_dir():
        for u in users.iterdir():
            docs = u / "Documents" / "Image-Line"
            if docs.is_dir() and not (u / "Documents").is_symlink():
                kept = C.HOME / "Documents" / "Image-Line (saved by FL Studio Setup)"
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(docs), str(kept))
                log(f"Kept your projects in {kept}")
    if delete_user_data:
        shared = C.HOME / "Documents" / "Image-Line"
        if shared.is_dir():
            shutil.rmtree(shared, ignore_errors=True)
    log("Removing shortcuts and file associations…")
    integration.remove_files(state.get("files", []) + [str(C.LAUNCHER),
                             str(C.APPS_DIR / C.DESKTOP_ID), str(C.APPS_DIR / C.SETUP_DESKTOP_ID),
                             str(C.APPS_DIR / C.RUNEXE_DESKTOP_ID), str(C.desktop_dir() / "FL Studio.desktop")])
    log("Removing FL Studio and its Windows environment…")
    if (prefix / "drive_c").is_dir() and (prefix / "system.reg").exists():
        shutil.rmtree(prefix, ignore_errors=True)  # only ever delete a real Wine prefix
    shutil.rmtree(C.CACHE_DIR, ignore_errors=True)
    log("Removing Wine runtime…")
    shutil.rmtree(C.WINE_DIR, ignore_errors=True)
    shutil.rmtree(C.DATA_DIR / "dxvk-backup", ignore_errors=True)
    for p in (C.STATE_FILE, C.SETUP_COPY, C.DATA_DIR / "fl-icon.png", C.DATA_DIR / "fl-icon.ico",
              C.DATA_DIR / "launcher.env", C.APPS_DIR / "fl-studio-linux-files.desktop"):
        Path(p).unlink(missing_ok=True)
    return kept
