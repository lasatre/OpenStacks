# Third-party software

## Shipped in the release ZIP

| Component | Where | License | Source |
|---|---|---|---|
| QEMU 11.1.0 (with the OpenStacks devices in `qemu/hw/misc/`) | `qemu/qemu-system-x86_64.exe`, `qemu/pc-bios/` | GPL-2.0 (firmware in `pc-bios/`: see QEMU's `pc-bios/README`) | https://download.qemu.org/qemu-11.1.0.tar.xz, plus `qemu/` in this repository |
| GLib, GIO, GObject, GModule | `qemu/lib{glib,gio,gobject,gmodule}-2.0-0.dll` | LGPL-2.1-or-later | MSYS2 `mingw-w64-ucrt-x86_64-glib2` |
| gettext runtime (libintl) | `qemu/libintl-8.dll` | LGPL-2.1-or-later | MSYS2 `mingw-w64-ucrt-x86_64-gettext-runtime` |
| libiconv | `qemu/libiconv-2.dll` | LGPL-2.1-or-later | MSYS2 `mingw-w64-ucrt-x86_64-libiconv` |
| libslirp | `qemu/libslirp-0.dll` | BSD-3-Clause | MSYS2 `mingw-w64-ucrt-x86_64-libslirp` |
| pixman | `qemu/libpixman-1-0.dll` | MIT | MSYS2 `mingw-w64-ucrt-x86_64-pixman` |
| dtc (libfdt) | `qemu/libfdt-1.dll` | GPL-2.0-or-later | MSYS2 `mingw-w64-ucrt-x86_64-dtc` |
| PCRE2 | `qemu/libpcre2-8-0.dll` | BSD-3-Clause | MSYS2 `mingw-w64-ucrt-x86_64-pcre2` |
| libffi | `qemu/libffi-8.dll` | MIT | MSYS2 `mingw-w64-ucrt-x86_64-libffi` |
| zlib | `qemu/zlib1.dll`, `qemu/libz-1.dll` | Zlib | MSYS2 `mingw-w64-ucrt-x86_64-zlib` |
| zstd | `qemu/libzstd.dll` | BSD-3-Clause or GPL-2.0-or-later | MSYS2 `mingw-w64-ucrt-x86_64-zstd` |
| bzip2 | `qemu/libbz2-1.dll` | bzip2 license (BSD-style) | MSYS2 `mingw-w64-ucrt-x86_64-bzip2` |
| ncurses | `qemu/libncursesw6.dll` | MIT | MSYS2 `mingw-w64-ucrt-x86_64-ncurses` |
| mingw-w64 winpthreads | `qemu/libwinpthread-1.dll` | MIT and BSD-3-Clause | MSYS2 `mingw-w64-ucrt-x86_64-libwinpthread` |
| Python 3.12 | `_internal/` | PSF-2.0 | https://www.python.org |
| Qt 6 / PySide6 | `_internal/PySide6/` | LGPL-3.0 | https://www.qt.io, https://pypi.org/project/PySide6/ |
| PyInstaller bootloader | `OpenStacks.exe` | GPL-2.0 with the bootloader exception | https://pyinstaller.org |
| 7-Zip 26.06 (`7zr.exe`) | `tools/7zr.exe` | LGPL-2.1 | https://github.com/ip7z/7zip/releases/tag/26.06 |
| Sora (brand wordmark, outlined) | `_internal/brand/` | SIL OFL-1.1 (`brand/font-license/Sora-OFL.txt`) | https://fonts.google.com/specimen/Sora |
| gfxstream (with `renderer/gfxstream-patches/`) | `qemu/ost_renderer.dll` | Apache-2.0 | https://github.com/google/gfxstream (pinned commit) |
| ANGLE shader translator (Android emulator build 16740729) | `qemu/libshadertranslator.dll` | BSD-3-Clause (`renderer/ANGLE-LICENSE.txt`) | https://dl.google.com/android/repository/emulator-windows_x64-16740729.zip (pinned SHA-1/SHA-256); header from AOSP `platform/external/angle` (`emu-master-dev`) |

The LGPL libraries are dynamically linked and can be replaced in place.

## Shipped in the Linux AppImage

Paths are inside the AppImage (`OpenStacks/`); the AppImage extracts with
`--appimage-extract`.

| Component | Where | License | Source |
|---|---|---|---|
| QEMU 11.1.0 (with the OpenStacks devices in `qemu/hw/misc/`) | `qemu/qemu-system-x86_64`, `qemu/pc-bios/` | GPL-2.0 (firmware in `pc-bios/`: see QEMU's `pc-bios/README`) | https://download.qemu.org/qemu-11.1.0.tar.xz, plus `qemu/` in this repository |
| libslirp | `qemu/lib/libslirp.so.0` | BSD-3-Clause | Ubuntu 22.04 `libslirp0` |
| dtc (libfdt) | `qemu/lib/libfdt.so.1` | BSD-2-Clause or GPL-2.0-or-later | Ubuntu 22.04 `libfdt1` |
| pixman | `qemu/lib/libpixman-1.so.0` | MIT | Ubuntu 22.04 `libpixman-1-0` |
| gfxstream (with `renderer/gfxstream-patches/`) | `qemu/libost_renderer.so` | Apache-2.0 | https://github.com/google/gfxstream (pinned commit) |
| ANGLE shader translator (Android emulator build 16740729) | `qemu/libshadertranslator.so` | BSD-3-Clause (`renderer/ANGLE-LICENSE.txt`) | https://dl.google.com/android/repository/emulator-linux_x64-16740729.zip (pinned SHA-1/SHA-256) |
| LLVM libc++ (the shader translator's C++ runtime, same package) | `qemu/libc++.so` | Apache-2.0 WITH LLVM-exception (`renderer/LIBCXX-LICENSE.txt`) | same as above |
| Python 3, Qt 6 / PySide6, PyInstaller bootloader | `OpenStacks`, `_internal/` | PSF-2.0; LGPL-3.0; GPL-2.0 with the bootloader exception | as in the Windows ZIP |
| Qt's runtime libraries (xcb, fontconfig, FreeType, ICU, ...) | `_internal/` | their own (mostly MIT, LGPL, FTL); see Ubuntu 22.04's `/usr/share/doc/<package>/copyright` | Ubuntu 22.04 packages, collected by PyInstaller |
| 7-Zip 26.06 (`7zzs`, static) | `tools/7zz` | LGPL-2.1, BSD parts, unRAR restriction (`License.txt` in the 7-Zip archive) | https://github.com/ip7z/7zip/releases/tag/26.06 |
| AppImage type2-runtime 20251108 | the AppImage's own header | MIT | https://github.com/AppImage/type2-runtime/releases/tag/20251108 |
| Sora (brand wordmark, outlined) | `_internal/brand/` | SIL OFL-1.1 (`brand/font-license/Sora-OFL.txt`) | https://fonts.google.com/specimen/Sora |

GLib, libpulse, zlib, X11/GL and the C/C++ runtimes come from the system.

## Downloaded on demand (not shipped)

| Component | When | License / terms | Source |
|---|---|---|---|
| BlueStacks 5 Android images (incl. libhoudini) | when you create an instance for an image | BlueStacks' terms | BlueStacks' public CDN (`cdn3.bluestacks.com`) |
| Lawnchair | while an image is installed | GPL-3.0 | https://github.com/LawnchairLauncher/lawnchair/releases (pinned SHA-256) |
| Kitsune Magisk v27.2-kitsune-4 | the player's Root button | GPL-3.0 | https://github.com/AndnixSH/KitsuneMagisk/releases (pinned SHA-256) |
| Android SDK platform-tools (adb) | first-run setup, if no adb is found | Android SDK license | https://dl.google.com/android/repository/platform-tools-latest-windows.zip (`-linux.zip` on Linux) |

## Required, not shipped

Windows only, and only for instances set to the BlueStacks GPU renderer (new instances use the open renderer,
gfxstream, which needs nothing from BlueStacks): BlueStacks 5's GPU renderer (`libOpenglRender.dll` and its folder) is proprietary and is loaded from
your own BlueStacks 5 installation. OpenStacks is not affiliated with or endorsed by BlueStacks or now.gg.
