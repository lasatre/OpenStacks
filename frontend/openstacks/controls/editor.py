"""The controls editor, laid out like BlueStacks' Controls editor.

- A side panel in the player window holds the scheme picker, the Keyboard/Gamepad
  toggle, the palette of control types, the selected control's settings, and Import,
  Export, Reset and Save.
- An editing surface covers the game picture:
  - drag a type from the palette onto it;
  - drag a control (or one of its points) to move it;
  - click a key badge, then press a key, mouse button, wheel or gamepad button to bind it.
    Esc cancels; Backspace clears.
  - Delete removes the selected control; the arrow keys nudge it.
- Mapping is paused while the editor is open. Saving writes our copy of the game's
  `.cfg` and reloads it.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
                               QSpinBox, QToolButton, QVBoxLayout, QWidget)

from .. import paths
from . import keys, store, templates
from .bscfg import BASE_FIELDS, ControlsFile, Scheme, control_type

MIME = "application/x-openstacks-control"
_SKIP = {"$type", "Type", "Guidance"}


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _binding_text(c: dict, field: str, gamepad: bool) -> str:
    v = c.get(field + "_alt1" if gamepad else field) or ""
    return " + ".join(keys.label(p) for p in keys.split_combo(v)) if v else ""


# ------------------------------------------------------------------ editing surface
class EditSurface(QWidget):
    """Interactive layer over the game picture (a tool window owned by the player)."""
    selected = Signal(object)        # the control dict, or None
    edited = Signal()

    FLAGS = Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint

    def __init__(self, editor: "ControlsEditor", owner: QWidget):
        super().__init__(owner, self.FLAGS)
        self.ed = editor
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.sel: Optional[dict] = None
        self.capture: Optional[tuple[dict, str]] = None
        self._pending_mod = ""           # a modifier pressed during capture, alone so far
        self._drag: Optional[tuple] = None
        self._badges: list[tuple[QRectF, dict, str]] = []
        self._handles: list[tuple[QRectF, dict, str, str]] = []
        self._pad_timer = QTimer(self, interval=16)
        self._pad_timer.timeout.connect(self._poll_pad)

    # -- geometry
    def pt(self, x, y) -> QPointF:
        return QPointF(_f(x) / 100 * self.width(), _f(y) / 100 * self.height())

    def pct(self, p: QPointF) -> tuple[float, float]:
        return (round(min(100.0, max(0.0, p.x() / max(1, self.width()) * 100)), 2),
                round(min(100.0, max(0.0, p.y() / max(1, self.height()) * 100)), 2))

    # -- selection and capture
    def select(self, c: Optional[dict]) -> None:
        self.sel = c
        self.selected.emit(c)
        self.update()

    def start_capture(self, c: dict, field: str) -> None:
        self.sel = c
        self.capture = (c, field)
        self._pending_mod = ""
        self.activateWindow()
        self.setFocus()
        self._pad_timer.start()
        self.selected.emit(c)
        self.update()

    def _end_capture(self) -> None:
        self.capture = None
        self._pending_mod = ""
        self._pad_timer.stop()
        self.update()

    def _assign(self, name: str) -> None:
        if not self.capture:
            return
        c, field = self.capture
        target = field + "_alt1" if keys.is_gamepad(name) else field
        if name and (target in c or not target.endswith("_alt1") or field in c):
            c[target] = name
        self._end_capture()
        self.edited.emit()
        self.selected.emit(c)

    def _poll_pad(self) -> None:
        for ev in self.ed.pads_poll():
            if ev[0] == "button" and ev[2]:
                self._assign(ev[1])
                return

    # -- painting
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, self.height() / 70))
        f.setBold(True)
        p.setFont(f)
        self._badges, self._handles = [], []
        gp = self.ed.gamepad_mode
        for c in self.ed.controls():
            sel = c is self.sel
            t = control_type(c)
            # points (handles) and the lines between them
            pts = templates.points(c)
            if t in ("Swipe", "Zoom") and len(pts) >= 3:
                p.setPen(QPen(QColor(255, 255, 255, 170), 2, Qt.DashLine))
                p.drawLine(self.pt(c["X1"], c["Y1"]), self.pt(c["X2"], c["Y2"]))
            if t in ("Dpad", "MOBADpad", "FreeLook", "Rotate", "MOBASkill") and "X" in c:
                r = _f(c.get("XRadius", c.get("Radius", 9)), 9) / 100 * self.width()
                p.setPen(QPen(QColor(120, 200, 255) if sel else QColor(255, 255, 255, 170), 2))
                p.setBrush(QColor(0, 0, 0, 60))
                p.drawEllipse(self.pt(c["X"], c["Y"]), r, r)
            for xf, yf, lab in pts:
                if t in ("Swipe", "Zoom") and xf == "X":
                    continue
                at = self.pt(c[xf], c[yf])
                rr = QRectF(at.x() - 7, at.y() - 7, 14, 14)
                p.setPen(QPen(QColor(120, 200, 255) if sel else QColor(255, 255, 255, 200), 2))
                p.setBrush(QColor(20, 120, 220, 200) if sel else QColor(0, 0, 0, 120))
                p.drawEllipse(rr)
                self._handles.append((rr.adjusted(-4, -4, 4, 4), c, xf, yf))
            # key badges
            for field, at in self._badge_spots(c):
                cap = self.capture and self.capture[0] is c and self.capture[1] == field
                text = "Press a key" if cap else (_binding_text(c, field, gp) or "+")
                fm = p.fontMetrics()
                w = max(fm.horizontalAdvance(text) + 14, 26)
                h = fm.height() + 6
                box = QRectF(at.x() - w / 2, at.y() - h / 2, w, h)
                p.setPen(QPen(QColor(255, 200, 0), 2) if cap else
                         QPen(QColor(120, 200, 255), 2) if sel else Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 200))
                p.drawRoundedRect(box, h / 2, h / 2)
                p.setPen(QColor(255, 255, 255))
                p.drawText(box, Qt.AlignCenter, text)
                self._badges.append((box, c, field))
        p.setPen(QColor(255, 255, 255, 200))
        p.drawText(self.rect().adjusted(8, 6, -8, -6), Qt.AlignLeft | Qt.AlignTop,
                   "Drag controls from the panel. Click a key badge, then press a key"
                   + (" or a gamepad button." if gp else ", mouse button or wheel."))
        p.end()

    def _badge_spots(self, c: dict) -> list[tuple[str, QPointF]]:
        """Where each key field's badge goes."""
        t = control_type(c)
        out = []
        if t in ("Dpad", "FreeLook", "Tilt") and "X" in c:
            ctr = self.pt(c["X"], c["Y"])
            r = _f(c.get("XRadius", 9), 9) / 100 * self.width() * 0.75
            for f, dx, dy in (("KeyUp", 0, -1), ("KeyDown", 0, 1), ("KeyLeft", -1, 0),
                              ("KeyRight", 1, 0)):
                if f in c:
                    out.append((f, QPointF(ctr.x() + dx * r, ctr.y() + dy * r)))
            for i, f in enumerate(("KeySpeedModifier1", "KeySpeedModifier2", "Key")):
                if f in c and (c.get(f) or f == "Key" and t == "FreeLook" or c is self.sel):
                    out.append((f, QPointF(ctr.x(), ctr.y() + r * 1.5 + i * 24)))
            return out
        if t == "Swipe":
            return [("Key", self.pt(c.get("X1", 50), c.get("Y1", 50)))]
        if t == "Zoom":
            mid = self.pt((_f(c.get("X1")) + _f(c.get("X2"))) / 2,
                          (_f(c.get("Y1")) + _f(c.get("Y2"))) / 2)
            return [("KeyIn", QPointF(mid.x(), mid.y() - 14)),
                    ("KeyOut", QPointF(mid.x(), mid.y() + 14))]
        if t == "Pan":
            out.append(("KeyStartStop", self.pt(c.get("X", 50), c.get("Y", 50))))
            if "LButtonX" in c:
                out.append(("KeyAction", self.pt(c["LButtonX"], c["LButtonY"]) + QPointF(0, 20)))
            if "LookAroundX" in c:
                out.append(("KeyLookAround",
                            self.pt(c["LookAroundX"], c["LookAroundY"]) + QPointF(0, 20)))
            if c is self.sel:
                out.append(("KeySuspend", self.pt(c.get("X", 50), c.get("Y", 50)) + QPointF(0, 26)))
            return out
        if t == "Rotate":
            ctr = self.pt(c.get("X", 50), c.get("Y", 50))
            return [("KeyClock", ctr + QPointF(30, 0)), ("KeyAntiClock", ctr - QPointF(30, 0))]
        if t == "MOBADpad":
            return [("KeyMove", self.pt(c.get("X", 50), c.get("Y", 50)))]
        if t == "MOBASkill":
            out = [("KeyActivate", self.pt(c.get("X", 50), c.get("Y", 50)))]
            if "CancelX" in c and c.get("IsCancelSkillEnabled", True):
                out.append(("KeyCancel", self.pt(c["CancelX"], c["CancelY"]) + QPointF(0, 20)))
            return out
        if t == "Zoom" or "Key" not in c:
            return out
        return [("Key", self.pt(c.get("X", 50), c.get("Y", 50)))]

    # -- mouse
    def mousePressEvent(self, ev) -> None:
        pos = ev.position()
        b = ev.button()
        if self.capture:
            if b != Qt.LeftButton:
                self._assign({Qt.RightButton: "MouseRButton", Qt.MiddleButton: "MouseMButton",
                              Qt.BackButton: "MouseXButton1",
                              Qt.ForwardButton: "MouseXButton2"}.get(b, ""))
                return
            for box, c, field in self._badges:     # left click on the badge itself: LMB
                if box.contains(pos) and self.capture == (c, field):
                    self._assign("MouseLButton")
                    return
            self._end_capture()
        if b != Qt.LeftButton:
            return
        for rr, c, xf, yf in reversed(self._handles):
            if rr.contains(pos):
                self.select(c)
                self._drag = (c, xf, yf)
                return
        for box, c, field in reversed(self._badges):
            if box.contains(pos):
                self.start_capture(c, field)
                return
        self.select(None)

    def mouseMoveEvent(self, ev) -> None:
        if not self._drag:
            return
        c, xf, yf = self._drag
        x, y = self.pct(ev.position())
        if xf == "X" and control_type(c) in ("Swipe", "Zoom"):
            return
        if xf in ("X", "X1") and control_type(c) not in ("Swipe", "Zoom"):
            # moving the main point drags the control's other points along
            dx, dy = x - _f(c[xf]), y - _f(c[yf])
            for a, b2, _ in templates.points(c):
                if a not in (xf, "CrosshairX"):
                    c[a] = round(min(100, max(0, _f(c[a]) + dx)), 2)
                    c[b2] = round(min(100, max(0, _f(c[b2]) + dy)), 2)
        c[xf], c[yf] = x, y
        self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if self._drag:
            self._drag = None
            self.edited.emit()
            self.selected.emit(self.sel)

    def wheelEvent(self, ev) -> None:
        if self.capture:
            self._assign("MouseWheelUp" if ev.angleDelta().y() > 0 else "MouseWheelDown")

    # -- keys
    def keyPressEvent(self, ev) -> None:
        if ev.isAutoRepeat():
            return
        name = keys.name_from_native(ev.nativeScanCode(), paths.WINDOWS)
        if self.capture:
            if ev.key() == Qt.Key_Escape:
                self._end_capture()
                return
            if ev.key() == Qt.Key_Backspace:
                c, field = self.capture
                c[field] = ""
                self._end_capture()
                self.edited.emit()
                self.selected.emit(c)
                return
            if name in keys.MODIFIERS:
                self._pending_mod = name             # alone, or the start of a combo
                return
            if not name:
                return
            mods = [m for m, q in (("Ctrl", Qt.ControlModifier), ("Shift", Qt.ShiftModifier),
                                   ("Alt", Qt.AltModifier)) if ev.modifiers() & q]
            self._assign(" + ".join(mods + [name]))
            return
        if self.sel is not None:
            if ev.key() == Qt.Key_Delete:
                self.ed.delete_control(self.sel)
                return
            step = {Qt.Key_Left: (-0.5, 0), Qt.Key_Right: (0.5, 0), Qt.Key_Up: (0, -0.5),
                    Qt.Key_Down: (0, 0.5)}.get(ev.key())
            if step:
                for a, b2, _ in templates.points(self.sel):
                    self.sel[a] = round(min(100, max(0, _f(self.sel[a]) + step[0])), 2)
                    self.sel[b2] = round(min(100, max(0, _f(self.sel[b2]) + step[1])), 2)
                self.update()
                self.edited.emit()

    def keyReleaseEvent(self, ev) -> None:
        if ev.isAutoRepeat() or not self.capture or not self._pending_mod:
            return
        if keys.name_from_native(ev.nativeScanCode(), paths.WINDOWS) == self._pending_mod:
            self._assign(self._pending_mod)          # the modifier alone

    # -- drops from the palette
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasFormat(MIME):
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:
        self.dragEnterEvent(ev)

    def dropEvent(self, ev) -> None:
        t = bytes(ev.mimeData().data(MIME)).decode()
        x, y = self.pct(ev.position())
        ev.acceptProposedAction()
        self.ed.add_control(t, x, y)


