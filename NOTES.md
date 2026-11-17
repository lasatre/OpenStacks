# OpenStacks dev notes (fresh canon, from Phase 60)

The old canon (`../grbridge/NOTES.md` §1–69, `../qemu_bst/REPORT.md`, `../FINDINGS.md`)
stays as the historical record. **Its numbering is off by 10 for the late phases**
(NOTES §69 = Phase 59). Treat its conclusions as hypotheses: §1 below refutes several.

Workspace: `dev/` (git). `dev/src/hw/misc` is the ONLY canonical device source.
Build: `dev\build.cmd`. Boot: `python dev\boot.py <run> [--root ..] [--env K=V]`.
Grade: `python dev\tools\grade.py <run> --write`. Monitor: `dev\tools\mon.py`.

## Current best configuration (Phase 66: pristine Root.vhd boots to the BlueStacks home screen)

    python dev\boot.py <name> --root Root.vhd

All other boot.py defaults are the working path: Data_fresh.qcow2,
initramfs_full_p49bak.bin (pristine .ko), PGA_SLOTSHIFT=1, GR_PURE=1, GR_HPP=0.
Rebuild (dev\build.cmd) for the RGBA->BGRA capture fix.

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

## 11. Next

- Make the good path the default: PGA_SLOTSHIFT=1, GR_PURE=1, GR_HPP=0, pristine initramfs.
- Try the pristine Root.vhd (no libEGL patches). The p50 markers should be unnecessary now.
- DONE: live display (VNC console), touch (bstinput), keyboard (PS/2), front-end launch.
- bstaudio (stub), big-slot (BAR2) clients, and Linux portability (gfxstream).

## Pitfalls (new)

- `dev\build.cmd` taskkills QEMU, so a running boot dies on rebuild. Grade it first.
- guestwrite lines are throttled (first 24 + new shapes): counts are lower bounds.
  `recDropped` and `exeFrame` lines are not throttled.
- Boot runs reach surfaceflinger in ~20 s under WHPX; 2–3 minutes is enough to grade
  the EGL init loop.

## Language convention

Refer to the guest process as "the guest", "surfaceflinger" or "the EGL client" and use "it".
The old canon personifies it ("she/her"); don't carry that over.
