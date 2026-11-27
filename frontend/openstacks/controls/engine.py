"""Game controls runtime core: dispatch, touch contacts, timing (BlueStacks' "imap").

Follows HD-Player.exe as documented in docs/re/kmm_controls.md (section numbers below
refer to it). QEMU forwards the keys and buttons the active scheme maps (channel.py); this
engine runs the scheme's controls (runtime.py) and sends touch contacts back.

- **Positions** are percent of the view as shown, 0..100 (§1). The wire carries 0..32767,
  and QEMU applies the rotation.
- **Timing:** one tick every 16 ms (§3.0). Speeds are percent of the view per second,
  applied as `Speed * 16 / 1000` per tick, as HD-Player does.
- **Key dispatch (§0.3):** every enabled control sees every event for a key that any
  enabled control maps. Such a key is not forwarded to Android (QEMU swallows it) unless
  a SendOriginalKeys control re-sends it.
- **Combos:** a single-key binding is suppressed while a partner key of a longer combo
  that uses it is held (§0.3).
- **Mouse (§3.0.1):**
  - Right, middle and side buttons and the wheel are also dispatched as keys (MouseRButton,
    MouseWheelUp, ...).
  - The left button stays Android's own touch (contact 0) unless a control needs it, e.g.
    an aiming Pan or an active MOBA skill.
"""
from __future__ import annotations

import math
import os
import sys
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from . import expr, imgd, keys
from .bscfg import ControlsFile, Scheme, control_type
from .channel import InputChannel
from .gamepad import Gamepads

ABS_MAX = 32767
TICK_MS = 16                         # HD-Player's tick (§3.0)
STEP = TICK_MS / 1000.0              # "Speed * 16 / 1000"
DEBUG = bool(os.environ.get("OST_CONTROLS_DEBUG"))
ALL_KEY_CODES = tuple(range(1, 248))  # DontForwardKeys: every keyboard key


def now() -> float:
    return time.monotonic()


