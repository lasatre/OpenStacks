"""OpenStacks wallpaper instead of BlueStacks' (patches/assets/wallpaper-*.png).

BlueStacks' wallpaper is the framework default in the system image. Instead of rebuilding
framework resources, the boot ramdisk writes Android's per-user wallpaper files before
Android starts:

  /data/system/users/0/wallpaper_orig, wallpaper, wallpaper_info.xml

How the image is shown differs by Android version (images from
brand/openstacks_wallpaper.py --android):
- Android 7/9 draw the stored `wallpaper` file 1:1, and Launcher3-based launchers (Lawnchair)
  ask for a 2M x M wallpaper (M = longer screen side); Android's own boot-time crop comes out
  as a 2x1 pixel image here. So a ready-made 2:1 image per common M is shipped
  (wallpaper-<M>.png) and the one closest to the instance's M is used; it looks right in
  both orientations.
- Android 10+ scale the wallpaper to the screen: one 16:9 and one 9:16 image
  (wallpaper-land/port.png), picked by the instance's orientation.
The screen size comes from bst.fb_width / fb_height (exported as BST_FB_* by
populate_bst_conf).

It writes only when no wallpaper is set, or when the current one is a previous OpenStacks
wallpaper (a new image or a resolution change replaces it); a wallpaper the user picked is
left alone. The md5 of the image we set is kept in .ost_wallpaper.
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib import Ramdisk  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "assets"

SCRIPT = r'''#!/boot/bin/sh
# OpenStacks image patch (patches/all/15_wallpaper.py): default wallpaper, per instance.
D=/data/system/users/0
@SELECT@
if [ -n "$W" ] && [ -f $W ]; then
    new=`md5sum $W | cut -d ' ' -f 1`
    cur=`md5sum $D/wallpaper 2>/dev/null | cut -d ' ' -f 1`
    ours=`cat $D/.ost_wallpaper 2>/dev/null`
    if [ -z "$cur" ] || { [ "$cur" = "$ours" ] && [ "$cur" != "$new" ]; }; then
        for d in /data/system /data/system/users; do
            [ -d $d ] || { mkdir $d; chown 1000:1000 $d; chmod 775 $d; }
        done
        [ -d $D ] || { mkdir $D; chown 1000:1000 $D; chmod 700 $D; }
        cp $W $D/wallpaper_orig
        cp $W $D/wallpaper
        cat > $D/wallpaper_info.xml <<EOF
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<wp id="1" width="$OST_WW" height="$OST_WH" cropLeft="0" cropTop="0" cropRight="$OST_WW" cropBottom="$OST_WH" name="" backup="true" />
EOF
        echo $new > $D/.ost_wallpaper
        chown 1000:1000 $D/wallpaper_orig $D/wallpaper $D/wallpaper_info.xml $D/.ost_wallpaper
        chmod 600 $D/wallpaper_orig $D/wallpaper $D/wallpaper_info.xml $D/.ost_wallpaper
        # SELinux images (Nougat): label like Android does (no-op where SELinux is off)
        if [ -f /sys/fs/selinux/enforce ] && [ -x /system/bin/toybox ]; then
            /system/bin/toybox chcon u:object_r:wallpaper_file:s0 $D/wallpaper_orig $D/wallpaper 2>/dev/null
            /system/bin/toybox chcon u:object_r:system_data_file:s0 $D/wallpaper_info.xml $D/.ost_wallpaper 2>/dev/null
        fi
        echo "ost: wallpaper set ($OST_WW x $OST_WH)" > /dev/kmsg
    fi
fi
rm -f /boot/ost/wallpaper-*.img          # the ramdisk stays in RAM: free the images
'''

# Android 7/9: closest 2:1 image to M (longer screen side)
SELECT_1TO1 = r'''M=${BST_FB_WIDTH:-1600}
[ "${BST_FB_HEIGHT:-0}" -gt "$M" ] && M=$BST_FB_HEIGHT
W= OST_WW= OST_WH= best=99999
for s in @SIZES@; do
    diff=$(( s > M ? s - M : M - s ))
    if [ $diff -lt $best ]; then best=$diff; W=/boot/ost/wallpaper-$s.img; OST_WH=$s; OST_WW=$(( s * 2 )); fi
done'''

# Android 10+: by orientation
SELECT_COVER = r'''W=/boot/ost/wallpaper-land.img; OST_WW=@LW@; OST_WH=@LH@
if [ "${BST_FB_HEIGHT:-0}" -gt "${BST_FB_WIDTH:-1600}" ]; then
    W=/boot/ost/wallpaper-port.img; OST_WW=@PW@; OST_WH=@PH@
fi'''
DRAWS_1TO1 = ("Nougat32", "Nougat64", "Pie64")    # Android 7/9 (see above)

HOOK = '[ -f /boot/ost/wallpaper.sh ] && . /boot/ost/wallpaper.sh   # OpenStacks patch\n'
ANCHOR = 'log_echo "Starting Android"'


def png_size(data: bytes) -> tuple[int, int]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("wallpaper images must be PNG")
    return struct.unpack(">II", data[16:24])


def apply(image_dir: Path, image: str) -> None:
    def load(name):
        p = ASSETS / name
        return p.read_bytes() if p.exists() else None

    ship: dict[str, bytes] = {}
    if image in DRAWS_1TO1:
        sizes = []
        for p in sorted(ASSETS.glob("wallpaper-*.png")):
            m = re.fullmatch(r"wallpaper-(\d+)\.png", p.name)
            if m:
                data = p.read_bytes()
                if png_size(data) != (2 * int(m.group(1)), int(m.group(1))):
                    raise ValueError(f"{p.name}: expected a 2:1 image of height {m.group(1)}")
                ship[f"wallpaper-{m.group(1)}.img"] = data
                sizes.append(m.group(1))
        select = SELECT_1TO1.replace("@SIZES@", " ".join(sorted(sizes, key=int)))
    else:
        land, port = load("wallpaper-land.png"), load("wallpaper-port.png")
        if land:
            ship["wallpaper-land.img"] = land
            port = port or land
            ship["wallpaper-port.img"] = port
            (lw, lh), (pw, ph) = png_size(land), png_size(port)
            select = (SELECT_COVER.replace("@LW@", str(lw)).replace("@LH@", str(lh))
                      .replace("@PW@", str(pw)).replace("@PH@", str(ph)))
    if not ship:
        return                                  # no wallpaper shipped: keep BlueStacks'
    rd_path = image_dir / "initramfs.bin"
    rd = Ramdisk.load(rd_path)
    stage2 = rd.read("boot/stage2.sh").decode()
    if HOOK.strip() not in stage2:
        if ANCHOR not in stage2:
            raise ValueError(f"{image}: stage2.sh has no '{ANCHOR}' line to hook")
        stage2 = stage2.replace(ANCHOR, HOOK + ANCHOR, 1)
        rd.write("boot/stage2.sh", stage2.encode(), mode=0o755)
    for name, data in ship.items():
        rd.write(f"boot/ost/{name}", data, mode=0o644)
    rd.write("boot/ost/wallpaper.sh", SCRIPT.replace("@SELECT@", select).encode(), mode=0o755)
    rd.save(rd_path)