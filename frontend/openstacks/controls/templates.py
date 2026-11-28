"""What the controls editor creates: the palette, each type's fields and defaults.

The palette order and names follow BlueStacks' Controls editor. Default values are
provisional where docs/re/kmm_controls.md has not pinned them down yet. Fields are kept in
BlueStacks' order, so files we write diff cleanly against theirs.
"""
from __future__ import annotations

import copy

from .bscfg import new_control

# (palette label, control type, short help)
PALETTE = (
    ("Tap spot", "Tap", "Tap a spot on the screen with a key"),
    ("Repeated tap", "TapRepeat", "Tap a spot repeatedly while the key is held"),
    ("D-pad", "Dpad", "Move with four keys or the left stick"),
    ("Aim, pan and shoot", "Pan", "Mouse look and shoot (toggle with a key)"),
    ("Free look", "FreeLook", "Look around with keys while held"),
    ("Script", "Script", "Run a sequence of commands"),
    ("Swipe", "Swipe", "Swipe from one point to another"),
    ("Zoom", "Zoom", "Pinch to zoom in and out"),
    ("Tilt", "Tilt", "Tilt the device with keys"),
    ("MOBA D-pad", "MOBADpad", "Move the hero towards the cursor"),
    ("MOBA Skill pad", "MOBASkill", "Aim a skill with the cursor"),
    ("Rotate", "Rotate", "Rotate around a point with two keys"),
    ("Edge scroll", "EdgeScroll", "Scroll when the cursor reaches the edge"),
    ("Scroll", "Scroll", "Scroll an area with the mouse wheel"),
    ("Mouse wheel", "MouseZoom", "Zoom with the mouse wheel"),
)

_FIELDS: dict[str, dict] = {
    # BlueStacks' editor defaults (kmmGetNewControl, docs/re/kmm_controls.md §4). They
    # differ from the defaults used when a field is missing from a file (§2). Integer
    # fields stay ints: HD-Player rejects 200.0 where it reads an int.
    "Tap": {"X": 50.0, "Y": 50.0, "Key": "", "Key_alt1": ""},
    "TapRepeat": {"X": 50.0, "Y": 50.0, "Key": "", "Key_alt1": "", "Count": 5,
                  "Delay": 100, "RepeatUntilKeyUp": True},
    "Dpad": {"X": 15.0, "Y": 75.0, "KeyUp": "W", "KeyUp_alt1": "", "KeyDown": "S",
             "KeyDown_alt1": "", "KeyLeft": "A", "KeyLeft_alt1": "", "KeyRight": "D",
             "KeyRight_alt1": "", "KeySpeedModifier1": "", "KeySpeedModifier1_alt1": "",
             "KeySpeedModifier2": "", "KeySpeedModifier2_alt1": "",
             "GamepadStick": "LeftStick", "XRadius": 8.0, "XRadius1": 0.0, "XRadius2": 0.0,
             "DeadzoneRadius": 0.0, "Speed": 150.0, "ActivationSpeed": 0.0,
             "ActivationTime": 50, "DpadExpiryTime": 0, "DpadTitle": ""},
    "Pan": {"X": 70.0, "Y": 50.0, "KeyStartStop": "F1", "KeyStartStop_alt1": "",
            "KeySuspend": "Alt", "KeySuspend_alt1": "", "KeyLookAround": "",
            "KeyLookAround_alt1": "", "Sensitivity": 1.0, "SensitivityRatioY": 1.0,
            "LButtonX": 85.0, "LButtonY": 70.0, "TapX": -1.0, "TapY": -1.0,
            "LookAroundX": -1.0, "LookAroundY": -1.0, "MouseAcceleration": False,
            "IsLookAroundEnabled": False, "IsShootOnClickEnabled": True,
            "GamepadStick": "RightStick", "GamepadSensitivity": 1.0, "DeadzoneRadius": 0.0,
            "Left": 0.0, "Right": 0.0, "Top": 0.0, "Bottom": 0.0, "Speed": 0.0,
            "ActivationTimeMs": 0, "KeyAction": "MouseLButton", "WaitAfterShootTime": -1,
            "WaitAfterResetTime": -1, "CrosshairX": 50.05, "CrosshairY": 50.05,
            "IsCrosshairEnabled": False, "CrosshairType": "Cross", "CrosshairSize": 1.0,
            "CrosshairOpacity": 100.0, "CrosshairColor": "#FFFFFF", "CrosshairImage": ""},
    "FreeLook": {"X": 70.0, "Y": 50.0, "Key": "V", "Key_alt1": "", "KeyUp": "Up",
                 "KeyUp_alt1": "", "KeyDown": "Down", "KeyDown_alt1": "", "KeyLeft": "Left",
                 "KeyLeft_alt1": "", "KeyRight": "Right", "KeyRight_alt1": "",
                 "Sensitivity": 1.0, "Speed": 20.0, "DeviceType": 0, "Delay": 50,
                 "MouseAcceleration": False},
    "Script": {"X": 50.0, "Y": 50.0, "Key": "", "Key_alt1": "", "Commands": []},
    "Swipe": {"X1": 50.0, "Y1": 55.0, "X2": 50.0, "Y2": 40.0, "Speed": 200.0,
              "Hold": False, "KeyUpStop": False, "Key": "", "Key_alt1": ""},
    "Zoom": {"X1": 40.0, "Y1": 50.0, "X2": 60.0, "Y2": 50.0, "Speed": 1.0,
             "MinDistance": 5.0, "Mode": 0, "ResetDelay": 0, "Override": True,
             "KeyIn": "OemPlus", "KeyIn_alt1": "", "KeyOut": "OemMinus", "KeyOut_alt1": "",
             "KeyModifier": "", "KeyModifier_alt1": ""},
    "Tilt": {"X": 50.0, "Y": 50.0, "Radius": 10.0, "MaxAngle": 20.0, "Speed": 90.0,
             "AutoReset": True, "KeyUp": "Up", "KeyUp_alt1": "", "KeyDown": "Down",
             "KeyDown_alt1": "", "KeyLeft": "Left", "KeyLeft_alt1": "", "KeyRight": "Right",
             "KeyRight_alt1": ""},
    "MOBADpad": {"X": 15.0, "Y": 75.0, "OriginX": 50.0, "OriginY": 50.0, "CancelX": -1.0,
                 "CancelY": -1.0, "XRadius": 6.0, "DpadSpeed": 120.0, "CharSpeed": 10.0,
                 "IsCancelSkillEnabled": False, "KeyMove": "MouseRButton", "KeyCancel": "",
                 "KeyCancel_alt1": ""},
    "MOBASkill": {"X": 85.0, "Y": 70.0, "OriginX": 50.0, "OriginY": 50.0, "CancelX": -1.0,
                  "CancelY": -1.0, "YAxisRatio": 0.0, "XRadius": 8.0, "DeadZoneRadius": 0.0,
                  "Speed": 200.0, "CancelSpeed": 200.0, "MinSwipeRadius": 0.0,
                  "NoCancelOnSwitch": 0, "NoCancelTime": 0, "MinSkillTime": 0,
                  "MinSkillHoldTime": 0, "IsCancelSkillEnabled": False, "AutoAttack": False,
                  "StopMOBADpad": False, "AdvancedMode": True, "AutocastEnabled": True,
                  "KeyActivate": "", "KeyActivate_alt1": "", "KeyAutocastToggle": "",
                  "KeyAutocastToggle_alt1": "", "KeyCancel": "", "KeyCancel_alt1": "",
                  "GamepadStick": "", "SelectedSkillType": ""},
    "Rotate": {"X": 50.0, "Y": 50.0, "Tweaks": 1, "XRadius": 6.0, "StartingAngle": 90.0,
               "Speed": 60, "ActivationTime": 100, "KeyClock": "X", "KeyClock_alt1": "",
               "KeyAntiClock": "Z", "KeyAntiClock_alt1": ""},
    "EdgeScroll": {"X": 50.0, "Y": 50.0, "XVelocity": 100.0, "YVelocity": 100.0,
                   "XActiveMargin": 3.0, "YActiveMargin": 3.0, "SpeedUpFactor": 2.0,
                   "ResetDelay": 150, "SpeedUpWaitTime": 200, "EdgeScrollEnabled": True},
    "Scroll": {"X": 50.0, "Y": 50.0, "Speed": 100.0, "Amplitude": 20.0, "Override": True},
    "MouseZoom": {"X1": 40.0, "Y1": 50.0, "X2": 60.0, "Y2": 50.0, "Speed": 40.0,
                  "Amplitude": 25.0, "Override": True, "WheelSettings": 0,
                  "KeyModifier": "Ctrl", "KeyModifier_alt1": ""},
}


