# -*- coding: utf-8 -*-
"""Type ASCII text (and optional Enter) through QEMU's VNC server as key events.

  python tools\\vnc_type.py "text" [--enter] [--port 5901]
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
    text = a[0]
    port = int(a[a.index("--port") + 1]) if "--port" in a else 5901
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    recvn(s, 12)
    s.sendall(b"RFB 003.008\n")
    recvn(s, recvn(s, 1)[0])
    s.sendall(b"\x01")
    recvn(s, 4)
    s.sendall(b"\x01")
    recvn(s, 4)
    recvn(s, 16)
    recvn(s, struct.unpack(">I", recvn(s, 4))[0])
    s.sendall(struct.pack(">BxHi", 2, 1, 0))

    def key(sym, down):
        s.sendall(struct.pack(">BBxxI", 4, 1 if down else 0, sym))

    shift = 0xffe1
    for ch in text:
        up = ch.isupper() or ch in '~!@#$%^&*()_+{}|:"<>?'
        if up:
            key(shift, True)
        key(ord(ch), True)
        time.sleep(0.03)
        key(ord(ch), False)
        if up:
            key(shift, False)
        time.sleep(0.03)
    if "--enter" in a:
        key(0xff0d, True)
        time.sleep(0.03)
        key(0xff0d, False)
    time.sleep(0.2)
    s.close()
    print("typed %r%s" % (text, " + Enter" if "--enter" in a else ""))


if __name__ == "__main__":
    main()
