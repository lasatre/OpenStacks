# -*- coding: utf-8 -*-
"""Grade one boot run.

  python dev\\tools\\grade.py <run-dir-or-name> [--write]
  python dev\\tools\\grade.py --log <out.log> [--serial <serial.log>]   (archived boots)

Metrics are the ones that SURVIVED cross-boot validation (dev/NOTES.md §1):
her-record evidence only (guestwrite/exeFrame/recDropped), never the
`rctl reply op=` lines (our own rcInit step machine prints those).
"""
import os
import re
import sys
from collections import Counter

DEV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def grade(out_log, serial_log=None, bmp=None):
    r = []
    d = open(out_log, "rb").read()
    c = lambda pat: len(re.findall(pat, d))
    sf = Counter(int(m) for m in re.findall(rb"exeFrame cid=\d+ sfIdx=(\d+) ->", d))
    r.append("sfIdx frames        : %s" % dict(sorted(sf.items())))
    r.append("  sfIdx=4 (numCfg)  : %d   sfIdx=5 (getCfg): %d" % (sf.get(4, 0), sf.get(5, 0)))
    lead = Counter(m.decode() for m in re.findall(
        rb"guestwrite cid=(?:[2-9]|\d\d+) off=0 n=276 first=(0x[0-9a-f]+)", d))
    r.append("reconnect-hello lead: %s   (0x2711<0x2714<0x2715 = walk depth; throttled tap)"
             % dict(lead))
    big = re.findall(rb"guestwrite cid=\d+ off=0 n=276 first=0x2715: 15 27 00 00 10 00 00 00 "
                     rb"((?:[0-9a-f]{2} ){4})", d)
    if big:
        r.append("  0x2715 bufSize    : %s" % sorted(set(b.decode().strip() for b in big)))
    drops = Counter(m.decode() for m in re.findall(rb"recDropped cid=\d+ len=\d+ first=\S+ \((\S+)", d))
    r.append("recDropped          : %s" % dict(drops))
    r.append("echo passthrough    : %d" % c(rb"echoRaw cid="))
    r.append("ring-reset          : %d" % c(rb"hstAlloc ring-reset"))
    r.append("connect/disconnect  : %d / %d" % (c(rb"connect cid=\d+ OK"), c(rb"cmd=2 disconnect")))
    r.append("exePrefill 0x2715   : %d" % c(rb"exePrefill cid=\S+ op=0x2715"))
    r.append("hstRead req=1632    : %d" % c(rb"hstRead req=1632"))
    r.append("wantLen=262144      : %d" % c(rb"wantLen=262144"))
    r.append("batchWait ask=262144: %d" % c(rb"batchWait cid=\d+ op=0x\w+ ask=262144"))
    r.append("frame BMPs written  : %d" % c(rb"frame BMP #"))
    if serial_log and os.path.exists(serial_log):
        s = open(serial_log, "rb").read()
        r.append("serial bootanim     : %d" % len(re.findall(rb"starting service 'bootanim'", s)))
        r.append("serial Fatal signal : %d" % len(re.findall(rb"Fatal signal", s)))
        r.append("serial Kernel panic : %d" % len(re.findall(rb"Kernel panic", s)))
    if bmp and os.path.exists(bmp):
        b = open(bmp, "rb").read()[54:]
        nz = sum(1 for x in b if x)
        vals = Counter(b[i] for i in range(0, len(b), 997))
        r.append("screen_pga.bmp      : %d B, nonzero=%d, sampled distinct=%d"
                 % (len(b) + 54, nz, len(vals)))
    return r


def main():
    a = sys.argv[1:]
    if a and a[0] == "--log":
        out = a[1]
        ser = a[a.index("--serial") + 1] if "--serial" in a else None
        print("\n".join(grade(out, ser)))
        return
    run = a[0] if os.path.isdir(a[0]) else os.path.join(DEV, "runs", a[0])
    lines = grade(os.path.join(run, "out.log"), os.path.join(run, "serial.log"),
                  os.path.join(run, "screen_pga.bmp"))
    print("\n".join(lines))
    if "--write" in a:
        with open(os.path.join(run, "grade.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
