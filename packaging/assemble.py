"""Turn the PyInstaller output + the QEMU build into the release: the portable Windows folder
and ZIP, or the Linux AppImage.

    python packaging/assemble.py --version 0.2.0 [--sevenzip 7zr.exe] [--zip]       Windows
    python3 packaging/assemble.py --version 0.2.0 --sevenzip 7zzs \
        [--appimagetool appimagetool-x86_64.AppImage --runtime runtime-x86_64]        Linux

Layout of release/OpenStacks/ (Linux: release/OpenStacks.AppDir/OpenStacks/):
    OpenStacks[.exe], _internal/    front-end (PyInstaller, dist/OpenStacks)
    qemu/                           qemu-system-x86_64[.exe] + runtime libraries + pc-bios/
                                    + the open renderer (ost_renderer / shader translator)
    patches/                        image patches (run when an image is installed)
    licenses/                       license texts of the open renderer (gfxstream, ANGLE,
                                    LLVM libc++ on Linux)
    tools/7zr.exe | tools/7zz       7-Zip, unpacks downloaded images
    README.md, THIRD-PARTY.md, VERSION
Windows: data/ (images, instances, logs) is created next to the exe on first run.
Linux: the AppImage is read-only; data lives in $XDG_DATA_HOME/OpenStacks.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"

# Linux: QEMU's libraries that a desktop may lack, bundled in qemu/lib (RUNPATH $ORIGIN/lib).
# glib, libpulse, zlib & co. are on every desktop and come from the system.
BUNDLE_LIBS = ("libslirp.so", "libfdt.so", "libpixman-1.so")

DESKTOP = """[Desktop Entry]
Type=Application
Name=OpenStacks
Comment=Run BlueStacks' Android images on QEMU
Exec=OpenStacks
Icon=openstacks
Categories=Utility;Emulator;
Terminal=false
X-AppImage-Version={version}
"""

APPRUN = """#!/bin/sh
# AppImage entry: the PyInstaller front-end; QEMU and its libraries sit next to it
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/OpenStacks/OpenStacks" "$@"
"""


def copytree(src: Path, dst: Path) -> None:
    # symlinks as symlinks: PyInstaller's Linux layout links Qt libraries, and the ones the
    # spec drops (Quick, QML, PDF) leave dangling links behind, harmless but uncopyable
    shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def licenses(app: Path, q: Path) -> bool:
    """The open renderer's licenses, for the libraries that came along with QEMU."""
    lic = app / "licenses"
    for lib, text, name in (
            ("ost_renderer.dll", ROOT / "deps" / "gfxstream" / "LICENSE", "gfxstream-LICENSE.txt"),
            ("libost_renderer.so", ROOT / "deps" / "gfxstream" / "LICENSE",
             "gfxstream-LICENSE.txt"),
            ("libshadertranslator.dll", ROOT / "renderer" / "ANGLE-LICENSE.txt",
             "ANGLE-LICENSE.txt"),
            ("libshadertranslator.so", ROOT / "renderer" / "ANGLE-LICENSE.txt",
             "ANGLE-LICENSE.txt"),
            ("libc++.so", ROOT / "renderer" / "LIBCXX-LICENSE.txt", "LLVM-libc++-LICENSE.txt")):
        if (q / lib).exists():
            if not text.exists():
                print(f"missing {text} (license for {lib})", file=sys.stderr)
                return False
            lic.mkdir(exist_ok=True)
            shutil.copy2(text, lic / name)
    return True


def ldd_paths(binary: Path) -> dict[str, str]:
    """soname -> resolved path, from ldd."""
    out = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=True).stdout
    libs = {}
    for line in out.splitlines():
        parts = line.split("=>")
        if len(parts) == 2 and parts[1].strip().startswith("/"):
            libs[parts[0].strip()] = parts[1].split("(")[0].strip()
    return libs


