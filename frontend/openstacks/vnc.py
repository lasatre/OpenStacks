"""Pure-Python RFB/VNC client, timed for Qt threads.

Protocol logic reused from tools/vncshot.py (a working RAW-RFB screenshot
grabber): same handshake (RFB 003.008, None auth), same client pixel format
(32bpp little-endian, B,G,R,X in memory), raw encoding only, tolerant of
SetColourMapEntries / Bell / ServerCutText / extended-clipboard messages.

Ported to run continuously inside a QThread:
    * maintains one shared bytearray framebuffer (w*h*4)
    * requests incremental (Fast-path) framebuffer updates in a loop
    * emits VncClientThread.frameUpdated(w, h, snapshot_bytes) to the GUI
    * mouse (PointerEvent) and keyboard (KeyEvent) sends share the same socket
      guarded by a lock - this is the "QEMU VNC input" primary path
    * automatic reconnection with an exponential backoff

Notes / fixes vs. vncshot.py:
    * the update header is msg_type(1) + pad(1) + nrect(2); we nread exactly
      4 bytes (vncshot reads 6 -> relies on server quirks, we don't).
    * DesktopResize (pseudo-encoding -223) is requested and handled.
No external deps at all (PySide6 only for the QThread wrapper).
"""

from __future__ import annotations

import socket
import struct
import time
from typing import Callable

from PySide6.QtCore import QThread, Signal

DEFAULT_RETRIES = 6
DESKTOP_SIZE_ENC = 0xFFFFFF21   # -223
RAW_ENC = 0


def readn(s: socket.socket, n: int) -> bytes:
    b = b""
    while len(b) < n:
        chunk = s.recv(n - len(b))
        if not chunk:
            raise ConnectionError("short read")
        b += chunk
    return b


class RfbError(RuntimeError):
    pass


