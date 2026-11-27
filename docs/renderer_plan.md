# Renderer replacement: gfxstream instead of BlueStacks' libOpenglRender.dll

Goal: render the BlueStacks guest's GPU stream with Google's open-source gfxstream host
renderer, so OpenStacks no longer needs BlueStacks installed (Windows first, Linux later).

## What we know

- **Guest stack (Pie64)** is the goldfish (Android emulator) one: `libEGL_emulation`,
  `libGLESv1_enc`, `libGLESv2_enc`, `lib_renderControl_enc`, `libvulkan_enc`,
  `gralloc.ranchu`/`goldfish`. Composition: `composer@2.1-service.drmfb` (SurfaceFlinger GLES
  composition, frames presented via the framebuffer).
- **BlueStacks' DLL is a fork of emugl**, the Android emulator renderer that gfxstream grew from.
- **renderControl:** the guest emits opcodes 10000-10065 (66 calls; encoder constructor table,
  `tools/re/encoder_opcodes.py`). The DLL knows 66 `rc*` names: gfxstream's first 64
  (10000-10063) plus two BlueStacks additions, `rcGLShaderWorkaround` and `rcGLHostInfo`,
  most likely at 10064/10065, where gfxstream has `rcReadColorBufferDMA` and
  `rcGetFBDisplayConfigsCount`. These two must be handled before gfxstream sees them.
  To confirm with the stream tracer.
- **GLES1:** identical to gfxstream (opcodes 1024-1316).
- **GLES2/3** (`tools/re/gl_names.py`, names from `libGLESv2_emulation` exports via the shared
  context table): everything below 2475 matches gfxstream. Above it, BlueStacks diverges:
  - 2475/2476 `glTexBuffer(Range)` = gfxstream's `glTexBuffer(Range)OES` (same call).
  - 2478-2480: BlueStacks-only `glGetUCTList`, `glCompressedTexImage2DCache`,
    `glCompressedTexSubImage2DCache`, on numbers gfxstream uses for `glTexBufferRangeEXT`,
    `glEnableiEXT`, `glDisableiEXT`. **Conflict.**
  - 2496-2523: GLES 3.2 calls gfxstream lacks (gfxstream ends at 2486): `glColorMaski`,
    `glBlendEquationi`, `glBlendFuncSeparatei`, `glIsEnabledi`, `glFramebufferTexture`,
    `glPrimitiveBoundingBox`, `glMinSampleShading`, `glPatchParameteri`,
    `glTexStorage3DMultisample`, debug labels, `glGetPointerv`, `glBlendBarrier`,
    `glGetnUniformuiv`. Some have EXT equivalents in gfxstream (renumber); the rest need
    decoder entries.
  - Unnamed so far: 2477, 2481-2495, 2503/2504/2506/2507, 2515-2522 (not exported by the
    emulation lib); to be named from the DLL's decoder or the stream tracer.
  - Plan: patch gfxstream's generated GLES2 decoder (we build it, like QEMU) with a
    BlueStacks opcode table, instead of rewriting the stream.
- **Transport (ours, unchanged):** per guest connection (cid), `gr_render.c` already turns
  the slot window / HPP ring into a byte stream: take guest bytes, stage + send a reply. The
  first 0x100 bytes are BlueStacks' handshake (`name (pid P, tid T) a b hpp`), consumed by us.
- **gfxstream host API** (`host/include/render-utils`): `RenderLib::initRenderer(w, h, ...)`,
  `Renderer::createRenderChannel()` -> `tryWrite(Buffer)` / `tryRead(Buffer*)` per connection,
  `showOpenGLSubwindow(HWND, ...)`, `setPostCallback` (frame readback),
  `repaintOpenGLDisplay`. A one-to-one fit for what we do with the DLL.
- **Build:** gfxstream's Windows build is CMake + Visual Studio ClangCL. QEMU is MinGW, and C++
  can't cross that boundary, so gfxstream sits behind a small adapter DLL with a C API.

