"""OpenStacks branding for the Qt UI: app icon, logo (light/dark), colors (brand/README.md)."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPalette, QPixmap

from . import __version__, paths

# brand/README.md "Colors"
INDIGO_DEEP = "#1E1B4B"     # app icon tile, "Open" on light
INDIGO = "#4F46E5"          # "Stacks" on light
INDIGO_LIGHT = "#6366F1"    # phone (front) on dark
TEAL = "#0D9488"            # back layers on light
TEAL_LIGHT = "#2DD4BF"      # back layers on dark
APP_ID = "OpenStacks.OpenStacks"


def is_dark() -> bool:
    pal = QGuiApplication.palette()
    return pal.color(QPalette.Window).lightness() < 128


def app_icon() -> QIcon:
    icon = QIcon()
    ico = paths.brand("ico", "openstacks.ico")
    if ico.exists():
        icon.addFile(str(ico))
    for s in (16, 32, 48, 64, 128, 256, 512):
        p = paths.brand("png", f"openstacks-icon-{s}.png")
        if p.exists():
            icon.addFile(str(p))
    return icon


def logo(height: int, stacked: bool = False, dark: bool | None = None) -> QPixmap:
    """The wordmark lockup scaled to `height` px (device-pixel aware), matching the theme."""
    dark = is_dark() if dark is None else dark
    name = "openstacks-logo" + ("-stacked" if stacked else "") + ("-on-dark" if dark else "")
    pm = QPixmap(str(paths.brand("png", name + "-2000w.png")))
    if pm.isNull():
        return pm
    dpr = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    pm = pm.scaledToHeight(int(height * dpr), Qt.SmoothTransformation)
    pm.setDevicePixelRatio(dpr)
    return pm


def apply(app) -> None:
    """Window icon for every window + our own taskbar entry on Windows (not python's)."""
    app.setApplicationName("OpenStacks")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except (AttributeError, OSError):
            pass


def accent(dark: bool | None = None) -> QColor:
    return QColor(INDIGO_LIGHT if (is_dark() if dark is None else dark) else INDIGO)