class RfbClient:
    """A synchronous RFB session on one socket. Call connect() first."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5901):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.width = 0
        self.height = 0
        self.name = ""
        self.frame = bytearray(0)
        self._send_lock = _FakeLock()

    # -- handshake -------------------------------------------------------------
    def connect(self, timeout: float = 15.0) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        ver = readn(self.sock, 12)                       # e.g. b"RFB 003.008\n"
        if not ver.startswith(b"RFB 00"):
            raise RfbError(f"not an RFB banner: {ver!r}")
        self.sock.sendall(b"RFB 003.008\n")
        nsec = readn(self.sock, 1)[0]
        sectypes = readn(self.sock, nsec) if nsec else b"\x01"
        if nsec and 1 not in sectypes:
            raise RfbError(f"server offers only {[hex(x) for x in sectypes]} auth "
                           f"(None auth unsupported)")
        self.sock.sendall(b"\x01")
        if ver == b"RFB 003.008\n":
            res = readn(self.sock, 4)                    # security result
            if res != b"\x00\x00\x00\x00":
                rlen = struct.unpack(">I", readn(self.sock, 4))[0]
                raise RfbError("auth failed: " +
                               readn(self.sock, rlen).decode(errors="replace"))
        self.sock.sendall(struct.pack(">B", 1))          # ClientInit: shared
        hdr = readn(self.sock, 24)                       # ServerInit
        self.width, self.height = struct.unpack(">2H", hdr[0:4])
        name_len = struct.unpack(">I", hdr[20:24])[0]
        self.name = readn(self.sock, name_len).decode(errors="replace")
        self._alloc_frame(self.width, self.height)

        # SetPixelFormat: 32bpp truecolor little-endian; rsh16 gsh8 bsh0
        pf = struct.pack(">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        self.sock.sendall(b"\x00" + b"\x00" * 3 + pf)
        # SetEncodings: raw + DesktopSize pseudo
        self.sock.sendall(struct.pack(">BxH", 2, 2)
                          + struct.pack(">i", RAW_ENC)
                          + struct.pack(">i", -223))

        self._send_lock = _SocketLock(self.sock)

        # first update: full refresh
        self.request_refresh(incremental=False)

    def _alloc_frame(self, w: int, h: int) -> None:
        self.width = w
        self.height = h
        self.frame = bytearray(w * h * 4)

    # -- updates -----------------------------------------------------------------
    def request_refresh(self, incremental: bool = True) -> None:
        """FramebufferUpdateRequest; incremental=1 lets QEMU pace itself."""
        self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0,
                               self.width, self.height))

    def _send(self, payload: bytes) -> None:
        s = self.sock
        if s is None or getattr(self._send_lock, "sock", None) is not s:
            raise RfbError("not connected")
        self._send_lock.sendall(payload)

    def read_message(self) -> bool:
        """Read one RFB message. Returns True if a framebuffer update landed."""
        mtype = readn(self.sock, 1)[0]
        if mtype == 0:                                   # FramebufferUpdate
            # header is exactly 3 more bytes: pad(1) + nrect(2)
            hdr = readn(self.sock, 3)
            nrect = struct.unpack(">H", hdr[1:3])[0]
            return self._read_rects(nrect)
        if mtype == 1:                                   # SetColourMapEntries
            body = readn(self.sock, 5)
            n = struct.unpack(">H", body[3:5])[0]
            readn(self.sock, n * 6)
            return False
        if mtype == 2:                                   # Bell
            return False
        if mtype == 3:                                   # ServerCutText
            readn(self.sock, 3)
            ln = struct.unpack(">I", readn(self.sock, 4))[0]
            readn(self.sock, ln)
            return False
        if mtype == 33:                                  # extended clipboard
            body = readn(self.sock, 7)
            ln = struct.unpack(">I", body[3:7])[0]
            readn(self.sock, ln)
            return False
        raise RfbError(f"unexpected message {mtype}")

    # bytes per pixel (little-endian BGRX after our SetPixelFormat)
    REDUCED = 4

    def _read_rects(self, nrect: int) -> bool:
        for _ in range(nrect):
            rect = readn(self.sock, 12)
            rx, ry, rw, rh = struct.unpack(">2H2H", rect[0:8])
            enc = struct.unpack(">i", rect[8:12])[0]
            if enc == DESKTOP_SIZE_ENC:                  # DesktopResize
                self._alloc_frame(rx, ry)
                continue
            if enc == 0xFFFFFFFF:                        # lastRect pseudo
                break
            if enc != RAW_ENC:
                raise RfbError(f"unexpected encoding {enc}")
            payload = readn(self.sock, rw * rh * self.REDUCED)
            w = self.width
            for row in range(rh):                        # blit row by row
                off = ((ry + row) * w + rx) * 4
                self.frame[off:off + rw * 4] = \
                    payload[row * rw * 4:(row + 1) * rw * 4]
        return True

    # -- input (the primary path: mouse/keys ride the same RFB socket) ------------
    def send_pointer(self, x: int, y: int, buttons_mask: int) -> None:
        """PointerEvent; buttons_mask bit0=left bit1=middle bit2=right."""
        # RFB PointerEvent: u8 type=5, u8 button-mask, u16 x, u16 y
        self._send(struct.pack(">BBHH", 5, buttons_mask & 0xFF,
                               max(0, int(x)) & 0xFFFF, max(0, int(y)) & 0xFFFF))

    def send_keysym(self, keysym: int, down: bool) -> None:
        """KeyEvent for X11 keysym, press or release."""
        # RFB KeyEvent: u8 type=4, u8 down-flag, 2 pad, u32 keysym
        self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym & 0xFFFFFFFF))

    def close(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        self.sock = None


class _FakeLock:
    """Stub before the socket exists (all sends raise RfbError anyway)."""
    sock = None

    def sendall(self, payload: bytes) -> None:
        raise RfbError("not connected")


class _SocketLock:
    """Lock around sendall: reader thread + input threads share the socket."""

    def __init__(self, sock: socket.socket):
        import threading
        self.lock = threading.Lock()
        self.sock = sock

    def sendall(self, payload: bytes) -> None:
        with self.lock:
            self.sock.sendall(payload)


class VncClientThread(QThread):
    """Runs RfbClient.read_message() in a loop inside its own QThread.

    Signals:
      stateChanged(str)   'connecting' | 'connected' | 'reconnecting'
      frameUpdated(int,int,bytes)  w, h, snapshot[bytes] of BGRX pixels
      failed(str)         last error text (after retries exhausted)
    """

    stateChanged = Signal(str)
    frameUpdated = Signal(int, int, object)
    failed = Signal(str)

    def __init__(self, host: str, port: int, parent=None,
                 on_send_error: Callable[[str], None] | None = None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.on_send_error = on_send_error
        self._stopping = False
        self._client: RfbClient | None = None

    # -- networking loop -------------------------------------------------------
    def run(self) -> None:
        backoff = 1.0
        while not self._stopping:
            self.stateChanged.emit("connecting")
            client = RfbClient(self.host, self.port)
            try:
                client.connect()
            except (OSError, RfbError) as e:
                client.close()
                if self._stopping:
                    return
                self.stateChanged.emit("reconnecting")
                if not self._sleep(backoff):
                    return
                backoff = min(backoff * 2, 8.0)
                continue
            backoff = 1.0
            self._client = client
            self.stateChanged.emit("connected")
            try:
                while not self._stopping:
                    client.read_message()
                    # pace the request loop: exactly one update in flight at a
                    # time, which naturally throttles to whatever QEMU sends
                    client.request_refresh(incremental=True)
                    data = bytes(client.frame)      # snapshot for the GUI
                    self.frameUpdated.emit(client.width, client.height, data)
            except (OSError, RfbError, ConnectionError) as e:
                self._client = None
                client.close()
                if self._stopping:
                    return
                self.stateChanged.emit("reconnecting")
                if not self._sleep(1.0):
                    return
        self.stateChanged.emit("stopped")

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep on the QThread event loop."""
        end = time.monotonic() + seconds
        while not self._stopping and time.monotonic() < end:
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return not self._stopping

    # -- public API from other threads ------------------------------------------
    def stop(self, wait_ms: int = 3000) -> None:
        self._stopping = True
        c = self._client
        if c is not None:
            c.close()
        if self.isRunning():
            self.wait(wait_ms)

    def send_pointer(self, x: int, y: int, buttons_mask: int) -> bool:
        c = self._client
        if c is None:
            return False
        try:
            c.send_pointer(x, y, buttons_mask)
            return True
        except (OSError, RfbError) as e:
            if self.on_send_error:
                self.on_send_error(str(e))
            return False

    def send_keysym(self, keysym: int, down: bool) -> bool:
        c = self._client
        if c is None:
            return False
        try:
            c.send_keysym(keysym, down)
            return True
        except (OSError, RfbError) as e:
            if self.on_send_error:
                self.on_send_error(str(e))
            return False

    @property
    def server_name(self) -> str:
        return self._client.name if self._client else ""

    @property
    def is_connected(self) -> bool:
        return self._client is not None


