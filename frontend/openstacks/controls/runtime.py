"""Game control types: HD-Player.exe's runtime behaviour, per docs/re/kmm_controls.md.

Each class reads its JSON fields with HD-Player's *parse* defaults (§2) and implements the
§3 behaviour.
- Positions are percent of the view.
- Speeds are percent of the view per second, stepped every 16 ms (engine.STEP).
- Times are in ms.

Not implemented:
- Tilt: OpenStacks has no accelerometer path into the guest yet.
- SmartScroll, GenericSwipe, Callback: their HD-Player runtimes were not analysed.
These are listed as unsupported, and their keys are not captured.
"""
from __future__ import annotations

import math
import re
from typing import Iterator, Optional

from . import keys
from .engine import STEP, now

# ------------------------------------------------------------------ helpers


def fnum(c: dict, k: str, default: float = 0.0) -> float:
    try:
        v = c.get(k, default)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def combos(c: dict, field: str) -> list[list[str]]:
    """A key field and its _alt1 twin -> combos (each a list of key names)."""
    out = []
    for f in (field, field + "_alt1"):
        v = c.get(f)
        if isinstance(v, str) and v.strip():
            out.append(keys.split_combo(v))
    return out


def toward(cur: tuple[float, float], target: tuple[float, float], speed: float):
    """One 16 ms step from cur toward target at `speed` %/s. Returns (new point, arrived)."""
    dx, dy = target[0] - cur[0], target[1] - cur[1]
    dist = math.hypot(dx, dy)
    if dist < 0.01:
        return target, True
    if speed <= 0:
        return cur, False                        # HD-Player: speed 0 => nothing moves
    step = speed * STEP
    if step >= dist:
        return target, True
    return (cur[0] + dx / dist * step, cur[1] + dy / dist * step), False


class Control:
    """Base: key fields, relevance (§0.3), Exclusive (§1.1), enable state."""
    key_fields: tuple[str, ...] = ()

    def __init__(self, e, raw: dict):
        self.e = e
        self.raw = raw
        self.active = True
        self.tweaks = int(fnum(raw, "Tweaks", 0))
        self.exclusive_on = bool(raw.get("Exclusive", False))
        self.excl_delay = fnum(raw, "ExclusiveDelay", 200) / 1000.0
        self.pressed_at = 0.0
        self._excl_at: Optional[float] = None
        self.binds = {f: combos(raw, f) for f in self.key_fields}

    # -- bindings
    def all_combos(self) -> list[list[str]]:
        return [cb for f in self.binds for cb in self.binds[f]]

    def bound_names(self) -> set[str]:
        return {k for cb in self.all_combos() for k in cb}

    def relevant(self, field: str, name: str, down: bool) -> bool:
        for cb in self.binds.get(field, ()):
            if len(cb) == 1:
                if cb[0] == name and not (down and self.e.suppressed(name)):
                    return True
            elif name in cb:
                return True
        return False

    def held(self, field: str) -> bool:
        return any(all(k in self.e.held for k in cb) for cb in self.binds.get(field, ()))

    # -- mouse needs (engine.update_capture)
    def mouse_buttons(self) -> int:
        return 0                     # OST_BTN_LEFT when this control needs the left button

    def wants_hover(self) -> bool:
        return False

    def wants_lock(self) -> bool:
        return False

    def captures_all_keys(self) -> bool:
        return False

    # -- Exclusive (§1.1, §3.1b): lift the other exclusive controls, press after the delay
    def excl_begin(self) -> None:
        for c in list(self.e.exclusive):
            if c is not self:
                c.reset()
        base = max((c.pressed_at for c in self.e.exclusive if c is not self), default=0.0)
        self._excl_at = base + self.excl_delay if base else now()

    def excl_due(self, t: float) -> bool:
        if self._excl_at is not None and t >= self._excl_at:
            self._excl_at = None
            self.e.exclusive.clear()
            self.e.exclusive.add(self)
            self.pressed_at = t
            return True
        return False

    def excl_cancel(self) -> None:
        self._excl_at = None
        self.e.exclusive.discard(self)

    # -- hooks
    def on_key(self, name: str, down: bool) -> None:
        pass

    def on_mouse(self, ev: str, d: dict) -> None:
        pass

    def on_stick(self, stick: str, x: float, y: float) -> None:
        pass

    def tick(self, t: float) -> None:
        pass

    def set_active(self, on: bool) -> None:
        if not on and self.active:
            self.reset()
        self.active = on

    def reset(self) -> None:
        pass

    def xy(self, fx="X", fy="Y") -> tuple[float, float]:
        return fnum(self.raw, fx, 50.0), fnum(self.raw, fy, 50.0)


# ------------------------------------------------------------------ Tap (§3.1)
class Tap(Control):
    key_fields = ("Key",)

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.cid = 0
        self.pend = False

    def on_key(self, name, down):
        if not self.relevant("Key", name, down):
            return
        if not self.held("Key"):
            self.reset()
            return
        if self.exclusive_on:
            if not self.cid and not self.pend:
                self.pend = True
                self.excl_begin()
            return
        if self.tweaks & 1:
            pan = self.e.pan()
            if pan:
                pan.tap_pause(True)
        if not self.cid:
            self.cid = self.e.touch_down(*self.xy())
            self.pressed_at = now()

    def tick(self, t):
        if self.pend and self.excl_due(t):
            self.pend = False
            if self.held("Key"):
                self.cid = self.e.touch_down(*self.xy())

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
            if self.tweaks & 1:
                pan = self.e.pan()
                if pan:
                    pan.tap_pause(False)
        self.pend = False
        self.excl_cancel()


# ------------------------------------------------------------------ TapRepeat (§3.4)
class TapRepeat(Control):
    """One tap = `Delay` ms down + `Delay` ms up. RepeatUntilKeyUp: taps while held;
    otherwise exactly `Count` taps per press, finishing after the key is released."""
    key_fields = ("Key",)

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.count = int(fnum(raw, "Count", 1))
        self.delay = max(16, int(fnum(raw, "Delay", 16))) / 1000.0
        self.until_up = bool(raw.get("RepeatUntilKeyUp", True))
        self.cid = 0
        self.remaining = 0
        self.last = 0.0
        self.pend = False

    def _down(self, t):
        self.cid = self.e.touch_down(*self.xy())
        self.last = t

    def on_key(self, name, down):
        if not self.relevant("Key", name, down):
            return
        if self.held("Key"):
            if self.cid:
                self.e.touch_up(self.cid)
                self.cid = 0
            self.remaining = 1 if self.until_up else self.count - 1
            if self.exclusive_on:
                self.pend = True
                self.excl_begin()
            else:
                self._down(now())
        else:
            if self.cid:
                self.e.touch_up(self.cid)
                self.cid = 0
            self.last = now()
            if self.until_up:
                self.remaining = 0

    def tick(self, t):
        if self.pend:
            if self.excl_due(t):
                self.pend = False
                self._down(t)
            return
        if not self.cid and self.remaining <= 0:
            return
        if t - self.last < self.delay:
            return
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
            self.last = t
        elif self.remaining > 0:
            if not self.until_up:
                self.remaining -= 1
            self._down(t)

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
        self.remaining = 0
        self.pend = False
        self.excl_cancel()