def qemu_windows(q: Path, qbuild: Path, exe: Path, strip: str) -> None:
    shutil.copy2(exe, q)
    strip = strip or next((str(p) for p in (
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


def qemu_linux(q: Path, qbuild: Path, exe: Path, strip: str) -> bool:
    shutil.copy2(exe, q)
    qexe = q / exe.name
    subprocess.run([strip or "strip", "--strip-debug", str(qexe)], check=True)
    for lib in ("libost_renderer.so", "libshadertranslator.so", "libc++.so"):
        if (qbuild / lib).exists():
            shutil.copy2(qbuild / lib, q / lib)
    if (q / "libost_renderer.so").exists():
        subprocess.run([strip or "strip", "--strip-debug", str(q / "libost_renderer.so")],
                       check=True)
    # pc-bios: files + keymaps only (meson's subdirs hold symlinked sources)
    (q / "pc-bios" / "keymaps").mkdir(parents=True)
    for f in (qbuild / "pc-bios").iterdir():
        if f.is_file():
            shutil.copy2(f, q / "pc-bios" / f.name)
    for f in (qbuild / "pc-bios" / "keymaps").glob("*"):
        if f.is_file():
            shutil.copy2(f, q / "pc-bios" / "keymaps" / f.name)
    # the less common runtime libraries, found through RUNPATH $ORIGIN/lib
    libs = ldd_paths(exe)
    (q / "lib").mkdir()
    for soname, path in libs.items():
        if soname.startswith(BUNDLE_LIBS):
            shutil.copy2(path, q / "lib" / soname)
            print(f"bundled {soname}")
    missing = [b for b in BUNDLE_LIBS if not any(s.startswith(b) for s in libs)]
    if missing:
        print(f"note: QEMU does not link {', '.join(missing)}")
    if shutil.which("patchelf") is None:
        print("patchelf not found (needed to point QEMU at qemu/lib)", file=sys.stderr)
        return False
    subprocess.run(["patchelf", "--set-rpath", "$ORIGIN/lib", str(qexe)], check=True)
    print(f"QEMU: {qexe.stat().st_size / 2**20:.0f} MiB, RUNPATH $ORIGIN/lib")
    return True


def appimage(out: Path, app_name: str, version: str, tool: str, runtime: str,
             update_info: str = "") -> int:
    """release/OpenStacks.AppDir -> release/OpenStacks-<version>-x86_64.AppImage.

    update_info is embedded for AppImage updaters (AppManager, AppImageUpdate, Gear Lever...),
    e.g. gh-releases-zsync|owner|repo|latest|OpenStacks-*x86_64.AppImage.zsync; appimagetool
    also writes the matching <AppImage>.zsync when zsyncmake is on PATH (published with the
    release: delta updates)."""
    appdir = out / f"{app_name}.AppDir"
    (appdir / "AppRun").write_text(APPRUN, encoding="utf-8")
    (appdir / "AppRun").chmod(0o755)
    (appdir / "openstacks.desktop").write_text(DESKTOP.format(version=version), encoding="utf-8")
    icon = ROOT / "brand" / "png" / "openstacks-app-icon-512.png"
    if not icon.exists():
        print(f"missing {icon}", file=sys.stderr)
        return 1
    shutil.copy2(icon, appdir / "openstacks.png")
    shutil.copy2(icon, appdir / ".DirIcon")
    target = out / f"{app_name}-{version}-x86_64.AppImage"
    cmd = [tool, "--no-appstream"]
    if runtime:
        cmd += ["--runtime-file", runtime]
    if update_info:
        cmd += ["--updateinformation", update_info]
    env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
    subprocess.run(cmd + [str(appdir), str(target)], check=True, env=env)
    print(f"{target}: {target.stat().st_size / 2**20:.0f} MiB")
    if update_info:
        zs = target.with_name(target.name + ".zsync")
        if not zs.exists() and Path(target.name + ".zsync").exists():   # written to the cwd
            shutil.move(target.name + ".zsync", zs)
        print(f"update information: {update_info}")
        print(f"{zs}" if zs.exists() else "no .zsync written (zsyncmake not on PATH): "
              "updaters will download whole AppImages")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--dist", default=str(ROOT / "dist" / "OpenStacks"))
    ap.add_argument("--qemu", default=str(ROOT / ("build" if WINDOWS else "build-linux")),
                    help="qemu/build.cmd | build.sh output dir")
    ap.add_argument("--sevenzip", default="", help="7zr.exe (Windows) / 7zz (Linux) to bundle")
    ap.add_argument("--out", default=str(ROOT / "release"))
    ap.add_argument("--zip", action="store_true", help="Windows: also write the ZIP")
    ap.add_argument("--strip", default="", help="strip: drop debug info from the release "
                    "copy of QEMU (Windows: default MSYS2 ucrt64 strip if present)")
    ap.add_argument("--appimagetool", default="", help="Linux: build the AppImage with it")
    ap.add_argument("--runtime", default="", help="Linux: AppImage runtime file (offline)")
    ap.add_argument("--update-info", default="", help="Linux: AppImage update information")
    a = ap.parse_args()
    version = a.version.lstrip("vV")

    dist, qbuild, out = Path(a.dist), Path(a.qemu), Path(a.out)
    exe_name = "OpenStacks.exe" if WINDOWS else "OpenStacks"
    exe = qbuild / ("qemu-system-x86_64.exe" if WINDOWS else "qemu-system-x86_64")
    for need in (dist / exe_name, exe, qbuild / "pc-bios"):
        if not need.exists():
            print(f"missing {need}", file=sys.stderr)
            return 1
    if WINDOWS:
        app = out / "OpenStacks"
        shutil.rmtree(app, ignore_errors=True)
    else:
        shutil.rmtree(out / "OpenStacks.AppDir", ignore_errors=True)
        app = out / "OpenStacks.AppDir" / "OpenStacks"
    copytree(dist, app)

    q = app / "qemu"
    q.mkdir()
    if WINDOWS:
        qemu_windows(q, qbuild, exe, a.strip)
    elif not qemu_linux(q, qbuild, exe, a.strip):
        return 1
    if not licenses(app, q):
        return 1

    copytree(ROOT / "patches", app / "patches")
    if a.sevenzip:
        (app / "tools").mkdir(exist_ok=True)
        dst = app / "tools" / ("7zr.exe" if WINDOWS else "7zz")
        shutil.copy2(a.sevenzip, dst)
        if not WINDOWS:
            dst.chmod(0o755)
    for doc in ("README.md", "THIRD-PARTY.md", "LICENSE"):
        if (ROOT / doc).exists():
            shutil.copy2(ROOT / doc, app / doc)
    (app / "VERSION").write_text(version + "\n", encoding="utf-8")

    n = sum(1 for _ in app.rglob("*") if _.is_file())
    size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file())
    print(f"{app}: {n} files, {size / 2**20:.0f} MiB")
    if WINDOWS and a.zip:
        z = shutil.make_archive(str(out / f"OpenStacks-{version}-win64"),
                                "zip", root_dir=out, base_dir="OpenStacks")
        print(z)
    if not WINDOWS and a.appimagetool:
        return appimage(out, "OpenStacks", version, a.appimagetool, a.runtime, a.update_info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
