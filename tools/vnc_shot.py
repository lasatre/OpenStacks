# -*- coding: utf-8 -*-
"""Grab one frame from QEMU's VNC server (raw RFB, no auth) into a half-size PNG.

  python tools\\vnc_shot.py [port=5901] [out.png]
"""
import socket
import struct
import sys
import zlib


def recvn(s, n):
    b = b""
    while len(b) < n:
        d = s.recv(n - len(b))
        if not d:
            raise EOFError("vnc closed")
        b += d
    return b


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5901
    out = sys.argv[2] if len(sys.argv) > 2 else "vnc.png"
    s = socket.create_connection(("127.0.0.1", port), timeout=20)
    ver = recvn(s, 12)
    s.sendall(b"RFB 003.008\n")
    n = recvn(s, 1)[0]
    types = recvn(s, n)
    if 1 not in types:
        sys.exit("no 'None' security type offered: %r" % types)
    s.sendall(b"\x01")
    if struct.unpack(">I", recvn(s, 4))[0] != 0:
        sys.exit("security handshake failed")
    s.sendall(b"\x01")                                  # shared
    w, h = struct.unpack(">HH", recvn(s, 4))
    recvn(s, 16)                                        # server pixel format
    name = recvn(s, struct.unpack(">I", recvn(s, 4))[0])
    # SetPixelFormat: 32bpp, depth 24, little endian, true colour, R@16 G@8 B@0
    s.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
    s.sendall(struct.pack(">BxHi", 2, 1, 0))            # SetEncodings: Raw only
    s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))  # full update request
    fb = bytearray(w * h * 4)
    got = 0
    while got < w * h:
        t = recvn(s, 1)[0]
        if t != 0:                                      # skip non-framebuffer msgs
            if t == 2:
                continue
            if t == 3:
                recvn(s, 3 + struct.unpack(">I", recvn(s, 7)[3:])[0])
                continue
            sys.exit("unexpected message %d" % t)
        recvn(s, 1)
        nrect = struct.unpack(">H", recvn(s, 2))[0]
        for _ in range(nrect):
            x, y, rw, rh, enc = struct.unpack(">HHHHi", recvn(s, 12))
            if enc != 0:
                continue
            data = recvn(s, rw * rh * 4)
            for row in range(rh):
                o = ((y + row) * w + x) * 4
                fb[o:o + rw * 4] = data[row * rw * 4:(row + 1) * rw * 4]
            got += rw * rh
    rows = []
    for yy in range(0, h, 2):
        r = bytearray(b"\x00")
        for xx in range(0, w, 2):
            o = (yy * w + xx) * 4
            r += bytes((fb[o + 2], fb[o + 1], fb[o]))
        rows.append(bytes(r))

    def ch(tag, b):
        c = struct.pack(">I", len(b)) + tag + b
        return c + struct.pack(">I", zlib.crc32(tag + b) & 0xffffffff)

    open(out, "wb").write(b"\x89PNG\r\n\x1a\n" +
                          ch(b"IHDR", struct.pack(">IIBBBBB", w // 2, len(rows), 8, 2, 0, 0, 0)) +
                          ch(b"IDAT", zlib.compress(b"".join(rows), 6)) + ch(b"IEND", b""))
    print("%s %dx%d '%s' -> %s" % (ver.strip().decode(), w, h, name.decode(), out))


if __name__ == "__main__":
    main()