# ------------------------------------------------------------------ settings form
class PropertyForm(QWidget):
    """Every field of one control, typed from its current value."""
    changed = Signal()

    def __init__(self, c: dict, surface: EditSurface, parent=None):
        super().__init__(parent)
        self.c = c
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow(QLabel(f"<b>{control_type(c)}</b>"))
        kf = set(templates.key_fields(c))
        advanced = set(BASE_FIELDS) | {"GuidanceCategory"}
        order = [k for k in c if k not in advanced] + [k for k in c if k in advanced]
        adv_shown = False
        for k in order:
            v = c[k]
            if k in _SKIP or k.endswith("_alt1") and k[:-5] in kf:
                continue
            if k in advanced and not adv_shown:
                adv_shown = True
                form.addRow(QLabel("<i>Advanced</i>"))
            label = k
            if k in kf:
                row = QHBoxLayout()
                for fld, gp in ((k, False), (k + "_alt1", True)):
                    b = QPushButton(_binding_text(c, k, gp) or ("pad: none" if gp else "none"))
                    b.setToolTip("Click, then press a key / mouse button" +
                                 (" (gamepad button)" if gp else ""))
                    b.clicked.connect(lambda _=False, f=k: surface.start_capture(c, f))
                    row.addWidget(b)
                w = QWidget()
                w.setLayout(row)
                row.setContentsMargins(0, 0, 0, 0)
                form.addRow(label, w)
            elif isinstance(v, bool):
                w = QCheckBox()
                w.setChecked(v)
                w.toggled.connect(lambda on, key=k: self._set(key, on))
                form.addRow(label, w)
            elif isinstance(v, int):
                w = QSpinBox()
                w.setRange(-1_000_000, 1_000_000)
                w.setValue(v)
                w.valueChanged.connect(lambda val, key=k: self._set(key, int(val)))
                form.addRow(label, w)
            elif isinstance(v, float):
                w = QDoubleSpinBox()
                w.setRange(-100_000.0, 100_000.0)
                w.setDecimals(2)
                w.setValue(v)
                w.valueChanged.connect(lambda val, key=k: self._set(key, float(val)))
                form.addRow(label, w)
            elif isinstance(v, str):
                w = QLineEdit(v)
                w.editingFinished.connect(lambda key=k, e=w: self._set(key, e.text()))
                form.addRow(label, w)
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                w = QPlainTextEdit("\n".join(v))
                w.setMinimumHeight(110)
                w.textChanged.connect(lambda key=k, e=w: self._set(
                    key, [ln for ln in e.toPlainText().splitlines() if ln.strip()]))
                form.addRow(label, w)
        rm = QPushButton("Delete control")
        rm.clicked.connect(lambda: surface.ed.delete_control(c))
        form.addRow(rm)

    def _set(self, k, v) -> None:
        self.c[k] = v
        self.changed.emit()


