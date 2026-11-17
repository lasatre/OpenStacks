"""Player display widget + input plumbing for OpenStacks.

Abstractions designed for later swapping:
  * VncDisplay      - the QImage-on-QPainter widget rendering the guest
                      framebuffer (currently fed by the pure-python RFB
                      stream, but the frame source is just a callback); maps
                      widget<->guest coordinates, forwards mouse/keys to an
                      InputBridge.
  * InputBridge     - interface for getting input INTO the guest. Two
                      implementations:
                        VncInputBridge - QEMU VNC path (primary): PointerEvent
                        and KeyEvent over the same RFB socket.
                        AdbInputBridge - pip-through-ADB option: `adb shell
                        input tap/keyevent` on press events.
  * InputWorker     - a queue-consuming worker thread so UI stays responsive
                      while raw adb / RFB sends happen off-thread.

Adding a native framebuffer pipe later == make a new "bridge"-style provider
that emits (w, h, bgrx_bytes) and skip RfbClient entirely.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal, QPoint, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QLabel

from .adb import Adb, AdbError, keysym_to_keycode

# X11 keysyms for Qt keys that aren't plain printable ASCII
_SPECIAL_QT_TO_KEYSYM = {
    Qt.Key_Return: 0xFF0D, Qt.Key_Enter: 0xFF8D, Qt.Key_Backspace: 0xFF08,
    Qt.Key_Tab: 0xFF09, Qt.Key_Backtab: 0xFF89, Qt.Key_Escape: 0xFF1B,
    Qt.Key_Delete: 0xFFFF, Qt.Key_Insert: 0xFF63, Qt.Key_Home: 0xFF50,
    Qt.Key_End: 0xFF57, Qt.Key_PageUp: 0xFF55, Qt.Key_PageDown: 0xFF56,
    Qt.Key_Left: 0xFF51, Qt.Key_Up: 0xFF52, Qt.Key_Right: 0xFF53,
    Qt.Key_Down: 0xFF54, Qt.Key_Space: 0x0020, Qt.Key_Menu: 0xFF67,
    Qt.Key_Shift: 0xFFE1, Qt.Key_Control: 0xFFE3, Qt.Key_Alt: 0xFFE9,
    Qt.Key_Meta: 0xFFEB, Qt.Key_CapsLock: 0xFFE5, Qt.Key_NumLock: 0xFF7F,
}
for _i, _k in enumerate(range(Qt.Key_F1, Qt.Key_F12 + 1)):
    _SPECIAL_QT_TO_KEYSYM[_k] = 0xFFBE + _i


def qt_key_to_keysym(key: int, text: str) -> int:
    holder = _SPECIAL_QT_TO_KEYSYM.get(key)
    if holder is not None:
        return holder
    if text and text.isprintable():
        return ord(text[0])                       # shifted letters give uppercase
    return 0


class InputBridge:
    """Interface: inject input into the guest. Subclasses override pointer/key."""

    def pointer(self, x: int, y: int, buttons_mask: int, pressed: bool) -> None:
        raise NotImplementedError

    def key(self, keysym: int, down: bool) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return "none"


class VncInputBridge(InputBridge):
    """Primary path: mouse/keyboard over the RFB socket itself."""

    def __init__(self, vnc_thread_provider: Callable[[], Optional[object]]):
        self._prov = vnc_thread_provider   # -> VncClientThread | None

    def pointer(self, x, y, buttons_mask, pressed):
        t = self._prov()
        if t is not None:
            t.send_pointer(x, y, buttons_mask)

    def key(self, keysym, down):
        t = self._prov()
        if t is not None:
            t.send_keysym(keysym, down)

    def describe(self) -> str:
        return "vnc"


class AdbInputBridge(InputBridge):
    """Alt path: `adb shell input tap` on click / `input keyevent` on press.

    Press-driven mouse: a left press fires `input tap` right away (snappy
    feedback), and if the pointer has moved far enough by release a
    `input swipe x1 y1 x2 y2 ms` is sent for drag gestures.

    All failures go to `on_error(msg)` (the player flashes them in its status
    bar); default is a console print. Nothing here ever raises - a dead adb
    connection degrades to a status message, not a crash.
    """

    SWIPE_MIN_DIST = 20.0   # widget-guest px before a press counts as a drag

    def __init__(self, adb_provider: Callable[[], Adb],
                 on_error: Optional[Callable[[str], None]] = None):
        self._prov = adb_provider
        self._on_error = on_error or (lambda msg: print(f"[openstacks] adb: {msg}"))
        self._own = 0          # buttons held last report
        self._press: Optional[tuple[int, int]] = None
        self._cur: tuple[int, int] = (0, 0)

    def _fail(self, what: str, e: AdbError) -> None:
        try:
            self._on_error(f"{what} failed: {e}")
        except Exception:
            pass

    def pointer(self, x, y, buttons_mask, pressed):
        new = buttons_mask & ~self._own
        self._own = buttons_mask
        if pressed and new and buttons_mask & 1:      # left press -> tap
            self._press = (int(x), int(y))
            self._cur = (int(x), int(y))
            try:
                self._prov().tap(x, y)
            except AdbError as e:
                self._fail("adb tap", e)
        elif self._press is not None:
            self._cur = (int(x), int(y))
            if not pressed:                            # release -> maybe swipe
                x1, y1 = self._press
                x2, y2 = self._cur
                self._press = None
                # (0,0) coords mean "release outside the framebuffer" -> cancel
                if x2 > 0 and y2 > 0 and \
                        (x2 - x1) ** 2 + (y2 - y1) ** 2 >= self.SWIPE_MIN_DIST ** 2:
                    try:
                        self._prov().swipe(x1, y1, x2, y2, ms=150)
                    except AdbError as e:
                        self._fail("adb swipe", e)

    def key(self, keysym, down):
        if not down:
            return
        code = keysym_to_keycode(keysym)
        try:
            if code is not None:
                self._prov().keyevent(code)
            elif 0x20 <= keysym < 0x7F:
                self._prov().text(chr(keysym))
        except AdbError as e:
            self._fail("adb key", e)

    def describe(self) -> str:
        return "adb"


class InputWorker(QThread):
    """Queue-consuming worker thread (keeps adb/RFB sends off the GUI thread)."""

    _POINTER, _KEY = 0, 1

    def __init__(self, bridge_provider: Callable[[], Optional[InputBridge]]):
        super().__init__()
        import queue as _q
        self._q: _q.Queue = _q.Queue()
        self._bridge_provider = bridge_provider
        self._stop = False

    def run(self) -> None:
        import queue as _q
        while not self._stop:
            try:
                kind, payload = self._q.get(timeout=0.2)
            except _q.Empty:
                continue
            bridge = self._bridge_provider()
            if bridge is None:
                continue
            try:
                if kind == self._POINTER:
                    x, y, mask, pressed = payload
                    bridge.pointer(x, y, mask, pressed)
                else:
                    keysym, down = payload
                    bridge.key(keysym, down)
            except Exception as e:                    # keep the worker alive
                print(f"[openstacks] input dispatch failed: {e}")

    def stop(self, wait_ms: int = 2000) -> None:
        self._stop = True
        if self.isRunning():
            self.wait(wait_ms)

    # enqueue helpers -----------------------------------------------------------
    def pointer(self, x: int, y: int, buttons_mask: int, pressed: bool) -> None:
        self._q.put((self._POINTER, (int(x), int(y), buttons_mask, pressed)))

    def key(self, keysym: int, down: bool) -> None:
        self._q.put((self._KEY, (keysym, down)))


# -- widget ---------------------------------------------------------------------

class VncDisplay(QLabel):
    """Renders the guest framebuffer; maps widget<->guest coordinates; sends input."""

    statusTextChanged = Signal(str)

    def __init__(self, parent=None, input_provider: Callable[[], Optional[InputBridge]] = None,
                 bridge_mode_provider: Callable[[], str] = None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self._img: QImage | None = None
        self._fb_w, self._fb_h = (1600, 900)
        self._buttons_down = 0
        self._input_provider = input_provider or (lambda: None)
        self._bridge_mode_provider = bridge_mode_provider or (lambda: "vnc")

    # -- frame feeding -----------------------------------------------------------
    def set_frame(self, w: int, h: int, raw: bytes) -> None:
        """Feed a complete BGRX frame (called from the VNC background thread)."""
        if len(raw) < w * h * 4:
            return
        self._fb_w, self._fb_h = w, h
        img = QImage(bytes(raw), w, h, w * 4, QImage.Format_RGB32)
        self._img = img.copy()
        self.update()         # repaint in the GUI thread (signal hop upstream)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101014"))
        if self._img is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(self._target_rect(), self._img, self._img.rect())

    def _target_rect(self) -> QRectF:
        """Letterboxed smallest KeepAspectRatio rect filling the widget."""
        if self._img is None:
            return QRectF()
        sw = self._fb_w
        sh = self._fb_h
        cw, ch = self.width(), self.height()
        scale = min(cw / sw, ch / sh)
        dw, dh = sw * scale, sh * scale
        x, y = (cw - dw) / 2.0, (ch - dh) / 2.0
        return QRectF(x, y, dw, dh)

    def resizeEvent(self, ev) -> None:
        self.update()
        super().resizeEvent(ev)

    # -- coordinates -------------------------------------------------------------
    def widget_to_fb(self, px: int, py: int) -> Optional[tuple[float, float]]:
        r = self._target_rect()
        if r.width() <= 0 or r.height() <= 0:
            return None
        fx = (px - r.x()) / r.width() * self._fb_w
        fy = (py - r.y()) / r.height() * self._fb_h
        if fx < 0 or fy < 0 or fx >= self._fb_w or fy >= self._fb_h:
            return None
        return fx, fy

    def fb_to_widget(self, fx: float, fy: float) -> QPoint:
        r = self._target_rect()
        return QPoint(int(r.x() + fx / self._fb_w * r.width()),
                      int(r.y() + fy / self._fb_h * r.height()))

    # -- mouse -------------------------------------------------------------------
    def _mask_from_buttons(self, buttons) -> int:
        mask = 0
        if buttons & Qt.LeftButton:
            mask |= 1
        if buttons & Qt.MiddleButton:
            mask |= 2
        if buttons & Qt.RightButton:
            mask |= 4
        return mask

    def _push_pointer(self, ev, pressed: bool) -> None:
        pos = self.widget_to_fb(ev.position().x(), ev.position().y())
        if pos is None and not pressed:
            self._buttons_down = 0
            bridge = self._input_provider()
            if bridge:
                bridge.pointer(0, 0, 0, pressed)  # release-outside
            return
        mask = self._mask_from_buttons(ev.buttons())
        if pressed:
            self._buttons_down = mask
            self.setFocus()
        bridge = self._input_provider()
        if bridge and pos is not None:
            bridge.pointer(int(pos[0]), int(pos[1]), mask, pressed)

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.NoButton:
            self._push_pointer(ev, True)
            ev.accept()
        else:
            super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        self._push_pointer(ev, False)
        if ev.button() != Qt.NoButton:
            ev.accept()
        else:
            super().mouseReleaseEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self._buttons_down:
            self._push_pointer(ev, False)
            ev.accept()
        else:
            super().mouseMoveEvent(ev)

    def wheelEvent(self, ev) -> None:  # stub: RFB 3.x has no wheel; adb could page
        ev.accept()

    # -- keyboard ------------------------------------------------------------
    def keyPressEvent(self, ev) -> None:
        self._send_key(ev.key(), ev.text(), True)
        ev.accept()

    def keyReleaseEvent(self, ev) -> None:
        self._send_key(ev.key(), ev.text(), False)
        ev.accept()

    def _send_key(self, key: int, text: str, down: bool) -> None:
        ks = qt_key_to_keysym(key, text)
        if not ks:
            return
        bridge = self._input_provider()
        if bridge:
            bridge.key(ks, down)

    # -- drag&drop APK install hook ------------------------------------------
    def dragEnterEvent(self, ev):
        urls = ev.mimeData().urls() if ev.mimeData() else []
        if any(u.toLocalFile().lower().endswith(".apk") for u in urls):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in (ev.mimeData().urls() or []):
            f = url.toLocalFile()
            if f.lower().endswith(".apk") and hasattr(self.parent(), "install_apk"):
                self.parent().install_apk(f)
        ev.acceptProposedAction()
