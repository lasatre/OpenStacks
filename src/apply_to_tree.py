#!/usr/bin/env python3
"""
Apply the BlueStacks virtual-PCI-device add-on to a QEMU source tree.

Usage:
    python apply_to_tree.py [path-to-qemu-source-tree]

Default tree: ..\\qemu-11.1.0 (relative to this script).
The operation is idempotent: running it twice does not duplicate hooks.
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TREE = os.path.normpath(os.path.join(HERE, "..", "..", "qemu_bst", "qemu-11.1.0"))

SOURCES = ["bst_vmsg.c", "bst_pgaipc.c", "bst_audio.c", "hvmem.c", "vmmdev.c",
           "gr_render.c", "gr_render.h"]

KCONFIG_BLOCK = """
config BST_VMSG
    bool
    default y if PCI_DEVICES
    depends on PCI

config BST_PGAIPC
    bool
    default y if PCI_DEVICES
    depends on PCI

config BST_AUDIO
    bool
    default y if PCI_DEVICES
    depends on PCI

config HVMEM
    bool
    default y if PCI_DEVICES
    depends on PCI

config VMMDEV
    bool
    default y if PCI_DEVICES
    depends on PCI
"""

MESON_BLOCK = """
# BlueStacks paravirtual PCI devices
system_ss.add(when: 'CONFIG_BST_VMSG', if_true: files('bst_vmsg.c'))
system_ss.add(when: 'CONFIG_BST_PGAIPC', if_true: files('bst_pgaipc.c'))
system_ss.add(when: 'CONFIG_BST_AUDIO', if_true: files('bst_audio.c'))
system_ss.add(when: 'CONFIG_HVMEM', if_true: files('hvmem.c'))
system_ss.add(when: 'CONFIG_VMMDEV', if_true: files('vmmdev.c'))
"""

VMMDEV_KCONFIG_ONLY = """
config VMMDEV
    bool
    default y if PCI_DEVICES
    depends on PCI
"""

VMMDEV_MESON_ONLY = "system_ss.add(when: 'CONFIG_VMMDEV', if_true: files('vmmdev.c'))\n"


def main() -> int:
    tree = os.path.normpath(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TREE
    hw_misc = os.path.join(tree, "hw", "misc")
    if not os.path.isdir(hw_misc):
        print(f"error: {hw_misc} not found - is {tree} a QEMU tree?", file=sys.stderr)
        return 1

    src_dir = os.path.join(HERE, "hw", "misc")
    for name in SOURCES:
        shutil.copyfile(os.path.join(src_dir, name), os.path.join(hw_misc, name))
        print(f"copied hw/misc/{name}")

    kconfig_path = os.path.join(hw_misc, "Kconfig")
    with open(kconfig_path, "r", encoding="utf-8") as f:
        kconfig = f.read()
    if "config VMMDEV" not in kconfig:
        with open(kconfig_path, "r+", encoding="utf-8") as f:
            kconfig = f.read()
            if "config BST_VMSG" not in kconfig:
                f.write(KCONFIG_BLOCK)
                print("patched hw/misc/Kconfig (full block)")
            else:
                f.write(VMMDEV_KCONFIG_ONLY)
                print("patched hw/misc/Kconfig (VMMDEV block)")
    else:
        print("hw/misc/Kconfig already patched")

    meson_path = os.path.join(hw_misc, "meson.build")
    with open(meson_path, "r", encoding="utf-8") as f:
        meson = f.read()
    if "CONFIG_VMMDEV" not in meson:
        with open(meson_path, "r+", encoding="utf-8") as f:
            meson = f.read()
            if "CONFIG_BST_VMSG" not in meson:
                f.write(MESON_BLOCK)
                print("patched hw/misc/meson.build (full block)")
            else:
                f.write(VMMDEV_MESON_ONLY)
                print("patched hw/misc/meson.build (VMMDEV line)")
    else:
        print("hw/misc/meson.build already patched")

    # gr_render.c rides CONFIG_BST_PGAIPC; append its line if missing
    # (idempotent; trees patched by earlier runs need only this part).
    with open(meson_path, "r", encoding="utf-8") as f:
        meson = f.read()
    if "gr_render.c" not in meson:
        with open(meson_path, "a", encoding="utf-8") as f:
            f.write("system_ss.add(when: 'CONFIG_BST_PGAIPC', "
                    "if_true: files('gr_render.c'))\n")
        print("patched hw/misc/meson.build (gr_render.c line)")
    else:
        print("hw/misc/meson.build already has gr_render.c")

    print("done - now run configure and ninja")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
