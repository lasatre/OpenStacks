"""OpenStacks player window shell.

A QMainWindow with:
  * a closable QTabWidget of PlayerPane (one per instance),
  * a shared vertical sidebar toolbar (BlueStacks-style) on the right,
  * a status bar showing instance / adb / input state of the current pane.

Each PlayerPane holds a nested QTabWidget:
    Screen       - the RFB/VNC display (empty until the GL route lands; see
                   README - the guest boots headless for now)
    Console      - live tail of logs/<name>.serial.log (the QEMU -serial
                   boot console: this is the real boot-progress signal)
    Diagnostics  - live tail of logs/<name>.qemu.log filtered to the
                   [gr_render]/[bstvmsg]/[vmmdev]-style debug counters

The control plane surfaced in the status bar is a per-instance STATE model
(adb.py BootState):
    stopped -> starting -> adb-online -> ready
with offline / unauthorized surfaced verbatim when adbd misbehaves. A
BootStateWatch worker thread polls `adb devices` + `getprop sys.boot_completed`
through a connect-with-retry loop, so the GUI never blocks on adb.

Sidebar actions (shared across tabs, act on the current pane):
    Rotate, Keymap, Screenshot, Install APK, Multi-touch, Input: VNC/ADB.

Run via:
    python -m openstacks.player        (opens the manager + player shell)
    python run_player.py               (same, from the repo root)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (QFileDialog, QMainWindow, QTabWidget,
                               QToolBar, QVBoxLayout, QWidget)

from .adb import Adb, AdbError, BootState
from .config import Instance, REPO_ROOT, Store
from .console import ConsolePane
from .display import (AdbInputBridge, InputBridge, InputWorker, VncDisplay,
                      VncInputBridge)
from .qemu_engine import ENGINE as qemu_engine
from .vnc import VncClientThread


class BootStateWatch(QThread):
    """Background probe thread: per-instance boot state for the status bar.

    Every poll cycle does one `adb devices` (+ one `adb connect` on the way)
    plus a `getprop sys.boot_completed` shell once the device is online - all
    blocking subprocess work happens here, never on the GUI thread.
    """

    stateUpdated = Signal(str, str)     # BootState.value, human detail

    def __init__(self, adb: Adb, is_running=None, poll_s: float = 2.0,
                 parent=None):
        super().__init__(parent)
        self.adb = adb
        self._is_running = is_running or (lambda: False)
        self.poll_s = poll_s
        self._stop = False
        self._last: tuple[str, str] = ("", "")

    def run(self) -> None:
        while not self._stop:
            state, detail = self._probe()
            if (state, detail) != self._last:
                self._last = (state, detail)
                self.stateUpdated.emit(state, detail)
            self._sleep()
        # final flush so a stop shows as STOPPED promptly
        state, detail = self._probe(budget=0)
        if state != self._last[0]:
            self.stateUpdated.emit(state, detail)

    def _probe(self, budget: float | None = None) -> tuple[str, str]:
        """snapshot() never raises; probe hiccups degrade to a status."""
        running = bool(self._is_running())
        try:
            state, detail = self.adb.snapshot(running)
        except Exception as e:                     # paranoia: adb.py is Qt-free
            state, detail = BootState.STARTING.value, f"probe error: {e}"
        return state.value, detail

    def _sleep(self) -> None:
        for _ in range(max(1, int(self.poll_s * 10))):
            if self._stop:
                return
            time.sleep(0.1)

    def stop(self) -> None:
        self._stop = True
        if self.isRunning():
            self.wait(3000)


class PlayerPane(QWidget):
    """One instance's screen + its private bridges/threads."""

    statusChanged = Signal(str)

    def __init__(self, inst: Instance, store: Store, parent=None):
        super().__init__(parent)
        self.inst = inst
        self.store = store
        self.adb = Adb(port=inst.adb_port,
                       adb_path=store.global_settings.resolved_adb_path() or None)

        self.vnc_thread: Optional[VncClientThread] = None
        self.input_worker = InputWorker(lambda: self.input_bridge)

        # bridges: primary = QEMU VNC (same RFB socket); option = adb pip-through
        self.vnc_input = VncInputBridge(lambda: self.vnc_thread)
        self.adb_input = AdbInputBridge(lambda: self.adb,
                                        on_error=self._on_input_error)
        self.input_bridge: InputBridge = self.vnc_input   # current mode

        self.native = getattr(inst, "display", "native") == "native"
        if self.native:
            # Phase-75: the renderer window itself (GPU present, input direct)
            from .native import NativeDisplay
            self.display = NativeDisplay(lambda: qemu_engine.pid(self.inst),
                                         aspect=inst.res_wh(), parent=self)
        else:
            self.display = VncDisplay(
                parent=self,
                input_provider=lambda: self.input_bridge,
                bridge_mode_provider=lambda: "adb" if self.input_bridge is self.adb_input else "vnc",
            )

        # nested pane tabs: Screen / Console / Diagnostics
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self.display, "Screen")
        self.console = ConsolePane(
            qemu_engine.serial_log_path(self.inst),
            title_hint=f"serial console: logs/{self.inst.name}.serial.log "
                       f"(-serial file:; boot progress appears here)")
        self.tabs.addTab(self.console.start(), "Console")
        self.diagnostics = ConsolePane(
            qemu_engine.qemu_log_path(self.inst.name),
            counter_filter=True,
            title_hint=f"QEMU log: logs/{self.inst.name}.qemu.log "
                       "[gr_render]/[bstvmsg]/[vmmdev] counters")
        self.tabs.addTab(self.diagnostics.start(), "Diagnostics")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)

        self._status = ""
        self._adb_state = f"adb {inst.adb_port}: probing"
        self._vnc_state = f"vnc 127.0.0.1:{inst.vnc_port}: idle"
        self._adb_threads: list[_CallThread] = []

        # control-plane watchdog: stopped -> starting -> adb-online -> ready
        self.boot_watch = BootStateWatch(
            self.adb, lambda: qemu_engine.is_running(self.inst), parent=self)
        self.boot_watch.stateUpdated.connect(self._on_boot_state)
        self.boot_watch.start()

    # -- wiring ------------------------------------------------------------------
    def start(self) -> None:
        """(Re)start VNC streaming + adb poll for this instance."""
        if self.native:
            self._vnc_state = "display: native (GPU)"
        elif self.vnc_thread is None:
            self.input_worker.start()
            self.vnc_thread = VncClientThread("127.0.0.1", self.inst.vnc_port,
                                              on_send_error=self._on_send_error)
            self.vnc_thread.stateChanged.connect(self._on_vnc_state)
            self.vnc_thread.frameUpdated.connect(self._on_frame)
            self.vnc_thread.failed.connect(self._on_vnc_failed)
            self.vnc_thread.start()
        self._emit_status()

    def shutdown(self) -> None:
        t, w, b, c, d = (self.vnc_thread, self.input_worker, self.boot_watch,
                         self.console, self.diagnostics)
        self.vnc_thread, self.input_worker, self.boot_watch = None, None, None
        if self.native:
            self.display.release()   # hand the renderer window back first
        if b:
            b.stop()
        if t:
            t.stop()
        if w:
            w.stop()
        for p in (c, d):
            try:
                p.stop()
            except Exception:
                pass

    # -- boot-state ----------------------------------------------------------------
    def _on_boot_state(self, state: str, detail: str) -> None:
        port = self.inst.adb_port
        icon = {"stopped": "○", "starting": "◍", "offline": "◌",
                "unauthorized": "⊙", "adb-online": "◐", "ready": "●"
                }.get(state, "○")
        self._adb_state = f"adb {port}: {state}" + (f" ({detail})" if detail else "")
        self._emit_status()
        if state == BootState.READY.value:
            self.flash(f"{self.inst.name}: Android boot complete - adb/{port}"
                       " ready (input + install now live)")
        elif state == BootState.UNAUTHORIZED.value:
            self.flash(f"adb {port}: unauthorized - accept the RSA prompt")
        elif state == BootState.OFFLINE.value:
            self.flash(f"adb {port}: adbd offline - guest init still coming up")

    def _on_input_error(self, msg: str) -> None:
        """Called from InputWorker threads; hop to the GUI via the signal."""
        try:
            self.statusChanged.emit(f"input: {msg}")
        except RuntimeError:
            pass

    # -- VNC thread callbacks ------------------------------------------------------
    def _on_frame(self, w: int, h: int, data: object) -> None:
        self.display.set_frame(w, h, data)

    def _on_vnc_state(self, state: str) -> None:
        port = self.inst.vnc_port
        self._vnc_state = f"vnc 127.0.0.1:{port}: {state}"
        self._emit_status()

    def _on_vnc_failed(self, text: str) -> None:
        self._vnc_state = f"vnc 127.0.0.1:{self.inst.vnc_port}: failed ({text})"
        self._emit_status()

    def _on_send_error(self, text: str) -> None:
        self._vnc_state = f"vnc input error: {text}"
        self._emit_status()

    def set_input_mode(self, mode: str) -> None:
        self.input_bridge = self.adb_input if mode == "adb" else self.vnc_input
        self._emit_status()

    # -- status --------------------------------------------------------------------
    def _emit_status(self) -> None:
        mode = "adb" if self.input_bridge is self.adb_input else "vnc"
        text = (f"{self.inst.name}  |  {self._vnc_state}  |  {self._adb_state}"
                f"  |  input:{mode}  |  {self.inst.resolution}@{self.inst.dpi}dpi")
        self._status = text
        self.statusChanged.emit(text)

    def refresh_adb(self) -> None:
        def job():
            state, detail = self.adb.snapshot(
                bool(qemu_engine.is_running(self.inst)))
            return f"adb {self.inst.adb_port}: {state.value}" \
                   + (f" ({detail})" if detail else "")
        thread = _CallThread(job)
        thread.done.connect(self._set_adb_state)
        thread.failed.connect(self._set_adb_state)
        thread.start()
        self._adb_threads = [t for t in self._adb_threads if t.isRunning()]
        self._adb_threads.append(thread)

    def _set_adb_state(self, text: str) -> None:
        self._adb_state = text
        self._emit_status()

    # -- sidebar operations --------------------------------------------------------
    def rotate(self) -> None:
        def job():
            # best effort via user_rotation system setting
            self.adb.ensure_connected()
            self.adb.shell("settings", "put", "system", "user_rotation", "1")
            self.adb.shell("settings", "put", "system", "accelerometer_rotation", "0")
            return "rotate: user_rotation=1 sent"
        th = _CallThread(job)
        th.done.connect(lambda s: self.flash(s))
        th.failed.connect(lambda e: self.flash(f"rotate failed: {e}"))
        th.start()
        self._adb_threads.append(th)

    def keymap(self) -> None:
        self.flash("keymap editor: not implemented yet (WIP)")

    def multitouch(self) -> None:
        self.flash("multi-touch gestures: not implemented yet (WIP)")

    def screenshot(self) -> None:
        """Save what we render (VNC) preferring adb screencap for exactness."""
        shots = Path(self.store.global_settings.resolved_screenshots_dir())
        shots.mkdir(parents=True, exist_ok=True)
        png = shots / f"{self.inst.name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        # 1) fast path: our own VNC frame
        if not self.native and self.display._img is not None:
            if self.display._img.save(str(png), "PNG"):
                self.flash(f"screenshot: {png.name} (VNC frame)")
                return
        # 2) fallback: adb screencap == exact guest surface
        try:
            self.adb.screencap(str(png))
            self.flash(f"screenshot: {png.name} (adb screencap)")
        except (AdbError, OSError) as e:
            self.flash(f"screenshot failed: {e}")

    def install_apk(self, path="") -> None:
        """Install one APK, or several selected together as base + splits."""
        if not path:
            path, _ = QFileDialog.getOpenFileNames(
                self, "Install APK (select base + splits for split APKs)", "",
                "APK files (*.apk);;All files (*)")
        if not path:
            return
        paths = [path] if isinstance(path, str) else list(path)
        self.flash(f"installing {Path(paths[0]).name}"
                   + (f" + {len(paths) - 1} splits" if len(paths) > 1 else "") + "...")
        def job():
            out = self.adb.install(paths, timeout=600)
            return (f"install: {Path(paths[0]).name}: "
                    f"{out.splitlines()[-1] if out else 'ok'}")
        th = _CallThread(job, self)
        th.done.connect(lambda s: self.flash(s))
        th.failed.connect(lambda e: self.flash(f"install failed: {e}"))
        th.start()
        self._adb_threads.append(th)

    def flash(self, text: str) -> None:
        self.statusChanged.emit(text)
        QTimer.singleShot(4000, self._emit_status)   # fade back to standard text


