# OpenStacks dev notes (fresh canon, from Phase 60)

The old canon (`../grbridge/NOTES.md` §1–69, `../qemu_bst/REPORT.md`, `../FINDINGS.md`)
stays as the historical record. **Its numbering is off by 10 for the late phases**
(NOTES §69 = Phase 59). Treat its conclusions as hypotheses: §1 below refutes several.

Workspace: `dev/` (git). `dev/src/hw/misc` is the ONLY canonical device source.
Build: `dev\build.cmd`. Boot: `python dev\boot.py <run> [--root ..] [--env K=V]`.
Grade: `python dev\tools\grade.py <run> --write`. Monitor: `dev\tools\mon.py`.

## Current best configuration

    python dev\boot.py <name> --root Root_p50.vhd --data Data_fresh.qcow2 ^
        --env GR_ECHO_MODE=1 --env GR_PAIRARM=0

-> sfIdx=4 posted on every attempt, `{34,11}` served, `exePrefill 0x2715` (1636-B configs
blob) committed 6/6, no 262144 storm. The walk then dies before reading the blob (§4).

## 0. Environment (2026-11-17, F:\Projects\BluestacksReversing)

- MSYS2 ucrt64 installed (winget + pacman list from REPORT §2). `dev\build.cmd` -> Build OK.
- BlueStacks 5.21.550.1031 installed; `libOpenglRender.dll` sha256 == `refs/` copy
  (af2c643c...), so the `GR_DLL_PATH` hardcode is correct on this machine.
- WSL `Debian` has e2fsprogs 1.47.2 (debugfs/e2fsck/mke2fs/losetup), runs as root.
  No qemu-img there yet (`apt install qemu-utils` if VHD<->raw is needed).
- `qemu_bst/build.cmd` TRAP (now neutralised): it copies `qemu-patch/hw/misc/*` over
  the tree, and `qemu-patch/gr_render.c` was the STALE Nov-11 copy (2878 lines, no
  Phase 57–59 code). The tree copy (3136 lines) is what the p59 exe was built from.
  Fixed by copying the tree version over it (stale kept as `gr_render.c.stale_20261111`).
- `dev\boot.py` uses `snapshot=on` on both drives by default, so image files never drift.
  The 51 old `qemu_test\boot_bst_*.cmd` still point at the old C:\Users path (unused).
- Backups: `qemu_test\Data_fresh.qcow2.bak_p59` (the disk 59B ran on),
  `qemu_test\Data.qcow2.bak_20261112` (user-extracted).

## 1. Corrections to the old canon (evidence: archived `grbridge/shots/*_out.log`)

1. **Phase 59B ran on `Root_p54.vhd` + `Data_fresh.qcow2`** (boot_bst_p54fresh.cmd;
   serialP54F.log == phase59_bootB_serial.log byte-for-byte), not Root_dpyfix.
2. **sfIdx=4 census across every archived boot**: present in phases 43–51 and 52A (64),
   **0 in 52B and every boot after**. The regression predates the phase-57 echo drop,
   Cave C, and the fresh-Data test, so the Phase-60 "echo pacing" premise (HANDOFF §4) is
   refuted as the root cause.
3. **The "raw 0x2714 record" of 52A is not a cid=1 post.** The first 16 bytes of every
   reconnect hello (cid>=2, n=276) carry the guest goldfish-encoder record that was queued
   when the previous connection died: `{op, totalSize, args}`. The lead op tracks walk
   depth: phase 51 = 0x2711, 52A = 0x2714, 53..59 = 0x2715.
4. **From phase 53 on, the 0x2715 lead has bufSize 0x17DD0290 in every boot** (53, 56,
   57, 58A/B, 59B, and the dev setup repro), even across builds and images. Factorisation
   with the true ret=11 gives count = 0x007F4563. Those bytes appear in no served
   reply we logged. Best reading: the guest never sends numConfigs on the p52+ images; the NOPed
   value gates (state-4) let it go on with an uninitialised count.
5. **The 262144 "storm" follows the IMAGE, not the environment.** Same exe + same Data:
   Root_p54 -> wantLen=262144 x3051 in 20 s; Root_p50 -> 0 (p60A). REPORT §52's
   "environment-class / Data drift" attribution is refuted. The p52+ image edits (FIX G:
   NOP 0x18123 + all value gates NOPed + alloca mask) are the prime suspect.
6. **Phase 59b's "real small-index records are exactly 8 B" is false.** 52A's own log:
   `guestwrite cid=2 n=16 first=0x4: 04 00 .. -> exeFrame sfIdx=4`. On Root_p50 the 59b
   rule drops the guest `{4}` n=16 record on every reconnect (p60A).
7. **Phase 59 #1 (raw-0x2714 pair arm) is reachable and harmful** once `{4}` passes: our
   sfIdx=4 translation writes 0x2714 at RBASE, rcTryParse arms the pair at reply+0, and
   the exe's own prefill lands at reply+8 (wpos=16). p48ret's wpos==8 gate never matches,
   so there's no 0x2715 commit (p60B).

## 2. Phase 60 — image vs code (2 graded boots + 1 setup)

