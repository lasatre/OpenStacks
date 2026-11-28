"""Program settings (not per instance): the [global] section of instances.conf.

Appearance applies at once (and is undone on Cancel); emulator paths and options apply to
instances started afterwards.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

from . import brand, paths
from .config import DEFAULT_ACCEL, Store


def _path_row(parent: QWidget, value: str, default: str, pick_dir: bool,
              title: str) -> tuple[QWidget, QLineEdit]:
    """A line edit (empty = the default, shown as placeholder) with a Browse button."""
    row = QWidget(parent)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    ed = QLineEdit(value, row)
    ed.setPlaceholderText(f"default: {default}" if default else "default")
    ed.setClearButtonEnabled(True)
    btn = QPushButton("Browse...", row)

    def browse() -> None:
        start = ed.text() or default
        if pick_dir:
            p = QFileDialog.getExistingDirectory(row, title, start)
        else:
            p, _ = QFileDialog.getOpenFileName(row, title, start)
        if p:
            ed.setText(p)
    btn.clicked.connect(browse)
    lay.addWidget(ed, 1)
    lay.addWidget(btn)
    return row, ed


class SettingsDialog(QDialog):
    def __init__(self, store: Store, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OpenStacks settings")
        self.store = store
        g = store.global_settings
        self._theme0 = g.theme
        outer = QVBoxLayout(self)

        # -- appearance
        box = QGroupBox("Appearance", self)
        form = QFormLayout(box)
        self.cb_theme = QComboBox(box)
        for key, label in brand.THEMES:
            self.cb_theme.addItem(label, key)
        self.cb_theme.setCurrentIndex(max(0, self.cb_theme.findData(g.theme)))
        self.cb_theme.currentIndexChanged.connect(self._preview_theme)
        form.addRow("Theme", self.cb_theme)
        outer.addWidget(box)

        # -- game controls
        box = QGroupBox("Game controls", self)
        form = QFormLayout(box)
        self.ck_download = QCheckBox("Download BlueStacks' controls for games that have none",
                                     box)
        self.ck_download.setChecked(g.controls_download)
        self.ck_download.setToolTip(
            "When you open a game with no controls file, ask BlueStacks' server for its file "
            "(only that game's package name is sent). Downloads are kept, and each game is "
            "checked again at most once a week.")
        form.addRow(self.ck_download)
        outer.addWidget(box)

        # -- emulator
        box = QGroupBox("Emulator", self)
        form = QFormLayout(box)
        self.cb_accel = QComboBox(box)
        hw = "Windows Hypervisor Platform (whpx)" if paths.WINDOWS else "KVM"
        self.cb_accel.addItem(f"Hardware: {hw}", DEFAULT_ACCEL)
        self.cb_accel.addItem("Software (tcg, very slow)", "tcg")
        self.cb_accel.setCurrentIndex(max(0, self.cb_accel.findData(g.acceleration)))
        form.addRow("Acceleration", self.cb_accel)
        self.cb_prio = QComboBox(box)
        self.cb_prio.addItem("Normal", "normal")
        self.cb_prio.addItem("High", "high")
        self.cb_prio.setCurrentIndex(max(0, self.cb_prio.findData(g.priority)))
        form.addRow("Process priority", self.cb_prio)
        w, self.ed_qemu = _path_row(box, g.qemu_path, store.global_settings.resolved_qemu_path()
                                    if not g.qemu_path else "", False, "QEMU executable")
        form.addRow("QEMU", w)
        w, self.ed_adb = _path_row(box, g.adb_path, store.global_settings.resolved_adb_path()
                                   if not g.adb_path else "", False, "adb executable")
        form.addRow("adb", w)
        outer.addWidget(box)

        # -- folders
        box = QGroupBox("Folders", self)
        form = QFormLayout(box)
        w, self.ed_shots = _path_row(box, g.screenshots_dir, str(paths.SCREENSHOTS_DIR), True,
                                     "Screenshots folder")
        form.addRow("Screenshots", w)
        outer.addWidget(box)

        note = QLabel("Emulator settings apply to instances started from now on.", self)
        note.setEnabled(False)
        outer.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        outer.addWidget(bb)
        self.resize(520, 0)

    def _preview_theme(self) -> None:
        brand.apply_theme(QApplication.instance(), self.cb_theme.currentData())

    def accept(self) -> None:
        g = self.store.global_settings
        g.theme = self.cb_theme.currentData()
        g.controls_download = self.ck_download.isChecked()
        g.acceleration = self.cb_accel.currentData()
        g.priority = self.cb_prio.currentData()
        g.qemu_path = self.ed_qemu.text().strip()
        g.adb_path = self.ed_adb.text().strip()
        g.screenshots_dir = self.ed_shots.text().strip()
        self.store.save()
        super().accept()

    def reject(self) -> None:
        if self.cb_theme.currentData() != self._theme0:
            brand.apply_theme(QApplication.instance(), self._theme0)
        super().reject()
