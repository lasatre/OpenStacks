"""Android notifications on the desktop, the way BlueStacks shows them.

The guest's BlueStacks services already forward every posted notification to the host
(hcall 44, OnAppNotificationReceived, JSON {"packageName", "appName", "contentTitle",
"contentText", "mSecsFromEpochUTC", ...}). QEMU's bstvmsg device writes each one to
logs/<name>.qemu.log as a single line "[bstvmsg] notification <JSON>"; NotificationWatch
follows that log and the Notifier shows them:

  * Linux: notify-send (libnotify; the freedesktop notification service of KDE, GNOME and
    the rest), falling back to the tray when it's missing;
  * Windows: a tray icon's message (a Windows toast), shown from the first notification on.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QSystemTrayIcon

from . import brand, paths

MARKER = b"[bstvmsg] notification "


def parse_line(line: bytes) -> Optional[dict]:
    """The notification on one QEMU log line, or None."""
    k = line.find(MARKER)
    if k < 0:
        return None
    try:
        n = json.loads(line[k + len(MARKER):].decode("utf-8", "replace"))
    except ValueError:
        return None
    return n if isinstance(n, dict) else None


def describe(n: dict, instance: str) -> tuple[str, str]:
    """(title, body) for the desktop: "App (instance)", then the title and text."""
    app = str(n.get("appName") or n.get("packageName") or "Android")
    parts = [str(n.get(k) or "").strip() for k in ("contentTitle", "contentText")]
    return f"{app} ({instance})", "\n".join(p for p in parts if p)


class NotificationWatch(QObject):
    """Follows a QEMU log from its current end and emits each new notification."""

    received = Signal(object)            # dict

    def __init__(self, log_path: Path, parent=None, interval_ms: int = 1000):
        super().__init__(parent)
        self.path = Path(log_path)
        try:
            self._off = self.path.stat().st_size      # only notifications from now on
        except OSError:
            self._off = 0
        self._partial = b""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._timer.start(interval_ms)

    def stop(self) -> None:
        self._timer.stop()

    def poll(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self._off:                           # recreated/truncated
            self._off, self._partial = 0, b""
        if size == self._off:
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self._off)
                data = f.read(min(size - self._off, 1 << 20))
        except OSError:
            return
        self._off += len(data)
        lines = (self._partial + data).split(b"\n")
        self._partial = lines.pop()[-65536:]           # an unfinished line: next time
        for line in lines:
            n = parse_line(line)
            if n is not None:
                self.received.emit(n)


class Notifier(QObject):
    """Shows notifications on the desktop (one per process, see notifier())."""

    clicked = Signal()                   # Windows: the toast was clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tray: Optional[QSystemTrayIcon] = None
        self._send = shutil.which("notify-send") if sys.platform.startswith("linux") else None
        self._icon = paths.brand("png", "openstacks-icon-128.png")
        self._recent: dict[tuple[str, str], float] = {}

    def show(self, title: str, body: str) -> bool:
        """False when it was a repeat (not shown)."""
        now = time.monotonic()
        # apps re-post the same ongoing notification (progress, "VPN active"): once a minute
        if now - self._recent.get((title, body), -1e9) < 60:
            return False
        self._recent = {k: t for k, t in self._recent.items() if now - t < 60}
        self._recent[(title, body)] = now
        if self._send:
            try:
                subprocess.Popen([self._send, "--app-name=OpenStacks",
                                  f"--icon={self._icon}", "--", title, body],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            except OSError:
                self._send = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return True
        if self._tray is None:
            self._tray = QSystemTrayIcon(brand.app_icon(), self)
            self._tray.setToolTip("OpenStacks")
            self._tray.messageClicked.connect(self.clicked)
            self._tray.activated.connect(lambda _reason: self.clicked.emit())
            self._tray.show()
        self._tray.showMessage(title, body, brand.app_icon(), 8000)
        return True


_NOTIFIER: Optional[Notifier] = None


def notifier() -> Notifier:
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = Notifier()
    return _NOTIFIER
