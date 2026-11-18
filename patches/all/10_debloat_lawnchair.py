"""Strip BlueStacks' launcher, store, ads, rewards and billing hooks; install Lawnchair.

How: the BlueStacks extras live in the image's prebundled dataFS and are copied into
/data/downloads and /data/priv-downloads (BlueStacks' package manager installs from those
dirs). This patch adds /boot/ost/ost_boot.sh to the boot ramdisk and runs it from
stage2.sh right before Android's init, on EVERY boot (fresh or existing /data): it deletes
the stripped app dirs (and any /data/app updates of them) and places Lawnchair in
/data/downloads, so it is the only launcher. Root.vhd is not modified.

Kept on purpose: Google apps (Play Store/services, GSF), Chrome, BlueStacks' location
provider, the per-app compatibility rules (config.db), and the system-image services the
host integration relies on (BstCommandProcessor, com.bluestacks.settings).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib import Ramdisk, deps_dir, fetch  # noqa: E402

CACHE = deps_dir("lawnchair")

# Lawnchair 15 beta 3 needs Android 8+ (minSdk 26, ships x86_64 code); 1.2 covers Nougat.
LAWNCHAIR = {
    "modern": ("app.lawnchair",
               "https://github.com/LawnchairLauncher/lawnchair/releases/download/"
               "v15.0.0-beta3.0/Lawnchair.15.0.0.Beta.3.0.apk",
               "d4200d0985169fd79ba1bd225d653f2a2fe7b50aa07cb0d05ca64c7623f86059"),
    "14": ("app.lawnchair",
           "https://github.com/LawnchairLauncher/lawnchair/releases/download/"
           "v14.0.0-beta3/Lawnchair.14.0.0.Beta.3.apk",
           "b1cc5bc468bbc5fc26a0107e348149dac349fd0247dca40f290bfd11b78957db"),
    "12": ("app.lawnchair",
           "https://github.com/LawnchairLauncher/lawnchair/releases/download/"
           "v12.1.0-alpha.4/Lawnchair.12.1.0.Alpha.4.apk",
           "db2c5ef7af633367b155dd7c132ddf5559a126456e69a4f029951afa2271c364"),
    "legacy": ("ch.deletescape.lawnchair",
               "https://github.com/LawnchairLauncher/lawnchair/releases/download/"
               "1.2.0.1884/Lawnchair-1.2.0.1884.apk",
               "47525c98210fe738414ec2d77d170a51f0ced55ed624499ae2ab9cf804d4865d"),
}

# per-image build (tested): override with env OST_LAWNCHAIR=modern|14|12|legacy
VARIANT = {"Nougat32": "legacy", "Nougat64": "legacy",
           "Pie64": "12"}   # 14/15 crash on Android 9 (Resources$NotFoundException)

STRIP = [
    # launchers
    "com.uncube.launcher3", "com.bluestacks.bsxlauncher",
    # BlueStacks store / home / rewards / file manager
    "com.bluestacks.gamecenter", "com.bluestacks.home", "com.bluestacks.nowgg",
    "com.bluestacks.piggy", "com.bluestacks.quest", "com.bluestacks.filemanager",
    # now.gg accounts, ads, billing interception
    "gg.now.accounts", "gg.now.ads.service", "gg.now.billing.interceptor",
    "gg.now.billing.service2", "now.gg.billing.service", "com.bluestacks.billing.service",
    # ad config
    "admob_user_agent.xml",
]

BOOT_SCRIPT = r'''#!/boot/bin/sh
# OpenStacks image patch (patches/all/10_debloat_lawnchair.py) - runs every boot from
# stage2.sh before Android's init: strip BlueStacks extras, keep Lawnchair installed.
ost_log() { echo "ost: $*" > /dev/kmsg; }
for p in @STRIP@; do
    for d in /data/downloads/$p /data/priv-downloads/$p /data/app/$p-* /data/app/*/$p-*; do
        if [ -e "$d" ]; then
            rm -rf "$d" && ost_log "removed $d"
        fi
    done
done
if [ -f /boot/ost/launcher.apk ]; then
    d=/data/downloads/@LPKG@
    new=`md5sum /boot/ost/launcher.apk | cut -d ' ' -f 1`
    old=`md5sum $d/@LPKG@.apk 2>/dev/null | cut -d ' ' -f 1`
    if [ "$new" != "$old" ]; then
        mkdir -p $d
        cp /boot/ost/launcher.apk $d/@LPKG@.apk
        chown -R 1000:1000 $d
        chmod 775 $d
        chmod 644 $d/@LPKG@.apk
        ost_log "installed @LPKG@ launcher"
    fi
fi
'''

HOOK = '[ -f /boot/ost/ost_boot.sh ] && . /boot/ost/ost_boot.sh   # OpenStacks patch\n'
ANCHOR = 'log_echo "Starting Android"'


def apply(image_dir: Path, image: str) -> None:
    rd_path = image_dir / "initramfs.bin"
    if not rd_path.exists():
        raise FileNotFoundError(f"{rd_path} (patch needs the boot ramdisk)")
    kind = os.environ.get("OST_LAWNCHAIR") or VARIANT.get(image, "modern")
    pkg, url, sha = LAWNCHAIR[kind]
    apk = fetch(url, CACHE / Path(url).name, sha)

    rd = Ramdisk.load(rd_path)
    stage2 = rd.read("boot/stage2.sh").decode()
    if HOOK.strip() not in stage2:
        if ANCHOR not in stage2:
            raise ValueError(f"{image}: stage2.sh has no '{ANCHOR}' line to hook")
        stage2 = stage2.replace(ANCHOR, HOOK + ANCHOR, 1)
        rd.write("boot/stage2.sh", stage2.encode(), mode=0o755)
    script = BOOT_SCRIPT.replace("@STRIP@", " ".join(STRIP)).replace("@LPKG@", pkg)
    rd.write("boot/ost/ost_boot.sh", script.encode(), mode=0o755)
    rd.write("boot/ost/launcher.apk", apk.read_bytes(), mode=0o644)
    rd.save(rd_path)
