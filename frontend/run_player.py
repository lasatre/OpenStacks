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


def smoke(report: str = "") -> int:
    """--smoke [report.txt]: import everything and check the bundled files (CI, no GUI)."""
    import importlib
    lines, ok = [], True
    for m in ("config", "paths", "brand", "checks", "images", "magisk", "adb", "files",
              "qemu_engine", "native", "manager", "player"):
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


def main() -> int:
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
        QTimer.singleShot(600, lambda: win.launch_by_name(sys.argv[2]))
        return app.exec()
    from openstacks.player import main as player_main
    player_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
