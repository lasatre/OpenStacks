# Linux port plan

Goal: OpenStacks on x86-64 Linux with the same features as Windows: QEMU with the OpenStacks
devices on KVM, the open renderer (gfxstream), the PySide6 front-end with the embedded native
screen, image download/patching, and a release artifact built in CI. Windows stays as it is;
every change keeps the Windows build green.

## Decisions

- **Release format: AppImage**, built on Ubuntu 22.04 (glibc 2.35 baseline): QEMU + its .so deps,
  `libost_renderer.so` + `libshadertranslator.so`, and the PyInstaller front-end in one file.
- **Display: X11/XWayland only** for the first version. The player runs on xcb even on Wayland
  desktops (`QT_QPA_PLATFORM=xcb`); native Wayland (shared frames instead of window embedding)
  goes to the backlog.
- **Renderer: gfxstream only.** BlueStacks' renderer is a Windows DLL; Linux instances always
  use the open renderer (the GPU renderer setting is hidden there).
- **Testing: WSL2 for day-to-day work, real Linux for milestones.** A separate Ubuntu 22.04 WSL
  distro (the existing Debian one stays untouched), nested KVM for `/dev/kvm`, WSLg for the
  display. WSLg's GPU path is Mesa over D3D12 and nested KVM is slower, so WSL2 proves
  correctness; the user confirms each milestone on a real Linux machine. Note: `.wslconfig`
  sets a custom WSL kernel for all distros, so `/dev/kvm` depends on it; check before phase 2
  and ask before changing anything global.

## Where Windows lives today (survey)

