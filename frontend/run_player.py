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


def main() -> int:
    from openstacks.player import main as player_main
    player_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
