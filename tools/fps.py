# -*- coding: utf-8 -*-
"""Measure guest frame rate: SurfaceFlinger page flips over a window of time.

  python tools\\fps.py [--serial 127.0.0.1:25556] [--seconds 10]
                       [--launch com.pkg] [--warmup 25]

Uses `service call SurfaceFlinger 1013` (the page-flip counter), so it counts
composited frames of whatever is on screen (a game, an animation).
"""
import argparse
import os
import re
import subprocess
import time

ADB = os.environ.get("ADB", "adb")


def adb(serial, *args, timeout=30):
    return subprocess.run([ADB, "-s", serial, *args], capture_output=True, text=True,
                          timeout=timeout).stdout


def flips(serial):
    # the flip counter needs root: BlueStacks ships su at /system/xbin/bstk/su
    out = adb(serial, "shell", "/system/xbin/bstk/su -c 'service call SurfaceFlinger 1013'")
    m = re.search(r"Parcel\(\s*([0-9a-f]{8})", out)
    return int(m.group(1), 16) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:25556")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--launch", default="")
    ap.add_argument("--warmup", type=float, default=25)
    a = ap.parse_args()
    subprocess.run([ADB, "connect", a.serial], capture_output=True)
    if a.launch:
        adb(a.serial, "shell", "monkey", "-p", a.launch, "-c",
            "android.intent.category.LAUNCHER", "1")
        time.sleep(a.warmup)
    f0, t0 = flips(a.serial), time.time()
    time.sleep(a.seconds)
    f1, t1 = flips(a.serial), time.time()
    if f0 is None or f1 is None:
        print("could not read the SurfaceFlinger flip counter")
        return 1
    print("flips %d in %.1f s = %.1f fps" % (f1 - f0, t1 - t0, (f1 - f0) / (t1 - t0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
