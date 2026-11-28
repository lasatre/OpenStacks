# BlueStacks 5 game controls (KMM / "imap") runtime spec

Target: `C:\Program Files\BlueStacks_nxt\HD-Player.exe` (SHA-256 `ab5383d4…3dd3`, 11,896,696 bytes,
built from `C:\code\app-player\hd\Source\{imap,kmm}\*`). All addresses are EXE VAs (image base
0x140000000). Static analysis only (Ghidra 12.1.4 decompilation + own capstone/RTTI scan); nothing
here was observed at runtime.

Confidence tags: **[C]** = read directly from decompiled code; **[I]** = inferred (naming, data
flow, or partially-read code); **[?]** = not determined.

Status: sections 0-4 analysed; items marked TODO / section 5 are open.

---

## 0. Architecture

### 0.1 Class families (RTTI) [C]

Three parallel class hierarchies per control type `T`:

| layer | class | role |
|---|---|---|
| data | `ImapControl` (base) / `ImapControlT` | parsed from one JSON object of `GameControls[]`. vtable slot 0 = to-JSON serializer, slot 1 = `createRt()` factory. |
| runtime | `ImapRt` (base) / `ImapRtT` | live state machine for one control (touch contact, timers). |
| editor | `KmmControlT` (QML-side wrapper, holds an `ImapControlT` at +0x38) | used only by the KMM editor (`Kmm.cpp`, `KmmEditor.cpp`). |

`ImapControlT` vtables (slot0 = toJson, slot1 = createRt) [C]:

| type | vtable | toJson | createRt | JSON ctor |
|---|---|---|---|---|
| (base) ImapControl | 0x14096ff80 | 0x1402f8540 | — | 0x140326100 |
| Tap | 0x140970010 | 0x1402f8ed0 | 0x140307810 | 0x14032e090 |
| Script | 0x140970038 | 0x1402f8ab0 | 0x1402f6e60 | 0x14032cf50 |
| Scroll | 0x140970748 | 0x140305ff0 | 0x140307f60 | 0x14032d190 |
| Zoom | 0x140970798 | 0x140306210 | 0x140308a20 | 0x14032eb00 (via 0x140330f00) |
| MouseZoom | 0x140970830 | 0x1403058f0 | 0x140309c20 | 0x14032ac60 |
| Raw (legacy) | 0x140970858 | 0x140305d90 | 0x14030ad20 | ? |
| TapRepeat | 0x1409729d8 | 0x14034b9e0 | 0x1404a1350 | 0x14032e250 |
| Dpad | 0x140972a18 | 0x140344bd0 | 0x1404a1e40 | 0x140326c60 |
| Pan | 0x140972b90 | 0x140349210 | 0x14030fca0 | 0x14032b720 |
| FreeLook | 0x140972f10 | 0x140345df0 | 0x1404a3410 | 0x140327c60 |
| Swipe | 0x140972f38 | 0x14034b590 | 0x1404a46d0 | 0x14032dc60 |
| Rotate | 0x140972f68 | 0x14034a820 | 0x1404a5550 | 0x14032cae0 |
| Tilt | 0x140972fd8 | 0x14034bd90 | 0x1404a6210 | 0x14032e590 |
| MOBADpad | 0x140973018 | 0x140347520 | 0x1404a6cd0 | 0x140328df0 |
| MOBASkill | 0x140973148 | 0x140347e70 | 0x1404a8c00 | 0x140329d40 |
| State | 0x1409732b0 | 0x14034b170 | 0x1404aa320 | 0x14032d830 |
| EdgeScroll | 0x1409732d0 | 0x140345a80 | 0x1404aadc0 | 0x140327700 |
| SendOriginalKeys | 0x140973360 | 0x14034adc0 | 0x1404ab740 | 0x140324d80 |
| DontForwardKeys | 0x140973388 | 0x140344b10 | 0x1404aba20 | 0x140326100 (base fields only) |
| Callback | 0x1409733a0 | 0x1403448e0 | 0x1404abcf0 | 0x140326970 |
| Alias | 0x1409733b8 | 0x140344500 | 0x1404abff0 | 0x140330860 (KeyIn/KeyOut) |
| LockCursor | 0x1409733d0 | 0x140347460 | 0x140312490 | base + `Key` (inline in 0x14033a170) |
| NativeCursor | 0x1409733e8 | 0x140348fa0 | 0x1404ac610 | 0x14032b120 |
| GenericSwipe | 0x140973450 | 0x1403468c0 | 0x1404ad330 | 0x1403282e0 |
| SmartScroll | 0x1409734a8 | 0x14034af20 | 0x1404af3b0 | 0x14032d450 |

The JSON `"Type"` string → class dispatch is `FUN_14033a170` (Kmm.cpp) [C]. Exact type strings
compared: `Tap, TapRepeat, Dpad, Swipe, Scroll, Pan, Rotate, Zoom, MouseZoom, Tilt, MOBADpad,
MOBASkill, FreeLook, State, Script, SendOriginalKeys, DontForwardKeys, Alias(*), EdgeScroll,
Callback, LockCursor, NativeCursor, GenericSwipe, SmartScroll`. (The editor factory
`kmmGetNewControl` 0x14035fc20 accepts `"Swipe*"` by prefix — SwipeUp/Down/Left/Right are four
Swipe controls grouped by the editor.)

`ImapRtT` vtables (8 slots) [C for slot roles where noted]:

| slot | offset | role |
|---|---|---|
| 0 | +0x00 | scalar deleting dtor |
| 1 | +0x08 | `onKey(int type /*1=down,2=up*/, int key, uint16 flags)` [C, called from 0x1402e9990] |
| 2 | +0x10 | mouse/other event hook (default no-op 0x1402f7f80) [I] |
| 3 | +0x18 | (default no-op 0x1402f7e10) [?] |
| 4 | +0x20 | periodic `tick()` (timers, motion) [C for Tap/TapRepeat] |
| 5 | +0x28 | `setActive(bool)`; false ⇒ calls slot 6 [C for Tap] |
| 6 | +0x30 | `reset()` — lift contact, clear state [C for Tap] |
| 7 | +0x38 | `control()` → `ImapControlT*` [C] |

| rt type | vtable | slots 0..7 |
|---|---|---|
| ImapRt (base) | 0x140970060 | 1402f3860 1402f7e20 1402f7f80 1402f7e10 1402f7fb0 1402f8530 1402f4520 1402f75b0 |
| Script | 0x1409700a8 | 1402f38a0 1402f7e30 1402f7f90 1402f7e10 1402f7fc0 1402f8530 1402f4530 1402f7720 |
| Tap | 0x140970d78 | 1403077a0 140307870 1402f7f80 1402f7e10 140307a30 140307c70 1403077e0 140307860 |
| Scroll | 0x140970dc0 | 140307ef0 1402f7e20 1403080b0 1402f7e10 1403082e0 1402f8530 140307f30 1403080a0 |
| Zoom | 0x140970e10 | 140308990 140308bc0 1402f7f80 1402f7e10 140308e00 1403095f0 1403089d0 140308bb0 |
| MouseZoom | 0x140970e68 | 140309b90 1402f7e20 140309c80 1402f7e10 14030a150 1402f8530 140309bd0 140309c70 |
| Raw | 0x140970eb0 | 14030ac80 14030ae80 1402f7f80 1402f7e10 14030b160 1402f8530 14030acd0 14030ae70 |
| Pan | 0x140971158 | 14030f900 140310020 140310420 14030fd00 140310f60 140311650 14030f9c0 14030fcf0 |
| LockCursor | 0x140971978 | 1403123e0 140312760 1402f7f80 1402f7e10 1402f7fb0 1403127c0 140312420 1403124e0 |
| TapRepeat | 0x1409dccb0 | 1404a12e0 1404a14a0 1402f7f80 1402f7e10 1404a16a0 1404a19c0 1404a1320 1404a1490 |
| Dpad | 0x1409dccf8 | 1404a1d80 1404a2530 1402f7f80 1404a2210 1404a2900 1404a3010 1404a1dc0 1404a1e90 |
| FreeLook | 0x1409dce58 | 1404a3330 1404a3470 1404a3a00 1402f7e10 1404a3ee0 1402f8530 1404a3370 1404a3460 |
| Swipe | 0x1409dcea0 | 1404a4660 1404a4730 1402f7f80 1402f7e10 1404a4ad0 1404a4f50 1404a46a0 1404a4720 |
| Rotate | 0x1409dcee8 | 1404a54c0 1404a5700 1402f7f80 1402f7e10 1404a58f0 1404a5f30 1404a5500 1404a56f0 |
| Tilt | 0x1409dcf90 | 1404a61a0 1404a6360 1402f7f80 1402f7e10 1404a6560 1402f8530 1404a61e0 1404a6350 |
| MOBADpad | 0x1409dcfd8 | 1404a6c40 1404a6f70 1404a7040 1402f7e10 1404a73e0 1402f8530 1404a6c90 1404a6d20 |
| MOBASkill | 0x1409dd0b8 | 1404a8a00 1404a90f0 1404a95b0 1404a8e40 1404a96f0 1404a9cc0 1404a8b70 1404a8c50 |
| State | 0x1409dd100 | 1404aa0c0 1404aa450 1402f7f80 1402f7e10 1404aa460 1402f8530 1404aa310 1404aa440 |
| EdgeScroll | 0x1409dd1d0 | 1404aad50 1402f7e20 1404aaf20 1402f7e10 1404ab060 1402f8530 1404aad90 1404aaf10 |
| SendOriginalKeys | 0x1409dd218 | 1404ab700 1404ab820 1402f7f80 1402f7e10 1402f7fb0 1402f8530 1402f4520 1404ab810 |
| DontForwardKeys | 0x1409dd260 | 1404ab9e0 1402f7e20 1402f7f80 1402f7e10 1402f7fb0 1402f8530 1402f4520 1404abb00 |
| Callback | 0x1409dd2a8 | 1404abcb0 1402f7e20 1402f7f80 1402f7e10 1402f7fb0 1402f8530 1402f4520 1404abdd0 |
| Alias | 0x1409dd2f0 | 1404abfb0 1404ac100 1402f7f80 1402f7e10 1402f7fb0 1402f8530 1402f4520 1404ac0f0 |
| NativeCursor | 0x1409dd438 | 1404ac3e0 1404ac670 1404ac6c0 1402f7e10 1402f7fb0 1404ac910 1404ac420 1404ac660 |
| GenericSwipe | 0x1409dd5f8 | 1404ad290 1404ad390 1404adfb0 1402f7e10 1404ae2b0 1402f8530 1402f4520 1404ad380 |
| SmartScroll | 0x1409dd710 | 1404aed50 1404af4b0 1404af580 1402f7e10 1404af6b0 1402f8530 1404aee40 1404af4a0 |

(Slots equal to the base no-ops 0x1402f7e20/7f80/7e10/7fb0/8530/4520 mean the type does not
override that hook.)

### 0.2 Input manager globals [C]

`DAT_140af9968` = imap manager singleton (`Imap.cpp`). Relevant members:

| offset | type | meaning |
|---|---|---|
| +0x18 | bool | input suppressed (key handler returns early when set) [I] |
| +0x60 | mutex | guards key handling |
| +0xb0/+0xb8 | `vector<ImapRt*>` | all runtime controls of the active scheme |
| +0x120 | `std::set<int>` | currently pressed host keys (internal key ids) |
| +0x132 | bool | keymapping active |
| +0x134 | int | display rotation 0..3 (see rotation_spec.md) |
| +0x138/+0x13c | int | guest fb width/height (used for optional pixel snapping) |
| +0x140 | `std::set<ImapRt*>` | controls currently holding an *Exclusive* touch [I] |
| +0x249 | bool | imap enabled for current app |
| +0x286 | bool | treat all keys as mapped (forces dispatch to controls) [I] |
| +0x2e8 | `map<int,vector<int>>` | key → other keys it is combined with in some combo (modifier shadowing) |
| +0x2f8 | `map<int,vector<ImapRt*>>` | key → controls that reference it (built by 0x1402feed0) |

