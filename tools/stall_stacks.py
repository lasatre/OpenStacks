# -*- coding: utf-8 -*-
"""Wait for the render lane to go quiet, then dump all QEMU thread stacks.

  python tools\\stall_stacks.py <run> [--quiet S] [--timeout S]

Watches runs/<run>/out.log for `pwrite`/`psend` lines; once there are none for
<quiet> seconds (default 15), attaches MSYS2 gdb to qemu-system-x86_64.exe and
writes runs/<run>/stacks.txt (thread apply all bt + loaded library bases).
"""
import os
import re
import subprocess
import sys
import time

DEV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GDB = r"C:\msys64\ucrt64\bin\gdb.exe"


def qemu_pid():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq qemu-system-x86_64.exe",
                          "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    m = re.search(r'"qemu-system-x86_64\.exe","(\d+)"', out)
    return int(m.group(1)) if m else None


def count(log):
    try:
        d = open(log, "rb").read()
    except OSError:
        return 0
    return len(re.findall(rb"\] p(?:write|send) ", d))


def main():
    run = sys.argv[1]
    quiet = float(sys.argv[sys.argv.index("--quiet") + 1]) if "--quiet" in sys.argv else 15
    timeout = float(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 600
    rd = os.path.join(DEV, "runs", run)
    log = os.path.join(rd, "out.log")
    t0 = time.time()
    last, last_t = -1, time.time()
    while time.time() - t0 < timeout:
        n = count(log)
        if n != last:
            last, last_t = n, time.time()
        elif n > 0 and time.time() - last_t >= quiet:
            break
        time.sleep(1)
    else:
        sys.exit("timeout without a quiet period")
    pid = qemu_pid()
    if not pid:
        sys.exit("qemu not running")
    print("quiet at %d events after %.0fs; attaching gdb to %d" % (last, time.time() - t0, pid))
    out = os.path.join(rd, "stacks.txt")
    env = dict(os.environ)
    env["PATH"] = r"C:\msys64\ucrt64\bin;" + env.get("PATH", "")
    with open(out, "w") as f:
        subprocess.run([GDB, "-q", "-batch", "-p", str(pid),
                        "-ex", "set pagination off",
                        "-ex", "info sharedlibrary",
                        "-ex", "thread apply all bt 30",
                        "-ex", "detach"],
                       stdout=f, stderr=subprocess.STDOUT, env=env, timeout=300)
    print("wrote", out, os.path.getsize(out))


if __name__ == "__main__":
    main()
