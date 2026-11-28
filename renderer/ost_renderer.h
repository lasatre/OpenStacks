/*
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * ost_renderer.h - C API of OpenStacks' renderer plugin (ost_renderer.dll), the boundary
 * between QEMU (MinGW, gr_render.c) and gfxstream (C++, Visual Studio ClangCL).
 * Plain C types only, so the two toolchains can talk. docs/renderer_plan.md.
 */
#ifndef OST_RENDERER_H
#define OST_RENDERER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#if defined(_WIN32) && defined(OST_RENDERER_BUILD)
#define OST_API __declspec(dllexport)
#elif defined(_WIN32)
#define OST_API __declspec(dllimport)
#else
#define OST_API __attribute__((visibility("default")))
#endif

#define OST_RENDERER_API_VERSION 1

typedef void (*ost_log_fn)(void *user, int level, const char *msg);  /* 0 err .. 3 debug */
/* A composited frame (RGBA8, w*h*4 bytes, top row first), only while frame capture is on. */
typedef void (*ost_frame_fn)(void *user, int w, int h, const uint8_t *rgba);

typedef struct OstRendererConfig {
    int api_version;           /* OST_RENDERER_API_VERSION */
    int width, height;         /* guest display size */
    int guest_api_level;       /* Android SDK level of the guest (28 = Android 9) */
    ost_log_fn log;            /* optional */
    void *user;
} OstRendererConfig;

/* Start the renderer (host GPU). 0 on success. Call once. */
OST_API int ost_renderer_init(const OstRendererConfig *cfg);

/* Show the display inside `parent_hwnd`: a child window at (x, y) of w x h client pixels,
 * which the (rotated) guest frame fills, stretched; the caller letterboxes. rotation_deg:
 * 0/90/180/270. Call again to move/resize/rotate. 0 on success. */
OST_API int ost_renderer_show(void *parent_hwnd, int x, int y, int w, int h,
                              float rotation_deg);
OST_API void ost_renderer_set_rotation(float rotation_deg);
/* Forget the display child window (e.g. it died with its parent); the next
 * ost_renderer_show creates a new one. */
OST_API void ost_renderer_drop_window(void);
OST_API void ost_renderer_repaint(void);

/* Frame capture for readback consumers (VNC display, screenshots); NULL turns it off. */
OST_API void ost_renderer_set_frame_callback(ost_frame_fn fn, void *user);

/* One channel per guest connection, after the BlueStacks handshake was consumed. */
typedef struct OstChannel OstChannel;
OST_API OstChannel *ost_channel_open(void);
/* Guest -> host bytes. Blocks while the renderer's queue is full. 0 ok, -1 channel stopped. */
OST_API int ost_channel_write(OstChannel *ch, const void *data, size_t len);
/* Host -> guest reply bytes: up to `cap` bytes into `buf`. Waits up to timeout_ms
 * (-1 = forever, 0 = poll). Returns bytes (> 0), 0 on timeout, -1 if the channel stopped. */
OST_API long ost_channel_read(OstChannel *ch, void *buf, size_t cap, int timeout_ms);
/* Wake-up for a pump that waits on its own condition: `fn` runs on a renderer thread (with
 * the channel's lock held: only signal, never call back in) once reply bytes are readable
 * after ost_channel_arm(), or when the channel stops. Arming is one-shot, like the
 * emulator's setWantedEvents; it fires at once if bytes are already waiting. */
typedef void (*ost_notify_fn)(void *user);
OST_API void ost_channel_set_notify(OstChannel *ch, ost_notify_fn fn, void *user);
OST_API void ost_channel_arm(OstChannel *ch);
/* Stop the channel: wakes blocked writes/reads (they return -1). Any thread. */
OST_API void ost_channel_stop(OstChannel *ch);
/* Stop + free. Only once no other thread uses `ch`. */
OST_API void ost_channel_close(OstChannel *ch);

OST_API void ost_renderer_shutdown(void);

/* Image detection for BlueStacks "Smart" game controls (docs/re/kmm_controls.md 3.20):
 * the translator hashes texture uploads and tests each draw against a list of images.
 * Configure with begin, add per image, commit (an empty list turns it off). `emu` 0 = no
 * screen-position test, 1 = Unity (GroupA); `sampler` names the sampler uniform whose unit
 * is checked ("" = unit 0), `sampler2` a fallback. `rect` (x1 y1 x2 y2, percent) or NULL.
 * The callback runs on a render thread when the set of images matched in a frame changes
 * (indices into the committed list, ascending); signal only, never call back in. */
typedef void (*ost_imgd_fn)(void *user, uint32_t gen, const int32_t *idx, int n);
OST_API void ost_imgd_set_callback(ost_imgd_fn fn, void *user);
OST_API void ost_imgd_begin(uint32_t gen, int emu, const char *sampler, const char *sampler2);
OST_API void ost_imgd_add(uint32_t crc, int tex_attr, int pos_attr, float u, float v,
                          const float *rect);
OST_API void ost_imgd_commit(void);

#ifdef __cplusplus
}
#endif
#endif /* OST_RENDERER_H */
