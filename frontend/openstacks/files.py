"""File transfer window: browse the guest's storage and move files in and out over adb.

- Drag files/folders from Explorer onto the list to upload them into the current folder.
- Drag entries out of the list into Explorer (they are pulled to a temp folder first).
- Double-click a folder to open it; double-click a file to download it to the host
  download folder and open it there.
- Buttons: upload files / folder, download (to a chosen folder), new folder, delete.

All adb work runs on one worker thread, in order, so the window never blocks except when
dragging out (Explorer needs the files to exist when the drag starts).
"""
from __future__ import annotations

import os
import posixpath
import queue
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QDrag, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog,
                               QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QStyle, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from .adb import Adb, AdbError

HOME = "/sdcard"
PLACES = [("Internal storage", "/sdcard"), ("Download", "/sdcard/Download"),
          ("Pictures", "/sdcard/Pictures"), ("Camera (DCIM)", "/sdcard/DCIM"),
          ("Documents", "/sdcard/Documents"), ("Movies", "/sdcard/Movies"),
          ("Music", "/sdcard/Music"), ("Android/data", "/sdcard/Android/data"),
          ("/data/local/tmp", "/data/local/tmp")]


def host_downloads() -> Path:
    p = Path.home() / "Downloads"
    return p if p.is_dir() else Path.home()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class _Worker(QThread):
    """Runs queued (label, fn, on_done) jobs one at a time."""
    started_job = Signal(str)
    finished_job = Signal(object, object, str)    # on_done, result, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self.q: "queue.Queue[Optional[tuple]]" = queue.Queue()

    def submit(self, label: str, fn: Callable, on_done: Optional[Callable] = None) -> None:
        self.q.put((label, fn, on_done))

    def run(self) -> None:
        while True:
            job = self.q.get()
            if job is None:
                return
            label, fn, on_done = job
            self.started_job.emit(label)
            try:
                self.finished_job.emit(on_done, fn(), "")
            except Exception as e:                       # adb errors etc.
                self.finished_job.emit(on_done, None, f"{label}: {e}")

    def stop(self) -> None:
        self.q.put(None)
        self.wait(3000)


class _List(QTreeWidget):
    """The file list: accepts Explorer drops, starts drags out to Explorer."""

    def __init__(self, owner: "FileTransferDialog"):
        super().__init__(owner)
        self.owner = owner
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    # in: Explorer -> guest
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls() and ev.source() is not self:
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev) -> None:
        self.dragEnterEvent(ev)

    def dropEvent(self, ev) -> None:
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            ev.acceptProposedAction()
            self.owner.upload(paths)

    # out: guest -> Explorer
    def startDrag(self, actions) -> None:
        remotes = self.owner.selected_paths()
        if not remotes:
            return
        local = self.owner.pull_for_drag(remotes)
        if not local:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(p)) for p in local])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


