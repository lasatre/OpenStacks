"""Fetch libshadertranslator.dll (x64) for the gfxstream renderer, pinned.

    python renderer/fetch_shadertranslator.py [--dest DIR ...]

On desktop OpenGL, gfxstream translates the guest's GLSL ES shaders with ANGLE's shader
translator, loaded at run time as libshadertranslator.dll (gfxstream built with
USE_ANGLE_SHADER_PARSER; header in renderer/third_party/shadertranslator/). Google ships
it in the Android emulator package; nothing else publishes it. Source: Google's SDK
repository (dl.google.com, listed in repository2-3.xml with this SHA-1); the SHA-256 is
pinned here too. ~455 MB download, cached in deps/shadertranslator/ (the DLL only).
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
ZIP_NAME = f"emulator-windows_x64-{EMULATOR_BUILD}.zip"
URL = f"https://dl.google.com/android/repository/{ZIP_NAME}"
ZIP_SHA1 = "4d7268aa7787c640ad892844e6c9a5f159e91ad3"       # Google's manifest
ZIP_SHA256 = "625276a1d3ac6b15d00f8aad5e233e7dc3a7cb71435f97d64e3be298399e171d"
MEMBER = "emulator/lib64/libshadertranslator.dll"
DLL = "libshadertranslator.dll"

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "deps" / "shadertranslator"


def digests(path: Path) -> tuple[str, str]:
    h1, h2 = hashlib.sha1(), hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h1.update(block)
            h2.update(block)
    return h1.hexdigest(), h2.hexdigest()


def fetch() -> Path:
    """Download (once) and verify the emulator zip; extract the DLL into CACHE."""
    CACHE.mkdir(parents=True, exist_ok=True)
    stamp = CACHE / "VERSION"
    if (CACHE / DLL).exists() and stamp.exists() and stamp.read_text().strip() == EMULATOR_BUILD:
        return CACHE
    z = CACHE / ZIP_NAME
    if not z.exists():
        print(f"[shadertranslator] downloading {URL} (~455 MB)")
        tmp = z.with_suffix(".part")
        req = urllib.request.Request(URL, headers={"User-Agent": "openstacks-build"})
        with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        tmp.replace(z)
    sha1, sha256 = digests(z)
    if (sha1, sha256) != (ZIP_SHA1, ZIP_SHA256):
        z.unlink(missing_ok=True)
        raise SystemExit(f"[shadertranslator] {ZIP_NAME}: sha1 {sha1} / sha256 {sha256} "
                         f"do not match the pinned {ZIP_SHA1} / {ZIP_SHA256}")
    with zipfile.ZipFile(z) as zf, zf.open(MEMBER) as src, open(CACHE / DLL, "wb") as dst:
        shutil.copyfileobj(src, dst)
    stamp.write_text(EMULATOR_BUILD + "\n")
    z.unlink()                           # the DLL (1.5 MB) is all we keep
    print(f"[shadertranslator] {DLL} from Android emulator {EMULATOR_BUILD} (verified)")
    return CACHE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", action="append", default=[], help="copy the DLL here too")
    a = ap.parse_args()
    src = fetch()
    for d in a.dest:
        Path(d).mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / DLL, Path(d) / DLL)
        print(f"[shadertranslator] -> {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
