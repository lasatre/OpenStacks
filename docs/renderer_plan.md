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

## Status

- **M1 (in progress):** renderControl, GLES1 and GLES2 mapped (above). Open: the Vulkan
  encoder (`libvulkan_enc`, opcodes 20000+), the unnamed GLES2 opcodes, and runtime
  confirmation with the stream tracer (`GR_TRACE=1`).
- **M2 (done):** `renderer/build_gfxstream.cmd` builds `gfxstream_backend.dll` (6.8 MB) with VS
  2022 Build Tools + ClangCL at the pinned commit cf47bd2. Needs `_WINSOCKAPI_` (winsock.h vs
  WinSock2.h clash) and `renderer/gfxstream-patches/0001-vk-log-string-c_str.patch`. The DLL
  exports `initLibrary` (RenderLib, C++) and the virtio-gpu `stream_renderer_*` C API; the
  adapter uses `initLibrary`.

## Milestones

1. **Compatibility map** - encoder opcode tables (rc done; GLES1/2, Vulkan next), BlueStacks
   additions, a stream tracer for real workloads.
2. **Build gfxstream's host renderer on Windows** (`gfxstream_backend`, ClangCL).
3. **Backend interface in gr_render.c**, DLL backend first, no behaviour change.
4. **gfxstream backend:** `ost_renderer.dll` adapter + per-connection pump; display subwindow,
   rotation, readback.
5. **Close the gaps** until the home screen, apps and games run; then make it the default.
