"""Fetch libshadertranslator (x64) for the gfxstream renderer, pinned.

    python renderer/fetch_shadertranslator.py [--platform windows|linux] [--dest DIR ...]

On desktop OpenGL, gfxstream translates the guest's GLSL ES shaders with ANGLE's shader
translator, loaded at run time as libshadertranslator.dll / .so (gfxstream built with
USE_ANGLE_SHADER_PARSER; header in renderer/third_party/shadertranslator/, checked by
st_probe). Google ships it in the Android emulator package; nothing else publishes it.
Source: Google's SDK repository (dl.google.com, listed in repository2-3.xml with these
SHA-1s); the SHA-256s are pinned here too. ~350-455 MB download per platform, cached in
deps/shadertranslator/<platform>/ (the library only).
License: ANGLE's BSD-3-Clause (renderer/ANGLE-LICENSE.txt).
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

EMULATOR_BUILD = "16740729"
PLATFORMS = {
    "windows": {
        "zip": f"emulator-windows_x64-{EMULATOR_BUILD}.zip",
        "sha1": "4d7268aa7787c640ad892844e6c9a5f159e91ad3",        # Google's manifest
        "sha256": "625276a1d3ac6b15d00f8aad5e233e7dc3a7cb71435f97d64e3be298399e171d",
        "files": {"libshadertranslator.dll": "emulator/lib64/libshadertranslator.dll"},
    },
    "linux": {
        "zip": f"emulator-linux_x64-{EMULATOR_BUILD}.zip",
        "sha1": "f41f9e31eb5ca64926603c03265c50bbc0ff4172",        # Google's manifest
        "sha256": "cbc7c3cfb83220b8a0f9c513fdab557712e74d339ec19ddffa43f23350ae9bdd",
        # the translator links the emulator's own LLVM libc++ (NEEDED libc++.so, RUNPATH $ORIGIN)
        "files": {"libshadertranslator.so": "emulator/lib64/libshadertranslator.so",
                  "libc++.so": "emulator/lib64/libc++.so"},
    },
}

ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "deps" / "shadertranslator"


def digests(path: Path) -> tuple[str, str]:
    h1, h2 = hashlib.sha1(), hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h1.update(block)
            h2.update(block)
    return h1.hexdigest(), h2.hexdigest()


def fetch(platform: str) -> list[Path]:
    """Download (once) and verify the emulator zip; extract the libraries into the cache."""
    spec = PLATFORMS[platform]
    cache = CACHE_ROOT / platform
    cache.mkdir(parents=True, exist_ok=True)
    libs, stamp = [cache / n for n in spec["files"]], cache / "VERSION"
    if all(p.exists() for p in libs) and stamp.exists() \
            and stamp.read_text().strip() == EMULATOR_BUILD:
        return libs
    z = cache / spec["zip"]
    url = f"https://dl.google.com/android/repository/{spec['zip']}"
    if not z.exists():
        print(f"[shadertranslator] downloading {url}")
        tmp = z.with_suffix(".part")
        req = urllib.request.Request(url, headers={"User-Agent": "openstacks-build"})
        with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        tmp.replace(z)
    sha1, sha256 = digests(z)
    if (sha1, sha256) != (spec["sha1"], spec["sha256"]):
        z.unlink(missing_ok=True)
        raise SystemExit(f"[shadertranslator] {spec['zip']}: sha1 {sha1} / sha256 {sha256} "
                         f"do not match the pinned {spec['sha1']} / {spec['sha256']}")
    with zipfile.ZipFile(z) as zf:
        for name, member in spec["files"].items():
            with zf.open(member) as src, open(cache / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    stamp.write_text(EMULATOR_BUILD + "\n")
    z.unlink()                           # the libraries (a few MB) are all we keep
    print(f"[shadertranslator] {', '.join(spec['files'])} from Android emulator "
          f"{EMULATOR_BUILD} (verified)")
    return libs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=sorted(PLATFORMS),
                    default="windows" if sys.platform == "win32" else "linux")
    ap.add_argument("--dest", action="append", default=[], help="copy the library here too")
    a = ap.parse_args()
    libs = fetch(a.platform)
    for d in a.dest:
        Path(d).mkdir(parents=True, exist_ok=True)
        for lib in libs:
            shutil.copy2(lib, Path(d) / lib.name)
        print(f"[shadertranslator] -> {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
