"""Game controls runtime: turns keyboard, mouse and gamepad input into touches.

QEMU forwards the keys and buttons the active scheme binds (see channel.py). This engine
runs the scheme's controls and sends touch contacts back.
- Positions are BlueStacks percentages of the view as displayed (X, Y in 0..100). The
  wire carries 0..32767, and QEMU applies the rotation.
- Contacts 1..15 are ours; contact 0 is the host pointer.

Control types run here (milestone 1): Tap, TapRepeat, Dpad, Swipe, Zoom, Pan (aim mode
with pointer lock), Script and State. Other types load but do nothing yet; see
docs/keymapping_plan.md.

Behaviour constants marked "approx" follow how BlueStacks feels in use. They are still to
be checked against HD-Player.exe.
"""
from __future__ import annotations

import math
import os
import re
import sys
import time
from typing import Callable, Iterator, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from . import expr, keys
from .bscfg import ControlsFile, Scheme, control_type
from .channel import InputChannel
from .gamepad import Gamepads

ABS_MAX = 32767
DEBUG = bool(os.environ.get("OST_CONTROLS_DEBUG"))
TICK_MS = 4                          # 250 Hz: animations, scripts, gamepad polling


def _now() -> float:
    return time.monotonic()


def _f(c: dict, k: str, default: float = 0.0) -> float:
    try:
        return float(c.get(k, default))
    except (TypeError, ValueError):
        return default


class Binding:
    """One key binding such as 'Ctrl + Q' or 'GamepadA': a key plus required modifiers."""
    __slots__ = ("key", "mods")

    def __init__(self, spec: str):
        parts = keys.split_combo(spec)
        self.key = parts[-1] if parts else ""
        self.mods = tuple(parts[:-1])

    def __bool__(self) -> bool:
        return bool(self.key)

    def matches(self, name: str, held: set[str]) -> bool:
        # modifiers count when the key goes down; a release matches the key alone (the
        # modifier may already be up)
        return name == self.key and (name not in held or all(m in held for m in self.mods))


# ---------------------------------------------------------------- base control
class Control:
    """A loaded control. Subclasses override the input hooks they care about."""
    kind = "?"
    key_fields: tuple[str, ...] = ("Key",)
    hover = False                    # wants pointer positions with no button held

    def __init__(self, eng: "Engine", raw: dict):
        self.e = eng
        self.raw = raw
        self.binds: dict[str, list[Binding]] = {}
        for f in self.key_fields:
            bs = [Binding(raw.get(f, "")), Binding(raw.get(f + "_alt1", ""))]
            self.binds[f] = [b for b in bs if b]

    # -- helpers
    def bound(self, field: str, name: str) -> bool:
        return any(b.matches(name, self.e.held) for b in self.binds.get(field, ()))

    def bound_names(self) -> set[str]:
        out = set()
        for bs in self.binds.values():
            for b in bs:
                out.add(b.key)
                out.update(b.mods)
        return out

    def xy(self, fx: str = "X", fy: str = "Y") -> tuple[float, float]:
        return _f(self.raw, fx), _f(self.raw, fy)

    def enabled(self) -> bool:
        return self.e.condition(self.raw.get("EnableCondition", ""))

    # -- input hooks
    def key(self, name: str, down: bool) -> bool:
        """A bound key changed; return True when this control used it."""
        return False

    def stick(self, stick: str, x: float, y: float) -> None:
        pass

    def rel(self, dx: int, dy: int) -> bool:
        return False

    def tick(self, now: float) -> None:
        pass

    def release(self) -> None:
        """Lift everything (focus lost, scheme switched, controls disabled)."""


class Unsupported(Control):
    key_fields = ()


# ---------------------------------------------------------------- Tap / TapRepeat
class Tap(Control):
    """Hold the key = hold a finger on X, Y."""
    kind = "Tap"

    def __init__(self, eng, raw):
        super().__init__(eng, raw)
        self.cid: Optional[int] = None

    def key(self, name, down):
        if not self.bound("Key", name):
            return False
        if down and self.cid is None and self.enabled():
            self.cid = self.e.touch_down(*self.xy())
        elif not down and self.cid is not None:
            self.e.touch_up(self.cid)
            self.cid = None
        return True

    def release(self):
        if self.cid is not None:
            self.e.touch_up(self.cid)
            self.cid = None


