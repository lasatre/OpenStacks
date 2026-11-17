# OpenStacks dev notes (fresh canon, from Phase 60)

The old canon (`../grbridge/NOTES.md` §1–69, `../qemu_bst/REPORT.md`, `../FINDINGS.md`)
stays as the historical record. **Its numbering is off by 10 for the late phases**
(NOTES §69 = Phase 59). Treat its conclusions as hypotheses: §1 below refutes several.

Workspace: `dev/` (git; layout reorganised in Phase 74, see §10l and README.md).
`qemu/hw/misc` is the ONLY canonical device source (older entries say `src/hw/misc`).
Build: `qemu\build.cmd`. Boot: `python boot.py <run> [--vnc 1] [--env K=V]`.
Assets: `python tools\prepare_assets.py`. Grade: `python tools\grade.py <run> --write`.
Monitor: `tools\mon.py` (port 4456).

## Current best configuration (Phase 74: everything from the user's BlueStacks install)

    python boot.py <name> --vnc 1

boot.py defaults are the working path: assets/Pie64 (kernel.bin + pristine initramfs.bin
cut from fastboot.vdi, Root.vhd, Data_template.vhdx = BlueStacks' blank Data_orig.vhdx),
CPU qemu64+SSE4, host props from bluestacks.conf, PGA_SLOTSHIFT=1, GR_PURE=1, GR_HPP=0.
(Before Phase 74: --root Root.vhd with ../qemu_test images, initramfs_full_p49bak.bin.)

-> sys.boot_completed=1 (~3 min), adb works (refs\HD-Adb.exe connect 127.0.0.1:25556),
focus = com.uncube.launcher3/HomeActivity; 24 GL clients (surfaceflinger, systemui,
launcher, Play Store, GMS, settings, system_server). Screenshot:
dev/shots/phase65_bluestacks_home.png. History of how we got here: §5–§10.

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

## 6. The "small-index records" are host residue, not guest data (Phase 62)

1. Follow-up decode (subagent): eglDisplay::initialize has NO false-return path after
   rcGetNumConfigs (0x181ad) on the p50 image (all later gates are marker stores; 0x18354
   is unreachable). rcGetNumConfigs_enc (lib_renderControl_enc 0x94a0) = write
   {0x2714,0xc,4} + exactly two 4-byte readbacks (numAttribs, then ret = numConfigs);
   rcGetConfigs_enc (0x95c0) = write {0x2715,0x10,bs,bs} + readback(buf, bs) + readback(ret,4).
   Our 34/11 match numAttribs=34, numConfigs=11 (bs = 34*12*4 = 1632). The observed THIRD
   4-byte read matches neither encoder function.
2. p61A serve/write interleave: the first dword of EVERY small guestwrite equals the reply
   served immediately before it (03->03, ANDR->ANDR, GL E->GL E, 01->01, 04->04). The
   "[idx][arg] index records" that phases 12-59 translated into sfIdx frames are
   our own read reply left at win[0..4).
3. p62A (GR_WINDUMP=1, dev build knob): for every write after the hello, the nonzero extent
   of win[0..0x10000) is [0..8], exactly the residue of our last reply plus our own
   post-relocation zero-fill. The guest's 8/8/16-byte writes add NO bytes to the client
   region. On a fresh connection the real goldfish record ({0x2711,16,4,4} =
   rcGetEGLVersion) IS at win[0..16), overlaying the hello (doorbell len=276).
   => The guest's stream protocol is still unknown: the records go somewhere other than
   win[0..0x10000), or the small doorbells carry no payload. HstStream decode
   (libOpenglSystemCommon, OpenCode DeepSeek V4.1 Flash) is in flight.
4. Consequence: everything keyed on sfIdx (exeFrame translation, pair machinery, the 0x2715
   commit) is driven by our own reply bytes. Its "success" in p61 is coincidental pacing,
   not protocol fidelity.

## 7. ROOT CAUSE: device slot mapping off by one (Phase 63)

1. HstStream decode (OpenCode Go DeepSeek V4.1 Flash; raw notes dev/re_scratch/hststream_notes.md),
   libOpenglSystemCommon: vtable _ZTV9HstStream 0x2a348. alloc (+0x50 -> hstAlloc 0x192d0)
   returns window+wcur (wcur += len; heap buffer if len > windowSize). flush (hstFlush
   0x18fb0) = WRITE ioctl 0x40087004 with len = wcur and data at window+0, then wcur = 0.
   readback (+0x60 -> hstRecvCopy 0x19270 -> hstRecv 0x18b70) flushes first, then
   READ ioctl 0x40087003 with want = the full length; the guest copies `want` bytes from
   window+0. commitBuffer/readFully are no-ops. The first WRITE after connect is the
   0x100-B handshake plus the first record (276 = 0x100 + 20). The ioctl arg is 8 B but
   only dword 0 is set; dword 1 = stack canary low half (ignore).
   Records are plain goldfish {u32 op, u32 len, payload}: there is NO index-record layer.
2. p62B (recscan: scan every slot window on each WRITE for goldfish records): guest
   channel c's records are in OUR slot c+1. Channel 1: {0x2713,20,0x1F03,0x400} at
   slot2+0x100 (after the handshake), then {0x2710,8} and {0x2711,16,4,4} at slot2+0.
   Slot 0 = control page (mailboxes at cid*0x10, served-cid at +0x870), so the windows
   start at BAR1 + (c+1)*0x40000. bst_pgaipc.c pga_window used c*0x40000.
   => **Since Phase 1 the guest never received one reply byte we served** (it read
   back its own records), and the host never saw a real record after the first one.
   Phases 12–62's sfIdx/echo/pair/commit machinery was built on that artifact.
3. Fix (knob PGA_SLOTSHIFT=1, bst_pgaipc.c): p63A shows the real op stream above and no
   storm, with the guest reaching rcChooseConfig. The served replies are still
   synthesised by the legacy shims (e.g. the 0x2710 reply), so they are meaningless.
   Big slots (cid >= 0x7f, BAR2) are untested for the same shift.

## 8. Next (redesign, not tweaks)

With the transport correct, the render lane should be a faithful pipe:
guest WRITE bytes (slot c+1, [0..len)) -> libOpenglRender's own decoder (the HstSocketHost
vtable recv path, exe semantics) -> its hstAlloc/hstSend reply bytes -> slot c+1 [0..want)
on READ. Plan:
1. Make PGA_SLOTSHIFT=1 the default.
2. Add GR_PURE=1: bypass every synthetic stage (sfIdx translation, rcInit step machine,
   prefill builders, pair/commit/fallback reserves, echo drops, synOp, string-class clamps)
   so OP_WRITE only appends the guest's bytes to the exe record lane, and OP_READ only serves
   what the exe staged (blocking up to a bound for the exe's reply). Keep the knob-off path
   intact for A/B.
3. Boot GR_PURE=1 and grade: rc op census, eglChooseConfig/eglCreateContext progress,
   bootanim, screen_pga.bmp.
4. Re-test on Root.vhd (pristine, no libEGL patches) once the pure lane works: most image
   patches compensated for garbage replies and should become unnecessary.

## 9. Phase 64: GR_PURE, a faithful HstSocketHost lane (first Android UI)

Code: gr_render.c "Phase-64 GR_PURE" block (pure_* / pvt_*), installed as a separate
vtable (g_vt_pure) when GR_PURE=1; OP_WRITE/OP_READ branch early into pure_write/pure_read.
All legacy shims stay intact for GR_PURE=0.

1. Contract (decoded, not guessed):
   - Guest (libOpenglSystemCommon HstStream, §7.1): WRITE(len) = records at window[0..len);
     READ(want) copies `want` bytes from window+0; ret = bytes served.
   - HD-Player hstPeekTimed 0x1402abde0: flush first (0x1402aabc0), then return data if
     avail >= needed; else ack the current request (0x1402aa750) and wait (0x1402acc90):
     0 = new request, 0x6e = ETIMEDOUT, 0x68 = disconnect. Flush: replies larger than the
     window go out in window-sized chunks (one guest READ each); small ones get ret = sendWpos.
     vt+0x38 = destroy (frees the 0x228 sock).
   - DLL RenderThread::main (libOpenglRender 0x18012b64c..849, Sonnet subagent decode):
     createFinish(+0x50); loop { +0x40 (release prev packet); peek(+0x08, 8, &out)
     (NO timeout argument: r9 is stale); recv(+0x00, size) = full packet incl. header;
     decoders }. ANY non-zero peek/recv return ends the thread and leads to destroy.
     Decoders alloc replies via +0x30 and flush via +0x28 or the next peek.
2. Implementation: guest bytes are read in place from the guest window. The WRITE is acked
   when the DLL consumes its last byte (tail copied out first) or before a flush. Replies are
   staged in a private 8 MiB buffer and delivered by pure_flush (called from peek and +0x28)
   on the guest READ, chunked by window size. createFinish consumes the 0x100 handshake
   (parses "%s (pid %d, tid %d) %d %d %d"; HPP only if the 3rd int != 0; sf sends "0 0 0").
3. Boots (Root_p50, Data_fresh, PGA_SLOTSHIFT=1, GR_HPP=0):
   - p64A/B: peek returned -1/0x6e on "no data" -> DLL destroy right after the first reply.
   - p64D (flush-in-peek, blocking peek): the full real renderControl conversation:
     GLString, RendererVersion=1, EGLVersion {1,4}, NumConfigs (34 attribs, 10 configs),
     GetConfigs 1500 B, EGL vendor/ext strings, ChooseConfig, CreateContext=1,
     CreateWindowSurface=2, CreateColorBuffer=3, MakeCurrent, ... 854 writes / 681 replies.
     Stalled at rcReadColorBuffer (0x2727, 5.76 MB reply > 256 KiB window: no chunking).
   - p64E (chunked flush): 7 channels: surfaceflinger x2 (cid0 = the rcFBPost lane),
     com.android.settings x2, com.android.systemui x2, system_server. Then a stall.
   - p64F (+ the render window moved to a UI thread with a message loop; it used to be made
     on the worker, which never pumps messages): screen_pga.bmp = real composited frame,
     the status-bar clock "9:10" (5,758,200 of 5,760,000 bytes nonzero).
4. Open: all channels stop after roughly 20-60 s with WRITEs unconsumed (the DLL render
   threads are blocked inside the DLL, not in our callbacks; no "waiting >3 s" flush logs).
   Last ops: cid0 rcFBPost (0x2722), cid1 rcSetWindowColorBuffer-class 0x271f, GLES
   traffic on the app channels. Suspects: a DLL-internal lock held across the post
   callback / readPixels lane (readpix_engine calls FB getPixels every ~10 s; post_install
   starts PgaStartReadPixels), a sync/fence wait (rcCreateSyncKHR family + SyncThread),
   or the DLL expecting HD-Player's display/subwindow callbacks. Next: per-thread stack
   snapshot of the QEMU process during the stall (e.g. procdump / WinDbg `~*k`) to see
   where the DLL threads wait.
5. Colour: the dump looks dark red; check the channel order in bmp_write/the PNG converter
   (the BMP is written as-is from the DLL's buffer, so it may be RGBA vs BGRA).

## 10. Phase 65: the "stall" was two bugs (now fixed). Android boots fully.

1. gdb thread dump (dev/tools/stall_stacks.py): DLL render threads idle in our pure_take,
   no host deadlock. The pstall watchdog showed every channel drained, acked and answered:
   the guest itself had stopped.
2. Bug 1 (guest kernel panic at about 23 s, in every p64 run; `mon info status` =
   "paused (shutdown)" because of -no-reboot): Oops in bstpgaipc_read_write <-
   bstpgaipc_fops_compat_ioctl, comm=ncube.launcher3 (a 32-bit app). The write went to
   ctl_page - 0xc = the mailbox of cid -1 (open() sets cid = -1; the probe maps one page,
   ioremap_nocache(BAR1, 0x1000)). Cause: the Phase-50/51 .ko patch (ko sha 78600deb in
   initramfs_full.bin) put a code cave in compat_ioctl, assuming it was dead in an
   "x64-only guest". It is not: 32-bit apps use it. The pristine compat_ioctl (ko 495ad40d)
   re-encodes _IOW size 4 -> 8 for cmds 3/4 and forwards everything to fops_ioctl.
   Fix: boot qemu_test/initramfs_full_p49bak.bin (pristine .ko; the only other delta vs
   initramfs_full.bin is the phase-55 blank-Data formatter in bstsetup.env). boot.py
   gained --initrd.
3. Bug 2 (lost completions): with the pristine .ko there was no panic, but system_server
   was killed by the Android Watchdog. Its android.ui thread held the AMS and WM locks while
   blocked in BpSurfaceComposer::captureScreen, and SurfaceFlinger was the only native
   process debuggerd could not dump (stuck in the .ko). Cause: bst_pgaipc pga_complete wrote
   the single served-cid register (BAR1+0x870) even while the previous completion's IRQ was
   unacked, so the earlier cid was lost and its waiter slept forever. HD-Player's
   hstRaiseInterruptAcked (0x1402aa750) waits for the ack first. Fix: a completion FIFO;
   the next cid is delivered on the BAR0+8 ack (queue depth reached 2 in p65G).
4. Side findings: the DLL's real GL extension string has no native_sync and no
   host_composition (Vulkan features + GLES 3.2), so GR_NO_NATIVESYNC was a dead end (knob
   kept, harmless). mon.py now resolves dump paths to absolute (QEMU's cwd is the run
   dir). HD-Adb (v36) kills a newer adb server (v41) on the host when it starts. The QEMU
   process also hosts RivaTuner's Vulkan layer and both NVIDIA and AMD ICDs (dual GPU):
   keep in mind if GPU issues appear.
5. Result p65G_cq: boot_completed=1, the BlueStacks home screen renders.

## 10b. Phase 66: pristine Root.vhd boots too; colour fix

- p66A_rootpristine: --root Root.vhd (pristine, no libEGL patches) + the Phase-65 defaults
  gives boot_completed=1, launcher HomeActivity focused, 23 GL clients, no panic. The p50
  image patches are no longer needed.
- Colour: the DLL frame buffer is RGBA, but bmp_write dumped it as BMP (BGRA), so captures
  looked dark red. The real BlueStacks background is dark blue. bmp_write now converts;
  dev/tools/shot.py makes PNGs (--rgba for older captures). Screens:
  dev/shots/phase66_home_pristine_root.png (correct colours).

## 10c. Phase 66: interaction over ADB works; BlueStacks adbd allow-list

- BlueStacks' adbd only allows certain FIRST words in `adb shell` commands (getprop,
  dumpsys, ... are allowed; echo/id/uptime/am/toybox get an immediate CLSE = "error: closed").
  Workaround: start every command with an allowed word, e.g.
  `adb -s 127.0.0.1:25556 shell "getprop ro.serialno >/dev/null; am start -a android.settings.SETTINGS"`.
  (Not fd exhaustion: the raw CNXN/OPEN probe shows CLSE on the first OPEN of a fresh connection.)
- Use one adb only: the system adb (C:\Path\platform-tools, v41) and refs/HD-Adb.exe (v36)
  kill each other's server.
- Settings opened via am start renders correctly (dev/shots/phase66_settings.png).

## 10d. Phase 67: live display over VNC; front-end updated

- bst_pgaipc (gr=on) registers a QEMU graphic console (qemu_graphic_console_create) and a
  30 Hz main-loop timer copies each new renderer frame (gr_render_frame(): RGBA + seq,
  bumped by the DLL's pixel post and the getPixels fallback) into the console surface
  (x8r8g8b8). Build now has VNC (configure without --disable-vnc).
- QEMU picks the first console for VNC: run with `-vga none -parallel none` (the default
  parallel port is a vc text console that would come first; `-vnc ...,display=<id>` fails
  in QEMU 11 on that text console's missing 'device' property). boot.py --vnc N does this.
- dev/tools/vnc_shot.py (raw RFB client) grabs a frame: phase67_vnc_home.png = 1600x900,
  correct colours.
- openstacks front-end (outside git, original in the user's 7z): adb.shell() prefixes the
  adbd allow-list word; qemu_engine GR_ENV = Phase-65 knobs, default initrd =
  initramfs_full_p49bak.bin, `-vga none -parallel none` with gr=on; instances.conf points
  at F:\Projects\BluestacksReversing (dev build, pristine initrd; instance Pie64 uses its own
  qemu_test\Data_ost1.qcow2, a copy of the known-good Data); selftest 16/16 with PySide6
  6.12 in openstacks\.venv.

## 10e. Phase 68: touch input through "BlueStacks Virtual Touch"

Decode (Sonnet subagent; scratch dev/re_scratch/{vmsg,inp}.dis):
- bstvmsg host->guest push: guest hard ISR (.text+0x460) reads cid = shm+0x1c (<0 = none),
  writes BAR0+0x28 (clear); the threaded handler calls cb(cid, shm+host_off) and then writes
  BAR0+0x38 = cid (ack). One message in flight (a second IRQ before the ack corrupts the list).
- bstinput inpOnReceive: u32 type at +0: 0 keyboard (u8 set-1 scancode at +4 into serio
  "bstkbd"; 0xFF = sysrq escape), 1 IME (s32 len at +4, bytes at +8 -> /dev/bst_ime),
  2 touch, 3 gamepad, 4 control ({4,0} = /system/bin/bstshutdown).
- Touch = 132 B: type 2 + 16 x {u32 x, u32 y} (MT protocol A, ABS_MT_POSITION_X/Y 0..32767,
  0x7fffffff = no contact); each message is a full snapshot; all-none = lift.
Implementation (bst_vmsg.c): push queue (serialised by the 0x38 ack, touch moves coalesced),
BAR0+0x28 deasserts INTx and resets shm+0x1c to 0xFFFFFFFF; a QEMU abs+btn input handler
maps the left button + absolute position (QEMU abs range 0..0x7fff = the device's range) to
touch frames. It is re-activated at machine-init-done so it is first in QEMU's handler list
(unbound: binding to a console hides it from the absolute-mode check).
Fix: shm+0x1c was initialised to 0 = "cid 0 pending" on every shared-IRQ ISR run, causing
the spurious "atkbd serio2: Unknown key pressed (code 0x0 on bstkbd)" lines (now 0).
Note: QEMU's VNC server switches a client to absolute mode only on SetEncodings (real viewers
send it; dev/tools/vnc_tap.py now does).
Result p68D: a VNC click on App Center opens com.bluestacks.gamecenter/AppCenterActivity
(dev/shots/phase68_after_tap.png).

- Keyboard: VNC keys -> QEMU PS/2 keyboard -> guest "AT Translated Set 2 keyboard" -> Android
  (InputDispatcher RecentQueue shows the KeyEvents; dev/tools/vnc_type.py). getevent prints
  nothing from adb shell here (no access), so use `dumpsys input` to verify input.
- Networking works: App Center's Store loads its online catalogue (phase68_typed.png).

## 10f. Front-end launch verified (openstacks.qemu_engine, no GUI)

Starting instance Pie64 through QemuEngine.start boots to the home screen; a VNC frame on
5901 = dev/shots/phase68_frontend_boot.png. Front-end fixes (outside git): qemu_bios_dir in
instances.conf pointed at the old machine; `-vnc 127.0.0.1::PORT` was never valid (the old
build had no VNC), it is now `127.0.0.1:(PORT-5900)`; Root.vhd is attached with snapshot=on
so the pristine image is never modified (user data lives on Data_ost1.qcow2).

## 10g. Phase 69: audio playback (bstaudio)

Decode (Sonnet subagent; dev/re_scratch/{dis,ro,reladata}.txt). It corrects the old stub:
events at BAR1+0x000 (7 = playback period, 13 = capture), command mailbox at BAR1+0x400
{op, -, len, payload @+0x0c} + doorbell BAR0+0x00 (nothing read back), IRQ ack BAR0+0x08,
counters (free-running PERIOD counts) capture +0x800 / playback +0xc00, rings capture +0x1000 /
playback +0x21000 (<= 0x20000 each), BAR1 = 0x80000. Ops: 1/8 init, 3/0xa config {rate,bits,
channels,buffer_bytes,period_bytes,bytes_per_sec,frame_bytes}, 4/0xb start, 5/0xc stop,
6 data-available {count, offset}. One period per event (the guest advances one period per
pointer call); serialise on the ack; reset the counter on config.
Implementation (bst_audio.c rewrite; stub kept as re_scratch/bst_audio.c.stub_pre_p69):
the QEMU audio voice (audio_be_open_out) pull callback consumes the playback ring period by
period and posts type-7 events; without an audiodev (and for capture = silence) a real-time
period timer paces. After config the event type stays parked, so spurious shared-IRQ ISR
runs see delta 0 (the pre-config "bstaudio_isr Unknown cmd = 0" noise remains: the IRQ line
is shared with virtio-net).
boot.py --audio dsound (default, host speakers) | wav (runs/<run>/audio.wav) | none.
Verified p69A (wav): a UI click recorded twice (peak 1474 at 0.0 s and 2.7 s); AudioFlinger
standby/resume works (op 5 / op 4). adbd blocks `adb push` (sync protocol fault), and the
shell user cannot open /dev/snd, so test via Android sounds (tap a clickable view).
Front-end: qemu_engine adds -audiodev dsound,id=snd0 + bstaudio audiodev=snd0.

- Front-end VNC client (openstacks/vnc.py, outside git) had both input messages wrong:
  send_pointer packed type 4 with a 5-field format (struct.error on every mouse event) and
  send_keysym packed type 5 with a u16 key. Fixed to RFB PointerEvent (5,mask,x,y) and
  KeyEvent (4,down,pad,u32 keysym). Verified: the RfbClient receives 1600x900 frames and
  its click opens App Center (dev/shots/phase69_fe_click.png).

## 10h. Phase 70: full ADB (gate property served by bstvmsg)

- Gate: BlueStacks adbd `bst_allow_adb_cmd(char*)` @0x4162f0, called from handle_packet on
  every A_OPEN with the text after the first ':' (`shell:ls` -> `ls`, `sync:` literal); also
  re-checked per sync request path in file_sync_service (that was the push "protocol fault").
  Core `_bst_allow_adb_cmd` @0x415df0: allow all if `atoi(bst.enable_adb_access) > 0` or
  /data/downloads/.tmp/.adbconnect exists; otherwise prefix-match against comma tokens on the
  first line of /data/downloads/.adbcmd (`*` = all; fgets keeps the newline, so the last
  token never matches); a missing file denies everything.
- Source of the property: host props -> initramfs `bstconf` (bstvmsg GetProps, method 1) ->
  /.bstconf.prop -> /data/.bstconf.prop -> init. Real host value comes from bluestacks.conf
  `bst.enable_adb_access="1"`. bst_vmsg.c now serves it by default and gained
  `props=k=v;k=v` and `propsfile=<path>` device properties (appended; one k=v per line).
  The per-request log line is capped (method 0 polls made the qemu log 46 MB).
- Result p70_adb: unrestricted shell (id/echo/uptime), 5 MB push+pull byte-identical,
  40/40 shells and 10/10 disconnect/connect cycles clean. The old "offline" flapping was the
  gate closing adb's own service opens plus HD-Adb (v36) vs platform-tools (v41) killing each
  other's server; the front-end now prefers the PATH / platform-tools adb, HD-Adb last.
- Install: `adb install` works (ExternalStorageProvider reinstall: Success 0.5 s). BlueStacks
  PackageManagerService.installPackageLI -> isGoogleAppUpdateUnsafe refuses
  com.android.chrome / com.android.vending / com.google.android.gms ("has not been validated
  for this platform", INSTALL_FAILED_INVALID_APK) until a `com.google` account exists; with a
  test account added (as uid 10004 via addAccountExplicitly) Chrome install-multiple succeeded.
  System APKs from /system are odexed ("code is missing") so they are no install test.
- Also found: bst.version / bst.oem / bst.country are empty in our guest, so every rule in
  /data/downloads/.config.db (BstFilterAppsService: GL/ABI/hotfix tweaks) is rejected by a
  NumberFormatException. Real HD-Player supplies these; worth serving later.

## 10i. Phase 71: HD-Player host props (bst.oem/version/abi_list/...)

- Reference: the real /data/.bstconf.prop read (read-only, 7-Zip) from
  C:\ProgramData\BlueStacks_nxt\Engine\Pie64\Data.vhdx: 119 keys. HD-Player sends a fixed
  whitelist: global bst.* keys + bst.instance.<name>.* (prefix dropped) + bst.oem (oem cfg,
  "nxt") + bst.version (HKLM\SOFTWARE\BlueStacks_nxt Version, 5.21.550.1031) +
  bst.instance; bstsetconf.sh then appends serialno/imei/macs. Secrets in bluestacks.conf
  (refresh_token, nowgg_*) are not in the whitelist.
- dev/tools/bstprops.py rebuilds that set from bluestacks.conf at launch (reproduces the
  real file except two moved window coordinates); boot.py writes runs/<run>/bst.props and
  passes bstvmsg propsfile= (default --props host; --prop k=v overrides; --props min =
  built-ins). Overrides per boot: android_id, fb_width/height, dpi, cpus, ram,
  status.adb_port. The front-end has a copy (openstacks/bstprops.py) and writes
  logs/<inst>.bst.props from the instance settings.
- Built-in fallback list gained oem/version/country/locale/abi_list/instance.
- Result p71_props + front-end Pie64: props visible via getprop, 1600x900@240, home renders
  (Play Store icon now on the home screen). BstFilterAppsService loads .config.db:
  per-package Gl_renderer answers (bstfilterapps txn 20, e.g. com.infoldgames.infinitynikki
  -> Mali-G72, wildcard -> Adreno (TM) 650). The boot "loadConfigDb ... Permission denied"
  is a race inside BlueStacks: com.bluestacks.home writes the file via su (root umask),
  the FileObserver fires on CLOSE_WRITE before the chown/chmod, and the queued ATTRIB events
  reload it successfully. Rules gated on prodversion_gte 5.22.x stay off (installed 5.21).
- boot.py's monitor moved to 4456 (the front-end's Pie64 uses 4446; a just-killed dev boot
  made the front-end launch fail with "address in use").

## 10j. Phase 72: bstvmsg buffers 4K -> 256K (Rvc64_2 boot loop)

- Rvc64_2 (the user's original data disk, ~20 third-party apps) looped: system_server
  SIGTRAP in libhostcall_jni.so VmsgGuestClient::getHostBoundBuffer -> xdbgBreak, called from
  hcallSyncInstalledAppsRpc. getHostBoundBuffer(n) asserts n <= client+4 (the slot's in
  size, shm header +0x10). The installed-apps sync (method 6) is 6784 bytes; our in window
  was 4K. Now in = out = 0x40000, BAR1 8 MiB (8 slots x 0x81000). Boot stable, no crashes.
- Unity games (all arm64, ARM translation): the app's GL client attaches as a BIG slot
  (cid=127, BAR2) with handshake "... 0 0 1" (HPP requested); gr_render answers
  hpp=REQUESTED(unsupported), the client disconnects and the app aborts with
  "FORTIFY: pthread_mutex_lock called on a destroyed mutex". Needs the HPP / big-slot path.

## 10k. Phase 73: Unity / third-party apps run (HPP big slots + guest CPU features)

Two independent blockers, both fixed:
1. HPP transport (spec: dev/re_scratch/hpp_spec.md, from guest libOpenglSystemCommon,
   bstpgaipc.ko and HD-Player disassembly). Every third-party app (uid >= 10000, not
   com.bluestacks/com.google/com.android/...) asks the .ko for a BIG slot (cid 0x7f..0x86,
   BAR2 + (cid-0x7f)*4 MiB) and, after the 0x100 handshake ("... 0 0 1"), switches itself to
   a shared-memory ring: W[0] lock 0x55/0xaa, W[1] guest state, W[2] host state, W[3] reply
   ready, head +4, tail +8, slotLen[1022] at +0xc, 4 KiB slots from +0x2000; replies are
   written contiguously from +0x2000 (chunked, W[2]=3 = more follows). The host must
   memset the header and set W[0]=0x55 BEFORE acking the handshake WRITE, otherwise the
   guest's first lock reads 0 -> "Unexpected condition" -> exit(1) ("pthread_mutex_lock
   called on a destroyed mutex"). Kick = ioctl 0x7006 -> doorbell cmd 6: wake the render
   thread only, no IRQ (a spurious IRQ breaks the state machine). Host IRQ only when W[1] is
   2 (ring space / sync waiter, raised when the ring is found empty) or 3 (reply waiter).
   gr_render.c: hpp_init/hpp_take/hpp_flush/gr_render_kick under the unchanged pure vtable
   (the DLL is unaware); bst_pgaipc.c handles cmd 6 inline. Op census logged at close.
2. Guest CPU: QEMU's default qemu64 has no SSSE3/SSE4/POPCNT; libhoudini (ARM translation)
   then aborts translated apps via its deliberate crash at 0xdead0000 (libhoudini 0x2f1ab7).
   `-cpu host` and EPYC-Milan (AVX/XSAVE) make the initramfs `sh` jump to 0 and panic under
   WHPX, so the default is now `qemu64,+ssse3,+sse4.1,+sse4.2,+popcnt` (boot.py --cpu,
   front-end QEMU_CPU / env OST_CPU). AVX under WHPX is an open item (games may want it).
- Result (Rvc64_2, user's data disk): com.mindolin.nicedice (2D Unity) and
  com.Kayac.ShurikenCut (3D Unity) run, several HPP channels each, no crashes
  (dev/shots/phase73_unity_*.png). Portrait games are shown unrotated (display rotation
  is not applied to the console yet).
- Also seen: every bstpgaipc IRQ makes the guest bstaudio ISR print "Unknown cmd = 0" on
  the shared line (serial printk per interrupt): costly, fix with the perf work.

## 10l. Phase 74: production layout (self-contained repo, assets from the BlueStacks install)

- Layout: qemu/hw/misc (devices), qemu/apply_to_tree.py + qemu/build.cmd (fetches QEMU 11.1.0
  into deps/, also disables the meson symlink-install postconf that needs Developer Mode,
  copies the ucrt64 runtime DLLs next to the exe), frontend/ (moved from ../openstacks, now
  ../openstacks_pre_repo), tools/, docs/re (specs; raw dumps in docs/re/raw), docs/shots,
  boot.py. Gitignored: build/ deps/ assets/ instances/ work/ (RE binaries + guestlibs).
- tools/prepare_assets.py: kernel.bin = fastboot.vdi LBA1.. (bzImage, (1+setup_sects)*512 +
  syssize*16 = 7068448 B), initramfs.bin = the gzip right after it, size MBR+0x1fa
  (1350013 B, complete stream). Both byte-identical to the files we used. Root.vhd copied from
  Engine/Pie64 (our ../qemu_test copy differed: early non-snapshot boots wrote to it),
  Data_template.vhdx = Engine/Pie64/Data_orig.vhdx (BlueStacks' blank /data), manifest.json.
- p74_pristine_initrd + p74_fresh_assets: the PRISTINE BlueStacks initramfs boots to the
  home screen (initramfs_full_p49bak.bin, with its appended override cpio, is obsolete), and a
  first boot from the blank template reaches the home screen in ~20 s.
- Front-end: data_disk "" = instances/<name>/Data.vhdx created from the template; disk format
  by extension (vhdx/qcow2/vhd); adb discovery has one source (config.DEFAULT_ADB_CANDIDATES,
  HD-Adb last). The user's two instances keep their qemu_test data disks (absolute paths).
- Full from-scratch QEMU build verified (1699 steps) and the exe runs without MSYS2 on PATH.

## 10m. Phase 75: performance - native GPU display, no worker pad, quiet console

Measured with tools/fps.py (SurfaceFlinger flip counter, root via bstk/su), Rvc64_2 data:
| config | home screen | ShurikenCut (3D Unity) |
|---|---|---|
| Phase-50 2 ms worker pad (GR_WORKER_PAD=2) | 1.1 fps | 55.8 fps |
| Phase 75 (no pad, condvar waits, timeBeginPeriod 1) | 60.0 fps | 59.9 fps (vsync cap) |
1. Native display: the DLL presents into a child window ("BlueStacksApp") it creates inside
   the HWND passed to PgaServerInit - exactly HD-Player's path, GPU only. We used to keep that
   window hidden and pull 30 fps of RGBA back (PgaStartReadPixels) into the QEMU console ->
   VNC -> Python. Now gr_render makes it a borderless popup (class gr_renderHostWnd, title
   "OpenStacks display"; GR_SHOW=1 = normal top-level window), resizes the DLL child to the
   client area (the DLL scales), subclasses the child for input: mouse -> abs pointer + left
   button (bstvmsg touch handler), keys -> PS/2 scancode (qnum, 0x80|sc for E0 keys), both via
   aio_bh_schedule_oneshot on the main loop. GR_READBACK=0 skips the pixel readback (the VNC
   console then stays blank); default 1 so boot.py/VNC tools keep working.
   Front-end: openstacks/native.py finds the window by QEMU pid + class, embeds it with
   QWindow.fromWinId + createWindowContainer (letterboxed), and SetParent(NULL)s it back
   before the container dies (child windows die with their parent). Instance key
   display=native|vnc (default native -> GR_READBACK=0).
2. The Phase-50 2 ms Sleep after every gr_worker op (all channels serialised on one worker;
   2 ms = up to 15.6 ms at the default timer resolution) is gone (GR_WORKER_PAD=<ms> restores
   it); the Phase-65 completion FIFO made it unnecessary. pure_take/pure_flush wait on the sock
   condvar (pure_write/pure_read wake it) instead of Sleep(1) polls; timeBeginPeriod(1).
3. Log volume: per-op gr_render/bstpgaipc lines only with GR_VERBOSE=1 (a boot's log went from
   tens of MB to ~60 KB).
4. bstaudio shares IRQ 10 with bstpgaipc (IRQ 11: virtio-net, bstvmsg, vboxguest); its ISR
   printk(KERN_ERR "Unknown cmd = 0") for any event other than 7/13 (bstaudio.ko 0xab0), i.e.
   on every graphics IRQ (2046 lines per boot) through the trapping serial console. The kernel
   cmdline now has loglevel=3 (boot.py --console-verbose to keep everything on serial.log).
- Verified: front-end player embeds the native window (home renders, click opens the Store),
  closing the player leaves QEMU running; PostMessage'd keys arrive as KeyEvents.

## 10n. Phase 76: Unity game 13 -> 60 fps (uncached BAR2), gcal spin, clocks, IRQ split

Game: com.dino.runnergames.adventure.evolution (user's Pie64 data). Measured with tools/fps.py.
| step | fps | notes |
|---|---|---|
| Phase 75 baseline | 12.6 | BstCommandProcessor 169% + logd 54% CPU in the guest |
| gcal parked | 13.2 | spin gone; UnityMain still 100% |
| hRx compaction fix | 13.1 | |
| TSC clocksource | 13.2 | clocks were not the bottleneck |
| BAR2 64-bit prefetchable | **59.9** | UnityMain 100% -> 31% |
1. Root cause (simpleperf on UnityMain): 77% memcpy + 15% _hppFlushDataInBuf = the guest
   copying ~120 MB/s of GL data (glBufferSubData) into the HPP big-slot window. BAR2 lived at
   0xfa000000, inside the 32-bit PCI hole that SeaBIOS' MTRR (reg00 3G-4G) marks uncachable,
   so the guest's mmap was UC- -> every store uncached. bst_pgaipc BAR2 is now 64-bit
   prefetchable: the firmware puts it above 4 GiB (0xc020000000), default MTRR type WB.
   BAR1 (small slots: surfaceflinger/systemui) is still 32-bit in the hole (UC) because the
   .ko reads resource index 2 for BAR2; low traffic, fine for now.
2. gcal: BstCommandProcessor's host->guest call channel ('gcal', sid 0x6c616367) sends
   method 0 = "wait for the next host call"; bstvmsg answered at once -> guest spin
   ("gcallDecode: invalid opcode: 0" flood). bstvmsg.ko vmsgIoctlProcessRequest sleeps while
   the poll word is 1 (woken by vmsgIsr for that cid), so the device now parks it (poll=1).
3. hpp_take compacted (memmove) the receive buffer on every message (O(n^2)); now it resets
   when fully consumed and compacts only before growing (HD-Player semantics).
4. Clock: the BlueStacks kernel had no usable TSC clocksource (hpet only: every
   clock_gettime = MMIO exit). Real BlueStacks sets VBoxInternal/GIM/Provider=KVM (kvm-clock).
   We use QEMU's existing WHPX VMware mode instead: -accel whpx,hyperv=off + -cpu
   ...,+invtsc,vmware-cpuid-freq=on -> "Hypervisor detected: VMware", "TSC freq read from
   hypervisor", clocksource tsc. Defaults in boot.py and the front-end.
5. IRQs: bstaudio pinned to slot 0x0c (IRQ 11), bstpgaipc to 0x05 (IRQ 10): graphics IRQs no
   longer run the bstaudio ISR (which printk()s on every foreign IRQ).
- boot.py gained --smp (default 4), --mem, --accel, --append.

## 10o. Phase 77 (part): custom resolutions, nav buttons, host-call payload log

- The renderer framebuffer was hard-coded 1600x900 (gr_render GR_W/GR_H), so a 720x1280
  instance rendered a landscape Android squashed into a portrait frame. GR_W/GR_H now come
  from env GR_FB_W/GR_FB_H (front-end: instance resolution; boot.py --res). Verified:
  PgaServerInit(720x1280), `wm size` 720x1280 @160 dpi, portrait home layout.
- Player side bar: Back / Home / Recents = PS/2 keys via the QEMU monitor `sendkey`
  (ac_back -> KEY_BACK 158, ac_home -> KEY_HOMEPAGE 172, audionext -> KEY_NEXTSONG 163 =
  APP_SWITCH in Generic.kl). Verified Settings -> Back -> launcher, Recents, Home.
- bstvmsg logs a printable payload for rejected hcal methods: 10 = activity displayed
  (package, activity, JSON), 22 = per-app config, 62 / 7 = package-scoped (unknown).

- Rotation (spec docs/re/rotation_spec.md): the guest reports Surface.ROTATION_0..3 with hcal
  method 7 ([7][0xc][rot]; the generic bufLen slot is the rotation; reply u32 0 at out+0).
  bstvmsg -> gr_render_set_orientation(o): grApiInit table +0x40 = void(float, float, int)
  (DLL maps 0..3 to zRot 0/90/180/270, queued to its render thread); touch is inverted on the
  host like HD-Player EXE 0x1402ec260 (r1: x'=1-y, y'=x; r2: 1-x, 1-y; r3: y, 1-x); the host
  window title becomes "OpenStacks display r<o>", which the front-end NativeDisplay polls to
  letterbox at the rotated aspect (GR_SHOW windows resize themselves). Verified: the Dinosaur
  game launches -> hcal 7 rotation=1 -> upright portrait in the player. Rotate button toggles
  user_rotation 0 <-> 1 (accelerometer off).
- Player: Pop Out (side bar / Ctrl+Shift+P) moves just the Android screen into its own bare
  window sized to the screen's aspect (it follows rotation); the Screen tab shows a "bring it
  back" placeholder, closing the window docks the screen again. The native display releases the
  renderer window before each move and re-embeds after. (A first version moved the whole tab
  into a second player window; that is not what was wanted and was reverted.)
- Front-end refuses to start an instance whose ports are busy (another QEMU), and reports
  QEMU's own error if it exits within 1.5 s.

- REGRESSION fixed: hcal 24 = hcallIsAppLaunchAllowedRpc (launcher asks before starting an
  app; [24][msglen][strlen+1][pkg][u8]); the guest reads the FIRST BYTE of the out window,
  non-zero = allowed. We never answered it; byte 0 held "b" (from the GetProps text), so it
  "worked" by accident until the hcal 7 handler wrote 0 at out+0 -> every launcher launch was
  refused after the first rotation. Now answered explicitly (out+0 = 1). Lesson: hcal replies
  share one out window; unhandled calls read stale bytes. (Method ids = the order of the
  hcall*Rpc functions in libhostcall_jni: 1 GetProps ... 22 SetAppConfigDbParams,
  23 GoogleAccountListUpdated, 24 IsAppLaunchAllowed, ...)

## 10p. Phase 78: AVX in the guest (QEMU WHPX CPUID 0xD bug)

- Trigger: com.game.merge.anything SIGILL (ILL_ILLOPN) in houdini's translation cache at
  `c4 e1 f9 6e` = VEX vmovq: libhoudini emits AVX for some code, our CPU model had none.
- Earlier AVX attempts (-cpu host, EPYC-Milan) crashed the initramfs `sh` (jump to 0). Cause:
  the kernel warned in init_xstate_size - CPUID(0xD,0).EBX said 576 B while XCR0 = x87|SSE|AVX
  needs 832, so XSAVE buffers were too small and AVX state saves corrupted memory. Upstream
  WHPX (target/i386/whpx/whpx-all.c) takes the hypervisor's dynamic EBX for sub-leaves 1 and
  2 and leaves sub-leaf 0 at QEMU's model (stale XCR0). qemu/apply_to_tree.py now patches it
  to sub-leaves 0 and 1 (2 is the fixed AVX offset).
- Default -cpu (boot.py, front-end): qemu64 + ssse3/sse4.1/sse4.2/popcnt/invtsc/
  vmware-cpuid-freq + xsave/avx/avx2/fma/f16c/bmi1/bmi2/movbe. Verified on the user's Rvc64_2
  data: boots ("context size is 832 bytes"), Merge Anything runs (60 fps, no crash), Dinosaur
  59.8 fps, ShurikenCut 59.9 fps.
- The "Dinosaur crashes" report on Rvc64_2 was the low memory killer (2 GB instance, the game
  needs ~1.6 GB): Rvc64_2 is now 4096 MB in the user's instances.conf.

## 10q. Phase 79: images from BlueStacks' CDN, instances per image, Android 11 boots

- Spec: docs/re/image_download_spec.md. No metadata API: one self-extracting 7z per image at
  `https://cdn3.bluestacks.com/downloads/windows/nxt/<client version>/<per-build key>/Android/
  <Image>/<Image>.exe` (5.21.550.1031 key 5a99b4c3b70826e26cecb85b296b66c5; 301 -> ak-build mirror,
  Range ok, md5 in x-amz-meta-md5). Contents: Root.vhd, fastboot.vdi, Data.vhdx (blank template),
  Android.bstk.in, oem_<Image>.cfg. 5.21.550 offers Nougat32/64, Pie64, Rvc64.
- frontend/openstacks/images.py: download (resumable .part, md5) -> extract (7-Zip reads the
  archive behind the ELF SFX stub; note the 7z magic ALSO occurs inside the stub at 390,248, so
  never slice at the first match) -> apply_patches (patches/all + patches/<Image>/*.py, recorded
  in the manifest) -> build_assets (assets/<Image>/). tools/prepare_assets.py --list/--download.
- Downloaded Pie64: kernel/initramfs/Data template byte-identical to the install-derived ones;
  Root.vhd is BlueStacks' original 2024-09 image (the installed one was updated in place). Boots
  to home in ~30 s (p79_dl_pie64).
- Instances have an `image` key (assets/<image>/, bstandroid=<image lower>, bst.instance=<image>);
  the manager's New dialog picks the Android version and downloads it with a progress dialog.
- Rvc64 (Android 11) created through that flow boots to the BlueStacks home screen on the first
  try (ro.build.version.release=11, docs/shots/phase79_android11_rvc64.png).

- Client build 5.22.290.1031 (user-supplied HD-MultiInstanceManager.exe): key
  b3b8c6d5d7f2ef642075595e7aa5754c = the only 32-hex string in the exe (5.21's key is too);
  HEAD-verified Nougat32/64, Pie64, Rvc64, Tiramisu64 (Android 13, 815,153,759 B). Images come
  from the default build (5.21.550, matches the installed renderer DLL) unless only a newer
  build has them (Tiramisu64) or --build is given; cache names carry the build.
- Tiramisu64 (Android 13) downloaded + instance created: boots to the BlueStacks home screen
  (ro.build.version.release=13, docs/shots/phase79_android13_tiramisu64.png).

- Android 13 touch (Phase 80): Tiramisu64 ships a 5.15 kernel whose bstinput.ko dispatches
  inpOnReceive types 0 kbd, 1 IME, 3 gamepad, 4 control, **5 multi-touch** (inpMtTouchDispatch);
  type 2 (our 4.19-era 16-slot frame) is "unknown", so clicks did nothing. Type 5 =
  [u32 5][u32 count] + count x {u32 slot, u32 state, u32 x, u32 y}: ABS_MT_SLOT=slot; state 2 ->
  TRACKING_ID -1 (up), else TRACKING_ID=slot + POSITION_X/Y (0..32767, 16 slots); then
  BTN_TOUCH(active>0) + SYN. bstvmsg property touch-mt=on selects it; the launchers set it when
  the image's bzImage kernel_version (header 0x20E) is >= 5 (images.kernel_version). Verified:
  click on Store -> com.bluestacks.gamecenter AppCenterActivity. Pie64/Rvc64 are 4.19 (type 2).
- boot.py --images now resolves relative paths (QEMU runs in the run dir).

## 10r. Phase 81: image patching - debloat + Lawnchair

- `patches/_lib.py`: pure-Python gzip'd cpio (newc) ramdisk editor (`Ramdisk.load/read/write/
  save`, round trip verified) + `fetch(url, dest, sha256)` (pinned-hash download cache in
  deps/lawnchair).
- `patches/all/10_debloat_lawnchair.py`: adds /boot/ost/ost_boot.sh + /boot/ost/launcher.apk to
  the boot ramdisk and sources the script from stage2.sh just before `log_echo "Starting
  Android"`. Every boot it deletes the stripped packages from /data/downloads,
  /data/priv-downloads and any /data/app updates, then places Lawnchair in
  /data/downloads/<pkg>/ (BlueStacks' PMS installs from there). Root.vhd untouched; existing
  /data gets cleaned on the next boot too.
- Stripped: uncube launcher3, bsxlauncher, gamecenter, home, nowgg, piggy, quest, filemanager,
  gg.now accounts/ads/billing, now.gg + bluestacks billing, admob config. Kept: GMS/Play,
  Chrome, location provider, config.db, system-image BlueStacks services.
- Lawnchair per image: Pie64 = 12.1.0 alpha 4 (14 and 15 crash on Android 9 with
  Resources$NotFoundException); Rvc64/Tiramisu64 = 15.0.0 beta 3; Nougat = 1.2.0.1884
  (ch.deletescape.lawnchair). Override: env OST_LAWNCHAIR=modern|14|12|legacy.
- images.py: `cut_boot_files` runs before the patches (they see kernel.bin/initramfs.bin);
  `initramfs.orig.bin` keeps the pristine ramdisk; `repatch(image)` /
  `prepare_assets.py --repatch IMAGE|all` re-applies patches without a download (kill QEMU
  first: a running VM holds initramfs.bin open).
- Verified: Pie64, Rvc64, Tiramisu64 boot straight to Lawnchair, no crashes, stripped dirs gone.
- Nougat64 (Android 7.1.2, kernel 4.9.95) now boots too:
  - The kernel cmdline is compiled in and overrides ours (`-append` is ignored; only
    earlyprintk reaches serial).
  - No ata_piix. BlueStacks puts Root/Data on AHCI, but QEMU's ich9-ahci under WHPX hands the
    guest garbage on DMA reads (sector 0 came back without 55aa). With the VMware CPUID leaf
    (hyperv=off) it also soft-locks in the AHCI error handler (LAPIC timer storm).
  - Fix (`patches/all/05_nougat_ide.py`): the ramdisk binds the built-in ata_generic to the
    PIIX3 IDE controller via `new_id "8086 7010 ffffffff ffffffff 0 0 2"` (driver_data 2 =
    ATA_GEN_FORCE_DMA). Without it the controller runs in PIO mode, which is so slow under
    WHPX that the first-boot copy of the prebundled data stalls for minutes. With it the disks
    run at UDMA/100. Disks stay on IDE (`images.disk_args`, shared by boot.py and the
    front-end).
  - Lawnchair 1.2 is portrait-only at 1600x900/240dpi (below the 720dp tablet cutoff;
    pref_enableScreenRotation doesn't change it). The guest reports rotation 1 via hcal 7, so
    the player shows it as a portrait screen. ADB is "offline" until boot completes, then fine.
- Nougat32 (x86, kernel 4.9.31, non-PAE): same IDE patch, plus bstpgaipc `bar2-64=off`.
  A non-PAE kernel can't assign the 64-bit BAR2 above 4G ("BAR 2 not assigned", then a flood
  of "Unexpected cid -1" and no display). The launchers add the option when the bzImage
  xloadflags lack XLF_KERNEL_64 (`images.pgaipc_opts`). BAR2 is then UC in the PCI hole, so
  the HPP big-slot path is slower there. Boots to Lawnchair 1.2.

## 11. Next

- DONE: good path is the boot.py default; pristine Root.vhd boots (p66A).
- DONE: full ADB incl. push/install (p70); HD-Player host props (p71).
- DONE: live display (VNC console), touch (bstinput), keyboard (PS/2), front-end launch.
- DONE: audio playback. Open: capture source (mic), the shared-IRQ noise before config,
  big-slot (BAR2) clients, Linux portability (gfxstream), front-end GUI smoke test.

## Pitfalls (new)

- `dev\build.cmd` taskkills QEMU, so a running boot dies on rebuild. Grade it first.
- guestwrite lines are throttled (first 24 + new shapes): counts are lower bounds.
  `recDropped` and `exeFrame` lines are not throttled.
- Boot runs reach surfaceflinger in ~20 s under WHPX; 2–3 minutes is enough to grade
  the EGL init loop.

## Language convention

Refer to the guest process as "the guest", "surfaceflinger" or "the EGL client" and use "it".
The old canon personifies it ("she/her"); don't carry that over.
