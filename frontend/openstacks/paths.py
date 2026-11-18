"""Where OpenStacks keeps its files, for a source checkout and for the packaged build.

Source checkout (python frontend/run_player.py):
    <repo>/build/qemu-system-x86_64.exe     QEMU (qemu/build.cmd)
    <repo>/assets, instances, deps          images, per-instance disks, download caches
    <repo>/frontend/instances.conf, logs    settings and logs
    <repo>/patches, <repo>/brand            image patches, branding

Packaged build (OpenStacks.exe, a portable folder; see packaging/):
    <app>/qemu/qemu-system-x86_64.exe       QEMU + runtime DLLs + pc-bios
    <app>/patches, <app>/tools/7zr.exe      image patches, 7-Zip
    <app>/_internal/brand                   branding (PyInstaller bundle)
    <data>/assets, instances, deps, logs, screenshots, instances.conf
where <data> = %OPENSTACKS_HOME% if set, else <app>/data (the folder stays portable).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
    DATA_DIR = Path(os.environ.get("OPENSTACKS_HOME") or (APP_DIR / "data"))
    CONF_DIR = DATA_DIR                      # instances.conf, logs/, screenshots/
    QEMU_EXE = APP_DIR / "qemu" / "qemu-system-x86_64.exe"
    PATCH_DIR = APP_DIR / "patches"
    BRAND_DIR = BUNDLE_DIR / "brand"
    TOOLS_DIR = APP_DIR / "tools"
else:
    APP_DIR = Path(__file__).resolve().parents[2]          # <repo>
    BUNDLE_DIR = APP_DIR
    DATA_DIR = Path(os.environ.get("OPENSTACKS_HOME") or APP_DIR)
    CONF_DIR = APP_DIR / "frontend"
    QEMU_EXE = APP_DIR / "build" / "qemu-system-x86_64.exe"
    PATCH_DIR = APP_DIR / "patches"
    BRAND_DIR = APP_DIR / "brand"
    TOOLS_DIR = APP_DIR / "tools"

ASSETS_DIR = DATA_DIR / "assets"
INSTANCES_DIR = DATA_DIR / "instances"
DEPS_DIR = DATA_DIR / "deps"                 # download caches (images, Magisk, adb)
LOGS_DIR = CONF_DIR / "logs"
SCREENSHOTS_DIR = CONF_DIR / "screenshots"
CONF_FILE = CONF_DIR / "instances.conf"

# BlueStacks 5's GPU renderer. Not redistributable: the packaged build needs BlueStacks 5
# installed (or OST_RENDERER_DLL pointing at a copy of its install folder's DLL).
RENDERER_DLL = Path(os.environ.get("OST_RENDERER_DLL")
                    or r"C:\Program Files\BlueStacks_nxt\libOpenglRender.dll")


def brand(*parts: str) -> Path:
    return BRAND_DIR.joinpath(*parts)