Touch contact table (global, not in manager) [C]:

* `DAT_140af9a00[16]` — slot-in-use flags; `DAT_140af9980[16]` — `{int32 x, int32 y}` per slot in
  0..32767 space, `0x7fffffff` = no contact; `DAT_140af9a10` dirty flag.
* `FUN_1402e6910()` allocate first free slot (0..15) or −1 when all 16 are busy.
* `FUN_1402e6f10(slot)` free slot.
* `FUN_1402ec410(slot, float xy_percent[2], bool snap)` / `FUN_1402ec260(contact*, xy, snap)`:
  rotate by +0x134 then `v = (int)(pct*32767.0/100.0)`; if `snap`, quantise to guest pixels:
  `v = ((fbW*v)/0x7fff)*0x7fff/fbW`.
* `FUN_1402ec550()` flush: if dirty, write `0x7fffffff7fffffff` into every free slot and send the
  whole 16-slot frame (`FUN_1402bd080`, message type 2, 0x84 bytes = 16×{x,y} + header) to the
  guest.

So **every touch frame carries all 16 slots**; controls own a slot index stored in their
runtime object (Tap: `rt+0xf0`, `rt+0xe8` = last {x,y} %).

Key events to Android are PS/2-style scan bytes: `FUN_1402bcfa0(code, down)` sends each non-zero
byte of the 32-bit code (MSB first), with `|0x80` on release [C].

### 0.3 Key dispatch (`FUN_1402e9990(type, key, flags)`, Imap.cpp) [C]

1. Lock +0x60. Update the pressed-key set +0x120 (insert on type 1, erase on type 2).
2. If `key != 0xf` and not suppressed (+0x18):
   * `flags & 0x800` ⇒ gamepad-originated event; special gamepad-mode handling (key 0x8e toggles
     gamepad mode +0x27a; keys 0x90/0x94/0x95 routed to gamepad helpers 0x140301c00/0x140304270;
     when +0x27c ≠ 0 the event goes to 0x140304a90). TODO.
   * If imap enabled (+0x249) **and** keymapping active (+0x132) **and** (the key is referenced by
     at least one *enabled* control — `FUN_140301930` checks map +0x2f8 and `control+0x14` —
     **or** +0x286): **every enabled control in +0xb0 receives `onKey(type,key,flags)`** (no
     first-match / priority; each control filters itself). The key is then **not forwarded to
     Android** (except a synthetic key-7 release bookkeeping, below). Then `FUN_1402ec550()`
     flushes touches.
   * Otherwise the key is forwarded to Android (`FUN_1402bcfa0(translate(key), down)`).
     Special case: keys 0x2e/0x30/0x43 (bitmask 0x200005 over key−0x2e) with `flags & 2` also
     send key id 7 down/up (+0x309 tracks it) [C; meaning of ids 7/0x2e/0x30/0x43 not mapped yet —
     likely Alt/AltGr-style modifiers].
3. Unlock.

Consequences: a key mapped by any enabled control is swallowed (not sent to Android) unless a
`SendOriginalKeys` control re-emits it; all controls bound to the same key fire together.

Per-control relevance filter `FUN_1402e9290(combo, key, type)` [C]: given one key combo (vector of
key ids):
* empty combo → not relevant;
* single-key combo: relevant only if `combo[0]==key`; additionally on key-down, if map +0x2e8 says
  this key participates in a longer combo and any of those partner keys is currently pressed, the
  single-key binding is **suppressed** (so `A` does not fire while `Ctrl` is held if `Ctrl+A` is
  bound somewhere);
* multi-key combo: always treated as relevant; the control then tests "is the whole combo
  pressed" with `FUN_1402ec080` (all keys of the combo in the pressed set) /
  `FUN_1402ec110` (any of `Key`, `Key_alt1` combos fully pressed).

### 0.4 Key fields: `Key` + `Key_alt1` [C]

Every key-typed field `F` is read by `FUN_14032f170`: it reads string `F` and string `F + "_alt1"`
(both default `""`), parses each into a combo (vector<int> of key ids, `FUN_1402f27f0`) and stores a
`vector<combo>` (24 bytes). Binding fires when either combo is fully held. Combo string syntax
is `+`-separated (`"Ctrl + A"`, parser 0x1402f27f0 splits on '+'); key names: section 3.19.

---

## 1. Common fields (`ImapControl` base, ctor 0x140326100, toJson 0x1402f8540) [C]

| JSON key | type | default | object offset | notes |
|---|---|---|---|---|
| `Tweaks` | int | 0 | +0x08 | bit flags, see §1.1 |
| `Exclusive` | bool | false | +0x0c | |
| `ExclusiveDelay` | int (ms) | 200 | +0x10 | |
| (enabled) | bool | true | +0x14 | not from JSON; runtime enable flag toggled by State/EnableCondition [I] |
| `XExpr` | string | "" | +0x18 | expression for X (compiled by ImapExpr) |
| `YExpr` | string | "" | +0x38 | |
| `XOverlayOffset` | string | "" | +0x58 | overlay-only |
| `YOverlayOffset` | string | "" | +0x78 | overlay-only |
| `StartCondition` | string | "" | +0x98 | expression |
| `EnableCondition` | string | "" | +0xb8 | expression |
| `ShowOnOverlay` | bool | false | +0xd8 | |

Getter semantics (Xjson.cpp) [C]:
* `xjsonGetDouble(obj,key,def)` 0x14046a060 — missing ⇒ def; accepts JSON double, int64, uint64;
  any other type logs "Illegal type for a double value" and throws.
* required double 0x140469e60 — `obj.at(key)` (throws if absent); used for every `X`,`Y`
  (and Swipe `X1..Y2`).
* `xjsonGetInt` 0x14046aca0 — missing ⇒ def; present but not int64 ⇒ throws ("not an int64"). So
  integer fields must be written as JSON integers (e.g. `200`, not `200.0`).
* `xjsonGetBool` 0x140469d80, `xjsonGetString` 0x14046ad80 (default arg), string array 0x14046b850.

Coordinates `X`,`Y` (and all `*X/*Y` positions) are **percent of the view, 0..100** (converted by
`pct*32767/100` at touch time, §0.2) [C].

### 1.1 Exclusive / ExclusiveDelay (Tap example, 0x140307870 / 0x140307a30 / 0x140300390) [C]

On activation of a control with `Exclusive=true`:
1. `FUN_140300390(self)`: every *other* control whose `Exclusive` is true gets `reset()` (its touch
   is lifted).
2. The control enters state 2 (pending) and records `t0 = now` (ns, QueryPerformanceCounter).
3. In `tick()`: once `now >= t0 + ExclusiveDelay*1e6 ns`, the manager's exclusive set (+0x140) is
   cleared, the touch is pressed, state → 1 and the control is inserted in +0x140.

i.e. Exclusive controls are mutually exclusive and the touch is delayed by `ExclusiveDelay` ms
(default 200) after the others are released.

### 1.2 Tweaks bits [partial]

Tweaks is interpreted per type (no global meaning). See each type in section 3; summary in
section 5 item 6. Tap: bit 0 (`Tweaks & 1`) pauses the active **Pan** control (first enabled
ImapRtPan, `FUN_140300490`) while the Tap key is held (`FUN_14030f9e0(pan,0)` on press /
`(pan,1)` on release, section 3.2).

---

## 2. Per-type JSON fields (parsers) [C unless noted]

Types: d = double, i = int (must be JSON integer), b = bool, s = string, k = key combo (`F`,
`F_alt1`). "REQ" = required (exception if missing).

### Tap (0x14032e090)
`X` d REQ, `Y` d REQ, `Key` k.

### TapRepeat (0x14032e250)
`X` d REQ, `Y` d REQ, `Key` k, `Count` i = 1, `Delay` i = 16 (ms), `RepeatUntilKeyUp` b = true.

### Dpad (0x140326c60)
`X` d REQ, `Y` d REQ, `KeyUp` k, `KeyDown` k, `KeyLeft` k, `KeyRight` k, `KeySpeedModifier1` k,
`KeySpeedModifier2` k, `GamepadStick` s = "", `XRadius` d = 0, `XRadius1` d = 0, `XRadius2` d = 0,
`DeadzoneRadius` d = 0, `Speed` d = 150, `ActivationSpeed` d = 0, `ActivationTime` i = 50,
`DpadExpiryTime` i = 0, `DpadTitle` s = "".

### Pan (0x14032b720)
| key | type | default | off |
|---|---|---|---|
| `X`,`Y` | d | REQ | +0xe0/+0xe8 |
| `KeyStartStop` | k | | +0xf0 |
| `KeySuspend` | k | | +0x108 |
| `KeyLookAround` | k | | +0x120 |
| `Sensitivity` | d | −1 | +0x138 |
| `SensitivityRatioY` | d | −1 | +0x140 |
| `LButtonX`,`LButtonY` | d | −1 | +0x148/+0x150 |
| `TapX`,`TapY` | d | −1 | +0x158/+0x160 |
| `LookAroundX`,`LookAroundY` | d | −1 | +0x168/+0x170 |
| `MouseAcceleration` | b | false | +0x178 |
| `IsLookAroundEnabled` | b | (LookAroundX,Y) ≠ (−1,−1) | +0x179 |
| `IsShootOnClickEnabled` | b | (LButtonX,Y) ≠ (−1,−1) | +0x17a |
| `GamepadStick` | s | "" | +0x180 |
| `GamepadSensitivity` | d | −1 | +0x1a0 |
| `DeadzoneRadius` | d | 14 | +0x1a8 |
| `Left`,`Right` | d | 250 | +0x1b0/+0x1b8 |
| `Top`,`Bottom` | d | 1000 | +0x1c0/+0x1c8 |
| `Speed` | d | 10 | +0x1d0 |
| `ActivationTimeMs` | i | 40 | +0x1d8 |
| `KeyAction` | s | "MouseLButton" | +0x1e0 |
| `WaitAfterShootTime` | i | −1 | +0x200 |
| `WaitAfterResetTime` | i | −1 | +0x204 |
| `CrosshairX`,`CrosshairY` | d | 50.05 | +0x208/+0x210 |
| `IsCrosshairEnabled` | b | false | +0x218 (+0x219 forced 1) |
| `CrosshairType` | s | "Cross" | +0x220 |
| `CrosshairSize` | d | 1.0 | +0x240 |
| `CrosshairOpacity` | d | 100 | +0x248 |
| `CrosshairColor` | s | "#FFFFFF" | +0x250 |
| `CrosshairImage`, `CrosshairShowOnOverlayExpr`, `LButtonXExpr`, `LButtonYExpr`, `LButtonXOverlayOffset`, `LButtonYOverlayOffset`, `LookAroundXExpr`, `LookAroundYExpr`, `LookAroundXOverlayOffset`, `LookAroundYOverlayOffset`, `LookAroundShowOnOverlayExpr`, `LButtonShowOnOverlayExpr` | s | "" | +0x270.. |

### Swipe (0x14032dc60)
`X1`,`Y1`,`X2`,`Y2` d REQ, `Speed` d = 200, `Hold` b = false, `KeyUpStop` b = false, `Key` k.

### Scroll (0x14032d190)
`X`,`Y` d REQ, `Speed` d = 100, `Amplitude` d = 20, `Override` b = true.
(Editor default object: Speed 100, Amplitude 20, Override true — 0x14035fc20.)

### Zoom (0x14032eb00, wrapper 0x140330f00 adds `Amplitude` d = 20, `Acceleration` d = 1.0, `KeyWheel` s)
`X1`,`Y1`,`X2`,`Y2` d = 0, `Speed` d = 1.0, `MinDistance` d = 5.0, `Mode` i = 0, `ResetDelay` i = 75
(ms), `Override` b = true, `KeyIn` k, `KeyOut` k, `KeyModifier` k.