class _CallThread(QThread):
    """One-off async helper running blocking adb work off the GUI thread."""
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as e:                 # adb errors etc.
            self.failed.emit(str(e))


class PlayerWindow(QMainWindow):
    """Window shell: closable instance tabs + shared sidebar + status bar."""

    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("OpenStacks Player")
        self.resize(1280, 760)

        self.tabs = QTabWidget(self)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_current_tab)
        self.setCentralWidget(self.tabs)

        self._build_sidebar()
        self.statusBar().showMessage("OpenStacks player ready - launch an instance from the manager.")

    # -- sidebar ---------------------------------------------------------------------
    _PANE_ACTIONS = ("rotate", "keymap", "screenshot", "install_apk", "multitouch")

    def _build_sidebar(self) -> None:
        bar = QToolBar("Actions", self)
        bar.setOrientation(Qt.Vertical)
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.addToolBar(Qt.RightToolBarArea, bar)
        self.sidebar = bar

        def add(text: str, tip: str = "", key: str = "") -> QAction:
            act = QAction(text, self)
            if key:
                act.setShortcut(QKeySequence(key))
            act.setToolTip(tip or text)
            self.addAction(act)                 # shortcuts work even without focus
            bar.addAction(act)
            return act

        for i, method in enumerate(self._PANE_ACTIONS):
            tips = {
                "rotate": "Rotate screen (Ctrl+Shift+R)",
                "keymap": "Keyboard mapping editor (WIP)",
                "screenshot": "Save a screenshot (Ctrl+S)",
                "install_apk": "Install an APK (drag&drop also works)",
                "multitouch": "Multi-touch gestures (WIP)",
            }
            keys = {"rotate": "Ctrl+Shift+R", "screenshot": "Ctrl+S",
                    "install_apk": "Ctrl+I"}
            add(method.replace("_", " ").title(), tips[method], keys.get(method, ""))

        self.act_input_mode = add("Input: VNC",
                                  "Toggle input pipeline (QEMU VNC <-> ADB)")
        self.act_input_mode.triggered.connect(self._toggle_input_mode)
        for i, method in enumerate(self._PANE_ACTIONS):
            self.sidebar.actions()[i].triggered.connect(
                lambda _=False, m=method: self._with_pane(m))

    # -- pane helpers -----------------------------------------------------------------
    def current_pane(self) -> Optional[PlayerPane]:
        w = self.tabs.currentWidget()
        return w if isinstance(w, PlayerPane) else None

    def _with_pane(self, method: str) -> None:
        pane = self.current_pane()
        if pane is None:
            self.statusBar().showMessage("No instance tab focused.", 3000)
            return
        getattr(pane, method)()

    def _toggle_input_mode(self) -> None:
        pane = self.current_pane()
        if pane is None:
            return
        new = "vnc" if pane.input_bridge is pane.adb_input else "adb"
        pane.set_input_mode(new)
        self.act_input_mode.setText(f"Input: {new.upper()}")
        pane.flash(f"input pipeline switched to {new}")

    def _on_current_tab(self, idx: int) -> None:
        self._with_pane("refresh_adb")

    def _close_tab(self, idx: int) -> None:
        w = self.tabs.widget(idx)
        if isinstance(w, PlayerPane):
            w.shutdown()
        self.tabs.removeTab(idx)

    # -- manager-facing API -------------------------------------------------------------
    def open_instance(self, inst: Instance) -> PlayerPane:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PlayerPane) and w.inst.name == inst.name:
                self.tabs.setCurrentIndex(i)
                return w
        pane = PlayerPane(inst, self.store, self)
        pane.statusChanged.connect(
            lambda text: self.statusBar().showMessage(text))
        i = self.tabs.addTab(pane, QIcon(), inst.name)
        self.tabs.setTabToolTip(i, f"{inst.name}: adb {inst.adb_port}, "
                                   f"vnc {inst.vnc_port}, {inst.resolution}@{inst.dpi}dpi")
        self.tabs.setCurrentIndex(i)
        pane.start()
        pane.refresh_adb()
        return pane

    def closeEvent(self, ev) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PlayerPane):
                w.shutdown()
        super().closeEvent(ev)


def main() -> None:
    """python -m openstacks.player - opens the manager which can spawn players."""
    from .manager import ManagerWindow, run_app     # late import: avoid cycle

    app = run_app()
    win = ManagerWindow()
    win.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
