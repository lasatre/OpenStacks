<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/svg/openstacks-logo-on-dark.svg">
    <img alt="OpenStacks" src="brand/svg/openstacks-logo.svg" width="360">
  </picture>
</p>

<p align="center">
  Run BlueStacks 5 Android images on QEMU, without the BlueStacks bloat.
</p>

<p align="center">
  <a href="https://github.com/lasatre/OpenStacks/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/lasatre/OpenStacks?include_prereleases&color=4F46E5"></a>
  <a href="https://github.com/lasatre/OpenStacks/actions/workflows/build.yml"><img alt="Build" src="https://github.com/lasatre/OpenStacks/actions/workflows/build.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: GPL-2.0-or-later" src="https://img.shields.io/badge/license-GPL--2.0--or--later-0D9488"></a>
  <img alt="Windows 10/11" src="https://img.shields.io/badge/platform-Windows%2010%2F11-1E1B4B">
</p>

<p align="center">
  <img alt="OpenStacks player" src="docs/screenshots/player.png" width="760">
</p>

---

OpenStacks is a Windows Android emulator built from open parts. It runs on QEMU,
BlueStacks' paravirtual devices are reimplemented as QEMU devices, graphics go through Google's
[gfxstream](https://github.com/google/gfxstream), and a custom front-end works similarly to the
BlueStacks software. The Android images come straight from BlueStacks' download servers and are
debloated on your machine. BlueStacks itself doesn't need to be installed.

> OpenStacks is not affiliated with or endorsed by BlueStacks or now.gg.

### AI Usage Disclosure

As is made clear by the commit history, this project was built entirely by AI, just with minor human guidance. It was initially built using GLM-5.3-Flash, DeepSeek V4.1 Flash, and MiMo-V2.6-Flash before being transitioned to Claude Opus 5.5 and Sonnet 5.5.

## Features

- Android 7, 9, 11 and 13, downloaded when you create an instance
- No BlueStacks launcher, store, rewards or ads; [Lawnchair](https://github.com/LawnchairLauncher/lawnchair) as the home screen
- GPU acceleration (open renderer, or BlueStacks' own per instance); ARM apps and 3D games run
- Multiple instances with their own resolution, CPU and RAM
- File transfer, APK install and drag & drop
- One-click root with Magisk
- Full ADB access

<p align="center">
  <img alt="Instance manager" src="docs/screenshots/manager.png" width="370">
  &nbsp;
  <img alt="Files window" src="docs/screenshots/files.png" width="440">
</p>

## Requirements

- Windows 10 or 11 (64-bit), with virtualization enabled
- **Windows Hypervisor Platform**: *Turn Windows features on or off*, tick it, reboot
- A GPU with up-to-date drivers (desktop OpenGL)
- Optional: **BlueStacks 5** installed, only for instances set to the BlueStacks renderer

OpenStacks checks these on first start and tells you what's missing.

## Getting started

1. Download the latest `OpenStacks-<version>-win64.zip` from Releases and extract it anywhere.
2. Run `OpenStacks.exe`.
3. Click **New**, name the instance and pick an Android version.
4. Double-click the instance to start it.

Everything OpenStacks stores goes in the `data` folder next to `OpenStacks.exe`.

### The sidebar

| Button | Shortcut | |
|---|---|---|
| Rotate | Ctrl+Shift+R | Portrait / landscape |
| Screenshot | Ctrl+S | Saved to `data\screenshots` |
| Install APK | Ctrl+I | Pick several files for split APKs |
| Files | Ctrl+Shift+F | Move files in and out |
| Root | | Install or remove Magisk |
| Pop out | Ctrl+Shift+P | Screen in its own window |

You can also drop files on the window: APKs get installed, anything else goes to the
instance's Download folder.

### ADB

Each instance's port is shown in the manager:

    adb connect 127.0.0.1:5555

## Building from source

You need [MSYS2](https://www.msys2.org) and Python 3.10+:

    pacman -S diffutils mingw-w64-ucrt-x86_64-{gcc,ninja,meson,pkgconf,python,glib2,pixman,libslirp,dtc}
    pip install -r frontend\requirements.txt

    qemu\build.cmd                  build QEMU with the OpenStacks devices
    renderer\build_gfxstream.cmd    build the open GPU renderer (VS 2022 with ClangCL)
    python frontend\run_player.py   run the app

The [build workflow](.github/workflows/build.yml) packages the release ZIP; `packaging\`
has the same steps for running locally.

For how it works and what's planned, see [NOTES.md](NOTES.md) (engineering log),
[docs/re](docs/re) (protocol specs) and [BACKLOG.md](BACKLOG.md).

## License

[GPL-2.0-or-later](LICENSE). Bundled and downloaded third-party software is listed in
[THIRD-PARTY.md](THIRD-PARTY.md).