### MouseZoom (0x14032ac60)
`X1`,`Y1`,`X2`,`Y2` d = 0, `Speed` d = 40, `Amplitude` d = 25, `Override` b = true,
`WheelSettings` i = 0, `KeyModifier` k.

### Rotate (0x14032cae0)
`X`,`Y` d REQ, `XRadius` d = 6.0, `StartingAngle` d = 90.0, `Speed` **i** = 60, `ActivationTime`
i = 100, `KeyClock` k, `KeyAntiClock` k. (No `Radius`/`MaxAngle`/`AutoReset` in Rotate — those
belong to Tilt.)

### Tilt (0x14032e590)
`X`,`Y` d REQ, `Radius` d = 10, `MaxAngle` d = 45, `Speed` d = 45, `AutoReset` b = true, `KeyUp`,
`KeyDown`, `KeyLeft`, `KeyRight` k.

### MOBADpad (0x140328df0)
`X`,`Y` d REQ, `OriginX`,`OriginY` d = 50, `CancelX`,`CancelY` d = −1, `XRadius` d = 6,
`DpadSpeed` d = 120, `CharSpeed` d = 10, `IsCancelSkillEnabled` b = (CancelX,Y) ≠ (−1,−1),
`KeyMove` s = "MouseRButton", `KeyCancel` k, `OriginXExpr`, `OriginYExpr`, `CancelXExpr`,
`CancelYExpr`, `CancelXOverlayOffset`, `CancelYOverlayOffset`, `CancelShowOnOverlayExpr` s = "".

### MOBASkill (0x140329d40)
`X`,`Y` d REQ, `OriginX`,`OriginY` d = 50, `CancelX`,`CancelY` d = −1, `YAxisRatio` d = 0,
`XRadius` d = 8, `DeadZoneRadius` d = 0, `Speed` d = 200, `CancelSpeed` d = 200,
`MinSwipeRadius` d = 0, `NoCancelOnSwitch` i = 0, `NoCancelTime` i = 0, `MinSkillTime` i = 0,
`MinSkillHoldTime` i = 0, `IsCancelSkillEnabled` b = (CancelX,Y) ≠ (−1,−1), `AutoAttack` b =
false, `StopMOBADpad` b = false, `AdvancedMode` b = true, `AutocastEnabled` b = true, `KeyActivate`
k, `KeyAutocastToggle` k, `KeyCancel` k, `GamepadStick` s, `SelectedSkillType` s, `OriginXExpr`,
`OriginYExpr`, `CancelXExpr`, `CancelYExpr`, `CancelXOverlayOffset`, `CancelYOverlayOffset`,
`CancelShowOnOverlayExpr` s.
(Note the spelling: `DeadZoneRadius` here vs `DeadzoneRadius` in Dpad/Pan.)

### FreeLook (0x140327c60)
`X`,`Y` d REQ, `Key` k, `KeyUp`,`KeyDown`,`KeyLeft`,`KeyRight` k, `Sensitivity` d = 1.0, `Speed` d
= 100, `DeviceType` i = 0, `Delay` i = 50, `MouseAcceleration` b = false.

### State (0x14032d830)
`X`,`Y` d REQ, `Delay` i = 0, `Key` k, `Name` s = "", `Model` s = "Modifier".

### Script (0x14032cf50)
`X`,`Y` d REQ, `Key` k, `Commands` array of strings.

### EdgeScroll (0x140327700)
`X`,`Y` d REQ, `XVelocity`,`YVelocity` d = 100, `XActiveMargin`,`YActiveMargin` d = 3.0,
`SpeedUpFactor` d = 2.0, `ResetDelay` i = 150, `SpeedUpWaitTime` i = 200, `EdgeScrollEnabled` b =
true.

### SmartScroll (0x14032d450)
`X`,`Y` d = 50, `Speed` d = 200.

### Callback (0x140326970)
`X`,`Y` d REQ, `Id` s = "", `Action` s = "".

### Alias (0x140330860)
`KeyIn` k, `KeyOut` k.

### NativeCursor (0x14032b120)
`LockCursorOnRightMouseButtonPressed` b = false, `RelativeNorm` d = 1.0, `ShowNativeMousePointer`
b = false, `Key` k.

### LockCursor (inline, 0x14033a170)
`Key` k only.

### GenericSwipe (0x1403282e0)
`X`,`Y` d REQ, `Activate`, `Deactivate`, `Left`, `Right`, `Up`, `Down`, `Diff1`, `Diff2`, `Diff3` k.

### DontForwardKeys
base fields only (behaviour: section 3.17).

### SendOriginalKeys (0x140324d80)
parser not decompiled; runtime forwards all keys it receives (section 3.17).

---

## 3. Runtime behaviour per type

### 3.1 Tap (ImapRtTap, ctor 0x140307540) [C]
Runtime layout: +0x08 timestamp(ns), +0xc8/+0xd0 X/Y (copied from control), +0xd8 state
(0 idle, 1 down, 2 pending-exclusive), +0xe0 control*, +0xe8 last {x,y}% (floats), +0xf0 slot.

`onKey` 0x140307870:
1. Ignore unless `Key` or `Key_alt1` is relevant to this event (§0.3 filter).
2. If neither combo is fully held now ⇒ **release**: free slot, state 0; if `Tweaks&1` resume
   Pan.
3. Else (combo held) ⇒ **press**: if `Exclusive` ⇒ §1.1 path. Otherwise if `Tweaks&1` suspend
   Pan; allocate slot if none, set slot to (X,Y) without snapping, flush.

There is no auto-repeat handling: OS key-repeat downs just re-set the same position. Contact is
held for as long as the key is held.
`setActive(false)` / `reset()` lift the touch (state 0).

### 3.0 Timing model (tick thread `FUN_1402eb180`) [C]

A dedicated thread loops: `deadline = now + 16 ms; sleep_until(deadline)`; lock manager mutex;
for every control whose `control+0x14` (enabled) is set call `tick(now_ns)` (vtable slot 4);
flush the 16-slot touch frame if dirty; unlock. So **all animated motion is quantised to ~16 ms
ticks (~62.5 Hz)**, and most "Speed" values are applied as `Speed * 16/1000` per tick, i.e. the
unit is **percent-of-view per second** (assuming exactly 16 ms; the constant 16 is hard-coded, so
real speed drops slightly if the loop runs late). All timestamps are `QueryPerformanceCounter`
converted to ns; all `*Time`/`*Delay` JSON values are milliseconds (`ms*1e6` compared to ns).

### 3.0.1 Mouse events (`FUN_1402e9d80(type, MouseData*, wheelDelta)`) [C]

`MouseData` (copied to manager +0x48..+0x5c): `[2],[3]` int = cursor position in guest pixels
(virtual cursor, accumulates relative motion while captured), `[4],[5]` float = cursor position
in percent of view. Event types: 3 move, 4/5 L down/up, 6/7 M down/up, 8/9 R down/up,
10/11 X1 down/up, 12/13 X2 down/up, 14 wheel (event types 6/7 = middle, 8/9 = right; see the
correction note in 3.7).

* L button (4/5) sets bit 0x20 of manager+0x130; while held and native touch is allowed
  (manager+0x19) a **plain touch follows the cursor** (manager-owned contact at manager+0x40;
  coordinates clamped 0..100). L button is *not* turned into a key event here.
* Every enabled control gets slot 2 `onMouse(type, data, wheel)`.
* Then buttons are converted to *keys* and fed to the key dispatcher (section 0.3):
  events 6/7 = key 0x83 (MouseMButton), 8/9 = key 0x84 (MouseRButton), 10/11 = 0x85 (X1),
  12/13 = 0x86 (X2) (down = type 1, up = type 2); wheel = key 0x88
  (`wheelDelta>=0`, "MouseWheelUp") or 0x89 ("MouseWheelDown") sent as an immediate press+release.
  Key id 0x82 = MouseLButton (seen in Pan `FUN_140300930`).
* When manager+0x27b (gamepad pointer mode) is set, the event goes only to the control returned by
  `FUN_140300400` [I].

### 3.2 Pan (ImapRtPan, ctor 0x14030ec60, ImapPan.cpp) [C unless marked]

Runtime constants set in the ctor: `+0x1c0 = 200` (ms, periodic-reset/anti-stuck period),
`+0x1c4 = 50` (ms, look-around -> pan return delay), `+0x1c8 = 100` (ms, look-around engage delay),
`+0x1cc = 100` (ms, Tweaks 0x8000 periodic tap period), `+0x1d0 = 125` (Hz, Tweaks 0x8 move rate
limit = 8 ms), `+0x1d4 = 20` (gamepad stick -> pixel gain).
`TapX/TapY < 0` => replaced by `X/Y`. `Tweaks & 0x20` => *hold* mode (rt+0x220).
`Tweaks & 0x200` => activation driven by image detection (`panImgDetect`, 0x1403038d0).

Touch positions used by `start(state)` (0x1403117a0): state 1 / 0xc -> (`X`,`Y`); state 3 ->
(`LookAroundX`,`LookAroundY`); state 7 -> (`LButtonX`,`LButtonY`).

**start(state)**: release the manager's native-mouse touch; `centerPx = fb * centerPct/100`;
current = center; accumulators = 0; virtual cursor (manager+0x150) := centerPx; capture/hide the
host cursor (`FUN_1400cd090(1, MouseAcceleration)`, `FUN_1400d3ed0(0)`, `FUN_1400cb930(1,0)`);
press a touch (own slot rt+0x22c) at center; state := new state.
**stop(state)** (0x140311dc0): un-capture; in hold mode re-send the saved mouse-button-up via
`SendInput`; for new state 0/5/6 move the host cursor back to the window centre (50%,50%) (hold
mode: to the position saved when the key was pressed); show cursor unless new state is 6; lift the
touch.

State machine (`rt+0x244`):

| state | meaning |
|---|---|
| 0 | idle (cursor free) |
| 1 | panning (mouse drives touch) |
| 2 | look-around key pressed, waiting 100 ms |
| 3 | look-around active (touch at LookAround point, mouse drives it) |
| 4 | look-around released, after 50 ms (checked on next mouse move) back to 1 |
| 5 | suspended by `KeySuspend` (cursor free while held) |
| 6 | paused by a Tap with `Tweaks&1` (ref-counted, rt+0x1d8) / during shoot (Tweaks 0x100) |
| 7 | shooting with the pan finger at LButtonX/Y (Tweaks 0x100) |
| 8 / 9 | image-detect inactive/active wait (500 ms / 50 ms) |
| 10 | after re-centre with Tweaks 0x800: touch re-pressed >=20 ms later on next move |
| 0xb | Exclusive pending (ExclusiveDelay) |
| 0xc | activation wiggle (Tweaks 0x10000) |
| 0xd | wait `WaitAfterShootTime` ms then start(1) |
| 0xe | wait `WaitAfterResetTime` ms then start(1) (after Tap-pause ends) |

Key handling (`onKey` 0x140310020; "pressed" = event key in combo and type 1, "released" = type 2):
* state 0/6: `KeyStartStop` pressed => (Exclusive => state 0xb) else `start(Tweaks&0x10000 ? 0xc : 1)`.
* state 1/7: `KeyStartStop` pressed (toggle mode) or released (hold mode) => `stop(0)`;
  `KeyLookAround` pressed => `stop(2)` (t=now); `KeySuspend` pressed => `stop(5)`.
* state 2/3: `KeyLookAround` released => `stop(4)` (t=now).
* state 4: `KeyStartStop` pressed => `stop(0)`; `KeyLookAround` pressed => `start(3)`.
* state 5: `KeySuspend` released => `start(0xc or 1)`; hold mode & `KeyStartStop` released => `stop(0)`.

