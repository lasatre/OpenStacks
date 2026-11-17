"""BlueStacks engine images: download -> extract -> patch -> assets.

Pipeline (each stage is a function, so the front-end and the CLI share it):

  1. download(image)      <cache>/<Image>.exe from BlueStacks' CDN (resumable, md5-checked).
                          One self-extracting 7z per image: a 392,848-byte ELF 7-Zip SFX stub
                          + a standard 7z archive (docs/re/image_download_spec.md).
  2. extract(exe)         -> <staging>/<Image>/  (Root.vhd, fastboot.vdi, Data.vhdx,
                          Android.bstk.in, oem_<Image>.cfg)
  3. apply_patches()      patches/<Image>/*.py and patches/all/*.py run in name order on the
                          staging dir (the hook for the image clean-up step; none yet)
  4. build_assets()       -> assets/<Image>/: kernel.bin + initramfs.bin cut from fastboot.vdi,
                          Root.vhd, Data_template.vhdx, oem cfg, manifest.json

Nothing BlueStacks-owned is stored in the repo; everything is fetched/derived locally.
build_assets() also works on an installed BlueStacks Engine/<Image> dir (prepare_assets.py).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_ROOT = PROJECT_ROOT / "assets"
CACHE_DIR = PROJECT_ROOT / "deps" / "images"
PATCH_DIR = PROJECT_ROOT / "patches"

# Download locations are per BlueStacks client build: the URL embeds the version and a
# per-build key baked into HD-MultiInstanceManager.exe (getImageDownloadUrl).
CDN_BASE = "https://cdn3.bluestacks.com/downloads"
CLIENT_BUILDS = {
    "5.21.550.1031": {
        "oem": "nxt",
        "key": "5a99b4c3b70826e26cecb85b296b66c5",
        "images": {
            # name: (android, download bytes, md5 of the whole .exe)
            "Nougat32": ("7 (32-bit)", 514831954, "316e7ad283cf6045bf0fd5caef7895b4"),
            "Nougat64": ("7", 714106476, "8b48c5f81bcf8e9055fb1e58e75afd9f"),
            "Pie64": ("9", 594114143, "f482bb4cf40abcbec8422f5d4168c846"),
            "Rvc64": ("11", 693493911, "ffaea01e90e5e0ef2e7b1288c5df3306"),
        },
    },
    # key = the only 32-hex string in that build's HD-MultiInstanceManager.exe; verified
    # live (HEAD) 2026-11-17. Newer re-releases of the 5.21 images plus Tiramisu64.
    "5.22.290.1031": {
        "oem": "nxt",
        "key": "b3b8c6d5d7f2ef642075595e7aa5754c",
        "images": {
            "Nougat32": ("7 (32-bit)", 547530506, "6cb8913e664fc39cf682154a904aeb8f"),
            "Nougat64": ("7", 765639468, "a7265e7bd5c9f991edadc962707605b7"),
            "Pie64": ("9", 656337273, "e6ce22b1ca98621eacd1418b7d6cd399"),
            "Rvc64": ("11", 694319694, "57febb0ad58b3d4fba5382fa379b775b"),
            "Tiramisu64": ("13", 815153759, "b089fcfae345cec6ad84d271c17f289c"),
        },
    },
}
# Images come from DEFAULT_BUILD when it has them (it matches the installed renderer DLL,
# 5.21.550); images only a newer build offers (Tiramisu64) come from the newest such build.
DEFAULT_BUILD = "5.21.550.1031"
SFX_STUB = 392848
SEVENZ_MAGIC = b"7z\xbc\xaf\x27\x1c"
UA = "OpenStacks-image-fetch/1.0"
SECTOR = 512

ProgressFn = Callable[[str, int, int], None]      # (stage, done, total)


@dataclass
class ImageInfo:
    name: str
    android: str
    size: int
    md5: str
    build: str

    @property
    def url(self) -> str:
        b = CLIENT_BUILDS[self.build]
        return (f"{CDN_BASE}/windows/{b['oem']}/{self.build}/{b['key']}"
                f"/Android/{self.name}/{self.name}.exe")


def _builds_newest_first() -> list[str]:
    return sorted(CLIENT_BUILDS, key=lambda v: tuple(int(x) for x in v.split(".")),
                  reverse=True)


def image_info(name: str, build: str = "") -> ImageInfo:
    """The download for `name`: from `build` if given, else DEFAULT_BUILD, else the
    newest build that offers it."""
    order = [build] if build else [DEFAULT_BUILD] + _builds_newest_first()
    for b in order:
        imgs = CLIENT_BUILDS.get(b, {}).get("images", {})
        if name in imgs:
            a, sz, md5 = imgs[name]
            return ImageInfo(name, a, sz, md5, b)
    known = sorted({n for b in CLIENT_BUILDS for n in CLIENT_BUILDS[b]["images"]})
    raise KeyError(f"unknown image {name!r}" + (f" in build {build}" if build else "")
                   + f" (known: {', '.join(known)})")


def catalog(build: str = "") -> list[ImageInfo]:
    """One entry per image name (see image_info for which build provides it)."""
    names = []
    for b in ([build] if build else [DEFAULT_BUILD] + _builds_newest_first()):
        for n in CLIENT_BUILDS[b]["images"]:
            if n not in names:
                names.append(n)
    return [image_info(n, build) for n in names]


def installed_images(root: Path = ASSETS_ROOT) -> list[str]:
    """Images with usable assets (kernel + initramfs + Root.vhd)."""
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir()
                  if (d / "kernel.bin").exists() and (d / "initramfs.bin").exists()
                  and (d / "Root.vhd").exists())


class Cancelled(Exception):
    pass


def _check(cancel: Optional[Callable[[], bool]]) -> None:
    if cancel and cancel():
        raise Cancelled()


# -- 1. download ----------------------------------------------------------------------
def download(info: ImageInfo, cache: Path = CACHE_DIR, progress: Optional[ProgressFn] = None,
             cancel: Optional[Callable[[], bool]] = None) -> Path:
    """Fetch <cache>/<Image>.exe; resumes a .part file; verifies the md5."""
    cache.mkdir(parents=True, exist_ok=True)
    final = cache / f"{info.name}-{info.build}.exe"
    if final.exists() and final.stat().st_size == info.size:
        return final
    part = cache / f"{info.name}-{info.build}.exe.part"
    have = part.stat().st_size if part.exists() else 0
    if have > info.size:
        part.unlink()
        have = 0
    if have < info.size:
        req = urllib.request.Request(info.url, headers={"User-Agent": UA})
        if have:
            req.add_header("Range", f"bytes={have}-")
        with urllib.request.urlopen(req, timeout=60) as r:   # follows the 301 to the mirror
            if have and r.status != 206:                       # server ignored Range: restart
                have = 0
                part.unlink(missing_ok=True)
            with open(part, "ab") as f:
                last = 0.0
                while True:
                    _check(cancel)
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    have += len(chunk)
                    if progress and time.time() - last > 0.2:
                        progress("download", have, info.size)
                        last = time.time()
    if progress:
        progress("verify", 0, info.size)
    h = hashlib.md5()
    with open(part, "rb") as f:
        done = 0
        for chunk in iter(lambda: f.read(8 << 20), b""):
            _check(cancel)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress("verify", done, info.size)
    if h.hexdigest() != info.md5:
        part.unlink(missing_ok=True)
        raise IOError(f"{info.name}: md5 mismatch ({h.hexdigest()} != {info.md5}); "
                      "the partial download was removed, try again")
    part.replace(final)
    return final


# -- 2. extract -------------------------------------------------------------------------
def find_7z() -> Optional[str]:
    for name in ("7z", "7za", "7zz", "7zr"):
        p = shutil.which(name)
        if p:
            return p
    for p in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe",
              r"C:\Program Files\BlueStacks_nxt\7zr.exe"):
        if Path(p).exists():
            return p
    return None


def extract(exe: Path, staging: Path, progress: Optional[ProgressFn] = None,
            cancel: Optional[Callable[[], bool]] = None) -> Path:
    """Unpack the self-extracting download into a fresh staging dir. 7-Zip reads the
    archive behind the ELF SFX stub itself (exit code 1 = warnings, e.g. the stub)."""
    seven = find_7z()
    if not seven:
        raise RuntimeError("7-Zip not found: install 7-Zip (https://7-zip.org) or p7zip")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    total = exe.stat().st_size
    if progress:
        progress("unpack", 0, total)
    _check(cancel)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run([seven, "x", "-y", f"-o{staging}", str(exe)],
                       capture_output=True, text=True, creationflags=flags)
    if r.returncode not in (0, 1) or not (staging / "Root.vhd").exists():
        raise RuntimeError(f"7-Zip failed ({r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    if progress:
        progress("unpack", total, total)
    return staging


# -- 3. patch hook ----------------------------------------------------------------------
def apply_patches(staging: Path, image: str, progress: Optional[ProgressFn] = None) -> list[str]:
    """Run patches/all/*.py then patches/<Image>/*.py (name order) against the extracted
    files. Each patch module defines `apply(image_dir: Path, image: str) -> None`.
    Returns the names of the patches applied (recorded in the manifest)."""
    done: list[str] = []
    scripts: list[Path] = []
    for sub in ("all", image):
        d = PATCH_DIR / sub
        if d.is_dir():
            scripts += sorted(p for p in d.glob("*.py") if not p.name.startswith("_"))
    for i, script in enumerate(scripts):
        if progress:
            progress(f"patch {script.name}", i, len(scripts))
        spec = importlib.util.spec_from_file_location(f"ost_patch_{i}", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.apply(staging, image)
        done.append(f"{script.parent.name}/{script.name}")
    return done


# -- 4. assets --------------------------------------------------------------------------
def read_vdi(path: Path, max_bytes: int) -> bytes:
    """Flatten the first max_bytes of a VirtualBox VDI (fixed or dynamic)."""
    with path.open("rb") as f:
        hdr = f.read(0x200)
        if struct.unpack_from("<I", hdr, 0x40)[0] != 0xBEDA107F:
            raise IOError(f"{path}: not a VDI image")
        off_blocks, off_data = struct.unpack_from("<II", hdr, 0x154)
        cb_disk = struct.unpack_from("<Q", hdr, 0x170)[0]
        cb_block, _, c_blocks = struct.unpack_from("<III", hdr, 0x178)
        f.seek(off_blocks)
        bmap = struct.unpack(f"<{c_blocks}I", f.read(4 * c_blocks))
        want = min(max_bytes, cb_disk)
        out = bytearray()
        for i in range((want + cb_block - 1) // cb_block):
            if bmap[i] >= 0xFFFFFFFE:
                out += bytes(cb_block)
            else:
                f.seek(off_data + bmap[i] * cb_block)
                out += f.read(cb_block)
    return bytes(out[:want])


def split_boot_disk(raw: bytes) -> tuple[bytes, bytes]:
    """fastboot.vdi: LBA0 BlueStacks loader, LBA1.. bzImage, then the initrd (gzip, size at
    MBR+0x1fa, exactly what BlueStacks' loader hands the kernel)."""
    bz = raw[SECTOR:]
    if bz[0x202:0x206] != b"HdrS":
        raise IOError("fastboot: no bzImage header at LBA1")
    setup_sects = bz[0x1F1] or 4
    syssize = struct.unpack_from("<I", bz, 0x1F4)[0] * 16
    klen = (1 + setup_sects) * SECTOR + syssize
    isize = struct.unpack_from("<I", raw, 0x1FA)[0]
    gz = raw.find(b"\x1f\x8b\x08", SECTOR + klen)
    if gz < 0 or gz - (SECTOR + klen) > 0x10000:
        raise IOError("fastboot: initramfs gzip not found after the kernel")
    initrd = raw[gz:gz + isize]
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    d.decompress(initrd)
    if not d.eof:
        raise IOError("fastboot: initramfs gzip stream incomplete")
    return bz[:klen], initrd


def disk_args(root: str, data: Optional[str], data_fmt: str = "vhdx",
              root_snap: bool = True, data_snap: bool = False, root_fmt: str = "vpc") -> list[str]:
    """QEMU args for Root.vhd (sda) + the data disk (sdb), on the PIIX IDE controller.
    Nougat's 4.9 kernel has no ata_piix (BlueStacks uses AHCI there; QEMU's AHCI DMA
    returns garbage under WHPX), so its ramdisk is patched to bind ata_generic to the
    PIIX IDE controller instead (patches/all/05_nougat_ide.py)."""
    esc = lambda p: str(p).replace(",", ",,")
    s = lambda on: ",snapshot=on" if on else ""
    disks = [(root, root_fmt, root_snap)] + ([(data, data_fmt, data_snap)] if data else [])
    return [a for i, (f, fm, sn) in enumerate(disks) for a in
            ("-drive", f"file={esc(f)},format={fm},if=ide,index={i}{s(sn)}")]


def kernel_is_64bit(kernel: Path) -> bool:
    """bzImage xloadflags (0x236) bit 0 = XLF_KERNEL_64. Nougat32's kernel is 32-bit
    non-PAE: it can't use bstpgaipc's BAR2 above 4G (pgaipc_opts)."""
    try:
        with open(kernel, "rb") as f:
            hdr = f.read(0x240)
        return bool(struct.unpack_from("<H", hdr, 0x236)[0] & 1)
    except (OSError, struct.error):
        return True


def pgaipc_opts(kernel: Path) -> str:
    """Extra bstpgaipc options for this guest kernel."""
    return "" if kernel_is_64bit(kernel) else ",bar2-64=off"


def kernel_version(kernel: Path) -> tuple[int, int]:
    """(major, minor) from a bzImage's setup header (kernel_version string pointer at
    0x20E, relative to 0x200). (0, 0) if unreadable. Kernel 5.x images (Tiramisu64) use
    the newer bstinput touch protocol."""
    try:
        with open(kernel, "rb") as f:
            hdr = f.read(0x8000)
        ptr = struct.unpack_from("<H", hdr, 0x20E)[0] + 0x200
        ver = hdr[ptr:ptr + 32].split(b" ")[0].decode("ascii", "replace")
        major, minor = ver.split(".")[:2]
        return int(major), int(minor)
    except (OSError, ValueError, struct.error, IndexError):
        return (0, 0)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _place(src: Path, dst: Path, move: bool) -> None:
    if dst.exists() and dst.stat().st_size == src.stat().st_size and not move and \
            dst.stat().st_mtime >= src.stat().st_mtime:
        return
    dst.unlink(missing_ok=True)
    if move:
        shutil.move(str(src), str(dst))
    else:
        shutil.copy2(src, dst)


def build_assets(src: Path, image: str, out_root: Path = ASSETS_ROOT, move: bool = False,
                 source: dict | None = None, progress: Optional[ProgressFn] = None) -> Path:
    """src = an extracted download (staging) or an installed Engine/<Image> dir.
    move=True moves the big files out of a staging dir instead of copying."""
    out = out_root / image
    out.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("assets", 0, 3)
    if (src / "initramfs.bin").exists() and (src / "kernel.bin").exists():
        # already cut (and possibly patched) by install_image
        for name in ("kernel.bin", "initramfs.bin", "initramfs.orig.bin"):
            if (src / name).exists():
                shutil.copy2(src / name, out / name)
    else:
        raw = read_vdi(src / "fastboot.vdi", 32 << 20)
        kernel, initrd = split_boot_disk(raw)
        (out / "kernel.bin").write_bytes(kernel)
        (out / "initramfs.bin").write_bytes(initrd)
        (out / "initramfs.orig.bin").write_bytes(initrd)
    if progress:
        progress("assets", 1, 3)
    _place(src / "Root.vhd", out / "Root.vhd", move)
    # blank /data: Data_orig.vhdx in an installed engine dir, Data.vhdx in a download
    for name in ("Data_orig.vhdx", "Data.vhdx"):
        if (src / name).exists() and not (name == "Data.vhdx" and not source):
            _place(src / name, out / "Data_template.vhdx", move)
            break
    for extra in (f"oem_{image}.cfg", "Android.bstk.in"):
        if (src / extra).exists():
            shutil.copy2(src / extra, out / extra)
    if progress:
        progress("assets", 2, 3)
    man = {"image": image, "android": _android_of(image), "source": source or {"from": str(src)},
           "created": time.strftime("%Y-%m-%d %H:%M:%S"),
           "files": {p.name: _sha256(p) for p in sorted(out.iterdir())
                     if p.is_file() and p.name != "manifest.json"}}
    (out / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    if progress:
        progress("assets", 3, 3)
    return out


def _android_of(image: str) -> str:
    try:
        return image_info(image).android
    except KeyError:
        return "?"


# -- all stages -------------------------------------------------------------------------
def install_image(name: str, progress: Optional[ProgressFn] = None,
                  cancel: Optional[Callable[[], bool]] = None, keep_download: bool = False,
                  out_root: Path = ASSETS_ROOT, build: str = "") -> Path:
    """download -> extract -> patch -> assets/<Image>/. Returns the asset dir."""
    info = image_info(name, build)
    exe = download(info, progress=progress, cancel=cancel)
    staging = CACHE_DIR / f"{name}.staging"
    extract(exe, staging, progress=progress, cancel=cancel)
    cut_boot_files(staging)
    patches = apply_patches(staging, name, progress=progress)
    src = {"from": "download", "url": info.url, "client_build": info.build,
           "md5": info.md5, "patches": patches}
    out = build_assets(staging, name, out_root=out_root, move=True, source=src,
                       progress=progress)
    shutil.rmtree(staging, ignore_errors=True)
    if not keep_download:
        exe.unlink(missing_ok=True)
    return out


def cut_boot_files(d: Path) -> None:
    """kernel.bin + initramfs.bin (+ an untouched initramfs.orig.bin) from d/fastboot.vdi,
    so patches can edit the boot ramdisk before the assets are built."""
    kernel, initrd = split_boot_disk(read_vdi(d / "fastboot.vdi", 32 << 20))
    (d / "kernel.bin").write_bytes(kernel)
    (d / "initramfs.bin").write_bytes(initrd)
    (d / "initramfs.orig.bin").write_bytes(initrd)


def repatch(image: str, out_root: Path = ASSETS_ROOT,
            progress: Optional[ProgressFn] = None) -> list[str]:
    """Re-run the patches on an installed image from its pristine ramdisk (no download).
    Patches that edit Root.vhd are not re-applied here (only ramdisk-level patches)."""
    out = out_root / image
    orig = out / "initramfs.orig.bin"
    if not orig.exists():
        # assets made before patching existed: the current ramdisk is the original
        data = (out / "initramfs.bin").read_bytes()
        if b"/boot/ost/ost_boot.sh" in __import__("gzip").decompress(data):
            raise RuntimeError(f"{image}: no initramfs.orig.bin and the ramdisk is already "
                               "patched; re-run prepare_assets for this image")
        orig.write_bytes(data)
    work = CACHE_DIR / f"{image}.repatch"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    for name in ("kernel.bin", f"oem_{image}.cfg", "Android.bstk.in"):
        if (out / name).exists():
            shutil.copy2(out / name, work / name)
    shutil.copy2(orig, work / "initramfs.bin")
    patches = apply_patches(work, image, progress=progress)
    shutil.copy2(work / "initramfs.bin", out / "initramfs.bin")
    shutil.rmtree(work, ignore_errors=True)
    man_path = out / "manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}
    man.setdefault("source", {})["patches"] = patches
    man["patched"] = time.strftime("%Y-%m-%d %H:%M:%S")
    man.setdefault("files", {})["initramfs.bin"] = _sha256(out / "initramfs.bin")
    man_path.write_text(json.dumps(man, indent=2), encoding="utf-8")
    return patches


def _cli_progress(stage: str, done: int, total: int) -> None:
    pct = 100 * done / total if total else 100
    sys.stdout.write(f"\r  {stage:<12} {pct:5.1f}%  ({done >> 20} / {total >> 20} MiB)   ")
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")
