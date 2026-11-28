"""Where OpenStacks keeps its files, for a source checkout and for the packaged build.

Source checkout (python frontend/run_player.py):
    <repo>/build/qemu-system-x86_64.exe     QEMU (qemu/build.cmd)          Windows
    <repo>/build-linux/qemu-system-x86_64   QEMU (qemu/build.sh)           Linux
    <repo>/assets, instances, deps          images, per-instance disks, download caches
    <repo>/frontend/instances.conf, logs    settings and logs
    ($OPENSTACKS_HOME, when set, holds all of these instead of <repo>)
    <repo>/patches, <repo>/brand            image patches, branding

Packaged build (Windows: OpenStacks.exe in a portable folder; Linux: the AppImage; see
packaging/):
    <app>/qemu/qemu-system-x86_64[.exe]     QEMU + runtime libraries + pc-bios
    <app>/patches, <app>/tools/7zr.exe|7zz  image patches, 7-Zip
    <app>/_internal/brand                   branding (PyInstaller bundle)
    <data>/assets, instances, deps, logs, screenshots, instances.conf
where <data> = %OPENSTACKS_HOME% / $OPENSTACKS_HOME if set, else <app>/data on Windows (the
folder stays portable) and $XDG_DATA_HOME/OpenStacks (~/.local/share/OpenStacks) on Linux
(an AppImage's own folder is read-only).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
WINDOWS = sys.platform == "win32"
EXE = ".exe" if WINDOWS else ""


def _user_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "OpenStacks"


if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
    DATA_DIR = Path(os.environ.get("OPENSTACKS_HOME")
                    or (APP_DIR / "data" if WINDOWS else _user_data_dir()))
    CONF_DIR = DATA_DIR                      # instances.conf, logs/, screenshots/
    QEMU_EXE = APP_DIR / "qemu" / f"qemu-system-x86_64{EXE}"
    PATCH_DIR = APP_DIR / "patches"
    BRAND_DIR = BUNDLE_DIR / "brand"
    TOOLS_DIR = APP_DIR / "tools"
else:
    APP_DIR = Path(__file__).resolve().parents[2]          # <repo>
    BUNDLE_DIR = APP_DIR
    DATA_DIR = Path(os.environ.get("OPENSTACKS_HOME") or APP_DIR)
    # OPENSTACKS_HOME also moves settings + logs (e.g. a second OS on the same checkout)
    CONF_DIR = DATA_DIR if os.environ.get("OPENSTACKS_HOME") else APP_DIR / "frontend"
    QEMU_EXE = APP_DIR / ("build" if WINDOWS else "build-linux") / f"qemu-system-x86_64{EXE}"
    PATCH_DIR = APP_DIR / "patches"
    BRAND_DIR = APP_DIR / "brand"
    TOOLS_DIR = APP_DIR / "tools"

ASSETS_DIR = DATA_DIR / "assets"
INSTANCES_DIR = DATA_DIR / "instances"
DEPS_DIR = DATA_DIR / "deps"                 # download caches (images, Magisk, adb)
LOGS_DIR = CONF_DIR / "logs"
SCREENSHOTS_DIR = CONF_DIR / "screenshots"
CONF_FILE = CONF_DIR / "instances.conf"
CONTROLS_DIR = CONF_DIR / "controls"      # game controls: <package>.cfg (BlueStacks format)

# BlueStacks 5's GPU renderer (Windows only). Not redistributable: the packaged build needs
# BlueStacks 5 installed (or OST_RENDERER_DLL pointing at a copy of its install folder's DLL).
RENDERER_DLL = Path(os.environ.get("OST_RENDERER_DLL")
                    or r"C:\Program Files\BlueStacks_nxt\libOpenglRender.dll")
# Linux has only the open renderer
RENDERERS = ("gfxstream", "bluestacks") if WINDOWS else ("gfxstream",)

# The open renderer (instance renderer = gfxstream): built by renderer/build_gfxstream.cmd
# (.sh on Linux), loaded by QEMU from its own folder.
GFXSTREAM_FILES = (("ost_renderer.dll", "libshadertranslator.dll") if WINDOWS else
                   ("libost_renderer.so", "libshadertranslator.so", "libc++.so"))


def brand(*parts: str) -> Path:
    return BRAND_DIR.joinpath(*parts)