So by default `KeyStartStop` **toggles** pan mode; with `Tweaks&0x20` it must be held. If the
start key is a mouse button, `FUN_140300930` remembers the matching `SendInput` button-up flags
(0x82 L -> 4, 0x83 R -> 0x40(sic), 0x84 M -> 0x10, 0x85/0x86 X1/X2 -> 0x100 with data 1/2).

Tap with `Tweaks&1` (`FUN_14030f9e0(pan, resume)`): press => counter++ and if state 1 => stop(6)
(touch lifted, cursor stays captured); release => counter-- and at 0 from state 6 => state 0xe
("Wait after reset") => after `WaitAfterResetTime` ms (default -1 => immediately) start(1).

**Mouse move -> touch** (`onMouse` slot 2 = 0x140310420 -> `FUN_140310450(rt, type, data, 0,
Sensitivity)`; only in states 1,3,4,7,10):
```
dxPx  = (cursorPx.x - centerPx.x) * Sensitivity          // virtual cursor, guest px
dyPct = (cursorPx.y - centerPx.y) / fbH * Sensitivity * SensitivityRatioY * 100
x% = startX + accX + dxPx / fbW * 100     // Tweaks&0x4000 && mgr+0x180: |dxPx| via accel spline
y% = startY + accY + dyPct
```
i.e. touch displacement = cursor displacement x `Sensitivity` (pixel-for-pixel at 1.0), Y
additionally x `SensitivityRatioY`. NB: no code was found that replaces the parse defaults
(Sensitivity = SensitivityRatioY = -1); the editor always writes 1.0/1.0 (0x140355770). Treat -1
as "absent" and use 1.0 [I].

Re-centre ("reset") test, evaluated on each move (Tweaks bits of `control+8`):
* default (bits 0x2,0x4,0x10,0x400 clear): reset when the new point leaves the view
  (`|x/100-0.5| > 0.5` or `|y/100-0.5| >= 0.5`).
* `0x10`: reset when `|x - startX| >= 1.2 * max(startX, 100-startX)` (x only).
* `0x4` / `0x400`: allowed box x in [55,100] / y in [50,100].
* `0x2000`: additionally reset when the touch moved more than `Left`/`Right` px (x) or
  `Top`/`Bottom` px (y) from the start point (guest-fb px; defaults 250/250/1000/1000).
* `0x1`: reset at least every 200 ms; `0x80`: every 200 ms reset if the touch moved >10% while the
  raw mouse moved <50 px (anti-stuck).
* `0x8`: moves rate-limited to one per 8 ms.
* `0x40`: snap touch coordinates to guest pixels.

On reset: lift touch, fold the travelled distance into the accumulators, then (Tweaks 0x800 =>
state 10, re-press after >=20 ms) / (0x10000 => start(0xc)) / else press again at the start point
immediately and continue with the remaining motion. With `Tweaks&0x1000` the first move after a
re-press is offset by `DeadzoneRadius` px (default 14) in the motion direction (beats in-game touch
slop).

Activation wiggle (state 0xc, Tweaks 0x10000, tick 0x140310f60): each tick x += `Speed`*0.016 %
for the first `ActivationTimeMs/2`, then x -= `Speed`*0.016 % until `ActivationTimeMs`, then x = X
and state 1 (defaults Speed 10 => 0.16 %/tick, 40 ms).

`Tweaks&0x8000`: in state 1, every 100 ms (if the touch moved from start) issue an instant tap
(down+up in consecutive frames) at (`TapX`,`TapY`).

Left button / shoot (`onMouse` type 4/5, states 1,3,4,7,10): without Tweaks 0x100 a **second**
touch (slot rt+0xd8) is held at (`LButtonX`,`LButtonY`) while L is down. With 0x100 the pan
finger itself is moved: `stop(6)` + `start(7)` at LButtonX/Y, and on release `stop(6)` then
start(0xc) / `WaitAfterShootTime` wait (state 0xd) / start(1). `IsShootOnClickEnabled`,
`IsLookAroundEnabled` and the crosshair flag are re-evaluated from `LButtonShowOnOverlayExpr`,
`LookAroundShowOnOverlayExpr`, `CrosshairShowOnOverlayExpr` in 0x1402eb3c0 (the gating site of
the shoot touch was not located - TODO).

Gamepad (slot 3, 0x14030fd00): `GamepadStick` names a stick; stick values v in [-1,1] add `v*20`
px (y inverted) to the virtual cursor per event (through `FUN_140438750` when MouseAcceleration),
then the move handler runs with `GamepadSensitivity` instead of `Sensitivity`. Stick at (0,0)
marks the stick released (manager +0x290[stick], time +0x2a0[stick]).

Mouse-acceleration spline (Tweaks 0x4000 and global manager+0x180): `FUN_14030d710` builds a
spline from two ~80-point tables in the ctor (x: 1, 5.19, 10.59, 16.29, 22.5, 49.09, 80.48,
116.59, 135, 270 ... step 135 ... 3240, 6480 ... step 3240 ... 68040, then 7.128e6 ... step
3.24e5 ... 1.62e7; y: 1,5,10,15,20,40,60,80,85.9,145.9,189.9,226.9,263.9,295.9,324.9,351.9,
377.9,402.9,425.9,448.9,469.9,489.9,509.9,529.9,547.9,565.9,583.9,599.9,616.9,633.9,648.9,671.3,
987.3,1234.3,1445.3,1628.3,1798.3,1950.3,2098.3,2233.3,2373.3,2539.3,2643.2,...,5515.2,(one
value lost by the decompiler),5691.2). [C values; the x-axis jump looks like a units change in the
source table - verify before relying on it.]

### 3.3 Dpad (ImapRtDpad, ctor 0x1404a1a20) [C]

Control offsets: +0xf0 KeyUp, +0x108 KeyDown, +0x120 KeyLeft, +0x138 KeyRight, +0x150
KeySpeedModifier1, +0x168 KeySpeedModifier2, +0x1a0 XRadius, +0x1a8 XRadius1, +0x1b0 XRadius2,
+0x1b8 DeadzoneRadius, +0x1c0 Speed, +0x1c8 ActivationSpeed, +0x1d0 ActivationTime, +0x1d4
DpadExpiryTime. Runtime constants: rt+0x148 = 50 ms (re-press debounce), rt+0x14c = 100 ms
(stick re-engage delay).

`onKey` (0x1404a2530), on any event touching one of the six combos:
```
dx = held(Right) - held(Left);  dy = held(Down) - held(Up)            // -1,0,+1
deadzone = (dx, dy) * DeadzoneRadius                                   // percent
r = held(Mod1) ? XRadius1 : held(Mod2) ? XRadius2 : XRadius
aspect = fbW/fbH  (W and H swapped when rotation is 1 or 3)
target.x = X + dx*r ;  target.y = Y + dy*r*aspect
if dx && dy: both components /= sqrt(2)
```
So **XRadius is in percent of view width; the Y radius is the same physical length** (x W/H).
The deadzone offset is *not* aspect-corrected and *not* divided by sqrt(2).

Activation (`FUN_1404a1ea0(anyDirHeld)`), state rt+0xd8:
* idle(0) + press: Exclusive => pending state 5 (ExclusiveDelay); else if pressed again within
  50 ms of the last release => state 1 (debounce: tick presses at the centre after 50 ms);
  otherwise **touch down at centre+deadzone**, t0=now, state = 2 if `ActivationTime`!=0 else 3.
* release (state >=2): if `now >= t0 + DpadExpiryTime` lift the touch at once (state 0, record
  release time) else set "expiry pending" and the tick lifts it when the time is reached
  (= minimum hold time).
* state 4 (arrived) + press => state 3 (move again).
* `Tweaks&2`: on every direction change jump the touch straight to centre+deadzone (with a tiny
  -0.028/-0.029 % nudge on the moving axis) before moving.

Tick (0x1404a2900): state 2 lasts `ActivationTime` ms (moving at `ActivationSpeed`), then state 3
(moving at `Speed`). Each tick:
```
step = unit(target - cur) * speed * 16 / 1000     // percent per 16 ms tick => speed = %/s
if |step| <= |target-cur| per axis: cur += step (+0.001 if Tweaks&1); else cur = target, state 4
```
Defaults: Speed 150 %/s, ActivationSpeed 0 (=> no motion during the activation window),
ActivationTime 50 ms. With `speed == 0` nothing moves.

Gamepad (slot 3, 0x1404a2210): with `GamepadStick` set, the stick value (fx,fy in [-1,1]; left
stick at event+0x14/+0x18, right stick +0x1c/+0x20) drives the target directly:
`target = clamp0..100(X + fx*r, Y - fy*r*aspect)`, deadzone vector = `(fx, -fy)*DeadzoneRadius`,
`r` chosen by the modifiers as above; stick at (0,0) releases; re-engage after release waits 100 ms.

### 3.1b Exclusive timing detail (`FUN_1403008a0`) [C]

The pending control's wait base is the `rt+8` timestamp (time of its last press/activation) of
another control currently in the manager's exclusive set (+0x140); if the set is empty the base
is 0 and the touch is pressed on the next tick. So with two Exclusive controls A then B: B's
press lifts A immediately and B's touch goes down `ExclusiveDelay` ms after A was pressed (not
after B was pressed). When the delay elapses the set is cleared and B inserted.

### 3.4 TapRepeat (ImapRtTapRepeat, onKey 0x1404a14a0, tick 0x1404a16a0) [C]

Control offsets: +0x108 Count, +0x10c Delay, +0x110 RepeatUntilKeyUp. Runtime: +0xe8 slot,
+0xec phase (0 up, 1 down, 2 exclusive-pending), +0xf0 remaining, +0xf8 last toggle time.

* Key press (combo held): lift if currently down; `remaining = RepeatUntilKeyUp ? 1 : Count-1`;
  (Exclusive => pending, see 1.1) else touch **down** at (X,Y), phase 1, t=now.
* Key release: phase 0, touch lifted immediately; if `RepeatUntilKeyUp` then remaining = 0
  (stop); otherwise the remaining taps **continue after the key is released**.
* Tick: if `phase==0 && remaining==0` nothing. Otherwise every `Delay` ms toggle: up->down
  (and `remaining--` unless RepeatUntilKeyUp) / down->up.

So one tap = `Delay` ms down + `Delay` ms up (period 2*Delay, quantised to 16 ms ticks);
RepeatUntilKeyUp=true repeats while held (remaining pinned at 1); RepeatUntilKeyUp=false gives
exactly `Count` taps in total (first on key-down). Parse defaults: Count 1, Delay 16,
RepeatUntilKeyUp true; editor defaults: Count 5, Delay 100, RepeatUntilKeyUp true.

### 3.5 Swipe (ImapRtSwipe, onKey 0x1404a4730, tick 0x1404a4ad0) [C]

Control offsets: +0xe0..+0xf8 X1,Y1,X2,Y2, +0x100 Speed, +0x108 Hold, +0x109 KeyUpStop, +0x110
Key. Runtime state +0x110: 0 idle, 1 swiping, 2 swiping (Hold), 3 holding at end, 4 exclusive
pending.

* idle + press: cur = (X1,Y1); Exclusive => 4; else state = Hold ? 2 : 1, touch down at (X1,Y1).
* Tick (states 1/2): `FUN_140301490(cur, end, Speed, 16)`:
  `dist = |end-cur|; step = (end-cur)/dist * Speed * 16/1000` (=> **Speed in percent of view per
  second**, default 200 => 3.2 %/tick). If `dist < 0.01` lift, idle. If the step would overshoot on
  either axis: state 2 => state 3 (finger stays where it is, near the end, until key-up); state 1
  => move to (X2,Y2), flush, lift, flush, idle. Else cur += step.
* Key down again while swiping (OS auto-repeat or re-press) **restarts** the swipe from (X1,Y1)
  (lift, re-press at start).