# -- pure-python helpers (no Qt): useful for scripts / tests --------------------
def grab_frame(host: str, port: int) -> tuple[int, int, bytes]:
    """One-shot screenshot via the same protocol logic (like tools/vncshot.py)."""
    c = RfbClient(host, port)
    try:
        c.connect()
        c.read_message()
        return c.width, c.height, bytes(c.frame)
    finally:
        c.close()


if __name__ == "__main__":  # pragma: no cover  (python -m openstacks.vnc host port out.png)
    import sys
    import zlib
    from struct import pack as _p

    def write_png(path, w, h, rgbx):
        def chunk(tag, data):
            c = tag + data
            return _p(">I", len(data)) + c + _p(">I", zlib.crc32(c) & 0xFFFFFFFF)
        raw = bytearray()
        for row in range(h):
            raw.append(0)
            off = row * w * 4
            px = rgbx[off:off + w * 4]
            buf = bytearray(w * 3)
            for i in range(0, len(px), 4):
                # little-endian 32bpp rsh16 gsh8 bsh0 => B,G,R,X in memory
                buf[i:i + 3] = bytes((px[i + 2], px[i + 1], px[i]))
            raw += buf
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", _p(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(bytes(raw)))
               + chunk(b"IEND", b""))
        with open(path, "wb") as fh:
            fh.write(png)

    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5901
    w, h, px = grab_frame(host, port)
    write_png(sys.argv[3] if len(sys.argv) > 3 else "shot.png", w, h, px)
    print(f"saved ({w}x{h})")
