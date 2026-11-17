# -*- coding: utf-8 -*-
"""CLI / boot.py entry for the HD-Player-style bst.* host props.

The implementation lives in frontend/openstacks/bstprops.py (single copy);
this shim re-exports it for boot.py and the command line:

  python tools\\bstprops.py [--instance Pie64] [--out file] [k=v ...]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend"))

from openstacks.bstprops import (build_props, main, props_text,  # noqa: E402,F401
                                 read_conf, write_props_file)

if __name__ == "__main__":
    main()
