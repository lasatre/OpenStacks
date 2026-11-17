"""Per-instance console tailing for OpenStacks.

Two layers so headless CLI tools can reuse the pure-python half:

  * TailBuffer   - offset-based append-tailing of a (growing) log file.
                   Tracks the byte offset, backfills the last N lines on
                   attach, survives truncation/recreation, never raises.
  * ConsolePane  - Qt widget (QFileSystemWatcher + a small safety QTimer)
                   fed by a TailBuffer. The watcher alone can miss rapid
                   appends on Windows, hence the belt-and-braces timer.

Log sources (both per-instance, written by the engine's QEMU process):
  * serial log:  logs/<name>.serial.log   (-serial file:... - boot console)
  * qemu log:    logs/<name>.qemu.log     (QEMU stdout+stderr - carries the
                  [gr_render]/[bstvmsg]/[vmmdev] debug counters)

Qt-free until a ConsolePane is instantiated, so the headless selftest can use
TailBuffer without a display server.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, List, Optional

# NOTE: this module imports Qt at import time (ConsolePane subclasses QWidget;
# the player is the only consumer). The *pure-python* half (TailBuffer,
# filter_counter_lines) stays free of Qt calls, which is what the headless
# selftest uses.
from PySide6.QtCore import QObject, QFileSystemWatcher, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

# --- pure-python tail (no Qt; safe for CLI/selftest use) ----------------------


class TailBuffer:
    """Reads appended bytes from a file; exposes them as decoded lines."""

    def __init__(self, path: str | Path, max_lines: int = 5000,
                 encoding: str = "utf-8", errors: str = "replace"):
        self.path = Path(path)
        self.max_lines = max_lines
        self.encoding = encoding
        self.errors = errors
        self._offset = 0
        self._lines: List[str] = []
        self._attached = False

    def attach(self, backfill: int = 300) -> List[str]:
        """Initial fill: the last `backfill` lines already in the file."""
        self._lines = self._read_all_lines()[-backfill:]
        self._attached = True
        try:
            self._offset = self.path.stat().st_size
        except OSError:
            self._offset = 0
        return list(self._lines)

    def poll(self, limit: int = 8192) -> List[str]:
        """New lines since the last call ([] when nothing changed)."""
        self._check_rotation()          # handle truncate/recreate before reading
        new: List[str] = []
        chunks = 0
        while chunks < 8:                      # cap work per timer tick
            chunk = self._read_from(self._offset)
            if not chunk:
                break
            self._offset += len(chunk)
            new += chunk.splitlines()
            chunks += 1
            if len(new) > limit:
                new = new[-limit:]
                break
        if new:
            self._lines = (self._lines + new)[-self.max_lines:]
        return new

    # -- helpers ---------------------------------------------------------------
    def _read_from(self, offset: int) -> str:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(offset)
                return fh.read(65536).decode(self.encoding, errors=self.errors)
        except OSError:
            return ""

    def _read_all_lines(self) -> List[str]:
        try:
            return self.path.read_text(encoding=self.encoding,
                                       errors=self.errors).splitlines()
        except OSError:
            return []

    def _check_rotation(self) -> None:
        """QEMU writes serial files append-only, but recreate-safe is cheap."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self._offset:
            self._offset = 0          # file truncated/recreated -> re-read

    # -- API for UI -------------------------------------------------------------
    def tail(self, n: int = 50) -> List[str]:
        return list(self._lines[-n:])

    @property
    def lines(self) -> List[str]:
        return list(self._lines)

    @property
    def attached(self) -> bool:
        return self._attached


_COUNTER_RE = re.compile(
    r"\[(?:gr_render|grbridge|grsyn|bstvmsg|bstpgaipc|vmmdev|bstv[a-z]*)\]")


def filter_counter_lines(lines: Iterable[str]) -> List[str]:
    """Lines carrying the [gr_render]/[bstvmsg]/[vmmdev]-style debug counters."""
    return [ln for ln in lines if _COUNTER_RE.search(ln)]


# --- Qt layer ------------------------------------------------------------------


