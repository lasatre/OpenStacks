# -*- coding: utf-8 -*-
"""QEMU telnet-monitor helper (127.0.0.1:4446).

  python dev\\tools\\mon.py cmd "info status"           one command
  python dev\\tools\\mon.py freeze <outdir>              stop + the THREE pmemsave
                                                       windows (emugl heap is above
                                                       4 GiB) + cont
  python dev\\tools\\mon.py freeze <outdir> --stay       same, but leave the guest stopped
  python dev\\tools\\mon.py quit
"""
import os
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 4446
WINDOWS = [("lo", 0x0, 0x80000000), ("mid", 0x80000000, 0x80000000),
           ("top", 0x100000000, 0x40000000)]


def strip_iac(buf):
    out, i, n = bytearray(), 0, len(buf)
    while i < n:
        b = buf[i]
        if b == 0xFF and i + 1 < n:
            c = buf[i + 1]
            if c in (251, 252, 253, 254):
                i += 3
                continue
            if c == 250:
                j = i + 2
                while j + 1 < n and not (buf[j] == 0xFF and buf[j + 1] == 0xF0):
                    j += 1
                i = j + 2
                continue
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


class Mon:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=10)
        self.s.settimeout(1)
        self._read_prompt(5)

    def _read_prompt(self, wait):
        t0, buf = time.time(), b""
        while time.time() - t0 < wait:
            try:
                d = self.s.recv(65536)
            except socket.timeout:
                continue
            if not d:
                break
            buf += d
            if strip_iac(buf).rstrip().endswith(b"(qemu)"):
                break
        return strip_iac(buf).decode("ascii", "replace")

    def cmd(self, c, wait=900.0):
        t0 = time.time()
        self.s.sendall(c.encode("ascii") + b"\r\n")
        r = self._read_prompt(wait)
        print("[%s] >> %s (%.1fs)" % (time.strftime("%H:%M:%S"), c, time.time() - t0),
              flush=True)
        return r


def freeze(outdir, stay):
    os.makedirs(outdir, exist_ok=True)
    m = Mon()
    m.cmd("stop", 5)
    print("frozen at %s" % time.strftime("%H:%M:%S"))
    for tag, addr, size in WINDOWS:
        path = os.path.join(outdir, "ram_%s.bin" % tag)
        m.cmd('pmemsave 0x%x 0x%x "%s"' % (addr, size, path.replace("\\", "/")))
        for _ in range(120):
            if os.path.exists(path) and os.path.getsize(path) == size:
                break
            time.sleep(1)
        print("  %s %d/%d" % (path, os.path.getsize(path) if os.path.exists(path) else -1, size))
    if not stay:
        m.cmd("cont", 5)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    op = sys.argv[1]
    if op == "cmd":
        print(Mon().cmd(" ".join(sys.argv[2:]), 30))
    elif op == "freeze":
        freeze(sys.argv[2], "--stay" in sys.argv)
    elif op == "quit":
        Mon().cmd("quit", 5)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
