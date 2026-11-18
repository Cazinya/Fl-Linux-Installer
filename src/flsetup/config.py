"""Constants and well-known paths for FL Studio Setup for Linux."""
import json
import os
from pathlib import Path

APP_NAME = "FL Studio Setup for Linux"
APP_VERSION = "1.1.1"

# Official Image-Line redirect to the latest Windows installer.
FL_INSTALLER_URL = os.environ.get(
    "FLSETUP_FL_URL", "https://support.image-line.com/redirect/flstudio_win_installer"
)
FL_EULA_URL = "https://www.image-line.com/eula/"

# Portable Wine runtime (Kron4ek builds). WoW64 build = no 32-bit host libraries needed.
WINE_VERSION = os.environ.get("FLSETUP_WINE_VERSION", "11.22")
WINE_FLAVOR = "staging-amd64-wow64"
WINE_URL = os.environ.get(
    "FLSETUP_WINE_URL",
    f"https://github.com/Kron4ek/Wine-Builds/releases/download/{WINE_VERSION}/"
    f"wine-{WINE_VERSION}-{WINE_FLAVOR}.tar.xz",
)

# Optional compatibility components.
WINETRICKS_URL = "https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"

HOME = Path.home()
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache")

DATA_DIR = XDG_DATA / "fl-studio-linux"          # our private files
WINE_DIR = DATA_DIR / "wine"                     # bundled wine runtime
SETUP_COPY = DATA_DIR / "FL Studio Setup.AppImage"  # copy used for Modify/Uninstall
STATE_FILE = DATA_DIR / "install.json"
LOG_FILE = DATA_DIR / "setup.log"
CACHE_DIR = XDG_CACHE / "fl-studio-linux"
DEFAULT_PREFIX = DATA_DIR / "prefix"

BIN_DIR = HOME / ".local/bin"
LAUNCHER = BIN_DIR / "fl-studio"
APPS_DIR = XDG_DATA / "applications"
ICON_DIR = XDG_DATA / "icons/hicolor"
MIME_DIR = XDG_DATA / "mime"

DESKTOP_ID = "fl-studio-linux.desktop"
SETUP_DESKTOP_ID = "fl-studio-linux-setup.desktop"
RUNEXE_DESKTOP_ID = "fl-studio-linux-run-exe.desktop"
FLP_MIME = "application/x-flstudio-project"
ICON_NAME = "fl-studio-linux"


def desktop_dir() -> Path:
    """The user's Desktop folder (localized), like xdg-user-dir DESKTOP."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "user-dirs.dirs"
    try:
        for line in cfg.read_text().splitlines():
            if line.startswith("XDG_DESKTOP_DIR="):
                val = line.split("=", 1)[1].strip().strip('"')
                return Path(val.replace("$HOME", str(HOME)))
    except OSError:
        pass
    return HOME / "Desktop"


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
