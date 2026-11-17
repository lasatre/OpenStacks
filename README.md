# OpenStacks

BlueStacks 5 (Pie64, Android 9 x86_64) running on stock **QEMU 11.1** with
reimplemented BlueStacks paravirtual devices and BlueStacks' own renderer
(`libOpenglRender.dll`) loaded in-process. It boots BlueStacks' unmodified images to the
home screen with live display, touch, keyboard, network, audio, full ADB and
third-party/Unity apps (ARM translation via the guest's libhoudini).

Nothing BlueStacks-owned is stored in this repo: `tools/prepare_assets.py` (or the manager's
New dialog) downloads the Android images from BlueStacks' CDN, or takes them from a local
BlueStacks 5 install, and prepares them locally.

## Requirements (Windows 10/11 x64)

- **BlueStacks 5** installed (tested 5.21.550) for `libOpenglRender.dll`. The Android images
  themselves are downloaded from BlueStacks' CDN (or taken from the install).
- **7-Zip** (to unpack downloaded images).
- **Windows Hypervisor Platform** enabled (QEMU uses WHPX).
- **MSYS2** at `C:\msys64` with the ucrt64 toolchain, to build QEMU:
  `pacman -S mingw-w64-ucrt-x86_64-{gcc,ninja,meson,python,glib2,pixman,libslirp,pkgconf}`
- **Python 3.10+** (`pip install PySide6` for the front-end).
- **adb** from Android platform-tools (not BlueStacks' `HD-Adb.exe`).

## Setup

    qemu\build.cmd                      # fetch QEMU 11.1.0 -> deps\, add devices, build -> build\
    python tools\prepare_assets.py --download Pie64   # Android 9 -> assets\Pie64\ (also Rvc64 =
                                                       # Android 11, Nougat64/32 = Android 7)
    python tools\prepare_assets.py --list              # what is available / installed

The manager's **New** dialog can also download an image (pick the Android version).

## Run

Front-end (instance manager + player windows):

    python frontend\run_player.py

Headless dev boot (logs to `runs\<name>\`, disks snapshot=on so nothing is written):

    python boot.py myrun --vnc 1                   # VNC on 127.0.0.1:5901
    python tools\vnc_shot.py 5901 shot.png         # grab a frame
    python tools\vnc_tap.py 170 294                # tap
    python tools\vnc_type.py "hello" --enter       # type
    adb connect 127.0.0.1:25556                    # full adb: shell, push/pull, install

Chrome / Play Store / Google Play services refuse `adb install` until a Google account is
signed in (a BlueStacks rule); every other APK installs normally.

## Layout

| path | what |
|---|---|
| `qemu/hw/misc/` | device sources: `bst_pgaipc.c` (graphics transport) + `gr_render.c` (renderer bridge, HPP), `bst_vmsg.c` (host props, touch/keys), `bst_audio.c`, `hvmem.c`, `vmmdev.c` |
| `qemu/apply_to_tree.py`, `qemu/build.cmd` | add the devices to a QEMU tree and build |
| `frontend/` | PySide6 front-end (`frontend/README.md`) |
| `boot.py` | one headless dev boot with the working configuration |
| `tools/` | `prepare_assets.py`, `bstprops.py`, VNC/adb helpers, `grade.py`, `mon.py` (QEMU monitor), `dump_guestlibs.sh` (pull guest files out of Root.vhd) |
| `docs/re/` | protocol specs from reverse engineering (`hpp_spec.md`, `hststream_notes.md`); raw dumps in `docs/re/raw/` |
| `docs/shots/` | milestone screenshots |
| `NOTES.md` | the engineering log: every phase, decoded protocols, pitfalls |
| `BACKLOG.md` | known, not-yet-done items worth doing later |
| `runs/` | per-boot evidence (`meta.txt`/`grade.txt` tracked, logs local) |
| `build/`, `deps/`, `assets/`, `instances/`, `work/` | generated / local, gitignored |

## Configuration knobs

- Guest CPU: `qemu64,+ssse3,+sse4.1,+sse4.2,+popcnt` (`boot.py --cpu`, front-end env
  `OST_CPU`). `-cpu host` (AVX) crashes the guest under WHPX.
- Host props sent to the guest (`bst.oem`, `bst.version`, `bst.abi_list`, the adb gate,
  ...): built from `C:\ProgramData\BlueStacks_nxt\bluestacks.conf` like HD-Player does;
  `boot.py --prop key=value` overrides one.
- Renderer env knobs (`GR_PURE=1`, `PGA_SLOTSHIFT=1`, ...) are set by `boot.py` and the
  front-end; see NOTES.md "Current best configuration".

## Known gaps

See [BACKLOG.md](BACKLOG.md) for everything known but not done yet.
