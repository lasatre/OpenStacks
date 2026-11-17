# BlueStacks 5 display rotation - static RE spec

Evidence: DLL = libOpenglRender.dll (base 0x180000000), EXE = HD-Player.exe (base 0x140000000),
GL = guestlibs/_android_system_lib64_libhostcall_jni.so (file offsets). Nothing here was run.

## 1. DLL side

### 1.1 Rotation setter = grApiInit table slot +0x40 (DLL 0x180136da0)

Filled by the table writer at DLL 0x180137e3e..0x180137e5a (`mov [rsi+0x40], 0x180136da0`).
Other slots from the same writer (for reference): +0x00 0x180136df0 PgaServerInit, +0x38 0x180136cb0 (key/debug toggles, `cmp ebx,0x18/0x21/0x14`, NOT rotation),
+0x48 0x180136aa0, +0x68 0x180137150 (astc mode), +0x78 0x180137240 (PgaStartReadPixels), +0x80 0x180136be0 (getPixels, tail-jumps FB 0x1801180d0),
+0x88 0x180136c10, +0xc8 0x180137210, +0xd0 0x1801371d0.

Disassembly of 0x180136da0 (exact):

    mov rcx,[rip+g_renderer]        ; global Renderer* (0x180136da0+7+0x9ee831); returns immediately if NULL
    test rcx,rcx ; je ret
    xorps xmm1,xmm1                 ; rot==0 -> 0.0f
    cmp r8d,1 -> xmm1 = 90.0f  (const 0x180519444)
    cmp r8d,2 -> xmm1 = 180.0f (0x180519450)
    cmp r8d,3 -> xmm1 = 270.0f (0x180519454)
    jmp 0x18012a110                 ; Renderer::setRotation thunk: rcx=[rcx+8] (RenderWindow*), jmp 0x18012e8f0

C prototype (Win64 ABI, so float args occupy xmm0/xmm1, the int is the 3rd positional = r8d):

    void PgaSetRotation(float unused0, float unused1, int rotation);   /* rotation = Android Surface.ROTATION_* 0..3 */

HD-Player calls it as `table[0x40](xmm0=1.0f, xmm1=1.0f, r8d=orientation)` (EXE 0x1400d9b19..0x1400d9b3b;
the 1.0f is the constant at EXE 0x14055e41c). rcx/rdx are not read. Values other than 0..3 behave as 0.
So the rotation is NOT degrees from the caller: the caller passes the 0..3 index, the DLL maps it to zRot = 0/90/180/270 degrees (float).

