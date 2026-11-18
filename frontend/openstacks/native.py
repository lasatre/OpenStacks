"""Native GPU display: embed the renderer's own window from the QEMU process.

QEMU (gr_render.c) owns a window (class ``gr_renderHostWnd``, title ``OpenStacks display
r<rotation>``) that the renderer presents into: BlueStacks' libOpenglRender.dll or the open
gfxstream renderer, like HD-Player. Embedding that window here keeps every frame on the GPU:
no pixel readback, no VNC encode/decode. Mouse and keyboard input reach the window directly
and QEMU forwards them to the guest (touch via bstvmsg, keys via the keyboard).

Windows: found by owner pid + class (EnumWindows), embedded by HWND.
Linux: an X11 window found by _NET_WM_PID + WM_CLASS (XQueryTree), embedded by XID; Qt runs
on xcb (XWayland on Wayland desktops, see openstacks/__init__.py).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
import time
from typing import Callable, Optional

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QWindow
from PySide6.QtWidgets import QWidget

HOST_CLASS = "gr_renderHostWnd"


def _title_rotation(t: str) -> int:
    """Guest rotation 0..3 published by QEMU in the window title ("... r<o>")."""
    return int(t[-1]) if len(t) > 2 and t[-2] == "r" and t[-1] in "0123" else 0


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
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def display_rotation(hwnd: int) -> int:
        if not hwnd:
            return 0
        buf = ctypes.create_unicode_buffer(64)
        _user32.GetWindowTextW(hwnd, buf, 64)
        return _title_rotation(buf.value)

    def describe_display(pid: int) -> str:
        """Why the display isn't embedded yet, for the waiting text: does QEMU `pid` own a
        renderer window at all, and if so, is it top-level or already inside some window?"""
        if not pid:
            return ""
        _user32.EnumChildWindows.argtypes = [wintypes.HWND, _EnumProc, wintypes.LPARAM]
        _user32.GetParent.argtypes = [wintypes.HWND]
        _user32.GetParent.restype = wintypes.HWND
        top, nested = [], []

        def owner(hwnd) -> int:
            p = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            return p.value

        def is_host(hwnd) -> bool:
            buf = ctypes.create_unicode_buffer(64)
            _user32.GetClassNameW(hwnd, buf, 64)
            return buf.value == HOST_CLASS

        def child_cb(hwnd, _l):
            if is_host(hwnd) and owner(hwnd) == pid:
                par = _user32.GetParent(hwnd)
                nested.append((int(hwnd), owner(par) if par else 0))
            return True

        def top_cb(hwnd, _l):
            if is_host(hwnd) and owner(hwnd) == pid:
                top.append(int(hwnd))
            _user32.EnumChildWindows(hwnd, _EnumProc(child_cb), 0)
            return True

        _user32.EnumWindows(_EnumProc(top_cb), 0)
        if top:
            return f"QEMU pid {pid}: screen window {top[0]:#x} is top-level (not embedded yet)"
        if nested:
            h, ppid = nested[0]
            where = ("another window of this player" if ppid == os.getpid()
                     else f"a window of process {ppid}")
            return f"QEMU pid {pid}: screen window {h:#x} is held by {where}"
        return f"QEMU pid {pid}: no screen window yet (renderer not started?)"

    def find_display_hwnd(pid: int) -> int:
        """HWND of the renderer host window owned by QEMU process `pid` (0 = none)."""
        if not pid:
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

    def _alive(hwnd: int) -> bool:
        return bool(_user32.IsWindow(hwnd))

    def _show(hwnd: int) -> None:
        _user32.ShowWindow(hwnd, 5)            # SW_SHOW (QEMU creates it hidden)

    def _release(hwnd: int) -> None:
        if _user32.IsWindow(hwnd):
            _user32.ShowWindow(hwnd, 0)        # SW_HIDE
            _user32.SetParent(hwnd, None)

else:
    # -- X11 (Xlib through ctypes, on a connection of our own) --------------------------
    class _XClassHint(ctypes.Structure):                 # Xlib-allocated: XFree both
        _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]

    _ErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    _x = None
    _dpy = None
    _atoms: dict[str, int] = {}

    @_ErrorHandler
    def _x_error(_d, _e):
        # BadWindow & co. are expected (QEMU's window can die at any time); Xlib's default
        # handler would exit the player
        return 0

    def _xlib():
        """(libX11, Display*) or (None, None) without an X server."""
        global _x, _dpy
        if _dpy is None and _x is None:
            name = ctypes.util.find_library("X11") or "libX11.so.6"
            try:
                x = ctypes.CDLL(name)
            except OSError:
                _x = False
                return None, None
            V, W, A = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong
            x.XOpenDisplay.restype = V
            x.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x.XDefaultRootWindow.restype = W
            x.XDefaultRootWindow.argtypes = [V]
            x.XInternAtom.restype = A
            x.XInternAtom.argtypes = [V, ctypes.c_char_p, ctypes.c_int]
            x.XQueryTree.argtypes = [V, W, ctypes.POINTER(W), ctypes.POINTER(W),
                                     ctypes.POINTER(ctypes.POINTER(W)),
                                     ctypes.POINTER(ctypes.c_uint)]
            x.XGetClassHint.argtypes = [V, W, ctypes.POINTER(_XClassHint)]
            x.XGetWindowProperty.argtypes = [V, W, A, ctypes.c_long, ctypes.c_long,
                                             ctypes.c_int, A, ctypes.POINTER(A),
                                             ctypes.POINTER(ctypes.c_int),
                                             ctypes.POINTER(ctypes.c_ulong),
                                             ctypes.POINTER(ctypes.c_ulong),
                                             ctypes.POINTER(ctypes.c_void_p)]
            x.XFetchName.argtypes = [V, W, ctypes.POINTER(ctypes.c_void_p)]
            x.XGetWindowAttributes.argtypes = [V, W, ctypes.c_void_p]
            x.XMapWindow.argtypes = [V, W]
            x.XUnmapWindow.argtypes = [V, W]
            x.XReparentWindow.argtypes = [V, W, W, ctypes.c_int, ctypes.c_int]
            x.XFlush.argtypes = [V]
            x.XSync.argtypes = [V, ctypes.c_int]
            x.XFree.argtypes = [V]
            x.XSetErrorHandler.argtypes = [_ErrorHandler]
            d = x.XOpenDisplay(None)
            if not d:
                _x = False
                return None, None
            x.XSetErrorHandler(_x_error)
            _x, _dpy = x, d
        return (_x, _dpy) if _dpy else (None, None)

    def _atom(name: str) -> int:
        x, d = _xlib()
        if name not in _atoms:
            _atoms[name] = x.XInternAtom(d, name.encode(), 0)
        return _atoms[name]

    def _children(win: int) -> list[int]:
        x, d = _xlib()
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        kids, n = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if not x.XQueryTree(d, win, ctypes.byref(root), ctypes.byref(parent),
                            ctypes.byref(kids), ctypes.byref(n)):
            return []
        out = [kids[i] for i in range(n.value)]
        if kids:
            x.XFree(kids)
        return out

    def _parent(win: int) -> int:
        x, d = _xlib()
        root, parent = ctypes.c_ulong(), ctypes.c_ulong()
        kids, n = ctypes.POINTER(ctypes.c_ulong)(), ctypes.c_uint()
        if not x.XQueryTree(d, win, ctypes.byref(root), ctypes.byref(parent),
                            ctypes.byref(kids), ctypes.byref(n)):
            return 0
        if kids:
            x.XFree(kids)
        return parent.value

    def _is_host(win: int) -> bool:
        x, d = _xlib()
        ch = _XClassHint()
        if not x.XGetClassHint(d, win, ctypes.byref(ch)):
            return False
        ok = bool(ch.res_name) and ctypes.string_at(ch.res_name) == HOST_CLASS.encode()
        for p in (ch.res_name, ch.res_class):
            if p:
                x.XFree(p)
        return ok

    def _pid_of(win: int) -> int:
        x, d = _xlib()
        typ, fmt = ctypes.c_ulong(), ctypes.c_int()
        n, after, data = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        if x.XGetWindowProperty(d, win, _atom("_NET_WM_PID"), 0, 1, 0, 6,   # XA_CARDINAL
                                ctypes.byref(typ), ctypes.byref(fmt), ctypes.byref(n),
                                ctypes.byref(after), ctypes.byref(data)) != 0:
            return 0
        pid = 0
        if data.value and n.value and fmt.value == 32:
            pid = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0]
        if data.value:
            x.XFree(data)
        return int(pid)

    def _hosts(pid: int, under: int = 0, depth: int = 0) -> list[tuple[int, int]]:
        """(window, parent) of every host window of QEMU `pid`, anywhere in the tree."""
        x, d = _xlib()
        if not d:
            return []
        under = under or x.XDefaultRootWindow(d)
        out = []
        for w in _children(under):
            if _is_host(w) and _pid_of(w) == pid:
                out.append((w, under))
            elif depth < 6:
                out += _hosts(pid, w, depth + 1)
        return out

    def display_rotation(xid: int) -> int:
        x, d = _xlib()
        if not d or not xid:
            return 0
        name = ctypes.c_void_p()
        if not x.XFetchName(d, xid, ctypes.byref(name)) or not name.value:
            return 0
        t = ctypes.string_at(name.value).decode(errors="replace")
        x.XFree(name)
        return _title_rotation(t)

    def find_display_hwnd(pid: int) -> int:
        """XID of QEMU `pid`'s host window while it is top-level (0 = none / held)."""
        x, d = _xlib()
        if not d or not pid:
            return 0
        root = x.XDefaultRootWindow(d)
        for w in _children(root):
            if _is_host(w) and _pid_of(w) == pid:
                return int(w)
        return 0

    def describe_display(pid: int) -> str:
        x, d = _xlib()
        if not pid:
            return ""
        if not d:
            return "No X11 display (the native screen needs X11 or XWayland)"
        root = x.XDefaultRootWindow(d)
        hosts = _hosts(pid)
        top = [w for w, p in hosts if p == root]
        if top:
            return f"QEMU pid {pid}: screen window {top[0]:#x} is top-level (not embedded yet)"
        if hosts:
            return f"QEMU pid {pid}: screen window {hosts[0][0]:#x} is held by another window"
        return f"QEMU pid {pid}: no screen window yet (renderer not started?)"

    def _alive(xid: int) -> bool:
        x, d = _xlib()
        if not d:
            return False
        attrs = ctypes.create_string_buffer(256)       # XWindowAttributes (~136 bytes)
        return bool(x.XGetWindowAttributes(d, xid, attrs))

    def _show(xid: int) -> None:
        x, d = _xlib()
        if d:
            x.XMapWindow(d, xid)
            x.XFlush(d)

    def _release(xid: int) -> None:
        """Back to the root and hidden before our container dies: X destroys a window's
        inferiors with it, which would take QEMU's screen window down too (QEMU makes a
        new one, but the hand-back is cleaner)."""
        x, d = _xlib()
        if d and _alive(xid):
            x.XUnmapWindow(d, xid)
            x.XReparentWindow(d, xid, x.XDefaultRootWindow(d), 0, 0)
            x.XSync(d, 0)


