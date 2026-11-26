"""The game-controls channel to QEMU (bstvmsg "input" chardev, docs/keymapping_plan.md).

The channel is a persistent localhost TCP connection carrying one ASCII line per event.
QEMU is the server, and this side reconnects whenever the instance restarts.
"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket


_DEBUG = bool(os.environ.get("OST_CONTROLS_DEBUG"))


class InputChannel(QObject):
    connected = Signal()
    disconnected = Signal()
    line = Signal(str)               # one event line from QEMU, without the newline

    def __init__(self, port: int, parent=None):
        super().__init__(parent)
        self.port = int(port)
        self._sock = QTcpSocket(self)
        self._sock.connected.connect(self._on_connected)
        self._sock.disconnected.connect(self._on_disconnected)
        self._sock.readyRead.connect(self._on_ready)
        self._sock.errorOccurred.connect(lambda _e: self._retry.start())
        self._buf = b""
        self._out: list[str] = []
        self._retry = QTimer(self, singleShot=True, interval=1000)
        self._retry.timeout.connect(self._connect)
        self._running = False

    @property
    def is_open(self) -> bool:
        return self._sock.state() == QAbstractSocket.SocketState.ConnectedState

    def start(self) -> None:
        self._running = True
        self._connect()

    def stop(self) -> None:
        self._running = False
        self._retry.stop()
        if self.is_open:
            self._sock.write(b"r\ncap\nbtn 0\nlock 0\n")
            self._sock.flush()
        self._sock.abort()

    def _connect(self) -> None:
        if not self._running or self._sock.state() != QAbstractSocket.SocketState.UnconnectedState:
            return
        self._sock.connectToHost("127.0.0.1", self.port)

    def _on_connected(self) -> None:
        self._sock.setSocketOption(QAbstractSocket.SocketOption.LowDelayOption, 1)
        self._buf = b""
        self.connected.emit()

    def _on_disconnected(self) -> None:
        self.disconnected.emit()
        if self._running:
            self._retry.start()

    def _on_ready(self) -> None:
        self._buf += bytes(self._sock.readAll())
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            text = raw.decode("ascii", "replace").strip()
            if text:
                self.line.emit(text)

    # -- sending: lines are batched and written at flush() (one write per event batch)
    def send(self, text: str) -> None:
        self._out.append(text)

    def flush(self) -> None:
        if not self._out:
            return
        # touch changes take effect as one frame at "s" (QEMU then pushes it to the guest)
        if any(t[0] in "tu" for t in self._out) and self._out[-1] != "s":
            self._out.append("s")
        if self.is_open:
            if _DEBUG:
                print("[controls] -> " + " | ".join(self._out), file=sys.stderr, flush=True)
            self._sock.write(("\n".join(self._out) + "\n").encode("ascii"))
            self._sock.flush()
        self._out.clear()
