# -*- coding: utf-8 -*-
"""Convert a run's screen_pga.bmp (top-down 32-bpp BGRA) to a half-size PNG.

  python dev\\tools\\shot.py <run-or-bmp> [out.png] [--rgba]

--rgba: the BMP holds raw RGBA (captures made before the Phase-66 bmp_write fix).
"""
import os
import struct
import sys
import zlib

DEV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    rgba = "--rgba" in sys.argv
    src = a[0]
    if not src.lower().endswith(".bmp"):
        src = os.path.join(DEV, "runs", src, "screen_pga.bmp")
    out = a[1] if len(a) > 1 else os.path.splitext(src)[0] + ".png"
    d = open(src, "rb").read()
    w, h = struct.unpack_from("<ii", d, 18)
    h = abs(h)
    px = d[54:]
    rows = []
    for y in range(0, h, 2):
        r = bytearray(b"\x00")
        for x in range(0, w, 2):
            o = (y * w + x) * 4
            r += bytes((px[o], px[o + 1], px[o + 2])) if rgba else \
                bytes((px[o + 2], px[o + 1], px[o]))
        rows.append(bytes(r))

    def ch(t, b):
        c = struct.pack(">I", len(b)) + t + b
        return c + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)

    png = (b"\x89PNG\r\n\x1a\n" +
           ch(b"IHDR", struct.pack(">IIBBBBB", w // 2, len(rows), 8, 2, 0, 0, 0)) +
           ch(b"IDAT", zlib.compress(b"".join(rows), 6)) + ch(b"IEND", b""))
    open(out, "wb").write(png)
    print(out)


if __name__ == "__main__":
    main()
