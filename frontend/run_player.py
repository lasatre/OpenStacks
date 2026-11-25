#!/usr/bin/env python3
"""OpenStacks convenience launcher.

Equivalent to `python -m openstacks.player`: opens the instance manager,
which spawns per-instance player windows.

Double-clickable from Explorer (ensure python/pythonw is on PATH) or:
    python run_player.py
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _use_os_trust_store() -> None:
    """Verify HTTPS with the Windows certificate verifier instead of Python's snapshot of
    the root store. A fresh Windows install only ships a few roots and fetches the rest on
    demand, which only the OS verifier does; without this, downloads from BlueStacks' CDN
    or GitHub fail with CERTIFICATE_VERIFY_FAILED on new machines/VMs."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:          # missing package or unsupported platform: Python's defaults
        pass


def smoke(report: str = "") -> int:
    """--smoke [report.txt]: import everything and check the bundled files (CI, no GUI)."""
    import importlib
    lines, ok = [], True
    for m in ("config", "paths", "brand", "checks", "images", "magisk", "adb", "files",
              "qemu_engine", "native", "notify", "manager", "player"):
        try:
            importlib.import_module("openstacks." + m)
            lines.append(f"ok   import openstacks.{m}")
        except Exception as e:                  # report every failure, not just the first
            ok = False
            lines.append(f"FAIL import openstacks.{m}: {e!r}")
    from openstacks import __version__, paths
    from openstacks.images import find_7z
    need = {"qemu": paths.QEMU_EXE, "pc-bios": paths.QEMU_EXE.parent / "pc-bios" / "bios-256k.bin",
            "patches": paths.PATCH_DIR / "all" / "10_debloat_lawnchair.py",
            "brand icon": paths.brand("ico", "openstacks.ico"),
            "brand logo": paths.brand("png", "openstacks-logo-2000w.png")}
    for k, p in need.items():
        ok &= p.exists()
        lines.append(f"{'ok  ' if p.exists() else 'FAIL'} {k}: {p}")
    try:
        import truststore
        lines.append(f"ok   truststore {truststore.__version__} (OS certificate verification)")
    except ImportError as e:
        ok = False
        lines.append(f"FAIL truststore: {e!r}")
    # PySide6 6.12 drops a reference to True on every Signal.emit(); only Python 3.12+
    # (immortal True) survives that. A 3.10 build ran out of True and aborted at exit.
    from PySide6.QtCore import QObject, Signal

    class _Probe(QObject):
        sig = Signal(int)
    probe, before = _Probe(), sys.getrefcount(True)
    for _ in range(200):
        probe.sig.emit(1)
    leaked = before - sys.getrefcount(True) >= 100
    ok &= not leaked
    lines.append(f"{'FAIL' if leaked else 'ok  '} Signal.emit keeps True alive "
                 f"(Python {sys.version.split()[0]})")
    seven = find_7z()
    lines.append(f"{'ok  ' if seven else 'FAIL'} 7-Zip: {seven}")
    ok &= bool(seven)
    lines.append(f"version {__version__}, frozen={paths.FROZEN}, data={paths.DATA_DIR}")
    text = "\n".join(lines) + "\n"
    if report:
        with open(report, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0 if ok else 1


def _restore_library_path() -> None:
    """Frozen on Linux: PyInstaller's bootloader points LD_LIBRARY_PATH at its own bundle, and
    every child would inherit it (QEMU, adb, xdg-open: system programs picking up our copies
    of libraries). The loader read it at our own start already, so put the original back."""
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        orig = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig is not None:
            os.environ["LD_LIBRARY_PATH"] = orig
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)


def main() -> int:
    _restore_library_path()
    _use_os_trust_store()
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        return smoke(sys.argv[2] if len(sys.argv) > 2 else "")
    if len(sys.argv) > 2 and sys.argv[1] == "--install-image":
        # OpenStacks.exe --install-image Pie64 [log.txt]: download + patch, no GUI
        from openstacks import images
        log = open(sys.argv[3], "w", encoding="utf-8") if len(sys.argv) > 3 else sys.stdout
        last = {}

        def progress(stage, done, total):
            pct = int(done * 100 / total) if total else 0
            if last.get(stage) != pct // 10:
                last[stage] = pct // 10
                print(f"{stage} {pct}%", file=log, flush=True)
        try:
            out = images.install_image(sys.argv[2], progress=progress)
            print(f"ready: {out}", file=log, flush=True)
            return 0
        except Exception as e:
            print(f"FAILED: {e!r}", file=log, flush=True)
            return 1
    if len(sys.argv) > 2 and sys.argv[1] == "--launch":
        # OpenStacks.exe --launch <instance>: manager + start that instance (shortcuts)
        from PySide6.QtCore import QTimer
        from openstacks.manager import ManagerWindow, run_app
        app = run_app()
        win = ManagerWindow()
        win.show()
        name = " ".join(sys.argv[2:])          # unquoted names with spaces still work
        QTimer.singleShot(600, lambda: win.launch_by_name(name))
        return app.exec()
    from openstacks.player import main as player_main
    player_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
