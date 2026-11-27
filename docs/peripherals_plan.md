# Peripherals: microphone, camera, gamepad, storage, other input (issue #15)

Goal: the host's devices work inside Android the way BlueStacks provides them, on Windows and
Linux. The report asks for the microphone (Gemini / Google voice search), USB storage,
gamepads and "input devices" in general.

The guiding rule is the same as for the rest of OpenStacks: use BlueStacks' own guest-side
paths, which the images already contain, and implement the host side in QEMU. Generic QEMU USB
passthrough is a fallback, not the plan. The BlueStacks kernels and HALs expect their
paravirtual devices, and passthrough takes the device away from the host.

## What exists today

| device | guest side (BlueStacks image) | OpenStacks host side |
|---|---|---|
| speaker | `bstaudio` PCI device, playback ring | done (`bst_audio.c` -> QEMU audiodev) |
| microphone | `bstaudio` capture ring (ops 8/0xa/0xb/0xc, event 13, `+0x1000` ring) | capture starts, but **delivers silence** (`bst_audio.c` "no capture source yet") |
| camera | BlueStacks camera HAL; HD-Player captures with Qt Multimedia (`QCamera`, `QMediaCaptureSession`, "Multiple cameras detected") | none |
| keyboard / mouse / touch | `bstinput` over bstvmsg (`inpOnReceive` types 0 kbd, 1 IME, 2 touch, 5 multi-touch) | done |
| gamepad (as a gamepad) | `bstinput` type 3 "gamepad"; gcall 0x401 `SetGamepadState` | none. Host pads only drive game controls (touch), through SDL2 in the controls engine |
| files / storage | vboxsf.ko (VirtualBox shared folders); gcall 0x3f5 `ImportFiles` / 0x3f6 `ExportFiles` | adb push/pull (Files window, drag and drop); vmmdev answers the shared-folder service with stubs |
| clipboard | gcall 0x3f9 `SetClipboardText` (host -> guest); the guest -> host direction is still to find | none (BACKLOG) |

## Plan, in order of value

### 1. Microphone (the reported bug)
1. **Host capture:** open the QEMU audiodev input voice (`AUD_open_in`) in `bst_audio.c` with
   the rate, bits and channels from capture config (op 0xa). Fill the capture ring
   (`+0x1000`) in periods, bump the completed-periods counter (`+0x800`) and raise event 13,
   the way the silence generator does now. Silence stays the fallback when no input exists.
2. **Backends:** Windows `dsound` (already used for playback) or `wasapi` if the QEMU build
   has it; Linux `pa`/`pipewire` (the AppImage already sets `PULSE_SERVER` and
   `XDG_RUNTIME_DIR` in `qemu_engine.py`). The audiodev line needs `in.*` options;
   check that the existing `-audiodev` enables input.
3. **Privacy:** a per-instance "Microphone" setting (off / on), off by default. Capture opens
   only while the guest records (ops 0xb..0xc), and the player shows a "mic in use"
   indicator. On Windows, the OS microphone permission for desktop apps applies.
4. **Verify:** Gemini and Google voice search, plus `arecord`/a recorder app on Pie64 and
   Tiramisu64. Check the period size against Android's AudioFlinger buffer so there is no
   underrun.

### 2. Gamepad as a real Android controller
Many games and emulators read controllers directly. Today a pad only drives game-control
mappings.
1. **Reverse** `bstinput` type 3: the message layout (buttons, axes, triggers, pad index), the
   uinput device the guest creates (name, VID/PID, key/axis codes), and gcall 0x401
   `SetGamepadState` (probably connect/disconnect per slot). Also check HD-Player's gamepad
   sender (`plrGamepadDetectionToggle`, "Gamepad detected") and the `gamepad.db` mapping file.
2. **Host side:** reuse the SDL2 GameController code from `controls/gamepad.py`. On hotplug,
   send 0x401 plus type-3 frames over the controls channel (a new `p ...` line, coalesced
   like touch moves).
3. **Mode switch, as in BlueStacks:** a pad either feeds the game-controls engine or Android
   directly, per game. The default is direct when the game's controls have no gamepad
   bindings.
4. **Verify:** an Android gamepad tester app, a controller-native game, then a game with
   BlueStacks gamepad controls (no double input).

### 3. Files and USB storage
"USB disk" really means getting files from host storage into Android.
1. **Shared folder (recommended):** implement the VBoxSharedFolders HGCM service in our
   vmmdev. The images already load vboxsf.ko, which is BlueStacks' "Media folder". Expose a
   chosen host folder, removable drives included, read/write, mounted where BlueStacks mounts
   it (to find in the image's init scripts). This replaces copying files for most uses.
2. **Import/Export:** wire gcall 0x3f5/0x3f6 so "Import files" lands in the Android media store
   (gallery and file pickers see them), as BlueStacks' "Import from Windows" does.
3. **Raw USB mass storage:** not planned by default. It needs a USB controller in the guest
   kernel (to verify: `/sys/bus/usb`, `/proc/config.gz` on each image) and it detaches the
   drive from the host. If a kernel supports it, offer it later as an advanced per-instance
   "USB passthrough" (QEMU `qemu-xhci` + `usb-host`, libusb on Linux, UsbDk on Windows).

### 4. Camera
1. **Reverse** the camera path: the guest HAL, its transport (a bstvmsg service, a PCI
   device or shared memory), and the frame format, from HD-Player's `QCamera` use and the
   image's camera HAL.
2. **Host side:** capture with Qt Multimedia in the front-end, which mirrors HD-Player, and
   pass the frames to QEMU (shared memory, not the line channel). Select the device per
   instance, off by default, with an in-use indicator as for the microphone.

### 5. Other input
- **Clipboard** (BACKLOG): gcall 0x3f9 for host -> guest; find the guest -> host notification.
- **IME / text input:** `bstinput` type 1 (IME) exists. Check non-Latin keyboard layouts and
  host IME composition (CJK) on the native display window.
- **Sensors:** the accelerometer is done (gcall 0x3fc). Gyroscope/orientation is only if a game
  needs it.
- **Location:** a fixed or host-provided location (BlueStacks has a location setting), later.

## Order and size

| step | size | depends on |
|---|---|---|
| microphone | small: QEMU audio input into an existing ring | none |
| per-device privacy settings + indicators | small | program/instance settings UI |
| gamepad passthrough | medium: RE type 3 + 0x401, SDL2 reuse | controls channel |
| shared folder (vboxsf HGCM) | medium-large: HGCM protocol in vmmdev | none |
| import/export gcalls | small | gcal queue (done) |
| camera | large: RE the transport + video path | none |
| USB passthrough | large and optional | guest kernel support |

Every step gets a headless check where possible (QEMU unit-level: ring contents and counters
for audio; channel lines for gamepads) and one live check per image, Pie64 and Tiramisu64.