# ------------------------------------------------------------------ Dpad (§3.3)
class Dpad(Control):
    key_fields = ("KeyUp", "KeyDown", "KeyLeft", "KeyRight", "KeySpeedModifier1",
                  "KeySpeedModifier2")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.r = (fnum(raw, "XRadius", 0), fnum(raw, "XRadius1", 0), fnum(raw, "XRadius2", 0))
        self.dead = fnum(raw, "DeadzoneRadius", 0)
        self.speed = fnum(raw, "Speed", 150)
        self.act_speed = fnum(raw, "ActivationSpeed", 0)
        self.act_time = fnum(raw, "ActivationTime", 50) / 1000.0
        self.expiry = fnum(raw, "DpadExpiryTime", 0) / 1000.0
        self.stick = raw.get("GamepadStick") or ""
        self.state = 0               # 0 idle, 2 activating, 3 moving, 4 arrived, 5 excl pending
        self.cid = 0
        self.cur = self.xy()
        self.target = self.xy()
        self.t0 = 0.0
        self.released_at = -1.0
        self.release_pending = False
        self.stick_xy = (0.0, 0.0)
        self.stick_released_at = -1.0

    def _radius(self) -> float:
        if self.held("KeySpeedModifier1") and self.r[1]:
            return self.r[1]
        if self.held("KeySpeedModifier2") and self.r[2]:
            return self.r[2]
        return self.r[0]

    def _vector(self) -> tuple[float, float]:
        sx, sy = self.stick_xy
        if sx or sy:
            return sx, sy
        dx = (1 if self.held("KeyRight") else 0) - (1 if self.held("KeyLeft") else 0)
        dy = (1 if self.held("KeyDown") else 0) - (1 if self.held("KeyUp") else 0)
        return float(dx), float(dy)

    def _aim(self) -> None:
        dx, dy = self._vector()
        x, y = self.xy()
        r = self._radius()
        keyb = not (self.stick_xy[0] or self.stick_xy[1])
        tx, ty = dx * r, dy * r * self.e.aspect
        if keyb and dx and dy:
            tx, ty = tx / math.sqrt(2), ty / math.sqrt(2)
        self.target = (max(0, min(100, x + tx)), max(0, min(100, y + ty)))
        self.dz = (dx * self.dead, dy * self.dead)

    def _update(self) -> None:
        dx, dy = self._vector()
        if dx or dy:
            self._aim()
            if self.state == 0:
                if self.exclusive_on:
                    self.state = 5
                    self.excl_begin()
                    return
                self._press()
            elif self.state == 4:
                self.state = 3
            if self.tweaks & 2 and self.cid:            # snap to centre on direction change
                x, y = self.xy()
                self.cur = (x + self.dz[0], y + self.dz[1])
                self.e.touch_move(self.cid, *self.cur)
            self.release_pending = False
        elif self.state:
            if now() >= self.t0 + self.expiry:
                self._lift()
            else:
                self.release_pending = True

    def _press(self) -> None:
        t = now()
        x, y = self.xy()
        if t - self.released_at < 0.05 and self.released_at >= 0:
            self.state = 1                               # debounce: press after 50 ms
            self.t0 = t
            return
        self.cur = (x + self.dz[0], y + self.dz[1])
        self.cid = self.e.touch_down(*self.cur)
        self.t0 = t
        self.state = 2 if self.act_time else 3

    def _lift(self) -> None:
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
        self.state = 0
        self.release_pending = False
        self.released_at = now()
        self.excl_cancel()

    def on_key(self, name, down):
        if any(self.relevant(f, name, down) for f in self.key_fields):
            self._update()

    def on_stick(self, stick, x, y):
        if not self.stick or stick != self.stick:
            return
        if (x or y) and not (self.stick_xy[0] or self.stick_xy[1]) and \
                now() - self.stick_released_at < 0.1:
            return                                       # re-engage after 100 ms
        if not (x or y) and (self.stick_xy[0] or self.stick_xy[1]):
            self.stick_released_at = now()
        self.stick_xy = (x, y)
        self._update()

    def tick(self, t):
        if self.state == 5:
            if self.excl_due(t):
                self._press()
            return
        if self.state == 1 and t - self.t0 >= 0.05:
            self._aim()
            self.cur = (self.xy()[0] + self.dz[0], self.xy()[1] + self.dz[1])
            self.cid = self.e.touch_down(*self.cur)
            self.state = 2 if self.act_time else 3
            self.t0 = t
            return
        if self.release_pending and t >= self.t0 + self.expiry:
            self._lift()
            return
        if self.state == 2:
            if t - self.t0 >= self.act_time:
                self.state = 3
            elif self.act_speed:
                self.cur, _ = toward(self.cur, self.target, self.act_speed)
                self.e.touch_move(self.cid, *self.cur)
            return
        if self.state == 3:
            self.cur, arrived = toward(self.cur, self.target, self.speed)
            if self.tweaks & 1 and not arrived:
                self.cur = (self.cur[0] + 0.001, self.cur[1])
            self.e.touch_move(self.cid, *self.cur)
            if arrived:
                self.state = 4

    def reset(self):
        self.stick_xy = (0.0, 0.0)
        self._lift()


