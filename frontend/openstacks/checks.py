"""Startup prerequisite checks (Qt-free): what a fresh machine needs to run an instance."""
from __future__ import annotations

import io
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"


@dataclass
class Problem:
    key: str          # "qemu" | "renderer" | "whpx" | "adb"
    title: str
    detail: str
    fixable: bool = False   # an automatic fix exists (download)


def whpx_available() -> bool | None:
    """True/False from WHvGetCapability(HypervisorPresent); None if it can't be asked."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        dll = ctypes.WinDLL("WinHvPlatform.dll")
    except OSError:
        return False                      # feature not installed
    try:
        present = ctypes.c_int(0)
        written = ctypes.c_uint32(0)
        hr = dll.WHvGetCapability(0, ctypes.byref(present), ctypes.sizeof(present),
                                  ctypes.byref(written))
        return hr == 0 and bool(present.value)
    except (AttributeError, OSError):
        return None


def run(global_settings, instances=()) -> list[Problem]:
    """Problems that stop instances from starting. With `instances`, the renderer check
    covers only the renderers they use (BlueStacks' DLL vs the open gfxstream one)."""
    out: list[Problem] = []
    qemu = global_settings.resolved_qemu_path()
    if not qemu or not Path(qemu).exists():
        out.append(Problem("qemu", "QEMU not found",
                           f"Expected {paths.QEMU_EXE}. In a source checkout run "
                           "qemu\\build.cmd; the release ZIP ships it in qemu\\."))
    used = {getattr(i, "renderer", "bluestacks") for i in instances} or {"bluestacks"}
    if "gfxstream" in used and qemu:
        missing = [f for f in paths.GFXSTREAM_FILES if not (Path(qemu).parent / f).exists()]
        if missing:
            out.append(Problem("renderer", "The open renderer is missing",
                               f"{', '.join(missing)} not found next to QEMU. In a source "
                               "checkout run renderer\\build_gfxstream.cmd."))
    if "bluestacks" in used and not paths.RENDERER_DLL.exists():
        out.append(Problem("renderer", "BlueStacks 5 is not installed",
                           "OpenStacks uses BlueStacks 5's GPU renderer "
                           f"({paths.RENDERER_DLL.name}), which cannot be redistributed. "
                           "Install BlueStacks 5 from bluestacks.com (it does not need to "
                           "run), or set OST_RENDERER_DLL to the DLL's path."))
    if whpx_available() is False:
        out.append(Problem("whpx", "Windows Hypervisor Platform is off",
                           "Enable it in 'Turn Windows features on or off' (Windows "
                           "Hypervisor Platform) and reboot. Without it QEMU falls back to "
                           "software emulation, which is far too slow for Android."))
    adb = global_settings.resolved_adb_path()
    if not adb or not Path(adb).exists():
        out.append(Problem("adb", "adb not found",
                           "adb (Android platform-tools) is used for input, app installs, "
                           "files and status. OpenStacks can download Google's "
                           "platform-tools for you.", fixable=True))
    elif "HD-Adb" in adb:
        out.append(Problem("adb", "Only BlueStacks' HD-Adb was found",
                           "It fights newer adb servers ('offline' flapping). OpenStacks "
                           "can download Google's platform-tools instead.", fixable=True))
    return out


def download_platform_tools(progress=None) -> Path:
    """Google's platform-tools zip -> <data>/deps/platform-tools/adb.exe."""
    dest = paths.DEPS_DIR
    dest.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(PLATFORM_TOOLS_URL, headers={"User-Agent": "OpenStacks"})
    with urllib.request.urlopen(req, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf = io.BytesIO()
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            buf.write(chunk)
            if progress:
                progress(buf.tell(), total)
    shutil.rmtree(dest / "platform-tools", ignore_errors=True)
    with zipfile.ZipFile(buf) as z:
        z.extractall(dest)
    adb = dest / "platform-tools" / "adb.exe"
    if not adb.exists():
        raise RuntimeError("platform-tools zip had no adb.exe")
    return adb
