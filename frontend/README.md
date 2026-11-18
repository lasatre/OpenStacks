# OpenStacks front-end

The PySide6 app: the instance manager and the player. See the [main README](../README.md)
for setup and usage.

    pip install -r requirements.txt
    python run_player.py                       # manager; double-click an instance to start it
    python run_player.py --smoke               # import/bundle self-check (used by CI)

## Configuration

`instances.conf` holds the global settings and one section per instance; the manager writes
it. In a source checkout it lives here (gitignored, see `instances.conf.example`); in the
release it's in `data\`. Empty path settings use the defaults from `openstacks/paths.py`:

| Setting | Default |
|---|---|
| `qemu_path` | `<repo>\build\qemu-system-x86_64.exe` (release: `qemu\`) |
| `assets_dir` | `<data>\assets\<Image>` (prepared images) |
| `instances_dir` | `<data>\instances` (each instance gets `<name>\Data.vhdx`) |
| `adb_path` | adb on PATH, `C:\Path\platform-tools`, the Android SDK, `<data>\deps\platform-tools`, BlueStacks' HD-Adb last |

`OST_RENDERER_DLL` overrides where BlueStacks' `libOpenglRender.dll` is loaded from.

## Modules

| File | Role |
|---|---|
| `openstacks/manager.py` | Instance manager window, first-run checks, About |
| `openstacks/player.py` | Player window: tabs, sidebar, root and restart flows |
| `openstacks/qemu_engine.py` | QEMU command line (devices, CPU, disks), start/stop, logs |
| `openstacks/images.py` | Image download, unpack, patch and assets |
| `openstacks/files.py` | File transfer window |
| `openstacks/magisk.py` | Magisk install/remove over adb |
| `openstacks/checks.py` | Prerequisite checks, platform-tools download |
| `openstacks/paths.py` | Source-checkout vs. release file locations |
| `openstacks/brand.py` | Icon, logo, colors |
| `openstacks/config.py` | `instances.conf` model |
| `openstacks/adb.py` | adb wrapper (shell, install, push/pull, screencap) |
| `openstacks/native.py`, `display.py`, `vnc.py` | Native GPU window embedding; VNC fallback display |
| `openstacks/bstprops.py` | BlueStacks-style `bst.*` host properties |

Logs per instance: `logs\<name>.qemu.log` (QEMU and device output), `logs\<name>.serial.log`
(guest kernel console), `logs\<name>.bst.props` (props served to the guest).
