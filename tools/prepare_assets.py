# -*- coding: utf-8 -*-
"""Build OpenStacks' runtime assets from the user's own BlueStacks 5 install.

Nothing BlueStacks-owned is stored in git; this script extracts/copies it locally:

  assets/<image>/kernel.bin      bzImage from Engine/<image>/fastboot.vdi (LBA 1..)
  assets/<image>/initramfs.bin   the pristine gzip cpio right after the kernel
                                 (size = MBR+0x1fa, exactly what BlueStacks' loader passes)
  assets/<image>/Root.vhd        copy of Engine/<image>/Root.vhd (system, used snapshot=on)
  assets/<image>/Data_template.vhdx  copy of Engine/<image>/Data_orig.vhdx (blank /data)
  assets/<image>/manifest.json   BlueStacks version + sha256 of everything

The renderer (libOpenglRender.dll) is loaded straight from the BlueStacks install dir.

  python tools\\prepare_assets.py [--image Pie64] [--engine C:\\ProgramData\\BlueStacks_nxt\\Engine]
                                 [--out assets] [--no-root]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = Path(r"C:\ProgramData\BlueStacks_nxt\Engine")
INSTALL = Path(r"C:\Program Files\BlueStacks_nxt")
SECTOR = 512


def read_vdi(path: Path, max_bytes: int) -> bytes:
    """Flatten the first max_bytes of a VirtualBox VDI (fixed or dynamic)."""
    f = path.open("rb")
    hdr = f.read(0x200)
    if struct.unpack_from("<I", hdr, 0x40)[0] != 0xBEDA107F:
        raise SystemExit(f"{path}: not a VDI image")
    off_blocks, off_data = struct.unpack_from("<II", hdr, 0x154)
    cb_disk = struct.unpack_from("<Q", hdr, 0x170)[0]
    cb_block, _, c_blocks = struct.unpack_from("<III", hdr, 0x178)
    f.seek(off_blocks)
    bmap = struct.unpack(f"<{c_blocks}I", f.read(4 * c_blocks))
    want = min(max_bytes, cb_disk)
    out = bytearray()
    for i in range((want + cb_block - 1) // cb_block):
        if bmap[i] >= 0xFFFFFFFE:                 # not allocated / zero block
            out += bytes(cb_block)
        else:
            f.seek(off_data + bmap[i] * cb_block)
            out += f.read(cb_block)
    return bytes(out[:want])


def split_boot_disk(raw: bytes) -> tuple[bytes, bytes]:
    """fastboot.vdi: LBA0 BlueStacks loader, LBA1.. bzImage, then the initrd (gzip)."""
    bz = raw[SECTOR:]
    if bz[0x202:0x206] != b"HdrS":
        raise SystemExit("fastboot: no bzImage header at LBA1")
    setup_sects = bz[0x1F1] or 4
    syssize = struct.unpack_from("<I", bz, 0x1F4)[0] * 16
    klen = (1 + setup_sects) * SECTOR + syssize
    kernel = bz[:klen]
    isize = struct.unpack_from("<I", raw, 0x1FA)[0]
    gz = raw.find(b"\x1f\x8b\x08", SECTOR + klen)
    if gz < 0 or gz - (SECTOR + klen) > 0x10000:
        raise SystemExit("fastboot: initramfs gzip not found after the kernel")
    initrd = raw[gz:gz + isize]
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    d.decompress(initrd)
    if not d.eof:
        raise SystemExit("fastboot: initramfs gzip stream incomplete")
    return kernel, initrd


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bst_version() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\BlueStacks_nxt") as k:
            return winreg.QueryValueEx(k, "Version")[0]
    except OSError:
        return "unknown"


def copy_if_changed(src: Path, dst: Path) -> None:
    if dst.exists() and dst.stat().st_size == src.stat().st_size and \
            dst.stat().st_mtime >= src.stat().st_mtime:
        print(f"  keep {dst.name} (up to date)")
        return
    print(f"  copy {src} -> {dst} ({src.stat().st_size >> 20} MiB)")
    shutil.copy2(src, dst)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--image", default="Pie64", help="BlueStacks engine image (Pie64 = Android 9)")
    ap.add_argument("--engine", default=str(ENGINE))
    ap.add_argument("--out", default=str(REPO / "assets"))
    ap.add_argument("--no-root", action="store_true", help="skip the 1.8 GB Root.vhd copy")
    a = ap.parse_args()

    src = Path(a.engine) / a.image
    out = Path(a.out) / a.image
    if not (src / "fastboot.vdi").exists():
        print(f"BlueStacks image not found: {src}\\fastboot.vdi "
              "(install BlueStacks 5 and create the instance once)", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)

    print(f"[1/3] boot disk {src / 'fastboot.vdi'}")
    raw = read_vdi(src / "fastboot.vdi", 32 << 20)
    kernel, initrd = split_boot_disk(raw)
    (out / "kernel.bin").write_bytes(kernel)
    (out / "initramfs.bin").write_bytes(initrd)
    print(f"  kernel.bin {len(kernel)} B, initramfs.bin {len(initrd)} B")

    print("[2/3] disks")
    if not a.no_root:
        copy_if_changed(src / "Root.vhd", out / "Root.vhd")
    tmpl = src / "Data_orig.vhdx"
    if tmpl.exists():
        copy_if_changed(tmpl, out / "Data_template.vhdx")
    else:
        print("  WARNING: Data_orig.vhdx missing; new instances need an existing data disk")

    print("[3/3] manifest")
    man = {"image": a.image, "bluestacks_version": bst_version(),
           "renderer_dll": str(INSTALL / "libOpenglRender.dll"),
           "renderer_dll_present": (INSTALL / "libOpenglRender.dll").exists(),
           "files": {p.name: sha256(p) for p in sorted(out.iterdir())
                     if p.is_file() and p.name != "manifest.json"}}
    (out / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    print(f"  {out / 'manifest.json'}")
    if not man["renderer_dll_present"]:
        print("  WARNING: libOpenglRender.dll not found in the BlueStacks install dir")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