* Key up: state 1: `KeyUpStop` => lift now; else finish the swipe. State 2: `KeyUpStop` => lift;
  else state 1 (finish and release). State 3: lift.
* Disable handler (0x140304f80): forces KeyUpStop and injects synthetic key-ups for its keys.

`SwipeUp/SwipeDown/SwipeLeft/SwipeRight` in the editor are four ordinary Swipe controls
(`kmmGetNewControl`, `pKmmControlUp/Down/Left/Right` QML properties).

### 3.6 Zoom (ImapRtZoom, ctor 0x140308810, onKey 0x140308bc0, tick 0x140308e00) [C]

Control offsets: +0xe0 X1, +0xe8 Y1, +0xf0 X2, +0xf8 Y2, +0x108 Speed, +0x110 MinDistance,
+0x118 Mode, +0x11c ResetDelay, +0x120 Override, +0x128 KeyIn, +0x140 KeyOut, +0x158 KeyModifier.
`Override` true => manager+0x260 = 1 (global flag, meaning not traced) [C/?].

* onKey: active when (`KeyModifier` empty or held) and (`KeyIn` or `KeyOut` held). From idle:
  init finger positions (`FUN_140309600`): P1 = (X1,Y1), P2 = (X2,Y2); with `Tweaks&1` the pair
  is re-centred on the mouse cursor (manager+0x58/+0x5c, percent) keeping half the X/Y spans,
  only if the cursor is inside 5..95 %; if a finger would fall outside the view the pair is
  forced to 5/25 or 75/95 on that axis. Two contacts pressed (Exclusive => pending). Otherwise
  (keys released) both fingers lift, idle.
* Tick: runs only while exactly one of KeyIn/KeyOut is held.
  `d = P2 - P1` (Mode 1: the *configured* `(X2-X1, Y2-Y1)` instead);
  `s = (KeyIn ? +1 : -1) * Speed * 16/1000`; `step = s * d`;
  `P2 += step`; `P1 -= step` unless `Tweaks&2` (then only finger 2 moves).
  => KeyIn spreads the fingers (zoom in), KeyOut pinches; motion is proportional to the current
  separation (exponential): per tick the separation is multiplied by `1 +/- 2*Speed*0.016`
  (default Speed 1.0 => ~3.2 %/tick).
  When zooming out and `0 < |dx| < MinDistance` or `0 < |dy| < MinDistance` (percent, default 5)
  both fingers lift, state 3, and after `ResetDelay` ms (default 75) the fingers are re-pressed at
  the initial positions (re-grip) and pinching continues.
  Mode != 0: before stepping, if the step would make a finger cross half of the configured span
  (sign test on `2*step + d - 0.5*configuredSpan` vs `d`) the fingers are lifted, re-initialised
  and re-pressed (re-grip) [C, exact geometric intent inferred].

**Correction to 3.0.1** [C]: from `FUN_140300930` (SendInput flags: 0x82 -> LEFTUP 0x4, 0x83 ->
MIDDLEUP 0x40, 0x84 -> RIGHTUP 0x10) the key ids are **0x82 = MouseLButton, 0x83 = MouseMButton,
0x84 = MouseRButton, 0x85/0x86 = XButton1/2**; therefore mouse event types 6/7 are *middle*
down/up and 8/9 are *right* down/up (bit 0x80 of manager+0x130 = right button held).

### 3.7 MOBADpad (ImapRtMOBADpad, ctor 0x1404a6a10, onKey 0x1404a6f70, onMouse 0x1404a7040, tick 0x1404a73e0) [C]

Control offsets: +0xe0 X, +0xe8 Y (joystick centre), +0xf0 OriginX, +0xf8 OriginY (character
position on screen), +0x100 CancelX, +0x108 CancelY, +0x110 XRadius, +0x118 DpadSpeed, +0x120
CharSpeed, +0x128 IsCancelSkillEnabled, +0x130 KeyMove (string; the move button is handled in
`onMouse` - right button events 8/9 - so `KeyMove` is effectively "MouseRButton"; other values
not observed being honoured [I]), +0x150 KeyCancel. Runtime constant rt+0x188 = 200 ms.

* Right-button down at cursor p (percent) while idle: touch down at the joystick centre (X,Y);
  target = `FUN_1404a7cd0`: `u = (p - Origin)/|p - Origin|`;
  `target = (X + u.x*XRadius, Y + u.y*XRadius*aspect)` (aspect = W/H, swapped for rotation 1/3).
  `FUN_1404a7a20` also computes a walk time `T = |p - Origin| * 1000 / CharSpeed` ms
  ("MOBA: Got Dpad expiry duration") => expiry = now + T; press time = now. State 1.
  (Exclusive => pending state 3, then same on tick.) Manager+0x168 := this control.
* Tick: state 1 moves the finger from centre towards target at `DpadSpeed` %/s (16 ms steps);
  on arrival state 2 (held at target).
* Mouse move with right button held (states 1/2): retarget from the new cursor point (finger
  jumps directly to the new target in state 2).
* Right-button down again (states 1/2): retarget + new expiry, cancel pending release.
* Right-button up: if held > 200 ms **or** now >= expiry => release now (state 0); else set
  "release pending" and the tick releases the joystick when `now >= expiry` (a quick click walks
  the character for the estimated time `distance/CharSpeed`). A MOBASkill with `StopMOBADpad`
  sets rt+0xe8 so the pending release happens immediately.
* `KeyCancel` pressed => release immediately, state 0.

### 3.8 MOBASkill (ImapRtMOBASkill, ctor 0x1404a7f80, onKey 0x1404a90f0, onMouse 0x1404a95b0, tick 0x1404a96f0) [C, some I]

Control offsets: +0xe0 X, +0xe8 Y (skill button), +0xf0 OriginX, +0xf8 OriginY, +0x100 CancelX,
+0x108 CancelY, +0x110 YAxisRatio, +0x118 XRadius, +0x120 DeadZoneRadius, +0x128 Speed, +0x130
CancelSpeed, +0x138 MinSwipeRadius, +0x140 NoCancelOnSwitch, +0x144 NoCancelTime, +0x148
MinSkillTime, +0x14c MinSkillHoldTime, +0x150 IsCancelSkillEnabled, +0x151 AutoAttack, +0x152
StopMOBADpad, +0x153 AdvancedMode, +0x154 AutocastEnabled, +0x158 KeyActivate, +0x170
KeyAutocastToggle, +0x188 KeyCancel. Runtime: rt+0x130 state, rt+0x148 cast mode, rt+0x1e8 =
20 ms (delay before the finger starts moving), manager+0x178 = currently active skill.

Cast mode (rt+0x148): `AutoAttack` => 3; otherwise `AdvancedMode ? (Autocast ? 1 : 0) :
(Autocast ? 1 : 2)`; `KeyAutocastToggle` press flips `AutocastEnabled` and recomputes.
* mode 0 (advanced, not autocast): press = start aiming, release = state 3 (finish move then
  cast);
* mode 1 (autocast / quick cast): release casts immediately; aim follows the mouse only while
  the left button is held;
* mode 2 (non-advanced): aim follows mouse; pressing the key again while active re-casts;
* mode 3 (AutoAttack): plain press/release at the button.

Activation (`FUN_1404a8c60`, on KeyActivate press while idle/3; Exclusive => pending 5):
* If another skill S is active (manager+0x178) and `now < S.press + S.NoCancelTime` (and S is not
  AutoAttack) the press is ignored.
* If this skill aims (mode 0/2, or 1 with L held) and `NoCancelOnSwitch == 0`: S is
  **cancelled** (S state 4, drag to its cancel point) and this skill is queued (S+0x158); it starts
  when the cancel completes, if its key is still held.
* Otherwise S is released (cast) and this skill starts.
* AutoAttack skills: ignored while `now < lastSkillPress(manager+0x170) + NoCancelTime`.

Start (`FUN_1402fe900`): release the native-mouse touch, touch down at (X,Y), press time = now;
aim target = `getSkillCoords(cursorPx)` (aiming modes) else (X,Y); state 1; manager+0x19 = 0
(native mouse touch suppressed while a skill is active), manager+0x178 = this.

Aim (`FUN_140300980` "getSkillCoords", default path; W,H = manager+8/+0xc view size in px):
```
o = (OriginX/100*W, OriginY/100*H);  d = cursorPx - o
drag = d * XRadius/100 * 2                       // px; XRadius is percent of W *per 50 %*
if now - press < MinSkillHoldTime and |drag| < MinSwipeRadius/100*W: drag = unit(d)*MinSwipeRadius/100*W
k = (YAxisRatio != 0) ? YAxisRatio : (double)(W / H)   // NB integer division W/H (=1 for 16:9)
dx% = drag.x*100/W ;  dy% = k*drag.y*100/H
on start (param6): if |d*XRadius/50| < DeadZoneRadius/100*W: offset = unit(d)*DeadZoneRadius/100*W (flag)
target = (X + dx%, Y + dy%)                       // not clamped
```
(Global manager+0x181 selects an alternative perspective formula
`dx% = (k2*drag.x/(k2+drag.y))*100/W, k = (1 - dy/k2)*YAxisRatio`, k2 = 2*W*XRadius/100.)
`Tweaks&2` / `Tweaks&4` replace the formula by a 2-D spline / calibration grid
(`FUN_14030c150`, table at control+0x2c0) - not analysed.

Tick: 20 ms after the press the finger moves from (X,Y) towards the aim target at `Speed` %/s
(16 ms steps). On arrival: state 1 -> 2 (aiming; `onMouse` move events now set the finger
directly to `getSkillCoords(cursor)`), except AdvancedMode with the deadzone flag recomputes the
target once. State 3 (key already released): on arrival cast = lift (with manager+0x181 only after
`MinSkillTime` ms since press), state 0. L-button up (event 5) in state 2 casts.

Cancel (`KeyCancel`, after `NoCancelTime` ms since press): state 4: the finger is dragged from
its current position to (`CancelX`,`CancelY`) at `CancelSpeed` %/s (`FUN_1402ff640`), then
lifted; with no cancel point (-1,-1) it is simply lifted. Then a queued skill starts if its key
is still held.

Gamepad (slot 3, 0x1404a8e40): `GamepadStick` aims the skill - TODO.

### 3.9 Script (ImapRtScript, ImapScript.cpp) [C unless marked]

