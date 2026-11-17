# Backlog

Things that are known, not done yet, and probably worth doing. Newest context is in
NOTES.md (section numbers in brackets). Move an item to NOTES when it lands.

## Display / UX
- **Home screen stays portrait after a portrait app** - Android keeps rotation 1 when the
  launcher returns; BlueStacks snaps back to landscape (likely the host forcing orientation over
  gcal / setGameOrientation, hcal 54 is its reply). Rotate button works around it [10o].
- **VNC console is not rotated** - the readback frame is the unrotated panel (display=vnc only).
- **Mouse wheel and multi-touch on the native display** - the embedded renderer window only
  forwards left-button drags as one finger and keys [10m]. BlueStacks maps the wheel to
  scroll/zoom and has a multi-touch mode.
- **Keymap editor** (game controls overlay, BlueStacks' signature feature) - stub only.
- **Player window sizing** - follow the guest resolution / orientation like BlueStacks (fit
  window to 16:9 or 9:16, remember size per instance).
- **Thumbnails in the manager** use `adb screencap`; could use the renderer instead.

## Performance
- **Full host CPU model** - we now expose SSE4 + AVX/AVX2/FMA/BMI/F16C/MOVBE on a qemu64
  base; `-cpu host` (AVX-512 etc.) is untested since the CPUID 0xD fix [10p].
- **BAR1 (small-slot windows) is still uncachable** - it sits in the 32-bit PCI hole (MTRR UC);
  surfaceflinger/systemui traffic goes through it. Moving it above 4 GiB needs the guest
  bstpgaipc.ko to accept a 64-bit BAR at index 1 (it reads BAR2 at resource index 2) [10n].
- **kvm-clock instead of the VMware TSC leaf** - BlueStacks uses the KVM paravirt clock
  (GIM/Provider=KVM). Our VMware-leaf TSC works; kvm-clock would need MSR handling in the
  WHPX backend [10n].
- **bstaudio ISR spam** - its ISR logs `Unknown cmd = 0` for every interrupt on its line
  (now IRQ 11: network + bstvmsg). Harmless with loglevel=3, still fills dmesg [10m, 10n].

## Guest integration
- **Microphone capture** - bstaudio capture delivers silence [10g].
- **gcal host->guest calls** - the channel is parked; nothing sends BlueStacks host commands
  (open URL, install from host, etc.) [10n].
- **Unknown hcal methods** are rejected (3, 5, 6, 8, 10, 22, 44, 62...). They all share one
  reply window, so a call that reads its answer from a different spot than our reject path
  writes sees stale bytes (that is how IsAppLaunchAllowed broke). Audit each hcall*Rpc reply
  format in libhostcall_jni and answer them explicitly [10o].
- **Google sign-in seeding** - Chrome / Play Store / GMS updates over adb need a com.google
  account (BlueStacks rule) [10h].
- **bst.version 5.22+ rules** - .config.db has per-app tweaks gated on prodversion_gte 5.22;
  we report the installed 5.21.550 [10i].
- **Shared folders** (vboxsf: Documents/Pictures/InputMapper) - vmmdev HGCM stubs only.

## Distribution / platform
- **Download images on demand** from BlueStacks' servers (HD-MultiInstanceManager
  `MimBackend::downloadImage`) so no BlueStacks install is needed; checksums + pinned
  versions.
- **Other Android versions** - Rvc64 (Android 11), Tiramisu64 (Android 13) images; asset prep
  is per image already (`prepare_assets.py --image`).
- **Cleaned-up images** - script that strips ads/bloat and swaps the launcher on a local copy.
- **Linux host (plan: option A, open-source gfxstream renderer)**. Everything except the renderer
  is porting work; the renderer swap is the project (estimate: ~2 weeks focused).
  1. Pluggable renderer backend in gr_render: the DLL only sees the HstSocketHost vtable
     (peek/recv/recvcopy/alloc/alloccopy/send/createFinish); put that behind an interface.
  2. gfxstream backend, built and A/B-tested on Windows first against libOpenglRender.dll
     (same guest, same tools). GL (renderControl + GLES) first, then Vulkan.
  3. Compatibility: diff the guest encoders' opcode tables (vendor/lib64 lib_renderControl_enc,
     libGLESv2_enc, the Vulkan encoder) against gfxstream's decoders; fill gaps for BlueStacks'
     additions (e.g. rcSetColorBufferVulkanMode2, ANDROID_EMU extension negotiation, rcComposeAsync*).
  4. Port gr_render's Win32 bits (HWND window, SRWLOCK/CONDITION_VARIABLE, LoadLibrary,
     timeBeginPeriod) to QEMU primitives + SDL/X11 - mostly disappears with gfxstream.
  5. KVM instead of WHPX: kvm-clock natively (drop the VMware TSC leaf), AVX without the
     CPUID 0xD patch; audio via PulseAudio/PipeWire; build.cmd -> shell script.
  6. Front-end: native-window embedding works on X11 (QWindow.fromWinId with an XID); Wayland
     cannot embed foreign windows -> share frames (dmabuf / shared Vulkan image) or render in
     the front-end process.
  7. Assets: the image downloader (no BlueStacks install needed) covers Linux.
  Rejected alternatives: the DLL under Wine (cross-process window/GPU plumbing, fragile), our
  own renderer (far too large).
- **Packaging** - an installer/zip with build/, the front-end and a first-run asset step.
