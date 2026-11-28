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

import ctypes
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QCursor, QIcon, QKeySequence
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QSlider,
                               QPushButton, QTabWidget, QToolButton,
                               QToolBar, QVBoxLayout, QWidget)

from .adb import Adb, AdbError, BootState
from .config import Instance, REPO_ROOT, Store
from .console import ConsolePane
from .notify import NotificationWatch, describe, notifier
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


# -- Windows messages from/for the native screen ------------------------------------------
# Files dropped on the native screen arrive from QEMU (gr_render.c) as WM_COPYDATA
# (dwData 'OSTD', UTF-16 paths joined by '\n'); WM_SIZING keeps the pop-out proportional.
_WM_COPYDATA, _WM_SIZING = 0x004A, 0x0214
_DROP_MAGIC = 0x4F535444


def _win_msg(message):
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    return wintypes.MSG.from_address(int(message))


def _dropped_paths(msg) -> Optional[list[str]]:
    """Paths from a QEMU drop message, else None."""
    if msg is None or msg.message != _WM_COPYDATA or not msg.lParam:
        return None

    class COPYDATA(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", ctypes.c_uint32),
                    ("lpData", ctypes.c_void_p)]
    cds = COPYDATA.from_address(msg.lParam)
    if cds.dwData != _DROP_MAGIC or not cds.lpData:
        return None
    return [p for p in ctypes.wstring_at(cds.lpData, cds.cbData // 2).split("\n") if p]


def _keep_aspect(msg, hwnd: int, aw: int, ah: int) -> bool:
    """WM_SIZING: adjust the dragged window rect so its client area stays aw:ah."""
    if msg is None or msg.message != _WM_SIZING or aw <= 0 or ah <= 0:
        return False
    from ctypes import wintypes
    u32 = ctypes.windll.user32
    wr, cr = wintypes.RECT(), wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(wr))
    u32.GetClientRect(hwnd, ctypes.byref(cr))
    fw = (wr.right - wr.left) - cr.right               # frame + caption, physical px
    fh = (wr.bottom - wr.top) - cr.bottom
    r = wintypes.RECT.from_address(msg.lParam)
    cw, ch = (r.right - r.left) - fw, (r.bottom - r.top) - fh
    edge = msg.wParam                                  # WMSZ_LEFT=1 .. BOTTOMRIGHT=8

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    mi = MONITORINFO(ctypes.sizeof(MONITORINFO))
    u32.MonitorFromWindow.restype = ctypes.c_void_p
    u32.GetMonitorInfoW(ctypes.c_void_p(u32.MonitorFromWindow(hwnd, 2)), ctypes.byref(mi))
    max_ch = (mi.rcWork.bottom - mi.rcWork.top) - fh   # client may not outgrow the desktop
    capped = edge not in (3, 6) and max_ch > 0 and round(cw * ah / aw) > max_ch
    if edge in (3, 6) or capped:
        # height-driven: top/bottom edges, or a width that would outgrow the desktop
        ch = max_ch if capped else (min(ch, max_ch) if max_ch > 0 else ch)
        nw = round(ch * aw / ah) + fw
        if edge in (1, 4, 7):                          # dragging a left edge: keep the right
            r.left = r.right - nw
        else:
            r.right = r.left + nw
        if edge not in (3, 6):
            if edge in (4, 5):
                r.top = r.bottom - (ch + fh)
            else:
                r.bottom = r.top + ch + fh
    else:                                              # sides and corners: height follows
        nh = round(cw * ah / aw) + fh
        if edge in (4, 5):                             # top corners move the top edge
            r.top = r.bottom - nh
        else:
            r.bottom = r.top + nh
    return True


class ScreenWindow(QWidget):
    """The Android screen alone in its own window (Pop Out). Closing it puts
    the screen back into the player's Screen tab; the instance keeps running."""

    def __init__(self, pane: "PlayerPane"):
        super().__init__(None, Qt.Window)
        self.pane = pane
        self.setWindowTitle(f"{pane.inst.name} - OpenStacks")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._returning = False
        # the player's pop-out shortcut also brings the screen back from here
        dock = QAction(self)
        dock.setShortcut(QKeySequence("Ctrl+Shift+P"))
        dock.setShortcutContext(Qt.WindowShortcut)
        dock.triggered.connect(lambda: self.pane.dock_screen())
        self.addAction(dock)

    def fit(self, w: int, h: int) -> None:
        """Size the window to the screen's aspect (max ~85% of the desktop)."""
        avail = self.screen().availableGeometry() if self.screen() else None
        mw = int(avail.width() * 0.85) if avail else 1500
        mh = int(avail.height() * 0.85) if avail else 860
        scale = min(1.0, mw / max(w, 1), mh / max(h, 1))
        self.resize(int(w * scale), int(h * scale))

    def nativeEvent(self, event_type, message):
        # Only look at our two messages, and never touch winId() here: this also runs
        # while the native window is being created, and winId() would re-enter creation.
        msg = _win_msg(message)
        if msg is not None and msg.message == _WM_COPYDATA:
            paths = _dropped_paths(msg)
            if paths:
                self.pane.drop_files(paths)
                return True, 1
        elif msg is not None and msg.message == _WM_SIZING:
            # always proportional, like BlueStacks (maximized: letterboxed in the window colour)
            if _keep_aspect(msg, int(msg.hWnd), *self.pane._screen_size()):
                return True, 1
        return super().nativeEvent(event_type, message)

    def closeEvent(self, ev) -> None:
        if not self._returning:
            self.pane.dock_screen()
        super().closeEvent(ev)


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
        self.screen_window: Optional[ScreenWindow] = None
        self._screen_placeholder: Optional[QWidget] = None
        if self.native:
            self.display.rotationChanged = self._on_rotation
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
        # Activity: what the player did for this instance (installs, root, file copies,
        # screenshots, boot state), kept in logs/<name>.activity.log; flash() writes it
        from . import activity
        self._activity_path = activity.path(self.inst.name)
        self.activity = ConsolePane(
            self._activity_path,
            title_hint=f"Activity: logs/{self.inst.name}.activity.log "
                       "(installs, root, file transfer, screenshots)")
        self.tabs.addTab(self.activity.start(), "Activity")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)

        self._status = ""
        self._adb_state = f"adb {inst.adb_port}: probing"
        # game controls (key mapping + gamepad; controls/): native display only, the
        # engine talks to QEMU's window input directly
        self.controls = None
        self._controls_started = False
        if self.native:
            from .controls.session import ControlsSession
            self.controls = ControlsSession(
                inst, self.adb, self.display, parent=self,
                download=lambda: self.store.global_settings.controls_download)
            self.controls.status.connect(self.flash)
            self.controls.editor_requested.connect(self.open_controls_editor)

        self._vnc_state = f"vnc 127.0.0.1:{inst.vnc_port}: idle"
        self._adb_threads: list[_CallThread] = []
        self._vol_pid = 0                 # QEMU process the saved volume was applied to

        # control-plane watchdog: stopped -> starting -> adb-online -> ready
        self.boot_watch = BootStateWatch(
            self.adb, lambda: qemu_engine.is_running(self.inst), parent=self)
        self.boot_watch.stateUpdated.connect(self._on_boot_state)
        self.boot_watch.start()

        # Android notifications -> the desktop (QEMU logs them, see notify.py)
        self.notes: Optional[NotificationWatch] = None
        if inst.notifications:
            self.notes = NotificationWatch(qemu_engine.qemu_log_path(inst.name), parent=self)
            self.notes.received.connect(self._on_notification)

    # -- wiring ------------------------------------------------------------------
    def start(self) -> None:
        """(Re)start VNC streaming + adb poll for this instance."""
        if self.native:
            self._vnc_state = "display: native (GPU)"
            if self.controls is not None and not self._controls_started:
                self._controls_started = True
                self.controls.start()
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
        if self.notes is not None:
            self.notes.stop()
            self.notes = None
        self.vnc_thread, self.input_worker, self.boot_watch = None, None, None
        if self.controls is not None:
            self.controls.stop()
            self.controls = None
        if self.native:
            self.display.stop()      # stop polling + hand the renderer window back first
        if self.screen_window is not None:
            self.screen_window._returning = True
            self.screen_window.close()
            self.screen_window = None
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
        if state != "stopped":
            pid = qemu_engine.pid(self.inst) or 0
            if pid and pid != self._vol_pid:
                self._vol_pid = pid
                self.apply_volume()
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
        mode = ("direct" if self.native else
                "adb" if self.input_bridge is self.adb_input else "vnc")
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

    def apply_volume(self) -> None:
        """Host-side volume/mute of this instance's audio (bstaudio's QOM properties,
        over the HMP monitor; a background thread so the UI never waits on QEMU)."""
        inst, vol, muted = self.inst, max(0, min(100, int(self.inst.volume))), bool(self.inst.muted)

        def job():
            dev = "/machine/peripheral/bstaudio0"
            try:
                qemu_engine.monitor(inst, f"qom-set {dev} volume {vol}")
                qemu_engine.monitor(inst, f"qom-set {dev} muted {'on' if muted else 'off'}")
            except OSError:
                pass                         # not up yet: applied again on boot
        threading.Thread(target=job, daemon=True).start()

    def _set_adb_state(self, text: str) -> None:
        self._adb_state = text
        self._emit_status()

    # -- sidebar operations --------------------------------------------------------
    # -- Android navigation (BlueStacks side bar) ------------------------------
    def _nav(self, qcode: str, label: str) -> None:
        def job():
            qemu_engine.send_key(self.inst, qcode)
            return label
        th = _CallThread(job)
        th.failed.connect(lambda e: self.flash(f"{label} failed: {e}"))
        th.start()
        self._adb_threads.append(th)

    def nav_back(self) -> None:
        self._nav("ac_back", "back")

    def nav_home(self) -> None:
        self._nav("ac_home", "home")

    def nav_recents(self) -> None:
        self._nav("audionext", "recent apps")

    # -- screen pop-out ----------------------------------------------------------
    def _screen_size(self) -> tuple[int, int]:
        w, h = self.inst.res_wh()
        if self.native and self.display.rotation() & 1:
            w, h = h, w
        return w, h

    def pop_screen(self) -> None:
        """Toggle: the screen alone in its own window <-> the Screen tab."""
        if self.screen_window is not None:
            self.dock_screen()
            return
        if self.native:
            self.display.release()        # re-embedded by its poll in the new parent
        idx = self.tabs.indexOf(self.display)
        self.tabs.removeTab(idx)
        ph = QWidget()
        lay = QVBoxLayout(ph)
        msg = QLabel("The screen is in its own window.")
        msg.setAlignment(Qt.AlignCenter)
        back = QPushButton("Bring the screen back")
        back.clicked.connect(self.dock_screen)
        lay.addStretch(1)
        lay.addWidget(msg)
        lay.addWidget(back, 0, Qt.AlignCenter)
        lay.addStretch(1)
        self.tabs.insertTab(0, ph, "Screen")
        self.tabs.setCurrentIndex(0)
        self._screen_placeholder = ph
        win = ScreenWindow(self)
        win.layout().addWidget(self.display)
        win.fit(*self._screen_size())
        win.show()
        self.display.show()
        self.screen_window = win
        self.flash("screen popped out (close its window to bring it back)")

    def dock_screen(self) -> None:
        win, self.screen_window = self.screen_window, None
        if win is None:
            return
        if self.native:
            self.display.release()
        self.tabs.removeTab(self.tabs.indexOf(self._screen_placeholder))
        self._screen_placeholder.deleteLater()
        self._screen_placeholder = None
        self.tabs.insertTab(0, self.display, "Screen")
        self.tabs.setCurrentIndex(0)
        self.display.show()
        win._returning = True
        win.close()
        win.deleteLater()
        w = self.window()
        if hasattr(w, "fit_to_screen"):
            QTimer.singleShot(0, lambda: w.fit_to_screen(self))

    def _on_rotation(self, rot: int) -> None:
        # the screen follows portrait <-> landscape like BlueStacks (popped out or docked)
        if self.screen_window is not None:
            self.screen_window.fit(*self._screen_size())
        else:
            win = self.window()
            if hasattr(win, "fit_to_screen"):
                win.fit_to_screen(self)

    def rotate(self) -> None:
        """Toggle Android between its natural orientation and 90 degrees.
        Android then reports the rotation (hcal 7) and the display follows;
        apps that lock their orientation keep it, as on a phone."""
        cur = self.display.rotation() if self.native else 0
        target = 0 if cur & 1 else 1
        def job():
            self.adb.ensure_connected()
            self.adb.shell("settings", "put", "system", "accelerometer_rotation", "0")
            self.adb.shell("settings", "put", "system", "user_rotation", str(target))
            return f"rotate: user_rotation={target}"
        th = _CallThread(job)
        th.done.connect(lambda s: self.flash(s))
        th.failed.connect(lambda e: self.flash(f"rotate failed: {e}"))
        th.start()
        self._adb_threads.append(th)

    def keymap(self) -> None:
        if self.controls is None:
            self.flash("game controls need the native display (display=native)")
            return
        self.controls.menu(self).exec(QCursor.pos())

    def open_controls_editor(self) -> None:
        if self.controls is None:
            return
        panel = self.controls.open_editor()
        if panel is None:
            return
        win = self.window()
        if hasattr(win, "attach_side_panel"):
            win.attach_side_panel(panel)
        else:                                # screen popped out: a floating panel
            panel.setParent(None)
            panel.setWindowFlag(Qt.Tool, True)
            panel.show()

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

    def files(self) -> None:
        """File transfer window for this instance (one per pane, re-raised if open)."""
        from .files import FileTransferDialog
        dlg = getattr(self, "_files_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg.raise_()
            dlg.activateWindow()
            return
        self._files_dlg = FileTransferDialog(self.adb, self.inst.name, self.window())
        self._files_dlg.setAttribute(Qt.WA_DeleteOnClose)
        self._files_dlg.destroyed.connect(lambda *_: setattr(self, "_files_dlg", None))
        self._files_dlg.show()

    def drop_files(self, paths: list[str]) -> None:
        """Files dropped on the player: APKs are installed (several together = base +
        splits), everything else goes to the guest's Download folder."""
        apks = [p for p in paths if p.lower().endswith(".apk") and Path(p).is_file()]
        rest = [p for p in paths if p not in apks and Path(p).exists()]
        if apks:
            self.install_apk(apks)
        if not rest:
            return
        dest = "/sdcard/Download"
        self.flash(f"copying {len(rest)} item(s) to {dest}...")

        def job():
            for p in rest:
                self.adb.push(p, dest + "/")
                if Path(p).is_file():
                    self.adb.media_scan(f"{dest}/{Path(p).name}")
            return f"copied {len(rest)} item(s) to {dest}"
        th = _CallThread(job, self)
        th.done.connect(lambda s: self.flash(s))
        th.failed.connect(lambda e: self.flash(f"copy failed: {e}"))
        th.start()
        self._adb_threads.append(th)

    # -- root (Magisk) ------------------------------------------------------------------
    def root(self) -> None:
        """Install Magisk (Kitsune) on this instance, or remove it. Restarts the instance."""
        from . import magisk
        if getattr(self, "_root_busy", False):
            self.flash("root: already working on it...")
            return
        if not qemu_engine.is_running(self.inst):
            self.flash("root: start the instance first")
            return
        name = self.inst.name
        if self.inst.magisk:
            if QMessageBox.question(
                    self, "Remove Magisk",
                    f"Remove Magisk from {name}?\n\nThe Magisk app, modules and root grants "
                    "are deleted and the instance restarts.") != QMessageBox.Yes:
                return
            self._run_root(lambda log: magisk.uninstall(self.adb, log), rooted=False)
            return

        have = magisk.cached_apk() is not None
        if QMessageBox.question(
                self, "Root with Magisk",
                f"Root {name} with Kitsune Magisk {magisk.APK_VERSION}?\n\n"
                + ("" if have else "The APK is downloaded once from github.com/AndnixSH/"
                   "KitsuneMagisk (checked against a pinned SHA-256).\n\n") +
                "Magisk is started from OpenStacks' boot ramdisk (the shared system image "
                "is not modified) and the Magisk app is installed; apps then ask for root "
                "through Magisk. adb shell keeps root. The instance restarts once.\n\n"
                "Remove it again with the same button.") != QMessageBox.Yes:
            return
        self._run_root(lambda log: magisk.install(self.adb, magisk.ensure_apk(log), log),
                       rooted=True)

    def _run_root(self, job, rooted: bool) -> None:
        self._root_busy = True
        th = _CallThread(None, self)

        def work():
            job(th.progress.emit)
            th.progress.emit("syncing and restarting...")
            try:
                self.adb.shell("sync", timeout=30)
            except AdbError:
                pass
            return "ok"
        th._fn = work
        th.progress.connect(lambda s: self.flash(f"root: {s}"))
        th.done.connect(lambda _: self._root_restart(rooted))
        th.failed.connect(self._root_failed)
        th.start()
        self._adb_threads.append(th)

    def _root_failed(self, err: str) -> None:
        self._root_busy = False
        QMessageBox.warning(self, "Root", f"{self.inst.name}: {err}")
        self.flash(f"root failed: {err}")

    def _root_restart(self, rooted: bool) -> None:
        self.inst.magisk = rooted               # props: BlueStacks su off when rooted
        self.store.update(self.inst)
        self._root_busy = False
        win = self.window()
        if hasattr(win, "restart_instance"):
            win.restart_instance(self.inst, after_boot=self._root_after_boot if rooted
                                 else lambda pane: pane.flash("Magisk removed"))

    @staticmethod
    def _root_after_boot(pane: "PlayerPane") -> None:
        from . import magisk
        th = _CallThread(None, pane)

        def work():
            pane.adb._connected = False
            if not pane.adb.wait_boot(timeout=600):
                raise AdbError("the instance did not finish booting")
            time.sleep(3)                    # magiskd boot-complete hook
            return magisk.finish(pane.adb, th.progress.emit)
        th._fn = work
        th.progress.connect(lambda s: pane.flash(f"root: {s}"))
        th.done.connect(lambda v: (pane.flash(f"rooted: Magisk {v}"),
                                   QMessageBox.information(
                                       pane, "Rooted", f"{pane.inst.name} is rooted "
                                       f"(Magisk {v}). Open the Magisk app to manage root "
                                       "access and modules.")))
        th.failed.connect(pane._root_failed)
        th.start()
        pane._adb_threads.append(th)

    def _on_notification(self, n: dict) -> None:
        title, body = describe(n, self.inst.name)
        shown = notifier().show(title, body)
        from . import activity
        what = " - ".join(p for p in (str(n.get("appName") or n.get("packageName") or "?"),
                                      body.replace("\n", ": ")) if p)
        activity.append(self.inst.name, f"notification: {what}"
                        + ("" if shown else " (repeat, not shown again)"))

    def flash(self, text: str) -> None:
        self.statusChanged.emit(text)
        from . import activity
        activity.append(self.inst.name, text)
        QTimer.singleShot(4000, self._emit_status)   # fade back to standard text


class _CallThread(QThread):
    """One-off async helper running blocking adb work off the GUI thread."""
    done = Signal(str)
    failed = Signal(str)
    progress = Signal(str)

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
        self._bg_threads: list[_CallThread] = []
        self._build_sidebar()
        self.setAcceptDrops(True)       # files dropped anywhere on the player -> the VM
        notifier().clicked.connect(self._bring_to_front)     # an Android notification
        self.statusBar().showMessage("OpenStacks player ready - launch an instance from the manager.")

    def _bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    # -- drag & drop: Explorer -> current instance -------------------------------------
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls() and self.current_pane() is not None:
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:
        self.dragEnterEvent(ev)

    def dropEvent(self, ev) -> None:
        pane = self.current_pane()
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if pane is not None and paths:
            ev.acceptProposedAction()
            pane.drop_files(paths)

    # -- sidebar ---------------------------------------------------------------------
    # tools (top of the rail) and Android navigation (anchored to the bottom),
    # laid out like BlueStacks' right-hand bar: icon only, tooltip names them
    _TOOLS = (
        # (method, icon, tooltip, shortcut)
        ("rotate", "rotate", "Rotate screen", "Ctrl+Shift+R"),
        ("screenshot", "screenshot", "Take a screenshot", "Ctrl+S"),
        ("install_apk", "install_apk", "Install APK", "Ctrl+I"),
        ("files", "files", "Files: move files in and out of the VM "
         "(or drop files on the window)", "Ctrl+Shift+F"),
        ("root", "root", "Root this instance with Magisk / remove Magisk", ""),
        ("pop_screen", "pop_screen", "Pop the screen out into its own window / bring it back",
         "Ctrl+Shift+P"),
        ("keymap", "keymap", "Game controls: key mapping and gamepad", ""),
        ("multitouch", "multitouch", "Multi-touch gestures (coming soon)", ""),
    )
    _NAV = (
        ("nav_back", "back", "Back", ""),
        ("nav_home", "home", "Home", ""),
        ("nav_recents", "recents", "Recent apps", ""),
    )

    def _build_sidebar(self) -> None:
        from .icons import icon

        rail = QWidget(self)
        rail.setObjectName("sideRail")
        rail.setFixedWidth(46)
        # native look: the window background + the style's flat tool buttons
        col = QVBoxLayout(rail)
        col.setContentsMargins(4, 8, 4, 8)
        col.setSpacing(4)
        self._rail_buttons: dict[str, QToolButton] = {}

        def button(method: str, icon_name: str, tip: str, key: str,
                   slot=None) -> QToolButton:
            act = QAction(icon(icon_name), tip, self)
            act.setProperty("ostIcon", icon_name)   # redrawn on a theme change
            if key:
                act.setShortcut(QKeySequence(key))
                tip = f"{tip} ({key})"
            act.setToolTip(tip)
            act.triggered.connect(slot or (lambda _=False, m=method: self._with_pane(m)))
            self.addAction(act)                 # shortcuts work even without focus
            b = QToolButton(rail)
            b.setDefaultAction(act)
            b.setIconSize(QSize(22, 22))
            b.setFixedSize(38, 38)
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            b.setAutoRaise(True)
            col.addWidget(b, 0, Qt.AlignHCenter)
            self._rail_buttons[method] = b
            return b

        for method, ic, tip, key in self._TOOLS:
            button(method, ic, tip, key)
        self._vol_btn = button("volume", "volume", "Volume / mute", "",
                               lambda _=False: self._volume_popup())
        # VNC display only: switch input between VNC and adb
        b = button("input_mode", "input", "Input: VNC (click to switch to ADB)", "",
                   lambda _=False: self._toggle_input_mode())
        self.act_input_mode = b.defaultAction()
        col.addStretch(1)
        for method, ic, tip, key in self._NAV:
            button(method, ic, tip, key)

        # tabs + rail side by side
        holder = QWidget(self)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.tabs, 1)
        row.addWidget(rail)
        self.setCentralWidget(holder)
        self.sidebar = rail
        self._side_row = row

    def attach_side_panel(self, panel: QWidget) -> None:
        """Dock a panel (the controls editor) at the window's right edge, BlueStacks-style."""
        self._side_row.addWidget(panel)
        panel.show()
        panel.closed.connect(lambda: self._side_row.removeWidget(panel))

    def changeEvent(self, ev) -> None:
        """Light/dark switch (system or Settings > Theme): redraw the rail icons, which are
        painted in the palette's text colour."""
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.PaletteChange and hasattr(self, "_rail_buttons"):
            from .icons import icon
            for act in self.actions():
                name = act.property("ostIcon")
                if name:
                    act.setIcon(icon(name))
            pane = self.current_pane()
            if pane is not None:
                self._vol_btn.defaultAction().setIcon(
                    icon("volume_muted" if pane.inst.muted else "volume"))
        super().changeEvent(ev)

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

    def _volume_popup(self) -> None:
        """BlueStacks-style: the speaker button opens a slider with a mute toggle."""
        from .icons import icon
        pane = self.current_pane()
        if pane is None:
            self.statusBar().showMessage("No instance tab focused.", 3000)
            return
        inst = pane.inst
        pop = QFrame(self, Qt.Popup)
        pop.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(pop)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        pct = QLabel(f"{inst.volume}%", pop)
        pct.setAlignment(Qt.AlignHCenter)
        slider = QSlider(Qt.Vertical, pop)
        slider.setRange(0, 100)
        slider.setValue(int(inst.volume))
        slider.setFixedHeight(140)
        mute = QToolButton(pop)
        mute.setCheckable(True)
        mute.setChecked(bool(inst.muted))
        mute.setAutoRaise(True)
        mute.setIconSize(QSize(20, 20))

        def show_mute():
            mute.setIcon(icon("volume_muted" if inst.muted else "volume"))
            mute.setToolTip("Unmute" if inst.muted else "Mute")
            self._vol_btn.defaultAction().setIcon(
                icon("volume_muted" if inst.muted else "volume"))

        def save():
            self.store.update(inst)
            pane.apply_volume()

        apply_later = QTimer(pop)
        apply_later.setSingleShot(True)
        apply_later.setInterval(120)          # a drag sends a few updates, not hundreds
        apply_later.timeout.connect(save)

        def on_value(v: int):
            inst.volume = v
            pct.setText(f"{v}%")
            if inst.muted and v > 0:          # moving the slider unmutes
                inst.muted = False
                mute.setChecked(False)
                show_mute()
            apply_later.start()

        def on_mute(on: bool):
            inst.muted = on
            show_mute()
            save()

        slider.valueChanged.connect(on_value)
        mute.toggled.connect(on_mute)
        show_mute()
        lay.addWidget(pct)
        lay.addWidget(slider, 0, Qt.AlignHCenter)
        lay.addWidget(mute, 0, Qt.AlignHCenter)
        pop.adjustSize()
        b = self._vol_btn
        pos = b.mapToGlobal(b.rect().topLeft())
        pop.move(pos.x() - pop.width() - 4, pos.y() - pop.height() // 2 + b.height() // 2)
        pop.show()

    def _toggle_input_mode(self) -> None:
        pane = self.current_pane()
        if pane is None:
            return
        new = "vnc" if pane.input_bridge is pane.adb_input else "adb"
        pane.set_input_mode(new)
        self.act_input_mode.setToolTip(
            f"Input: {new.upper()} (click to switch to {'VNC' if new == 'adb' else 'ADB'})")
        pane.flash(f"input pipeline switched to {new}")

    def _on_current_tab(self, idx: int) -> None:
        self._with_pane("refresh_adb")
        pane = self.current_pane()
        # the VNC/ADB input toggle only applies to the VNC display
        if pane is not None:
            from .icons import icon
            self._rail_buttons["input_mode"].setVisible(not pane.native)
            self._vol_btn.defaultAction().setIcon(
                icon("volume_muted" if pane.inst.muted else "volume"))
            QTimer.singleShot(0, lambda: self.fit_to_screen(pane))

    def _close_tab(self, idx: int) -> None:
        w = self.tabs.widget(idx)
        if isinstance(w, PlayerPane):
            stop = False
            if qemu_engine.is_running(w.inst):
                box = QMessageBox(self)
                box.setWindowTitle("Close instance")
                box.setText(f"Shut down {w.inst.name}?")
                box.setInformativeText("Keep running closes only this tab; the instance "
                                       "stays up and can be reopened from the manager.")
                b_stop = box.addButton("Shut down", QMessageBox.DestructiveRole)
                box.addButton("Keep running", QMessageBox.AcceptRole)
                b_cancel = box.addButton(QMessageBox.Cancel)
                box.setDefaultButton(b_stop)
                box.exec()
                if box.clickedButton() is b_cancel:
                    return
                stop = box.clickedButton() is b_stop
            w.shutdown()
            if stop:
                inst = w.inst
                th = _CallThread(lambda: "ok" if qemu_engine.shutdown(inst) else "")
                th.done.connect(lambda ok, n=inst.name: self.statusBar().showMessage(
                    f"{n} shut down" if ok == "ok" else f"{n}: QEMU did not stop", 5000))
                th.start()
                self._bg_threads.append(th)
        self.tabs.removeTab(idx)

    def nativeEvent(self, event_type, message):
        msg = _win_msg(message)
        paths = _dropped_paths(msg) if msg is not None and msg.message == _WM_COPYDATA else None
        if paths:                     # dropped on a native screen: its pane, else the current
            sender = int(msg.wParam or 0)
            target = self.current_pane()
            for pane in self._panes():
                host = getattr(pane.display, "_hwnd", 0) if pane.native else 0
                if host and sender and (host == sender
                                        or ctypes.windll.user32.IsChild(host, sender)):
                    target = pane
                    break
            if target is not None:
                target.drop_files(paths)
            return True, 1
        return super().nativeEvent(event_type, message)

    def fit_to_screen(self, pane: Optional["PlayerPane"] = None) -> None:
        """Resize the window so the Screen tab has exactly the guest's aspect ratio (no
        letterbox strips), keeping the current screen height where it fits on the desktop."""
        pane = pane or self.current_pane()
        if pane is None or pane.screen_window is not None or self.isMaximized() \
                or self.isFullScreen():
            return
        gw, gh = pane._screen_size()
        disp = pane.display
        if disp.width() < 10 or disp.height() < 10:
            return
        chrome_w = self.width() - disp.width()
        chrome_h = self.height() - disp.height()
        avail = self.screen().availableGeometry() if self.screen() else None
        max_w = (avail.width() * 0.9 if avail else 1600) - chrome_w
        max_h = (avail.height() * 0.9 if avail else 900) - chrome_h
        # keep the current screen height (landscape) or width (portrait), within the desktop
        th = min(max(disp.height(), 360), max_h)
        tw = th * gw / gh
        if tw > max_w:
            tw, th = max_w, max_w * gh / gw
        self.resize(int(round(tw)) + chrome_w, int(round(th)) + chrome_h)

    def restart_instance(self, inst: Instance, after_boot=None) -> None:
        """Stop QEMU, start it again with fresh props, reopen the tab; after_boot(pane)
        runs once the new pane exists (it should wait for the guest itself)."""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PlayerPane) and w.inst.name == inst.name:
                w.shutdown()
                self.tabs.removeTab(i)
                break
        self.statusBar().showMessage(f"restarting {inst.name}...")

        def job():
            qemu_engine.shutdown(inst, timeout=10)
            res = qemu_engine.start(inst, self.store.global_settings)
            if not res.ok:
                raise RuntimeError(res.error or "; ".join(res.warnings) or "start failed")
            return "ok"
        th = _CallThread(job, self)

        def started(_):
            pane = self.open_instance(inst)
            if after_boot:
                after_boot(pane)
        th.done.connect(started)
        th.failed.connect(lambda e: QMessageBox.critical(self, "Restart failed",
                                                         f"{inst.name}: {e}"))
        th.start()
        self._bg_threads.append(th)

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
        QTimer.singleShot(0, lambda: self.fit_to_screen(pane))   # after the layout settles
        return pane

    def close_quietly(self) -> None:
        """Close without asking (the manager is exiting and stops every instance itself)."""
        self._quiet_close = True
        self.close()

    def _panes(self) -> list[PlayerPane]:
        return [w for w in (self.tabs.widget(i) for i in range(self.tabs.count()))
                if isinstance(w, PlayerPane)]

    def closeEvent(self, ev) -> None:
        panes = self._panes()
        running = [p.inst for p in panes if qemu_engine.is_running(p.inst)]
        stop = False
        if running and not getattr(self, "_quiet_close", False):
            names = ", ".join(i.name for i in running)
            box = QMessageBox(self)
            box.setWindowTitle("Close player")
            box.setIcon(QMessageBox.Question)
            box.setText(f"Shut down {'all running instances' if len(running) > 1 else names}?")
            box.setInformativeText(
                (f"Running: {names}.\n\n" if len(running) > 1 else "")
                + "Keep running closes only this window; the instances stay up and can be "
                  "reopened from the manager.")
            b_stop = box.addButton("Shut down" + (" all" if len(running) > 1 else ""),
                                   QMessageBox.DestructiveRole)
            box.addButton("Keep running", QMessageBox.AcceptRole)
            b_cancel = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(b_stop)
            box.exec()
            if box.clickedButton() is b_cancel:
                ev.ignore()
                return
            stop = box.clickedButton() is b_stop
        self._quiet_close = False
        for p in panes:
            p.shutdown()
        self.tabs.clear()                     # reopening from the manager starts fresh tabs
        if stop:
            th = _CallThread(lambda: ",".join(i.name for i in running
                                              if not qemu_engine.shutdown(i)), None)
            th.done.connect(lambda bad: bad and QMessageBox.warning(
                None, "OpenStacks", f"QEMU did not stop: {bad}"))
            th.start()
            self._bg_threads.append(th)
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
