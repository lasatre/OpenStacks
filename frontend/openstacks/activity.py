"""Per-instance activity log: logs/<name>.activity.log, shown in the player's Activity tab.

What OpenStacks did for an instance (image download and patching, APK installs, root,
file transfer, screenshots, boot state), one timestamped line each.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import paths


def path(name: str) -> Path:
    return paths.LOGS_DIR / f"{name}.activity.log"


def append(name: str, text: str) -> None:
    try:
        p = path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            for line in str(text).splitlines() or [""]:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
    except OSError:
        pass
