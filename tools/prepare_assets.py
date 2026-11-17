# -*- coding: utf-8 -*-
"""Build OpenStacks' runtime assets (assets/<Image>/) - from BlueStacks' CDN or an install.

  python tools\\prepare_assets.py --list                       images and what is installed
  python tools\\prepare_assets.py --download Pie64             fetch + extract + patch + assets
  python tools\\prepare_assets.py [--image Pie64] [--engine DIR]
                                  from an installed BlueStacks 5 (Engine/<Image>)

Each asset dir holds kernel.bin + initramfs.bin (cut from fastboot.vdi), Root.vhd,
Data_template.vhdx (blank /data), the image's oem cfg and manifest.json. The pipeline lives
in frontend/openstacks/images.py (shared with the instance manager).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend"))

from openstacks import images  # noqa: E402

ENGINE = Path(r"C:\ProgramData\BlueStacks_nxt\Engine")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build OpenStacks image assets")
    ap.add_argument("--list", action="store_true", help="list downloadable/installed images")
    ap.add_argument("--download", metavar="IMAGE", help="download IMAGE from BlueStacks' CDN")
    ap.add_argument("--keep-download", action="store_true", help="keep the .exe in deps/images")
    ap.add_argument("--build", default="", help="BlueStacks client build to download from "
                    f"({', '.join(images.CLIENT_BUILDS)}; default: {images.DEFAULT_BUILD}, "
                    "newer only for images it lacks)")
    ap.add_argument("--repatch", metavar="IMAGE", help="re-apply patches/ to an installed "
                    "image (or 'all'), from its pristine ramdisk")
    ap.add_argument("--image", default="Pie64", help="image name for install mode")
    ap.add_argument("--engine", default=str(ENGINE), help="BlueStacks Engine dir (install mode)")
    ap.add_argument("--out", default=str(images.ASSETS_ROOT))
    a = ap.parse_args()
    out_root = Path(a.out)

    if a.list:
        have = set(images.installed_images(out_root))
        for info in images.catalog(a.build):
            mark = "installed" if info.name in have else f"download {info.size / 2**20:,.0f} MiB"
            print(f"  {info.name:<10} Android {info.android:<11} {mark:<20} (build {info.build})")
        return 0

    if a.repatch:
        names = images.installed_images(out_root) if a.repatch == "all" else [a.repatch]
        for n in names:
            print(f"{n}: patches applied: {', '.join(images.repatch(n, out_root)) or 'none'}")
        return 0

    if a.download:
        print(f"{a.download}: {images.image_info(a.download, a.build).url}")
        out = images.install_image(a.download, progress=images._cli_progress,
                                   keep_download=a.keep_download, out_root=out_root,
                                   build=a.build)
        print(f"assets ready: {out}")
        return 0

    src = Path(a.engine) / a.image
    if not (src / "fastboot.vdi").exists():
        print(f"BlueStacks image not found: {src}\\fastboot.vdi "
              f"(or use --download {a.image})", file=sys.stderr)
        return 1
    out = images.build_assets(src, a.image, out_root=out_root, move=False,
                              progress=images._cli_progress)
    print(f"assets ready: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
