"""Read GPU-written buffers back from the host (glMapBufferRange), for every app.

BlueStacks' guest GLES encoder (libGLESv2_enc.so, GL2Encoder::s_glMapBufferRange) only asks
the host for a buffer's contents when the app is on its per-app "GLMBRH" list
(BstFilterAppsManager::isGlMapBufferRangeHost, rules from /data/downloads/.config.db; about
40 games). For every other app a mapped buffer returns the guest's own copy: whatever the app
last uploaded, never what the GPU wrote. Anything that reads compute-shader or transform-
feedback output then sees zeros or stale data - e.g. io.heseri.car builds its terrain mesh
and physics collider with a compute shader and read back nothing: no ground, the car falls
through the map ("cleaning the mesh failed" from PhysX).

The rule service also reads /data/downloads/.config_user.db, and a "*" package rule applies
to every app (verified on Pie64 and Rvc64; logcat "check GLMBRH with pkg[...] with res[1]").
This patch installs that file every boot from the ramdisk. The host round trip happens only
for reads and for writes that keep the old contents, which is upstream goldfish's normal
behaviour.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _lib import Ramdisk  # noqa: E402

RULES = {
    "packages": [
        {
            "pkgname": "*",
            "rules": [
                {
                    "GLMBRH": "true",
                    "countries": "*",
                    "oems": "*",
                    "prodversion_gte": "0.0.0.0",
                }
            ],
        }
    ]
}

BOOT_SCRIPT = r'''#!/boot/bin/sh
# OpenStacks image patch (patches/all/25_gl_map_host.py) - runs every boot from stage2.sh
# before Android's init: per-app GL rules for all apps (buffer read-back from the host).
src=/boot/ost/config_user.db
dst=/data/downloads/.config_user.db
if [ -f $src ]; then
    new=`md5sum $src | cut -d ' ' -f 1`
    old=`md5sum $dst 2>/dev/null | cut -d ' ' -f 1`
    if [ "$new" != "$old" ]; then
        if [ ! -d /data/downloads ]; then
            mkdir -p /data/downloads
            chown 1000:1000 /data/downloads
            chmod 775 /data/downloads
        fi
        cp $src $dst
        chown 1000:1000 $dst
        chmod 644 $dst
        echo "ost: installed $dst" > /dev/kmsg
    fi
fi
'''

HOOK = '[ -f /boot/ost/ost_gl.sh ] && . /boot/ost/ost_gl.sh   # OpenStacks patch (GL rules)\n'
ANCHOR = 'log_echo "Starting Android"'


def apply(image_dir: Path, image: str) -> None:
    rd_path = image_dir / "initramfs.bin"
    if not rd_path.exists():
        raise FileNotFoundError(f"{rd_path} (patch needs the boot ramdisk)")
    rd = Ramdisk.load(rd_path)
    stage2 = rd.read("boot/stage2.sh").decode()
    if HOOK.strip() not in stage2:
        if ANCHOR not in stage2:
            raise ValueError(f"{image}: stage2.sh has no '{ANCHOR}' line to hook")
        stage2 = stage2.replace(ANCHOR, HOOK + ANCHOR, 1)
        rd.write("boot/stage2.sh", stage2.encode(), mode=0o755)
    rd.write("boot/ost/ost_gl.sh", BOOT_SCRIPT.encode(), mode=0o755)
    rd.write("boot/ost/config_user.db", (json.dumps(RULES, indent=4) + "\n").encode(),
             mode=0o644)
    rd.save(rd_path)