# ------------------------------------------------------------------ Pan (§3.2)
class Pan(Control):
    """Aim mode. KeyStartStop toggles it (or must be held, with Tweaks 0x20):
    - the cursor is captured and a finger at (X, Y) follows the mouse pixel for pixel
      times Sensitivity;
    - it re-centres when it leaves the view;
    - the left button holds a shoot finger at LButtonX/Y;
    - KeyLookAround moves a finger from LookAroundX/Y;
    - KeySuspend frees the cursor while held."""
    key_fields = ("KeyStartStop", "KeySuspend", "KeyLookAround")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        s = fnum(raw, "Sensitivity", -1)
        self.sens = 1.0 if s <= 0 else s                 # -1 = absent (§5 item 1)
        r = fnum(raw, "SensitivityRatioY", -1)
        self.ratio_y = 1.0 if r <= 0 else r
        gs = fnum(raw, "GamepadSensitivity", -1)
        self.pad_sens = 1.0 if gs <= 0 else gs
        self.lbutton = self.xy("LButtonX", "LButtonY") if "LButtonX" in raw else (-1, -1)
        self.look = self.xy("LookAroundX", "LookAroundY") if "LookAroundX" in raw else (-1, -1)
        self.shoot_on = bool(raw.get("IsShootOnClickEnabled", self.lbutton != (-1, -1)))
        self.look_on = bool(raw.get("IsLookAroundEnabled", self.look != (-1, -1)))
        self.stick = raw.get("GamepadStick") or ""
        self.bounds = (fnum(raw, "Left", 250), fnum(raw, "Right", 250),
                       fnum(raw, "Top", 1000), fnum(raw, "Bottom", 1000))
        self.dead_px = fnum(raw, "DeadzoneRadius", 14)
        self.wait_shoot = fnum(raw, "WaitAfterShootTime", -1) / 1000.0
        self.wait_reset = fnum(raw, "WaitAfterResetTime", -1) / 1000.0
        self.act_ms = fnum(raw, "ActivationTimeMs", 40) / 1000.0
        self.wiggle_speed = fnum(raw, "Speed", 10)
        self.hold = bool(self.tweaks & 0x20)
        self.state = 0
        self.cid = 0                 # the pan finger
        self.shoot_cid = 0           # the second finger at LButton (no Tweaks 0x100)
        self.start = self.xy()
        self.acc = (0.0, 0.0)
        self.vcur = (0.0, 0.0)       # virtual cursor, guest px, relative to the press point
        self.t = 0.0
        self.pauses = 0
        self.stick_xy = (0.0, 0.0)
        self.last_reset = 0.0
        self.first_move = False

    # -- capture
    def wants_lock(self):
        return self.state not in (0, 5, 0xb)

    def mouse_buttons(self):
        return 1 if self.state in (1, 3, 4, 7, 10, 0xd) else 0

    def captures_mouse(self):
        return self.wants_lock()

    # -- start / stop (§3.2)
    def _start(self, state: int) -> None:
        self.start = self.look if state == 3 else self.lbutton if state == 7 else self.xy()
        self.acc = (0.0, 0.0)
        self.vcur = (0.0, 0.0)
        if self.cid:
            self.e.touch_up(self.cid)
        self.cid = self.e.touch_down(*self.start)
        self.state = state
        self.t = now()
        self.first_move = bool(self.tweaks & 0x1000)
        self.e.changed.emit()

    def _stop(self, state: int) -> None:
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
        if self.shoot_cid:
            self.e.touch_up(self.shoot_cid)
            self.shoot_cid = 0
        self.state = state
        self.t = now()
        self.e.changed.emit()

    def tap_pause(self, pause: bool) -> None:
        """A Tap with Tweaks&1 pauses panning while held (§3.1, §3.2 state 6)."""
        if pause:
            self.pauses += 1
            if self.state == 1:
                self._stop(6)
        else:
            self.pauses = max(0, self.pauses - 1)
            if self.pauses == 0 and self.state == 6:
                self.state, self.t = 0xe, now()

    def _activate(self) -> None:
        self._start(0xc if self.tweaks & 0x10000 else 1)

    # -- keys
    def on_key(self, name, down):
        st = self.relevant("KeyStartStop", name, down)
        su = self.relevant("KeySuspend", name, down)
        lo = self.relevant("KeyLookAround", name, down) and self.look_on
        s = self.state
        if s in (0, 6):
            if st and down:
                if self.exclusive_on:
                    self.state = 0xb
                    self.excl_begin()
                else:
                    self._activate()
        elif s in (1, 7, 0xc, 10, 0xd, 0xe):
            if st and (down != self.hold):
                self._stop(0)
            elif lo and down:
                self._stop(2)
            elif su and down:
                self._stop(5)
        elif s in (2, 3):
            if lo and not down:
                self._stop(4)
        elif s == 4:
            if st and down:
                self._stop(0)
            elif lo and down:
                self._start(3)
        elif s == 5:
            if su and not down:
                self._activate()
            elif self.hold and st and not down:
                self._stop(0)
        elif s == 0xb and st and not down and self.hold:
            self.excl_cancel()
            self.state = 0

    # -- mouse
    def _move(self, dxp: float, dyp: float, sens: float) -> None:
        """Relative motion in guest px; the finger follows (§3.2 formula)."""
        if self.state == 4:
            if now() - self.t >= 0.05:
                self._start(1)
            else:
                return
        if self.state not in (1, 3, 7, 10) or not self.cid and self.state != 10:
            return
        if self.state == 10:
            if now() - self.t < 0.02:
                return
            self.cid = self.e.touch_down(*self.start)
            self.state = 1
        fw, fh = self.e.fb
        if self.first_move and (dxp or dyp):         # Tweaks 0x1000: beat the touch slop
            m = math.hypot(dxp, dyp)
            dxp += dxp / m * self.dead_px
            dyp += dyp / m * self.dead_px
            self.first_move = False
        self.vcur = (self.vcur[0] + dxp, self.vcur[1] + dyp)
        x = self.start[0] + self.acc[0] + self.vcur[0] * sens / fw * 100
        y = self.start[1] + self.acc[1] + self.vcur[1] / fh * sens * self.ratio_y * 100
        if self._out(x, y):
            # re-centre: lift, re-press at the start point, keep this event's motion
            self.e.touch_up(self.cid)
            self.acc, self.vcur = (0.0, 0.0), (0.0, 0.0)
            self.last_reset = now()
            if self.tweaks & 0x800:
                self.cid = 0
                self.state, self.t = 10, now()
                return
            self.cid = self.e.touch_down(*self.start)
            x = self.start[0] + dxp * sens / fw * 100
            y = self.start[1] + dyp / fh * sens * self.ratio_y * 100
            self.vcur = (dxp, dyp)
            if self._out(x, y):                      # one huge jump: stay at the start
                x, y = self.start
                self.vcur = (0.0, 0.0)
        self.e.touch_move(self.cid, x, y)

    def _out(self, x: float, y: float) -> bool:
        tw = self.tweaks
        sx, sy = self.start
        if tw & 0x10:
            if abs(x - sx) >= 1.2 * max(sx, 100 - sx):
                return True
        elif tw & (0x4 | 0x400):
            if not (55 <= x <= 100 and 50 <= y <= 100):
                return True
        elif abs(x / 100 - 0.5) > 0.5 or abs(y / 100 - 0.5) >= 0.5:
            return True
        if tw & 0x2000:
            fw, fh = self.e.fb
            px, py = (x - sx) / 100 * fw, (y - sy) / 100 * fh
            if px < -self.bounds[0] or px > self.bounds[1] or py < -self.bounds[2] \
                    or py > self.bounds[3]:
                return True
        if tw & 0x1 and now() - self.last_reset >= 0.2:
            return True
        return False

    def on_mouse(self, ev, d):
        if ev == "rel":
            vw, vh = self.e.view_px
            fw, fh = self.e.fb
            self._move(d["dx"] * fw / vw, d["dy"] * fh / vh, self.sens)
        elif ev in ("ldown", "lup") and self.state in (1, 3, 4, 7, 10, 0xd):
            down = ev == "ldown"
            if not self.shoot_on or self.lbutton == (-1, -1):
                return
            if self.tweaks & 0x100:                   # the pan finger itself shoots
                if down and self.state != 7:
                    self._stop(6)
                    self._start(7)
                elif not down and self.state == 7:
                    self._stop(6)
                    if self.wait_shoot > 0:
                        self.state, self.t = 0xd, now()
                    else:
                        self._activate()
            elif down and not self.shoot_cid:
                self.shoot_cid = self.e.touch_down(*self.lbutton)
            elif not down and self.shoot_cid:
                self.e.touch_up(self.shoot_cid)
                self.shoot_cid = 0

    def on_stick(self, stick, x, y):
        if self.stick and stick == self.stick:
            self.stick_xy = (x, y)

    def tick(self, t):
        s = self.state
        if s == 0xb and self.excl_due(t):
            self._activate()
        elif s == 2 and t - self.t >= 0.1:
            self._start(3)
        elif s == 0xd and t - self.t >= max(0.0, self.wait_shoot):
            self._activate()
        elif s == 0xe and t - self.t >= max(0.0, self.wait_reset):
            self._activate()
        elif s == 0xc:                               # activation wiggle (Tweaks 0x10000)
            el = t - self.t
            if el < self.act_ms / 2:
                self.start = (self.start[0] + self.wiggle_speed * STEP, self.start[1])
            elif el < self.act_ms:
                self.start = (self.start[0] - self.wiggle_speed * STEP, self.start[1])
            else:
                self.start = self.xy()
                self.state = 1
            if self.cid:
                self.e.touch_move(self.cid, *self.start)
        if self.tweaks & 0x8000 and self.state == 1 and t - self.t >= 0.1:
            tx, ty = self.xy("TapX", "TapY") if fnum(self.raw, "TapX", -1) >= 0 else self.xy()
            cid = self.e.touch_down(tx, ty)
            self.e.touch_up(cid)
            self.t = t
        sx, sy = self.stick_xy
        if (sx or sy) and self.state in (1, 3, 7):
            vw, vh = self.e.view_px
            fw, fh = self.e.fb
            self._move(sx * 20 * fw / vw, sy * 20 * fh / vh, self.pad_sens)
        elif (sx or sy) and self.state == 0 and self.stick:
            self._activate()                         # the right stick pans like the mouse

    def reset(self):
        self._stop(0)
        self.pauses = 0
        self.stick_xy = (0.0, 0.0)
        self.excl_cancel()


# ------------------------------------------------------------------ Swipe (§3.5)
class Swipe(Control):
    key_fields = ("Key",)

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.p1 = (fnum(raw, "X1", 50), fnum(raw, "Y1", 50))
        self.p2 = (fnum(raw, "X2", 50), fnum(raw, "Y2", 50))
        self.speed = fnum(raw, "Speed", 200)
        self.hold = bool(raw.get("Hold", False))
        self.keyup_stop = bool(raw.get("KeyUpStop", False))
        self.state = 0               # 0 idle, 1 swiping, 2 swiping (hold), 3 held at end, 4 excl
        self.cid = 0
        self.cur = self.p1

    def _begin(self):
        if self.cid:
            self.e.touch_up(self.cid)
        self.cur = self.p1
        self.cid = self.e.touch_down(*self.cur)
        self.state = 2 if self.hold else 1

    def on_key(self, name, down):
        if not self.relevant("Key", name, down):
            return
        if self.held("Key"):
            if self.state in (0, 1, 2):
                if self.exclusive_on and self.state == 0:
                    self.state = 4
                    self.excl_begin()
                else:
                    self._begin()                # re-press restarts the swipe
        else:
            if self.state == 4:
                self.state = 0
                self.excl_cancel()
            elif self.state == 1:
                if self.keyup_stop:
                    self.reset()
            elif self.state == 2:
                if self.keyup_stop:
                    self.reset()
                else:
                    self.state = 1
            elif self.state == 3:
                self.reset()

    def tick(self, t):
        if self.state == 4:
            if self.excl_due(t):
                self._begin()
            return
        if self.state not in (1, 2):
            return
        nxt, arrived = toward(self.cur, self.p2, self.speed)
        if not arrived:
            self.cur = nxt
            self.e.touch_move(self.cid, *self.cur)
            return
        if self.state == 2:
            self.state = 3                       # the finger stays near the end until key-up
        else:
            self.e.touch_move(self.cid, *self.p2)
            self.reset()

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
        self.state = 0
        self.excl_cancel()


