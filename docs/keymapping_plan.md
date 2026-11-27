# Game controls (key mapping + gamepad): plan

Goal: BlueStacks-compatible game controls. Import BlueStacks `.cfg` files unchanged
(`C:\ProgramData\BlueStacks_nxt\Engine\UserData\InputMapper\<package>.cfg`), run them with the
same behaviour from keyboard, mouse and gamepad, and edit them in an editor laid out like
BlueStacks' Controls editor.

## The BlueStacks format (ParserVersion 17)

The file is UTF-8 JSON (with a BOM):

```
{ "MetaData": {ParserVersion, UpdateVersion, ...},
  "ControlSchemes": [ { Name, BuiltIn, Selected, KeyboardLayout,
                        GameControls: [ {"$type": "<Type>, Bluestacks", "Type": "<Type>", ...} ],
                        Images: [ {ImageId, TextureCRC, TextureIndex, ...} ] } ],
  "Strings": { "<locale>": {...}, "User-Defined": {...} } }
```

- The file holds one or more schemes, and `Selected` is the active one. "Smart" schemes enable
  controls by on-screen state. "Standard" schemes have no `Images`.
- **Common control fields:**
  - `X`, `Y` (percent of the current view, 0..100)
  - `Tweaks` (bit flags)
  - `Exclusive` / `ExclusiveDelay`
  - `XExpr` / `YExpr`
  - `XOverlayOffset` / `YOverlayOffset`
  - `StartCondition` / `EnableCondition`: boolean expressions over image ids and states,
    e.g. `(WiFi1 || Tutorial) && !Settings`
  - `ShowOnOverlay`
  - `GuidanceCategory` / `Guidance`
- **Control types** (all 24 that HD-Player.exe knows, as `"<Type>, Bluestacks"`):
  - Alias, Callback, DontForwardKeys, Dpad, EdgeScroll, FreeLook, GenericSwipe, LockCursor
  - MOBADpad, MOBASkill (with `MOBASkillCancel`), MouseZoom, NativeCursor, Pan, Rotate
  - Script, Scroll, SendOriginalKeys, SmartScroll, State, Swipe, Tap, TapRepeat, Tilt, Zoom
- **Key names:**
  - Keyboard names: `W`, `Num1`, `Ctrl`, `F1`, `Space`, ...
  - `MouseLButton` / `MouseRButton` / `MouseMButton` / `MouseXButton1` / `MouseXButton2`, and
    `MouseWheelUp` / `MouseWheelDown`
  - `GamepadA` / `B` / `X` / `Y`, `GamepadLeftShoulder` / `RightShoulder`, `GamepadLTrigger` /
    `RTrigger`, `GamepadLeftThumb` / `RightThumb`, `GamepadStart` / `Back`,
    `GamepadDpadUp` / `Down` / `Left` / `Right`, and `GamepadLStickUp` ... `GamepadRStickRight`
  - Sticks are bound with `GamepadStick: LeftStick | RightStick`.
  - A `_alt1` field gives a second binding, normally the gamepad one.
- **Script commands:**
  - `tap x y`, `keyDown K` / `keyUp K`, `wait ms`, `onRelease`
  - `mouseDown` / `mouseMove` / `mouseUp`, `mouseWheel`, `swipe`, `zoom`
  - `loop` / `loopend` / `repeat`, `if` / `state`, `enable` / `disable`, `text`
- **Gamepad identification:** `gamepad.db` maps controller product names to button and axis
  numbers. With SDL's GameController database we get the standard layout directly, so it is
  only a fallback.

## Architecture

```
 host window (QEMU gr_render: Win32 / X11)
   | keys, all mouse buttons, wheel, raw mouse motion (pointer lock)
   v
 QEMU "ostinput" chardev  <---- text protocol, persistent local socket ---->  front-end (Python)
   ^                                                                     keymap engine
   | multi-touch frames (contacts 1..15)                                 gamepad (SDL2)
 bst_vmsg (bstinput type 2 / type 5)                                     overlay, editor (Qt)
```

