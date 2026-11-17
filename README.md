# dev/ — OpenStacks active workspace

BlueStacks 5 (Pie64) on stock QEMU 11.1 with custom paravirtual devices and BlueStacks' own
renderer (`libOpenglRender.dll`) loaded in-process. As of Phase 69 the guest boots to the
BlueStacks home screen with live display, touch, keyboard, network and audio playback.
Read `NOTES.md` for the full history and decoded protocols.

## Quick start (Windows, MSYS2 ucrt64 + BlueStacks 5.21 installed)

    dev\build.cmd                                   # configure once + ninja -> dev\build
    python dev\boot.py myrun --root Root.vhd --vnc 1   # boots; VNC on 127.0.0.1:5901
    python dev\tools\vnc_shot.py 5901 shot.png      # grab a frame
    python dev\tools\vnc_tap.py 170 294             # tap (App Center on the home screen)
    python dev\tools\vnc_type.py "hello" --enter    # type
    adb connect 127.0.0.1:25556
    adb -s 127.0.0.1:25556 shell "getprop ro.serialno >/dev/null; <command>"   # adbd allow-list

Any VNC viewer works on 127.0.0.1:5901 (the console is the renderer output; clicks become
BlueStacks Virtual Touch, keys go to the PS/2 keyboard). Audio plays to the default output
device (`--audio dsound`; `--audio wav` records to runs/<run>/audio.wav).
The PySide6 front-end in ..\openstacks launches the same configuration (instances.conf).

| Path | What |
|---|---|
| `src/hw/misc/` | canonical device sources (gr_render.c, bst_pgaipc.c, bst_vmsg.c, bst_audio.c, ...) |
| `src/apply_to_tree.py` | copies `src/hw/misc` into `../qemu_bst/qemu-11.1.0` |
| `build.cmd` | apply + configure (once) + ninja into `build/` |
| `boot.py` | one boot; logs to `runs/<name>/` (never overwrites); disks snapshot=on |
| `tools/` | grade.py, mon.py (monitor/freeze), freeze_on.py, stall_stacks.py (gdb), shot.py, vnc_shot/tap/type.py, dump_guestlibs.sh |
| `shots/` | milestone screenshots |
| `runs/` | per-boot evidence (logs ignored by git; grade.txt + meta.txt tracked) |

Defaults in boot.py = the working path: Data_fresh.qcow2, initramfs_full_p49bak.bin (pristine
bstpgaipc.ko), GR_PURE=1, PGA_SLOTSHIFT=1, GR_HPP=0, GR_RCVER=3.
