"""The on-screen key overlay: badges showing which key does what, like BlueStacks'.

The overlay is a frameless, translucent, click-through tool window kept over the game
picture. The picture is QEMU's native window embedded in the player, and Qt cannot paint
over a foreign window from inside it.

The overlay is owned by the window showing the picture: the player, or the popped-out
screen window. So it stacks just above that window (not above every other app), and
minimizes and hides with it.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import keys
from .bscfg import control_type


def _badge_text(c: dict, *fields: str) -> str:
    for f in fields:
        for k in (c.get(f), c.get(f + "_alt1")):
            if k:
                return " + ".join(keys.label(p) for p in keys.split_combo(k))
    return ""


class Overlay(QWidget):
    FLAGS = (Qt.Tool | Qt.FramelessWindowHint | Qt.WindowTransparentForInput |
             Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)

    def __init__(self, rect_provider: Callable[[], Optional[QRect]],
                 controls_provider: Callable[[], list[dict]],
                 aiming_provider: Callable[[], bool] = lambda: False,
                 owner_provider: Callable[[], Optional[QWidget]] = lambda: None):
        super().__init__(owner_provider(), self.FLAGS)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self._rect = rect_provider
        self._controls = controls_provider
        self._aiming = aiming_provider
        self._owner = owner_provider
        self.opacity = 0.75
        self.wanted = True
        # the owner's move/resize events reposition it at once (no visible lag while the
        # window is dragged); the timer catches layout changes inside the window
        self._watched: Optional[QObject] = None
        self._follow = QTimer(self, interval=150)
        self._follow.timeout.connect(self.sync)

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() in (QEvent.Move, QEvent.Resize, QEvent.WindowStateChange, QEvent.Show,
                         QEvent.Hide):
            self.sync()
        return False

    def _watch(self, owner: Optional[QWidget]) -> None:
        if owner is self._watched:
            return
        if self._watched is not None:
            self._watched.removeEventFilter(self)
        self._watched = owner
        if owner is not None:
            owner.installEventFilter(self)

    def start(self) -> None:
        self._follow.start()
        self.sync()

    def stop(self) -> None:
        self._follow.stop()
        self._watch(None)
        self.hide()

    def sync(self) -> None:
        """Follow the picture: position, size and visibility."""
        owner = self._owner()
        self._watch(owner)
        if owner is not None and self.parentWidget() is not owner:
            self.hide()
            self.setParent(owner, self.FLAGS)        # the screen was popped out / back
        r = self._rect() if self.wanted else None
        if r is None or r.width() < 50 or not self._controls():
            if self.isVisible():
                self.hide()
            return
        if self.geometry() != r:
            self.setGeometry(r)
        if not self.isVisible():
            self.show()
        self.update()

    # -- painting
    def _pt(self, x: float, y: float) -> QPointF:
        return QPointF(x / 100.0 * self.width(), y / 100.0 * self.height())

    def _badge(self, p: QPainter, at: QPointF, text: str, radius: float = 0.0) -> None:
        if not text:
            return
        fm = p.fontMetrics()
        w = max(fm.horizontalAdvance(text) + 12, 24)
        h = fm.height() + 6
        box = QRectF(at.x() - w / 2, at.y() - h / 2, w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(box, h / 2, h / 2)
        p.setPen(QColor(255, 255, 255))
        p.drawText(box, Qt.AlignCenter, text)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self.opacity)
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, self.height() / 70))
        f.setBold(True)
        p.setFont(f)
        ring = QPen(QColor(255, 255, 255, 200), 2)
        for c in self._controls():
            if not c.get("ShowOnOverlay", True):
                continue
            t = control_type(c)
            try:
                x, y = float(c.get("X", 0)), float(c.get("Y", 0))
            except (TypeError, ValueError):
                continue
            if t == "Dpad":
                r = float(c.get("XRadius", 9) or 9) / 100.0 * self.width()
                ctr = self._pt(x, y)
                p.setPen(ring)
                p.setBrush(QColor(0, 0, 0, 60))
                p.drawEllipse(ctr, r, r)
                for f_, dx, dy in (("KeyUp", 0, -1), ("KeyDown", 0, 1),
                                   ("KeyLeft", -1, 0), ("KeyRight", 1, 0)):
                    self._badge(p, QPointF(ctr.x() + dx * r * 0.7, ctr.y() + dy * r * 0.7),
                                _badge_text(c, f_))
            elif t == "Pan":
                self._badge(p, self._pt(x, y), _badge_text(c, "KeyStartStop") or "Aim")
                if self._aiming():
                    cx = float(c.get("CrosshairX", 50) or 50)
                    cy = float(c.get("CrosshairY", 50) or 50)
                    ctr = self._pt(cx, cy)
                    p.setPen(QPen(QColor(c.get("CrosshairColor") or "#FFFFFF"), 2))
                    p.drawLine(QPointF(ctr.x() - 10, ctr.y()), QPointF(ctr.x() + 10, ctr.y()))
                    p.drawLine(QPointF(ctr.x(), ctr.y() - 10), QPointF(ctr.x(), ctr.y() + 10))
                if c.get("IsShootOnClickEnabled", True) and "LButtonX" in c:
                    self._badge(p, self._pt(float(c["LButtonX"]), float(c["LButtonY"])),
                                _badge_text(c, "KeyAction") or "LMB")
            elif t == "Swipe":
                x1, y1 = float(c.get("X1", x)), float(c.get("Y1", y))
                x2, y2 = float(c.get("X2", x)), float(c.get("Y2", y))
                p.setPen(QPen(QColor(255, 255, 255, 160), 2, Qt.DashLine))
                p.drawLine(self._pt(x1, y1), self._pt(x2, y2))
                self._badge(p, self._pt(x1, y1), _badge_text(c, "Key"))
            elif t == "Zoom":
                x1, y1 = float(c.get("X1", x)), float(c.get("Y1", y))
                x2, y2 = float(c.get("X2", x)), float(c.get("Y2", y))
                mid = self._pt((x1 + x2) / 2, (y1 + y2) / 2)
                self._badge(p, mid, " / ".join(filter(None, (_badge_text(c, "KeyIn"),
                                                             _badge_text(c, "KeyOut")))))
            elif t in ("Tap", "TapRepeat", "Script", "State", "MOBASkill", "FreeLook",
                       "Rotate", "Tilt"):
                self._badge(p, self._pt(x, y), _badge_text(c, "Key", "KeyActivate"))
        p.end()
