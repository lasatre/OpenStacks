<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/svg/openstacks-logo-on-dark.svg">
    <img alt="OpenStacks" src="brand/svg/openstacks-logo.svg" width="360">
  </picture>
</p>

<p align="center">
  Run BlueStacks 5 Android images on QEMU, without the Bluestacks bloat.
</p>

---

OpenStacks is a Windows Android emulator built from open parts. It runs on QEMU,
BlueStacks' paravirtual devices are reimplemented as QEMU devices, and a custom front-end that works similarly to the BlueStacks software. The Android images come straight
from BlueStacks' download servers and are debloated on your machine.

> OpenStacks is not affiliated with or endorsed by BlueStacks or now.gg.

### AI Usage Disclosure

As is made clear by the commit history, this project was built entirely by AI, just with minor human guidance. It was initially built using GLM-5.3-Flash, DeepSeek V4.1 Flash, and MiMo-V2.6-Flash before being transitioned to Claude Opus 5.5 and Sonnet 5.5.

## Features

- **Android 7, 9, 11 and 13**: BlueStacks' Nougat32/64, Pie64, Rvc64 and Tiramisu64 images.
  Each is downloaded on demand when you create an instance.
- **Debloated**: the BlueStacks launcher, game center, rewards, now.gg accounts, ads and
  billing hooks are removed. [Lawnchair](https://github.com/LawnchairLauncher/lawnchair) is
  the home screen. Google Play, Chrome and the compatibility layer for ARM apps (libhoudini)
  stay.
- **GPU-accelerated** through BlueStacks' renderer, with Hyper-V acceleration (Windows
  Hypervisor Platform) and AVX/AVX2 in the guest. Unity and other 3D games run well.
- **Multiple instances**, each with its own resolution, DPI, CPU, RAM and data disk.
  Instances can be cloned.
- **Player**: rotation, Back/Home/Recents, screenshots, pop-out screen window, APK install
  (including split APKs), and drag & drop of files onto the window.
- **Files window**: browse the instance's storage and drag files in and out.
- **One-click root** with Magisk (Kitsune), per instance and removable.
- **Full ADB**: shell, push/pull and install, on a per-instance port.

## Requirements

- Windows 10 or 11, x64, with virtualization enabled in the BIOS/UEFI.
- **Windows Hypervisor Platform** turned on: *Turn Windows features on or off* → *Windows
  Hypervisor Platform*, then reboot.
- **BlueStacks 5** installed. OpenStacks uses its GPU renderer (`libOpenglRender.dll`),
  which can't be redistributed. BlueStacks doesn't have to be running, and its own
  instances aren't touched.
- About 3 GB of disk per Android version, plus each instance's data.

On first start, OpenStacks checks these and tells you what's missing. If no adb is found,
it offers to download Google's platform-tools.

## Install and use

