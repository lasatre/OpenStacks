"""Line icons for the player's side bar, drawn with QPainter (no image files).

Each icon is painted on a 24x24 logical canvas with a 1.8 px stroke and
rendered at 1x/2x/3x so it stays crisp on high-DPI screens.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

STROKE = QColor("#d6dbe1")
STROKE_DISABLED = QColor("#5d6673")


def _arrow_head(p: QPainter, tip: QPointF, angle_deg: float, size: float = 4.0) -> None:
    a = math.radians(angle_deg)
    for side in (-1, 1):
        b = a + math.pi + side * math.radians(35)
        p.drawLine(tip, QPointF(tip.x() + size * math.cos(b), tip.y() + size * math.sin(b)))


def _draw(name: str, p: QPainter) -> None:
    if name == "back":                      # Android back: left-pointing triangle
        path = QPainterPath(QPointF(16, 5))
        path.lineTo(7, 12)
        path.lineTo(16, 19)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "home":                    # Android home: circle
        p.drawEllipse(QRectF(6, 6, 12, 12))
    elif name == "recents":                 # Android recents: rounded square
        p.drawRoundedRect(QRectF(6, 6, 12, 12), 2, 2)
    elif name == "rotate":                  # circular arrow
        p.drawArc(QRectF(5, 5, 14, 14), 60 * 16, 280 * 16)
        _arrow_head(p, QPointF(15.6, 6.0), -30)
    elif name == "screenshot":              # camera
        p.drawRoundedRect(QRectF(4, 8, 16, 11), 2, 2)
        p.drawRect(QRectF(9, 5.5, 6, 2.5))
        p.drawEllipse(QRectF(9, 10.5, 6, 6))
    elif name == "install_apk":             # box with a down arrow
        p.drawRoundedRect(QRectF(5, 12, 14, 8), 1.5, 1.5)
        p.drawLine(QPointF(12, 4), QPointF(12, 14))
        _arrow_head(p, QPointF(12, 14.5), 90, 4.5)
    elif name == "pop_screen":              # window with an arrow leaving it
        p.drawRoundedRect(QRectF(4, 7, 13, 13), 2, 2)
        p.drawLine(QPointF(11, 13), QPointF(20, 4))
        _arrow_head(p, QPointF(20, 4), -45, 5)
    elif name == "keymap":                  # keyboard
        p.drawRoundedRect(QRectF(3, 7, 18, 11), 2, 2)
        for x in (6.5, 10, 13.5, 17):
            p.drawPoint(QPointF(x, 10.5))
        p.drawLine(QPointF(8, 14.5), QPointF(16, 14.5))
    elif name == "multitouch":              # two touch points with ripples
        for cx, cy in ((8, 9), (16, 15)):
            p.drawEllipse(QPointF(cx, cy), 1.6, 1.6)
            p.drawEllipse(QPointF(cx, cy), 4.5, 4.5)
    elif name == "input":                   # mouse pointer
        path = QPainterPath(QPointF(7, 4))
        path.lineTo(7, 18)
        path.lineTo(11, 14.5)
        path.lineTo(14, 20)
        path.lineTo(16, 19)
        path.lineTo(13, 13.5)
        path.lineTo(18, 13.5)
        path.closeSubpath()
        p.drawPath(path)
    else:                                   # unknown: a dot
        p.drawEllipse(QRectF(10, 10, 4, 4))


def icon(name: str) -> QIcon:
    """Stroke in the theme's text colour so the icons match the rest of the UI."""
    from PySide6.QtGui import QGuiApplication, QPalette
    pal = QGuiApplication.palette()
    normal = pal.color(QPalette.Active, QPalette.WindowText)
    disabled = pal.color(QPalette.Disabled, QPalette.WindowText)
    ic = QIcon()
    for mode, color in ((QIcon.Normal, normal), (QIcon.Disabled, disabled)):
        for scale in (1, 2, 3):
            pm = QPixmap(24 * scale, 24 * scale)
            pm.setDevicePixelRatio(scale)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(color, 1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            _draw(name, p)
            p.end()
            ic.addPixmap(pm, mode)
    return ic
