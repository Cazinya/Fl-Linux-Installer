"""Entry point: FL Studio Setup for Linux."""
import argparse
import sys

from . import config as C


def main(argv=None):
    ap = argparse.ArgumentParser(prog="FL-Studio-Setup", description=C.APP_NAME)
    ap.add_argument("--maintenance", action="store_true", help="update, repair or uninstall")
    ap.add_argument("--uninstall", action="store_true", help="open Setup with Uninstall selected")
    ap.add_argument("--run-exe", nargs="?", const="", metavar="FILE",
                    help="run a Windows installer (e.g. a VST plugin) inside FL Studio's environment")
    ap.add_argument("--plugins", nargs="*", metavar="FILE",
                    help="open the plugin manager (optionally installing the given files)")
    ap.add_argument("--files", nargs="?", const="", metavar="FOLDER",
                    help="open the FL Studio file browser (sample packs, presets, plugins)")
    ap.add_argument("--settings", action="store_true", help="open FL Studio compatibility settings")
    ap.add_argument("--launch", action="store_true", help="start FL Studio (if installed)")
    ap.add_argument("--version", action="version", version=f"{C.APP_NAME} {C.APP_VERSION}")
    a, _rest = ap.parse_known_args(argv)

    if a.launch:
        from .ui import launch_fl
        sys.exit(0 if launch_fl() else 1)
    import importlib.util
    if importlib.util.find_spec("tkinter") is None:
        print("This program needs a graphical desktop.", file=sys.stderr)
        sys.exit(1)
    from . import ui
    try:
        if a.files is not None:
            from .files import files_mode
            files_mode(a.files or None)
            return
        if a.settings:
            from .settings import settings_mode
            settings_mode()
            return
        if a.plugins is not None or a.run_exe is not None:
            files = list(a.plugins or []) + ([a.run_exe] if a.run_exe else [])
            ui.plugin_manager_mode(files or None)
            return
        mode = "uninstall" if a.uninstall else "maintenance" if a.maintenance else "auto"
        ui.Wizard(mode).run()
    except Exception as e:  # tk.TclError: no display
        if "display" in str(e).lower():
            print("FL Studio Setup needs a graphical desktop session (no display found).", file=sys.stderr)
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