1. Download `OpenStacks-<version>-win64.zip` from the Releases page and extract it
   anywhere (it's portable).
2. Run `OpenStacks.exe`.
3. Click **New**, pick a name and an Android version. The first instance for each version
   downloads and prepares that image (a few hundred MB).
4. Double-click the instance to start it and open its player.

### Player sidebar

| Button | Shortcut | What it does |
|---|---|---|
| Rotate | Ctrl+Shift+R | Toggle portrait / landscape |
| Screenshot | Ctrl+S | Save a PNG to `data\screenshots` |
| Install APK | Ctrl+I | Install one APK, or several selected together (base + splits) |
| Files | Ctrl+Shift+F | Browse the instance's storage, drag files in and out |
| Root | | Install or remove Magisk (the instance restarts once) |
| Pop out | Ctrl+Shift+P | Move the screen into its own window and back |
| Back / Home / Recents | | Android navigation |

Dropping files on the player window installs APKs and copies everything else to the
instance's `Download` folder.

Closing an instance's tab, or the player window, asks whether to shut the instances down
or keep them running in the background. Closing the manager stops all instances.

### ADB

Each instance has its own ADB port, shown in the manager (5555, 5556, ...):

    adb connect 127.0.0.1:5555
    adb -s 127.0.0.1:5555 shell

Google Play, Chrome and Play services can't be updated with `adb install` until a Google
account is signed in. That's a BlueStacks rule; other APKs install normally.

### Command line

    OpenStacks.exe --launch <instance>      open the manager and start an instance (shortcuts)
    OpenStacks.exe --install-image Pie64    download and prepare an image without the GUI

### Where things are stored

Everything goes in `data\` next to `OpenStacks.exe`: Android images (`assets\`),
instance disks (`instances\`), downloads (`deps\`), logs, screenshots and
`instances.conf`. Set `OPENSTACKS_HOME` to put it somewhere else.

## How it works

```
 OpenStacks.exe (PySide6)            QEMU 11.1 + WHPX                       Android guest
 manager, player, files, root   ->   bstpgaipc  GPU command transport  <->  BlueStacks' GL/Vulkan stack
                                     gr_render  hosts BlueStacks'           (unmodified system image)
                                                libOpenglRender.dll
                                     bstvmsg    host props, touch, keys,
                                                host calls
                                     bstaudio, hvmem, vmmdev
```

- `qemu/hw/misc/` reimplements the virtual PCI devices that BlueStacks' guest kernel modules
  expect, so the system images run unmodified.
- An image is prepared by downloading it from BlueStacks' CDN and unpacking it. The kernel and
  boot ramdisk are cut from `fastboot.vdi`, and the patches in `patches/` are applied. The
  patches change only the boot ramdisk: debloat, Lawnchair, Nougat's disk driver, and the
  optional per-instance Magisk.
- [`NOTES.md`](NOTES.md) is the engineering log: decoded protocols, every phase, and the
  pitfalls. Specs are in [`docs/re/`](docs/re/). Planned work is in [`BACKLOG.md`](BACKLOG.md),
  including Linux support.

## Building from source

Requirements:

- [MSYS2](https://www.msys2.org) with the UCRT64 toolchain:

      pacman -S diffutils mingw-w64-ucrt-x86_64-{gcc,ninja,meson,pkgconf,python,glib2,pixman,libslirp,dtc}

- Python 3.10+ and `pip install -r frontend\requirements.txt` (PySide6).
- 7-Zip on PATH or in its default install folder.

Build and run:

    qemu\build.cmd                     QEMU 11.1.0 + devices -> build\ (MSYS2_ROOT if not C:\msys64)
    python frontend\run_player.py      run the front-end from source

Package a release like CI does:

    pip install pyinstaller
    pyinstaller --noconfirm --distpath dist --workpath build-pyi packaging\openstacks.spec
    python packaging\assemble.py --version 0.1.0 --sevenzip path\to\7zr.exe --zip
                                       -> release\OpenStacks-0.1.0-win64.zip

[`.github/workflows/build.yml`](.github/workflows/build.yml) builds the ZIP on every push.
Tags `v*` create a draft GitHub release.

Developer tools (from a source checkout):

| Tool | Use |
|---|---|
| `boot.py` | Headless boot with logs in `runs\<name>\`; disks are snapshots unless `--persist` |
| `tools\prepare_assets.py` | `--list`, `--download <Image>`, `--repatch all` |
| `tools\vnc_shot.py`, `vnc_tap.py`, `vnc_type.py` | Drive a headless boot over VNC |
| `tools\fps.py`, `grade.py`, `mon.py` | Frame rate, boot grading, QEMU monitor |

## Repository layout

| Path | Contents |
|---|---|
| `qemu/` | Device sources (`hw/misc/`), `apply_to_tree.py`, `build.cmd` |
| `frontend/` | The PySide6 app (`openstacks/`) and `run_player.py` |
| `patches/` | Image patches, applied when an image is installed |
| `packaging/` | PyInstaller spec, release assembly, version stamping |
| `brand/` | Logo, icons, colors ([`brand/README.md`](brand/README.md)) |
| `tools/`, `boot.py` | Developer tools |
| `docs/` | Reverse-engineering specs and milestone screenshots |
| `NOTES.md`, `BACKLOG.md` | Engineering log, planned work |

## License

OpenStacks is licensed under the [GNU General Public License v2.0 or later](LICENSE)
(GPL-2.0-or-later).

## Third-party software

The QEMU-based parts are GPL-2.0. The release bundles Qt/PySide6 (LGPL-3.0), 7-Zip
(LGPL-2.1) and MSYS2 runtime libraries. Lawnchair, Kitsune Magisk and Android
platform-tools are downloaded on demand. See [`THIRD-PARTY.md`](THIRD-PARTY.md) for details.
