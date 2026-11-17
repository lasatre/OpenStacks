/*
 * gr_render.h — Phase-2 render-bridge API for bstpgaipc.
 *
 * Encapsulates ALL work against BlueStacks' own libOpenglRender.dll:
 *   LoadLibraryExA -> grApiInit(table, objA, objB, logPair) -> table[0] = PgaServerInit
 *   table[+0x88]   = PgaRendererHandleConnection(wrapper) per Hst cid connect
 *   + RGB readback -> BMP dumps (screen_pga.bmp) via PgaStartReadPixels'
 *     internal FrameBuffer::getPixels route (worker thread only).
 *
 * Nothing here runs on the vCPU/doorbell context: doorbell ops are queued
 * to a dedicated QemuThread worker which does the DLL calls, mailbox
 * ret/status writes and IRQ completion callbacks.
 */
#ifndef HW_MISC_GR_RENDER_H
#define HW_MISC_GR_RENDER_H

#include "qemu/osdep.h"

typedef struct GrRenderHostOps {
    /* device-relative shared-BAR accessors (plain host RAM pointers; safe
     * to hold across threads, contents guarded by the gr lock) */
    uint8_t *(*mbox)(void *dev, uint32_t cid);      /* 16 bytes, BAR1+cid*0x10 */
    uint8_t *(*window)(void *dev, uint32_t cid);    /* slot window base */
    size_t (*windowSize)(uint32_t cid);
    /* raise served-cid report + IRQ (runs a BH on the main loop) */
    void (*complete)(void *dev, uint32_t cid);
} GrRenderHostOps;

/* Enabled = device property "gr=on" OR env GR_BRIDGE (checked lazily). */
bool gr_render_enabled(void);

/* Device-side property latch (bstpgaipc "gr" bool prop; env ORs in). */
void gr_render_set_device_enabled(bool on);

/* Called from device realize: latch device callbacks + host ops. */
void gr_render_attach(void *dev, const GrRenderHostOps *ops);

/*
 * Doorbell hooks — called from BAR0 write (main-loop context).
 * Return true  = the gr path owns this op's completion: the mbox record
 *                (ret +0x04 / status +0x08) and the IRQ are supplied by the
 *                gr worker thread (possibly later, possibly immediately).
 * Return false = caller falls back to the legacy stub reply.
 */
bool gr_render_op_connect(uint32_t cid);
bool gr_render_op_disconnect(uint32_t cid);
bool gr_render_op_request(uint32_t cid, uint32_t cmd, uint32_t len);

/*
 * Phase-73 HPP kick (guest ioctl 0x7006 -> doorbell cmd 6): wakes the
 * cid's render thread; no mbox result beyond status, NO IRQ. Non-blocking.
 */
void gr_render_kick(uint32_t cid);

/*
 * Phase-77: guest display rotation (hcal method 7, Surface.ROTATION_0..3):
 * rotates the renderer's present, the touch mapping and tells the front-end
 * (window title "OpenStacks display r<o>"). Thread-safe.
 */
void gr_render_set_orientation(int o);

/*
 * Phase-67 live display: latest composited frame from the renderer
 * (RGBA, *w x *h, tightly packed) and a sequence number that changes
 * whenever new pixels arrived. NULL until the first frame.
 */
const uint8_t *gr_render_frame(unsigned *seq, int *w, int *h);

#endif /* HW_MISC_GR_RENDER_H */
