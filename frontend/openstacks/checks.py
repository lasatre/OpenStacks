"""Startup prerequisite checks (Qt-free): what a fresh machine needs to run an instance."""
from __future__ import annotations

import io
import os
import shutil
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

PLATFORM_TOOLS_URL = ("https://dl.google.com/android/repository/platform-tools-latest-"
                      + ("windows" if paths.WINDOWS else "linux") + ".zip")


@dataclass
class Problem:
    key: str          # "qemu" | "renderer" | "whpx" | "kvm" | "adb"
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


def kvm_problem() -> Problem | None:
    """Linux: /dev/kvm must exist and be read/writable by us (usually: the kvm group)."""
    if paths.WINDOWS:
        return None
    kvm = Path("/dev/kvm")
    if not kvm.exists():
        return Problem("kvm", "KVM is not available",
                       "/dev/kvm does not exist. Enable virtualization (VT-x / AMD-V) in the "
                       "firmware settings and make sure the kvm_intel or kvm_amd module is "
                       "loaded. Without it QEMU falls back to software emulation, which is "
                       "far too slow for Android.")
    if not os.access(kvm, os.R_OK | os.W_OK):
        return Problem("kvm", "No permission to use KVM",
                       "/dev/kvm is not accessible to your user. Add yourself to its group "
                       "(usually: sudo usermod -aG kvm $USER) and log in again.")
    return None


def run(global_settings, instances=()) -> list[Problem]:
    """Problems that stop instances from starting. With `instances`, the renderer check
    covers only the renderers they use (BlueStacks' DLL vs the open gfxstream one)."""
    out: list[Problem] = []
    qemu = global_settings.resolved_qemu_path()
    build = r"qemu\build.cmd" if paths.WINDOWS else "qemu/build.sh"
    if not qemu or not Path(qemu).exists():
        out.append(Problem("qemu", "QEMU not found",
                           f"Expected {paths.QEMU_EXE}. In a source checkout run {build}; "
                           "the release ships it in qemu" + os.sep + "."))
    used = {getattr(i, "renderer", "gfxstream") for i in instances} or {"gfxstream"}
    if "gfxstream" in used and qemu:
        missing = [f for f in paths.GFXSTREAM_FILES if not (Path(qemu).parent / f).exists()]
        if missing:
            script = (r"renderer\build_gfxstream.cmd" if paths.WINDOWS
                      else "renderer/build_gfxstream.sh")
            out.append(Problem("renderer", "The open renderer is missing",
                               f"{', '.join(missing)} not found next to QEMU. In a source "
                               f"checkout run {script}."))
    if "bluestacks" in used and paths.WINDOWS and not paths.RENDERER_DLL.exists():
        out.append(Problem("renderer", "BlueStacks 5 is not installed",
                           "An instance is set to BlueStacks' GPU renderer "
                           f"({paths.RENDERER_DLL.name}), which cannot be redistributed. "
                           "Switch it to the open renderer (Settings > GPU renderer), or install "
                           "BlueStacks 5 from bluestacks.com (it does not need to run)."))
    if whpx_available() is False:
        out.append(Problem("whpx", "Windows Hypervisor Platform is off",
                           "Enable it in 'Turn Windows features on or off' (Windows "
                           "Hypervisor Platform) and reboot. Without it QEMU falls back to "
                           "software emulation, which is far too slow for Android."))
    kvm = kvm_problem()
    if kvm:
        out.append(kvm)
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
    """Google's platform-tools zip -> <data>/deps/platform-tools/adb[.exe]."""
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
    adb = dest / "platform-tools" / f"adb{paths.EXE}"
    if not adb.exists():
        raise RuntimeError(f"platform-tools zip had no {adb.name}")
    if not paths.WINDOWS:                 # zipfile drops the execute bits
        for f in (dest / "platform-tools").iterdir():
            if f.is_file() and (f.suffix == "" or f.suffix == ".so"):
                f.chmod(f.stat().st_mode | 0o755)
    return adb