def make_console_pane(tail: TailBuffer, *, title_hint: str = "",
                      counter_filter: bool = False, poll_ms: int = 500,
                      backfill: int = 300):
    """Build (widget, controller) tailing `tail` into a QPlainTextEdit."""
    class Controller(QObject):
        infoChanged = Signal(str)

        def __init__(self, widget: QWidget, view: QPlainTextEdit, parent=None):
            super().__init__(parent)
            self.widget = widget
            self.view = view
            self.tail = tail
            self.line_filter: Optional[Callable[[List[str]], List[str]]] = (
                filter_counter_lines if counter_filter else None)
            self._watcher = QFileSystemWatcher(self)
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._drain)
            self._paused = False
            self._missing_shown = False

        # -- lifecycle ----------------------------------------------------------
        def start(self):
            self.tail.attach(backfill=backfill)
            if self.tail.lines:
                self.view.appendPlainText("\n".join(self.tail.tail(backfill)))
            self._scroll_end()
            self.infoChanged.emit(
                f"attached: {self.tail.path} "
                f"(backfill {len(self.tail.lines)} lines)")
            parent = str(self.tail.path.parent)
            try:
                if parent not in self._watcher.directories():
                    self._watcher.addPath(parent)
            except OSError:
                pass           # watcher unavailable -> the safety timer still runs
            self._watcher.directoryChanged.connect(lambda _p: self._drain())
            self._timer.start(poll_ms)
            self._drain()

        def stop(self):
            self._timer.stop()

        # -- update pump ----------------------------------------------------------
        def set_paused(self, on: bool) -> None:
            self._paused = on

        def _drain(self):
            if self._paused:
                return
            try:
                new = self.tail.poll()
            except Exception:                    # never crash the GUI on IO quirks
                return
            if self.line_filter is not None and new:
                shown = self.line_filter(new)
            else:
                shown = new
            if shown:
                self.view.appendPlainText("\n".join(shown))
                self._scroll_end()
                self._missing_shown = False
            elif not shown and not self.tail.path.exists() and not self._missing_shown:
                self._missing_shown = True
                self.view.appendPlainText(
                    f"--- waiting for {self.tail.path.name} "
                    "(QEMU writes it on start) ---")

        def _scroll_end(self):
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    widget = QWidget()
    lay = QVBoxLayout(widget)
    lay.setContentsMargins(2, 2, 2, 2)

    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setMaximumBlockCount(4000)
    view.setLineWrapMode(QPlainTextEdit.NoWrap)
    view.setStyleSheet("QPlainTextEdit { background:#0d1117; color:#c9d1d9; "
                       "font-family:Consolas,'Courier New',monospace; "
                       "font-size:12px; }")
    lay.addWidget(view, 1)

    follow = QCheckBox("Follow")
    follow.setChecked(True)
    pause = QPushButton("Pause")
    pause.setCheckable(True)
    pause.setFixedWidth(84)
    clear = QPushButton("Clear")
    clear.setFixedWidth(72)
    if not title_hint:
        title_hint = f"tail: {tail.path.name}"
        if counter_filter:
            title_hint += "  [counter filter: gr_render/bstvmsg/vmmdev]"
    hint = QLabel(title_hint, widget)
    hint.setStyleSheet("color:#8b949e; font-size:11px;")

    bar = QHBoxLayout()
    bar.addWidget(hint, 1)
    bar.addWidget(follow)
    bar.addWidget(pause)
    bar.addWidget(clear)
    lay.addLayout(bar)

    ctrl = Controller(widget, view)
    pause.toggled.connect(ctrl.set_paused)
    pause.toggled.connect(lambda on: pause.setText("Resume" if on else "Pause"))
    clear.clicked.connect(view.clear)

    def _follow(_on: bool) -> None:
        if follow.isChecked():
            ctrl._scroll_end()
    follow.toggled.connect(_follow)

    return widget, ctrl


class ConsolePane(QWidget):
    """One tailed log pane for the player tab ("Console" / "Diagnostics")."""

    def __init__(self, path: str | Path, *, counter_filter: bool = False,
                 title_hint: str = "", parent=None, poll_ms: int = 500):
        super().__init__(parent)
        self.filter_counters = counter_filter
        self.tail = TailBuffer(path)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        widget, self.ctrl = make_console_pane(
            self.tail, title_hint=title_hint,
            counter_filter=counter_filter, poll_ms=poll_ms)
        lay.addWidget(widget)

    def start(self) -> "ConsolePane":
        if self.ctrl is not None:
            self.ctrl.start()
        return self

    def stop(self) -> None:
        if self.ctrl is not None:
            self.ctrl.stop()

    def current_text(self, n: int = 200) -> str:
        lines = self.tail.tail(n)
        if self.filter_counters:
            lines = filter_counter_lines(lines)
        return "\n".join(lines)
