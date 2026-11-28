"""Where game-controls files live.

Ours: `<config dir>/controls/<package>.cfg`, shared by every instance like BlueStacks'
InputMapper folder. A game with no file of ours uses BlueStacks' own file when BlueStacks
is installed on this machine (read-only; a save writes ours). "Import from BlueStacks"
copies them all over.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from .. import paths


def bluestacks_dirs() -> list[Path]:
    """BlueStacks 5 InputMapper folders on this machine (user edits first)."""
    if not paths.WINDOWS:
        return []
    root = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    out = []
    for prod in ("BlueStacks_nxt", "BlueStacks_nxt_cn", "BlueStacks"):
        base = root / prod / "Engine" / "UserData" / "InputMapper"
        for d in (base / "UserFiles", base):
            if d.is_dir():
                out.append(d)
    return out


def ours(package: str) -> Path:
    return paths.CONTROLS_DIR / f"{package}.cfg"


def find(package: str) -> Optional[Path]:
    """The controls file for a package: ours, else BlueStacks'."""
    if not package:
        return None
    p = ours(package)
    if p.is_file():
        return p
    for d in bluestacks_dirs():
        q = d / f"{package}.cfg"
        if q.is_file():
            return q
    return None


def import_dir(src: Path, overwrite: bool = False) -> tuple[int, int]:
    """Copy every <package>.cfg from a folder (BlueStacks' InputMapper, or an export).
    Returns (copied, skipped because we already have one)."""
    paths.CONTROLS_DIR.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for f in sorted(Path(src).glob("*.cfg")):
        dst = paths.CONTROLS_DIR / f.name
        if dst.exists() and not overwrite:
            skipped += 1
            continue
        shutil.copyfile(f, dst)
        copied += 1
    return copied, skipped


def import_bluestacks(overwrite: bool = False) -> tuple[int, int]:
    """Import every BlueStacks controls file. A file in UserFiles (the user's own edits)
    takes precedence over BlueStacks' built-in one for the same package."""
    copied = skipped = 0
    seen: set[str] = set()
    for d in bluestacks_dirs():
        for f in sorted(d.glob("*.cfg")):
            if f.name in seen:
                continue
            seen.add(f.name)
            dst = paths.CONTROLS_DIR / f.name
            if dst.exists() and not overwrite:
                skipped += 1
                continue
            paths.CONTROLS_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
            copied += 1
    return copied, skipped
