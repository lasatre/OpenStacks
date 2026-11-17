"""Native GPU display: embed the renderer's own window from the QEMU process.

BlueStacks' libOpenglRender.dll presents straight into a child window it
creates inside the HWND that OpenStacks' QEMU passes to PgaServerInit (class
``gr_renderHostWnd``, title ``OpenStacks display``), exactly like HD-Player.
Embedding that window here keeps every frame on the GPU: no pixel readback,
no VNC encode/decode. Mouse and keyboard input reach the window directly and
QEMU forwards them to the guest (touch via bstvmsg, keys via PS/2).

Windows only (falls back gracefully elsewhere).
"""

from __future__ import annotations

import ctypes
import sys
from typing import Callable, Optional

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QWindow
from PySide6.QtWidgets import QWidget

HOST_CLASS = "gr_renderHostWnd"

if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                 ctypes.POINTER(wintypes.DWORD)]
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
    _user32.SetParent.restype = wintypes.HWND
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]


def find_display_hwnd(pid: int) -> int:
    """HWND of the renderer host window owned by QEMU process `pid` (0 = none)."""
    if sys.platform != "win32" or not pid:
        return 0
    found = []

    def cb(hwnd, _l):
        p = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            buf = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(hwnd, buf, 64)
            if buf.value == HOST_CLASS:
                found.append(hwnd)
                return False
        return True

    _user32.EnumWindows(_EnumProc(cb), 0)
    return int(found[0]) if found else 0


class NativeDisplay(QWidget):
    """Hosts the renderer window, letterboxed to the guest's aspect ratio."""

    def __init__(self, pid_provider: Callable[[], Optional[int]],
                 aspect: tuple[int, int] = (1600, 900), parent=None):
        super().__init__(parent)
        self._pid_provider = pid_provider
        self._aspect = aspect
        self._pid = 0
        self._hwnd = 0
        self._win: Optional[QWindow] = None
        self._container: Optional[QWidget] = None
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMinimumSize(320, 180)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(500)

    # -- state ------------------------------------------------------------------
    def state(self) -> str:
        if self._container is not None:
            return "native (GPU)"
        return "native: waiting for renderer" if self._pid else "native: QEMU not running"

    def set_aspect(self, w: int, h: int) -> None:
        self._aspect = (max(1, w), max(1, h))
        self._layout()

    # -- embedding ----------------------------------------------------------------
    def _poll(self) -> None:
        pid = self._pid_provider() or 0
        if pid != self._pid:
            self.release()
            self._pid = pid
        if not pid:
            return
        if self._hwnd and not _user32.IsWindow(self._hwnd):
            self.release()
        if self._container is None:
            hwnd = find_display_hwnd(pid)
            if hwnd:
                self._embed(hwnd)

    def _embed(self, hwnd: int) -> None:
        win = QWindow.fromWinId(hwnd)
        if win is None:
            return
        self._hwnd, self._win = hwnd, win
        self._container = QWidget.createWindowContainer(win, self)
        self._container.setFocusPolicy(Qt.StrongFocus)
        self._layout()
        self._container.show()
        _user32.ShowWindow(hwnd, 5)          # SW_SHOW (QEMU creates it hidden)

    def release(self) -> None:
        """Hand the window back to the desktop BEFORE our container dies:
        Windows destroys child windows with their parent, which would take
        the renderer's present window (in QEMU) down with the player."""
        if self._hwnd and sys.platform == "win32" and _user32.IsWindow(self._hwnd):
            _user32.ShowWindow(self._hwnd, 0)  # SW_HIDE
            _user32.SetParent(self._hwnd, None)
        if self._container is not None:
            self._container.hide()
            self._container.setParent(None)
            self._container.deleteLater()
        self._container, self._win, self._hwnd = None, None, 0

    # -- layout / paint -------------------------------------------------------------
    def _target_rect(self) -> QRect:
        aw, ah = self._aspect
        w, h = self.width(), self.height()
        if w * ah > h * aw:          # too wide: pillarbox
            tw, th = h * aw // ah, h
        else:                        # too tall: letterbox
            tw, th = w, w * ah // aw
        return QRect((w - tw) // 2, (h - th) // 2, tw, th)

    def _layout(self) -> None:
        if self._container is not None:
            self._container.setGeometry(self._target_rect())

    def resizeEvent(self, ev) -> None:
        self._layout()
        super().resizeEvent(ev)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0d12"))
        if self._container is None:
            p.setPen(QColor("#7d8590"))
            p.drawText(self.rect(), Qt.AlignCenter, self.state())
        p.end()

    def closeEvent(self, ev) -> None:
        self.release()
        super().closeEvent(ev)
