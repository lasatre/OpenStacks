# OpenStacks front-end (PySide6)

A BlueStacks-style desktop shell over the OpenStacks QEMU build: an **instance manager**
(list, launch/stop, clone, settings) and a **player** window per instance with the live
screen (VNC client), a serial console tail, a diagnostics tail and ADB tooling.

## Run

    pip install -r requirements.txt        # PySide6 (or: setup_env.cmd for a .venv)
    python run_player.py                   # opens the instance manager

Prerequisites (see the repo README): `qemu\build.cmd` has produced `build\`, and
`tools\prepare_assets.py` has produced `assets\Pie64\`. Double-click an instance to boot it
and open its player.

## Configuration

`instances.conf` (gitignored; created on first save, see `instances.conf.example`).
Per instance: cpus, ram_mb, resolution, dpi, adb/vnc/monitor ports, android_id, disks,
device set. Empty path settings use the repo layout:

| setting | default |
|---|---|
| `qemu_path` | `<repo>\build\qemu-system-x86_64.exe` |
| `assets_dir` | `<repo>\assets\Pie64` (kernel, initramfs, Root.vhd, Data template) |
| `instances_dir` | `<repo>\instances` (each instance gets `<name>\Data.vhdx`) |
| `adb_path` | adb on PATH, then `C:\Path\platform-tools`, BlueStacks' HD-Adb last |

`data_disk` empty = `instances\<name>\Data.vhdx`, copied from BlueStacks' blank data
template on first launch. An absolute path keeps using an existing disk.

## Modules

| file | role |
|---|---|
| `openstacks/manager.py` | instance manager window |
| `openstacks/player.py` | player window: Screen / Console / Diagnostics tabs, side toolbar |
| `openstacks/qemu_engine.py` | builds the QEMU argv (devices, CPU model, disks, VNC, audio), starts/stops QEMU, logs |
| `openstacks/bstprops.py` | HD-Player-style `bst.*` host props from bluestacks.conf (copy of `tools/bstprops.py`) |
| `openstacks/config.py` | instances.conf model and path defaults |
| `openstacks/adb.py` | adb wrapper (connect, shell, install incl. split APKs, screencap) |
| `openstacks/vnc.py`, `display.py` | RFB client and the screen widget (input -> touch/keys) |
| `openstacks/console.py` | log tail widgets |
| `openstacks/selftest.py` | `python -m openstacks.selftest` (no VM needed) |

Logs per instance: `logs\<name>.qemu.log` (QEMU + device lines), `logs\<name>.serial.log`
(guest kernel console), `logs\<name>.bst.props` (props served to the guest).
