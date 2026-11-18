"""Turn the PyInstaller output + the QEMU build into the portable release folder and ZIP.

    python packaging/assemble.py --version 0.2.0 [--sevenzip 7zr.exe] [--zip]

Layout of release/OpenStacks/:
    OpenStacks.exe, _internal/      front-end (PyInstaller, dist/OpenStacks)
    qemu/                           qemu-system-x86_64.exe + runtime DLLs + pc-bios/
    patches/                        image patches (run when an image is installed)
    licenses/                       license texts of the open renderer (gfxstream, ANGLE)
    tools/7zr.exe                   7-Zip, unpacks downloaded images
    README.md, THIRD-PARTY.md, VERSION
data/ (images, instances, logs) is created next to the exe on first run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--dist", default=str(ROOT / "dist" / "OpenStacks"))
    ap.add_argument("--qemu", default=str(ROOT / "build"), help="qemu/build.cmd output dir")
    ap.add_argument("--sevenzip", default="", help="7zr.exe to bundle")
    ap.add_argument("--out", default=str(ROOT / "release"))
    ap.add_argument("--zip", action="store_true", help="also write OpenStacks-<ver>-win64.zip")
    ap.add_argument("--strip", default="", help="strip.exe: drop debug info from the release "
                    "copy of QEMU (80 -> ~20 MB); default: MSYS2 ucrt64 strip if present")
    a = ap.parse_args()

    dist, qbuild, out = Path(a.dist), Path(a.qemu), Path(a.out)
    exe = qbuild / "qemu-system-x86_64.exe"
    for need in (dist / "OpenStacks.exe", exe, qbuild / "pc-bios"):
        if not need.exists():
            print(f"missing {need}", file=sys.stderr)
            return 1
    app = out / "OpenStacks"
    shutil.rmtree(app, ignore_errors=True)
    copytree(dist, app)

    q = app / "qemu"
    q.mkdir()
    shutil.copy2(exe, q)
    strip = a.strip or next((str(p) for p in (
        Path(os.environ.get("MSYS2_ROOT") or r"C:\msys64") / "ucrt64" / "bin" / "strip.exe",)
        if p.exists()), "")
    if strip:
        subprocess.run([strip, "--strip-debug", str(q / exe.name)], check=True)
        print(f"stripped {exe.name}: {(q / exe.name).stat().st_size / 2**20:.0f} MiB")
    else:
        print("note: no strip.exe, QEMU keeps its debug info")
    for dll in qbuild.glob("*.dll"):
        shutil.copy2(dll, q)
    copytree(qbuild / "pc-bios", q / "pc-bios")

    # the open renderer's licenses, when its DLLs came along from build\
    lic = app / "licenses"
    for dll, text, name in (
            ("ost_renderer.dll", ROOT / "deps" / "gfxstream" / "LICENSE", "gfxstream-LICENSE.txt"),
            ("libshadertranslator.dll", ROOT / "renderer" / "ANGLE-LICENSE.txt",
             "ANGLE-LICENSE.txt")):
        if (q / dll).exists():
            if not text.exists():
                print(f"missing {text} (license for {dll})", file=sys.stderr)
                return 1
            lic.mkdir(exist_ok=True)
            shutil.copy2(text, lic / name)

    copytree(ROOT / "patches", app / "patches")
    if a.sevenzip:
        (app / "tools").mkdir(exist_ok=True)
        shutil.copy2(a.sevenzip, app / "tools" / "7zr.exe")
    for doc in ("README.md", "THIRD-PARTY.md", "LICENSE"):
        if (ROOT / doc).exists():
            shutil.copy2(ROOT / doc, app / doc)
    (app / "VERSION").write_text(a.version.lstrip("vV") + "\n", encoding="utf-8")

    n = sum(1 for _ in app.rglob("*") if _.is_file())
    size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file())
    print(f"{app}: {n} files, {size / 2**20:.0f} MiB")
    if a.zip:
        z = shutil.make_archive(str(out / f"OpenStacks-{a.version.lstrip('vV')}-win64"),
                                "zip", root_dir=out, base_dir="OpenStacks")
        print(z)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
