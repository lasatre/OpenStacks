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
        _register_app_id()


def _register_app_id() -> None:
    """Name and icon for APP_ID (per user). Without them Windows heads notifications with the
    raw id ("OpenStacks.OpenStacks") and no icon: the id has no Start-menu shortcut."""
    try:
        import winreg
        icon = paths.brand("png", "openstacks-icon-128.png")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\AppUserModelId\{APP_ID}") as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "OpenStacks")
            if icon.exists():
                winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(icon.resolve()))
    except OSError:
        pass


def accent(dark: bool | None = None) -> QColor:
    return QColor(INDIGO_LIGHT if (is_dark() if dark is None else dark) else INDIGO)


# -- theme (Settings > Appearance, GlobalSettings.theme) ----------------------------------
THEMES = (("system", "Follow the system"), ("light", "Light"), ("dark", "Dark"))
_base_style = ""
_custom = False             # Fusion + our palette in use


def _palette(dark: bool) -> QPalette:
    """A complete light or dark palette for the Fusion style (used where the platform does
    not honour a colour-scheme request, e.g. Linux without a desktop portal)."""
    if dark:
        win, base, alt, text, btn = "#202124", "#17181a", "#2a2b2e", "#e8e8ea", "#2d2e31"
        dis, hl, link, tip = "#7d7e82", INDIGO_LIGHT, "#8b8ff8", "#2d2e31"
    else:
        win, base, alt, text, btn = "#f3f3f3", "#ffffff", "#f5f5f7", "#1c1c1e", "#fbfbfb"
        dis, hl, link, tip = "#a0a0a4", INDIGO, INDIGO, "#ffffdc"
    p = QPalette()
    for role, c in ((QPalette.Window, win), (QPalette.WindowText, text),
                    (QPalette.Base, base), (QPalette.AlternateBase, alt),
                    (QPalette.Text, text), (QPalette.Button, btn),
                    (QPalette.ButtonText, text), (QPalette.BrightText, "#ff5555"),
                    (QPalette.Highlight, hl), (QPalette.HighlightedText, "#ffffff"),
                    (QPalette.Link, link), (QPalette.ToolTipBase, tip),
                    (QPalette.ToolTipText, text), (QPalette.PlaceholderText, dis)):
        p.setColor(role, QColor(c))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(dis))
    return p


def apply_theme(app, mode: str) -> None:
    """mode: "system", "light" or "dark". Asks Qt for the colour scheme first (the native
    style follows it on Windows, and on Linux with a desktop portal); where the palette does
    not follow, switches to Fusion with an explicit palette."""
    global _base_style, _custom
    if not _base_style:
        _base_style = app.style().name()
    if _custom:                                      # back to the platform's own look first
        app.setStyle(_base_style)
        app.setPalette(QPalette())
        _custom = False
    hints = app.styleHints()
    if mode in ("light", "dark"):
        want_dark = mode == "dark"
        try:
            hints.setColorScheme(Qt.ColorScheme.Dark if want_dark else Qt.ColorScheme.Light)
        except AttributeError:                       # Qt < 6.8
            pass
        app.processEvents()
        if is_dark() != want_dark:
            app.setStyle("Fusion")
            app.setPalette(_palette(want_dark))
            _custom = True
    else:
        try:
            hints.unsetColorScheme()
        except AttributeError:
            pass
        app.processEvents()