| run | image | knobs | sfIdx | lead | 262144 | 0x2715 commit |
|---|---|---|---|---|---|---|
| setup1_p59repro | p54 | default | {3:1} | 0x2715 (0x17DD0290) | 3051 | 0 |
| p60A_imgp50 | p50 | default | {1:10,3:1} | 0x2711 | 0 | 0 |
| p60B_imgp50_echo1 | p50 | ECHO_MODE=1 | {1:10,3:1,4:5} | 0x2711 | 0 | 0 |

setup1 reproduces 59B exactly (the move broke nothing). p60A: image swap kills the storm,
but 59b drops `{4}`. p60B: `{4}` passes, `{34,11}` is served, but the pair is double-staged
(§1.7).

Code: `GR_ECHO_MODE` (2 default = phase-59; 1 = 59b small-index drop off; 0 = no drops).

## 3. Phase 61 — pair arm off (1 graded boot so far)

| run | image | knobs | sfIdx | 0x2715 commit | req=1632 | bootanim |
|---|---|---|---|---|---|---|
| p61A_p50_echo1_noarm | p50 | ECHO_MODE=1, PAIRARM=0 | {1:11,3:1,4:6} | 6/6 | 0 | 0 |

Code: `GR_PAIRARM` (1 default = phase-59 arm; 0 = off). Per attempt (cid=3):
`exePrefill 0x2714 -> reply+0`, seq19 out=`22`(34), `exePrefill 0x2715 len=1636`,
seq20 ret=`0b` (rctl2715fb pos 4/8), seq21 `0b` (fb pos 8/8), then **disconnect**. Same
shape as 52A (which REPORT §52 attributes to the alloca bomb at 0x18201 on that image).

## 4. Next

1. **Freeze at the death point.** Boot the §0 config, `mon.py freeze <dir> --stay` right
   after the first `exePrefill 0x2715`, scan the 3 windows for the logd ring / tombstone
   (`Fatal signal`, `libEGL_emulation`, `eglDisplay`, `initialize+0x`). Question: does
   Root_p50's libEGL (markers image, alloca mask NOT applied) crash in config prep
   (alloca bomb) or bail on a gate?
2. If it's the alloca bomb: build ONE new image = Root_p50 + ONLY the alloca mask bound
   (0x181e2 imm 0x7fffffff0 -> 0x7ff0), with none of the gate NOPs that seem to cause the
   storm (§1.5). WSL Debian recipe in old NOTES §48/§54/§55. Leave the gates original.
3. Check whether the third 4-byte read (seq21) is part of the BST numConfigs ABI or a
   misread, by comparing with 52A seq16..18 (`0b`, guestwrite `{0xb}` n=12, `0b`, `01`).
4. Untested pieces still in the code: phase-57 reply-echo drop (ECHO_MODE=0 would test
   it), phase-59 #2–#4 reserves.

## 5. Phase 61 freeze — the death is a surfaceflinger NULL-string crash (p61B_freeze)

Freeze 3 s after the 3rd `exePrefill 0x2715` (dev/tools/freeze_on.py; dumps in
runs/p61B_freeze/freeze, ~5 GB, untracked). 4 tombstones, all identical:
`SIGSEGV fault addr 0x0` in surfaceflinger: `#00 libc strlen+17 <- #01 libutils
String8::setTo+24 <- (#02 GLExtensions::initWithEGLStrings 0xa6618 <- #03
RenderEngine::create 0xa94dd, subagent static decode) <- #04 SurfaceFlinger::init+894`.

Static decode (subagent; libs dumped from Root_p50 into dev/guestlibs/ via
tools/dump_guestlibs.sh; image libEGL_emulation.so == qemu_test/dpyfix/libEGL_emulation.p50.so):
- stock libEGL.so egl_display_t::initialize ALWAYS returns 1; if the driver's
  eglInitialize fails it only logs a warning (level 5), and the cached EGL_VERSION/EXTENSIONS
  stay NULL. RenderEngine::create -> initWithEGLStrings(NULL, ..) -> strlen(NULL).
  => **the driver's eglDisplay::initialize (0x18060) returns false.**
- The driver's queryString never returns NULL once the init flag ([s_display+0x28]) is set.
- Image p50 bytes: 0x18123 `jne 0x1832a` INTACT (rcGetEGLVersion must be 1);
  0x181b6/0x181c1/0x18220 = phase-47 marker stores (mov byte [0x53018],0xd1/d2/d3 + NOPs);
  0x181e0 alloca mask movabs 0x7fffffff0 unpatched.
- The host trace shows the guest past 0x18123 (it reaches rcGetNumConfigs), so the false
  return is later; follow-up decode pending.
- Usage: libs under /android/system/... inside the image (BlueStacks layout); the VHDs
  are DYNAMIC (type 3, 8 GiB virtual), so convert with qemu-img (now in WSL Debian).

## Pitfalls (new)

- `dev\build.cmd` taskkills QEMU, so a running boot dies on rebuild. Grade it first.
- guestwrite lines are throttled (first 24 + new shapes): counts are lower bounds.
  `recDropped` and `exeFrame` lines are not throttled.
- Boot runs reach surfaceflinger in ~20 s under WHPX; 2–3 minutes is enough to grade
  the EGL init loop.

## Language convention

Refer to the guest process as "the guest", "surfaceflinger" or "the EGL client" and use "it".
The old canon personifies it ("she/her"); don't carry that over.
