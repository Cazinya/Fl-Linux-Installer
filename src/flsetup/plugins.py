"""Plugin management: install VST2/VST3/CLAP plugins into FL Studio's Windows environment."""
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from . import config as C

INSTALLER_EXT = {".exe", ".msi"}
PLUGIN_EXT = {".vst3", ".dll", ".clap"}
ARCHIVE_EXT = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".xz", ".bz2"}


def prefix():
    st = C.load_state() or {}
    return Path(st.get("prefix") or C.DEFAULT_PREFIX)


def folders():
    """Standard Windows plugin folders that FL Studio scans by default."""
    pf = prefix() / "drive_c" / "Program Files"
    return {
        "VST3": pf / "Common Files" / "VST3",
        "VST2": pf / "VSTPlugins",
        "CLAP": pf / "Common Files" / "CLAP",
    }


def list_plugins():
    """Return [(name, kind, path)] for everything in the standard plugin folders."""
    out = []
    f = folders()
    if f["VST3"].is_dir():
        for p in f["VST3"].rglob("*.vst3"):
            if not any(par.suffix.lower() == ".vst3" for par in p.parents if par != f["VST3"]):
                out.append((p.stem, "VST3", p))
    if f["VST2"].is_dir():
        out += [(p.stem, "VST2", p) for p in f["VST2"].rglob("*") if p.suffix.lower() == ".dll" and p.is_file()]
    if f["CLAP"].is_dir():
        out += [(p.stem, "CLAP", p) for p in f["CLAP"].rglob("*.clap")]
    return sorted(out, key=lambda t: t[0].lower())


def kind_of(path: Path):
    s = path.suffix.lower()
    if s in INSTALLER_EXT:
        return "installer"
    if s in PLUGIN_EXT:
        return "plugin"
    if s in ARCHIVE_EXT or path.name.lower().endswith((".tar.gz", ".tar.xz", ".tar.bz2")):
        return "archive"
    return None


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in ("TCL_LIBRARY", "TK_LIBRARY", "TKPATH", "SSL_CERT_FILE", "PYTHONHOME", "PYTHONPATH")}


def run_installer(path: Path, log):
    """Run a Windows installer in FL Studio's environment and wait until it finishes."""
    log(f"Running installer {path.name} (follow its steps; keep the suggested folders)…")
    r = subprocess.run([str(C.LAUNCHER), "--run-exe", str(path)], env=clean_env(),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"{path.name} finished" + (f" (exit code {r.returncode})" if r.returncode else "."))
    return r.returncode == 0


def copy_plugin(path: Path, log):
    f = folders()
    s = path.suffix.lower()
    dest_dir = f["VST3"] if s == ".vst3" else f["CLAP"] if s == ".clap" else f["VST2"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        shutil.rmtree(dest) if dest.is_dir() and not dest.is_symlink() else dest.unlink()
    if path.is_dir():
        shutil.copytree(path, dest)
    else:
        shutil.copy2(path, dest)
    kind = {".vst3": "VST3", ".clap": "CLAP"}.get(s, "VST2")
    log(f"Installed {path.stem} ({kind})")
    return True


def _extract(path: Path, dest: Path, log):
    name = path.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for m in z.infolist():
                target = (dest / m.filename).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise ValueError("unsafe path in archive")
            z.extractall(dest)
        return
    if any(name.endswith(e) for e in (".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        with tarfile.open(path) as t:
            t.extractall(dest, filter="data")
        return
    for tool, args in (("7z", ["x", "-y", f"-o{dest}", str(path)]),
                       ("7za", ["x", "-y", f"-o{dest}", str(path)]),
                       ("unrar", ["x", "-o+", str(path), str(dest) + "/"])):
        if shutil.which(tool):
            subprocess.run([tool, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            return
    raise RuntimeError(f"Can't open {path.name}: install 'p7zip' (7z) to unpack this type of archive.")


def install(path: Path, log):
    """Install one file: an installer, a plugin file/bundle, or an archive containing them.
    Returns the number of things installed."""
    path = Path(path)
    k = kind_of(path) if not (path.is_dir() and path.suffix.lower() == ".vst3") else "plugin"
    if k == "installer":
        return int(run_installer(path, log))
    if k == "plugin":
        return int(copy_plugin(path, log))
    if k == "archive":
        tmp = Path(tempfile.mkdtemp(prefix="flplugin-"))
        try:
            log(f"Unpacking {path.name}…")
            _extract(path, tmp, log)
            # Prefer installers inside the archive; otherwise copy plugin files.
            installers = [p for p in tmp.rglob("*") if p.is_file() and p.suffix.lower() in INSTALLER_EXT]
            bundles = [p for p in tmp.rglob("*.vst3")
                       if not any(par.suffix.lower() == ".vst3" for par in p.parents if par != tmp)]
            others = [p for p in tmp.rglob("*") if p.is_file() and p.suffix.lower() in (".dll", ".clap")
                      and not any(par.suffix.lower() == ".vst3" for par in p.parents)]
            if installers:
                return sum(run_installer(p, log) for p in installers)
            if not bundles and not others:
                log(f"No plugins were found in {path.name}.")
                return 0
            n = 0
            for p in bundles + others:
                n += copy_plugin(p, log)
            return n
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    log(f"Skipped {path.name}: not a plugin, installer or archive.")
    return 0


def remove(path: Path):
    path = Path(path)
    # only ever delete inside the plugin folders
    if not any(str(path).startswith(str(f) + os.sep) for f in folders().values()):
        raise ValueError("not inside a plugin folder")
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
