# Backlog

Things that are known, not done yet, and probably worth doing. Newest context is in
NOTES.md (section numbers in brackets). Move an item to NOTES when it lands.

## Display / UX
- **Screen rotation for portrait apps** - in progress (Phase 77).
- **Mouse wheel and multi-touch on the native display** - the embedded renderer window only
  forwards left-button drags as one finger and keys [10m]. BlueStacks maps the wheel to
  scroll/zoom and has a multi-touch mode.
- **Status bar says `input:vnc` in native mode**; the Input: VNC/ADB toggle only applies to
  the VNC display (hide or relabel it when display=native).
- **Keymap editor** (game controls overlay, BlueStacks' signature feature) - stub only.
- **Player window sizing** - follow the guest resolution / orientation like BlueStacks (fit
  window to 16:9 or 9:16, remember size per instance).
- **Thumbnails in the manager** use `adb screencap`; could use the renderer instead.

## Performance
- **BAR1 (small-slot windows) is still uncachable** - it sits in the 32-bit PCI hole (MTRR UC);
  surfaceflinger/systemui traffic goes through it. Moving it above 4 GiB needs the guest
  bstpgaipc.ko to accept a 64-bit BAR at index 1 (it reads BAR2 at resource index 2) [10n].
- **kvm-clock instead of the VMware TSC leaf** - BlueStacks uses the KVM paravirt clock
  (GIM/Provider=KVM). Our VMware-leaf TSC works; kvm-clock would need MSR handling in the
  WHPX backend [10n].
- **No AVX in the guest** - `-cpu host`/AVX models crash the initramfs `sh` under WHPX
  (likely XSAVE state handling). Some games/houdini paths would benefit [10k].
- **bstaudio ISR spam** - its ISR logs `Unknown cmd = 0` for every interrupt on its line
  (now IRQ 11: network + bstvmsg). Harmless with loglevel=3, still fills dmesg [10m, 10n].

## Guest integration
- **Microphone capture** - bstaudio capture delivers silence [10g].
- **gcal host->guest calls** - the channel is parked; nothing sends BlueStacks host commands
  (open URL, install from host, etc.) [10n].
- **Unknown hcal methods** are rejected (3, 5, 6, 8, 10, 22, 44, 62...): app-install sync,
  activity/top-app notifications, ... Some may matter (e.g. orientation) [10j].
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
- **Linux host** - replace libOpenglRender.dll (Windows-only) with gfxstream/emugl, WHPX with
  KVM; the devices themselves are portable QEMU code.
- **Packaging** - an installer/zip with build/, the front-end and a first-run asset step.