`Commands` (JSON array of strings, one command per element) is parsed by `loadImapCommands`
0x1402f7730 (empty lines ignored: "Empty line found in script, ignoring..", unknown keyword =>
"Invalid Keyword", bad args => "Invalid number of args (%d) for Keyword %s") into a command table
(0x110 bytes per command), then `fixUpCmds` 0x1402f6ff0 pairs `loop`/`loopend` ("No loop begin
for the loopEnd, failed at index {}", "No loopEnd, failed at index {}"), and `areCommandsValid`
0x1402f5740. Tokens are whitespace-separated; keywords are matched lower-case. The editor's
validator `kmmEditorValidateScriptCommands` (0x14035e700) reports "Failed to parse script command
: %s. Error Index : %d".

Keyword table (`FUN_1402f61c0`; id, declared argument kinds) [C]:

| keyword | id | args | semantics (executor `FUN_1402f9dd0` / tick 0x1402f7fc0) |
|---|---|---|---|
| `#` | 0 | string... | comment / ignored by the executor [I] |
| `keydown K` | 1 | key name | inject a **host** key-down into the imap dispatcher (other controls react; re-entrant) |
| `keydown2 K` | 2 | key name | add K to pressed set and send K **directly to Android** (scan code), bypassing mapping |
| `keyup K` | 3 | key name | inject host key-up into the dispatcher |
| `keyup2 K` | 4 | key name | remove from pressed set and send key-up directly to Android |
| `mousedown x y` | 5 | point | press a script-owned touch at (x,y) % |
| `mouseup` | 6 | - | release this section's mousedown touch |
| `mousemove x y` | 7 | point | move this section's touch to (x,y) % |
| `mousewheel x y n` | 8 | point, int | synthesise a mouse-wheel event (type 14) at (x,y) with delta `n*120` (reaches Scroll/MouseZoom controls and wheel keys 0x88/0x89) |
| `touch x1 y1 [x2 y2 ...]` | 9 | point list | press/move one contact per point (multi-touch); contacts persist [I: no-arg form releases] |
| `text S` | 10 | string | type text into Android (`FUN_140305600`) |
| `wait N` / `wait onkeypressed` / `wait onmousemove` / `wait frame` | 11 | int or string | block N ms; or block while the press section / mouse section is still flagged; or wait one tick |
| `tap x1 y1 [x2 y2 ...] [ms]` | 12 | point list, optional int | press all points at once (one slot each, max 16), release after `ms` (default 0 => next tick); blocks |
| `swipe x1 y1 x2 y2 [... xn yn] ms` | 13 | point list, int | touch down at P1 and move through the waypoints; each segment takes `ms/(n-1)`; per-segment speed = `segLen*1000/segMs` %/s applied in 16 ms steps; release at the end; blocks |
| `loop [n]` | 14 | optional int | loop start; n repetitions, no n = infinite |
| `loopend` | 15 | - | jump back to `loop` while the counter lasts |
| `onrelease` | 16 | - | marker: commands after it run when the key is released |
| `entershoot` / `exitshoot` / `toggleshoot` | 17/18/19 | - | control the active Pan: start if idle / stop / toggle (`panHandleCommand` 0x1403035d0) |
| `onmousemove` | 20 | - | marker: commands after it run on every mouse event while the key is held |
| `dtouch ...` | 21 | expression list | "dynamic touch": coordinates are expressions with variables `x`,`y` = current cursor position % (`FUN_1402ff8c0`) [C partial] |
| `abort [onkeypressed\|onmousemove]` | 22 | optional string | stop the press section / mouse section (empty = press section) |

All script coordinates are percent of view, with `x' = (100 - off)*x/100 + off` where
`off = manager+0x10` (a horizontal offset, normally 0 [I]).

Sections (runtime): three independent program counters (rt+0xd4[3]), run flags rt+0xe0[3]:
* section 0 "press": starts on key press (after ExclusiveDelay if Exclusive) at index 0 and runs
  until `onrelease`, `onmousemove` or the end. Reaching the end marks the script finished.
* section 1 "mouse": started by any mouse event while the key is held and not yet released;
  runs from after `onmousemove`; reaching `onrelease` jumps back to the start of the section.
* section 2 "release": started on key release (if the script is active and not finished) from
  after `onrelease`.
Execution: every tick (16 ms) pending taps are released when due, swipes advance, then each
running section executes commands until a blocking command (`wait`, `tap`, `swipe`, `wait frame`).
When no section runs and the script finished, `reset()` lifts all script contacts.
Pressing the key again while the script is active does nothing (no restart).


### 3.10 FreeLook (ImapRtFreeLook, onKey 0x1404a3470, onMouse 0x1404a3a00, tick 0x1404a3ee0) [C]

Control offsets: +0xf0 Key, +0x108 KeyUp, +0x120 KeyDown, +0x138 KeyLeft, +0x150 KeyRight, +0x168
Sensitivity, +0x170 Speed, +0x178 DeviceType, +0x17c Delay, +0x180 MouseAcceleration.

* `DeviceType == 1` (mouse free-look, hold `Key`): press => capture/hide the cursor (like Pan),
  remember the cursor position, touch down at (X,Y), state 1. Mouse move:
  `x = X + accX + (cursorPx.x - startPx.x)/W * Sensitivity * 100`,
  `y = Y + accY + (cursorPx.y - startPx.y)/H * Sensitivity * 100` (each axis normalised by its own
  dimension, W/H = manager+8/+0xc). When the point leaves the view (`|v/100-0.5| > 0.5`): lift,
  fold distance into the accumulators, and if `Delay`!=0 wait `Delay` ms (state 2) before
  re-pressing at (X,Y), else re-press immediately. Release => un-capture, restore the cursor, lift,
  send the mouse-button-up if `Key` is a mouse button.
* `DeviceType == 0` (keyboard free-look): any of KeyUp/Down/Left/Right held => touch down at
  (X,Y) and every tick move it by `(right-left, (down-up)*W/H) * Speed * 16/1000` (% per tick; Speed
  in %/s, default 100, editor 20). Leaving the view => lift, re-press at (X,Y) after `Delay` ms.
  All released => lift. (Key `Key` is not used in this mode.)
* Exclusive => pending state 3.

### 3.11 Rotate (ImapRtRotate, onKey 0x1404a5700, tick 0x1404a58f0) [C]

Control offsets: +0xf0 XRadius, +0xf8 StartingAngle, +0x100 Speed (int, degrees/s), +0x104
ActivationTime, +0x108 KeyClock, +0x120 KeyAntiClock. Runtime radius `R = fbW * XRadius / 100`
(px, rt+0x110).

* Press KeyClock or KeyAntiClock from idle: angle = StartingAngle, t0 = now; unless `Tweaks&1`
  an **anchor touch** is held at the centre (X,Y); state 1 (Exclusive => pending 3).
* Tick: state 1 waits `ActivationTime` ms then state 2. In state 2 (and not both keys held) the
  second touch is placed at `(X + cos(a)*R*100/fbW, Y - sin(a)*R*100/fbH)` (so the circle is
  round in pixels), then `a += Speed*16/1000` deg if KeyClock is **not** held, `a -= ...` if it is
  (KeyClock = clockwise on screen).
* Both keys released => lift both touches, idle. Editor defaults: XRadius 6, StartingAngle 90,
  ActivationTime 100, KeyClock "X", KeyAntiClock "Z", Tweaks 1 (no anchor finger), Speed 0 in the
  editor ctor (parse default 60).

### 3.12 Tilt (ImapRtTilt, onKey 0x1404a6360, tick 0x1404a6560, send 0x1404a68d0) [C]

Tilt drives the **accelerometer**, not touch. Control offsets: +0xf0 Radius, +0xf8 MaxAngle,
+0x100 Speed (deg/s), +0x108 AutoReset, +0x110 KeyUp, +0x128 KeyDown, +0x140 KeyLeft, +0x158
KeyRight. Two angles ax (left/right, rt+0xd8) and ay (up/down, rt+0xe0):
* while Left xor Right held: `ax += (Left ? -1 : +1) * Speed*16/1000`, clamped to +/-MaxAngle;
  same for ay with Down (-1) / Up (+1);
* when an axis' keys are both released (or both held) and `AutoReset`, that angle snaps to 0;
* each change sends gravity `g = (sin ax, sin ay, cos ax * cos ay)`, rotated for the display
  orientation (`FUN_140306d20`), as integers `-g*1e6` to the sensor path (`FUN_1402a4c70`).
* orientation turn (`FUN_140306d20`, o = manager+0x134): o=0 (-x, -y), o=1 (-y, x), o=2 (x, y),
  o=3 (y, -x); z unchanged.
* transport: `gcallSendAccelerometerDataRpc` (`FUN_1402a4c70`, gcall/GcallEnc.cpp) writes
  `{u32 0x3fc, u32 20, x, y, z}` into the buffer of the guest's parked `gcal` request
  (service 'gcal', registered at 0x1402a6740 with in/out 0x1000; its request callback 0x1402a67a0
  keeps the out pointer and returns 1 = pending), then `vmsgCompleteRequest(cid, 0)` sets the poll
  word to 0 and raises the IRQ for that cid. The guest's bstvmsg ISR wakes the sleeping ioctl
  (no 0x38 ack for a connection without callback). libgcall_jni `gcallDecode` switches on the
  opcode (0x3e8..0x412) and passes +8/+0xc/+0x10 to `gcallSendAccelerometerDataClbk`; the
  accelerometer then reads `-v / 1e6 * 9.81` m/s^2 [C, verified at runtime].
`Radius` is not used by the runtime (overlay only) [I].

### 3.13 Scroll (ImapRtScroll, onMouse 0x1403080b0, tick 0x1403082e0) [C]

Triggered by mouse-wheel events (type 14) only, and only when the native mouse touch is allowed
(manager+0x19) and no MouseZoom is in progress (manager+0x164). The swipe happens **at the
cursor position** (manager+0x58/+0x5c), not at the control's X/Y:
`dist = clamp(|wheelDelta/120| * Amplitude, 5, 100)` %; wheel down => target y = curY - dist
(if not enough room: start at y=dist, target 0), wheel up => target y = curY + dist (clamped to
100). Touch down at (cursorX, startY); tick moves y toward the target at `Speed` %/s; further wheel
notches while active extend the target from the current y. After reaching the target the finger
is held and released after a hold time (rt+0x118 ms) [C partial]. `Override` (default true) and
key 7 / manager+0x30b gating not fully traced.

### 3.14 MouseZoom (ImapRtMouseZoom, onMouse 0x140309c80, tick 0x14030a150) [C partial]

Mouse wheel with `KeyModifier` held (editor default "Ctrl"): two fingers at (X1,Y1)/(X2,Y2)
(or around the cursor) are moved apart (wheel up) / together (wheel down) by
`|delta/120| * Amplitude` % in total at `Speed` %/s; motion stops when the fingers come within 5 %;
releasing the modifier lifts both; manager+0x164 = zoom active (suppresses Scroll).

### 3.15 EdgeScroll (ImapRtEdgeScroll, tick 0x1404ab060) [C partial / I]

When the cursor is within `XActiveMargin`/`YActiveMargin` % of a view edge, a touch is pressed
(at X,Y) and dragged each tick by `unit(cursor - (50,50)) * (XVelocity, YVelocity) * 16/1000` %;
after `SpeedUpWaitTime` ms the velocity is multiplied by `SpeedUpFactor`; when the finger nears
the view border it is lifted and re-pressed (`ResetDelay` ms); leaving the margin releases.
Gated by `EdgeScrollEnabled` and a global toggle ("Scroll edge enabled: {}", 0x1402e81c0).

### 3.16 SmartScroll, GenericSwipe, NativeCursor, Callback [C unless marked]

**SmartScroll** (ImapSmartScroll.cpp; onKey 0x1404af4b0, onMouse 0x1404af580, tick 0x1404af6b0,
reset 0x1404aee40, activateSwipe 0x1404aee70). Besides X/Y/Speed the control has `Swipes[]`:
`{X1, Y1, X2, Y2, Speed, Name, EnableCondition, Key}` (control item 0x80 bytes: doubles at
+0..+0x20, condition at +0x48, key at +0x68). The runtime keeps per entry `{Speed, (X2,Y2),
(X1,Y1)}` and an enable flag from the entry's EnableCondition.
* key down: the first entry whose `Key` matches; if enabled, activate it;
* wheel (only while no swipe runs): index += (delta > 0 ? -1 : +1) * (|delta/120| mod n), modulo
  n; then step one at a time in the same direction past disabled entries (none enabled:
  nothing); activate the result;
* activate(i): touch down at (X1,Y1), target (X2,Y2), speed = the entry's Speed (%/s); the
  control-level Speed is not used;
* tick: step = unit(target - cur) * Speed * 16/1000 (`FUN_140301490`); move while the distance
  is >= 0.01 and the step does not overshoot either axis, else lift (so X1,Y1 = X2,Y2 is a
  one-tick tap). Free Fire's bundled Smart scheme uses it for weapon slots (keys 1/2/3, wheel).

**GenericSwipe** (ImapGenericSwipe.cpp; onKey 0x1404ad390, onMouse 0x1404adfb0, tick no-op) is a
calibration tool. Keys (control +0xf0.. in order): Activate, Deactivate, Left, Right, Up, Down,
Diff1, Diff2, Diff3. Activate (idle): touch down at X,Y, native mouse touch off (manager+0x19).
While active, Diff1/2/3 set the step to 1.0/1.5/2.0 (default 0.5) and Left/Right/Up/Down move
the touch by -x/+x/-y/+y step. Mouse moves (type 4 while active) record (touch - origin, mouse -
first mouse) samples. Deactivate: lift, native touch on, and write the samples as
`"ExtraData": [...]` to `Engine/UserData/InputMapper/extraData.txt`.

**NativeCursor** (ImapNativeCursor.cpp; onKey 0x1404ac670, onMouse 0x1404ac6c0, setActive
0x1404ac910, abort 0x1404ac420): the host mouse becomes an Android mouse.
* every mouse event -> `gcallSendMouseDataRpc(action, x, y, wheel)` (gcall 0x3fd); action from
  the event type (`FUN_140300820`): 3 move -> 0, 4/5 L -> 1/2, 6/7 M -> 3/4, 8/9 R -> 5/6;
  x, y = view percent * 327.67 (0..32767);
* Key (down) toggles manager+0x308 (relative mode); with Tweaks bit 1 and relative mode the sent
  position is (pct - last pct) / RelativeNorm; Tweaks bit 0 rate-limits moves (rt+200 Hz);
* ShowNativeMousePointer: R down/up (8/9) show/hide the host cursor;
* setActive(true) with Tweaks bit 1 and relative: `imapEnableNativeMousePan(1)` (0x1402e7d50:
  `gcallEnableNativeMouseRpc` (0, -), (1, 0), cursor lock); abort: the reverse ((0, 1), (1, -));
* guest: `gcallEnableNativeMouseClbk(enable, flag)` opens /dev/uinput and creates "Bluestacks
  Mouse Device" (enable 0 destroys it; flag stored next to the fd);
  `gcallshowNativeMousePointerClbk(show)` (0x40a). Verified: moves and clicks arrive as
  MotionEvents. A device being re-created drops events, so OpenStacks creates it once.

**Callback** overrides no runtime hook (only dtor and `control()`): Id/Action are read by the UI.

### 3.16.1 gcall opcodes (libgcall_jni `gcallDecode`, jump table 0x34be4) [C]

Host -> guest calls on the 'gcal' channel, `{u32 opcode, u32 length incl. header, args}`:

| op | callback | op | callback |
|---|---|---|---|
| 0x3e8 | SetDeviceProfile | 0x3fe | SynchronizeTime |
| 0x3e9 | SetCustomDeviceProfile | 0x3ff | SetVolume |
| 0x3ea | SetLocale | 0x400 | LaunchAppStoreSearch |
| 0x3eb | RootDevice | 0x401 | SetGamepadState |
| 0x3ec | EnableInputDebugging | 0x402 | (disconnect) |
| 0x3ed | SetPreferredOrientation | 0x403 | AffiliateTrackingForPackage |
| 0x3ee | SetMaxFps | 0x404..0x406 | Get/Add/RemoveNowggAccount(s) |
| 0x3ef | InstallApk | 0x407 | EnableSignInPopup |
| 0x3f0 | InstallStudioApkZip | 0x408 | EnableClickSound |
| 0x3f1 | UninstallApp | 0x409 | StopApp |
| 0x3f2 | TakeScreenshot | 0x40a | showNativeMousePointer (bool) |
| 0x3f3 | LaunchActivity | 0x40b | SetDifferentImagesPkg |
| 0x3f4 | LaunchUrl | 0x40c | SetCustomAppOrientation |
| 0x3f5 | ImportFiles | 0x40d | SetAirplaneMode |
| 0x3f6 | ExportFiles | 0x40e | StartRecording |
| 0x3f7 | EnableAdb | 0x40f | EnableAndroidAds |
| 0x3f8 | EnableNativeMouse (enable, flag) | 0x410 | AndroidInterstitialAdSetting |
| 0x3f9 | SetClipboardText | 0x411 | CommonCommand |
| 0x3fa | ClearAppData | 0x412 | OnUnzipFileCompleted |
| 0x3fb | LaunchAppStore | | |
| 0x3fc | SendAccelerometerData (x, y, z) | | |
| 0x3fd | SendMouseData (action, x, y, wheel) | | |

### 3.17 Key-routing controls [C]

* **SendOriginalKeys** (`onKey` 0x1404ab820): forwards *every* key event it receives to Android
  (`FUN_1402bcfa0(translate(key), type==1)`), without filtering on its own keys. Since controls
  only receive keys that are mapped (section 0.3), an enabled SendOriginalKeys makes mapped keys
  reach Android *as well as* driving their controls.
* **DontForwardKeys** (`createRt` 0x1404aba20): sets manager+0x286 = 1, which makes the
  dispatcher treat **every** key as mapped: all keys go to the controls and **no** key is forwarded
  to Android (except via SendOriginalKeys). It has no key fields and no-op handlers.
* **Alias** (`onKey` 0x1404ac100): when an event key is part of `KeyIn`, re-injects the same event
  type with the **first key of the first `KeyOut` combo** into the dispatcher (`FUN_1402e9990`).
* **LockCursor** (`onKey` 0x140312760): each press of `Key` toggles a lock flag and calls
  `FUN_1402e7d50(locked, 1)` ("Native Pan Disable" / cursor lock); `setActive(true)` re-applies
  global cursor state (manager+0x265/+0x266).
* **State** (0x1404aa570): `Key` pressed/released updates the boolean variable named `Name` in
  the variable store (manager+0x1c8) — `Model "Modifier"` = true while held, `Model "Toggle"` =
  flips on each press — then re-evaluates all EnableConditions (0x1402eb3c0). With `Delay` != 0
  the change is queued and applied `Delay` ms later (tick 0x1404aa460).

### 3.18 StartCondition vs EnableCondition, expressions [C]

Expressions (`ImapExpr.cpp`: tokenize 0x1402efb00, getRPN 0x1402eeb00, createExprTree
0x1402edc00, evaluate 0x1402ee500): infix with `&&`, `||`, `!`, `( )`, `+ - * / ^`, identifiers
and numbers (tokenizer separator set `"&!|()+-*/^"`; a single `&`/`|` is an error).

* **EnableCondition** is evaluated against the **State-variable store** (manager+0x1c8) whenever
  variables change (0x1402eb3c0) and on scheme/tag updates (`imapSetTags` 0x1402eab00). The result
  is the control's enabled flag (control+0x14): disabled controls receive no key/mouse events and
  no ticks; on a true->false transition the runtime's activation callback (rt+0x10) is invoked
  with `false` (Tap/TapRepeat/Swipe/... lift their touches and drop their keys from the pressed
  set). Empty EnableCondition => always enabled.
* **StartCondition** is evaluated against the **image-recognition tag store** (manager+0x208)
  in `imapSetTags` (called when the screen-detection engine reports new tags); the boolean goes to
  rt+0x90 and the runtime's std::function at rt+0x50 (called only when set) is invoked. Only
  Pan sets it (`panImgDetect` 0x1403038d0, also gated by manager+0x132): on a rising edge, with
  state 0 and `Tweaks&0x200` -> state 9 (start(1) after 50 ms), otherwise stop + start(1); on a
  falling edge, with state 1 and `Tweaks&0x200` -> state 8 (stop after 500 ms), otherwise stop.
  Other types' StartConditions (Roblox's LockCursor `!Arrow`) have no effect.
