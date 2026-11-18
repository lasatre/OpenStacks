# Backlog

Known gaps and ideas, roughly by area. NOTES.md has the background (section numbers in
brackets). Move an item to NOTES when it lands.

## Release
- **Installer and signing** - the release is a portable ZIP. An installer (Inno Setup / MSI)
  with Start menu shortcuts, per-instance shortcuts (`OpenStacks.exe --launch <name>`) and
  a signed exe would avoid SmartScreen warnings.
- **Updates** - no update check; a newer release has to be extracted over the old one
  (`data\` is kept).

## Display / UX
- **Home screen stays portrait after a portrait app** - Android keeps rotation 1 when the
  launcher returns, while BlueStacks snaps back to landscape (probably by forcing the
  orientation over gcal / setGameOrientation; hcal 54 is its reply). The Rotate button works
  around it [10o].
- **Mouse wheel and multi-touch on the native display** - the embedded renderer window only
  forwards left-button drags (one finger) and keys [10m]. BlueStacks maps the wheel to
  scroll/zoom and has a multi-touch mode.
- **Keymap editor** (game controls overlay, BlueStacks' signature feature) - the button is a
  stub.
- **Player window sizing** - the window fits the guest's aspect when an instance opens,
  rotates or its tab is selected. Open: keep the aspect while resizing by hand, and remember
  the size per instance.
- **Manager thumbnails are black with the native display** - they come from a readback that
  the native GPU path skips (`GR_READBACK=0`). Use `adb screencap` or a renderer snapshot.
- **VNC console is not rotated** - the readback frame is the unrotated panel (display=vnc only).
- **Nougat launcher** - Lawnchair 1.2 is portrait-only at 1600x900/240dpi. A
  landscape-capable launcher for Android 7 would look better [10r].
- **Slow instance shutdown** - "Shut down" waits for QEMU's graceful stop (around 10 s)
  before it falls back to killing the process.

## Performance
- **Full host CPU model** - SSE4, AVX/AVX2, FMA, BMI, F16C and MOVBE are exposed on a qemu64
  base. `-cpu host` (AVX-512 etc.) is untested since the CPUID 0xD fix [10p].
- **BAR1 (small-slot windows) is still uncachable** - it sits in the 32-bit PCI hole (MTRR UC),
  and surfaceflinger/systemui traffic goes through it. Moving it above 4 GiB needs the guest's
  bstpgaipc.ko to accept a 64-bit BAR at index 1 [10n].
- **Nougat32 BAR2 is uncachable too** - its non-PAE kernel can't map a BAR above 4 GiB
  (`bar2-64=off`), so the HPP big-slot path is slower there [10r].
- **kvm-clock instead of the VMware TSC leaf** - BlueStacks uses the KVM paravirt clock. The
  VMware-leaf TSC works; kvm-clock would need MSR handling in the WHPX backend [10n].
- **bstaudio ISR spam** - its ISR logs `Unknown cmd = 0` for every interrupt on its shared
  line. Harmless with loglevel=3, but it fills dmesg [10m, 10n].

## Guest integration
- **Unanswered host calls** - methods 3, 5, 6, 8, 10, 22, 44, 62 and others are rejected
  (24, 7 and 68 are answered). They all share one reply window, so a call that reads its answer
  from a different offset than the reject path writes sees stale bytes; that's how
  IsAppLaunchAllowed and Android 13's su broke. Audit each hcall*Rpc reply format in
  libhostcall_jni and answer them explicitly [10o, 10t].
- **gcal host->guest calls** - the channel is parked, so nothing sends BlueStacks host
  commands (open URL, install from host, etc.) [10n].
- **Live shared folder** - files move over adb (Files window, drag & drop) [10s]. A mounted
  host folder needs the VBoxSharedFolders HGCM service in vmmdev; vboxsf.ko already loads
  and only stubs answer.
- **Microphone** - bstaudio capture delivers silence [10g].
- **Google sign-in seeding** - updating Chrome, Play Store or GMS over adb needs a Google
  account (a BlueStacks rule) [10h].
- **bst.version 5.22+ rules** - .config.db has per-app tweaks gated on prodversion_gte 5.22,
  and we report 5.21.550 [10i].
- **Magisk extras** - Zygisk is off and modules are untested. Magisk's db read fails early in
  boot, which the per-boot adb-shell grant works around [10t].

## Images
- **Android 7, 11 and 13 beyond boot** - all five images boot to Lawnchair with touch, adb
  and Magisk. Games, HPP throughput and audio have only been tested on Pie64 [10q, 10r].
- **Newer BlueStacks builds** - each client build has its own download key (the only 32-hex
  string in its HD-MultiInstanceManager.exe), so add the version and key to
  `images.CLIENT_BUILDS`. Known builds: 5.21.550, and 5.22.290 (adds Tiramisu64). Their
  renderer DLL may differ from the installed one, so newer image re-releases are opt-in
  (`--build`) [10q].
- **More cleanup** - Lawnchair is the default home screen only because it's the only one
  installed. Lawnchair 15 on Pie64 needs a build that fixes Android 9. System apps (e.g.
  com.bluestacks.settings) would need a Root.vhd-level patch path [10r].

## Linux host (open-source gfxstream renderer)
The renderer swap is done on Windows (docs/renderer_plan.md): gr_render has a gfxstream backend
(`renderer/`, ost_renderer.dll) that runs all three images without BlueStacks. Left for Linux:
1. Build gfxstream + the adapter as a .so (CMake, clang/gcc); libshadertranslator.so comes in
   Google's Linux emulator package (same pinned-download approach).
2. Vulkan guests: the Vulkan encoder is not mapped or tested yet (GL only so far).
3. Compatibility gaps not hit yet: BlueStacks' rcGLShaderWorkaround / rcGLHostInfo, the
   texture-cache GLES opcodes (2478-2480), GLES 3.2 extras (2496+).
4. Port gr_render's Win32 parts (HWND window, SRWLOCK/CONDITION_VARIABLE, LoadLibrary,
   timeBeginPeriod) to QEMU primitives and SDL/X11; most of this disappears with gfxstream.
5. KVM instead of WHPX: native kvm-clock (drop the VMware TSC leaf), AVX without the
   CPUID 0xD patch, audio via PulseAudio/PipeWire, build.cmd becomes a shell script.
6. Front-end: native-window embedding works on X11 (QWindow.fromWinId with an XID). Wayland
   can't embed foreign windows, so share frames (dmabuf / shared Vulkan image) or render in the
   front-end process.
7. Assets: the image downloader already works without a BlueStacks install, so it covers Linux.

Rejected alternatives: the DLL under Wine (cross-process window and GPU plumbing, fragile), and
writing our own renderer (far too large).
