"""BlueStacks key names <-> Linux evdev key codes.

BlueStacks names keys after Windows virtual-key codes (HD-Player.exe's key-name table, in
VK order: "Backspace", "Tab", "Enter", "Shift", "Ctrl", "Alt", "Esc", "Space", "PgUp", "PgDn",
..., "0".."9", "A".."Z", "Num0".."Num9", "Mul", "Add", "Sub", "Decimal", "Div", "F1".."F24",
"Oem1".., then the mouse and gamepad names). Names are matched without regard to case
(scripts write "NUM1" for "Num1"). QEMU reports host keys as evdev codes, so everything here
is keyed by evdev; a name can cover several codes (both Ctrl keys are "Ctrl").
"""
from __future__ import annotations

# evdev codes (linux/input-event-codes.h)
_EV: dict[str, tuple[int, ...]] = {
    "Esc": (1,), "Backspace": (14,), "Tab": (15,), "Enter": (28, 96),
    "Ctrl": (29, 97), "Shift": (42, 54), "Alt": (56, 100), "Space": (57,),
    "CapsLock": (58,), "NumLock": (69,), "ScrlLock": (70,), "Pause": (119,),
    "Snapshot": (99,), "Print": (99,), "Insert": (110,), "Delete": (111,),
    "Home": (102,), "End": (107,), "PgUp": (104,), "PgDn": (109,),
    "Up": (103,), "Down": (108,), "Left": (105,), "Right": (106,),
    "WinCmd": (125, 126), "Apps": (127,),
    "LShift": (42,), "RShift": (54,), "LCtrl": (29,), "RCtrl": (97,),
    "LAlt": (56,), "RAlt": (100,),
    # OEM keys, US layout (VK_OEM_*)
    "Oem1": (39,),            # ;:
    "OemPlus": (13,),         # =+
    "OemComma": (51,),        # ,<
    "OemMinus": (12,),        # -_
    "OemPeriod": (52,),       # .>
    "Oem2": (53,),            # /?
    "Oem3": (41,),            # `~
    "Oem4": (26,),            # [{
    "Oem5": (43,),            # \|
    "Oem6": (27,),            # ]}
    "Oem7": (40,),            # '"
    "OemBackSlash": (86,),    # the 102nd key
    # keypad
    "Num0": (82,), "Num1": (79,), "Num2": (80,), "Num3": (81,), "Num4": (75,),
    "Num5": (76,), "Num6": (77,), "Num7": (71,), "Num8": (72,), "Num9": (73,),
    "Mul": (55,), "Add": (78,), "Sub": (74,), "Decimal": (83,), "Div": (98,),
}
for _i, _c in enumerate((11, 2, 3, 4, 5, 6, 7, 8, 9, 10)):          # "0".."9"
    _EV[str(_i)] = (_c,)
for _ch, _c in zip("QWERTYUIOP", range(16, 26)):
    _EV[_ch] = (_c,)
for _ch, _c in zip("ASDFGHJKL", range(30, 39)):
    _EV[_ch] = (_c,)
for _ch, _c in zip("ZXCVBNM", range(44, 51)):
    _EV[_ch] = (_c,)
for _i, _c in enumerate(list(range(59, 69)) + [87, 88], start=1):  # F1..F12
    _EV[f"F{_i}"] = (_c,)
for _i, _c in enumerate(range(183, 195), start=13):                 # F13..F24
    _EV[f"F{_i}"] = (_c,)

# mouse names (QEMU reports OST_BTN_* bits; the wheel is a press + release)
MOUSE_BUTTONS = {"MouseLButton": 1, "MouseRButton": 2, "MouseMButton": 4,
                 "MouseXButton1": 8, "MouseXButton2": 16}
MOUSE_WHEEL = ("MouseWheelUp", "MouseWheelDown")
OST_BTN_WHEEL, OST_BTN_HOVER = 32, 64

