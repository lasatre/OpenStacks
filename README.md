# dev/ — OpenStacks active workspace

Fresh, git-tracked workspace started 2026-11-17. Everything outside `dev/` is the legacy
project (docs, images, evidence). Read `NOTES.md` first.

| Path | What |
|---|---|
| `src/hw/misc/` | canonical device sources (gr_render.c etc.) — edit here only |
| `src/apply_to_tree.py` | copies `src/hw/misc` into `../qemu_bst/qemu-11.1.0` |
| `build.cmd` | apply + configure (once) + ninja into `build/` (needs MSYS2 ucrt64) |
| `boot.py` | launches one boot; logs to `runs/<name>/` (never overwrites); disks snapshot=on |
| `tools/grade.py` | grades a run (`--write` saves `grade.txt`) or an archived log (`--log`) |
| `tools/mon.py` | QEMU monitor: `cmd`, `freeze <dir> [--stay]` (3-window pmemsave), `quit` |
| `runs/` | per-boot evidence (logs ignored by git; `grade.txt` + `meta.txt` tracked) |

Runtime knobs (env, via `boot.py --env`): `GR_ECHO_MODE` (0/1/2), `GR_PAIRARM` (0/1),
plus the legacy `GR_BRIDGE/GR_RCVER=3/GR_HPP/GR_SYN_MODE/GR_RENDERER_MODE` set by boot.py.
