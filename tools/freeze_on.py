# -*- coding: utf-8 -*-
"""Freeze the guest when a log event has been seen N times.

  python tools\\freeze_on.py <run> <regex> <count> <delay_s> [--stay] [--timeout S]

Polls runs/<run>/out.log; once <regex> has matched <count> times, waits <delay_s>,
then runs the 3-window pmemsave freeze into runs/<run>/freeze/ (see mon.py).
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mon  # noqa: E402

DEV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    run, pat, count, delay = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    timeout = float(sys.argv[sys.argv.index("--timeout") + 1]) if "--timeout" in sys.argv else 600
    log = os.path.join(DEV, "runs", run, "out.log")
    rx = re.compile(pat.encode())
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            n = len(rx.findall(open(log, "rb").read()))
        except OSError:
            n = 0
        if n >= count:
            print("hit %d x %r after %.1fs" % (n, pat, time.time() - t0), flush=True)
            time.sleep(delay)
            mon.freeze(os.path.join(DEV, "runs", run, "freeze"), "--stay" in sys.argv)
            return
        time.sleep(0.5)
    sys.exit("timeout: %r seen fewer than %d times" % (pat, count))


if __name__ == "__main__":
    main()