Back end: RenderWindow::setRotation(this, float zRot) = DLL 0x18012e8f0 builds a 0x38-byte command {type=4, float zRot} and
pushes it on the RenderWindowThread ring (this+0x10; if this+8 == 0 it runs inline). The consumer is 0x18012e0f0 (thread loop)
and the synchronous twin 0x18012e3b0; case 4 does `if (zRot != fb->0x2ac) { fb->0x2ac = zRot; FrameBuffer::repost = 0x18011c780(fb,1) }`.
Command table (both consumers): 0 init(0x180118b10) 1 setPostCallback(0x18011d360) 2 setupSubWindow(0x18011da80) 3 removeSubWindow(0x18011c460)
4 setRotation(zRot -> fb+0x2ac) 5 setPaneOffset(px,py clamped 0..1 -> fb+0x2b0/0x2b4) 6 repost 7 shutdown.
Thread: any thread is fine (command ring, SRW-locked; nothing needs the PgaServerInit/UI thread). The actual GL draw runs on the DLL's own
render thread; the child window lives on a DLL-private thread (CreateThread 0x1801c7790, own GetMessage loop, class "BlueStacksApp",
style 0x48000000 = WS_CHILD|WS_CLIPSIBLINGS, WS_EX 4, created with size = parent's client rect at that moment).

### 1.2 setupSubWindow (DLL 0x18011da80) - NOT reachable after init

Signature in this build (no hideWindow argument):

    bool FrameBuffer::setupSubWindow(FBNativeWindowType w, int wx, int wy, int ww, int wh,
                                     int fbw, int fbh, float dpr, float zRot, bool deleteExisting);

(fb fields: +0x68/+0x6c = wx/wy, +0x78/+0x7c = ww/wh, +0x80 = dpr, +0x2ac = zRot, +0x290 = child HWND). If a sub-window already exists and wx,wy,ww,wh,dpr,zRot are unchanged it returns true;
if only geometry changed it MoveWindow()s the child (0x1801c7ad0) and re-posts. The only caller is PgaServerInit
(DLL 0x18013704d: `call 0x18012a140` thunk -> 0x18012eab0, args: hwnd, wx=a2, wy=a3, ww=w, wh=h, fbw=w, fbh=h, dpr=1.0f (const at 0x180137027), zRot=0.0f, deleteExisting=0)
plus the vtable slot 0x1805169d0. There is NO table slot that calls it again, and HD-Player never calls PgaServerInit twice, so the real HD-Player does not re-run setupSubWindow
on rotation either. Do not try to re-call PgaServerInit for rotation.

### 1.3 How the picture follows the window

The sub-window is a plain child that does not follow parent resizes (WndProc 0x1801c7980 only forwards WM_SIZE/WM_PAINT to the repost callback).
The presenter measures the child with GetClientRect each frame (fn 0x1801208c0: `viewport = clientRect * dpr`, calls glViewport only when it changed). Therefore
the host must resize the *child* itself when the aspect changes. Our gr_layout() (gr_render.c) already does `SetWindowPos(g_child, 0,0,parent.cx,parent.cy)` on WM_SIZE, which is sufficient.
zRot only rotates the textured quad about Z inside that viewport (rot 90/270 swap fb w/h aspect for the quad), the window size is the caller's job.

## 2. HD-Player side

### 2.1 Trigger = hcal method 7 (guest hcallOnOrientationChangedRpc)

Guest sender GL 0x14140 (called from the thread-pool lambda at GL 0xf840 with the Java native_onOrientationChange(int) arg):

    getHostBoundBuffer(0xc); u32[0] = 7 (method)   u32[1] = 0x0c (msglen)   u32[2] = orientation (int, Android Surface.ROTATION_0..3)
    processRequest(buf, 0xc)  -> ret = first u32 at the response area (GL 0x1a5c0 returns this+0x20, the host-bound "out" window)
    result != 0  =>  guest logs error 6 "hcallOnOrientationChangedRpc failed, error %d" (non-fatal)

In our bst_vmsg device that is: method = in+0, msglen = in+4, **orientation = in+8** (the generic code calls that field buf_len; for method 7 it is NOT a buffer length).
Method ids from the guest (decoded from every hcall*Rpc, ids 1..64): 1 GetProps, 2 InitInputDebuggingStatus, 3 InitImeStatus, 4 InitVolume, 5 GetTimeInfo, 6 SyncInstalledApps,
**7 OnOrientationChanged**, 8 OnImeChanged, 9 OnTextEditModeChanged, 10 OnActivityDisplayed, 11 OnScreenshotSaved, 12 OnLocaleChanged, ... 22 SetAppConfigDbParams, 44 OnAppNotificationReceived,
54 OnCustomAppOrientationCompleted (string payload `[54][len][u32 strlen+1][chars]`, reply to the host-initiated setGameOrientation; not needed for rotation), 62 CommonCommand.
(The log of seen methods 3,5,6,8,10,22,44,62 lines up with this table; 7 has simply not been received yet: the guest only sends it when Java reports a rotation change.)

Host handler: EXE 0x140064920 `void onOrientationChangedHcall(int orientation)` (event adapter thunk 0x1402d7540 `mov ecx,[rcx+8]; jmp`; string "orientation_changed_hcall_received" is its telemetry event):
`if (cfg->orientation (DAT_140af6950+0x1d8) != orientation) FUN_1400d3f90(orientation, 1)`. No JSON, plain int.

### 2.2 What HD-Player does on rotation

FUN_1400d3f90 (EXE 0x1400d3f90, "applyOrientation(o, resizeWindow)"):
1. `cfg+0x1d8 = o`; aspect `cfg+0x1b8 = (o in {1,3}) ? cfg.h(+0x1b4)/cfg.w(+0x1b0) : w/h`  (portrait iff `((o-1) & ~2) == 0`; 0x1b0/0x1b4 = native panel size, 1600x900 for us).
2. Resizes the Qt main window to the new aspect (swap w/h, clamp to screen, `FUN_1400d3ab0` -> QWindow::setGeometry) and, for the normal layout, tail calls FUN_1400d9aa0, which does in order:
   - `FUN_1402e88c0(o)`: stores `o` in the input manager (global 0x140af9968 + 0x134, under its mutex) -> used for touch mapping (2.3);
   - `if (table[0x40]) table[0x40](1.0f, 1.0f, o)`  (EXE 0x1400d9b0d..0x1400d9b3b, this is the DLL rotate call);
   - `MoveWindow(cfg+0xf8 /*the render HWND given to PgaServerInit*/, x, y, w, h, TRUE)` with the new portrait/landscape rect (FUN_1400d5290), and updates the QML overlay geometry.
So the DLL sequence is just one call: `setRotation(o)`; everything else (window resize, sidebar, ClipCursor) is host UI.
(Unrelated: FUN_1400304f0 "changeResolution" is the ad-screen resize and FUN_14003c0e0 restoreResolution; they do not call the DLL.)

### 2.3 Input mapping: the HOST rotates pointer input

EXE 0x1402ec260 (pointer move/touch point setter, takes view percentages x,y in 0..100, param_2 packs two floats):

    switch (orientation)   // input manager +0x134
      0: (x',y') = (x,       y)
      1: (x',y') = (100-y,   x)
      2: (x',y') = (100-x,   100-y)
      3: (x',y') = (y,       100-x)
    abs_x = (int)(x' * 32767 / 100); abs_y = (int)(y' * 32767 / 100);       // ABS_MAX 0x7fff on the *native* (unrotated 1600x900) panel

i.e. Android receives coordinates in the unrotated panel frame; it expects no further rotation from the host (HD-Player's guest input device is not rotated by Android's
InputReader because the host already inverted the transform). The formulas are the inverse of a +zRot (CCW, GL) rotation of the quad, which is consistent with rotation index r -> zRot = r*90.
(Also scaled to the device size: when `param_3 != 0` abs = ((cfg+0x138 * abs)/0x7fff)*0x7fff/cfg+0x138 for x, +0x13c for y - a rounding quantizer, ignorable.)

## 3. Recipe for our code

### 3.1 bst_vmsg.c: handle hcal method 7
- Parse `orientation = ldl_le_p(in + 8)`; accept 0..3, ignore others.
- Reply: `stl_le_p(out + 0, 0);` (u32 result 0 = success; the guest reads the first u32 of the out window). Do NOT use the generic "buf_len" reject path for it (it would write at out+orientation and leave out+0 stale). Clear poll word as now.
- Hand the value to the renderer through a thread-safe setter, e.g. `gr_set_orientation(int)`; the vmsg handler runs on the vCPU/io thread, the DLL call is thread-safe (command ring) so it can be called directly, but keep the input state update atomic.
- Keep logging; the first time you see "method=7" the end-to-end path is proven.

### 3.2 gr_render.c: rotate
```c
typedef void (*PgaSetRotation_t)(float, float, int);   /* Win64: xmm0, xmm1, r8d */
static void gr_set_orientation(int o)               /* o = Surface.ROTATION_0..3 */
{
    if (o < 0 || o > 3) return;
    g_orientation = o;                              /* used by gr_pointer, atomic */
    PgaSetRotation_t f = (PgaSetRotation_t)TAB(0x40);
    if (f) f(1.0f, 1.0f, o);                        /* exactly what HD-Player does */
    /* then resize the host window: portrait for o==1||o==3 */
    PostMessage(g.hwnd_ui, WM_APP+1, o, 0);         /* UI thread: new size (900x1600 or 1600x900 scaled), SetWindowPos(g.hwnd,...) */
}
```
- The UI handler for WM_APP+1 swaps the window size (portrait = (GR_H, GR_W) scaled like the landscape size) and resizes `g.hwnd`; the existing `gr_layout()` (WM_SIZE) then resizes the DLL child. Tell the front-end embedder (which reparents the top window) about the new aspect so it resizes its container (HD-Player does the equivalent MoveWindow on cfg+0xf8).
- If the picture comes out upside-down/mirrored, swap 1 <-> 3 (the DLL maps index*90 to a GL CCW rotation; the input formulas in 2.3 were derived for that convention, so keep them in sync).
- Do not call PgaServerInit/setupSubWindow again.

### 3.3 Input (gr_pointer)
Replace the plain scale by the HD-Player transform. With `fx = x/(w-1)`, `fy = y/(h-1)` in the *current view* (w,h = child client size, i.e. already portrait when rotated):

    switch (g_orientation) { case 0: ax=fx; ay=fy; break;
                             case 1: ax=1-fy; ay=fx; break;
                             case 2: ax=1-fx; ay=1-fy; break;
                             case 3: ax=fy; ay=1-fx; break; }
    abs_x = ax * INPUT_EVENT_ABS_MAX; abs_y = ay * INPUT_EVENT_ABS_MAX;

Feed these to qemu_input_queue_abs unchanged; Android must keep seeing the panel in its native landscape frame. If the guest ever applies its own input rotation (touch looks rotated twice) the guest-side evdev is orientation-aware and the host transform should be dropped - verify by a tap test.

## 4. Open questions (static analysis cannot settle)
- Whether the guest actually emits method 7 on our images (it is the Java side `native_onOrientationChange`); if never seen, trigger rotation from another source (e.g. guest `dumpsys`/wm user-rotation via the same hcal path or parse method 10 OnActivityDisplayed + app orientation).
- Exact sign convention (swap 1/3) must be confirmed visually once.
- The out-window convention for non-GetProps replies is inferred from processRequest returning `this+0x20`; the bstvmsg "out" window start is assumed to be that pointer.