* `imapSetTags(tags)` (0x1402eab00): the same tag set as last time -> nothing. Else every old tag
  that is also a variable is reset, the tag store is cleared and refilled with the new tags
  that are image ids of the scheme (with their matched position), each also set as a true
  variable (so EnableConditions can name images), StartConditions are evaluated, then all
  EnableConditions (0x1402eb3c0).
* `XExpr`/`YExpr` (and Pan `LButtonXExpr` ... , MOBA `OriginXExpr` ... `CancelYExpr`) are
  numeric expressions that override the corresponding coordinates on re-evaluation
  (`FUN_140306fe0(value, expr, "X"/"Y")` in 0x1402eb3c0). `*ShowOnOverlayExpr` re-compute the
  Pan flags `IsLookAroundEnabled` / `IsShootOnClickEnabled` / crosshair visibility.

### 3.20 Image detection (Imgd.cpp, ImgdVertexDataHelpers.cpp, ImgdShaderEmul.cpp) [C unless marked]

How "Smart" schemes see the game. A scheme's `Images[]`: `{ImageId, ImageType ("Version 1"),
DetectColor, MatchColor, TextureCRC ("0x990CCA42"), TextureIndex, VertexIndex, TextureCoord [u, v],
VertexRect [] or [x1, y1, x2, y2]}`. HD-Player runs inside the GLES translator's calls (per GL
context of a package that has images; `bst.imgd_*` properties tune it).

**Texture CRCs.** CRC = zlib CRC32 (reflected 0xEDB88320, table 0x140acf1e0, init/final ~0;
`FUN_1402b52f0`). Kept per texture name, only for `GL_TEXTURE_2D` level 0, for the texture
bound to the active unit:
* `glTexImage2D` (0x1402b3e40): crc32(pixels, pixelSize(format, type) * w * h) (no row padding;
  `imgdComputePixelSize` 0x1402ba6c0); NULL pixels -> 0;
* `glTexSubImage2D` (0x1402b4340): the texture's CRC becomes the sub-upload's crc32(w * h * bpp);
* `glCompressedTexImage2D` (0x1402b3930) / `glCompressedTexSubImage2D` (0x1402b3ae0):
  crc32 of the compressed bytes (`imageSize`), but only when `bst.imgd_compressed_tex` is set
  (0x1402b2c20, hook table 0x1402b2d00); BlueStacks ships it as "0" (`bst.imgd_min_tex_size`
  "64"), so a compressed texture keeps the CRC of the translator's decompressed upload [I: that
  the decompressing call reaches the glTexImage2D hook].

**Per draw** (`imageDetect` 0x1402b09d0, from the draw hook 0x1402b0020):
1. The sampler unit: per-package uniform name (table below; `glGetUniformLocation` on the current
   program, then `glGetUniformiv` for its unit; Free Fire falls back to `_NormalTex`); no entry or
   no such uniform -> unit 0. The texture is the `GL_TEXTURE_2D` bound to that unit.
2. Every image of the scheme not yet matched this frame whose TextureCRC equals the texture's
   CRC (a DetectColor image matches any texture of pixel size 1, then compares MatchColor;
   not implemented in OpenStacks). TextureCoord must have 2 values ("Bad cfg. TextureCoordLen").
3. Triangle search (`FUN_1402b6d20`) over the draw's vertices (index buffer ubyte/ushort/uint, or
   sequential): GL_TRIANGLE_STRIP steps 1, any other mode steps 3. The texcoords come from vertex
   attribute `TextureIndex` (float, or unsigned short; normalized shorts scaled by 1/65536).
   Skip a triangle that is degenerate in u or v (|d| < 1e-5) or has a texcoord outside [0, 1];
   it matches when (u, v) is in its texcoord bounding box and on the same side of all three edges
   (inclusive). The search resumes after a match (cursor + 3).
4. Without `VertexRect` (or for a package without a shader emulation): matched. With it: the
   centre of the matched triangle's `VertexIndex` attribute (mean of the 3 vertices, w = 1;
   `FUN_1402b7d80`) goes through the package's vertex-shader emulation to clip space; percent =
   ((x * 0.5 + 0.5) * 100, (0.5 - y * 0.5) * 100) (no divide by w); matched if strictly inside
   the rect (`FUN_1402b8240`), else the search continues with the next triangle.
5. A match adds the image to this frame's found set (and stores the percent position).

**Per frame** (`sendFoundImages` 0x1402b4fd0, on swap): once a context has found anything it
reports its found set every frame (even when empty) to imap (`imapSetTags`), then clears it.

Per-package tables (strings 0x14096aa58.., pointer table 0x140acf600):

| package | sampler uniform | shader emulation |
|---|---|---|
| com.dts.freefireth / freefiremax | `_AlphaTex` (fallback `_NormalTex`) | GroupA |
| com.activision.callofduty.shooter, com.tencent.tmgp.kr.codm, com.garena.game.codm, com.vng.codmvn | `_MainTex` | GroupA |
| jp.pokemon.pokemonunite | `_MainTex` | GroupA |
| com.pixonic.wwr | `_MainTex` | WarRobots (0x1402b8800) |
| com.tencent.ig, com.pubg.imobile | `ps0` | 0x1402b93f0 (also Apex) |
| com.supercell.brawlstars | `s_texture` | BrawlStars (0x1402b91c0) |
| com.axlebolt.standoff2, com.kitkagames.fallbuddies, com.HoYoverse.hkrpgoversea | - | GroupA |
| com.roblox.client | - | Roblox (0x1402b9680) |
| com.ea.gp.apexlegendsmobilefps | - | 0x1402b93f0 |
| com.garena.game.lmjx, com.tencent.cosg/cosjp/cosna | - | 0x1402b9aa0 (Undawn-like) |

GroupA (`imgdNormalizedScreenPositionGroupA` 0x1402b8d60), Unity HLSLcc: O2W =
`hlslcc_mtx4x4unity_ObjectToWorld[0..3]`, VP = `hlslcc_mtx4x4unity_MatrixVP[0..3]` (vec4 columns,
`glGetUniformfv`); if a location is missing, from uniform blocks `UnityPerDraw` (float 0) and
`UnityPerFrame` (float 76) (`FUN_1402ba130`). world = O2W[0]*x + O2W[1]*y + O2W[2]*z + O2W[3];
clip = VP[0]*wx + VP[1]*wy + VP[2]*wz + VP[3]*ww. The other emulations were not analysed.

Environment (OpenStacks): `OST_IMGD=0` no hashing at all, `OST_IMGD_COMPRESSED=1` the compressed
CRC, `OST_IMGD_LOG=1` log uploads/draws of an image's texture (2: every texture, 3: every call).
Seen on Free Fire 1.132.1 (Android 13): ETC1 (0x8d64) and ETC2 RGBA8 (0x9278) textures plus
uncompressed GL_ALPHA sub-uploads; none of the lobby's textures are in the Smart scheme (its
images are the in-match HUD), so a match is still to be observed.

**OpenStacks:** gfxstream patch 0009 (translator hooks + `glestranslator/common/imgd.cpp`), the
`ost_imgd_*` API of ost_renderer, QEMU `I`/`T` lines (keymapping_plan.md), engine `imgd.py`.
Matches go into a found set per render thread; a thread that never matched stays quiet, and
reports go out only when the set changes. Not reproduced: DetectColor, the non-GroupA
emulations (their VertexRect is ignored), mapped-buffer (glMapBufferRange) vertex data, the
image picker / texture dump tools.

### 3.19 Key names [C partial]

Combo strings are split on `+` (`FUN_1402f27f0`) and each token looked up by `FUN_140426f20`
(name table built in 0x140426530). Names observed: letters/digits (`A`,`W`,`1`...), `Backspace,
Tab, Clear, Enter, Shift, Ctrl, Alt, Pause, CapsLock, Esc, Space, PgUp, PgDn, End, Home, Left,
Up, Right, Down, Select, Print, Execute, Snapshot, Insert, Delete, Help, WinCmd, Apps, Sleep,
Num0..Num9, Mul, Add, Sub, Decimal, Div, F1..F24, NumLock, ScrlLock, Oem1..Oem8, OemPlus,
OemMinus, OemBackSlash, Kana, Junja, Final, Hanja, Convert, Nonconvert, Accept, Modechange,
MouseLButton, MouseRButton, MouseMButton, MouseWheelUp, MouseWheelDown`, and gamepad keys
`Gamepad{DpadUp,DpadDown,DpadLeft,DpadRight,Start,Back,LeftThumb,RightThumb,LeftShoulder,
RightShoulder,A,B,X,Y,LStickUp,LStickDown,LStickLeft,LStickRight,RStickUp,RStickDown,RStickLeft,
RStickRight,LTrigger,RTrigger}.N` for pads N = 1..3. Internal ids known: 0x82 L, 0x83 M, 0x84 R,
0x85/0x86 X1/X2, 0x88/0x89 wheel up/down; keys 0x8a..0xe9 are gamepad keys (MOBASkill treats
`0x8a <= key < 0xea` as gamepad). The full name->id table was not extracted.

---

## 4. Editor defaults (`kmmGetNewControl` 0x14035fc20 and the default `ImapControlT` ctors) [C]

Values a freshly dropped control gets (X/Y are set by the drop position). Base defaults for every
type (`FUN_1402f2730` + 0x140307360): Tweaks 0, Exclusive false, ExclusiveDelay 200, enabled,
ShowOnOverlay true.

| type | defaults (keys in quotes) |
|---|---|
| Tap | Key "" |
| TapRepeat | Key "", Count 5, Delay 100, RepeatUntilKeyUp true |
| Dpad | KeyUp "W", KeyDown "S", KeyLeft "A", KeyRight "D", modifiers "", XRadius 8, XRadius1/2 0, DeadzoneRadius 0, Speed 150, ActivationSpeed 0, ActivationTime 50, DpadExpiryTime 0 |
| Pan | KeyStartStop "F1", KeySuspend "Alt", KeyLookAround "", Sensitivity 1, SensitivityRatioY 1, LButtonX/Y, TapX/Y, LookAroundX/Y -1, MouseAcceleration false, GamepadSensitivity 1, DeadzoneRadius/Left/Right/Top/Bottom/Speed 0, ActivationTimeMs 0, KeyAction "MouseLButton", WaitAfterShootTime/WaitAfterResetTime -1, Crosshair 50.05/50.05, IsCrosshairEnabled false (+0x219 visible true), CrosshairType "Cross", Size 1, Opacity 100, Color "#FFFFFF"; creating a Pan also sets two global UI flags |
| Swipe (SwipeUp/Down/Left/Right) | X1..Y2 0 (set by editor), Speed 200, Key "" |
| Zoom | X1..Y2 -1, Speed 1, MinDistance 5, Mode 0, ResetDelay 0, Override true, KeyIn "OemPlus", KeyOut "OemMinus", KeyModifier "" |
| MouseZoom | X1..Y2 -1, Speed 40, Amplitude 25, Override true, KeyModifier "Ctrl" |
| Scroll | Speed 100, Amplitude 20, Override true |
| MOBADpad | OriginX/Y -1, CancelX/Y -1, XRadius 6, DpadSpeed 120, CharSpeed 10, KeyMove "MouseRButton" [I] |
| MOBASkill | OriginX/Y 50, CancelX/Y -1, XRadius 8, Speed 200, CancelSpeed 200, AutocastEnabled true, keys "" (the editor copies OriginX/Y from an existing MOBADpad if present) |
| Rotate | XRadius 6, StartingAngle 90, ActivationTime 100, KeyClock "X", KeyAntiClock "Z", Tweaks 1 |
| Tilt | Radius 10, MaxAngle 20, Speed 90, AutoReset true, KeyUp "Up", KeyDown "Down", KeyLeft "Left", KeyRight "Right" |
| FreeLook | Key "V", KeyUp "Up", KeyDown "Down", KeyLeft "Left", KeyRight "Right", Sensitivity 1, Speed 20 |
| EdgeScroll | XVelocity 100, YVelocity 100, XActiveMargin 3, YActiveMargin 3, SpeedUpFactor 2, ResetDelay 150, SpeedUpWaitTime 200, EdgeScrollEnabled true |
| SmartScroll | Speed 200, (+0x128.. 0) |
| NativeCursor | RelativeNorm 1 |
| State / Script / LockCursor / Callback / SendOriginalKeys / DontForwardKeys | empty keys/strings |

Note several editor defaults differ from the JSON parse defaults (e.g. TapRepeat Count 5/Delay 100
vs 1/16; Tilt MaxAngle 20/Speed 90 vs 45/45; FreeLook Speed 20 vs 100; Pan Sensitivity 1 vs -1).
OpenStacks should use the **parse** defaults when loading a .cfg and the **editor** defaults
when creating a control.

---

## 6. Built-in controls download (`cldGetPackageCfgAsync` 0x14018bd80) [C]

HD-Player fetches a game's `.cfg` from BlueStacks' cloud.
- **Request:** GET `<bst.bluestacks_cloud_url>/get_cfg_info` (`https://cloud.bluestacks.com`)
  with the common client query (FUN_140183aa0) plus `country` (`bst.country`),
  `parser_version`, `package_name`, `package_version_code` and, for the `nxt_cn` / `msi5_cn`
  OEMs, `use_china_cdn=1`.