class NativeDisplay(QWidget):
    """Hosts the renderer window, letterboxed to the guest's aspect ratio."""

    def __init__(self, pid_provider: Callable[[], Optional[int]],
                 aspect: tuple[int, int] = (1600, 900), parent=None):
        super().__init__(parent)
        self._pid_provider = pid_provider
        self._aspect = aspect
        self._rotation = 0
        self._lost = False             # our embedded window died while QEMU kept running
        self._pid_since = 0.0
        self._pid = 0
        self._hwnd = 0                 # HWND (Windows) / XID (X11)
        self._win: Optional[QWindow] = None
        self._container: Optional[QWidget] = None
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMinimumSize(320, 180)
        self.rotationChanged: Optional[Callable[[int], None]] = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(250)

    # -- state ------------------------------------------------------------------
    def state(self) -> str:
        if self._container is not None:
            return "native (GPU)"
        if self._lost and self._pid:
            return ("The screen window was lost (the player closed while it was embedded).\n"
                    "Stop and start this instance to get it back.")
        if self._pid and time.monotonic() - self._pid_since > 30:
            return ("Waiting for the screen...\nIf this instance was running when the player "
                    "closed unexpectedly, stop and start it.\n\n" + describe_display(self._pid))
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
            self._pid_since = time.monotonic()
            self._lost = False
        if not pid:
            return
        if self._hwnd and not _alive(self._hwnd):
            self.release()
            self._lost = True
            self.update()
        if self._container is None:
            hwnd = find_display_hwnd(pid)
            if hwnd:
                self._lost = False             # QEMU made a new one: embed it
                self._embed(hwnd)
            else:
                self.update()              # keep the waiting text current
        if self._hwnd:
            rot = display_rotation(self._hwnd)
            if rot != self._rotation:      # portrait <-> landscape: re-letterbox
                self._rotation = rot
                self._layout()
                if self.rotationChanged is not None:
                    self.rotationChanged(rot)

    def _embed(self, hwnd: int) -> None:
        win = QWindow.fromWinId(hwnd)
        if win is None:
            return
        self._hwnd, self._win = hwnd, win
        self._container = QWidget.createWindowContainer(win, self)
        self._container.setFocusPolicy(Qt.StrongFocus)
        if sys.platform != "win32":
            # Qt hands drops on a window container to the embedded window, and QEMU's
            # foreign window ignores them: let them reach the player (APK install).
            # Windows drops land on QEMU's HWND itself (WM_DROPFILES -> the player).
            self._container.setAcceptDrops(False)
        self._layout()
        self._container.show()
        _show(hwnd)

    def stop(self) -> None:
        """This display is done (its tab closed): stop polling and hand the window back.
        A removed tab's display that kept polling grabbed the next QEMU's screen window
        into a hidden widget, and the new tab waited forever."""
        self._timer.stop()
        self.release()

    def release(self) -> None:
        """Hand the window back to the desktop BEFORE our container dies: Windows (and
        X11) destroy child windows with their parent, which would take the renderer's
        present window (in QEMU) down with the player."""
        if self._hwnd:
            _release(self._hwnd)
        if self._container is not None:
            self._container.hide()
            self._container.setParent(None)
            self._container.deleteLater()
        self._container, self._win, self._hwnd = None, None, 0

    # -- layout / paint -------------------------------------------------------------
    def rotation(self) -> int:
        return self._rotation

    def _target_rect(self) -> QRect:
        aw, ah = self._aspect
        if self._rotation & 1:       # guest rotated 90/270: the picture is too
            aw, ah = ah, aw
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
        # letterbox strips (window not at the guest's aspect) blend with the window
        p.fillRect(self.rect(), self.palette().window())
        if self._container is None:
            p.setPen(QColor("#7d8590"))
            p.drawText(self.rect(), Qt.AlignCenter, self.state())
        p.end()

    def closeEvent(self, ev) -> None:
        self.release()
        super().closeEvent(ev)
