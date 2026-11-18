"""Find running QEMU instances by fact: each one is started with
``-name openstacks-<instance>``, so the process command line says which instance
it is, independent of pid files or of what the user clicked.

    tagged_qemus() -> {instance name: [pid, ...]}

Windows: Toolhelp snapshot for the qemu-system-* processes, then each one's
command line via NtQueryInformationProcess(ProcessCommandLineInformation),
which needs only PROCESS_QUERY_LIMITED_INFORMATION (Windows 8.1+).
"""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

TAG_PREFIX = "openstacks-"
_NAME_RE = re.compile(r'(?:^|\s)-name\s+"?openstacks-([^",\s]+)')


def _instance_from_cmdline(cmd: str) -> str | None:
    m = _NAME_RE.search(cmd)
    return m.group(1) if m else None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _INVALID = wintypes.HANDLE(-1).value

    def _qemu_pids() -> list[int]:
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)        # TH32CS_SNAPPROCESS
        if not snap or snap == _INVALID:
            return []
        out = []
        try:
            e = _PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                if e.szExeFile.lower().startswith("qemu-system"):
                    out.append(int(e.th32ProcessID))
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return out

    def cmdline(pid: int) -> str | None:
        h = _k32.OpenProcess(0x1000, False, pid)            # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            size = wintypes.ULONG(0)
            _ntdll.NtQueryInformationProcess(h, 60, None, 0, ctypes.byref(size))
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if _ntdll.NtQueryInformationProcess(h, 60, buf, size, ctypes.byref(size)) != 0:
                return None
            us = _UNICODE_STRING.from_buffer(buf)
            if not us.Buffer or not us.Length:
                return None
            return ctypes.wstring_at(us.Buffer, us.Length // 2)
        finally:
            _k32.CloseHandle(h)
else:
    def _qemu_pids() -> list[int]:
        out = []
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    if "qemu-system" in (d / "exe").resolve().name:
                        out.append(int(d.name))
                except OSError:
                    pass
        return out

    def cmdline(pid: int) -> str | None:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return " ".join(shlex.quote(a) for a in raw.decode(errors="replace").split("\0") if a)


def tagged_qemus() -> dict[str, list[int]]:
    """{instance name: [pids]} of every running QEMU started by OpenStacks."""
    out: dict[str, list[int]] = {}
    for pid in _qemu_pids():
        cmd = cmdline(pid)
        name = _instance_from_cmdline(cmd) if cmd else None
        if name:
            out.setdefault(name, []).append(pid)
    return out


def tags_of(pid: int) -> set[str]:
    """Instance names `pid` is tagged with (empty for foreign/unreadable processes)."""
    cmd = cmdline(pid)
    name = _instance_from_cmdline(cmd) if cmd else None
    return {name} if name else set()


def instance_pids(name: str) -> list[int]:
    return tagged_qemus().get(name, [])