class Engine(QObject):
    status = Signal(str)
    text = Signal(str)               # Script `text`: the player types it through adb
    changed = Signal()               # controls loaded / aim mode toggled (overlay redraw)

    def __init__(self, port: int, parent=None):
        super().__init__(parent)
        self.ch = InputChannel(port, self)
        self.ch.line.connect(self._on_line)
        self.ch.connected.connect(self._on_connected)
        self.ch.disconnected.connect(self.release_all)
        self.pads = Gamepads()
        self.cfg: Optional[ControlsFile] = None
        self.scheme: Optional[Scheme] = None
        self.controls: list = []
        self.unsupported: dict[str, int] = {}
        self.held: set[str] = set()
        self.states: dict[str, bool] = {}
        self.enabled = True
        self.unknown_is_true = True  # image conditions while detection is unavailable
        # on-screen image detection ("Smart" schemes, docs/re/kmm_controls.md 3.20)
        self.package = ""            # the game in front (picks the sampler, imgd.PACKAGES)
        self.detecting = False       # QEMU's renderer matches this scheme's images
        self.tags: set[str] = set()  # image ids matched in the last frame
        self._img_gen = 0
        self._img_ids: list[str] = []
        # geometry: the view in host pixels, the guest framebuffer as currently oriented
        self.view_px = (1600, 900)
        self.fb = (1600, 900)
        self.cursor = (50.0, 50.0)   # last pointer position, percent of the view
        self.native_touch = True     # False while a MOBA skill is active (§3.8)
        self.active_skill = None     # the MOBASkill currently casting / aiming
        self.last_skill_press = 0.0
        self.mobapad = None          # the MOBADpad currently walking
        self.exclusive: set = set()  # controls holding an Exclusive touch (§1.1)
        self.zooming = False         # a MouseZoom is in progress (suppresses Scroll)
        self._partners: dict[str, set[str]] = {}
        self._free = list(range(15, 0, -1))
        self._pos: dict[int, tuple[int, int]] = {}
        self._focused = True
        self.orient = 0                  # guest display rotation (QEMU "O" line)
        self._native_mouse: Optional[int] = None   # guest uinput mouse flag, None = none
        self._lock = False
        self._caps = None
        self._timer = QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.ch.start()
        self._timer.start()

    def stop(self) -> None:
        self.release_all()
        self.ch.flush()
        self._timer.stop()
        self.ch.stop()
        self.pads.close()

    def set_geometry(self, view_w: int, view_h: int, fb_w: int, fb_h: int) -> None:
        """The view's size in host pixels and the guest framebuffer as shown (rotated)."""
        if view_w > 0 and view_h > 0:
            self.view_px = (view_w, view_h)
        if fb_w > 0 and fb_h > 0:
            self.fb = (fb_w, fb_h)

    @property
    def aspect(self) -> float:
        """Width / height of the screen as shown (HD-Player: fbW / fbH, swapped when
        rotated)."""
        return self.fb[0] / self.fb[1]

    def load(self, cfg: Optional[ControlsFile], scheme: Optional[str] = None) -> None:
        from .runtime import TYPES
        self.release_all()
        self.cfg = cfg
        self.scheme = (cfg.scheme(scheme) if cfg and scheme else None) or \
            (cfg.selected() if cfg else None)
        self.controls, self.unsupported = [], {}
        self.states = {}
        for raw in (self.scheme.controls if self.scheme else []):
            t = control_type(raw)
            cls = TYPES.get(t)
            if cls is None:
                self.unsupported[t] = self.unsupported.get(t, 0) + 1
                continue
            try:
                self.controls.append(cls(self, raw))
            except (TypeError, ValueError, KeyError) as e:
                self.status.emit(f"game controls: skipped a {t} ({e})")
        self._partners = {}
        for c in self.controls:
            for combo in c.all_combos():
                if len(combo) > 1:
                    for k in combo:
                        self._partners.setdefault(k, set()).update(x for x in combo if x != k)
        self.send_images()
        self.refresh_enabled()
        self.update_capture()
        self.changed.emit()

    def set_enabled(self, on: bool) -> None:
        if on != self.enabled:
            self.enabled = on
            self.release_all()
            self.update_capture()
            self.changed.emit()

    # ------------------------------------------------------------------ conditions (§3.18)
    def lookup(self, name: str):
        if name in self.states:
            return self.states[name]
        if self.detecting:
            return name in self.tags # images are variables while on screen (imapSetTags)
        return None                  # an image tag: unknown without screen detection

    def tag_lookup(self, name: str):
        """StartCondition identifiers: the image tags only (imapSetTags)."""
        return (name in self.tags) if self.detecting else None

    # ------------------------------------------------------------------ images (§3.20)
    def send_images(self) -> None:
        """Hand the scheme's Images to QEMU's renderer (or turn detection off)."""
        self._img_gen += 1
        self.detecting = False
        self.tags = set()
        if not self.ch.is_open:
            self._img_ids = []
            return
        lines, self._img_ids = imgd.config_lines(
            self._img_gen, self.package, self.scheme.images if self.scheme else [])
        for line in lines:
            self.ch.send(line)
        self.ch.flush()

    def _on_images_ready(self, gen: int, ok: bool) -> None:
        if gen != self._img_gen:
            return
        self.detecting = ok and bool(self._img_ids)
        if self._img_ids:
            self.status.emit("game controls: on-screen detection " + (
                f"on ({len(self._img_ids)} images)" if self.detecting else
                "unavailable (needs the gfxstream renderer); smart conditions ignored"))
        self._apply_tags()

    def _on_tags(self, gen: int, idx: list[int]) -> None:
        if gen != self._img_gen or not self.detecting:
            return
        tags = {self._img_ids[i] for i in idx if 0 <= i < len(self._img_ids)}
        if tags != self.tags:
            self.tags = tags
            self._apply_tags()

    def _apply_tags(self) -> None:
        """imapSetTags: StartConditions (tags only), then every EnableCondition."""
        for c in self.controls:
            sc = c.raw.get("StartCondition", "")
            if sc:
                v = expr.evaluate(sc, self.tag_lookup)
                c.on_start_condition(bool(v))
        self.refresh_enabled()
        self.update_capture()
        self.changed.emit()

    def condition(self, src: str) -> bool:
        v = expr.evaluate(src, self.lookup)
        return self.unknown_is_true if v is None else v

    def refresh_enabled(self) -> None:
        """Re-evaluate every EnableCondition (after a State change or a load)."""
        for c in self.controls:
            c.set_active(self.condition(c.raw.get("EnableCondition", "")))

    # ------------------------------------------------------------------ capture
    def update_capture(self) -> None:
        if not self.ch.is_open:
            return
        live = [c for c in self.controls if c.active] if self.enabled else []
        names: set[str] = set()
        mask = 0
        hover = lock = False
        all_keys = False
        for c in live:
            names |= c.bound_names()
            mask |= c.mouse_buttons()
            hover |= c.wants_hover()
            lock |= c.wants_lock()
            all_keys |= c.captures_all_keys()
        codes = set(ALL_KEY_CODES) if all_keys else set()
        for n in names:
            codes.update(keys.evdev_codes(n))
            if n in keys.MOUSE_BUTTONS and n != "MouseLButton":
                mask |= keys.MOUSE_BUTTONS[n]
            if n in keys.MOUSE_WHEEL:
                mask |= keys.OST_BTN_WHEEL
        if hover:
            mask |= keys.OST_BTN_HOVER
        lock = lock and self._focused
        caps = (tuple(sorted(codes)), mask, lock)
        if caps == self._caps:
            return
        self._caps = caps
        self.ch.send("cap " + " ".join(map(str, caps[0])))
        self.ch.send(f"btn {mask}")
        self.ch.send(f"lock {1 if lock else 0}")
        self.ch.flush()
        self.changed.emit()

    def _on_connected(self) -> None:
        self._caps = None
        self._native_mouse = None
        self.status.emit("game controls: connected")
        self.send_images()
        self.refresh_enabled()
        self.update_capture()

    # ------------------------------------------------------------------ touches
    def touch_down(self, x: float, y: float) -> int:
        if not self._free:
            return 0
        cid = self._free.pop()
        self.touch_move(cid, x, y)
        return cid

    def touch_move(self, cid: int, x: float, y: float) -> None:
        if not cid:
            return
        vx = int(max(0.0, min(100.0, x)) * ABS_MAX / 100)
        vy = int(max(0.0, min(100.0, y)) * ABS_MAX / 100)
        if self._pos.get(cid) == (vx, vy):
            return
        self._pos[cid] = (vx, vy)
        self.ch.send(f"t {cid} {vx} {vy}")

    def touch_up(self, cid: int) -> None:
        if not cid:
            return
        self.ch.send(f"u {cid}")
        self._pos.pop(cid, None)
        if cid not in self._free:
            self._free.append(cid)

    def release_all(self) -> None:
        for c in self.controls:
            c.reset()
        self.held.clear()
        self._free = list(range(15, 0, -1))
        self._pos.clear()
        self.exclusive.clear()
        self.native_touch = True
        self.active_skill = None
        self.mobapad = None
        self.ch.send("r")
        self.ch.flush()

    # ------------------------------------------------------------------ to the guest
    def send_tilt(self, ax: float, ay: float) -> None:
        """Gravity for tilt angles in degrees (HD-Player ImapRtTilt send, 0x1404a68d0):
        (sin ax, sin ay, cos ax cos ay), x/y turned for the display orientation
        (0x140306d20), sent as -g * 1e6 (the guest reports -v / 1e6 * 9.81 m/s^2)."""
        rx, ry = math.radians(ax), math.radians(ay)
        x, y, z = math.sin(rx), math.sin(ry), math.cos(rx) * math.cos(ry)
        x, y = {0: (-x, -y), 1: (-y, x), 2: (x, y), 3: (y, -x)}[self.orient]
        self.ch.send(f"a {int(x * -1e6)} {int(y * -1e6)} {int(z * -1e6)}")

    def native_mouse(self, flag: int) -> None:
        """Create the guest's uinput mouse (gcallEnableNativeMouseRpc(1, flag)) once per
        connection: Android takes a moment to add a new device, and events sent to one
        being re-created are lost. flag: 0 in HD-Player's relative (native pan) path."""
        if self._native_mouse != flag:
            if self._native_mouse is not None:
                self.ch.send("g 1016 0 0")
            self.ch.send(f"g 1016 1 {flag}")
            self._native_mouse = flag

    def send_key_guest(self, name: str, down: bool) -> None:
        for code in keys.evdev_codes(name)[:1]:
            self.ch.send(f"k {code} {1 if down else 0}")

    # ------------------------------------------------------------------ dispatch (§0.3)
    def suppressed(self, name: str) -> bool:
        """A single-key binding of `name` is shadowed while a combo partner is held."""
        return any(p in self.held for p in self._partners.get(name, ()))

    def is_mapped(self, name: str) -> bool:
        return any(c.active and name in c.bound_names() for c in self.controls) or \
            any(c.active and c.captures_all_keys() for c in self.controls)

    def dispatch(self, name: str, down: bool, forward_unmapped: bool = False) -> bool:
        """A key event: every active control gets it. Returns True when it was mapped."""
        if down:
            if name in self.held:
                return True          # OS auto-repeat: HD-Player re-sets positions; we skip
            self.held.add(name)
        else:
            self.held.discard(name)
        if not self.enabled:
            return False
        if not self.is_mapped(name):
            if forward_unmapped:
                self.send_key_guest(name, down)
            return False
        for c in list(self.controls):
            if c.active:
                c.on_key(name, down)
        return True

    def inject_key(self, name: str, down: bool) -> None:
        """Script keydown/keyup, Alias: a host key event into the dispatcher."""
        self.dispatch(name, down, forward_unmapped=True)

    def mouse(self, ev: str, **data) -> None:
        """A mouse event for every active control: ev = move | rel | ldown | lup |
        down | up (other buttons, with `button`) | wheel (with `delta`)."""
        for c in list(self.controls):
            if c.active:
                c.on_mouse(ev, data)

    def inject_wheel(self, x: float, y: float, notches: int) -> None:
        self.cursor = (x, y)
        self.mouse("wheel", delta=notches * 120)
        n = "MouseWheelUp" if notches >= 0 else "MouseWheelDown"
        self.dispatch(n, True)
        self.dispatch(n, False)

    def pan(self):
        from .runtime import Pan
        return next((c for c in self.controls if isinstance(c, Pan) and c.active), None)

    # ------------------------------------------------------------------ QEMU events
    def _pct(self, v: str) -> float:
        return int(v) * 100.0 / ABS_MAX

    def _on_line(self, line: str) -> None:
        if DEBUG:
            print(f"[controls] <- {line}", file=sys.stderr, flush=True)
        p = line.split()
        kind = p[0]
        try:
            if kind == "K":
                for n in keys.names_for_code(int(p[1])):
                    self.dispatch(n, p[2] == "1")
            elif kind == "B":
                bit, down = int(p[1]), p[2] == "1"
                if len(p) >= 5:
                    self.cursor = (self._pct(p[3]), self._pct(p[4]))
                if bit == 1:
                    self.mouse("ldown" if down else "lup")
                else:
                    self.mouse("down" if down else "up", button=bit)
                    for n, b in keys.MOUSE_BUTTONS.items():
                        if b == bit:
                            self.dispatch(n, down)
            elif kind == "W":
                if len(p) >= 4:
                    self.cursor = (self._pct(p[2]), self._pct(p[3]))
                delta = int(p[1])
                self.mouse("wheel", delta=delta)
                n = "MouseWheelUp" if delta >= 0 else "MouseWheelDown"
                self.dispatch(n, True)
                self.dispatch(n, False)
            elif kind == "M":
                self.cursor = (self._pct(p[1]), self._pct(p[2]))
                self.mouse("move")
            elif kind == "R":
                self.mouse("rel", dx=int(p[1]), dy=int(p[2]))
            elif kind == "F":
                self._focused = p[1] == "1"
                if not self._focused:
                    self.release_all()
                self._caps = None
                self.update_capture()
            elif kind == "O":
                self.orient = int(p[1]) & 3
                self.changed.emit()
            elif kind == "I":
                self._on_images_ready(int(p[1]), p[2] == "1")
            elif kind == "T":
                self._on_tags(int(p[1]), [int(x) for x in p[2:]])
        except (IndexError, ValueError):
            return
        self.update_capture()
        self.ch.flush()

    def _tick(self) -> None:
        t = now()
        if self.enabled and self._focused:
            for ev in self.pads.poll():
                if ev[0] == "button":
                    self.dispatch(ev[1], ev[2])
                else:
                    for c in list(self.controls):
                        if c.active:
                            c.on_stick(ev[1], ev[2], ev[3])
        for c in list(self.controls):
            if c.active:
                c.tick(t)
        self.update_capture()
        self.ch.flush()