## Architecture

```
QEMU (MinGW)                                  ost_renderer.dll (ClangCL)
gr_render.c                                   adapter: C API -> gfxstream C++
  transport: slots, HPP, handshake              RenderLib / Renderer / RenderChannel
  backend interface                             host GL (ANGLE/WGL) / Vulkan
    - bluestacks: libOpenglRender.dll (today)
    - gfxstream:  per-connection pump  ------>  ost_channel_write / ost_channel_read
```

Selected with `OST_RENDERER=bluestacks|gfxstream` (default stays `bluestacks` until gfxstream
is good enough).

## What real traffic uses (GR_TRACE=1, `tools/re/trace_summary.py`)

Pie64 boot plus Chrome, Settings, Play Store, Lawnchair, SystemUI and SurfaceFlinger:
112 distinct opcodes over 21 connections. gfxstream decodes **all but one**:

- **GLES2 2488 = `glGetProgramStateInfoCustom`** (BlueStacks): `GL2Encoder::s_glLinkProgram`
  calls it after **every** successful link, with no extension check (the stock goldfish loop of
  `glGetActiveUniform` calls is gone from this guest; `goldfish-opengl-pie` fork). Required.
  - request `[2488][12][program]`; reply `u32 L`, then `L` bytes:
    `{u32 nU, u32 uStride, u32 nA, u32 aStride, u32 x10, u32 x14}` + nU records of
    `uStride + 20` bytes + nA records of `aStride + 20` bytes, with
    `L == nU*(uStride+20) + nA*(aStride+20) + 24`.
  - record: `{u32 ?, u32 size, u32 type, u32 location, char name[stride]}`, fed to
    `GLSharedGroup::setProgramUniformInfo` / `setProgramAttributeInfo(program, index,
    location, size, type, name)`; the header goes to `initProgramData(program, nU, uStride, nA,
    aStride, x10, x14)`. x10/x14 unknown: capture the DLL's replies to pin them.
  - `s_glGetShaderProgramStateInfoCustom` (separable programs) emits the same opcode.
- renderControl stayed within 10000-10032 (10064/10065 never seen); GLES 3.2 extras
  (2496+) and the texture-cache calls (2478-2480) never seen. A game workload is still to trace.

## Status

- **M1 (mostly done):** renderControl, GLES1 and GLES2 mapped (above), runtime trace done.
  Open: 2488 reply fields x10/x14, a game workload, the Vulkan encoder, Android 11/13 libs.
- **M2 (done):** gfxstream builds on Windows (VS 2022 Build Tools + ClangCL, pinned commit
  cf47bd2). Needs `_WINSOCKAPI_` (winsock.h vs WinSock2.h clash) and
  `renderer/gfxstream-patches/0001-vk-log-string-c_str.patch`.
