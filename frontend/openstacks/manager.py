"""OpenStacks instance manager - the BlueStacks-multi-instance analogue.

QMainWindow with:
  * a QListWidget of instances (thumbnail icon, name, resolution, ports,
    running state refreshed by a timer),
  * Launch / Stop / Clone / Delete / Settings (+ New) buttons,
  * double-click opens-or-focuses the instance's player tab (PlayerWindow).

Run via:  python -m openstacks.manager
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QProgressDialog,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from . import __version__
from .adb import Adb, AdbError
from .config import Instance, KNOWN_DEVICES, Store
from .qemu_engine import ENGINE as qemu_engine


def run_app() -> QApplication:
    """QApplication with the shared QEMU-engine + Store singletons ready."""
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("OpenStacks")
    from . import brand
    brand.apply(app)
    Store()                # ensure instances.conf is parsed once
    return app


# -- thumbnail generation ---------------------------------------------------------

def make_thumb_placeholder(inst: Instance, running: bool,
                           max_w: int = 128, max_h: int = 72) -> QPixmap:
    """Simple painted placeholder: resolution box + name + state dot."""
    w, h = inst.res_wh()
    pm = QPixmap(max_w, max_h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    # brand colors: indigo tile, teal/indigo accents when running
    p.setPen(QColor(running and "#6366F1" or "#3a3d5c"))
    p.setBrush(QColor(running and "#272463" or "#1E1B4B"))
    p.drawRoundedRect(6, 6, max_w - 13, max_h - 13, 6, 6)
    p.setPen(QColor(running and "#FFFFFF" or "#A5B4FC"))
    p.drawText(QRect(8, 10, max_w - 18, 20), Qt.AlignCenter, inst.name)
    p.setPen(QColor("#8b8fc7"))
    p.drawText(QRect(8, max_h - 30, max_w - 18, 14),
               Qt.AlignCenter, f"{w}x{h} @{inst.dpi}")
    p.setBrush(QColor(running and "#2DD4BF" or "#555b66"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(8, 8, 8, 8)
    p.end()
    return pm


class _ThumbThread(QThread):
    """Fetches a real screenshot thumbnail as PNG bytes via adb."""
    done = Signal(str, object)          # instance name, QPixmap|None

    def __init__(self, inst: Instance, adb: Adb, parent=None):
        super().__init__(parent)
        self.inst, self.adb = inst, adb

    def run(self) -> None:
        import tempfile
        pm = None
        with tempfile.TemporaryDirectory(prefix="ostthumb_") as tmp:
            png = Path(tmp) / "t.png"
            try:
                self.adb.screencap(str(png), timeout=30)
                pm = QPixmap()
                pm.loadFromData(png.read_bytes())
            except Exception:               # adb offline etc. -> keep placeholder
                pm = None
        self.done.emit(self.inst.name, pm)


# -- dialogs ------------------------------------------------------------------------

class InstanceEditDialog(QDialog):
    """Create / edit an instance (name, cpus, ram, dpi, resolution, adb port,
    pci device set flags, extra -append tokens)."""

    def __init__(self, inst: Instance | None = None, parent=None,
                 suggested_adb_port: int = 5555):
        super().__init__(parent)
        self.setWindowTitle("Edit instance" if inst else "New instance")
        i = inst or Instance()
        form = QFormLayout(self)

        self.ed_name = QLineEdit(i.name)
        self.ed_name.setPlaceholderText("Pie64-style name (letters/digits/_)")

        from . import images
        self.cb_image = QComboBox()
        have = set(images.installed_images())
        for info in images.catalog():
            state = "ready" if info.name in have else f"download {info.size / 2**20:,.0f} MB"
            self.cb_image.addItem(f"Android {info.android} - {info.name} ({state})", info.name)
        k = self.cb_image.findData(i.image)
        self.cb_image.setCurrentIndex(k if k >= 0 else self.cb_image.findData("Pie64"))
        self.cb_image.setEnabled(inst is None)
        form.addRow("Name", self.ed_name)
        form.addRow("Android", self.cb_image)

        self.sp_cpus = QSpinBox()
        self.sp_cpus.setRange(1, 32)
        self.sp_cpus.setValue(i.cpus)
        form.addRow("vCPUs", self.sp_cpus)

        self.sp_ram = QSpinBox()
        self.sp_ram.setRange(256, 65536)
        self.sp_ram.setSuffix(" MB")
        self.sp_ram.setValue(i.ram_mb)
        form.addRow("RAM", self.sp_ram)

        self.ed_res = QLineEdit(i.resolution)
        self.ed_res.setPlaceholderText("WxH e.g. 1600x900")
        form.addRow("Resolution", self.ed_res)

        self.sp_dpi = QSpinBox()
        self.sp_dpi.setRange(120, 640)
        self.sp_dpi.setValue(i.dpi)
        form.addRow("DPI", self.sp_dpi)

        self.ed_adbport = QLineEdit(str(i.adb_port if inst else suggested_adb_port))
        form.addRow("ADB host port", self.ed_adbport)

        self.ed_devices = QLineEdit(i.devices)
        self.ed_devices.setToolTip(f"CSV of PCI device set flags; known: {', '.join(KNOWN_DEVICES)}")
        form.addRow("PCI devices", self.ed_devices)

        self.ed_append = QLineEdit(i.extra_append)
        self.ed_append.setPlaceholderText("extra kernel args / KEY=VALUE props for -append")
        form.addRow("Extra -append", self.ed_append)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def apply_to(self, inst: Instance) -> None:
        inst.name = self.ed_name.text().strip()
        inst.cpus = int(self.sp_cpus.value())
        inst.ram_mb = int(self.sp_ram.value())
        inst.resolution = self.ed_res.text().strip() or "1600x900"
        inst.dpi = int(self.sp_dpi.value())
        try:
            inst.adb_port = int(self.ed_adbport.text().strip())
        except ValueError:
            pass
        inst.devices = self.ed_devices.text().strip()
        inst.extra_append = self.ed_append.text().strip()
        if self.cb_image.isEnabled():
            inst.image = self.cb_image.currentData()


# -- main window ---------------------------------------------------------------------

class ManagerWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = Store()
        self.setWindowTitle("OpenStacks")
        self.resize(560, 660)

        central = QWidget(self)
        lay = QVBoxLayout(central)
        lay.addLayout(self._build_header(central))

        self.list = QListWidget(central)
        self.list.setIconSize(self._icon_size())
        self.list.itemDoubleClicked.connect(self._activate)
        lay.addWidget(self.list, 1)

        btns = QHBoxLayout()
        for text, slot in (("New", self._new), ("Launch", self._launch),
                           ("Stop", self._stop), ("Clone", self._clone),
                           ("Delete", self._delete), ("Set default", self._set_default),
                           ("Settings", self._edit_settings)):
            b = QPushButton(text, central)
            b.clicked.connect(slot)
            btns.addWidget(b)
        lay.addLayout(btns)
        self.setCentralWidget(central)

        self.statusBar().showMessage("Tip: double-click an instance to open its player.")
        self._player = None          # PlayerWindow, created lazily
        self._thumbs: dict[str, QPixmap] = {}
        self._thumb_pending: set[str] = set()
        self._thumb_threads: list[_ThumbThread] = []
        self._threads: list[QThread] = []

        self.refresh()
        QTimer.singleShot(400, self._preflight)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(2000)      # gentle running-state refresh

    # -- header / about --------------------------------------------------------------------
    def _build_header(self, parent) -> QHBoxLayout:
        from . import brand
        row = QHBoxLayout()
        row.setContentsMargins(4, 2, 4, 6)
        self._logo = QLabel(parent)
        self._logo.setPixmap(brand.logo(30))
        self._logo.setToolTip(f"OpenStacks {__version__}")
        row.addWidget(self._logo)
        row.addStretch(1)
        ver = QLabel(f"v{__version__}", parent)
        ver.setEnabled(False)                     # muted text
        row.addWidget(ver)
        about = QPushButton("About", parent)
        about.clicked.connect(self._about)
        row.addWidget(about)
        return row

    def _about(self) -> None:
        from . import brand
        dlg = QDialog(self)
        dlg.setWindowTitle("About OpenStacks")
        lay = QVBoxLayout(dlg)
        logo = QLabel(dlg)
        logo.setPixmap(brand.logo(96, stacked=True))
        logo.setAlignment(Qt.AlignCenter)
        lay.addWidget(logo)
        text = QLabel(dlg)
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setOpenExternalLinks(True)
        text.setText(
            f"<p align=center><b>OpenStacks {__version__}</b><br>"
            "Licensed under the GNU GPL v2.0 or later</p>"
            "<p>Runs de-bloated BlueStacks 5 Android images through QEMU"
            "<p>Not affiliated with or endorsed by BlueStacks or now.gg. BlueStacks 5 must "
            "be installed for its renderer.</p>"
            "<p><b>Third-party software</b>: QEMU (GPL-2.0), Qt / PySide6 (LGPL-3.0), "
            "7-Zip (LGPL-2.1), Sora font (OFL-1.1); downloaded on demand: Lawnchair "
            "(GPL-3.0), Kitsune Magisk (GPL-3.0), Android platform-tools (Google). "
            "See THIRD-PARTY.md.</p>")
        lay.addWidget(text)
        bb = QDialogButtonBox(QDialogButtonBox.Close, parent=dlg)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.resize(460, 0)
        dlg.exec()

    # -- first-run prerequisite check ------------------------------------------------------
    def _preflight(self) -> None:
        from . import checks
        problems = checks.run(self.store.global_settings)
        if not problems:
            return
        box = QMessageBox(self)
        box.setWindowTitle("OpenStacks setup")
        box.setIcon(QMessageBox.Warning)
        box.setText("Some things OpenStacks needs are missing:")
        box.setInformativeText("\n\n".join(f"\u2022 {p.title}: {p.detail}" for p in problems))
        fix = None
        if any(p.key == "adb" and p.fixable for p in problems):
            fix = box.addButton("Download platform-tools", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Ok)
        box.exec()
        if fix is not None and box.clickedButton() is fix:
            self._download_adb()

    def _download_adb(self) -> None:
        from . import checks
        dlg = QProgressDialog("Downloading Android platform-tools from Google...", "", 0, 0, self)
        dlg.setWindowTitle("OpenStacks setup")
        dlg.setCancelButton(None)
        dlg.setMinimumDuration(0)
        dlg.show()

        def done(path):
            dlg.close()
            self.statusBar().showMessage(f"adb ready: {path}", 8000)

        def failed(msg):
            dlg.close()
            QMessageBox.warning(self, "OpenStacks setup", f"platform-tools download failed: {msg}")
        th = _LaunchThread(checks.download_platform_tools, self)
        th.done.connect(done)
        th.failed.connect(failed)
        th.start()
        self._threads.append(th)

    def changeEvent(self, ev) -> None:            # light/dark switch: swap the logo
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.PaletteChange and hasattr(self, "_logo"):
            from . import brand
            self._logo.setPixmap(brand.logo(30))
        super().changeEvent(ev)

    # -- helpers --------------------------------------------------------------------------
    def _icon_size(self):
        return QSize(128, 72)

    def _selected_name(self) -> Optional[str]:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _is_running(self, name: str) -> bool:
        return qemu_engine.is_running(name)

    # -- list rendering ---------------------------------------------------------------------
    def _activate(self, item=None) -> None:
        """Double-click: boot the instance if it is stopped, then show its player."""
        if item is not None:
            self.list.setCurrentItem(item)
        self._launch()

    def refresh(self) -> None:
        keep = self._selected_name()
        insts = self.store.instances()
        names = [i.name for i in insts]
        same = names == [self.list.item(r).data(Qt.UserRole) for r in range(self.list.count())]
        if not same:
            self.list.clear()
        for row, inst in enumerate(insts):
            running = self._is_running(inst.name)
            err = "; ".join(inst.validate())
            line1 = f"{inst.name}{'  (default)' if inst.name == self.store.default_name() else ''}"
            line2 = (f"{inst.resolution}@{inst.dpi}dpi · {inst.cpus}vCPU · {inst.ram_mb}MB"
                     f" · adb {inst.adb_port} · vnc {inst.vnc_port}"
                     + (" · RUNNING" if running else "")
                     + (f" · {err}" if err else ""))
            pm = self._thumbs.get(inst.name)
            if pm is None:
                pm = make_thumb_placeholder(inst, running)
            text = f"{line1}\n{line2}"
            # update rows in place: rebuilding the list every tick swallows clicks
            item = self.list.item(row) if same else QListWidgetItem()
            if item.text() != text:
                item.setText(text)
            if (running, pm.cacheKey()) != item.data(Qt.UserRole + 1):
                item.setIcon(QIcon(pm))
                item.setData(Qt.UserRole + 1, (running, pm.cacheKey()))
            item.setData(Qt.UserRole, inst.name)
            item.setToolTip(err or f"{inst.name}: adb 127.0.0.1:{inst.adb_port}")
            item.setForeground(QColor("#2ecc71") if running
                               else self.list.palette().text().color())
            if not same:
                self.list.addItem(item)
                if inst.name == keep:
                    self.list.setCurrentItem(item)
            # fetch a real screenshot thumbnail for running instances (once)
            if running and inst.name not in self._thumbs \
                    and inst.name not in self._thumb_pending:
                self._thumb_pending.add(inst.name)
                adb = Adb(port=inst.adb_port,
                          adb_path=self.store.global_settings.resolved_adb_path() or None)
                th = _ThumbThread(inst, adb, self)
                th.done.connect(self._thumb_ready)
                th.start()
                self._thumb_threads.append(th)

    def _thumb_ready(self, name: str, pm) -> None:
        self._thumb_pending.discard(name)
        if pm is not None and not pm.isNull():
            self._thumbs[name] = pm
            self.refresh()

    # -- actions ----------------------------------------------------------------------------
    def _launch(self) -> None:
        name = self._selected_name()
        if not name:
            self.statusBar().showMessage("Select an instance first.", 5000)
            return
        inst = self.store.get_instance(name)
        if self._is_running(name):
            self._open_player()
            return
        self.statusBar().showMessage(f"Launching {name}...", 10000)
        def job():
            res = qemu_engine.start(inst, self.store.global_settings)
            return res
        self._run_async(job, self._launched)

    def _launched(self, res) -> None:
        name = ""
        for n, obj in qemu_engine.instances.items():
            name = n
            break
        if not res.ok:
            QMessageBox.critical(self, "Launch failed", res.error or "\n".join(res.warnings))
        else:
            for w in res.warnings:
                self.statusBar().showMessage(w, 8000)
        self.refresh()
        if res.ok:
            self._open_player()

    def launch_by_name(self, name: str) -> None:
        """Select an instance and launch it (OpenStacks.exe --launch <name>)."""
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == name:
                self.list.setCurrentRow(i)
                self._launch()
                return
        QMessageBox.warning(self, "OpenStacks", f"No instance named {name!r}.")

    def _stop(self) -> None:
        name = self._selected_name()
        if not name or not self._is_running(name):
            return
        def job():
            return qemu_engine.stop(name, timeout=10)
        self._run_async(job, lambda ok: self.statusBar().showMessage(
            "stopped" if ok else "still running (check QEMU)", 5000))

    def _clone(self) -> None:
        name = self._selected_name()
        if not name:
            return
        new, ok = QInputDialog.getText(self, "Clone instance",
                                       f"New name for <{name}>:", text=f"{name}_clone")
        if not ok or not new.strip():
            return
        new = new.strip()
        try:
            self.store.clone(name, new)
        except (ValueError, KeyError) as e:
            QMessageBox.critical(self, "Clone failed", str(e))
        self.refresh()

    def _delete(self) -> None:
        name = self._selected_name()
        if not name:
            return
        if QMessageBox.question(self, "Delete instance",
                                f"Delete configuration '{name}'?\n(disks are NOT deleted)") \
                == QMessageBox.Yes:
            if self._is_running(name):
                QMessageBox.warning(self, "Running", f"Stop '{name}' before deleting.")
                return
            try:
                self.store.delete(name)
            except KeyError as e:
                QMessageBox.critical(self, "Delete failed", str(e))
        self.refresh()

    def _new(self) -> None:
        dlg = InstanceEditDialog(suggested_adb_port=self.store._next_port(5555, "adb_port"),
                                 parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        inst = Instance()
        dlg.apply_to(inst)
        errs = inst.validate()
        if errs:
            QMessageBox.critical(self, "Invalid", "\n".join(errs))
            return
        from . import images
        if inst.image not in images.installed_images():
            if not self._install_image(inst.image):
                return
        try:
            self.store.create(inst.name, cpus=inst.cpus, ram_mb=inst.ram_mb,
                              dpi=inst.dpi, resolution=inst.resolution,
                              adb_port=inst.adb_port, devices=inst.devices,
                              extra_append=inst.extra_append, image=inst.image,
                              data_disk="")
        except ValueError as e:
            QMessageBox.critical(self, "Create failed", str(e))
        self.refresh()

    def _install_image(self, image: str) -> bool:
        """Download + prepare an engine image with a progress dialog. True = ready."""
        from . import images
        info = images.image_info(image)
        if QMessageBox.question(
                self, "Download Android image",
                f"{image} (Android {info.android}) is not on this PC yet.\n\n"
                f"Download it from BlueStacks' servers ({info.size / 2**20:,.0f} MB, "
                f"about {info.size * 4 / 2**30:.1f} GB on disk once unpacked)?") \
                != QMessageBox.Yes:
            return False
        dlg = QProgressDialog(f"Downloading {image}...", "Cancel", 0, 1000, self)
        dlg.setWindowTitle("Preparing Android image")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        th = _ImageInstallThread(image, self)
        result = {"ok": False, "err": ""}
        labels = {"download": "Downloading", "verify": "Verifying", "unpack": "Unpacking",
                  "assets": "Preparing kernel and disks"}

        def on_progress(stage, done, total):
            name = labels.get(stage, stage.capitalize())
            if stage == "download" and total:
                name += f" {done / 2**20:,.0f} / {total / 2**20:,.0f} MB"
            dlg.setLabelText(f"{name} - {image}")
            dlg.setValue(int(1000 * done / total) if total else 0)

        def on_ok(_path):
            result["ok"] = True
            dlg.close()

        def on_fail(msg):
            result["err"] = msg
            dlg.close()

        th.progress.connect(on_progress)
        th.finished_ok.connect(on_ok)
        th.failed.connect(on_fail)
        dlg.canceled.connect(th.cancel)
        th.start()
        dlg.exec()
        th.wait()
        if not result["ok"]:
            QMessageBox.warning(self, "Image not ready",
                                f"{image}: {result['err'] or 'cancelled'}")
        return result["ok"]

    def _set_default(self) -> None:
        name = self._selected_name()
        if not name:
            return
        try:
            self.store.set_default(name)
            self.statusBar().showMessage(f"default instance is now {name}", 5000)
        except KeyError as e:
            QMessageBox.critical(self, "Failed", str(e))
        self.refresh()

    def _edit_settings(self) -> None:
        name = self._selected_name()
        if not name:
            return
        inst = self.store.get_instance(name)
        dlg = InstanceEditDialog(inst, self)
        dlg.ed_name.setDisabled(True)     # rename not offered (ports/engines indirection)
        if dlg.exec() != QDialog.Accepted:
            return
        dlg.apply_to(inst)
        errs = inst.validate()
        if errs:
            QMessageBox.critical(self, "Invalid", "\n".join(errs))
            return
        self.store.update(inst)
        if self._player is not None:
            self._player._with_pane("refresh_adb")  # force status text refresh
        self.refresh()

    # -- player -----------------------------------------------------------------------------
    def _open_player(self, item=None) -> None:
        name = self._selected_name() or (item.data(Qt.UserRole) if item else None)
        if not name:
            return
        from .player import PlayerWindow            # late import avoids the cycle
        if self._player is None:
            self._player = PlayerWindow(self.store)
        if self._player.isVisible():
            self._player.raise_()
        else:
            self._player.show()
        self._player.open_instance(self.store.get_instance(name))

    # -- async plumbing -------------------------------------------------------------------------
    def _run_async(self, fn, on_done) -> None:
        th = _LaunchThread(fn, self)
        th.done.connect(on_done)
        th.failed.connect(lambda e: QMessageBox.critical(self, "Error", e))
        th.start()
        self._threads.append(th)

    def closeEvent(self, ev) -> None:
        running = [i.name for i in self.store.instances() if self._is_running(i.name)]
        if self._player is not None:
            self._player.close_quietly()          # the manager decides: stop everything
        if running:
            self.statusBar().showMessage(f"Shutting down {', '.join(running)}...")
            QApplication.processEvents()
            _shutdown_all(self.store)
        super().closeEvent(ev)
        QApplication.quit()

    def showEvent(self, ev) -> None:  # keep current icon size applied
        self.list.setIconSize(self._icon_size())
        super().showEvent(ev)


class _ImageInstallThread(QThread):
    """images.install_image() off the GUI thread (download, extract, patch, assets)."""
    progress = Signal(str, object, object)     # stage, done, total (can exceed 2^31)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, image: str, parent=None):
        super().__init__(parent)
        self.image = image
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from . import images
        try:
            out = images.install_image(self.image,
                                       progress=lambda st, d, t: self.progress.emit(st, d, t),
                                       cancel=lambda: self._cancel)
            self.finished_ok.emit(str(out))
        except images.Cancelled:
            self.failed.emit("cancelled (the partial download is kept and resumes next time)")
        except Exception as e:                   # network, disk, 7-Zip ...
            self.failed.emit(str(e))


class _LaunchThread(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as e:                  # QemuNotFoundError etc.
            self.failed.emit(str(e))


def _shutdown_all(store: Store) -> None:
    """Instance manager closed: no UI is left to use the VMs, stop them all."""
    for inst in store.instances():
        try:
            if qemu_engine.is_running(inst):
                qemu_engine.shutdown(inst, timeout=6.0)
        except Exception:                      # never block the exit
            pass


def main() -> None:
    app = run_app()
    win = ManagerWindow()
    win.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