# ------------------------------------------------------------------ palette
class PaletteButton(QToolButton):
    def __init__(self, label: str, type_name: str, tip: str, on_click: Callable, parent=None):
        super().__init__(parent)
        self.type_name = type_name
        self.setText(label)
        self.setToolTip(tip + " (drag onto the screen)")
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(44)
        self._press = None
        self.clicked.connect(lambda: on_click(type_name))

    def mousePressEvent(self, ev) -> None:
        self._press = ev.position()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        if self._press is not None and (ev.position() - self._press).manhattanLength() > 8:
            self._press = None
            self.setDown(False)
            md = QMimeData()
            md.setData(MIME, self.type_name.encode())
            drag = QDrag(self)
            drag.setMimeData(md)
            drag.exec(Qt.CopyAction)
            return
        super().mouseMoveEvent(ev)


# ------------------------------------------------------------------ the panel
class ControlsEditor(QFrame):
    """The side panel; owns the working copy of the controls file and the surface."""
    closed = Signal()

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.package = session.package
        src = session.engine.cfg
        self.cfg = ControlsFile.from_raw(copy.deepcopy(src.raw)) if src else \
            ControlsFile.new(self.package)
        self.scheme: Scheme = self.cfg.selected() or self.cfg.schemes[0]
        self.gamepad_mode = False
        self.dirty = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(300)
        self.setMaximumWidth(360)

        owner = session.display.window()
        self.surface = EditSurface(self, owner)
        self.surface.selected.connect(self._show_props)
        self.surface.edited.connect(self._mark)
        self._follow = QTimer(self, interval=150)
        self._follow.timeout.connect(self._sync_surface)

        lay = QVBoxLayout(self)
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>Controls editor</b>"))
        head.addStretch(1)
        x = QToolButton(text="✕")
        x.setAutoRaise(True)
        x.clicked.connect(self.close_editor)
        head.addWidget(x)
        lay.addLayout(head)
        lay.addWidget(QLabel(f"<small>{self.package}</small>"))

        lay.addWidget(QLabel("Control scheme"))
        srow = QHBoxLayout()
        self.cb = QComboBox()
        self.cb.currentIndexChanged.connect(self._pick_scheme)
        srow.addWidget(self.cb, 1)
        for text, tip, fn in (("+", "New scheme", self._new_scheme),
                              ("⧉", "Copy this scheme", self._copy_scheme),
                              ("✎", "Rename", self._rename_scheme),
                              ("🗑", "Delete scheme", self._delete_scheme)):
            b = QToolButton(text=text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            srow.addWidget(b)
        lay.addLayout(srow)

        mrow = QHBoxLayout()
        g = QButtonGroup(self)
        for i, text in enumerate(("Keyboard and mouse", "Gamepad")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(i == 0)
            g.addButton(b, i)
            mrow.addWidget(b)
        g.idClicked.connect(self._set_mode)
        lay.addLayout(mrow)

        lay.addWidget(QLabel("Add controls"))
        grid = QGridLayout()
        for i, (label, t, tip) in enumerate(templates.PALETTE):
            grid.addWidget(PaletteButton(label, t, tip, lambda tn: self.add_control(tn, 50, 50)),
                           i // 3, i % 3)
        lay.addLayout(grid)

        lay.addWidget(QLabel("Selected control"))
        self.props = QScrollArea()
        self.props.setWidgetResizable(True)
        self.props.setMinimumHeight(160)
        lay.addWidget(self.props, 1)
        self._show_props(None)

        brow = QHBoxLayout()
        for text, fn in (("Import", self._import), ("Export", self._export)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            brow.addWidget(b)
        lay.addLayout(brow)
        brow = QHBoxLayout()
        b = QPushButton("Reset")
        b.setToolTip("Discard unsaved changes")
        b.clicked.connect(self._reset)
        brow.addWidget(b)
        self.save_btn = QPushButton("Save")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save)
        brow.addWidget(self.save_btn)
        lay.addLayout(brow)

        self._fill_schemes()
        self._follow.start()
        self._sync_surface()

    # -- helpers used by the surface
    def controls(self) -> list[dict]:
        return self.scheme.controls if self.scheme else []

    def pads_poll(self) -> list:
        return self.session.engine.pads.poll()

    def _mark(self) -> None:
        self.dirty = True
        self.save_btn.setText("Save *")
        self.surface.update()

    def add_control(self, type_name: str, x: float, y: float) -> None:
        c = templates.make(type_name, x, y)
        self.controls().append(c)
        self._mark()
        kf = templates.key_fields(c)
        first = next((f for f in kf if not c.get(f)), kf[0] if kf else None)
        if first:
            self.surface.start_capture(c, first)
        else:
            self.surface.select(c)

    def delete_control(self, c: dict) -> None:
        lst = self.controls()
        if any(x is c for x in lst):
            lst[:] = [x for x in lst if x is not c]
            self.surface.select(None)
            self._mark()

    # -- surface placement
    def _sync_surface(self) -> None:
        r = self.session._screen_rect()
        if r is None:
            self.surface.hide()
            return
        if self.surface.geometry() != r:
            self.surface.setGeometry(r)
        if not self.surface.isVisible():
            self.surface.show()
            self.surface.raise_()

    # -- settings pane
    def _show_props(self, c) -> None:
        if c is None:
            w = QLabel("Click a control on the screen to edit it, or drag one in from above.")
            w.setWordWrap(True)
            w.setAlignment(Qt.AlignTop)
        else:
            w = PropertyForm(c, self.surface)
            w.changed.connect(self._mark)
        self.props.setWidget(w)

    def _set_mode(self, i: int) -> None:
        self.gamepad_mode = i == 1
        self.surface.update()

    # -- schemes
    def _fill_schemes(self) -> None:
        self.cb.blockSignals(True)
        self.cb.clear()
        for s in self.cfg.schemes:
            self.cb.addItem(s.name + ("  (smart)" if s.smart else ""))
        self.cb.setCurrentIndex(max(0, next((i for i, s in enumerate(self.cfg.schemes)
                                             if s is self.scheme), 0)))
        self.cb.blockSignals(False)

    def _pick_scheme(self, i: int) -> None:
        if 0 <= i < len(self.cfg.schemes):
            self.scheme = self.cfg.schemes[i]
            self.cfg.select(self.scheme.name)
            self.surface.select(None)
            self._mark()

    def _unique(self, base: str) -> str:
        names = {s.name for s in self.cfg.schemes}
        n, i = base, 2
        while n in names:
            n, i = f"{base} {i}", i + 1
        return n

    def _add_scheme(self, raw: dict) -> None:
        s = Scheme(raw)
        self.cfg.schemes.append(s)
        self.scheme = s
        self.cfg.select(s.name)
        self._fill_schemes()
        self.surface.select(None)
        self._mark()

    def _new_scheme(self) -> None:
        name, ok = QInputDialog.getText(self, "New scheme", "Name:", text=self._unique("Custom"))
        if ok and name.strip():
            self._add_scheme({"Name": self._unique(name.strip()), "BuiltIn": False,
                              "Selected": True, "IsBookMarked": False,
                              "IsCategoryVisible": True, "KeyboardLayout": "",
                              "GameControls": [], "Images": []})

    def _copy_scheme(self) -> None:
        raw = copy.deepcopy(self.scheme.raw)
        raw["Name"] = self._unique(self.scheme.name + " copy")
        raw["BuiltIn"] = False
        self._add_scheme(raw)

    def _rename_scheme(self) -> None:
        name, ok = QInputDialog.getText(self, "Rename scheme", "Name:", text=self.scheme.name)
        if ok and name.strip() and name.strip() != self.scheme.name:
            self.scheme.raw["Name"] = self._unique(name.strip())
            self._fill_schemes()
            self._mark()

    def _delete_scheme(self) -> None:
        if len(self.cfg.schemes) < 2:
            QMessageBox.information(self, "Delete scheme", "A game needs at least one scheme.")
            return
        self.cfg.schemes = [s for s in self.cfg.schemes if s is not self.scheme]
        self.scheme = self.cfg.schemes[0]
        self.cfg.select(self.scheme.name)
        self._fill_schemes()
        self.surface.select(None)
        self._mark()

    # -- files
    def _import(self) -> None:
        fn, _ = QFileDialog.getOpenFileName(self, "Import controls", "",
                                            "BlueStacks controls (*.cfg);;All files (*)")
        if not fn:
            return
        try:
            other = ControlsFile.load(Path(fn))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import controls", f"Could not read {fn}:\n{e}")
            return
        for s in other.schemes:
            raw = copy.deepcopy(s.raw)
            raw["Name"] = self._unique(s.name)
            raw["Selected"] = False
            self.cfg.schemes.append(Scheme(raw))
        self.scheme = self.cfg.schemes[-1]
        self.cfg.select(self.scheme.name)
        self._fill_schemes()
        self._mark()

    def _export(self) -> None:
        fn, _ = QFileDialog.getSaveFileName(self, "Export controls", f"{self.package}.cfg",
                                            "BlueStacks controls (*.cfg)")
        if fn:
            ControlsFile.from_raw(copy.deepcopy(self.cfg.raw) | {
                "ControlSchemes": [copy.deepcopy(s.raw) for s in self.cfg.schemes]}).save(Path(fn))

    def _reset(self) -> None:
        src = self.session.engine.cfg
        self.cfg = ControlsFile.from_raw(copy.deepcopy(src.raw)) if src else \
            ControlsFile.new(self.package)
        self.scheme = self.cfg.selected() or self.cfg.schemes[0]
        self._fill_schemes()
        self.surface.select(None)
        self.dirty = False
        self.save_btn.setText("Save")
        self.surface.update()

    def save(self) -> None:
        path = self.cfg.save(store.ours(self.package))
        self.dirty = False
        self.save_btn.setText("Save")
        self.session.status.emit(f"game controls: saved {path.name}")
        self.session.reload(keep_disabled=True)

    def close_editor(self) -> None:
        if self.dirty:
            r = QMessageBox.question(self, "Controls editor", "Save your changes?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel:
                return
            if r == QMessageBox.Save:
                self.save()
        self._follow.stop()
        self.surface.hide()
        self.surface.deleteLater()
        self.closed.emit()
