#!/usr/bin/env bash
# Build the "FL Studio Setup" AppImage into dist/.
# Fetches its dependencies on first run (scripts/fetch-deps.sh, scripts/build-cabextract.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PYROOT="${PYROOT:-$HERE/deps/pyroot}"        # unpacked python3.12 manylinux2014 AppImage
TOOL="${TOOL:-$HERE/deps/tool}"              # unpacked appimagetool AppImage
RUNTIME="${RUNTIME:-$HERE/deps/runtime-x86_64}"
OUT="${OUT:-$HERE/dist/FL-Studio-Setup-x86_64.AppImage}"
APPDIR="$HERE/deps/AppDir"

[ -d "$PYROOT" ] && [ -d "$TOOL" ] && [ -f "$RUNTIME" ] || bash "$HERE/scripts/fetch-deps.sh"
[ -x "$HERE/src/flsetup/bin/cabextract" ] || bash "$HERE/scripts/build-cabextract.sh"

rm -rf "$APPDIR"
cp -a "$PYROOT" "$APPDIR"
rm -f "$APPDIR"/*.desktop "$APPDIR"/python.png "$APPDIR"/.DirIcon "$APPDIR/AppRun"
rm -rf "$APPDIR"/usr/share/metainfo "$APPDIR"/usr/share/applications "$APPDIR"/usr/share/icons
# Trim the Python runtime.
PYLIB="$APPDIR/opt/python3.12/lib/python3.12"
rm -rf "$PYLIB"/{test,idlelib,ensurepip,lib2to3,pydoc_data,turtledemo,unittest/test} \
       "$PYLIB"/site-packages/pip* "$PYLIB"/tkinter/test "$APPDIR"/usr/bin/pip* \
       "$APPDIR"/opt/python3.12/bin/pip*
find "$APPDIR" -name "__pycache__" -type d -prune -exec rm -rf {} +

mkdir -p "$APPDIR/opt/app"
cp -a "$HERE/src/flsetup" "$APPDIR/opt/app/"
find "$APPDIR/opt/app" -name "__pycache__" -type d -prune -exec rm -rf {} +
cat > "$APPDIR/opt/app/start.py" <<'EOF'
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flsetup.__main__ import main
main()
EOF

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
# FL Studio Setup for Linux - AppImage entry point
if [ -z "${APPDIR:-}" ]; then
  APPDIR="$(dirname "$(readlink -f "$0")")"; export APPDIR
fi
export TCL_LIBRARY="$APPDIR/usr/share/tcltk/tcl8.6"
export TK_LIBRARY="$APPDIR/usr/share/tcltk/tk8.6"
export TKPATH="$TK_LIBRARY"
export SSL_CERT_FILE="$APPDIR/opt/_internal/certs.pem"
exec "$APPDIR/opt/python3.12/bin/python3.12" -I -B "$APPDIR/opt/app/start.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/fl-studio-setup.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=FL Studio Setup
Comment=Install FL Studio on Linux
Exec=AppRun
Icon=fl-studio-setup
Categories=AudioVideo;Audio;
Terminal=false
EOF
cp "$HERE/src/flsetup/assets/icon.png" "$APPDIR/fl-studio-setup.png"
ln -sf fl-studio-setup.png "$APPDIR/.DirIcon"

mkdir -p "$(dirname "$OUT")"
ARCH=x86_64 "$TOOL/AppRun" --no-appstream --runtime-file "$RUNTIME" "$APPDIR" "$OUT"
chmod +x "$OUT"
ls -la "$OUT"