# ------------------------------------------------------------------ Zoom (§3.6)
class Zoom(Control):
    key_fields = ("KeyIn", "KeyOut", "KeyModifier")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.c1 = (fnum(raw, "X1", 0), fnum(raw, "Y1", 0))
        self.c2 = (fnum(raw, "X2", 0), fnum(raw, "Y2", 0))
        self.speed = fnum(raw, "Speed", 1.0)
        self.min_d = fnum(raw, "MinDistance", 5.0)
        self.mode = int(fnum(raw, "Mode", 0))
        self.reset_delay = fnum(raw, "ResetDelay", 75) / 1000.0
        self.state = 0               # 0 idle, 1 active, 3 re-grip wait, 4 excl
        self.ids = [0, 0]
        self.p = [self.c1, self.c2]
        self.t = 0.0

    def _init_points(self):
        p1, p2 = self.c1, self.c2
        if self.tweaks & 1:                         # centred on the cursor
            cx, cy = self.e.cursor
            if 5 <= cx <= 95 and 5 <= cy <= 95:
                hx, hy = (p2[0] - p1[0]) / 2, (p2[1] - p1[1]) / 2
                p1, p2 = (cx - hx, cy - hy), (cx + hx, cy + hy)
        self.p = [p1, p2]

    def _press(self):
        self._init_points()
        self.ids = [self.e.touch_down(*self.p[0]), self.e.touch_down(*self.p[1])]
        self.state = 1

    def _lift(self):
        for i in self.ids:
            if i:
                self.e.touch_up(i)
        self.ids = [0, 0]

    def _engaged(self) -> bool:
        mod_ok = not self.binds["KeyModifier"] or self.held("KeyModifier")
        return mod_ok and (self.held("KeyIn") or self.held("KeyOut"))

    def on_key(self, name, down):
        if not any(self.relevant(f, name, down) for f in self.key_fields):
            return
        if self._engaged():
            if self.state == 0:
                if self.exclusive_on:
                    self.state = 4
                    self.excl_begin()
                else:
                    self._press()
        else:
            self.reset()

    def tick(self, t):
        if self.state == 4:
            if self.excl_due(t):
                self._press()
            return
        if self.state == 3:
            if t - self.t >= self.reset_delay:
                self._press()                        # re-grip at the initial positions
            return
        if self.state != 1 or self.held("KeyIn") == self.held("KeyOut"):
            return
        zin = self.held("KeyIn")
        if self.mode:
            d = (self.c2[0] - self.c1[0], self.c2[1] - self.c1[1])
        else:
            d = (self.p[1][0] - self.p[0][0], self.p[1][1] - self.p[0][1])
        s = (1 if zin else -1) * self.speed * STEP
        step = (s * d[0], s * d[1])
        p1, p2 = self.p
        p2 = (p2[0] + step[0], p2[1] + step[1])
        if not self.tweaks & 2:
            p1 = (p1[0] - step[0], p1[1] - step[1])
        self.p = [p1, p2]
        dx, dy = abs(p2[0] - p1[0]), abs(p2[1] - p1[1])
        if not zin and ((0 < dx < self.min_d) or (0 < dy < self.min_d)):
            self._lift()
            self.state, self.t = 3, t
            return
        if zin and not all(0 <= v <= 100 for pt in self.p for v in pt):
            self._lift()                             # out of room: re-grip
            self.state, self.t = 3, t
            return
        self.e.touch_move(self.ids[0], *p1)
        self.e.touch_move(self.ids[1], *p2)

    def reset(self):
        self._lift()
        self.state = 0
        self.excl_cancel()


# ------------------------------------------------------------------ MOBA D-pad (§3.7)
class MOBADpad(Control):
    """Right-click (KeyMove) walks the hero toward the cursor: the joystick at (X, Y)
    is pushed XRadius in the direction from Origin (the hero) to the cursor. A quick
    click walks for distance / CharSpeed."""
    key_fields = ("KeyCancel",)

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.origin = self.xy("OriginX", "OriginY") if "OriginX" in raw else (50.0, 50.0)
        self.r = fnum(raw, "XRadius", 6)
        self.dpad_speed = fnum(raw, "DpadSpeed", 120)
        self.char_speed = max(0.1, fnum(raw, "CharSpeed", 10))
        self.move_button = keys.MOUSE_BUTTONS.get(keys.canon(raw.get("KeyMove") or
                                                             "MouseRButton"), 2)
        self.state = 0               # 0 idle, 1 moving to target, 2 held, 3 excl
        self.cid = 0
        self.cur = self.xy()
        self.target = self.xy()
        self.press_t = 0.0
        self.expiry = 0.0
        self.release_pending = False
        self.held_btn = False

    def mouse_buttons(self):
        return self.move_button if self.move_button != 2 else 0   # R comes as a key too

    def bound_names(self):
        n = super().bound_names()
        for k, b in keys.MOUSE_BUTTONS.items():
            if b == self.move_button:
                n.add(k)
        return n

    def wants_hover(self):
        return True

    def _retarget(self):
        px, py = self.e.cursor
        ox, oy = self.origin
        dx, dy = px - ox, py - oy
        m = math.hypot(dx, dy) or 1.0
        x, y = self.xy()
        self.target = (x + dx / m * self.r, y + dy / m * self.r * self.e.aspect)
        self.expiry = now() + m * 1000 / self.char_speed / 1000.0

    def _press(self):
        self.cur = self.xy()
        self.cid = self.e.touch_down(*self.cur)
        self.press_t = now()
        self.state = 1
        self.e.mobapad = self

    def on_mouse(self, ev, d):
        if ev in ("down", "up") and d.get("button") == self.move_button:
            if ev == "down":
                self.held_btn = True
                self._retarget()
                self.release_pending = False
                if self.state == 0:
                    if self.exclusive_on:
                        self.state = 3
                        self.excl_begin()
                    else:
                        self._press()
            else:
                self.held_btn = False
                if self.state in (1, 2):
                    if now() - self.press_t > 0.2 or now() >= self.expiry:
                        self.reset()
                    else:
                        self.release_pending = True
        elif ev == "move" and self.held_btn and self.state in (1, 2):
            self._retarget()
            if self.state == 2:
                self.cur = self.target
                self.e.touch_move(self.cid, *self.cur)

    def on_key(self, name, down):
        if self.relevant("KeyCancel", name, down) and down:
            self.reset()

    def stop_now(self):
        """A MOBASkill with StopMOBADpad: a pending release happens at once."""
        if self.release_pending:
            self.reset()

    def tick(self, t):
        if self.state == 3:
            if self.excl_due(t):
                self._press()
            return
        if self.release_pending and t >= self.expiry:
            self.reset()
            return
        if self.state == 1:
            self.cur, arrived = toward(self.cur, self.target, self.dpad_speed)
            self.e.touch_move(self.cid, *self.cur)
            if arrived:
                self.state = 2

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
        self.state = 0
        self.release_pending = False
        if self.e.mobapad is self:
            self.e.mobapad = None
        self.excl_cancel()


