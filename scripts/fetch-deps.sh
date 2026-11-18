#!/usr/bin/env bash
# Download and unpack the build dependencies into ./deps:
#   - a relocatable Python 3.12 with Tk (python-appimage, manylinux2014)
#   - appimagetool and its AppImage runtime
# Needs: curl, unsquashfs (squashfs-tools), python3.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEPS="$HERE/deps"
PY_URL="${PY_URL:-https://github.com/niess/python-appimage/releases/download/python3.12/python3.12.15-cp312-cp312-manylinux2014_x86_64.AppImage}"
TOOL_URL="${TOOL_URL:-https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage}"

mkdir -p "$DEPS/download"
fetch() { [ -s "$2" ] || curl -fL --retry 3 -o "$2" "$1"; }
fetch "$PY_URL" "$DEPS/download/python.AppImage"
fetch "$TOOL_URL" "$DEPS/download/appimagetool.AppImage"

# Unpack without executing the AppImages (works without FUSE).
unpack() {
  local img="$1" dest="$2" off
  off="$(python3 "$HERE/scripts/elf-offset.py" "$img")"
  rm -rf "$dest"
  unsquashfs -q -o "$off" -d "$dest" "$img"
}
unpack "$DEPS/download/python.AppImage" "$DEPS/pyroot"
unpack "$DEPS/download/appimagetool.AppImage" "$DEPS/tool"
off="$(python3 "$HERE/scripts/elf-offset.py" "$DEPS/download/appimagetool.AppImage")"
head -c "$off" "$DEPS/download/appimagetool.AppImage" > "$DEPS/runtime-x86_64"
echo "Dependencies ready in $DEPS"
