"""Nougat: let the 4.9 kernel see the IDE disks.

The Nougat kernel has no ata_piix (BlueStacks attaches Root/Data to an AHCI controller),
and QEMU's AHCI returns garbage DMA data under WHPX. The kernel does have the built-in
ata_generic driver, which ignores IDE controllers unless told otherwise; its cmdline is
compiled in (our -append is ignored), so the ramdisk binds it to QEMU's PIIX3 IDE
controller (8086:7010) through sysfs new_id right before Root is mounted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib import Ramdisk  # noqa: E402

ANCHOR = 'log_echo "Mounting Android root file system /dev/sda1"'
HOOK = ('# OpenStacks patch (05_nougat_ide.py): ata_generic -> PIIX3 IDE, wait for sda1\n'
        # driver_data 2 = ATA_GEN_FORCE_DMA: without it ata_generic only uses DMA when the
        # BIOS set the BMDMA "drive DMA capable" bits (SeaBIOS doesn't) and falls back to
        # PIO, which is unusably slow under WHPX. Plain id as a fallback.
        'echo "8086 7010 ffffffff ffffffff 0 0 2" > /sys/bus/pci/drivers/ata_generic/new_id'
        ' 2>/dev/null || echo 8086 7010 > /sys/bus/pci/drivers/ata_generic/new_id\n'
        'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do\n'
        '    [ -e /sys/block/sda/sda1 ] && break; sleep 0.25\n'
        'done\n'
        'mdev -s\n')


def apply(image_dir: Path, image: str) -> None:
    if not image.startswith("Nougat"):
        return
    rd_path = image_dir / "initramfs.bin"
    rd = Ramdisk.load(rd_path)
    init = rd.read("boot/init").decode("latin1")
    if HOOK in init:
        return
    if ANCHOR not in init:
        raise ValueError(f"{image}: boot/init has no '{ANCHOR}' line to hook")
    rd.write("boot/init", init.replace(ANCHOR, HOOK + ANCHOR, 1).encode("latin1"), mode=0o755)
    rd.save(rd_path)