class FileTransferDialog(QDialog):
    def __init__(self, adb: Adb, title: str, parent=None, start: str = HOME):
        super().__init__(parent)
        self.adb = adb
        self.cwd = start
        self.history: list[str] = []
        self._tmp = Path(tempfile.mkdtemp(prefix="openstacks_drag_"))
        self.setWindowTitle(f"Files - {title}")
        self.resize(760, 520)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        st = self.style()

        # navigation row
        self.btn_back = self._tool(st.standardIcon(QStyle.SP_ArrowBack), "Back", self.go_back)
        self.btn_up = self._tool(st.standardIcon(QStyle.SP_ArrowUp), "Up one folder", self.go_up)
        self.btn_home = self._tool(st.standardIcon(QStyle.SP_DirHomeIcon), "Internal storage",
                                   lambda: self.navigate(HOME))
        self.path_edit = QLineEdit(self)
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text().strip()))
        self.places = QComboBox(self)
        self.places.addItem("Go to...")
        for label, p in PLACES:
            self.places.addItem(label, p)
        self.places.activated.connect(self._place_chosen)
        self.btn_refresh = self._tool(st.standardIcon(QStyle.SP_BrowserReload), "Refresh (F5)",
                                      self.refresh)
        nav = QHBoxLayout()
        for w in (self.btn_back, self.btn_up, self.btn_home):
            nav.addWidget(w)
        nav.addWidget(self.path_edit, 1)
        nav.addWidget(self.places)
        nav.addWidget(self.btn_refresh)

        # list
        self.list = _List(self)
        self.list.setHeaderLabels(["Name", "Size", "Modified"])
        self.list.setRootIsDecorated(False)
        self.list.setSortingEnabled(False)
        self.list.setUniformRowHeights(True)
        hdr = self.list.header()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.list.itemDoubleClicked.connect(self._activated)
        self.list.itemSelectionChanged.connect(self._sel_changed)

        # actions row
        self.b_up_files = QPushButton(st.standardIcon(QStyle.SP_FileIcon), "Upload files...")
        self.b_up_files.clicked.connect(self._pick_upload_files)
        self.b_up_dir = QPushButton(st.standardIcon(QStyle.SP_DirIcon), "Upload folder...")
        self.b_up_dir.clicked.connect(self._pick_upload_dir)
        self.b_down = QPushButton(st.standardIcon(QStyle.SP_DialogSaveButton), "Download...")
        self.b_down.clicked.connect(self._pick_download)
        self.b_mkdir = QPushButton(st.standardIcon(QStyle.SP_FileDialogNewFolder), "New folder")
        self.b_mkdir.clicked.connect(self._new_folder)
        self.b_del = QPushButton(st.standardIcon(QStyle.SP_TrashIcon), "Delete")
        self.b_del.clicked.connect(self._delete)
        acts = QHBoxLayout()
        for b in (self.b_up_files, self.b_up_dir, self.b_down, self.b_mkdir):
            acts.addWidget(b)
        acts.addStretch(1)
        acts.addWidget(self.b_del)

        # status row
        self.status = QLabel("Drop files here to upload them; drag files out to download.")
        self.busy = QProgressBar(self)
        self.busy.setRange(0, 0)
        self.busy.setFixedWidth(120)
        self.busy.setTextVisible(False)
        self.busy.hide()
        stat = QHBoxLayout()
        stat.addWidget(self.status, 1)
        stat.addWidget(self.busy)

        lay = QVBoxLayout(self)
        lay.addLayout(nav)
        lay.addWidget(self.list, 1)
        lay.addLayout(acts)
        lay.addLayout(stat)

        QShortcut(QKeySequence(Qt.Key_F5), self, activated=self.refresh)
        QShortcut(QKeySequence(Qt.Key_Delete), self.list, activated=self._delete)
        QShortcut(QKeySequence(Qt.Key_Backspace), self.list, activated=self.go_up)
        QShortcut(QKeySequence.Paste, self, activated=self._paste)

        self._pending = 0
        self.worker = _Worker(self)
        self.worker.started_job.connect(self._job_started)
        self.worker.finished_job.connect(self._job_finished)
        self.worker.start()
        self._sel_changed()
        self.navigate(start, remember=False)

    # -- plumbing ------------------------------------------------------------------
    def _tool(self, icon, tip, slot) -> QToolButton:
        b = QToolButton(self)
        b.setIcon(icon)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        return b

    def _submit(self, label: str, fn: Callable, on_done: Optional[Callable] = None) -> None:
        self._pending += 1
        self.busy.show()
        self.worker.submit(label, fn, on_done)

    def _job_started(self, label: str) -> None:
        self.status.setText(label + "...")

    def _job_finished(self, on_done, result, error: str) -> None:
        self._pending -= 1
        if self._pending <= 0:
            self._pending = 0
            self.busy.hide()
        if error:
            self.status.setText(error.splitlines()[0][:200])
            return
        if on_done:
            on_done(result)

    def done(self, r: int) -> None:    # Esc, the close button and close() all end here
        if self.worker.isRunning():
            self.worker.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        super().done(r)

    # -- navigation ----------------------------------------------------------------
    def navigate(self, path: str, remember: bool = True) -> None:
        path = posixpath.normpath(path or "/")
        prev = self.cwd

        def done(entries):
            if remember and prev != path:
                self.history.append(prev)
            self.cwd = path
            self.path_edit.setText(path)
            self._fill(entries)
            n_dir = sum(e["dir"] for e in entries)
            self.status.setText(f"{path}: {n_dir} folders, {len(entries) - n_dir} files")
        self.path_edit.setText(path)
        self._submit(f"Listing {path}", lambda: self.adb.listdir(path), done)

    def refresh(self) -> None:
        self.navigate(self.cwd, remember=False)

    def go_up(self) -> None:
        if self.cwd != "/":
            self.navigate(posixpath.dirname(self.cwd.rstrip("/")) or "/")

    def go_back(self) -> None:
        if self.history:
            self.navigate(self.history.pop(), remember=False)

    def _place_chosen(self, idx: int) -> None:
        p = self.places.itemData(idx)
        self.places.setCurrentIndex(0)
        if p:
            self.navigate(p)

    def _fill(self, entries: list[dict]) -> None:
        st = self.style()
        dir_icon, file_icon = st.standardIcon(QStyle.SP_DirIcon), st.standardIcon(QStyle.SP_FileIcon)
        self.list.clear()
        for e in sorted(entries, key=lambda e: (not e["dir"], e["name"].lower())):
            it = QTreeWidgetItem([e["name"], "" if e["dir"] else human(e["size"]),
                                  time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"]))
                                  if e["mtime"] else ""])
            it.setIcon(0, dir_icon if e["dir"] else file_icon)
            it.setData(0, Qt.UserRole, e)
            it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.list.addTopLevelItem(it)
        self.btn_back.setEnabled(bool(self.history))
        self.btn_up.setEnabled(self.cwd != "/")

    def _activated(self, item: QTreeWidgetItem, _col: int) -> None:
        e = item.data(0, Qt.UserRole)
        remote = posixpath.join(self.cwd, e["name"])
        if e["dir"]:
            self.navigate(remote)
            return
        dest = host_downloads()

        def done(_):
            local = dest / e["name"]
            self.status.setText(f"Downloaded to {local}")
            try:                                  # open with the default app
                if sys.platform == "win32":
                    os.startfile(str(local))
                else:
                    subprocess.Popen(["xdg-open", str(local)], start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        self._submit(f"Downloading {e['name']}", lambda: self.adb.pull(remote, dest), done)

    def _sel_changed(self) -> None:
        has = bool(self.list.selectedItems())
        self.b_down.setEnabled(has)
        self.b_del.setEnabled(has)

    def selected_paths(self) -> list[str]:
        return [posixpath.join(self.cwd, it.data(0, Qt.UserRole)["name"])
                for it in self.list.selectedItems()]

    # -- transfers -----------------------------------------------------------------
    def upload(self, paths: list[str], dest: Optional[str] = None) -> None:
        dest = dest or self.cwd
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return

        def job():
            for p in paths:
                self.adb.push(p, dest + "/")
                if os.path.isfile(p) and dest.startswith(("/sdcard", "/storage")):
                    self.adb.media_scan(posixpath.join(dest, os.path.basename(p)))
            return len(paths)

        def done(n):
            self.status.setText(f"Uploaded {n} item(s) to {dest}")
            if dest == self.cwd:
                self.refresh()
        name = os.path.basename(paths[0]) + (f" + {len(paths) - 1} more" if len(paths) > 1 else "")
        self._submit(f"Uploading {name}", job, done)

    def _pick_upload_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Upload files to " + self.cwd)
        self.upload(files)

    def _pick_upload_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Upload folder to " + self.cwd)
        if d:
            self.upload([d])

    def _paste(self) -> None:
        md = QGuiApplication.clipboard().mimeData()
        if md and md.hasUrls():
            self.upload([u.toLocalFile() for u in md.urls() if u.isLocalFile()])

    def _pick_download(self) -> None:
        remotes = self.selected_paths()
        if not remotes:
            return
        d = QFileDialog.getExistingDirectory(self, "Download to", str(host_downloads()))
        if not d:
            return

        def job():
            for r in remotes:
                self.adb.pull(r, d)
            return len(remotes)
        self._submit(f"Downloading {len(remotes)} item(s)", job,
                     lambda n: self.status.setText(f"Downloaded {n} item(s) to {d}"))

    def pull_for_drag(self, remotes: list[str]) -> list[Path]:
        """Synchronous pull into a fresh temp folder (Explorer needs real files)."""
        out = Path(tempfile.mkdtemp(dir=self._tmp))
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.status.setText(f"Fetching {len(remotes)} item(s) for drag...")
        QApplication.processEvents()
        try:
            for r in remotes:
                self.adb.pull(r, out)
        except AdbError as e:
            self.status.setText(f"drag failed: {e}")
            return []
        finally:
            QApplication.restoreOverrideCursor()
        self.status.setText("Drop into Explorer to save.")
        return [out / posixpath.basename(r) for r in remotes]

    def _new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        name = name.strip().strip("/")
        if ok and name:
            p = posixpath.join(self.cwd, name)
            self._submit(f"Creating {name}", lambda: self.adb.mkdir(p), lambda _: self.refresh())

    def _delete(self) -> None:
        remotes = self.selected_paths()
        if not remotes:
            return
        names = "\n".join(posixpath.basename(r) for r in remotes[:10])
        more = f"\n... and {len(remotes) - 10} more" if len(remotes) > 10 else ""
        if QMessageBox.question(self, "Delete", f"Delete from the VM?\n\n{names}{more}") \
                != QMessageBox.Yes:
            return

        def job():
            for r in remotes:
                self.adb.remove(r)
        self._submit(f"Deleting {len(remotes)} item(s)", job, lambda _: self.refresh())