- **M4 adapter (done):** `renderer/` is a CMake project (gfxstream as a subdirectory) that
  builds `ost_renderer.dll` (C API, `renderer/ost_renderer.h`) and `ost_renderer_smoke.exe`;
  `renderer/build_gfxstream.cmd` -> `deps/ost-renderer-build/Release/`. The smoke test opens a
  channel and gets `rcGetRendererVersion` back.
  - **Host GL = desktop OpenGL (WGL)**, the Android emulator's host-GPU path on Windows
    (gfxstream renderer id HOST). Guest GLSL ES is translated by `libshadertranslator.dll`
    (ANGLE's translator from the Android emulator package, fetched pinned by
    `renderer/fetch_shadertranslator.py`; gfxstream built with `USE_ANGLE_SHADER_PARSER`, header
    vendored in `renderer/third_party/shadertranslator/` from AOSP's ANGLE `emu-master-dev`).
    Without it every guest shader fails to compile.
  - **ANGLE was tried first and dropped** (`OST_GL=angle` keeps it selectable: `EglOnEgl` +
    gfxstream's ANGLE_INDIRECT id; HOST would make it use "#version 330 core" helper shaders and
    a non-GLES readback type). Every ANGLE build tried (Chromium 2.1.28007 / 2.1.28055 from
    Electron, the Android emulator's own) crashes inside the D3D11 backend a few seconds into
    boot: use-after-free-looking reads in `Framebuffer11::markAttachmentsDirty` (post thread) or
    `StateManager11::applyUniformsForShader` (render threads), with or without the display
    window, readback, GLES3, or colour buffer ref counting. Fits ANGLE-D3D11 state shared across
    gfxstream's many GL threads. (Resolved with Electron's Breakpad symbols.) ANGLE's Vulkan
    backend has no matching GLES2 config here. One early boot got through by luck.
  - gfxstream's render thread reads a u32 `clientFlags` first; the BlueStacks guest does not
    send it (no framing loss after the 0x100 handshake in the trace), so the pump injects 0.
  - Logs: warnings and errors by default, `OST_RENDERER_VERBOSE=1` for gfxstream's info lines.
  - Present (patch 0005, issue #6): `OST_VSYNC` (default on for Linux, off elsewhere). Guest
    posts go into a one-slot mailbox and return at once. The post thread draws the newest frame
    and then swaps vsync'd (GLX interval 1). The guest is held only for the draw, never the
    vblank wait. Upstream's unsynced swap lost frames in bursts under XWayland + KWin when the
    guest's 60 Hz phase sat at the compositor's deadline. `OST_VSYNC=0` restores it.
    Patch 0006 drops `graphicsDriverLock` around the swap under `OST_VSYNC`. Otherwise a
    vblank-blocked swap stalls the guest's eglMakeCurrent and sync-fence waits (minimized:
    ~57 fps). That lock works around Vulkan driver bugs, so revisit it if Vulkan is enabled.
  - Patch 0007 makes readers wait on a colour buffer's write fence (binds, posts).
  - Patch 0009: image detection for BlueStacks "Smart" controls (docs/re/kmm_controls.md
    3.20): texture-upload CRCs and per-draw tests in the GLES translator, a report per frame
    flush (rcFlushWindowColorBuffer); `ost_imgd_*` in ost_renderer. Off (one atomic load per
    call) until the controls engine sends a scheme with images.
  - Patch 0008: the Android 9 guest presents frames with rcFlushWindowColorBufferAsync
    (no reply) and has no native fences. So SurfaceFlinger's rcBindTexture or rcFBPost
    can reach the host before the app's flush. The host keeps the order: a buffer set as
    a window surface's target has a pending write until that surface flushes, and readers
    on other threads wait for it (at most 100 ms). `OST_FLUSH_ORDER=0` turns it off.
- **M3 (first cut):** `OST_RENDERER=gfxstream` in gr_render.c: `ost_renderer.dll` (or
  `OST_GFXSTREAM_DLL`) instead of libOpenglRender.dll. Per connection a pump thread
  (`gx_pump`): handshake via the pure lane's createFinish, guest bytes (pure window or HPP
  ring, non-blocking drains) -> `ost_channel_write`, replies -> pure lane staging + send.
  The renderer's notify callback wakes the pump. Display: `ost_renderer_show` into
  gr_renderHostWnd from the UI thread (gr_layout); rotation via `setOpenGLDisplayRotation`;
  readback via the post callback into the VNC frame buffer. `OST_GX_LOG=1` logs every
  renderControl call and reply per connection.
