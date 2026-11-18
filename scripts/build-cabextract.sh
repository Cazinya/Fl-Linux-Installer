#!/usr/bin/env bash
# Build a static cabextract (used by winetricks for Windows core fonts) into src/flsetup/bin.
# cabextract is GPL-3.0-or-later: https://github.com/kyz/libmspack
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${CABEXTRACT_TAG:-v1.12}"
SRC="$HERE/deps/libmspack"
OUT="$HERE/src/flsetup/bin/cabextract"

[ -d "$SRC/.git" ] || git clone -q --depth 1 --branch "$TAG" https://github.com/kyz/libmspack "$SRC"
cd "$SRC/cabextract"
VER="$(grep -o 'AC_INIT(\[cabextract\], *\[[0-9.a-z]*' configure.ac | grep -o '[0-9][0-9.a-z]*$')"
mkdir -p "$(dirname "$OUT")"
# The autotools setup needs gettext macros; cabextract is small enough to compile directly.
gcc -O2 -static -o "$OUT.tmp" -I. -Imspack -D_FILE_OFFSET_BITS=64 -DVERSION="\"$VER\"" \
  -DHAVE_STRINGS_H=1 -DHAVE_TOWLOWER=1 -DHAVE_UTIME=1 -DHAVE_MKDIR=1 -DHAVE_UMASK=1 \
  -DHAVE_FSEEKO=1 -DHAVE_ICONV=0 -DLATIN1_FILENAMES=0 -DHAVE_INTTYPES_H=1 -DHAVE_STDINT_H=1 \
  -DHAVE_STDLIB_H=1 -DHAVE_STRING_H=1 -DHAVE_UNISTD_H=1 -DHAVE_FCNTL_H=1 -DHAVE_LIMITS_H=1 \
  -DHAVE_SYS_STAT_H=1 -DHAVE_SYS_TYPES_H=1 -DHAVE_SYS_TIME_H=1 -DHAVE_UTIME_H=1 \
  -DHAVE_DIRENT_H=1 -DHAVE_CTYPE_H=1 -DHAVE_WCTYPE_H=1 -DHAVE_FNMATCH_H=1 -DHAVE_ERRNO_H=1 \
  -DHAVE_STDARG_H=1 -DHAVE_LIBGEN_H=1 \
  src/cabextract.c md5.c mspack/cabd.c mspack/lzxd.c mspack/mszipd.c mspack/qtmd.c mspack/system.c
strip "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
chmod 755 "$OUT"
"$OUT" --version
