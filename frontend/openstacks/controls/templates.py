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
    "Tap": {"X": 50.0, "Y": 50.0, "Key": "", "Key_alt1": ""},
    "TapRepeat": {"X": 50.0, "Y": 50.0, "Key": "", "Key_alt1": "", "Count": 5,
                  "Delay": 100, "RepeatUntilKeyUp": True},
    "Dpad": {"X": 15.0, "Y": 75.0, "KeyUp": "W", "KeyUp_alt1": "", "KeyDown": "S",
             "KeyDown_alt1": "", "KeyLeft": "A", "KeyLeft_alt1": "", "KeyRight": "D",
             "KeyRight_alt1": "", "KeySpeedModifier1": "", "KeySpeedModifier1_alt1": "",
             "KeySpeedModifier2": "", "KeySpeedModifier2_alt1": "",
             "GamepadStick": "LeftStick", "XRadius": 9.0, "XRadius1": 0.0, "XRadius2": 0.0,
             "DeadzoneRadius": 0.0, "Speed": 200.0, "ActivationSpeed": 0.0,
             "ActivationTime": 50, "DpadExpiryTime": 0, "AnalogType": "", "DpadTitle": ""},
    "Pan": {"X": 70.0, "Y": 50.0, "LookAroundX": 70.0, "LookAroundY": 40.0,
            "LButtonX": 85.0, "LButtonY": 70.0, "Sensitivity": 1.0, "SensitivityRatioY": 1.0,
            "GamepadSensitivity": 1.0, "IsLookAroundEnabled": True,
            "IsShootOnClickEnabled": True, "MouseAcceleration": False,
            "KeyStartStop": "F1", "KeyStartStop_alt1": "", "KeySuspend": "",
            "KeySuspend_alt1": "", "KeyLookAround": "", "KeyLookAround_alt1": "",
            "KeyAction": "MouseLButton", "GamepadStick": "RightStick",
            "IsCrosshairEnabled": True, "CrosshairX": 50.0, "CrosshairY": 50.0,
            "CrosshairType": "Cross", "CrosshairSize": 1.0, "CrosshairOpacity": 100.0,
            "CrosshairColor": "#FFFFFF", "CrosshairImage": "",
            "Left": 0.0, "Right": 0.0, "Top": 0.0, "Bottom": 0.0, "ActivationTimeMs": 0,
            "Speed": 0.0},
    "FreeLook": {"X": 70.0, "Y": 50.0, "KeyUp": "Up", "KeyUp_alt1": "", "KeyDown": "Down",
                 "KeyDown_alt1": "", "KeyLeft": "Left", "KeyLeft_alt1": "",
                 "KeyRight": "Right", "KeyRight_alt1": "", "Key": "", "Key_alt1": "",
                 "XRadius": 10.0, "Speed": 100.0, "DeviceType": 0},
    "Script": {"X": 50.0, "Y": 50.0, "Key": "", "Key_alt1": "", "Commands": []},
    "Swipe": {"X1": 50.0, "Y1": 55.0, "X2": 50.0, "Y2": 40.0, "Speed": 150.0,
              "Hold": False, "KeyUpStop": False, "Key": "", "Key_alt1": ""},
    "Zoom": {"X1": 40.0, "Y1": 50.0, "X2": 60.0, "Y2": 50.0, "Speed": 50.0,
             "MinDistance": 5.0, "Mode": 0, "ResetDelay": 0, "Override": False,
             "KeyIn": "", "KeyIn_alt1": "", "KeyOut": "", "KeyOut_alt1": "",
             "KeyModifier": "Ctrl", "KeyModifier_alt1": "", "KeyWheel": True},
    "Tilt": {"X": 50.0, "Y": 50.0, "KeyUp": "", "KeyUp_alt1": "", "KeyDown": "",
             "KeyDown_alt1": "", "KeyLeft": "", "KeyLeft_alt1": "", "KeyRight": "",
             "KeyRight_alt1": "", "MaxAngle": 30.0, "Speed": 100.0, "AutoReset": True},
    "MOBADpad": {"X": 15.0, "Y": 75.0, "OriginX": 15.0, "OriginY": 75.0, "XRadius": 9.0,
                 "DpadSpeed": 100.0, "CharSpeed": 100.0, "KeyMove": "MouseRButton",
                 "KeyMove_alt1": ""},
    "MOBASkill": {"X": 85.0, "Y": 70.0, "XRadius": 8.0, "CancelX": 85.0, "CancelY": 20.0,
                  "IsCancelSkillEnabled": True, "Key": "Q", "Key_alt1": "",
                  "KeyCancel": "", "KeyCancel_alt1": "", "AdvancedMode": False,
                  "AutocastEnabled": False, "KeyActivate": "", "KeyActivate_alt1": "",
                  "KeyAutocastToggle": "", "KeyAutocastToggle_alt1": "",
                  "SelectedSkillType": 0, "YAxisRatio": 1.0, "DeadZoneRadius": 0.0,
                  "CancelSpeed": 100.0, "MinSwipeRadius": 0.0, "NoCancelOnSwitch": False,
                  "NoCancelTime": 0, "MinSkillTime": 0, "MinSkillHoldTime": 0,
                  "AutoAttack": False, "StopMOBADpad": False},
    "Rotate": {"X": 50.0, "Y": 50.0, "Radius": 10.0, "StartingAngle": 0.0,
               "KeyClock": "", "KeyClock_alt1": "", "KeyAntiClock": "",
               "KeyAntiClock_alt1": "", "Speed": 100.0},
    "EdgeScroll": {"X": 50.0, "Y": 50.0, "XVelocity": 1.0, "YVelocity": 1.0,
                   "XActiveMargin": 2.0, "YActiveMargin": 2.0, "SpeedUpFactor": 1.0,
                   "SpeedUpWaitTime": 0, "EdgeScrollEnabled": True},
    "Scroll": {"X": 50.0, "Y": 50.0, "Speed": 100.0},
    "MouseZoom": {"X": 50.0, "Y": 50.0, "Speed": 100.0},
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
