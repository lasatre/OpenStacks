"""Gamepads through SDL2's GameController API, loaded with ctypes.

SDL maps each controller to the standard Xbox layout (SDL's controller database), so a
DualShock, Switch Pro or generic pad all report the A/B/X/Y, shoulder, trigger, stick
and D-pad names BlueStacks binds (GamepadA, GamepadLTrigger, GamepadLStickUp, ...).
BlueStacks' own gamepad.db layout table is not needed.

Library: SDL2.dll from the pysdl2-dll wheel (Windows), else the system libSDL2 (Linux;
the AppImage bundles one). Without SDL2 gamepads are simply unavailable.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from typing import Optional

_SDL_INIT_JOYSTICK = 0x200
_SDL_INIT_GAMECONTROLLER = 0x2000

# SDL_GameControllerButton -> BlueStacks name
_BUTTONS = {0: "GamepadA", 1: "GamepadB", 2: "GamepadX", 3: "GamepadY", 4: "GamepadBack",
            6: "GamepadStart", 7: "GamepadLeftThumb", 8: "GamepadRightThumb",
            9: "GamepadLeftShoulder", 10: "GamepadRightShoulder", 11: "GamepadDpadUp",
            12: "GamepadDpadDown", 13: "GamepadDpadLeft", 14: "GamepadDpadRight"}
_AX_LX, _AX_LY, _AX_RX, _AX_RY, _AX_LT, _AX_RT = range(6)

DEADZONE = 0.15                      # stick radius treated as centred
STICK_PRESS = 0.5                    # a stick direction counts as a "key" past this
TRIGGER_PRESS = 0.3


def _load() -> Optional[ctypes.CDLL]:
    names = []
    bundle = getattr(sys, "_MEIPASS", "")        # the frozen build carries its own copy
    if sys.platform == "win32":
        if bundle:
            names.append(os.path.join(bundle, "SDL2.dll"))
        try:
            import sdl2dll                       # pysdl2-dll wheel
            names.append(os.path.join(sdl2dll.get_dllpath(), "SDL2.dll"))
        except Exception:
            pass
        names.append("SDL2.dll")
    else:
        if bundle:
            names.append(os.path.join(bundle, "libSDL2-2.0.so.0"))
        names += ["libSDL2-2.0.so.0", ctypes.util.find_library("SDL2") or ""]
    for n in names:
        if not n:
            continue
        try:
            return ctypes.CDLL(n)
        except OSError:
            continue
    return None


class Gamepads:
    """Polls every connected controller; poll() returns the changes since last time.

    Events: ("button", name, down) for buttons, triggers and stick directions
    (GamepadLStickUp ...), and ("stick", "LeftStick" | "RightStick", x, y) with
    x, y in -1..1 (deadzone applied, y down = +).
    """

    def __init__(self):
        self.sdl = _load()
        self.error = "" if self.sdl else "SDL2 library not found"
        self._pads: dict[int, ctypes.c_void_p] = {}
        self._state: dict[str, bool] = {}
        self._sticks: dict[str, tuple[float, float]] = {}
        self._scan_countdown = 0
        if not self.sdl:
            return
        s = self.sdl
        s.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        s.SDL_Init.argtypes = [ctypes.c_uint32]
        s.SDL_GameControllerOpen.restype = ctypes.c_void_p
        s.SDL_GameControllerOpen.argtypes = [ctypes.c_int]
        s.SDL_GameControllerGetAttached.argtypes = [ctypes.c_void_p]
        s.SDL_GameControllerClose.argtypes = [ctypes.c_void_p]
        s.SDL_GameControllerGetButton.argtypes = [ctypes.c_void_p, ctypes.c_int]
        s.SDL_GameControllerGetButton.restype = ctypes.c_uint8
        s.SDL_GameControllerGetAxis.argtypes = [ctypes.c_void_p, ctypes.c_int]
        s.SDL_GameControllerGetAxis.restype = ctypes.c_int16
        s.SDL_GameControllerName.argtypes = [ctypes.c_void_p]
        s.SDL_GameControllerName.restype = ctypes.c_char_p
        s.SDL_JoystickGetDeviceInstanceID.argtypes = [ctypes.c_int]
        s.SDL_GetError.restype = ctypes.c_char_p
        # no SDL window of ours: keep reading pads while another window has focus
        s.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        if s.SDL_Init(_SDL_INIT_JOYSTICK | _SDL_INIT_GAMECONTROLLER) != 0:
            self.error = "SDL_Init: " + (s.SDL_GetError() or b"").decode(errors="replace")
            self.sdl = None

    @property
    def available(self) -> bool:
        return self.sdl is not None

    def names(self) -> list[str]:
        if not self.sdl:
            return []
        return [(self.sdl.SDL_GameControllerName(p) or b"?").decode(errors="replace")
                for p in self._pads.values()]

    def _scan(self) -> None:
        s = self.sdl
        for iid, pad in list(self._pads.items()):
            if not s.SDL_GameControllerGetAttached(pad):
                s.SDL_GameControllerClose(pad)
                del self._pads[iid]
        for i in range(s.SDL_NumJoysticks()):
            if not s.SDL_IsGameController(i):
                continue
            iid = s.SDL_JoystickGetDeviceInstanceID(i)
            if iid in self._pads:
                continue
            pad = s.SDL_GameControllerOpen(i)
            if pad:
                self._pads[iid] = pad

    def poll(self) -> list[tuple]:
        if not self.sdl:
            return []
        s = self.sdl
        s.SDL_PumpEvents()
        s.SDL_FlushEvents(0, 0xFFFF)     # we read states, not SDL's event queue
        if self._scan_countdown <= 0:    # hot-plug about once a second at 250 Hz
            self._scan()
            self._scan_countdown = 250
        self._scan_countdown -= 1

        btn: dict[str, bool] = {}
        axes = [0.0] * 6
        for pad in self._pads.values():  # several pads act as one (BlueStacks: player 1)
            for b, name in _BUTTONS.items():
                if s.SDL_GameControllerGetButton(pad, b):
                    btn[name] = True
            for a in range(6):
                v = s.SDL_GameControllerGetAxis(pad, a) / 32767.0
                if abs(v) > abs(axes[a]):
                    axes[a] = v

        def dz(x, y):
            m = (x * x + y * y) ** 0.5
            if m < DEADZONE:
                return 0.0, 0.0
            k = min(1.0, (m - DEADZONE) / (1 - DEADZONE)) / m
            return x * k, y * k

        sticks = {"LeftStick": dz(axes[_AX_LX], axes[_AX_LY]),
                  "RightStick": dz(axes[_AX_RX], axes[_AX_RY])}
        for side, (x, y) in (("L", sticks["LeftStick"]), ("R", sticks["RightStick"])):
            btn[f"Gamepad{side}StickUp"] = y < -STICK_PRESS
            btn[f"Gamepad{side}StickDown"] = y > STICK_PRESS
            btn[f"Gamepad{side}StickLeft"] = x < -STICK_PRESS
            btn[f"Gamepad{side}StickRight"] = x > STICK_PRESS
        btn["GamepadLTrigger"] = axes[_AX_LT] > TRIGGER_PRESS
        btn["GamepadRTrigger"] = axes[_AX_RT] > TRIGGER_PRESS

        out: list[tuple] = []
        for name in set(btn) | set(self._state):
            v = btn.get(name, False)
            if v != self._state.get(name, False):
                out.append(("button", name, v))
                self._state[name] = v
        for stick, xy in sticks.items():
            if xy != self._sticks.get(stick, (0.0, 0.0)):
                out.append(("stick", stick, xy[0], xy[1]))
                self._sticks[stick] = xy
        return out

    def close(self) -> None:
        if self.sdl:
            for pad in self._pads.values():
                self.sdl.SDL_GameControllerClose(pad)
            self._pads.clear()