- **The engine lives in the front-end (Python/Qt).** The editor, the overlay, `.cfg`
  import/export and expression/script evaluation all live there, which keeps QEMU small.
- **QEMU owns the host window,** so it captures input and forwards it:
  - When mapping is on, the front-end sends QEMU the set of keys and buttons the active scheme
    binds. QEMU forwards those to the front-end and swallows them.
  - Everything else keeps going to the guest as today (PS/2 keys, left button as contact 0).
  - This matches BlueStacks, where unmapped keys still reach the game.
  - Pointer lock (Pan / aim mode) hides and confines the cursor and forwards relative motion.
- **bst_vmsg** merges the physical contact 0 with up to 15 synthetic contacts and sends full
  frames:
  - type 2: a 16-contact snapshot;
  - type 5: the changed slots, split across pushes as the 132-byte limit requires.
- **Coordinates are in view space** (0..32767 across the window as displayed). QEMU applies the
  orientation inverse, like `gr_pointer_view`, so the front-end works in the same percent space
  as the `.cfg` files.

### Channel: `-chardev socket,id=ostin,host=127.0.0.1,port=N,server=on,wait=off` + bstvmsg `input=ostin`

Line-based ASCII. One line per event; `\n` terminated.

From the front-end to QEMU:

| line | meaning |
|---|---|
| `t <id> <x> <y>` | contact `id` (1..15) down or moved, view coordinates 0..32767 |
| `u <id>` | contact `id` up |
| `s` | sync: emit one frame with all pending contact changes |
| `r` | release all synthetic contacts (and sync) |
| `cap <codes...>` | the set of key codes to capture (evdev numbers), replacing the old set; `cap` alone clears it |
| `btn <mask>` | mouse buttons to capture: 1 = left, 2 = right, 4 = middle, 8 = x1, 16 = x2, 32 = wheel |
| `lock <0/1>` | pointer lock: hide, confine and report relative motion |
| `k <evdev> <0/1>` | inject a key into the guest (SendOriginalKeys, Script `text` fallback) |
| `a <x> <y> <z>` | accelerometer (Tilt): gravity as HD-Player sends it, `-g * 1e6`; QEMU completes the guest's parked `gcal` request with gcall 0x3fc |
| `g <opcode> <args...>` | any gcall host call (docs/re/kmm_controls.md 3.16.1); NativeCursor sends the mouse (0x3f8 enable, 0x3fd data, 0x40a pointer). Queued in QEMU; accelerometer values and consecutive mouse moves coalesce |
| `I b <gen> <emu> <sampler> <sampler2>`, `I i <crc> <tex> <pos> <u> <v> [rect]`, `I c` | a Smart scheme's Images for the renderer's image detection (`-` = no sampler; emu 1 = Unity); docs/re/kmm_controls.md 3.20 |

From QEMU to the front-end:

| line | meaning |
|---|---|
| `K <evdev> <0/1>` | a captured key changed |
| `B <bit> <0/1> <x> <y>` | a captured mouse button changed, at view coordinates |
| `W <delta> <x> <y>` | wheel (120 per notch, positive is up) |
| `M <x> <y>` | absolute pointer position while unlocked (rate-limited, for Pan/FreeLook hover logic) |
| `R <dx> <dy>` | relative motion while locked |
| `F <0/1>` | the display window gained or lost focus (the engine releases everything on loss) |
| `O <orientation>` | orientation changed |
| `I <gen> <0/1>` | after `I c`: whether the renderer detects images (gfxstream ost_renderer only) |
| `I r` | the renderer became ready: lists sent before were refused, send it again |
| `T <gen> <index...>` | the images matched in a frame changed (indices of the `I i` lines) |

Key codes are Linux evdev numbers everywhere. Windows scancodes are converted in QEMU with
the existing qnum tables.

### Front-end modules (`frontend/openstacks/controls/`)