class TapRepeat(Control):
    """Taps X, Y `Count` times per second (approx). With RepeatUntilKeyUp it taps until
    the key is released; otherwise `Count` taps per press."""
    kind = "TapRepeat"

    def __init__(self, eng, raw):
        super().__init__(eng, raw)
        self.held = False
        self.left = 0
        self.next = 0.0
        self.cid: Optional[int] = None
        self.up_at = 0.0

    def _period(self) -> float:
        count = max(1.0, _f(self.raw, "Count", 5))
        return max(0.03, 1.0 / count) if self.raw.get("RepeatUntilKeyUp", True) else \
            max(0.03, _f(self.raw, "Delay", 100) / 1000.0)

    def key(self, name, down):
        if not self.bound("Key", name):
            return False
        self.held = down
        if down and self.enabled():
            self.left = -1 if self.raw.get("RepeatUntilKeyUp", True) else \
                max(1, int(_f(self.raw, "Count", 5)))
            self.next = _now()
        return True

    def tick(self, now):
        if self.cid is not None and now >= self.up_at:
            self.e.touch_up(self.cid)
            self.cid = None
        if self.left == 0 or now < self.next or self.cid is not None:
            return
        if self.left < 0 and not self.held:
            self.left = 0
            return
        p = self._period()
        self.cid = self.e.touch_down(*self.xy())
        self.up_at = now + min(0.03, p / 2)
        self.next = now + p
        if self.left > 0:
            self.left -= 1

    def release(self):
        self.left = 0
        self.held = False
        if self.cid is not None:
            self.e.touch_up(self.cid)
            self.cid = None


# ---------------------------------------------------------------- Dpad
class Dpad(Control):
    """Movement joystick: a finger goes down at X, Y and is held XRadius away in the
    direction of the pressed keys (or where the gamepad stick points). The speed
    modifiers switch to XRadius1 / XRadius2 (walk / sprint)."""
    kind = "Dpad"
    key_fields = ("KeyUp", "KeyDown", "KeyLeft", "KeyRight", "KeySpeedModifier1",
                  "KeySpeedModifier2")

    def __init__(self, eng, raw):
        super().__init__(eng, raw)
        self.dirs = {"KeyUp": False, "KeyDown": False, "KeyLeft": False, "KeyRight": False}
        self.mod = 0
        self.stick_xy = (0.0, 0.0)
        self.cid: Optional[int] = None
        self.down_at = 0.0

    def key(self, name, down):
        used = False
        for f in self.dirs:
            if self.bound(f, name):
                self.dirs[f] = down
                used = True
        for i, f in ((1, "KeySpeedModifier1"), (2, "KeySpeedModifier2")):
            if self.bound(f, name):
                self.mod = i if down else (0 if self.mod == i else self.mod)
                used = True
        return used

    def stick(self, stick, x, y):
        if stick == self.raw.get("GamepadStick"):
            self.stick_xy = (x, y)

    def _vector(self) -> tuple[float, float]:
        sx, sy = self.stick_xy
        if sx or sy:
            return sx, sy
        x = (1 if self.dirs["KeyRight"] else 0) - (1 if self.dirs["KeyLeft"] else 0)
        y = (1 if self.dirs["KeyDown"] else 0) - (1 if self.dirs["KeyUp"] else 0)
        if x and y:
            x, y = x * math.sqrt(0.5), y * math.sqrt(0.5)
        return float(x), float(y)

    def tick(self, now):
        vx, vy = self._vector()
        active = (vx or vy) and self.enabled()
        if not active:
            if self.cid is not None:
                self.e.touch_up(self.cid)
                self.cid = None
            return
        cx, cy = self.xy()
        if self.cid is None:
            self.cid = self.e.touch_down(cx, cy)
            self.down_at = now
            return
        # a short pause at the centre so the game registers the press before the drag
        # (ActivationTime, ms)
        if now - self.down_at < _f(self.raw, "ActivationTime", 50) / 1000.0:
            return
        r = _f(self.raw, {0: "XRadius", 1: "XRadius1", 2: "XRadius2"}[self.mod],
               _f(self.raw, "XRadius", 9)) or _f(self.raw, "XRadius", 9)
        ry = r * self.e.aspect                       # round on screen, not in percent
        self.e.touch_move(self.cid, cx + vx * r, cy + vy * ry)

    def release(self):
        for f in self.dirs:
            self.dirs[f] = False
        self.stick_xy = (0.0, 0.0)
        if self.cid is not None:
            self.e.touch_up(self.cid)
            self.cid = None


