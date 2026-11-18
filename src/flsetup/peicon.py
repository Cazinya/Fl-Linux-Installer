"""Minimal extractor for the main icon of a Windows .exe (PE) file.

Writes the largest image of the first icon group. Modern Windows icons store
their 256px image as PNG, which is written as .png; otherwise a .ico is built.
"""
import struct
from pathlib import Path

RT_ICON, RT_GROUP_ICON = 3, 14


def _sections(d):
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    if d[pe:pe + 4] != b"PE\0\0":
        raise ValueError("not a PE file")
    nsec = struct.unpack_from("<H", d, pe + 6)[0]
    optsz = struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    ddir = opt + (112 if magic == 0x20B else 96)
    rsrc_rva = struct.unpack_from("<I", d, ddir + 2 * 8)[0]
    secs = []
    off = opt + optsz
    for i in range(nsec):
        vsize, va, rawsz, rawptr = struct.unpack_from("<IIII", d, off + i * 40 + 8)
        secs.append((va, max(vsize, rawsz), rawptr))
    return rsrc_rva, secs


def _rva2off(rva, secs):
    for va, size, raw in secs:
        if va <= rva < va + size:
            return rva - va + raw
    raise ValueError("bad rva")


def _read_dir(d, base, off):
    named, ids = struct.unpack_from("<HH", d, off + 12)
    out = []
    for i in range(named + ids):
        name, target = struct.unpack_from("<II", d, off + 16 + i * 8)
        out.append((name, target))
    return out


def _leaves(d, base, off):
    """Yield data-entry offsets under a directory (depth-first)."""
    for name, target in _read_dir(d, base, off):
        if target & 0x80000000:
            yield from ((name, x) for _, x in _leaves(d, base, base + (target & 0x7FFFFFFF)))
        else:
            yield name, base + target


def extract_icon(exe: Path, out_base: Path) -> Path | None:
    with open(exe, "rb") as f:
        d = f.read()
    rsrc_rva, secs = _sections(d)
    if not rsrc_rva:
        return None
    base = _rva2off(rsrc_rva, secs)
    types = {n: t for n, t in _read_dir(d, base, base)}

    def entries(rtype):
        t = types.get(rtype)
        if t is None or not t & 0x80000000:
            return {}
        res = {}
        for name, target in _read_dir(d, base, base + (t & 0x7FFFFFFF)):
            sub = base + (target & 0x7FFFFFFF) if target & 0x80000000 else None
            if sub is None:
                continue
            for _, leaf in _leaves(d, base, sub):
                rva, size = struct.unpack_from("<II", d, leaf)
                res.setdefault(name, d[_rva2off(rva, secs):_rva2off(rva, secs) + size])
                break
        return res

    groups, icons = entries(RT_GROUP_ICON), entries(RT_ICON)
    if not groups:
        return None
    grp = next(iter(groups.values()))
    count = struct.unpack_from("<H", grp, 4)[0]
    items = []
    for i in range(count):
        w, h, cc, _, planes, bpp, size, iid = struct.unpack_from("<BBBBHHIH", grp, 6 + i * 14)
        if iid in icons:
            items.append(((w or 256), bpp, iid, (w, h, cc, planes, bpp)))
    if not items:
        return None
    items.sort(reverse=True)
    best = icons[items[0][2]]
    if best[:8] == b"\x89PNG\r\n\x1a\n":
        out = out_base.with_suffix(".png")
        out.write_bytes(best)
        return out
    # Build a .ico with every image of the group.
    data = [icons[it[2]] for it in items]
    hdr = struct.pack("<HHH", 0, 1, len(items))
    off = 6 + 16 * len(items)
    dirs = b""
    for it, blob in zip(items, data):
        w, h, cc, planes, bpp = it[3]
        dirs += struct.pack("<BBBBHHII", w, h, cc, 0, planes, bpp, len(blob), off)
        off += len(blob)
    out = out_base.with_suffix(".ico")
    out.write_bytes(hdr + dirs + b"".join(data))
    return out
