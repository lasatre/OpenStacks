"""Optional per-instance Magisk root (Kitsune Magisk "system mode", done from the ramdisk).

Kitsune's system-mode install copies magisk binaries to /system/etc/init/magisk and adds an
init .rc that runs `magisk --setup-sbin` + the post-fs-data / service / boot-complete hooks.
Our Root.vhd is a shared read-only snapshot, so instead the boot ramdisk does the same thing
in rootfs on every boot, for instances that opted in:

  /data/adb/.ost_magisk   flag file (written by the player's Root button)
  /data/adb/magisk/       Magisk's own bin dir (magisk32/64, magiskpolicy, magiskinit,
                          stub.apk, busybox, scripts), filled by the Root button

-> /ost_magisk/ (copies) + /init.ost_magisk.rc, imported from /init.rc. Nothing happens
without the flag, so un-rooting = delete the flag (and the app). Root.vhd stays pristine.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib import Ramdisk  # noqa: E402

SCRIPT = r'''#!/boot/bin/sh
# OpenStacks image patch (patches/all/20_magisk.py): Magisk root, per instance (opt-in).
M=/data/adb/magisk
if [ -f /data/adb/.ost_magisk ] && [ -f $M/magiskpolicy ] && \
   { [ -f $M/magisk64 ] || [ -f $M/magisk32 ]; }; then
    D=/ost_magisk
    mkdir -p $D
    for f in magisk32 magisk64 magiskpolicy magiskinit stub.apk; do
        [ -f $M/$f ] && cp $M/$f $D/$f
    done
    chmod 700 $D $D/*
    printf 'SYSTEMMODE=true\nRECOVERYMODE=false\n' > $D/config
    bin=magisk32
    [ -f $D/magisk64 ] && bin=magisk64
    # BlueStacks runs with SELinux disabled: exec with a seclabel would fail there
    ctx=-
    [ -f /sys/fs/selinux/enforce ] && ctx=u:r:su:s0
    cat > /init.ost_magisk.rc <<EOF
on post-fs-data
    start logd
    exec $ctx root root -- $D/magiskpolicy --live --magisk
    exec $ctx root root -- $D/$bin --auto-selinux --setup-sbin $D /sbin
    exec $ctx root root -- /sbin/magisk --auto-selinux --post-fs-data
on nonencrypted
    exec $ctx root root -- /sbin/magisk --auto-selinux --service
on property:vold.decrypt=trigger_restart_framework
    exec $ctx root root -- /sbin/magisk --auto-selinux --service
on property:sys.boot_completed=1
    mkdir /data/adb/magisk 755
    exec $ctx root root -- /sbin/magisk --auto-selinux --boot-complete
on property:init.svc.zygote=restarting
    exec $ctx root root -- /sbin/magisk --auto-selinux --zygote-restart
on property:init.svc.zygote=stopped
    exec $ctx root root -- /sbin/magisk --auto-selinux --zygote-restart
EOF
    chmod 644 /init.ost_magisk.rc
    # hide BlueStacks' setuid su from PATH (Magisk reports "abnormal state" otherwise):
    # bind a copy of /system/xbin without it; xbin/bstk/su stays for BlueStacks' services
    # (the host also turns bst.enable_root_access off for Magisk instances)
    if [ -f /system/xbin/su ] && [ ! -d /ost_xbin ]; then
        mkdir /ost_xbin
        cp -a /system/xbin/. /ost_xbin/ && rm -f /ost_xbin/su && \
            mount -o bind /ost_xbin /system/xbin
    fi
    # Android <= 9 runs rootfs /init.rc; 10+ runs /system/etc/init/hw/init.rc, which still
    # imports rootfs files (/init.environ.rc ...): hook the first of those (exactly once)
    T=/init.rc
    if [ -f /system/etc/init/hw/init.rc ]; then
        T=`grep -o '^import /init\.[A-Za-z0-9_.]*\.rc' /system/etc/init/hw/init.rc | head -n 1 | cut -d ' ' -f 2`
    fi
    if [ -n "$T" ] && [ -f "$T" ]; then
        grep -q init.ost_magisk.rc "$T" || echo "import /init.ost_magisk.rc" >> "$T"
    else
        echo "ost: magisk: no rootfs init file to hook" > /dev/kmsg
    fi
    echo "ost: magisk enabled ($bin)" > /dev/kmsg
fi
'''

HOOK = '[ -f /boot/ost/magisk.sh ] && . /boot/ost/magisk.sh   # OpenStacks patch\n'
ANCHOR = 'log_echo "Starting Android"'


def apply(image_dir: Path, image: str) -> None:
    rd_path = image_dir / "initramfs.bin"
    rd = Ramdisk.load(rd_path)
    stage2 = rd.read("boot/stage2.sh").decode()
    if HOOK.strip() not in stage2:
        if ANCHOR not in stage2:
            raise ValueError(f"{image}: stage2.sh has no '{ANCHOR}' line to hook")
        stage2 = stage2.replace(ANCHOR, HOOK + ANCHOR, 1)
        rd.write("boot/stage2.sh", stage2.encode(), mode=0o755)
    rd.write("boot/ost/magisk.sh", SCRIPT.encode(), mode=0o755)
    rd.save(rd_path)