# ---------------------------------------------------------------- Swipe
class Swipe(Control):
    """A press swipes from X1, Y1 to X2, Y2 at `Speed` (percent of the view per second,
    approx). With Hold the finger stays at the end while the key is held. With KeyUpStop
    a release mid-swipe lifts it."""
    kind = "Swipe"

    def __init__(self, eng, raw):
        super().__init__(eng, raw)
        self.cid: Optional[int] = None
        self.t0 = 0.0
        self.dur = 0.0
        self.held = False

    def key(self, name, down):
        if not self.bound("Key", name):
            return False
        self.held = down
        if down and self.cid is None and self.enabled():
            x1, y1 = self.xy("X1", "Y1")
            x2, y2 = self.xy("X2", "Y2")
            dist = math.hypot(x2 - x1, y2 - y1)
            speed = max(1.0, _f(self.raw, "Speed", 100))
            self.dur = max(0.05, dist / speed)                    # approx
            self.t0 = _now()
            self.cid = self.e.touch_down(x1, y1)
        elif not down and self.cid is not None and self.raw.get("KeyUpStop"):
            self.release()
        return True

    def tick(self, now):
        if self.cid is None:
            return
        t = (now - self.t0) / self.dur
        x1, y1 = self.xy("X1", "Y1")
        x2, y2 = self.xy("X2", "Y2")
        if t < 1.0:
            self.e.touch_move(self.cid, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            return
        self.e.touch_move(self.cid, x2, y2)
        if not (self.raw.get("Hold") and self.held):
            self.release()

    def release(self):
        if self.cid is not None:
            self.e.touch_up(self.cid)
            self.cid = None


# ---------------------------------------------------------------- Zoom
class Zoom(Control):
    """Pinch with two fingers. KeyIn spreads them apart from (X1, Y1)-(X2, Y2) and KeyOut
    brings them together, for as long as the key is held. With KeyModifier (Ctrl by
    default in BlueStacks), the mouse wheel does one step per notch."""
    kind = "Zoom"
    key_fields = ("KeyIn", "KeyOut", "KeyModifier")

    def __init__(self, eng, raw):
        super().__init__(eng, raw)
        self.dir = 0                 # +1 in, -1 out
        self.ids: list[int] = []
        self.t0 = 0.0
        self.until = 0.0             # wheel step end

    def _wheel_bound(self) -> bool:
        return bool(self.raw.get("KeyWheel", True)) and bool(self.binds.get("KeyModifier"))

    def bound_names(self):
        out = super().bound_names()
        if self._wheel_bound():
            out.update(keys.MOUSE_WHEEL)
        return out

    def key(self, name, down):
        if self.bound("KeyIn", name) or self.bound("KeyOut", name):
            d = 1 if self.bound("KeyIn", name) else -1
            if down and self.enabled():
                self._start(d, hold=True)
            elif not down and self.dir == d:
                self._stop()
            return True
        if name in keys.MOUSE_WHEEL and self._wheel_bound() and down and \
                any(m in self.e.held for b in self.binds["KeyModifier"] for m in [b.key]):
            self._start(1 if name == "MouseWheelUp" else -1, hold=False)
            return True
        return self.bound("KeyModifier", name)

    def _start(self, d, hold):
        self._stop()
        self.dir = d
        self.t0 = _now()
        self.until = 0.0 if hold else self.t0 + 0.12
        x1, y1 = self.xy("X1", "Y1")
        x2, y2 = self.xy("X2", "Y2")
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if d < 0:                                # out: start apart, move together
            self.ids = [self.e.touch_down(x1, y1), self.e.touch_down(x2, y2)]
        else:                                    # in: start near the centre
            self.ids = [self.e.touch_down(cx + (x1 - cx) * .2, cy + (y1 - cy) * .2),
                        self.e.touch_down(cx + (x2 - cx) * .2, cy + (y2 - cy) * .2)]

    def tick(self, now):
        if not self.ids:
            return
        speed = max(1.0, _f(self.raw, "Speed", 50)) / 100.0      # approx
        t = min(1.0, (now - self.t0) * speed * 2)
        x1, y1 = self.xy("X1", "Y1")
        x2, y2 = self.xy("X2", "Y2")
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        k = 0.2 + 0.8 * t if self.dir > 0 else 1.0 - 0.8 * t
        self.e.touch_move(self.ids[0], cx + (x1 - cx) * k, cy + (y1 - cy) * k)
        self.e.touch_move(self.ids[1], cx + (x2 - cx) * k, cy + (y2 - cy) * k)
        if (self.until and now >= self.until) or t >= 1.0:
            self._stop()

    def _stop(self):
        for i in self.ids:
            self.e.touch_up(i)
        self.ids = []
        self.dir = 0

    def release(self):
        self._stop()


# ---------------------------------------------------------------- Pan (aim mode)
class Pan(Control):
    """'Aim, pan and shoot'. KeyStartStop toggles aim mode:
    - The cursor is locked and hidden, and a finger at X, Y drags with the mouse
      (Sensitivity, SensitivityRatioY).
    - Near the edge of its area the finger is lifted and put back at X, Y.
    - KeyAction (left button) holds a finger on LButtonX/Y to shoot.
    - KeySuspend frees the cursor while held.
    - KeyLookAround drags a second finger from LookAroundX/Y.
    - The right stick (GamepadStick) pans continuously at GamepadSensitivity."""
    kind = "Pan"
    key_fields = ("KeyStartStop", "KeySuspend", "KeyLookAround", "KeyAction")
    PX_TO_PCT = 0.035                # mouse pixels -> percent of the view at sensitivity 1 (approx)
    REACH = 12.0                     # how far (percent) the finger travels before re-centring

    def __init__(self, eng, raw):
        super().__init__(eng, raw)
        self.on = False
        self.suspended = False
        self.cid: Optional[int] = None
        self.pos = (0.0, 0.0)
        self.shoot: Optional[int] = None
        self.look: Optional[int] = None
        self.look_pos = (0.0, 0.0)
        self.stick_xy = (0.0, 0.0)

    def bound_names(self):
        out = super().bound_names()
        if not self.binds.get("KeyAction"):
            out.add("MouseLButton")              # BlueStacks' default shoot button
        return out

    def captures_mouse(self) -> bool:
        return self.on and not self.suspended

    def _set(self, on: bool):
        if on == self.on:
            return
        self.on = on
        if not on:
            self.release_fingers()
        self.e.update_capture()

    def key(self, name, down):
        if self.bound("KeyStartStop", name):
            if down and (self.on or self.e.condition(self.raw.get("StartCondition", ""))):
                self._set(not self.on)
            return True
        if self.bound("KeySuspend", name):
            if self.on:
                self.suspended = down
                if down:
                    self.release_fingers()
                self.e.update_capture()
            return True
        if not self.captures_mouse():
            return False
        if self.bound("KeyLookAround", name) and self.raw.get("IsLookAroundEnabled", True):
            if down and self.look is None:
                self.look_pos = self.xy("LookAroundX", "LookAroundY")
                self.look = self.e.touch_down(*self.look_pos)
            elif not down and self.look is not None:
                self.e.touch_up(self.look)
                self.look = None
            return True
        action = self.binds.get("KeyAction") or [Binding("MouseLButton")]
        if any(b.matches(name, self.e.held) for b in action) and \
                self.raw.get("IsShootOnClickEnabled", True):
            if down and self.shoot is None:
                self.shoot = self.e.touch_down(*self.xy("LButtonX", "LButtonY"))
            elif not down and self.shoot is not None:
                self.e.touch_up(self.shoot)
                self.shoot = None
            return True
        return False

    def stick(self, stick, x, y):
        if stick == self.raw.get("GamepadStick"):
            self.stick_xy = (x, y)

    def _drag(self, dxp: float, dyp: float):
        if self.look is not None:                # look around: the second finger moves
            lx, ly = self.look_pos
            self.look_pos = (lx + dxp, ly + dyp)
            self.e.touch_move(self.look, *self.look_pos)
            return
        cx, cy = self.xy()
        if self.cid is None:
            self.cid = self.e.touch_down(cx, cy)
            self.pos = (cx, cy)
        x, y = self.pos[0] + dxp, self.pos[1] + dyp
        left = _f(self.raw, "Left", 0) or cx - self.REACH
        right = _f(self.raw, "Right", 0) or cx + self.REACH
        top = _f(self.raw, "Top", 0) or cy - self.REACH * self.e.aspect
        bottom = _f(self.raw, "Bottom", 0) or cy + self.REACH * self.e.aspect
        if not (left <= x <= right and top <= y <= bottom):
            self.e.touch_up(self.cid)            # out of room: lift, re-centre
            self.cid = self.e.touch_down(cx, cy)
            x, y = cx + dxp, cy + dyp
        self.pos = (x, y)
        self.e.touch_move(self.cid, x, y)

    def rel(self, dx, dy):
        if not self.captures_mouse() or not self.enabled():
            return False
        s = _f(self.raw, "Sensitivity", 1.0) * self.PX_TO_PCT
        self._drag(dx * s, dy * s * _f(self.raw, "SensitivityRatioY", 1.0) * self.e.aspect)
        return True

    def tick(self, now):
        sx, sy = self.stick_xy
        if (sx or sy) and self.enabled():
            s = _f(self.raw, "GamepadSensitivity", 1.0) * 0.6      # approx, percent per tick
            self._drag(sx * s, sy * s * self.e.aspect)

    def release_fingers(self):
        for attr in ("cid", "shoot", "look"):
            cid = getattr(self, attr)
            if cid is not None:
                self.e.touch_up(cid)
                setattr(self, attr, None)

    def release(self):
        self.release_fingers()
        self.suspended = False
        self.stick_xy = (0.0, 0.0)


# ---------------------------------------------------------------- Script
_CMD = re.compile(r"\s*(\S+)\s*(.*)")


class Script(Control):
    """Runs `Commands` on a key press. `onRelease` waits for the key to be released.
    Commands:
    - tap x y [ms]
    - keyDown K / keyUp K (keys of this scheme's other controls, else the guest)
    - wait ms
    - mouseDown x y / mouseMove x y / mouseUp
    - swipe x1 y1 x2 y2 [ms]
    - text "..."
    - state Name on|off|toggle
    - loop [n] ... loopend"""
    kind = "Script"

    def __init__(self, eng, raw):
        super().__init__(eng, raw)
        self.held = False
        self.run: Optional[Iterator[float]] = None
        self.wake = 0.0
        self.mouse: Optional[int] = None
        self.vkeys: set[str] = set()

    def key(self, name, down):
        if not self.bound("Key", name):
            return False
        self.held = down
        if down and self.run is None and self.enabled():
            self.run = self._program(list(self.raw.get("Commands") or []))
            self.wake = 0.0
        return True

    def _program(self, cmds: list[str]) -> Iterator[float]:
        """A generator: yields how long to sleep (s); -1 = until the key is released."""
        stack: list[tuple[int, int]] = []         # (loop start index, remaining)
        i = 0
        while i < len(cmds):
            m = _CMD.match(str(cmds[i]))
            i += 1
            if not m:
                continue
            op, rest = m.group(1).lower(), m.group(2).strip()
            a = rest.split()
            num = []
            for t in a:
                try:
                    num.append(float(t))
                except ValueError:
                    pass
            if op == "wait":
                yield (num[0] if num else 0) / 1000.0
            elif op == "onrelease":
                yield -1
            elif op in ("keydown", "keyup") and a:
                k = keys.canon(a[0])
                self.e.virtual_key(k, op == "keydown")
                (self.vkeys.add if op == "keydown" else self.vkeys.discard)(k)
            elif op == "tap" and len(num) >= 2:
                cid = self.e.touch_down(num[0], num[1])
                yield (num[2] if len(num) > 2 else 50) / 1000.0
                self.e.touch_up(cid)
            elif op == "mousedown" and len(num) >= 2:
                if self.mouse is None:
                    self.mouse = self.e.touch_down(num[0], num[1])
            elif op == "mousemove" and len(num) >= 2 and self.mouse is not None:
                self.e.touch_move(self.mouse, num[0], num[1])
            elif op == "mouseup" and self.mouse is not None:
                self.e.touch_up(self.mouse)
                self.mouse = None
            elif op == "swipe" and len(num) >= 4:
                dur = (num[4] if len(num) > 4 else 200) / 1000.0
                cid = self.e.touch_down(num[0], num[1])
                t0 = _now()
                while (t := (_now() - t0) / dur) < 1.0:
                    self.e.touch_move(cid, num[0] + (num[2] - num[0]) * t,
                                      num[1] + (num[3] - num[1]) * t)
                    yield 0
                self.e.touch_move(cid, num[2], num[3])
                self.e.touch_up(cid)
            elif op == "text":
                self.e.text.emit(rest.strip().strip('"'))
            elif op == "state" and a:
                v = (a[1].lower() if len(a) > 1 else "toggle")
                cur = self.e.states.get(a[0], False)
                self.e.states[a[0]] = (not cur) if v == "toggle" else v in ("on", "1", "true")
            elif op in ("loop", "repeat"):
                stack.append((i, int(num[0]) if num else -1))
            elif op == "loopend" and stack:
                start, left = stack[-1]
                if left == 1:
                    stack.pop()
                else:
                    stack[-1] = (start, left - 1 if left > 0 else -1)
                    if left < 0 and not self.held:       # endless loop: until key up
                        stack.pop()
                        continue
                    i = start
                    yield 0
            # unknown commands (zoom, mousewheel, if, enable, ...): milestone 2

    def tick(self, now):
        while self.run is not None and now >= self.wake:
            try:
                d = next(self.run)
            except StopIteration:
                self._finish()
                return
            if d < 0:
                if self.held:
                    self.wake = float("inf")              # resumed by the release
                    return
                continue
            self.wake = now + d
            if d > 0:
                return

    def on_release_wake(self):
        if self.run is not None and self.wake == float("inf"):
            self.wake = 0.0

    def _finish(self):
        self.run = None
        if self.mouse is not None:
            self.e.touch_up(self.mouse)
            self.mouse = None
        for k in list(self.vkeys):
            self.e.virtual_key(k, False)
        self.vkeys.clear()

    def release(self):
        self.held = False
        self._finish()


class State(Control):
    """Sets a named state while (or each time) its key is pressed, for conditions."""
    kind = "State"

    def key(self, name, down):
        if not self.bound("Key", name):
            return False
        n = self.raw.get("Name") or self.raw.get("State") or ""
        if n and down:
            self.e.states[n] = not self.e.states.get(n, False)
        return True


TYPES: dict[str, type[Control]] = {
    "Tap": Tap, "TapRepeat": TapRepeat, "Dpad": Dpad, "Swipe": Swipe, "Zoom": Zoom,
    "Pan": Pan, "Script": Script, "State": State,
}


# ---------------------------------------------------------------- engine
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
        self.controls: list[Control] = []
        self.unsupported: dict[str, int] = {}
        self.held: set[str] = set()
        self.states: dict[str, bool] = {}
        self.enabled = True
        self.unknown_is_true = True  # Smart conditions without image detection (milestone 4)
        self.aspect = 16 / 9         # view width / height, set by the player
        self._free = list(range(15, 0, -1))
        self._pos: dict[int, tuple[int, int]] = {}
        self._focused = True
        self._timer = QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self._tick)

    # -- lifecycle
    def start(self) -> None:
        self.ch.start()
        self._timer.start()

    def stop(self) -> None:
        self.release_all()
        self.ch.flush()
        self._timer.stop()
        self.ch.stop()
        self.pads.close()

    def set_view(self, width: int, height: int) -> None:
        if width > 0 and height > 0:
            self.aspect = width / height

    def load(self, cfg: Optional[ControlsFile], scheme: Optional[str] = None) -> None:
        self.release_all()
        self.cfg = cfg
        self.scheme = (cfg.scheme(scheme) if cfg and scheme else None) or \
            (cfg.selected() if cfg else None)
        self.controls, self.unsupported = [], {}
        for raw in (self.scheme.controls if self.scheme else []):
            t = control_type(raw)
            cls = TYPES.get(t)
            if cls is None:
                self.unsupported[t] = self.unsupported.get(t, 0) + 1
                continue
            self.controls.append(cls(self, raw))
        self.update_capture()
        self.changed.emit()

    def set_enabled(self, on: bool) -> None:
        if on != self.enabled:
            self.enabled = on
            self.release_all()
            self.update_capture()
            self.changed.emit()

    # -- conditions
    def condition(self, src: str) -> bool:
        v = expr.evaluate(src, lambda n: self.states.get(n) if n in self.states else None)
        return self.unknown_is_true if v is None else v

    # -- what QEMU should send us
    def bound_names(self) -> set[str]:
        out: set[str] = set()
        for c in self.controls:
            out |= c.bound_names()
        return out

    def update_capture(self) -> None:
        if not self.ch.is_open:
            return
        names = self.bound_names() if self.enabled and self.controls else set()
        codes = sorted({c for n in names for c in keys.evdev_codes(n)})
        mask = 0
        for n in names:
            mask |= keys.MOUSE_BUTTONS.get(n, 0)
            if n in keys.MOUSE_WHEEL:
                mask |= keys.OST_BTN_WHEEL
        aiming = any(isinstance(c, Pan) and c.captures_mouse() for c in self.controls)
        if not aiming:
            mask &= ~1               # the left button stays a touch outside aim mode
        self.ch.send("cap " + " ".join(map(str, codes)))
        self.ch.send(f"btn {mask}")
        self.ch.send(f"lock {1 if aiming and self._focused else 0}")
        self.ch.flush()

    def _on_connected(self) -> None:
        self.status.emit("game controls: connected")
        self.update_capture()

    # -- touches (percent of the view)
    def touch_down(self, x: float, y: float) -> int:
        if not self._free:
            return 0
        cid = self._free.pop()
        self.touch_move(cid, x, y)
        return cid

    def touch_move(self, cid: int, x: float, y: float) -> None:
        if not cid:
            return
        vx = int(round(max(0.0, min(100.0, x)) * ABS_MAX / 100))
        vy = int(round(max(0.0, min(100.0, y)) * ABS_MAX / 100))
        if self._pos.get(cid) == (vx, vy):
            return                                   # unchanged: no new frame
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
            c.release()
        self.held.clear()
        self._free = list(range(15, 0, -1))
        self._pos.clear()
        self.ch.send("r")
        self.ch.flush()

    # -- input
    def _dispatch(self, name: str, down: bool) -> bool:
        if down:
            if name in self.held:
                return True
            self.held.add(name)
        else:
            self.held.discard(name)
        if not self.enabled:
            return False
        used = False
        for c in self.controls:
            if c.key(name, down):
                used = True
            if not down and isinstance(c, Script) and                     any(b.key == name for b in c.binds.get("Key", [])):
                c.on_release_wake()
        return used

    def virtual_key(self, name: str, down: bool) -> None:
        """A key pressed by a script: other controls first, else it goes to the guest."""
        if not self._dispatch(name, down):
            for code in keys.evdev_codes(name)[:1]:
                self.ch.send(f"k {code} {1 if down else 0}")

    def _on_line(self, line: str) -> None:
        if DEBUG:
            print(f"[controls] <- {line}", file=sys.stderr, flush=True)
        p = line.split()
        kind = p[0]
        try:
            if kind == "K":
                for n in keys.names_for_code(int(p[1])):
                    self._dispatch(n, p[2] == "1")
            elif kind == "B":
                bit = int(p[1])
                for n, b in keys.MOUSE_BUTTONS.items():
                    if b == bit:
                        self._dispatch(n, p[2] == "1")
            elif kind == "W":
                n = "MouseWheelUp" if int(p[1]) > 0 else "MouseWheelDown"
                self._dispatch(n, True)
                self._dispatch(n, False)
            elif kind == "R":
                dx, dy = int(p[1]), int(p[2])
                for c in self.controls:
                    if c.rel(dx, dy):
                        break
            elif kind == "F":
                self._focused = p[1] == "1"
                if not self._focused:
                    self.release_all()
                self.update_capture()
            elif kind == "O":
                self.changed.emit()
        except (IndexError, ValueError):
            return
        self.ch.flush()

    def _tick(self) -> None:
        now = _now()
        if self.enabled and self._focused:
            for ev in self.pads.poll():
                if ev[0] == "button":
                    self._dispatch(ev[1], ev[2])
                else:
                    for c in self.controls:
                        c.stick(ev[1], ev[2], ev[3])
        for c in self.controls:
            c.tick(now)
        self.ch.flush()