def make(type_name: str, x: float, y: float) -> dict:
    """A new control of a palette type, centred at view percent (x, y)."""
    fields = copy.deepcopy(_FIELDS.get(type_name, {"X": 50.0, "Y": 50.0, "Key": ""}))
    dx = x - fields.get("X", fields.get("X1", 50.0))
    dy = y - fields.get("Y", fields.get("Y1", 50.0))
    for a, b in (("X", "Y"), ("X1", "Y1"), ("X2", "Y2"), ("LButtonX", "LButtonY"),
                 ("LookAroundX", "LookAroundY"), ("OriginX", "OriginY"),
                 ("CancelX", "CancelY")):
        if a in fields:
            fields[a] = round(min(100.0, max(0.0, fields[a] + dx)), 2)
            fields[b] = round(min(100.0, max(0.0, fields[b] + dy)), 2)
    return new_control(type_name, **fields)


# fields holding key bindings (each may have an _alt1 gamepad twin)
def key_fields(c: dict) -> list[str]:
    return [k for k in c if k.startswith("Key") and not k.endswith("_alt1")
            and k != "KeyboardLayout" and isinstance(c.get(k), str)]


# draggable points of a control: (x field, y field, label)
POINTS = (("X", "Y", ""), ("X1", "Y1", "1"), ("X2", "Y2", "2"),
          ("LButtonX", "LButtonY", "shoot"), ("LookAroundX", "LookAroundY", "look"),
          ("OriginX", "OriginY", "origin"), ("CancelX", "CancelY", "cancel"),
          ("CrosshairX", "CrosshairY", "+"))


def points(c: dict) -> list[tuple[str, str, str]]:
    return [p for p in POINTS if p[0] in c and p[1] in c]