| Area | Windows-specific part | Linux replacement |
|---|---|---|
| `qemu/hw/misc/gr_render.c` (5.1k lines, ~116 Win32 uses) | the whole file: Win32 window + message loop, SRWLOCK/condvars, CreateThread, LoadLibrary, OLE drop target, VEH; and ~60% of it is the BlueStacks-DLL bridge (exe sock replica, vtable lanes, Phase-x responders), which only makes sense with `libOpenglRender.dll` | split the file (below); Linux gets the transport + gfxstream backend + an X11 display, no DLL bridge |
| other devices (`bst_vmsg`, `bst_pgaipc`, `bst_audio`, `hvmem`, `vmmdev`) | none (plain QEMU APIs) | build as is |
| QEMU build | `qemu/build.cmd` (MSYS2 ucrt64, copies MinGW DLLs) | `qemu/build.sh` (distro gcc + meson; `--enable-kvm --enable-slirp`, pipewire/pulse audio) |
| QEMU argv (`qemu_engine.py`) | `-accel whpx,hyperv=off`, `-audiodev dsound`, MSYS2 on PATH, `.exe` names | `-accel kvm`, `-audiodev pipewire` (fallback `pa`), no PATH games; CPU model: keep the explicit feature list (the VMware TSC leaf also works on KVM) or try `-cpu host` |
| renderer build | `renderer/build_gfxstream.cmd` (VS 2022 ClangCL), `.dll` | `renderer/build_gfxstream.sh` (clang/gcc + CMake; gfxstream's primary platform), `libost_renderer.so`; `libshadertranslator.so` from Google's Linux emulator package (same build 16740729, same pinned-download script, its own SHA-1/SHA-256) |
| native screen embedding | QEMU's `gr_renderHostWnd` HWND reparented into Qt (`native.py`, `QWindow.fromWinId`), WM_SIZING aspect lock, WM_COPYDATA drop forwarding | X11: QEMU creates an X window, Qt embeds it by XID (`QWindow.fromWinId` works on xcb); gfxstream draws into it via its X11 subwindow. Drops land on the Qt window natively (XDND goes to the top level), so the drop forwarding is not needed |
| input | Win32 mouse/key messages on the screen window -> `gr_input_post` (QEMU input queue, portable) | X11 events on the screen window (an X event thread in QEMU) -> the same `gr_input_post` |
| front-end helpers | `procs.py` (Toolhelp, NtQuery...; has a /proc branch already), `checks.py` (WHPX), `paths.py` (data next to the exe), adb/7-Zip `.exe` names, `winreg` in `bstprops.py`, `os.startfile` | /proc, `/dev/kvm` access check, XDG data dir for packaged builds, `adb`/`7z` from PATH or downloaded Linux platform-tools, no registry |
| packaging + CI | PyInstaller onedir + `assemble.py` (MSYS2 DLLs, strip), windows-2022 job | PyInstaller onedir + a Linux assemble step (QEMU + its .so deps via an rpath'd lib dir), `ubuntu-22.04` job; artifact format: see decisions |

## Architecture changes

1. **Split `gr_render.c`** (no behaviour change on Windows, done first so every later step is a
   small diff):
   - `gr_core.c`: device glue, op queue/worker, socks, the pure lane + HPP ring transport, the
     stream tracer. Locks/condvars move from raw SRWLOCK to `QemuMutex`/`QemuCond` (the SRWLOCK
     regions exist only to mirror HD-Player's sock layout for the DLL).
   - `gr_bluestacks.c`: the libOpenglRender.dll bridge (vtables, exe replica, responders, VEH).
     Windows only; Kconfig/meson skip it elsewhere.
   - `gr_gfxstream.c`: the gfxstream backend (pump, loading `ost_renderer`, readback, recreate),
     with `dlopen`/`LoadLibrary` behind a tiny shim.
   - `gr_display_win32.c` / `gr_display_x11.c`: screen window, letterbox layout, input, drop
     target (Windows), rotation title. Same small interface: create, layout, rotation, quit.
2. **Adapter**: `ost_renderer` builds as `.so` from the same CMake project; the C API is already
   platform-neutral except `void *parent_hwnd` (becomes a native window handle: HWND or XID).
3. **Front-end**: platform checks behind small helpers (`native.py` gets an X11 path:
   `QWindow.fromWinId(xid)`, no WM_SIZING, aspect lock by resize events instead).

## Phases (each ends with a check, committed on the branch)

1. **Refactor on Windows** - the `gr_render.c` split and the lock/thread swap. Check: Windows build
   + boot Pie64 on both renderers, CI green.
2. **QEMU on Linux** - `qemu/build.sh`, the devices compile (gr: core + gfxstream + stub display),
   boot Pie64 on KVM with the VNC console. Check: Android boots to the home screen (readback).
3. **Renderer on Linux** - `renderer/build_gfxstream.sh`, `libost_renderer.so`,
   `fetch_shadertranslator.py --linux`, st_probe for the .so, smoke test. Check: smoke OK, Pie64
   home screen through gfxstream, Android 11/13, a 3D game, video.
4. **X11 display + input** - `gr_display_x11.c`; front-end embedding on xcb. Check: player shows
   the screen, resize/rotate/letterbox, mouse + keys, screenshots, pop-out.
5. **Front-end portability** - paths, checks (`/dev/kvm`, group membership), adb/7z, audio
   backend, bstprops without the registry, procs on /proc. Check: create an instance from scratch
   (image download + patches), install an APK, files, root, volume.
6. **Packaging + CI** - Linux job on ubuntu-22.04 (build QEMU, renderer, PyInstaller, smoke),
   AppImage as the release artifact next to the Windows ZIP. Check: the AppImage runs on a clean
   distro (and on the user's real Linux machine).
   - **One workflow, two parallel jobs** (`windows`, `linux`) in `build.yml`, plus a final
     `release` job that needs both and publishes the ZIP + AppImage together. Wall time = the
     slower job, not the sum. Building the Windows release on the Linux runner instead is not
     worth it: QEMU cross-builds with MinGW fine, but gfxstream needs MSVC headers/libs (xwin,
     licensing grey zone) and PyInstaller can't produce a Windows exe off Windows.
   - **Make the Windows job fast** (last run: 19 min, of which QEMU 11.5, gfxstream 4.6): ccache
     for the MSYS2 QEMU build and the ClangCL gfxstream build, kept in actions/cache keyed on the
     pinned versions + our patches, so unchanged sources recompile from cache (expect a few
     minutes instead of ~16). Same caching on the Linux job.
7. **Docs** - README Linux section, THIRD-PARTY (Linux deps), BACKLOG.

## Risks

- **Guest timekeeping on KVM**: the BlueStacks kernel was tuned for WHPX here (VMware TSC leaf,
  invtsc). KVM supports the same CPUID leaves; kvm-clock may or may not be in the guest kernel.
  Phase 2 finds out.
- **Wayland**: a foreign window can't be embedded into another process's surface. Plan: run the
  player on XWayland (`QT_QPA_PLATFORM=xcb`) and QEMU's window on X11 too. A native-Wayland path
  (frames shared via dmabuf, or the player drawing readback frames) is a later step.
- **GPU drivers**: desktop GL through GLX/EGL on Mesa (AMD/Intel) and NVIDIA's proprietary
  driver. gfxstream's Linux host path is well trodden (it's the emulator's main platform).
- **libshadertranslator.so ABI**: same check as on Windows (st_probe).
- **ARM translation (libhoudini)** is inside the guest image: unaffected by the host OS.
