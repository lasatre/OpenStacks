"""Game controls for one player pane: the engine, the foreground app, overlay, menu.

The game in front decides which controls file applies, like BlueStacks: the session polls
the guest's resumed activity over adb and loads that package's `.cfg` (store.py) when it
changes.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from PySide6.QtCore import QObject, QRect, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QMenu

from .. import paths
from ..qemu_engine import ENGINE, controls_port
from . import store
from .bscfg import ControlsFile
from .engine import Engine
from .runtime import Pan
from .overlay import Overlay

_RESUMED = re.compile(r"(?:mResumedActivity|topResumedActivity)[:=].*? u\d+ ([\w.]+)/")


class PackageWatch(QThread):
    """Polls the guest's foreground package (adb dumpsys) every 1.5 s."""
    package = Signal(str)

    def __init__(self, adb, is_running, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.is_running = is_running
        self._stop = False
        self._last = None

    def run(self) -> None:
        while not self._stop:
            pkg = None
            if self.is_running():
                try:
                    out = self.adb.shell("sh", "-c", "dumpsys activity activities | grep -E "
                                         "'mResumedActivity|topResumedActivity'",
                                         timeout=5)
                    m = _RESUMED.search(out or "")
                    pkg = m.group(1) if m else None
                except Exception:
                    pkg = None
            if pkg and pkg != self._last:
                self._last = pkg
                self.package.emit(pkg)
            for _ in range(15):
                if self._stop:
                    return
                time.sleep(0.1)

    def stop(self) -> None:
        self._stop = True
        self.wait(3000)


class ControlsSession(QObject):
    status = Signal(str)
    editor_requested = Signal()

    def __init__(self, inst, adb, display, parent=None):
        super().__init__(parent)
        self.inst = inst
        self.adb = adb
        self.display = display
        self.package = ""
        self.cfg_path = None
        self.engine = Engine(controls_port(inst), self)
        self.engine.status.connect(self.status)
        self.engine.text.connect(self._type_text)
        self.engine.changed.connect(lambda: self.overlay.update())
        self.watch = PackageWatch(adb, lambda: ENGINE.is_running(inst), self)
        self.watch.package.connect(self._on_package)
        self.overlay = Overlay(self._screen_rect, self._overlay_controls, self._aiming,
                               lambda: self.display.window())
        self.editor = None
        self._view = QTimer(self, interval=250)
        self._view.timeout.connect(self._sync_view)

    # -- lifecycle
    def start(self) -> None:
        self.engine.start()
        self.watch.start()
        self.overlay.start()
        self._view.start()

    def stop(self) -> None:
        if self.editor is not None:
            self.editor.dirty = False
            self.editor.close_editor()
        self._view.stop()
        self.watch.stop()
        self.overlay.stop()
        self.overlay.deleteLater()
        self.engine.stop()

    # -- geometry
    def _screen_rect(self) -> Optional[QRect]:
        d = self.display
        if not d.isVisible() or not d.window().isVisible() or d.window().isMinimized():
            return None
        if getattr(d, "_container", True) is None:      # nothing embedded yet
            return None
        r = d._target_rect() if hasattr(d, "_target_rect") else d.rect()
        return QRect(d.mapToGlobal(r.topLeft()), r.size())

    def _sync_view(self) -> None:
        r = self._screen_rect()
        if r is None:
            return
        w, h = self.inst.res_wh()
        if getattr(self.display, "rotation", lambda: 0)() & 1:
            w, h = h, w
        self.engine.set_geometry(r.width(), r.height(), w, h)

    def _overlay_controls(self) -> list[dict]:
        if not self.engine.enabled or not self.engine.scheme:
            return []
        return [c.raw for c in self.engine.controls]

    def _aiming(self) -> bool:
        return any(isinstance(c, Pan) and c.captures_mouse() for c in self.engine.controls)

    # -- the game in front
    def _on_package(self, pkg: str) -> None:
        self.package = pkg
        self.reload()

    def reload(self, keep_disabled: bool = False) -> None:
        if self.editor is not None and not keep_disabled:
            return                       # the editor owns the controls until it closes
        path = store.find(self.package)
        cfg = None
        if path:
            try:
                cfg = ControlsFile.load(path)
            except (OSError, ValueError) as e:
                self.status.emit(f"game controls: {path.name}: {e}")
        self.cfg_path = path
        self.engine.load(cfg)
        if self.editor is not None:
            self.engine.set_enabled(False)
        self.overlay.sync()
        self.status.emit(self.describe())

    def describe(self) -> str:
        e = self.engine
        if not e.scheme:
            return f"game controls: none for {self.package or 'this app'}"
        s = (f"game controls: {self.package} - scheme '{e.scheme.name}', "
             f"{len(e.controls)} controls")
        if e.unsupported:
            s += " (not yet supported: " + ", ".join(
                f"{n} {t}" for t, n in sorted(e.unsupported.items())) + ")"
        if e.scheme.smart:
            s += "; smart conditions ignored (on-screen detection not implemented)"
        return s

    # -- the controls editor
    def open_editor(self):
        """The editor side panel for the game in front (None if no game is known)."""
        if self.editor is not None:
            return self.editor
        if not self.package:
            self.status.emit("game controls: open a game first")
            return None
        from .editor import ControlsEditor
        self._was_enabled = self.engine.enabled
        self.engine.set_enabled(False)
        self.overlay.wanted, self._overlay_was = False, self.overlay.wanted
        self.overlay.sync()
        self.editor = ControlsEditor(self)
        self.editor.closed.connect(self._editor_closed)
        return self.editor

    def _editor_closed(self) -> None:
        ed, self.editor = self.editor, None
        if ed is not None:
            ed.setParent(None)
            ed.deleteLater()
        self.engine.set_enabled(getattr(self, "_was_enabled", True))
        self.overlay.wanted = getattr(self, "_overlay_was", True)
        self.reload()

    def _type_text(self, text: str) -> None:
        try:
            self.adb.shell("input", "text", text.replace(" ", "%s"), timeout=10)
        except Exception:
            pass

    # -- the sidebar button's menu
    def menu(self, parent) -> QMenu:
        m = QMenu(parent)
        e = self.engine
        m.addAction("Controls editor...", lambda: self.editor_requested.emit())
        m.addSeparator()
        a = m.addAction("Game controls on")
        a.setCheckable(True)
        a.setChecked(e.enabled)
        a.toggled.connect(e.set_enabled)
        a = m.addAction("Show key overlay")
        a.setCheckable(True)
        a.setChecked(self.overlay.wanted)
        a.toggled.connect(self._set_overlay)
        if e.cfg and len(e.cfg.schemes) > 1:
            sm = m.addMenu("Control scheme")
            g = QActionGroup(sm)
            for s in e.cfg.schemes:
                act = sm.addAction(s.name + ("  (smart)" if s.smart else ""))
                act.setCheckable(True)
                act.setChecked(e.scheme is s)
                g.addAction(act)
                act.triggered.connect(lambda _=False, n=s.name: self._pick_scheme(n))
        m.addSeparator()
        info = m.addAction(self.describe())
        info.setEnabled(False)
        m.addSeparator()
        if store.bluestacks_dirs():
            m.addAction("Import controls from BlueStacks", self._import_bluestacks)
        m.addAction("Open controls folder", self._open_folder)
        m.addAction("Reload controls", self.reload)
        return m

    def _set_overlay(self, on: bool) -> None:
        self.overlay.wanted = on
        self.overlay.sync()

    def _pick_scheme(self, name: str) -> None:
        cfg = self.engine.cfg
        if cfg is None:
            return
        cfg.select(name)
        self.engine.load(cfg, name)
        if self.cfg_path and self.cfg_path.parent == paths.CONTROLS_DIR:
            try:
                cfg.save()                        # remember the choice (our copy only)
            except OSError:
                pass
        self.status.emit(self.describe())

    def _import_bluestacks(self) -> None:
        copied, skipped = store.import_bluestacks()
        self.status.emit(f"game controls: imported {copied} file(s) from BlueStacks"
                         + (f", kept {skipped} existing" if skipped else ""))
        self.reload()

    def _open_folder(self) -> None:
        paths.CONTROLS_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.CONTROLS_DIR)))