# ------------------------------------------------------------------ MOBA skill (§3.8)
class MOBASkill(Control):
    key_fields = ("KeyActivate", "KeyAutocastToggle", "KeyCancel")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        g = lambda k, d: fnum(raw, k, d)
        self.origin = (g("OriginX", 50), g("OriginY", 50))
        self.cancel_pt = (g("CancelX", -1), g("CancelY", -1))
        self.y_ratio = g("YAxisRatio", 0)
        self.r = g("XRadius", 8)
        self.dead = g("DeadZoneRadius", 0)
        self.speed = g("Speed", 200)
        self.cancel_speed = g("CancelSpeed", 200)
        self.min_swipe = g("MinSwipeRadius", 0)
        self.no_cancel_switch = int(g("NoCancelOnSwitch", 0))
        self.no_cancel_t = g("NoCancelTime", 0) / 1000.0
        self.min_hold = g("MinSkillHoldTime", 0) / 1000.0
        self.can_cancel = bool(raw.get("IsCancelSkillEnabled", self.cancel_pt != (-1, -1)))
        self.auto_attack = bool(raw.get("AutoAttack", False))
        self.stop_pad = bool(raw.get("StopMOBADpad", False))
        self.advanced = bool(raw.get("AdvancedMode", True))
        self.autocast = bool(raw.get("AutocastEnabled", True))
        self.state = 0               # 0 idle, 1 moving to aim, 2 aiming, 3 casting, 4 cancel, 5 excl
        self.cid = 0
        self.cur = self.xy()
        self.target = self.xy()
        self.queued = None
        self.l_held = False
        self._mode()

    def _mode(self):
        self.mode = 3 if self.auto_attack else \
            (1 if self.autocast else 0) if self.advanced else (1 if self.autocast else 2)

    def mouse_buttons(self):
        return 1 if self.state in (1, 2) else 0

    def wants_hover(self):
        return True

    def _aims(self) -> bool:
        return self.mode in (0, 2) or (self.mode == 1 and self.l_held)

    def _aim_target(self) -> tuple[float, float]:
        if not self._aims():
            return self.xy()
        w, h = self.e.view_px
        cx, cy = self.e.cursor[0] / 100 * w, self.e.cursor[1] / 100 * h
        ox, oy = self.origin[0] / 100 * w, self.origin[1] / 100 * h
        dx, dy = cx - ox, cy - oy
        drag = (dx * self.r / 100 * 2, dy * self.r / 100 * 2)
        m = math.hypot(*drag)
        if self.min_swipe and now() - self.pressed_at < self.min_hold and \
                m < self.min_swipe / 100 * w and m:
            k = self.min_swipe / 100 * w / m
            drag = (drag[0] * k, drag[1] * k)
        if self.dead and m and m < self.dead / 100 * w and self.state == 1:
            k = self.dead / 100 * w / m
            drag = (drag[0] * k, drag[1] * k)
        k = self.y_ratio if self.y_ratio else float(int(w // max(1, h)))
        x, y = self.xy()
        return (x + drag[0] * 100 / w, y + k * drag[1] * 100 / h)

    def _start(self):
        e = self.e
        self.cur = self.xy()
        self.cid = e.touch_down(*self.cur)
        self.pressed_at = now()
        e.last_skill_press = self.pressed_at
        self.target = self._aim_target()
        self.state = 1
        e.native_touch = False
        e.active_skill = self
        if self.stop_pad and e.mobapad:
            e.mobapad.stop_now()

    def _finish(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
        self.state = 0
        if self.e.active_skill is self:
            self.e.active_skill = None
            self.e.native_touch = True
        q, self.queued = self.queued, None
        if q is not None and q.held("KeyActivate"):
            q._start()

    def _activate(self):
        e = self.e
        t = now()
        s = e.active_skill
        if self.mode == 3:
            if t < e.last_skill_press + self.no_cancel_t:
                return
            self._start()
            return
        if s is not None and s is not self:
            if not s.auto_attack and t < s.pressed_at + s.no_cancel_t:
                return
            if self._aims() and not self.no_cancel_switch and s.can_cancel:
                s.queued = self
                s.state = 4                      # cancel S, then start this one
                return
            s._finish()
        if self.exclusive_on:
            self.state = 5
            self.excl_begin()
            return
        self._start()

    def on_key(self, name, down):
        if self.relevant("KeyAutocastToggle", name, down) and down:
            self.autocast = not self.autocast
            self._mode()
        if self.relevant("KeyCancel", name, down) and down and self.state in (1, 2, 3):
            if now() - self.pressed_at >= self.no_cancel_t:
                self.state = 4
            return
        if not self.relevant("KeyActivate", name, down):
            return
        if down:
            if self.state in (0, 3) and self.held("KeyActivate"):
                self._activate()
            elif self.mode == 2 and self.state in (1, 2):
                self.state = 3                   # pressing again re-casts
        else:
            if self.mode == 3:
                self._finish()
            elif self.mode in (0, 1) and self.state in (1, 2):
                self.state = 3                   # finish the move, then cast
            elif self.state == 5:
                self.state = 0
                self.excl_cancel()

    def on_mouse(self, ev, d):
        if ev == "ldown":
            self.l_held = True
        elif ev == "lup":
            self.l_held = False
            if self.state == 2 and self.mode in (1, 2):
                self.state = 3
        elif ev == "move" and self.state == 2:
            self.cur = self._aim_target()
            self.e.touch_move(self.cid, *self.cur)
        if ev in ("move", "ldown") and self.state == 1:
            self.target = self._aim_target()

    def tick(self, t):
        if self.state == 5:
            if self.excl_due(t):
                self._start()
            return
        if self.state in (1, 3) and t - self.pressed_at >= 0.02:
            self.cur, arrived = toward(self.cur, self.target, self.speed)
            self.e.touch_move(self.cid, *self.cur)
            if arrived:
                if self.state == 3:
                    self._finish()               # cast = lift
                else:
                    self.state = 2
        elif self.state == 4:
            if self.cancel_pt == (-1, -1) or not self.can_cancel:
                self._finish()
                return
            self.cur, arrived = toward(self.cur, self.cancel_pt, self.cancel_speed)
            self.e.touch_move(self.cid, *self.cur)
            if arrived:
                self._finish()

    def reset(self):
        self.queued = None
        self._finish()
        self.excl_cancel()


# ------------------------------------------------------------------ FreeLook (§3.10)
class FreeLook(Control):
    key_fields = ("Key", "KeyUp", "KeyDown", "KeyLeft", "KeyRight")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.mouse_mode = int(fnum(raw, "DeviceType", 0)) == 1
        self.sens = fnum(raw, "Sensitivity", 1.0)
        self.speed = fnum(raw, "Speed", 100)
        self.delay = fnum(raw, "Delay", 50) / 1000.0
        self.state = 0               # 0 idle, 1 active, 2 waiting to re-press, 3 excl
        self.cid = 0
        self.cur = self.xy()
        self.acc = (0.0, 0.0)
        self.vcur = (0.0, 0.0)
        self.t = 0.0

    def wants_lock(self):
        return self.mouse_mode and self.state in (1, 2)

    def _press(self):
        self.cur = self.xy()
        self.vcur = (0.0, 0.0)
        self.acc = (0.0, 0.0)
        self.cid = self.e.touch_down(*self.cur)
        self.state = 1

    def _keys_vec(self):
        dx = (1 if self.held("KeyRight") else 0) - (1 if self.held("KeyLeft") else 0)
        dy = (1 if self.held("KeyDown") else 0) - (1 if self.held("KeyUp") else 0)
        return dx, dy

    def on_key(self, name, down):
        if self.mouse_mode:
            if not self.relevant("Key", name, down):
                return
            if self.held("Key") and self.state == 0:
                if self.exclusive_on:
                    self.state = 3
                    self.excl_begin()
                else:
                    self._press()
            elif not self.held("Key"):
                self.reset()
            return
        if not any(self.relevant(f, name, down) for f in
                   ("KeyUp", "KeyDown", "KeyLeft", "KeyRight")):
            return
        if self._keys_vec() != (0, 0):
            if self.state == 0:
                self._press()
        else:
            self.reset()

    def _place(self, x, y):
        if abs(x / 100 - 0.5) > 0.5 or abs(y / 100 - 0.5) > 0.5:
            self.e.touch_up(self.cid)
            self.cid = 0
            if self.delay:
                self.state, self.t = 2, now()
            else:
                self._press()
            return
        self.cur = (x, y)
        self.e.touch_move(self.cid, x, y)

    def on_mouse(self, ev, d):
        if not self.mouse_mode or ev != "rel" or self.state != 1:
            return
        vw, vh = self.e.view_px
        self.vcur = (self.vcur[0] + d["dx"] / vw, self.vcur[1] + d["dy"] / vh)
        x, y = self.xy()
        self._place(x + self.vcur[0] * self.sens * 100, y + self.vcur[1] * self.sens * 100)

    def tick(self, t):
        if self.state == 3:
            if self.excl_due(t):
                self._press()
            return
        if self.state == 2 and t - self.t >= self.delay:
            self._press()
            return
        if self.state == 1 and not self.mouse_mode:
            dx, dy = self._keys_vec()
            if dx or dy:
                self._place(self.cur[0] + dx * self.speed * STEP,
                            self.cur[1] + dy * self.e.aspect * self.speed * STEP)

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
        self.state = 0
        self.excl_cancel()


# ------------------------------------------------------------------ Rotate (§3.11)
class Rotate(Control):
    key_fields = ("KeyClock", "KeyAntiClock")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.r = fnum(raw, "XRadius", 6.0)
        self.angle0 = fnum(raw, "StartingAngle", 90.0)
        self.speed = fnum(raw, "Speed", 60)
        self.act = fnum(raw, "ActivationTime", 100) / 1000.0
        self.state = 0
        self.anchor = 0
        self.cid = 0
        self.a = self.angle0
        self.t0 = 0.0

    def on_key(self, name, down):
        if not (self.relevant("KeyClock", name, down) or
                self.relevant("KeyAntiClock", name, down)):
            return
        if self.held("KeyClock") or self.held("KeyAntiClock"):
            if self.state == 0:
                self.a = self.angle0
                self.t0 = now()
                if not self.tweaks & 1:
                    self.anchor = self.e.touch_down(*self.xy())
                self.state = 1
        else:
            self.reset()

    def tick(self, t):
        if self.state == 1 and t - self.t0 >= self.act:
            self.state = 2
        if self.state != 2 or (self.held("KeyClock") and self.held("KeyAntiClock")):
            return
        fw, fh = self.e.fb
        rpx = fw * self.r / 100
        x, y = self.xy()
        p = (x + math.cos(math.radians(self.a)) * rpx * 100 / fw,
             y - math.sin(math.radians(self.a)) * rpx * 100 / fh)
        if not self.cid:
            self.cid = self.e.touch_down(*p)
        else:
            self.e.touch_move(self.cid, *p)
        self.a += -self.speed * STEP if self.held("KeyClock") else self.speed * STEP

    def reset(self):
        for attr in ("cid", "anchor"):
            if getattr(self, attr):
                self.e.touch_up(getattr(self, attr))
                setattr(self, attr, 0)
        self.state = 0


# ------------------------------------------------------------------ Tilt (§3.12)
class Tilt(Control):
    """Arrow keys tilt the device: the accelerometer, not touch (gcall 0x3fc via QEMU)."""
    key_fields = ("KeyUp", "KeyDown", "KeyLeft", "KeyRight")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.max = fnum(raw, "MaxAngle", 45)
        self.speed = fnum(raw, "Speed", 45)          # degrees per second
        self.auto_reset = bool(raw.get("AutoReset", True))
        self.ax = self.ay = 0.0                      # left/right, up/down in degrees

    def _axis(self, a: float, neg: str, pos: str) -> float:
        n, p = self.held(neg), self.held(pos)
        if n != p:
            a += (self.speed if p else -self.speed) * STEP
            return max(-self.max, min(self.max, a))
        return 0.0 if self.auto_reset else a

    def tick(self, t):
        ax = self._axis(self.ax, "KeyLeft", "KeyRight")
        ay = self._axis(self.ay, "KeyDown", "KeyUp")
        if (ax, ay) != (self.ax, self.ay):
            self.ax, self.ay = ax, ay
            self.e.send_tilt(ax, ay)

    def reset(self):
        if self.ax or self.ay:
            self.ax = self.ay = 0.0
            self.e.send_tilt(0.0, 0.0)


# ------------------------------------------------------------------ SmartScroll (§3.16)
class SmartScroll(Control):
    """A list of `Swipes` (weapon slots and the like): a key picks its entry, the wheel
    steps through the enabled entries; the pick is a swipe X1,Y1 -> X2,Y2 at the entry's
    Speed (a tap when both points match). ImapSmartScroll.cpp activateSwipe 0x1404aee70."""

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.items = [w for w in raw.get("Swipes", []) if isinstance(w, dict)]
        self.item_keys = [combos(w, "Key") for w in self.items]
        self.index = 0
        self.cid = 0
        self.cur = self.target = (0.0, 0.0)
        self.speed = 0.0

    def all_combos(self):
        return [cb for ks in self.item_keys for cb in ks]

    def bound_names(self):
        return super().bound_names() | set(keys.MOUSE_WHEEL)

    def enabled(self, i: int) -> bool:
        return self.e.condition(self.items[i].get("EnableCondition", ""))

    def on_key(self, name, down):
        if not down:
            return
        for i, ks in enumerate(self.item_keys):          # the first entry bound to the key
            if any((cb == [name] and not self.e.suppressed(name)) or
                   (len(cb) > 1 and name in cb and all(k in self.e.held for k in cb))
                   for cb in ks):
                if self.enabled(i):
                    self.activate(i)
                return

    def on_mouse(self, ev, d):
        n = len(self.items)
        if ev != "wheel" or not d.get("delta") or self.cid or not n:
            return
        up = d["delta"] > 0
        steps = int(abs(d["delta"] / 120)) % n
        self.index = (self.index + (-steps if up else steps)) % n
        start = self.index
        while not self.enabled(self.index):
            self.index = (self.index + (-1 if up else 1)) % n
            if self.index == start:
                return
        self.activate(self.index)

    def activate(self, i: int) -> None:
        w = self.items[i]
        self.index = i
        self.cur = (fnum(w, "X1", 50.0), fnum(w, "Y1", 50.0))
        self.target = (fnum(w, "X2", self.cur[0]), fnum(w, "Y2", self.cur[1]))
        self.speed = fnum(w, "Speed", 30.0)
        if self.cid:
            self.e.touch_move(self.cid, *self.cur)
        else:
            self.cid = self.e.touch_down(*self.cur)

    def tick(self, t):
        if not self.cid:
            return
        dx, dy = self.target[0] - self.cur[0], self.target[1] - self.cur[1]
        dist = math.hypot(dx, dy)
        if dist >= 0.01:
            sx, sy = dx / dist * self.speed * STEP, dy / dist * self.speed * STEP
            if abs(sx) <= abs(dx) and abs(sy) <= abs(dy):
                self.cur = (self.cur[0] + sx, self.cur[1] + sy)
                self.e.touch_move(self.cid, *self.cur)
                return
        self.reset()                                   # arrived (or a tap): lift

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0


# ------------------------------------------------------------------ GenericSwipe (§3.16)
class GenericSwipe(Control):
    """HD-Player's position-calibration tool (ImapGenericSwipe.cpp onKeyEvent 0x1404ad390):
    Activate holds a touch at X,Y with the native mouse touch off; Left/Right/Up/Down nudge
    it by 0.5 % (Diff1/2/3: 1, 1.5, 2); Deactivate lifts it. (HD-Player also logs mouse/touch
    offsets to InputMapper/extraData.txt on Deactivate; not reproduced.)"""
    key_fields = ("Activate", "Deactivate", "Left", "Right", "Up", "Down",
                  "Diff1", "Diff2", "Diff3")

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.cid = 0
        self.step = 0.5
        self.p = self.xy()

    def on_key(self, name, down):
        if not down:
            return
        hit = {f: self.relevant(f, name, down) for f in self.key_fields}
        if hit["Activate"] and not self.cid:
            self.p = self.xy()
            self.cid = self.e.touch_down(*self.p)
            self.e.native_touch = False
        if hit["Deactivate"] and self.cid:
            self.reset()
            self.step = 0.5
        if not self.cid:
            return
        for f, v in (("Diff1", 1.0), ("Diff2", 1.5), ("Diff3", 2.0)):
            if hit[f]:
                self.step = v
        x, y = self.p
        if hit["Left"]:
            x -= self.step
        elif hit["Right"]:
            x += self.step
        elif hit["Up"]:
            y -= self.step
        elif hit["Down"]:
            y += self.step
        else:
            return
        self.p = (x, y)
        self.e.touch_move(self.cid, x, y)

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0
            self.e.native_touch = True


class Callback(Control):
    """Id/Action for BlueStacks' UI layer; HD-Player's runtime overrides no hook."""


# ------------------------------------------------------------------ wheel family (§3.13-15)
class Scroll(Control):
    """Mouse wheel: a vertical swipe at the cursor, Amplitude % per notch."""

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.speed = fnum(raw, "Speed", 100)
        self.amp = fnum(raw, "Amplitude", 20)
        self.cid = 0
        self.cur = (0.0, 0.0)
        self.target_y = 0.0
        self.done_at = 0.0

    def bound_names(self):
        return set(keys.MOUSE_WHEEL)

    def wants_hover(self):
        return True

    def on_mouse(self, ev, d):
        if ev != "wheel" or not self.e.native_touch or self.e.zooming:
            return
        dist = max(5.0, min(100.0, abs(d["delta"] / 120) * self.amp))
        cx, cy = self.e.cursor
        up = d["delta"] > 0
        if self.cid:                                 # more notches extend the swipe
            self.target_y = max(0, min(100, self.target_y + (dist if up else -dist)))
            return
        if up:
            start, target = cy, min(100.0, cy + dist)
        else:
            start, target = (cy, cy - dist) if cy - dist >= 0 else (dist, 0.0)
        self.cur = (cx, start)
        self.target_y = target
        self.cid = self.e.touch_down(*self.cur)
        self.done_at = 0.0

    def tick(self, t):
        if not self.cid:
            return
        if self.done_at:
            if t - self.done_at >= 0.05:
                self.reset()
            return
        self.cur, arrived = toward(self.cur, (self.cur[0], self.target_y), self.speed)
        self.e.touch_move(self.cid, *self.cur)
        if arrived:
            self.done_at = t

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0


class MouseZoom(Control):
    """Wheel with KeyModifier held: two fingers spread (wheel up) / pinch (down)."""
    key_fields = ("KeyModifier",)

    def __init__(self, e, raw):
        super().__init__(e, raw)
        if not self.binds["KeyModifier"]:
            self.binds["KeyModifier"] = [["Ctrl"]]
        self.c1 = (fnum(raw, "X1", 40), fnum(raw, "Y1", 50))
        self.c2 = (fnum(raw, "X2", 60), fnum(raw, "Y2", 50))
        if self.c1[0] < 0:
            self.c1, self.c2 = (40.0, 50.0), (60.0, 50.0)
        self.speed = fnum(raw, "Speed", 40)
        self.amp = fnum(raw, "Amplitude", 25)
        self.ids = [0, 0]
        self.p = [self.c1, self.c2]
        self.left = 0.0              # remaining spread (+) / pinch (-) in percent

    def bound_names(self):
        return super().bound_names() | set(keys.MOUSE_WHEEL)

    def on_mouse(self, ev, d):
        if ev != "wheel" or not self.held("KeyModifier"):
            return
        if not self.ids[0]:
            self.p = [self.c1, self.c2]
            self.ids = [self.e.touch_down(*self.p[0]), self.e.touch_down(*self.p[1])]
        self.left += (1 if d["delta"] > 0 else -1) * abs(d["delta"] / 120) * self.amp
        self.e.zooming = True

    def on_key(self, name, down):
        if self.relevant("KeyModifier", name, down) and not self.held("KeyModifier"):
            self.reset()

    def tick(self, t):
        if not self.ids[0] or abs(self.left) < 0.01:
            return
        step = min(abs(self.left), self.speed * STEP) * (1 if self.left > 0 else -1)
        self.left -= step
        (x1, y1), (x2, y2) = self.p
        dx, dy = x2 - x1, y2 - y1
        m = math.hypot(dx, dy) or 1.0
        if step < 0 and m <= 5:
            self.left = 0.0
            return
        ux, uy = dx / m * step / 2, dy / m * step / 2
        self.p = [(x1 - ux, y1 - uy), (x2 + ux, y2 + uy)]
        self.e.touch_move(self.ids[0], *self.p[0])
        self.e.touch_move(self.ids[1], *self.p[1])

    def reset(self):
        for i in self.ids:
            if i:
                self.e.touch_up(i)
        self.ids = [0, 0]
        self.left = 0.0
        self.e.zooming = False


class EdgeScroll(Control):
    """Cursor within the margin of an edge: drag a finger from (X, Y) toward that edge."""

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.vel = (fnum(raw, "XVelocity", 100), fnum(raw, "YVelocity", 100))
        self.margin = (fnum(raw, "XActiveMargin", 3), fnum(raw, "YActiveMargin", 3))
        self.speedup = fnum(raw, "SpeedUpFactor", 2.0)
        self.reset_delay = fnum(raw, "ResetDelay", 150) / 1000.0
        self.speedup_wait = fnum(raw, "SpeedUpWaitTime", 200) / 1000.0
        self.on = bool(raw.get("EdgeScrollEnabled", True))
        self.cid = 0
        self.cur = self.xy()
        self.t0 = 0.0
        self.wait_until = 0.0

    def wants_hover(self):
        return self.on

    def tick(self, t):
        if not self.on or not self.e.native_touch:
            return
        cx, cy = self.e.cursor
        at_edge = cx <= self.margin[0] or cx >= 100 - self.margin[0] or \
            cy <= self.margin[1] or cy >= 100 - self.margin[1]
        if not at_edge:
            self.reset()
            return
        if t < self.wait_until:
            return
        if not self.cid:
            self.cur = self.xy()
            self.cid = self.e.touch_down(*self.cur)
            self.t0 = t
            return
        dx, dy = cx - 50, cy - 50
        m = math.hypot(dx, dy) or 1.0
        k = self.speedup if t - self.t0 >= self.speedup_wait else 1.0
        x = self.cur[0] + dx / m * self.vel[0] * k * STEP
        y = self.cur[1] + dy / m * self.vel[1] * k * STEP
        if not (2 <= x <= 98 and 2 <= y <= 98):
            self.e.touch_up(self.cid)                # out of room: re-grip after a pause
            self.cid = 0
            self.wait_until = t + self.reset_delay
            return
        self.cur = (x, y)
        self.e.touch_move(self.cid, x, y)

    def reset(self):
        if self.cid:
            self.e.touch_up(self.cid)
            self.cid = 0


# ------------------------------------------------------------------ key routing (§3.17)
class State(Control):
    """Sets the variable `Name` for EnableConditions: while held ("Modifier") or toggled
    on each press ("Toggle"), optionally `Delay` ms later."""
    key_fields = ("Key",)

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.name = raw.get("Name") or ""
        self.toggle = (raw.get("Model") or "Modifier").lower() == "toggle"
        self.delay = fnum(raw, "Delay", 0) / 1000.0
        self.pending: list[tuple[float, bool]] = []

    def on_key(self, name, down):
        if not self.name or not self.relevant("Key", name, down):
            return
        held = self.held("Key")
        if self.toggle:
            if not down:
                return
            v = not self.e.states.get(self.name, False)
        else:
            v = held
        if self.delay:
            self.pending.append((now() + self.delay, v))
        else:
            self._apply(v)

    def _apply(self, v: bool) -> None:
        self.e.states[self.name] = v
        self.e.refresh_enabled()

    def tick(self, t):
        while self.pending and self.pending[0][0] <= t:
            self._apply(self.pending.pop(0)[1])

    def set_active(self, on):
        self.active = True            # a State control keeps working when "disabled"


class Alias(Control):
    """A key of KeyIn re-injects the same event as the first key of KeyOut."""
    key_fields = ("KeyIn", "KeyOut")

    def bound_names(self):
        return {k for cb in self.binds.get("KeyIn", ()) for k in cb}

    def on_key(self, name, down):
        if self.relevant("KeyIn", name, down) and self.binds.get("KeyOut"):
            out = self.binds["KeyOut"][0][0]
            if out != name:
                self.e.inject_key(out, down)


class SendOriginalKeys(Control):
    """Mapped keys reach Android as well as driving their controls."""

    def on_key(self, name, down):
        if keys.is_keyboard(name):
            self.e.send_key_guest(name, down)


class DontForwardKeys(Control):
    """Every key counts as mapped: nothing reaches Android except via SendOriginalKeys."""

    def captures_all_keys(self):
        return True


class LockCursor(Control):
    """Each press of Key toggles the native mouse touch off/on (cursor lock)."""
    key_fields = ("Key",)

    def on_key(self, name, down):
        if self.relevant("Key", name, down) and down:
            self.e.native_touch = not self.e.native_touch


class NativeCursor(Control):
    """The host mouse as an Android mouse (ImapNativeCursor.cpp, §3.16): while active, the
    guest's "Bluestacks Mouse Device" (gcall 0x3f8) gets every move, button and wheel as
    gcallSendMouseDataRpc (0x3fd: action, x, y, wheel; x/y 0..32767 of the view). Key toggles
    relative mode (Tweaks bit 1): the pointer is locked and motion is sent as offsets
    divided by RelativeNorm."""
    key_fields = ("Key",)
    # HD-Player event type -> action (0x140300820): move 0, L 1/2, M 3/4, R 5/6
    _BTN = {1: (1, 2), 4: (3, 4), 2: (5, 6)}

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.norm = fnum(raw, "RelativeNorm", 1.0) or 1.0
        self.show = bool(raw.get("ShowNativeMousePointer", False))
        self.rel_allowed = bool(self.tweaks & 2)
        self.relative = False
        self.on = False

    def mouse_buttons(self):
        return keys.MOUSE_BUTTONS["MouseLButton"] | keys.MOUSE_BUTTONS["MouseRButton"] |             keys.MOUSE_BUTTONS["MouseMButton"]

    def bound_names(self):
        return super().bound_names() | set(keys.MOUSE_WHEEL)

    def wants_hover(self):
        return True

    def wants_lock(self):
        return self.relative

    def _device(self, on: bool) -> None:
        if on != self.on:
            self.on = on
            if on:
                self.e.native_mouse(0 if self.relative else 1)
            self.e.ch.send(f"g 1034 {int(on and self.show and not self.relative)}")
            self.e.native_touch = not on

    def set_active(self, on):
        super().set_active(on)
        self._device(on and self.e.enabled)

    def _send(self, action: int, x: float, y: float, wheel: int = 0) -> None:
        self.e.ch.send(f"g 1021 {action} {int(x * 327.67)} {int(y * 327.67)} {wheel}")

    def on_key(self, name, down):
        if down and self.rel_allowed and self.relevant("Key", name, down):
            self.relative = not self.relative
            self.on = False                              # re-create in the other mode
            self._device(True)

    def on_mouse(self, ev, d):
        if not self.on:
            self._device(True)
        cx, cy = self.e.cursor
        if ev == "rel" and self.relative:
            vw, vh = self.e.view_px
            self._send(0, d.get("dx", 0) * 100 / vw / self.norm, d.get("dy", 0) * 100 / vh / self.norm)
        elif ev == "move" and not self.relative:
            self._send(0, cx, cy)
        elif ev in ("ldown", "lup"):
            self._send(self._BTN[1][ev == "lup"], cx, cy)
        elif ev in ("down", "up") and d.get("button") in self._BTN:
            self._send(self._BTN[d["button"]][ev == "up"], cx, cy)
        elif ev == "wheel":
            self._send(0, cx, cy, int(d.get("delta", 0) / 120))     # [I] wheel notches

    def reset(self):
        self.relative = False
        self._device(False)


# ------------------------------------------------------------------ Script (§3.9)
_SPLIT = re.compile(r"\s+")


def _arith(src: str, x: float, y: float) -> float:
    """dtouch coordinates: + - * / ^ ( ) over numbers and x, y (cursor percent)."""
    toks = re.findall(r"\d+\.?\d*|[xy]|[-+*/^()]", src.replace(" ", ""))
    pos = 0

    def atom():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t == "(":
            v = expr_()
            pos += 1
            return v
        if t == "-":
            return -atom()
        return x if t == "x" else y if t == "y" else float(t)

    def power():
        nonlocal pos
        v = atom()
        while pos < len(toks) and toks[pos] == "^":
            pos += 1
            v = v ** atom()
        return v

    def term():
        nonlocal pos
        v = power()
        while pos < len(toks) and toks[pos] in "*/":
            op = toks[pos]
            pos += 1
            r = power()
            v = v * r if op == "*" else (v / r if r else 0.0)
        return v

    def expr_():
        nonlocal pos
        v = term()
        while pos < len(toks) and toks[pos] in "+-":
            op = toks[pos]
            pos += 1
            v = v + term() if op == "+" else v - term()
        return v

    return expr_() if toks else 0.0


class Script(Control):
    """Commands in three sections: on press (to `onrelease` / `onmousemove`), on each
    mouse event while held (after `onmousemove`), and on release (after `onrelease`)."""
    key_fields = ("Key",)

    def __init__(self, e, raw):
        super().__init__(e, raw)
        self.cmds = []
        for line in raw.get("Commands") or []:
            parts = [p for p in _SPLIT.split(str(line).strip()) if p]
            if parts:
                self.cmds.append((parts[0].lower(), parts[1:], str(line).strip()))
        idx = {op: i for i, (op, _, _) in reversed(list(enumerate(self.cmds)))}
        self.i_mouse = idx.get("onmousemove")
        self.i_release = idx.get("onrelease")
        self.sec: list[Optional[Iterator]] = [None, None, None]
        self.wake = [0.0, 0.0, 0.0]
        self.contacts: dict[int, list[int]] = {0: [], 1: [], 2: []}
        self.touch_ids: list[int] = []
        self.pressed = False
        self.finished = False
        self.pend = False
        self.uses_mouse = any(op in ("onmousemove", "dtouch") for op, _, _ in self.cmds)

    def wants_hover(self):
        return self.uses_mouse

    def _end(self, i: int) -> int:
        """Index where section starting at i stops (next marker)."""
        while i < len(self.cmds) and self.cmds[i][0] not in ("onrelease", "onmousemove"):
            i += 1
        return i

    def _run(self, sec: int, start: int) -> Iterator[float]:
        """Yields sleep seconds (0 = next tick, -1 = wait for a condition)."""
        e = self.e
        stack: list[list[int]] = []
        i = start
        end = self._end(start)
        while i < end:
            op, a, line = self.cmds[i]
            i += 1
            nums = []
            for t in a:
                try:
                    nums.append(float(t))
                except ValueError:
                    pass
            if op in ("keydown", "keyup"):
                if a:
                    e.inject_key(keys.canon(a[0]), op == "keydown")
            elif op in ("keydown2", "keyup2"):
                if a:
                    n = keys.canon(a[0])
                    (e.held.add if op == "keydown2" else e.held.discard)(n)
                    e.send_key_guest(n, op == "keydown2")
            elif op == "mousedown" and len(nums) >= 2:
                if not self.contacts[sec]:
                    self.contacts[sec].append(e.touch_down(nums[0], nums[1]))
            elif op == "mousemove" and len(nums) >= 2 and self.contacts[sec]:
                e.touch_move(self.contacts[sec][0], nums[0], nums[1])
            elif op == "mouseup":
                for cid in self.contacts[sec]:
                    e.touch_up(cid)
                self.contacts[sec] = []
            elif op == "mousewheel" and len(nums) >= 3:
                e.inject_wheel(nums[0], nums[1], int(nums[2]))
            elif op in ("touch", "dtouch"):
                pts = []
                if op == "touch":
                    pts = list(zip(nums[0::2], nums[1::2]))
                else:
                    cx, cy = e.cursor
                    vals = [_arith(t, cx, cy) for t in a]
                    pts = list(zip(vals[0::2], vals[1::2]))
                if not pts:
                    for cid in self.touch_ids:
                        e.touch_up(cid)
                    self.touch_ids = []
                for k, (px, py) in enumerate(pts):
                    if k < len(self.touch_ids):
                        e.touch_move(self.touch_ids[k], px, py)
                    else:
                        self.touch_ids.append(e.touch_down(px, py))
                for cid in self.touch_ids[len(pts):] if pts else []:
                    e.touch_up(cid)
                if pts:
                    self.touch_ids = self.touch_ids[:len(pts)]
            elif op == "text":
                e.text.emit(line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else "")
            elif op == "wait":
                if a and a[0].lower() == "onkeypressed":
                    while self.pressed:
                        yield -1
                elif a and a[0].lower() == "onmousemove":
                    while self.sec[1] is not None:
                        yield -1
                elif a and a[0].lower() == "frame":
                    yield 0
                else:
                    yield (nums[0] if nums else 0) / 1000.0
            elif op == "tap":
                ms = nums[-1] if len(nums) % 2 == 1 else 0
                pts = list(zip(nums[0::2], nums[1::2]))[:16]
                ids = [e.touch_down(px, py) for px, py in pts]
                yield ms / 1000.0
                for cid in ids:
                    e.touch_up(cid)
            elif op == "swipe" and len(nums) >= 5:
                ms = nums[-1]
                pts = list(zip(nums[0:-1:2], nums[1:-1:2]))
                cid = e.touch_down(*pts[0])
                cur = pts[0]
                seg_ms = ms / max(1, len(pts) - 1)
                for nxt in pts[1:]:
                    seg = math.hypot(nxt[0] - cur[0], nxt[1] - cur[1])
                    speed = seg * 1000 / seg_ms if seg_ms else 1e9
                    arrived = False
                    while not arrived:
                        cur, arrived = toward(cur, nxt, speed)
                        e.touch_move(cid, *cur)
                        yield 0
                e.touch_up(cid)
            elif op == "loop":
                stack.append([i, int(nums[0]) if nums else -1])
            elif op == "loopend" and stack:
                top = stack[-1]
                if top[1] == 1:
                    stack.pop()
                else:
                    if top[1] > 0:
                        top[1] -= 1
                    i = top[0]
                    yield 0
            elif op in ("entershoot", "exitshoot", "toggleshoot"):
                pan = e.pan()
                if pan is not None:
                    on = pan.state not in (0, 5, 6)
                    if op == "entershoot" and not on or op == "toggleshoot" and not on:
                        pan._activate()
                    elif op == "exitshoot" and on or op == "toggleshoot" and on:
                        pan._stop(0)
            elif op == "abort":
                which = 1 if a and a[0].lower() == "onmousemove" else 0
                self.sec[which] = None
                if which == sec:
                    return

    def _start_sec(self, sec: int, start: Optional[int]) -> None:
        if start is None or start > len(self.cmds):
            return
        self.sec[sec] = self._run(sec, start)
        self.wake[sec] = 0.0

    def on_key(self, name, down):
        if not self.relevant("Key", name, down):
            return
        if self.held("Key"):
            if self.pressed or self.sec[0] is not None:
                return                           # pressing again does not restart
            self.pressed = True
            self.finished = False
            if self.exclusive_on:
                self.pend = True
                self.excl_begin()
            else:
                self._start_sec(0, 0)
        elif self.pressed:
            self.pressed = False
            self.pend = False
            if self.i_release is not None:
                self._start_sec(2, self.i_release + 1)

    def on_mouse(self, ev, d):
        if self.pressed and self.i_mouse is not None and self.sec[1] is None:
            self._start_sec(1, self.i_mouse + 1)

    def tick(self, t):
        if self.pend and self.excl_due(t):
            self.pend = False
            self._start_sec(0, 0)
        for s in range(3):
            g = self.sec[s]
            while g is not None and t >= self.wake[s]:
                try:
                    d = next(g)
                except StopIteration:
                    self.sec[s] = None
                    if s == 0:
                        self.finished = True
                    break
                if d < 0:
                    self.wake[s] = t                 # condition: re-check next tick
                    break
                self.wake[s] = t + d
                break
        if self.finished and all(g is None for g in self.sec) and not self.pressed:
            self._release_contacts()

    def _release_contacts(self) -> None:
        for s in self.contacts:
            for cid in self.contacts[s]:
                self.e.touch_up(cid)
            self.contacts[s] = []
        for cid in self.touch_ids:
            self.e.touch_up(cid)
        self.touch_ids = []

    def reset(self):
        self.sec = [None, None, None]
        self.pressed = False
        self.pend = False
        self._release_contacts()
        self.excl_cancel()


TYPES = {
    "Tap": Tap, "TapRepeat": TapRepeat, "Dpad": Dpad, "Pan": Pan, "Swipe": Swipe,
    "Zoom": Zoom, "MOBADpad": MOBADpad, "MOBASkill": MOBASkill, "FreeLook": FreeLook,
    "Rotate": Rotate, "Tilt": Tilt, "SmartScroll": SmartScroll, "Callback": Callback,
    "GenericSwipe": GenericSwipe, "Scroll": Scroll, "MouseZoom": MouseZoom, "EdgeScroll": EdgeScroll,
    "State": State, "Alias": Alias, "SendOriginalKeys": SendOriginalKeys,
    "DontForwardKeys": DontForwardKeys, "LockCursor": LockCursor, "NativeCursor": NativeCursor,
    "Script": Script,
}
