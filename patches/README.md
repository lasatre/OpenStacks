# Image patches

Run by `openstacks.images.apply_patches()` after a downloaded image is extracted and before
its assets are built (`tools/prepare_assets.py --download`, or "New instance" in the manager).

- `patches/all/*.py` run for every image, then `patches/<Image>/*.py` (e.g. `patches/Pie64/`),
  each folder in file-name order (prefix with numbers: `10_remove_ads.py`, `20_launcher.py`).
- Each script defines `apply(image_dir: pathlib.Path, image: str) -> None` and edits the
  extracted files in `image_dir` (`Root.vhd`, `fastboot.vdi`, `Data.vhdx`, `oem_<Image>.cfg`,
  `Android.bstk.in`). Files starting with `_` are skipped (helpers).
- The applied patch names are recorded in `assets/<Image>/manifest.json`.

- Most patches edit the boot ramdisk (`initramfs.bin`, cut from fastboot.vdi before the
  patches run); `_lib.Ramdisk` edits its cpio in place, `_lib.fetch` downloads pinned files.
  `initramfs.orig.bin` keeps the pristine ramdisk, so
  `python tools/prepare_assets.py --repatch <Image>|all` re-applies the patches without a
  download (stop QEMU first).

Current patches:

- `all/05_nougat_ide.py` - Nougat only: bind ata_generic to the PIIX IDE controller (the
  4.9 kernel has no ata_piix).
- `all/10_debloat_lawnchair.py` - remove BlueStacks' launcher, store, rewards, now.gg
  accounts/ads/billing; install Lawnchair as the only launcher (version per image).
- `all/20_magisk.py` - start Magisk at boot for instances rooted with the player's Root
  button (opt-in per instance via /data/adb/.ost_magisk; no-op otherwise).