- **Reply:** `{"cfg_file_url": {"normal": "https://eb.bluestacks.com/content_keymap/<package>.cfg?parserver=<v>[&cache=True]"}, "is_video_present": false, "success": true}`.
- **Observed behaviour:**
  - The reply is the same for any package name, known or not.
  - The file is a normal ParserVersion `.cfg`, served from `public/appsettings/imap/V<v>/`, or a 404 when there is none.
  - Neither request needs authentication, and the other query items are optional.
  - "Not fetching cfg as existing cfg already updated at {}" compares MetaData
    `CloudUpdateTimeUTC` with the local file.
  - There is no listing: `content_keymap/` returns 404 and the CDN path returns 403.
- **OpenStacks (`controls/cloud.py`):**
  - Only `package_name` and `parser_version=17` are sent, and only for a non-system app opened with no local file.
  - Each download is cached under `controls/bluestacks/`, and `index.json` records hits and misses.
  - A hit is rechecked weekly, a miss after 3 days, and an error after an hour.
  - Settings > Game controls turns this off.

## 5. Open questions / not determined

1. Pan `Sensitivity`/`SensitivityRatioY` parse default -1 is used as-is by the motion formula; no
   fix-up found. Real cfgs always contain the fields; treat missing as 1.0.
2. Which code gates the Pan shoot touch on `IsShootOnClickEnabled`, and the exact role of
   `KeyAction` (only seen as the default "MouseLButton").
3. Full key-name -> internal id table (0x140426530/0x140426f20) and id -> Android scan code
   (`FUN_140439bd0`) not extracted; ids for keys 7 / 0x2e / 0x30 / 0x43 used by the modifier
   special-case in the dispatcher are unknown.
4. Gamepad: stick naming (`GamepadStick` parse `FUN_1402ff3b0`), stick value scale (assumed
   [-1,1] floats at event+0x14..+0x20), trigger handling, the gamepad-mode keys 0x8e/0x90/0x94/0x95
   and `FUN_140304a90` were not analysed; MOBASkill gamepad aiming (0x1404a8e40) not analysed.
5. NativeCursor relative-mode units and the wheel argument of gcall 0x3fd; Scroll hold/release timing and
   `Override`; MouseZoom/EdgeScroll details (partial above).
6. Tweaks bits beyond those listed for Tap (bit0), Dpad (bit0 jitter, bit1 snap-to-centre on
   direction change), Rotate (bit0 no anchor), Zoom (bit0 cursor-centred, bit1 one finger),
   Pan (0x1,0x2,0x4,0x8,0x10,0x20,0x40,0x80,0x100,0x200,0x400,0x800,0x1000,0x2000,0x4000,0x8000,
   0x10000) and MOBASkill (0x2, 0x4 calibrated aim).
7. Script `touch`, `text`, `dtouch` argument details and the `#` keyword are only partially
   analysed; `wait onkeypressed`/`wait onmousemove` exact blocking condition inferred.
8. manager+0x10 (script x offset), +0x181 (MOBA perspective mode), +0x180 (Pan acceleration
   enable) and +0x260 (Zoom Override) are global flags whose sources were not traced.