| module | contents |
|---|---|
| `bscfg.py` | `.cfg` load/save (lossless: unknown fields are kept for round-tripping), scheme selection, key-name tables (BlueStacks name <-> evdev) |
| `expr.py` | condition expressions (`&&`, `||`, `!`, parentheses, identifiers); images and states are looked up in a state set |
| `engine.py` | the control runtime. A touch allocator hands out contacts 1..15. Each control type is a class with key/axis/motion handlers and timers. It runs on a Qt thread at about 250 Hz, with interpolated moves for Swipe, Dpad, Zoom and similar |
| `script.py` | the Script command interpreter (coroutine-style, with `wait`, `onRelease`, loops) |
| `gamepad.py` | SDL2 GameController through ctypes: Windows ships `SDL2.dll`; Linux uses the system `libSDL2-2.0.so.0` and the AppImage bundles one. Hotplug, a stick deadzone, and the map to `Gamepad*` names |
| `overlay.py` | a transparent click-through window over the display: key labels on each control (BlueStacks look), opacity, toggle (Ctrl+Shift+F6 in BlueStacks) |
| `editor.py` | the Controls editor side panel: palette (the 15 editor types), drag onto the screen, key assignment, per-control settings, schemes (new/copy/rename/delete/import/export), Keyboard and Gamepad tabs |
| `store.py` | per-instance storage at `instances/<name>/controls/<package>.cfg`, the user's copies, and import from a BlueStacks InputMapper folder |

- **The foreground package** (which picks the `.cfg`) comes from the guest: adb `dumpsys
  activity activities`, polled every second while the player is focused. A push from the guest
  would be better later.

## Milestones

Status:
- Milestones 1 and 3 are done.
- Milestone 2 is done: all 24 HD-Player control types have a runtime. Tilt (accelerometer)
  and NativeCursor (Android mouse) go through the `gcal` host-call channel; both verified on
  Pie64. GenericSwipe's extraData.txt logging is not reproduced.
- Milestone 4 (image detection) is implemented after HD-Player's Imgd.cpp (spec 3.20): texture
  CRCs and per-draw texcoord/triangle tests in gfxstream's translator (patch 0009), tags to the
  engine per frame; images become condition variables, Pan honours StartCondition. Unit-tested
  (renderer/imgd_test.cpp, the engine against Free Fire's Smart scheme); not yet seen on a game.
  Without detection (BlueStacks renderer) the unknown-image policy stays.
- The runtime follows docs/re/kmm_controls.md: a 16 ms tick, speeds in percent per second,
  HD-Player's dispatch and combo shadowing, parse defaults on load and editor defaults on
  create.

1. **Plumbing:** the QEMU chardev, multi-touch in bst_vmsg, and capture in gr_render on Win32 and
   X11, including pointer lock. The engine runs Tap, TapRepeat, Dpad, Swipe and Script basics
   from an imported `.cfg`. There is a minimal overlay. Gamepad buttons and sticks come through
   SDL2.
2. **The rest of the runtime:** Pan / FreeLook / LockCursor / NativeCursor (aim mode with
   crosshair), Zoom / MouseZoom, MOBADpad / MOBASkill (+cancel), Tilt, Rotate, Scroll /
   SmartScroll / EdgeScroll, State / Alias / Callback / SendOriginalKeys / DontForwardKeys,
   and the full Script language.
3. **The editor:** palette, placement, key capture, settings, schemes, import/export, and the
   Gamepad tab.
4. **Smart controls:** image detection for `Images` / `StartCondition` (texture CRC matching
   in the renderer, as BlueStacks does). Without it (the BlueStacks renderer), conditions that
   reference image ids evaluate as "unknown" and the default policy ignores them, so a Smart
   scheme behaves like its Standard counterpart. The status line shows which mode is in use.

Behaviour details (Dpad timing, Pan sensitivity scaling, MOBA radii, ...) follow HD-Player.exe;
reverse it per type where the field names do not make the behaviour obvious.