- **First light (Pie64):** the home screen (Lawnchair, SystemUI, wallpaper) renders on
  gfxstream + ANGLE; SurfaceFlinger, Settings, SystemUI, system_server, Play Store and GMS
  connect over the pure lane, Lawnchair over HPP. Two fixes got there:
  - **Colour buffer ref counting:** BlueStacks' gralloc (inside SurfaceFlinger) does
    rcCreateColorBuffer -> rcOpenColorBuffer2 -> rcCloseColorBuffer up front and keeps
    using the buffer. That is the pre-O rule (create holds a reference); with O+ rules
    gfxstream frees it at that close ("bad color buffer handle"). gr_render passes guest API
    level 25 to gfxstream (it only drives this and a Vulkan version cap);
    `OST_GUEST_API` overrides. NoDelayCloseColorBuffer off (emulator default, 1 s grace).
  - **Opcode 2488** (`renderer/gfxstream-patches/0002-...`): gfxstream's render thread waits
    forever on an opcode no decoder takes, so the first glLinkProgram hung SurfaceFlinger.
    The GLES2 decoder now answers it from the host program. Fields pinned from the guest:
    uStride/aStride are GL_ACTIVE_UNIFORM/ATTRIBUTE_MAX_LENGTH (ProgramData +0x38/+0x40),
    records are {nameLen, size, type, location, name[maxLen + 4]} (the guest reads size,
    type, location and name), x10 is the uniform block count (+0x44, read only by
    getActiveUniformBlockCount, which glGetProgramiv does not use), x14 (+0x48) has no
    reader. Both are sent as 0.
- **Desktop GL boot (Pie64):** boots to the home screen, stable across the boot, native
  display window shows it (checked by capturing the hidden window off-screen with PrintWindow).
- **Readback (VNC console, host screenshots):** on GLES 3 hosts gfxstream reads back
  asynchronously: the post callback only signals a frame, the pixels come from its
  ReadPixelsCallback (which waits on gfxstream's readback thread). The adapter pulls them on its
  own thread. Patch 0003 keeps the readback type GLES-valid for the translator.
- **Apps and games (Pie64):** a Unity 3D game (Dinosaur Evolution) launches, renders and plays
  (input via the display window, a posted click started a level); a minute of gameplay without
  errors. Portrait rotation: gfxstream's angle runs opposite to BlueStacks', so o -> 360 - 90o.
- **Front-end switch:** per-instance `renderer = bluestacks | gfxstream` (instance dialog "GPU
  renderer"); the engine sets `OST_RENDERER`; the setup check asks for BlueStacks only when an
  instance uses its renderer, and for `ost_renderer.dll` + `libshadertranslator.dll` next to QEMU
  when one uses gfxstream.
- **Android 11 and 13:** both boot to the home screen on gfxstream as they are (Android 11 also
  runs the separate gralloc allocator process; it connects like the others).
- **CI + release:** build.yml builds the renderer after QEMU (shallow fetch of the pinned
  gfxstream commit, patches, ClangCL) with the shader translator download cached; assemble.py
  ships both DLLs (they are in build\) plus their licenses in `licenses/`.
- **Player:** embedding, resizing and rotation work. gfxstream's showOpenGLSubwindow fbw/fbh are
  the viewport in window pixels (not the guest size); gr_render letterboxes the guest aspect
  into the window, and fills it when the aspect is within 2 px. Pixel-identical to the
  BlueStacks renderer in the player (user image diff).
- **CI:** green, release ZIP ships the renderer.
- **Default (done):** new instances use gfxstream; instances saved before keep `bluestacks`.
  The README no longer requires BlueStacks.
- **Next:** Linux (BACKLOG), the Vulkan encoder, the gaps no workload has hit yet.
- Crash locator: gr_render's gfxstream path logs the faulting module+offset and stack return
  addresses on an access violation; `ost_renderer.pdb` + llvm-symbolizer resolve ours.

## Milestones

1. **Compatibility map** - encoder opcode tables (rc done; GLES1/2, Vulkan next), BlueStacks
   additions, a stream tracer for real workloads.
2. **Build gfxstream's host renderer on Windows** (`gfxstream_backend`, ClangCL).
3. **Backend interface in gr_render.c**, DLL backend first, no behaviour change.
4. **gfxstream backend:** `ost_renderer.dll` adapter + per-connection pump; display subwindow,
   rotation, readback.
5. **Close the gaps** until the home screen, apps and games run; then make it the default.
