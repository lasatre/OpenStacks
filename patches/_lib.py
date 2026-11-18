"""Helpers for image patches: edit the boot ramdisk (gzip'd cpio "newc") in pure Python.

    from _lib import Ramdisk
    rd = Ramdisk.load(image_dir / "initramfs.bin")
    rd.read("boot/stage2.sh"); rd.write("boot/ost/x.sh", data, mode=0o755)
    rd.save(image_dir / "initramfs.bin")
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import urllib.request
from pathlib import Path

S_IFDIR, S_IFREG = 0o040000, 0o100000


class Ramdisk:
    def __init__(self, entries: list[dict]):
        self.entries = entries               # dicts: name, mode, uid, gid, mtime, data, ...

    # -- io --------------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "Ramdisk":
        raw = gzip.decompress(Path(path).read_bytes())
        entries, off = [], 0
        while off + 110 <= len(raw):
            h = raw[off:off + 110]
            if h[:6] not in (b"070701", b"070702"):
                raise ValueError(f"bad cpio magic at {off}")
            f = [int(h[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
            ino, mode, uid, gid, nlink, mtime, fsize, dmaj, dmin, rmaj, rmin, nsize, _ = f
            name = raw[off + 110:off + 110 + nsize - 1].decode()
            doff = (off + 110 + nsize + 3) & ~3
            data = raw[doff:doff + fsize]
            off = (doff + fsize + 3) & ~3
            if name == "TRAILER!!!":
                break
            entries.append(dict(name=name, mode=mode, uid=uid, gid=gid, mtime=mtime,
                                nlink=nlink, dev=(dmaj, dmin), rdev=(rmaj, rmin), data=data))
        return cls(entries)

    def save(self, path: Path) -> None:
        out = io.BytesIO()
        for ino, e in enumerate(self.entries + [dict(name="TRAILER!!!", mode=0, uid=0, gid=0,
                                                      mtime=0, nlink=1, dev=(0, 0),
                                                      rdev=(0, 0), data=b"")], start=1):
            name = e["name"].encode() + b"\0"
            hdr = "070701" + "".join(f"{v:08x}" for v in (
                ino if e["name"] != "TRAILER!!!" else 0, e["mode"], e["uid"], e["gid"],
                e["nlink"], e["mtime"], len(e["data"]), e["dev"][0], e["dev"][1],
                e["rdev"][0], e["rdev"][1], len(name), 0))
            out.write(hdr.encode() + name)
            out.write(b"\0" * ((-out.tell()) % 4))
            out.write(e["data"])
            out.write(b"\0" * ((-out.tell()) % 4))
        out.write(b"\0" * ((-out.tell()) % 512))
        Path(path).write_bytes(gzip.compress(out.getvalue(), compresslevel=9, mtime=0))

    # -- edit ------------------------------------------------------------------------
    def find(self, name: str) -> dict | None:
        return next((e for e in self.entries if e["name"] == name), None)

    def read(self, name: str) -> bytes:
        e = self.find(name)
        if e is None:
            raise KeyError(name)
        return e["data"]

    def mkdir(self, name: str, mode: int = 0o755) -> None:
        if self.find(name) is None:
            self.entries.append(dict(name=name, mode=S_IFDIR | mode, uid=0, gid=0, mtime=0,
                                     nlink=2, dev=(0, 0), rdev=(0, 0), data=b""))

    def write(self, name: str, data: bytes, mode: int = 0o644) -> None:
        parts = name.split("/")
        for i in range(1, len(parts)):
            self.mkdir("/".join(parts[:i]))
        e = self.find(name)
        if e is None:
            self.entries.append(dict(name=name, mode=S_IFREG | mode, uid=0, gid=0, mtime=0,
                                     nlink=1, dev=(0, 0), rdev=(0, 0), data=data))
        else:
            e["data"], e["mode"] = data, S_IFREG | mode


def deps_dir(*parts: str) -> Path:
    """Download cache for patches: OST_DEPS_DIR (set by the app: <data>/deps), else the
    source checkout's deps/."""
    base = os.environ.get("OST_DEPS_DIR") or str(Path(__file__).resolve().parents[1] / "deps")
    return Path(base).joinpath(*parts)


def fetch(url: str, dest: Path, sha256: str) -> Path:
    """Download once into dest (cache) and verify the pinned sha256."""
    dest = Path(dest)
    if not dest.exists() or hashlib.sha256(dest.read_bytes()).hexdigest() != sha256:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "OpenStacks-patch/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        got = hashlib.sha256(data).hexdigest()
        if got != sha256:
            raise IOError(f"{url}: sha256 {got} != pinned {sha256}")
        dest.write_bytes(data)
    return dest
