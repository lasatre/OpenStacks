# -*- coding: utf-8 -*-
"""Send a tap (or a drag) to QEMU's VNC server as pointer events.

  python tools\\vnc_tap.py <x> <y> [--port 5901] [--to X2 Y2] [--hold MS]

Coordinates are screen pixels of the VNC framebuffer (1600x900 here).
"""
import socket
import struct
import sys
import time


def recvn(s, n):
    b = b""
    while len(b) < n:
        d = s.recv(n - len(b))
        if not d:
            raise EOFError("vnc closed")
        b += d
    return b


def main():
    a = sys.argv[1:]
    x, y = int(a[0]), int(a[1])
    port = int(a[a.index("--port") + 1]) if "--port" in a else 5901
    hold = int(a[a.index("--hold") + 1]) if "--hold" in a else 80
    to = (int(a[a.index("--to") + 1]), int(a[a.index("--to") + 2])) if "--to" in a else None
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    recvn(s, 12)
    s.sendall(b"RFB 003.008\n")
    recvn(s, recvn(s, 1)[0])
    s.sendall(b"\x01")
    recvn(s, 4)
    s.sendall(b"\x01")
    w, h = struct.unpack(">HH", recvn(s, 4))
    recvn(s, 16)
    recvn(s, struct.unpack(">I", recvn(s, 4))[0])
    # SetEncodings (Raw): QEMU only evaluates absolute pointer mode on
    # SetEncodings; without it a client stays in relative mode.
    s.sendall(struct.pack(">BxHi", 2, 1, 0))
    time.sleep(0.1)

    def ptr(mask, px, py):
        s.sendall(struct.pack(">BBHH", 5, mask, px, py))

    ptr(0, x, y)
    time.sleep(0.05)
    ptr(1, x, y)
    if to:
        steps = 12
        for i in range(1, steps + 1):
            time.sleep(hold / 1000.0 / steps)
            ptr(1, x + (to[0] - x) * i // steps, y + (to[1] - y) * i // steps)
        ex, ey = to
    else:
        time.sleep(hold / 1000.0)
        ex, ey = x, y
    ptr(0, ex, ey)
    time.sleep(0.2)
    s.close()
    print("tap %d,%d%s on %dx%d" % (x, y, " -> %d,%d" % to if to else "", w, h))


if __name__ == "__main__":
    main()