GAMEPAD_BUTTONS = (
    "GamepadA", "GamepadB", "GamepadX", "GamepadY", "GamepadBack", "GamepadStart",
    "GamepadLeftThumb", "GamepadRightThumb", "GamepadLeftShoulder", "GamepadRightShoulder",
    "GamepadDpadUp", "GamepadDpadDown", "GamepadDpadLeft", "GamepadDpadRight",
    "GamepadLTrigger", "GamepadRTrigger",
    "GamepadLStickUp", "GamepadLStickDown", "GamepadLStickLeft", "GamepadLStickRight",
    "GamepadRStickUp", "GamepadRStickDown", "GamepadRStickLeft", "GamepadRStickRight",
)

_LOWER = {k.lower(): k for k in list(_EV) + list(MOUSE_BUTTONS) + list(MOUSE_WHEEL)
          + list(GAMEPAD_BUTTONS)}
_BY_CODE: dict[int, list[str]] = {}
for _n, _codes in _EV.items():
    if _n.startswith(("L", "R")) and _n[1:] in ("Shift", "Ctrl", "Alt"):
        continue                     # "Ctrl" etc. are the names events are reported as
    for _c in _codes:
        _BY_CODE.setdefault(_c, []).append(_n)


def canon(name: str) -> str:
    """The canonical spelling of a key name ("NUM1" -> "Num1"); unknown names unchanged.
    A gamepad name may carry a player suffix ("GamepadA.2"); it is dropped."""
    name = (name or "").strip()
    base = name.split(".", 1)[0] if name.lower().startswith("gamepad") else name
    return _LOWER.get(base.lower(), base)


def split_combo(spec: str) -> list[str]:
    """'Ctrl + Q' -> ['Ctrl', 'Q'] (a binding may name a modifier combination)."""
    return [canon(p) for p in (spec or "").split("+") if p.strip()]


def evdev_codes(name: str) -> tuple[int, ...]:
    return _EV.get(canon(name), ())


def names_for_code(code: int) -> list[str]:
    return _BY_CODE.get(code, [])


def is_keyboard(name: str) -> bool:
    return canon(name) in _EV


def is_mouse(name: str) -> bool:
    n = canon(name)
    return n in MOUSE_BUTTONS or n in MOUSE_WHEEL


def is_gamepad(name: str) -> bool:
    return canon(name).startswith("Gamepad")


def label(name: str) -> str:
    """Short text for the on-screen overlay, like BlueStacks' key badges."""
    n = canon(name)
    short = {"MouseLButton": "LMB", "MouseRButton": "RMB", "MouseMButton": "MMB",
             "MouseXButton1": "X1", "MouseXButton2": "X2", "MouseWheelUp": "Wheel↑",
             "MouseWheelDown": "Wheel↓", "Space": "Space", "Oem1": ";", "Oem2": "/",
             "Oem3": "`", "Oem4": "[", "Oem5": "\\", "Oem6": "]", "Oem7": "'",
             "OemPlus": "=", "OemMinus": "-", "OemComma": ",", "OemPeriod": ".",
             "Up": "↑", "Down": "↓", "Left": "←", "Right": "→"}
    if n in short:
        return short[n]
    if n.startswith("Gamepad"):
        return n[len("Gamepad"):]
    return n


# Windows scan codes of E0-extended keys (Qt: nativeScanCode | 0x100) -> evdev
_WIN_EXT = {0x1C: 96, 0x1D: 97, 0x35: 98, 0x37: 99, 0x38: 100, 0x47: 102, 0x48: 103,
            0x49: 104, 0x4B: 105, 0x4D: 106, 0x4F: 107, 0x50: 108, 0x51: 109, 0x52: 110,
            0x53: 111, 0x5B: 125, 0x5C: 126, 0x5D: 127}


def evdev_from_native(scan: int, windows: bool) -> int:
    """A Qt key event's nativeScanCode() -> evdev code (0 if unknown).
    X11 keycodes are evdev + 8. Windows set-1 scan codes equal evdev for plain keys;
    E0-extended ones (bit 0x100 in Qt's value) are mapped by table."""
    if not windows:
        return scan - 8 if scan >= 8 else 0
    if scan & 0x100 or scan & 0xE000 == 0xE000:
        return _WIN_EXT.get(scan & 0xFF, 0)
    return scan if 0 < scan < 0x80 else 0


def name_from_native(scan: int, windows: bool) -> str:
    names = names_for_code(evdev_from_native(scan, windows))
    return names[0] if names else ""


MODIFIERS = ("Ctrl", "Shift", "Alt")
