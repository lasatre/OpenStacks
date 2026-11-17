/*
 * gr_render.c — Phase-2: BlueStacks libOpenglRender.dll render bridge.
 *
 * Wires the DLL into the bstpgaipc Hst device:
 *   - grpoc-proven init path (grApiInit -> PgaServerInit) on a dedicated
 *     worker thread; the QEMU iothread / vCPU context never blocks in
 *     the renderer init (loads GL, creates a hidden window + D3D
 *     device on its own thread).
 *   - per-gfx-cid connections via table[+0x88] PgaRendererHandleConnection
 *     with an exe-shaped HstSocketHost replica (FUN_1402ac940 byte
 *     layout, REA_HST_PROTOCOL.md): mbox len -> sock+0x210, mbox+4
 *     pointer -> sock+0x218, run=1 + cond wake on every REQUEST so the
 *     DLL's own RenderThread (emugl::RenderThread::main) can drive
 *     peek/recv through the wrapper vtable we install.
 *   - SYN producer (root cause of the 0x1800fcbf0 cra-sh): the DLL's
 *     RendererImpl::start dispatches every connection through the
 *     consumer stream at [sock+0x30], a "{void*; SYN fn}" cell. The
 *     exe's slot vt+0x58 (0x1402aa6d0) returns sock+0x30 and the DLL
 *     dereferences cell[+0x08] inside std::deque work (0x18012f9b0
 *     rehash copies it into 0x40 byte blocks; vt 0x180517110).
 *     RendererImpl::start at 0x18012f2c0 movs the produced pointer
 *     into [sock+0x18]. Previous stub kept the cell NULL: kernel DEREFS
 *     at 0x1800fcbf0 (0x1800fcdb0 fn, HstStream, 4 minidumps).
 *     Behaviour per producer mode (table below, GR_SYN_MODE=1/2):
 *     mode 1 (sync0x18): direct blocking cell, handler at [0x18]
 *     = HstStream ctor with args == direct call.
 *     mode 2 (boxed): producer returns heap record {void*; vtable;
 *     sock-handoff}, 0-based index into stream, worker carry.
 *   - mbox ret/status + IRQ completion happen after the op is queued
 *     (async from the device doorbell); the device completes through
 *     its BH so the iothread never blocks.
 *   - any DLL failure degrades to Phase-1 mbox semantics: the guest
 *     boot continues exactly like the Phase-1 capture build.
 *   - pixel pull: renderer-frame-bits pull path through the post
 *     callback + FB getPixels route (table +0xa0) -> BMP dumps to
 *     "screen_pga.bmp" in QEMU's cwd every ~2 s when bytes change.
 *
 * Tags: [gr_render] ... first fire per condition; counters otherwise.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qemu/thread.h"
#include "qemu/bswap.h"
#include "qemu/atomic.h"
#include "hw/misc/gr_render.h"

/* windows.h comes in via qemu/osdep.h on win32 */

/* ================================================================= */
/* log helpers (stderr only, first-fire per condition)               */

static void gr_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    /* Phase-11: wall-clock stamp (host ms) so each line can be
     * correlated against the guest's serialGR.log timeline.          */
    fprintf(stderr, "[gr_render %10u] ", (unsigned)GetTickCount());
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

/* one-shot logger keyed by an opaque small id (up to 64 keys)        */
static uint64_t once_mask;
/* Phase-14 guestwrite tap globals */
static uint64_t gr_gw_serial;
static unsigned gr_gw_this;
static unsigned gw_shape_seen[32];

static void gr_once(unsigned key, const char *fmt, ...)
{
    uint64_t bit = 1ull << (key & 0x3f);
    if (qatomic_fetch_or(&once_mask, bit) & bit) return;
    va_list ap;
    va_start(ap, fmt);
    fputs("[gr_render] ", stderr);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

/* ================================================================= */
/* DLL typedefs (x64 Microsoft ABI)                                  */

typedef int  (*grApiInit_t)(void *table, void *objA, void *objB, void *logPair);
typedef int  (*PgaServerInit_t)(void *hwnd, int a2, int a3, int w, int h,
                                int mode, int dgpu, const char *name);
typedef int  (*PgaHandConn_t)(void *conn);
/* FB setPostCallback replica (DLL internal fcn @RVA 0x11cf20)         */
typedef void (*fb_setPost_t)(void *fb, void *postFn, void *postCtx,
                             int display, int width, int height,
                             int byteFlag);
/* FB getPixels route through table +0xa0 (fcn.1801180d0)             */
typedef int (*fn_rgba_t)(int display, void *dst, int linesize, int fmt,
                         const void *prev, int epoch);

#define TAB(off)  (((void **)g.table)[(off) / 8])

/* ================================================================= */
/* mbox record semantics (BAR1 + cid*0x10, little-endian)            */

static void mbox_set(uint8_t *mb, int32_t ret, int32_t status)
{
    if (!mb) return;
    stl_le_p(mb + 0x04, (uint32_t)ret);
    stl_le_p(mb + 0x08, (uint32_t)status);
}

/* Phase-5 content probe: gr_dump now owns its own internal quota       */

static void gr_dump(const char *tag, const uint8_t *p, unsigned n)
{
    static unsigned dump_budget = 24;   /* effective max dumps per boot   */
    if (!dump_budget || !p) return;
    dump_budget--;
    unsigned i, m = n < 48 ? n : 48;
    char hex[3 * 48 + 4];
    unsigned o = 0;
    for (i = 0; i < m; i++) o += sprintf(hex + o, "%02x ", p[i]);
    hex[o] = 0;
    gr_log("HEX %s n=%u: %s", tag, n, hex);
}

/* ================================================================= */
/* objA / objB / log-pair stubs                                      */

static int obj_stub_generic(void) { return 0; }

static int gr_logGate(int channel, int severity)
{
    static unsigned logged;
    if (logged++ == 0) {
        gr_log("logGate channel=%d severity=%d -> 1", channel, severity);
    }
    return 1;
}

/* DLL's xlog sink: string pointer decode (VirtualQuery-guarded)      */
#define SINK_PRINT_MAX 150
static unsigned sink_cnt;

static int gr_logSink(int a, int b, void *c, void *d)
{
    unsigned n = ++sink_cnt;
    (void)d;
    if (n > SINK_PRINT_MAX) return 0;
    MEMORY_BASIC_INFORMATION mbi;
    if (c && VirtualQuery(c, &mbi, sizeof(mbi)) != 0 &&
        mbi.State == MEM_COMMIT &&
        (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                        PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                        PAGE_EXECUTE_WRITECOPY)) != 0) {
        char buf[128];
        unsigned i;
        const unsigned char *p = (const unsigned char *)c;
        for (i = 0; i < sizeof(buf) - 1; i++) {
            unsigned char ch = p[i];
            if (ch == 0) break;
            buf[i] = (ch >= 0x20 && ch < 0x7f) ? (char)ch : '.';
        }
        buf[i] = 0;
        if (i >= 3) {
            fprintf(stderr, "[gr_render] xlog#%u a=0x%x b=0x%x: %s\n",
                    n, a, b, buf);
            if ((n & 0x07) == 0) fflush(stderr);
            return 0;
        }
    }
    return 0;
}

/* ================================================================= */
/* module state                                                      */

struct GrRenderHostOps;   /* full typedef in gr_render.h */

#define GR_DLL_PATH "C:\\Program Files\\BlueStacks_nxt\\libOpenglRender.dll"

/* renderer mode: 1 = GL/Vulkan ("vlcn" — the real BlueStacks conf),
 * 2 = ANGLE/D3D9 ("dx9"). Default 1; GR_RENDERER_MODE env overrides. */
static int gr_renderer_mode(void)
{
    static int mode, probed;
    if (!probed) {
        probed = 1;
        mode = 1;
        char buf[8];
        DWORD n = GetEnvironmentVariableA("GR_RENDERER_MODE", buf, sizeof(buf));
        if (n > 0) {
            int v = atoi(buf);
            if (v == 1 || v == 2) mode = v;
        }
    }
    return mode;
}

#define GR_W 1600
#define GR_H  900
#define GR_FB_BYTES (GR_W * GR_H * 4)

#define GR_QSZ     128
#define GR_MAX_CID 0x87

#define OP_CONNECT     1
#define OP_DISCONNECT  2
#define OP_READ        3
#define OP_WRITE       4
#define OP_HYPERCALL   6
#define OP_HDIRECT     7

typedef struct GrOp { uint32_t cid, cmd, len, pad; } GrOp;

/* complete exe HstSocketHost image (must precede GrState)            */
typedef struct GrSock {
    bool     used;
    bool     firstReplyLogged;   /* per-socket first reply hexdump tag   */
    unsigned cid;
    void    *wrapper;
    uint8_t  raw[0x228];
    /* --- Phase-6: gr-owned SHADOW cursors (Phase-6 fix (a): the exe's
     * own sock layout must stay byte-exact; our bookkeeping lives here
     * exclusively. Definitions per the client/exe decode (NOTES 14-15):
     *   wcnt  : guest doorbell write chunk valid at win[0..wcnt)
     *   rpos  : exe-side within-chunk read cursor
     *   qhead : our reply-serve cursor into the exe's reply ring
     *   wpos  : exe's reply ring write pos (hstAlloc)                  */
    uint32_t wcnt;
    uint32_t rpos;
    uint32_t qhead;
    uint32_t wpos;
    int32_t  stagingCap;         /* exe sock +0x0c mirrored (gr-owned)  */
    unsigned sessionSerial;      /* per-cid doorbell session counter    */
    /* Phase-8 per-vt-hook counters (gr-side; per cid)                  */
    unsigned peekCount, peekNeedSum;
    unsigned recvCount, recvBytes;
    unsigned allocCount, allocBytes;
    unsigned sendCount, sendBytes;
    unsigned relocCount, relocBytes;
    unsigned replayCount;
    /* Phase-10 renderControl responder state (gr-owned)                */
    bool     rcHelloDone;      /* exe's 1024-byte hello served          */
    uint32_t rcHelloPos;       /* consumed offset in the hello pattern  */
    unsigned rcOpServed;       /* sf ops we have answered this session  */
    bool     rcOpNamesLogged[128]; /* per-op once-per-session logging   */
    uint32_t rcLastOps[16];    /* the exe's per-record op log           */
    unsigned rcLastOpCount;    /* per-session exe record ops dispatched */
    /* Phase-10 gr-owned gr-render reply staging ring (0x20000 bytes)   */
    uint8_t  rcReplyBuf[0x20000];
    uint32_t rcReplyLen;       /* pending reply bytes                   */
    uint32_t rcReplyPos;       /* consumed offset                       */
    /* Phase-11: exe-side per-init reply chain (AOSP-P renderControl
     * full handshake, per-op ordered per exe Phase-3's decode)         */
    unsigned initSeq;          /* per-op index in exe Phase-3 handshake */
    bool     initSeqOn;        /* AOSP-P exe reply path enabled         */
    bool     synOpSent;        /* Phase-14 synthetic op stimulus flag   */
    uint32_t exeOpPending;     /* Phase-16 exe op awaiting reply write  */
    uint32_t exeStrName;       /* Phase-25: current string-class name   */
    uint32_t exeStrBuf;        /* Phase-25: her bsize arg (clamped)     */
    uint32_t rcReplyOp;        /* Phase-42: op that produced rcReplyBuf  */
    unsigned rctl2714ServeCount; /* Phase-42: per-session 0x2714 serve log count */
    bool     rctl2714Active;   /* Phase-42: sfIdx=4 reply in flight (both reads) */
    uint8_t  rctl2714Fb[8];    /* Phase-43: 0x2714 ret fallback {ret, ret}      */
    unsigned rctl2714FbPos;    /* Phase-43: consumed offset in the fallback     */
    bool     rctl2714PairOpen; /* Phase-48: her 0x2714 pair mid-read -> no reset */
    uint32_t rctl2714OutVal;   /* Phase-48: her out dword (capture at serve #1)  */
    bool     rctl2715Active;   /* Phase-48: staged 0x2715 class in flight        */
    bool     rctl2715PairOpen; /* Phase-48: her 0x2715 pair mid-read -> no reset */
    uint8_t  rctl2715Fb[8];    /* Phase-48: small-ask reserve {11,11}            */
    unsigned rctl2715FbPos;    /* Phase-48: reserve cursor                       */
    uint32_t lastAllocOp;      /* Phase-46: op class of the last ring prefill   */
    bool     nameCached;       /* Phase-27: guest name seen (createFinish) */
    char     nameCopy[0x100];  /* Phase-27 cached guest name post        */
    uint8_t  recv[0x40000];
    int32_t  recvLen;
    /* --- Phase-64 GR_PURE lane state (dev/NOTES.md §9) --- */
    bool     pWritePending;    /* guest WRITE not yet acked               */
    uint32_t pWriteLen;
    bool     pReadPending;     /* guest READ waiting for the DLL's send   */
    uint32_t pReadWant;
    uint8_t *pRbuf;            /* private reply buffer (hstAlloc)         */
    uint32_t pWpos;
    uint8_t *pCarry;           /* chunk-spanning record assembly          */
    uint32_t pCarryCap, pCarryLen, pCarryPos;
    uint8_t *pOut;             /* copy-out for chunk-tail reads           */
    uint32_t pOutCap;
    uint32_t pWrites, pReads, pSends, pAcks, pSpans;
} GrSock;

typedef struct GrState {
    bool started, failed, ready, devEnabled;

    QemuMutex lock;
    QemuCond  cv;
    QemuThread thread;

    GrOp q[GR_QSZ];
    unsigned qhead, qtail, qdropped;

    HMODULE dll;
    uint8_t *table;
    uint8_t  objA[0x88];
    uint8_t  objB[0x58];
    void    *logPair[2];
    HWND     hwnd;

    GrSock socks[GR_MAX_CID + 1];

    fn_rgba_t rgba;
    uint8_t  *fbBuf, *fbPrev;
    bool      fbHas;
    uint64_t  lastBmpMs;
    unsigned  bmps;
    bool      postOn;
    unsigned  lastPosts;
    bool      rgbaOnce;
    unsigned  connectOK, connectFAIL, writes, reads, readsServed;
} GrState;

static GrState g;
static void *g_dev;
static GrRenderHostOps g_ops;

#define GR_MBOX(i)  (g_ops.mbox ? g_ops.mbox(g_dev, (i)) : NULL)
#define GR_WIN(i)   (g_ops.window ? g_ops.window(g_dev, (i)) : NULL)
#define GR_WSZ(i)   (g_ops.windowSize ? g_ops.windowSize((i)) : 0)
#define GR_DONE(i)  do { if (g_ops.complete) g_ops.complete(g_dev, (i)); } while (0)

/* ================================================================== */
/* exe HstSocketHost replica — field accessors                        */
/* (FUN_1402ac940 byte layout, REA_HST_PROTOCOL.md)                   */

typedef void (*cellobj_fn_t)(void *cellObj, unsigned long long a2,
                             unsigned long long a3, unsigned long long a4);

/* --- SyncThreadCmd producer feature table (mode + pair) ---------
 * Exe invariant: cell inside sock at +0x30: [sock+0x30] = registered
 * entry, [sock+0x38] = the exe's SyncThreadCmd producer entry fn.
 * GR_SYN_MODE switches the producer semantic:
 *   1 (default) "sync"  — run the handler inline on the calling
 *     (DLL render) thread, then let the DLL resume.
 *   2 "thread" — the handler is queued to our own dispatcher
 *     thread; completion signalled there.
 * Installed per sock in sock_prepare so the table can be flipped
 * across boots by env alone (no rebuild of the pipeline).          */
static void *g_cellEntry;         /* registered record ("worker")  */

static void syn_producer_sync(void *obj, unsigned long long a2,
                              unsigned long long a3,
                              unsigned long long a4)
{
    (void)obj; (void)a3; (void)a4;
    gr_once(0x70, "SYN producer SYNC: handler=%p", (void *)(uintptr_t)a2);
}

static void syn_producer_thread(void *obj, unsigned long long a2,
                                unsigned long long a3,
                                unsigned long long a4)
{
    (void)obj; (void)a3; (void)a4;
    gr_once(0x71, "SYN producer THREAD: handler=%p", (void *)(uintptr_t)a2);
}

#define S_CELL(s)      (*(void       **)((s)->raw + 0x30))
#define S_CELLCMD(s)   (*(cellobj_fn_t *)((s)->raw + 0x38))

/* Phase-6: exe-layout accessors ring +0x00/04/08 are DROPPED - the exe's
 * own cursors; shadow equivalents (S_RPOS/S_WCNT) live in GrSock fields. */
#define S_RPOS(s)      ((s)->rpos)
#define S_CAP(s)       ((s)->stagingCap)       /* exe sock +0x0c (gr's)   */
#define S_MOVEL(s)     (*(int32_t       *)((s)->raw + 0x20))
#define S_MOVESRC(s)   (*(void         **)((s)->raw + 0x28))
/* ================================================================== */
/* Phase-6 window layout (per-cid aperture is 0x40000 bytes; gr-side
 * partition — the guest and the exe see the SAME bytes, so our lane
 * must not let the two directions collide):
 *
 *   win[0x00000 .. 0x10000)  CLIENT VIEW — the sf flush chunks land
 *                            here (guest hstFlush memcpy at win[0]) AND
 *                            our OP_READ reply-serve memcpy target
 *                            (the client's hstRecv stages win[0..served)).
 *                            One lane at a time, lock-step doorbells.
 *   win[0x10000 .. 0x20000)  EXE REPLY RING — our vt_alloc hands the
 *                            exe `win + 0x10000 + wpos`; the exe writes
 *                            its rcGet replies there; OP_READ serves
 *                            from qhead back into the client view.
 *   win[0x30000 .. 0x40000)  EXE RECORD STREAM — our OP_WRITE copies
 *                            the sf doorbell chunk from the client view
 *                            into win + 0x30000 + wcnt (accumulating so
 *                            the exe's record framing stays continuous
 *                            across doorbells); vt_recv/vt_peek4/
 *                            vt_recvcopy serve from that base.
 */

#define GR_WIN_CLIENT 0x00000
#define GR_WIN_REPLY  0x10000
/* Phase-53: the validated reply-class cap. The real getConfigs class is
 * 1636 B ({34,11} -> 34*(11+1)*4+4); 16 KB covers it with huge margin
 * and bounds every drifted computation (stale out/ret pairs, shifted
 * record-parse wants such as the 262144 storm) to a bounded drain.   */
#define RC_CLASS_MAX  0x4000
#define GR_WIN_RSIZE  0x20000
#define GR_WIN_RECORD 0x30000
#define GR_WIN_RBASE  0x30000

#define S_WINDOW(s)    (*(void         **)((s)->raw + 0x130))
#define S_WINSIZE(s)   (*(uint32_t      *)((s)->raw + 0x138))
#define S_HPPBUF(s)    (*(void         **)((s)->raw + 0x140))
#define S_HPPCNT(s)    (*(uint32_t      *)((s)->raw + 0x148))
#define S_HPP(s)       ((s)->raw[0x14c])
#define S_HPP2(s)      ((s)->raw[0x14d])
#define S_MTXP(s)      ((SRWLOCK              *)((s)->raw + 0x150))
#define S_CNDP(s)      ((CONDITION_VARIABLE   *)((s)->raw + 0x1a0))
#define S_RUN(s)       ((s)->raw[0x1e8])
#define S_CIDP(s)      ((uint32_t     *)((s)->raw + 0x1ec))
#define S_RAISED(s)    (*(uint32_t      *)((s)->raw + 0x200))
#define S_DISC(s)      (*(uint32_t      *)((s)->raw + 0x204))
#define S_REF(s)       (*(int32_t       *)((s)->raw + 0x208))
#define S_MBOXLEN(s)   (*(uint32_t      *)((s)->raw + 0x210))
#define S_MBOXDATA(s)  (*(void         **)((s)->raw + 0x218))
#define S_T220(s)      ((s)->raw[0x220])
/* Phase-6: all ring cursors are gr-owned SHADOW state (GrSock fields),
 * NOT sock raw bytes. Raw +0x00/^0x04/+0x08/+0x10 stay byte-zero
 * so the exe's own cursor semantics (its vt[0x00] out pointers, its
 * deque bookkeeping) see its OWN pristine ring. Accessors:           */
#define S_WPOS(s)      ((s)->wpos)
#define S_WCNT(s)      ((s)->wcnt)
#define S_QHEAD(s)     ((s)->qhead)
#define S_RPOS(s)      ((s)->rpos)
/* exe HPP staging buffer (NegotiatePipeType mallocs 0x80000 here)     */
#define S_STAGE(s)     (*(void         **)((s)->raw + 0x18))

static void pure_reset(GrSock *s);   /* Phase-64 GR_PURE lane */

static void sock_prepare(GrSock *s, uint32_t cid)
{
    pure_reset(s);
    if (s->wrapper != NULL) {
        /* RECONNECT on a live sock (exe refcounts it at +0x208): only
         * reset the ring cursors and the disconnect flag; the SRWLock
         * + cond regions at +0x150/+0x1a0 must survive untouched —
         * the DLL's own render thread may hold them right now.        */
        /* Phase-6: cursors are shadow fields now, no sock write side.  */
        S_WPOS(s) = 0;
        S_WCNT(s) = 0;
        S_RPOS(s) = 0;
        S_QHEAD(s) = 0;
        s->firstReplyLogged = false;   /* per-session dump tag reset     */
        s->rctl2714ServeCount = 0;     /* Phase-42: per-session 0x2714 serve log reset */
        s->rctl2714Active = false;
        s->rctl2714FbPos = 0;          /* Phase-43: fallback cursor reset           */
        s->rctl2714PairOpen = false;   /* Phase-48: pair/commit state reset          */
        s->rctl2714OutVal = 0;
        s->rctl2715Active = false;
        s->rctl2715PairOpen = false;
        s->rctl2715FbPos = 0;
        s->lastAllocOp = 0;            /* Phase-46: prefill-class marker reset      */
        s->rcReplyOp = 0;
        s->sessionSerial++;
        /* Phase-11: every reconnect re-plays the AOSP-P init chain     */
        s->initSeq = 0;
        s->initSeqOn = (S_HPP(s) == 0);
        s->rcHelloDone = false;
        s->synOpSent = false;
        s->exeOpPending = 0;
        S_RUN(s) = 0;
        S_RAISED(s) = 0;
        S_DISC(s) = 0;
        S_MBOXLEN(s) = 0;
        S_MBOXDATA(s) = NULL;
        return;
    }
    memset(s->raw, 0, sizeof(s->raw));
    s->used = true;
    s->cid = cid;
    S_CAP(s) = 0;
    S_MOVEL(s) = 0;
    S_MOVESRC(s) = NULL;
    S_WINDOW(s) = GR_WIN(cid);
    S_WINSIZE(s) = (uint32_t)GR_WSZ(cid);
    S_HPPBUF(s) = NULL;
    S_HPPCNT(s) = 0;
    S_HPP(s) = 0;
    S_HPP2(s) = 0;
    /* exe: _Mtx_init_in_situ(+0x2a=0x150, mode 2) semantics; ucrt uses
     * SRWLOCK-style internals. Initialize the SRWLOCK + cond regions
     * exactly at those offsets.                                          */
    {
        HMODULE k32 = GetModuleHandleA("kernel32.dll");
        BOOL (WINAPI *initSrw)(SRWLOCK *) =
            (BOOL (WINAPI *)(SRWLOCK *))
            (void *)GetProcAddress(k32, "InitializeSRWLock");
        void *(WINAPI *initCond)(CONDITION_VARIABLE *) =
            (void *(WINAPI *)(CONDITION_VARIABLE *))
            (void *)GetProcAddress(k32, "InitializeConditionVariable");
        if (initSrw) initSrw(S_MTXP(s)); else memset(S_MTXP(s), 0, 0x50);
        if (initCond) initCond(S_CNDP(s)); else memset(S_CNDP(s), 0, 8);
    }
    S_RUN(s) = 0;
    S_CIDP(s)[0] = cid;
    S_RAISED(s) = 0;
    S_DISC(s) = 0;
    S_REF(s) = 2;
    S_MBOXLEN(s) = 0;
    S_MBOXDATA(s) = NULL;
    S_T220(s) = 0;
    /* Phase-6: ring cursors are the SHADOW fields (NOT sock raw +0x00/
     * +0x04/+0x08, which stay zero for the exe's own ring semantics).
     * sock+0x30.. stays ZERO here: it is the 0x100 name record that
     * createFinish (vt+0x50) fills from the guest's handshake AND the
     * SyncThread cell that vt+0x58 resolves.                              */
    S_WPOS(s) = 0;
    S_WCNT(s) = 0;
    S_RPOS(s) = 0;
    S_QHEAD(s) = 0;
    s->sessionSerial++;
    /* Phase-11: enable the AOSP-P per-init renderControl reply chain
     * for this fresh session: rcGetRenderer → rcGetEGLVersion →
     * rcQueryEGLString → rcGetGLString → per-op default. Kept OFF
     * only for HPP-armed trials so the Phase-9 arm stays clean.     */
    s->initSeq = 0;
    s->initSeqOn = (S_HPP(s) == 0);
    s->rcHelloDone = false;
    s->synOpSent = false;
    s->exeOpPending = 0;
    s->rcOpServed = 0;
    s->rcReplyLen = 0;
    s->rcReplyPos = 0;
    s->rctl2714ServeCount = 0;
    s->rctl2714Active = false;
    s->rctl2714FbPos = 0;
    s->rctl2714PairOpen = false;
    s->rctl2714OutVal = 0;
    s->rctl2715Active = false;
    s->rctl2715PairOpen = false;
    s->rctl2715FbPos = 0;
    s->lastAllocOp = 0;
    s->rcReplyOp = 0;
}

/* exe doorbell REQUEST (hstPciWrite 0x1402ab270 REQ path):
 * sock->len(+0x210) = mbox len; sock->data(+0x218) = mbox +4; run=1;
 * cond broadcast.                                                      */
static void sock_req_prepare(GrSock *s, uint8_t *mb)
{
    S_MBOXLEN(s) = ldl_le_p(mb + 0x04);
    S_MBOXDATA(s) = mb + 0x04;
    S_RUN(s) = 1;
    WakeAllConditionVariable(S_CNDP(s));
}

/* ================================================================= */
/* wrapper vtable - HD-Player HstSocketHost::vftable @0x14096a4d0    */
/* (Phase-3 full decode; exe slot fns in parentheses)                 */
/*                                                                   */
/*   vt+0x00 hstRecv      (0x1402acde0 -> 0x1402ac070)               */
/*     (this, len, &out): out = ring + rpos; rpos += len             */
/*   vt+0x08 hstPeek4     (0x1402acdc0)                              */
/*     (this, needed, &out, timeout=-1): out = ring + rpos           */
/*   vt+0x10 hstPeek3     (0x1402acdd0) - passthrough to +0x08       */
/*   vt+0x18 hstAllocCopy (0x1402aa510) - alloc + memcpy (send-in)   */
/*   vt+0x20 hstRecvCopy  (0x1402acdf0 -> 0x1402ac200)               */
/*     (this, dst, len): memcpy dst <- ring+rpos; rpos += len        */
/*   vt+0x28 hstSend      (0x1402aa600 -> 0x1402aabc0)               */
/*     HOST->GUEST flush: mbox ret = queued bytes, status 0, ring    */
/*     send cursor reset; the exe blocks for doorbell REQ acks,      */
/*     our acks are async (GR_DONE mbox/IRQ path), never block.      */
/*   vt+0x30 hstAlloc     (0x1402aa500 -> 0x1402aa8c0)               */
/*     (this, len): returns ring + sendWpos; sendWpos += len         */
/*   vt+0x38 destroy      (0x1402aa5d0 -> 0x1402aaa90)               */
/*   vt+0x40 freeRecvBuf  (0x1402aa610)                              */
/*   vt+0x48 freeMoveSrc  (0x1402aa640)                              */
/*   vt+0x50 createFinish (0x1402aa6e0):                             */
/*     peek(8); hstRecvCopy([sock+0x30], 0x100); NegotiatePipeType   */
/*   vt+0x58 synResolve   (0x1402aa6d0):                             */
/*     return *(void**)(this+8) + 0x30 (the SyncThread cell; also    */
/*     the 0x100 name record filled by createFinish)                 */
/*   vt+0x60 stats        (0x1402aa670)                              */
/*   vt+0x68              (0x1402aa5e0): hppflag2 = 1                */
/*                                                                   */
/* Ring cursors in s->raw:  S_WPOS (+0x00) host send wpos,           */
/*   S_WCNT (+0x04) guest write count (doorbell bytes),              */
/*   S_RPOS (+0x08) host read rpos. Window = the device aperture.    */

typedef struct GrWrapper { const void *vtable; GrSock *sock; } GrWrapper;

/* SRWLock guards on the exe mutex region (raw+0x150 = S_MTXP)        */
#define S_LOCK(s)    AcquireSRWLockExclusive(S_MTXP(s))
#define S_UNLOCK(s)  ReleaseSRWLockExclusive(S_MTXP(s))

static int vt_peek4(void *w, uint32_t needed, void **out, int64_t timeout)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return -1;
    uint8_t *win = GR_WIN(s->cid);
    if (!win) return -1;
    /* exe hstPeekTimed semantics: with timeout == -1 the exe BLOCKS
     * until `needed` bytes are available in the ring. Our acks are
     * async doorbell records, so the exe's blocks (its own render
     * thread) must wait here with a bounded spin + sched_yield, or the
     * exe's decoder reads zeros and stalls (Phase-7: the exe's record
     * decode never REACHED a reply without this wait).               */
    int have = 0;
    unsigned avail = 0;
    for (int spin = 0; spin < (timeout < 0 ? 5000 : 50); spin++) {
        S_LOCK(s);
        uint32_t rpos = S_RPOS(s);
        if (rpos > S_WCNT(s)) rpos = S_WCNT(s);
        avail = (S_WCNT(s) > rpos) ? S_WCNT(s) - rpos : 0;
        S_UNLOCK(s);
        if (avail >= needed) { have = 1; break; }
        if (spin == 0) {
            gr_once(0x73, "hstPeek4 wait: avail=%u needed=%u (timeout=%d)",
                    avail, needed, (int)(timeout & 0x7fffffff));
        }
        Sleep(2);
    }
    S_LOCK(s);
    uint32_t rpos = S_RPOS(s);
    if (rpos > S_WCNT(s)) rpos = S_WCNT(s);
    avail = (S_WCNT(s) > rpos) ? S_WCNT(s) - rpos : 0;
    void *p = win + GR_WIN_RBASE + rpos;
    S_UNLOCK(s);
    if (out) *out = p;
    s->peekCount++;
    s->peekNeedSum += needed;
    gr_once(0x60, "hstPeek4 needed=%u avail=%u", needed, avail);
    return 0;
}

static int vt_peek3(void *w, uint32_t needed, void **out)
{
    return vt_peek4(w, needed, out, -1);
}

static int vt_recv(void *w, int32_t len, void **out)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return -1;
    uint8_t *win = GR_WIN(s->cid);
    if (!win) return -1;
    /* Phase-6 record region: the exe reads its record stream from
     * win[GR_WIN_RBASE + rpos); our OP_WRITE relocates the guest's
     * doorbell chunk there (continuous across doorbells).            */
    uint32_t n = len <= 0 ? 1 : (uint32_t)len;
    S_LOCK(s);
    uint32_t avail = (S_WCNT(s) > S_RPOS(s)) ? S_WCNT(s) - S_RPOS(s) : 0;
    /* Phase-9: DOORBELL-SCOPED framing (per-doorbell chunk, no
     * replay); the exe's rpos is reset per doorbell in OP_WRITE so a
     * full drain leaves got = 0 and the exe's waits resolve on the
     * next doorbell (matching the Phase-3/5 shape,>witnessed).        */
    uint32_t got = (n < avail) ? n : avail;
    uint32_t cur = S_RPOS(s);
    void *p = win + GR_WIN_RBASE + cur;
    S_RPOS(s) = cur + got;
    S_UNLOCK(s);
    if (out) *out = p;
    s->recvCount++;
    s->recvBytes += got;
    /* wrap discipline: the exe's per-record poll loop (len=1 got=0
     * between doorbells) floods the log — log real content every
     * time, silent polls only once per 64. Throttled since Phase-19
     * (the phase-18 boot's tail was ~90% got=0 poll lines).          */
    if (got > 0) {
        gr_log("hstRecv cid=%u len=%u got=%u rpos->%u (wc=%u)",
               s->cid, n, got, S_RPOS(s), S_WCNT(s));
        if (got > 1) {
            gr_dump("hstRecv", (uint8_t *)p, got);
        }
    } else if ((s->recvCount & 0x3f) == 0) {
        gr_log("hstRecv cid=%u len=%u got=0 (poll, wc=%u rpos=%u)",
               s->cid, n, S_WCNT(s), S_RPOS(s));
    }
    return 0;
}

static int vt_recvcopy(void *w, void *dst, int len)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s || !dst) return -1;
    uint8_t *win = GR_WIN(s->cid);
    if (!win) return -1;
    uint32_t n = len <= 0 ? 0 : (uint32_t)len;
    S_LOCK(s);
    /* createFinish's hstRecvCopy(0x100) is the exe's NAME record read:
     * serve it from the CLIENT VIEW (the sf handshake chunk the guest
     * flushed at win[0..]) rather than the record stream, matching the
     * exe's own "read the 0x100 announce buffer" semantics — and the
     * exe's stream offset must NOT advance for this name read.        */
    if (n > 0x100 && 0) n = 0x100;   /* (kept symbolic; n is caller-set) */
    if (S_WCNT(s) == 0) {
        /* no chunk yet: clients wait; serve zeros into the name area */
        memset(dst, 0, n);
        S_UNLOCK(s);
        gr_once(0x62, "hstRecvCopy len=%d (no chunk) zeros", len);
        return 0;
    }
    uint32_t avail = (S_WCNT(s) > S_RPOS(s)) ? S_WCNT(s) - S_RPOS(s) : 0;
    if (n > avail) n = avail;
    memcpy(dst, win + GR_WIN_RBASE + S_RPOS(s), n);
    S_RPOS(s) += n;
    S_UNLOCK(s);
    gr_once(0x62, "hstRecvCopy len=%d copied=%u", len, n);
    if (n > 1) {
        gr_dump("hstRecvCopy", (uint8_t *)dst, n);
    }
    return 0;
}

/* Phase-16: the exe's vt_alloc pre-fill builder — per-op ABI content
 * (self-contained; mirrors rcOpApply's D/S-class shapes) written into
 * the exe's alloc'd reply slot so the exe's bound impls read valid
 * buffers and their post-writes layer the rets on top.               */
static int grRcVer(void);
static void rcBuildReply(uint32_t op, uint32_t payLen, uint8_t *dst,
                         uint32_t maxLen, uint32_t *outLen)
{
    uint32_t n = 0;
    #define RST4(v) do { if (n + 4 <= maxLen) \
                         stl_le_p(dst + n, (uint32_t)(v)); n += 4; \
                     } while (0)

    if (outLen) *outLen = 0;
    if (op == 0 || maxLen == 0) return;

    if (op == 0x2712 || op == 0x2713) {      /* string class          */
        const char *str = (op == 0x2713)
            ? "OpenGL ES 2.0           "
            : "1.4 Android META-EGL";
        /* Phase-20 ROUND-WALK DECODE (NOTES §30): the guest enc-side
         * reply walk is NOT [u32 len][bytes] — the .so's rcGetGLString
         * /rcQueryEGLString reply shape is
         *   readFully(caller_buf, arg) ; readFully(&ret, 4)
         * (no length prefix; the caller's bufSize arg travels in the
         * record, disassembly fn 0x9340..0x9493 + both 0x2712/0x2713
         * tails = byte-identical shapes). her readFully consumes
         * min(bufSize, our staged). Phase-25: the class payload =
         * HER bsize arg (per-op, Phase-21/25 clamp [16, 0x400]) with
         * the trailing ret dword = strlen. The staged string is
         * NUL-term within payLen.                                       */
        if (payLen < 4) payLen = 4;
        if (payLen > 0x400) payLen = 0x400;
        if (n + payLen <= maxLen) {
            uint32_t i, slen = (uint32_t)strlen(str);
            if (slen > payLen - 1) slen = payLen - 1;
            memset(dst + n, 0, payLen);
            for (i = 0; i < slen; i++)
                dst[n + i] = (uint8_t)str[i];
            n += payLen;
        }
        RST4(payLen - 1);    /* trailing class ret = the strlen value  */
    } else {
        switch (op) {
        case 0x2710: RST4(grRcVer()); break;
        case 0x2711: RST4(1); RST4(4); RST4(1); break;
        case 0x2714: RST4(1); RST4(11); break;   /* {numConfigs=1, ret=11 (align exe)} */
        case 0x2715: RST4(0); RST4(0); break;   /* blob rets          */
        case 0x2716: RST4(1); break;
        case 0x2717: RST4(0x3200); break;
        case 0x2718: RST4(1); break;
        case 0x271A: RST4(2); break;
        case 0x271C: case 0x2729: case 0x2733: RST4(3); break;
        case 0x272A: RST4(4); break;
        case 0x272D: RST4(5); RST4(0); break;
        case 0x2736: RST4(1); RST4(1600); RST4(900); break;
        case 0x2739: RST4(1600); RST4(900); RST4(1); break;
        case 0x273A: RST4(1); RST4(1600); RST4(900); break;
        case 0x273B: RST4(0); RST4(0); RST4(0); RST4(1600); RST4(900);
                     RST4(0); break;
        case 0x2741: case 0x2745: case 0x274E: RST4(1); RST4(0); break;
        case 0x2735: RST4(0); break;
        default:     RST4(0); RST4(0); break;
        }
    }
    if (outLen) *outLen = n;
}

/* Phase-19: complete reply-chain byte total per op class — the sizes
 * her client's readFully classes bind to under the ret==ask contract.
 * The batch gate (OP_READ) holds the ack until the ring stages at
 * least the remaining class bytes so her refill doorbell gets the
 * WHOLE class content in a single serve. Classes staged complete:
 *   version = 4 bytes;   EGLVersion = 12 bytes {3,2,0};
 *   string  = 20 bytes ([u32 0][16B NUL-padded payload][ret] per the
 *             Phase-20 round-walk decode = [payload][ret], no
 *             len prefix; total = her 5-round 4B walk budget);
 *   configs (0x2715) = the exe's rcGetConfigs blob per ABI — exe-
 *             owned content (the exe's bound impl fills it; NO gr
 *             re-assert, per the Phase-17/18 discipline), only the
 *             staging gate applies.                              */
static uint32_t rcReplyClassTotal(uint32_t op)
{
    switch (op) {
    case 0x2710: return 4;      /* [u32 version]                  */
    case 0x2711: return 12;     /* {major, minor, ret} = {1,4,0}     */
    case 0x2712: return 68;     /* [64B NUL-terminated payload][ret] */
    case 0x2713: return 68;     /* [64B NUL-terminated payload][ret] */
    case 0x2714: return 8;      /* {numConfigs=1, ret=1} — gr-gate ON (phase-29) */
    default:     return 0;      /* exe-owned classes: no gr gate  */
    }
}

/* HOST->GUEST: hstAlloc hands out the ring send slot; the DLL writes
 * its output there; hstSend then publishes via the mbox record.     */
/* Phase-16: shared per-op reply builder (used by rcOpApply's staging
 * AND the exe's vt_alloc pre-fill). Forward decls.                  */
static void rcBuildReply(uint32_t op, uint32_t payLen, uint8_t *dst,
                         uint32_t maxLen, uint32_t *outLen);
static void rcStageStringReply(GrSock *s, const char *str,
                               const char *opName);
static void handlePostRoute(void);

static void *vt_alloc(void *w, uint32_t len)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return NULL;
    uint8_t *win = GR_WIN(s->cid);
    if (!win) return NULL;
    S_LOCK(s);
    /* Phase-46 (NOTES 55.8 stale-tail pitfall): a NEW op-class's prefill
     * landing while the previous class's reply was left partially
     * consumed (qhead != 0) would sit BEHIND the stale tail and her walk
     * would read the old class's bytes first (boot B: the 0x2711 tail
     * left qhead=8, so her 0x2714 prefill landed at reply+12 and her
     * numConfigs walk read both dwords 4 B early). Same-class chain
     * allocs stage with qhead==0 (batch gate: staging completes before
     * her first read of that class) and a fully drained class already
     * resets qhead/wpos at the serve site - so op-class change with
     * qhead!=0 can ONLY mean abandoned prior-class bytes: drop them and
     * stage the new class at reply+0 for deterministic walks.          */
    if (S_QHEAD(s) != 0 && s->exeOpPending != s->lastAllocOp &&
        !s->rctl2714PairOpen && !s->rctl2715PairOpen) {
        gr_log("hstAlloc ring-reset cid=%u op=0x%x (prevOp=0x%x) "
               "qhead=%u wpos=%u (%u stale B dropped)",
               s->cid, s->exeOpPending, s->lastAllocOp,
               S_QHEAD(s), S_WPOS(s), S_WPOS(s) - S_QHEAD(s));
        S_QHEAD(s) = 0;
        S_WPOS(s) = 0;
    }
    s->lastAllocOp = s->exeOpPending;
    uint32_t off = S_WPOS(s);
    if (off + len > GR_WIN_RSIZE) {
        gr_once(0x63, "hstAlloc wrap: off=%u len=%u (reset to 0)", off, len);
        off = 0;
    }
    S_WPOS(s) = off + len;
    /* Phase-24: string-class ring clamp — the exe allocs 50 B for its
     * own wire, but her DEC-side readFully(caller_buf, bufSize)
     * consumes min(bufSize, our staged): the class must DRAIN at the
     * walk's end (ret==ask on every round, ring reset at the class
     * tail). Publish len = the class total (68) for 0x2712/0x2713
     * only; the exe keeps writing its own bytes into the slot (they
     * extend past the published window, her reads never reach them).
     * Other classes (the 0x2715 blob etc.) stay exe-owned/unscaled. */
    /* Phase-24b: the publish-clamp is UNCONDITIONAL for the string
     * classes — the exe allocs 50 B (its own [len][string] wire,
     * leading `2e 00 00 00`: boot24A saw her rounds read `00 00 00
     * 00` ×n then `2e 00 00 00` = the exe's raw wire leaked when the
     * clamp required len >= 68). Publish class total (68) always; the
     * window [0..68) is repaired by the unconditional re-assert
     * before every serve. Other classes stay exe-owned/unscaled.    */
    /* Phase-25: the publish window = [bsize-payload + ret] = the
     * per-op string class (her bsize arg, clamped [16, 0x400]); the
     * window is repaired by the unconditional re-assert before every
     * serve, and the prefill writes the whole window.               */
    if (s->exeOpPending == 0x2712 || s->exeOpPending == 0x2713) {
        uint32_t cls = s->exeStrBuf;
        if (cls < 16) cls = 16;
        if (cls > 0x400) cls = 0x400;
        S_WPOS(s) = off + cls + 4;
    }
    /* Phase-43: the 0x2714 class is TWO ordered 4-B reads (out, then
     * ret) — publish the full 8-B window even if the exe's alloc was
     * short so the ring holds BOTH dwords before the drain.          */
    if (s->exeOpPending == 0x2714 && len < 8) {
        S_WPOS(s) = off + 8;
    }
    /* Phase-16: the exe's bound impls often leave the buffer empty or
     * write only the trailing ret — pre-fill our slot with the ABI-
     * correct content per the PENDING op (the exe reads valid
     * buffers, its post-writes layer on top).
     * Phase-19 (Task 2): this IS the chain-staging point — the
     * builders (rcBuildReply / rcStageStringReply via rcOpApply)
     * STAGE the COMPLETE reply chain per class here, at
     * exeOpPending set time + right after the exe's own hstAlloc,
     * so her single refill doorbell can be served ret==ask from a
     * whole-class ring (never fragmented per doorbell). The OP_READ
     * re-assert only REPAIRS the string class afterwards (the exe's
     * zero-GL clobber); the config class (0x2715+) stays exe-owned.
     * RACE check (S_LOCK discipline): the prefill + cursor advance
     * run inside S_LOCK(s); the exe's own bound-impl writes are raw
     * pointer writes outside our lock but land strictly inside its
     * alloc'd slot [off, off+len) and complete within the decode
     * callback — under the lock-step doorbell order her reads can
     * only observe them via OP_READ, whose serve memcpy (and the
     * string-class re-assert) run under S_LOCK again after the 2 s
     * staged-spin; the batch gate keeps our serve OFF the ring until
     * the class is staged. Her drained-then-reset ring (qhead==wpos
     * -> 0) is only reopened by the NEXT hstAlloc after she fully
     * consumed the chain, so a mid-consume alloc overlap cannot
     * re-enter her reading window.                                     */
    if (len && len <= 0x2000) {
        uint32_t built = 0;
        uint32_t cls = s->exeStrBuf;
        uint32_t maxLen = len;
        if (cls < 16) cls = 16;
        if (cls > 0x400) cls = 0x400;
        /* Phase-43: prefill must cover the published 8-B 0x2714 window */
        if (s->exeOpPending == 0x2714 && maxLen < 8) maxLen = 8;
        rcBuildReply(s->exeOpPending, cls,
                     win + GR_WIN_REPLY + off, maxLen, &built);
        if (built) {
            gr_log("exePrefill cid=%u op=0x%x len=%u built=%u",
                   s->cid, s->exeOpPending, len, built);
        }
    }
    S_UNLOCK(s);
    s->allocCount++;
    s->allocBytes += len;
    gr_log("hstAlloc cid=%u len=%u -> reply+%u (allocs=%u allocBytes=%u)",
           s->cid, len, off, s->allocCount, s->allocBytes);
    if (len > 1) {
        gr_dump("hstAlloc", win + GR_WIN_REPLY + off, len);
    }
    return win + GR_WIN_REPLY + off;
}

static int vt_alloccopy(void *w, void *src, int len)
{
    if (len <= 0 || !src) return -1;
    void *dst = vt_alloc(w, (uint32_t)len);
    if (!dst) return -1;
    memcpy(dst, src, (size_t)len);
    return 0;
}

/* HOST->GUEST flush (exe hstSend): mbox ret = pending bytes,
 * status 0, then raise the served-cid IRQ via our GR_DONE ack path. */
static int vt_send(void *w)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return -1;
    S_LOCK(s);
    uint32_t pending = S_WPOS(s);
    S_UNLOCK(s);
    uint8_t *mb = GR_MBOX(s->cid);
    if (mb) {
        mbox_set(mb, (int32_t)pending, 0);   /* ret = bytes served    */
    }
    s->sendCount++;
    s->sendBytes += pending;
    if (pending && pending <= 0x4000) {
        gr_dump("exeReply", GR_WIN(s->cid) + GR_WIN_REPLY, pending);
    }
    S_LOCK(s);
    S_WPOS(s) = 0;
    S_UNLOCK(s);
    gr_log("hstSend cid=%u pending=%u (sends=%u bytes=%u)",
           s->cid, pending, s->sendCount, s->sendBytes);
    GR_DONE(s->cid);
    return 0;
}

/* createFinish (exe 0x1402aa6e0): peek(8) + hstRecvCopy(name,0x100)
 * + NegotiatePipeType. GR_HPP=0 (default) keeps the simple ring path;
 * =1 arms the exe's HPP mode when the handshake parses.             */
static int vt_createfinish(void *w)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return -1;
    void *out = NULL;
    int rc = vt_peek4(w, 8, &out, -1);
    if (rc != 0) return rc;
    /* the exe's hstPeekTimed(timeout=-1) BLOCKS for the 8-byte header;
     * our caller-side vt_peek returns immediately, so wait here for the
     * guest's handshake flush (bounded 5 s) before reading the name.   */
    for (int spin = 0; spin < 500 && S_WCNT(s) < 8; spin++) Sleep(10);
    char *namebuf = (char *)s->raw + 0x30;   /* the exe's [sock+0x30]  */
    /* The exe reads the announce from the CLIENT VIEW (the guest's
     * flush-only chunk lives at win[0..]) — serve from win+0 without
     * touching our record-stream window cursors.                      */
    {
        uint8_t *win = GR_WIN(s->cid);
        memset(namebuf, 0, 0x100);
        if (win && S_WCNT(s) > 0) {
            memcpy(namebuf, win, S_WCNT(s) < 0x100 ? S_WCNT(s) : 0x100);
        }
        /* Phase-27: the exe's name read landed all-zeros on every
         * boot (the name post arrives after the wait-window). Fall
         * back to the phase-27 guest-name cache so the exe's own
         * strstr(" (pid ") peer path sees the REAL process name.   */
        if (!strstr(namebuf, " (pid ") && s->nameCached) {
            memcpy(namebuf, s->nameCopy, 0x100);
        }
        gr_dump("createfinish name", (uint8_t *)namebuf, 48);
    }
    const char *pidHdr = strstr(namebuf, " (pid ");
    int armHpp = 0;
    if (pidHdr) {
        int pid = atoi(pidHdr + 6);
        if (pid != 0) armHpp = 1;
    }
    char env[8];
    memset(env, 0, sizeof(env));
    DWORD envn = GetEnvironmentVariableA("GR_HPP", env, sizeof(env));
    bool wantHpp = true;                     /* default: arm on pid>0   */
    if (envn > 0 && env[0] == '0') wantHpp = false;
    if (armHpp) {
        gr_log("createFinish peer: %.40s", namebuf);
        if (wantHpp) {
            /* exe-NegotiatePipeType-faithful HPP trailing setup:
             * - [sock+0x0c] = 0x80000 (big staging cap)
             * - [sock+0x140] = ring+0x2000 (HPP slot window base;
             *   the exe pinned ring+0x2000, so the HPP record area is
             *   window bytes 0x2000.. with 0x1000-stride slots)
             * - [sock+0x148] = (ring-0x2000)/4KB
             * - malloc 0x80000 staging at [sock+0x18]
             * - [sock+0x14c]=1, [sock+0x14d]=1 (peer + spin)
             * - memset ring 0..0x2000; write the 0x55 PONG byte at
             *   win[0] (per client hppRecv: W[0]=lock, W[1]=own-state,
             *   W[2]=record-type, W[3]=peer-ready, W[4]/W[8]=lens)     */
            void *ring = S_WINDOW(s);
            if (ring) {
                S_HPP(s) = 1;
                S_HPP2(s) = 1;
                S_CAP(s) = 0x80000;
                void *big = calloc(1, 0x80000);
                /* exe field map (NegotiatePipeType 0x1402ace00):
                 *   +0x140 (S_HPPBUF) = ring + 0x2000 (HPP data window)
                 *   +0x148 (S_HPPCNT) = (ring-0x2000)/4K slot count
                 *   +0x18  (S_STAGE)  = malloc'd 0x80000 staging buf  */
                S_HPPBUF(s) = (char *)ring + 0x2000;
                S_HPPCNT(s) = (0x40000 - 0x2000) >> 12;
                S_STAGE(s) = big;
                memset(ring, 0, 0x2000);
                ((unsigned char *)ring)[0] = 0x55;   /* PONG at win[0] */
                gr_once(0x75, "HPP armed (pid>0): pong 0x55 at win[0], "
                        "data window ring+0x2000, staging+0x18, "
                        "flags +0x14c/14d=1");
            }
        } else {
            gr_once(0x76, "peer recognized but GR_HPP=0 (HPP kept off)");
        }
    }
    return 0;
}

/* vt+0x58: exe `return *(void**)(this+8) + 0x30;`                    */
static void *syn_resolve(void *w)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return NULL;
    gr_once(0x72, "syn resolver -> sock+0x30 (cell inside sock)");
    return s->raw + 0x30;
}

/* 0-return entries for the remaining slots (plain C ABI)             */
static int vt_stub_i(GrWrapper *w, int a)   { (void)w; (void)a; return 0; }
static void vt_stub_v(GrWrapper *w)         { (void)w; }
static bool vt_stub_b(GrWrapper *w)         { (void)w; return false; }

static void *g_vt[16];

static void vt_table_init(void)
{
    g_vt[0] = (void *)vt_recv;           /* vt+0x00 hstRecv           */
    g_vt[1] = (void *)vt_peek4;          /* vt+0x08 hstPeek4          */
    g_vt[2] = (void *)vt_peek3;          /* vt+0x10 hstPeek3 thunk    */
    g_vt[3] = (void *)vt_alloccopy;      /* vt+0x18 hstAllocCopy      */
    g_vt[4] = (void *)vt_recvcopy;       /* vt+0x20 hstRecvCopy       */
    g_vt[5] = (void *)vt_send;           /* vt+0x28 hstSend           */
    g_vt[6] = (void *)vt_alloc;          /* vt+0x30 hstAlloc          */
    g_vt[7] = (void *)vt_stub_i;         /* vt+0x38 destroy (no-op)   */
    g_vt[8] = (void *)vt_stub_v;         /* vt+0x40                   */
    g_vt[9] = (void *)vt_stub_v;         /* vt+0x48                   */
    g_vt[10] = (void *)vt_createfinish;  /* vt+0x50 createFinish      */
    g_vt[11] = (void *)syn_resolve;      /* vt+0x58 syn resolver      */
    g_vt[12] = (void *)vt_stub_v;        /* vt+0x60 stats             */
    g_vt[13] = (void *)vt_stub_i;        /* vt+0x68                   */
    g_vt[14] = (void *)vt_stub_b;
    g_vt[15] = (void *)vt_stub_i;
}

/* ================================================================= */
/* Phase-64 GR_PURE: a faithful HstSocketHost lane (dev/NOTES.md §9). */
/* Guest contract (libOpenglSystemCommon HstStream decode, NOTES §7):  */
/*   WRITE(len): records at guest window [0..len); READ(want): the     */
/*   guest copies `want` bytes from window+0, mbox ret = bytes served   */
/*   (> want allowed: the guest keeps the excess for its next read).   */
/* The DLL reads the guest's bytes in place and stages replies in a    */
/* private buffer; hstSend publishes them when the guest READ arrives. */
/* No synthetic replies, translation, prefill or reserves.            */

#define PURE_RBUF_CAP  (8u << 20)

static int gr_pure(void)
{
    static int cached = -1;
    if (cached < 0) {
        char buf[8] = {0};
        DWORD n = GetEnvironmentVariableA("GR_PURE", buf, sizeof(buf));
        cached = (n > 0 && buf[0] == '1') ? 1 : 0;
        gr_log("GR_PURE=%d", cached);
    }
    return cached;
}

static void pure_reset(GrSock *s)
{
    s->pWritePending = false;
    s->pWriteLen = 0;
    s->pReadPending = false;
    s->pReadWant = 0;
    s->pWpos = 0;
    s->pCarryLen = s->pCarryPos = 0;
}

static bool pure_ensure(uint8_t **buf, uint32_t *cap, uint32_t need)
{
    if (*cap >= need && *buf) return true;
    uint32_t nc = *cap ? *cap : 0x100000;
    while (nc < need) nc *= 2;
    uint8_t *nb = (uint8_t *)realloc(*buf, nc);
    if (!nb) return false;
    *buf = nb;
    *cap = nc;
    return true;
}

/* caller holds S_LOCK */
static void pure_ack_write_locked(GrSock *s)
{
    if (!s->pWritePending) return;
    uint8_t *mb = GR_MBOX(s->cid);
    s->pWritePending = false;
    if (mb) mbox_set(mb, (int32_t)s->pWriteLen, 0);
    s->pAcks++;
    GR_DONE(s->cid);
}

static void hexn(char *o, const uint8_t *p, unsigned n)
{
    unsigned i, k = 0;
    for (i = 0; i < n; i++) k += sprintf(o + k, "%02x ", p[i]);
    o[k] = 0;
}

/* Return a pointer to n contiguous stream bytes (carry first, then the
 * guest window), blocking for the guest's next WRITE chunk if needed.
 * advance: consume them. Reads that end a chunk are copied out before
 * the WRITE ack, because the guest may reuse its window right away.
 * timeoutMs < 0 = wait until disconnect. NULL = disconnect/timeout.  */
static uint8_t *pure_take(GrSock *s, uint32_t n, bool advance,
                          int64_t timeoutMs)
{
    uint8_t *win = GR_WIN(s->cid);
    int64_t waited = 0;
    static unsigned dbg;
    if (!win || n == 0) return NULL;
    if (dbg < 300) {
        dbg++;
        gr_log("ptake cid=%u n=%u adv=%d rpos=%u wcnt=%u carry=%u to=%lld",
               s->cid, n, advance, S_RPOS(s), S_WCNT(s),
               s->pCarryLen - s->pCarryPos, (long long)timeoutMs);
    }
    S_LOCK(s);
    for (;;) {
        uint32_t cav = s->pCarryLen - s->pCarryPos;
        uint32_t wav = S_WCNT(s) > S_RPOS(s) ? S_WCNT(s) - S_RPOS(s) : 0;
        if (cav == 0 && wav >= n) {
            uint8_t *p = win + S_RPOS(s);
            if (advance) {
                if (wav == n) {
                    if (!pure_ensure(&s->pOut, &s->pOutCap, n)) break;
                    memcpy(s->pOut, p, n);
                    p = s->pOut;
                    S_RPOS(s) = S_WCNT(s);
                    pure_ack_write_locked(s);
                } else {
                    S_RPOS(s) += n;
                }
            }
            S_UNLOCK(s);
            return p;
        }
        if (cav >= n) {
            uint8_t *p = s->pCarry + s->pCarryPos;
            if (advance) {
                if (!pure_ensure(&s->pOut, &s->pOutCap, n)) break;
                memcpy(s->pOut, p, n);
                p = s->pOut;
                s->pCarryPos += n;
                if (s->pCarryPos == s->pCarryLen) {
                    s->pCarryPos = s->pCarryLen = 0;
                }
            }
            S_UNLOCK(s);
            return p;
        }
        if (wav) {                       /* spanning: pull the chunk into carry */
            if (s->pCarryPos) {
                memmove(s->pCarry, s->pCarry + s->pCarryPos, cav);
                s->pCarryLen = cav;
                s->pCarryPos = 0;
            }
            if (!pure_ensure(&s->pCarry, &s->pCarryCap, cav + wav)) break;
            memcpy(s->pCarry + s->pCarryLen, win + S_RPOS(s), wav);
            s->pCarryLen += wav;
            S_RPOS(s) = S_WCNT(s);
            s->pSpans++;
            continue;
        }
        pure_ack_write_locked(s);        /* chunk drained: let the next come */
        if (S_DISC(s)) break;
        if (timeoutMs >= 0 && waited >= timeoutMs) break;
        S_UNLOCK(s);
        Sleep(1);
        waited++;
        S_LOCK(s);
    }
    S_UNLOCK(s);
    return NULL;
}

static int pure_flush(GrSock *s);

/* DLL RenderThread::main (0x18012b64c..849) calls peek(this, needed, &out)
 * with NO timeout argument (r9 is stale); ANY non-zero return ends the
 * render thread. HD-Player's hstPeekTimed (0x1402abde0) first flushes the
 * pending reply (0x1402aabc0), then waits for data. So: flush, then block. */
static int pvt_peek4(void *w, uint32_t needed, void **out, int64_t timeout)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    (void)timeout;
    if (!s) return 0x68;
    if (pure_flush(s) != 0) return 0x68;
    uint8_t *p = pure_take(s, needed ? needed : 1, false, -1);
    if (!p) return 0x68;                 /* only on disconnect */
    if (out) *out = p;
    return 0;
}

static int pvt_peek3(void *w, uint32_t needed, void **out)
{
    return pvt_peek4(w, needed, out, -1);
}

static int pvt_recv(void *w, int32_t len, void **out)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return -1;
    uint8_t *p = pure_take(s, len <= 0 ? 1 : (uint32_t)len, true, -1);
    if (out) *out = p;
    return p ? 0 : 0x68;                 /* only fails on disconnect */
}

static int pvt_recvcopy(void *w, void *dst, int len)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s || !dst || len <= 0) return -1;
    uint8_t *p = pure_take(s, (uint32_t)len, true, -1);
    if (!p) return 0x68;                 /* only fails on disconnect */
    memcpy(dst, p, (size_t)len);
    return 0;
}

static void *pvt_alloc(void *w, uint32_t len)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return NULL;
    S_LOCK(s);
    if (!s->pRbuf) s->pRbuf = (uint8_t *)calloc(1, PURE_RBUF_CAP);
    if (!s->pRbuf || s->pWpos + len > PURE_RBUF_CAP) {
        S_UNLOCK(s);
        gr_log("pure alloc FAIL cid=%u len=%u wpos=%u", s->cid, len, s->pWpos);
        return NULL;
    }
    uint8_t *p = s->pRbuf + s->pWpos;
    s->pWpos += len;
    S_UNLOCK(s);
    gr_log("palloc cid=%u len=%u -> wpos=%u", s->cid, len, s->pWpos);
    return p;
}

static int pvt_alloccopy(void *w, void *src, int len)
{
    if (len <= 0 || !src) return -1;
    void *dst = pvt_alloc(w, (uint32_t)len);
    if (!dst) return -1;
    memcpy(dst, src, (size_t)len);
    return 0;
}

static int pvt_send(void *w)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return 0x68;
    return pure_flush(s);
}

/* Deliver the staged reply (HD-Player flush 0x1402aabc0): ack the current
 * guest WRITE, wait for its READ, copy the reply to window+0, ret = bytes. */
static int pure_flush(GrSock *s)
{
    S_LOCK(s);
    uint32_t total = s->pWpos, pos = 0;
    if (total == 0) {                    /* nothing staged: no guest-visible event */
        S_UNLOCK(s);
        return 0;
    }
    pure_ack_write_locked(s);
    uint8_t *win = GR_WIN(s->cid);
    uint32_t wsz = (uint32_t)GR_WSZ(s->cid);
    /* HD-Player flush (0x1402aabc0): replies larger than the window go out
     * in window-sized chunks, one guest READ per chunk.                  */
    while (pos < total) {
        unsigned spins = 0;
        while (!s->pReadPending) {
            if (S_DISC(s)) {
                s->pWpos = 0;
                S_UNLOCK(s);
                return -1;
            }
            S_UNLOCK(s);
            Sleep(1);
            if (++spins == 3000) {
                gr_log("pure send cid=%u waiting >3 s for the guest READ "
                       "(staged %u B, sent %u)", s->cid, total, pos);
            }
            S_LOCK(s);
        }
        uint32_t n = total - pos;
        uint32_t want = s->pReadWant;
        if (n > wsz) n = wsz;
        if (win) memcpy(win, s->pRbuf + pos, n);
        pos += n;
        s->pReadPending = false;
        s->pSends++;
        {
            uint8_t *mb = GR_MBOX(s->cid);
            if (mb) mbox_set(mb, (int32_t)n, 0);
        }
        S_UNLOCK(s);
        if (total <= wsz || pos == n || pos == total) {
            char h[3 * 16 + 4];
            hexn(h, win ? win : (uint8_t *)"", n < 16 ? n : 16);
            gr_log("psend cid=%u want=%u served=%u (%u/%u): %s", s->cid, want,
                   n, pos, total, h);
        }
        GR_DONE(s->cid);
        S_LOCK(s);
    }
    s->pWpos = 0;
    S_UNLOCK(s);
    return 0;
}

static int pvt_createfinish(void *w)
{
    GrWrapper *ww = (GrWrapper *)w;
    GrSock *s = ww ? ww->sock : NULL;
    if (!s) return -1;
    char *namebuf = (char *)s->raw + 0x30;   /* the exe's [sock+0x30] */
    memset(namebuf, 0, 0x100);
    if (pvt_recvcopy(w, namebuf, 0x100) != 0) return -1;
    namebuf[0xff] = 0;
    {
        char proc[0x100] = {0};
        int pid = 0, tid = 0, a4 = 0, a5 = 0, a6 = 0;
        int k = sscanf(namebuf, "%255s (pid %d, tid %d) %d %d %d",
                       proc, &pid, &tid, &a4, &a5, &a6);
        gr_log("pure createFinish cid=%u '%.60s' parsed=%d hpp=%s", s->cid,
               namebuf, k, (k == 6 && a6 != 0) ? "REQUESTED(unsupported)" : "no");
    }
    return 0;
}

static void *g_vt_pure[16];

/* logging stubs for the slots the pure lane does not implement */
#define PSTUB(N) \
    static int64_t pstub_##N(void *a, int64_t b, int64_t c) \
    { (void)a; gr_once(0xa0 + N, "pure vt slot %d (+0x%x) called b=%lld c=%lld", \
                       N, N * 8, (long long)b, (long long)c); return 0; }
PSTUB(7) PSTUB(8) PSTUB(9) PSTUB(12) PSTUB(13) PSTUB(14) PSTUB(15)

static void vt_pure_table_init(void)
{
    memcpy(g_vt_pure, g_vt, sizeof(g_vt_pure));
    g_vt_pure[7] = (void *)pstub_7;
    g_vt_pure[8] = (void *)pstub_8;
    g_vt_pure[9] = (void *)pstub_9;
    g_vt_pure[12] = (void *)pstub_12;
    g_vt_pure[13] = (void *)pstub_13;
    g_vt_pure[14] = (void *)pstub_14;
    g_vt_pure[15] = (void *)pstub_15;
    g_vt_pure[0] = (void *)pvt_recv;
    g_vt_pure[1] = (void *)pvt_peek4;
    g_vt_pure[2] = (void *)pvt_peek3;
    g_vt_pure[3] = (void *)pvt_alloccopy;
    g_vt_pure[4] = (void *)pvt_recvcopy;
    g_vt_pure[5] = (void *)pvt_send;
    g_vt_pure[6] = (void *)pvt_alloc;
    g_vt_pure[10] = (void *)pvt_createfinish;
}

static void pure_write(GrSock *s, uint8_t *mb, uint32_t len)
{
    uint8_t *win = GR_WIN(s->cid);
    char h[3 * 16 + 4];
    S_LOCK(s);
    if (s->pWritePending) {
        gr_log("pure WRITE cid=%u while previous unacked (rpos=%u wcnt=%u)",
               s->cid, S_RPOS(s), S_WCNT(s));
    }
    S_WCNT(s) = len;
    S_RPOS(s) = 0;
    s->pWritePending = true;
    s->pWriteLen = len;
    s->pWrites++;
    S_UNLOCK(s);
    sock_req_prepare(s, mb);
    hexn(h, win ? win : (uint8_t *)"", len < 16 ? len : 16);
    gr_log("pwrite cid=%u len=%u: %s", s->cid, len, h);
}

static void pure_read(GrSock *s, uint32_t want)
{
    S_LOCK(s);
    s->pReadPending = true;
    s->pReadWant = want;
    s->pReads++;
    S_UNLOCK(s);
    gr_log("pread cid=%u want=%u", s->cid, want);
}

static void rcTryParse(GrSock *s, uint32_t relocBytes, uint8_t *win);
static int gr_echo_mode(void);   /* Phase-60 GR_ECHO_MODE knob */
static int gr_pairarm(void);     /* Phase-61 GR_PAIRARM knob   */
static int gr_windump(void);     /* Phase-62 GR_WINDUMP knob   */
static void post_install(void);   /* forward decl (exe readpix lane) */

/* ================================================================= */
/* Phase-10 renderControl responder (gr-side "stub reply-source")     */
/*                                                                   */
/* Wire format (matching AOSP P's renderControl protocol, verified   */
/* against the exe's own decode at 0x18019be80): each sf op record   */
/* = {u32 op_id = 0x2710..0x2750; u32 n_args; args...}; reply bytes  */
/* are exactly what the exe's own handler would emit through         */
/* our vt_alloc + vt_send path. Phase-3's observed exe reply bytes   */
/* (our `HEX hstAlloc n=4` 4-byte series + the 1024-byte {1,4,1}     */
/* rcGet pattern) seed the shapes.                                   */
/*                                                                   */
/* The per-op reply wire format (exe handler callgraph per Phase-3's */
/* exe dumps and the AOSP emugl source renderControl.enc/dec):       */
/*   rcGetRenderer        0x2710 args=0      → 4B  (renderer id = 2) */
/*   rcGetEGLVersion      0x2711 args=2      → 4B (EGL version      */
/*                            = {major=1, minor=4}; per Phase-3's    */
/*                            12-byte reply pattern bytes)           */
/*   rcQueryEGLString     0x2712 args=3      → 4B (bufsize)         */
/*   rcGetGLString        0x2713 args=3      → 4B+buf               */
/*   rcGetNumConfigs      0x2714 args=0      → 4B (config count=1)  */
/*   rcGetConfigs         0x2715 args=2      → 4B+buf               */
/*   rcChooseConfig       0x2716 args=5      → 4B (config id=0)     */
/*   rcGetFBParam         0x2717 args=2      → 4B (param value)     */
/*   rcCreateContext      0x2718 args=5(hdr/p/b/b/v) → 4B (handle)  */
/*   rcDestroyContext     0x2719 args=1      → 4B (ret 0)           */
/*   rcCreateWindowSurface 0x271A args=4    → 4B (handle)          */
/*   rcDestroyWindowSurface 0x271B args=2    → 4B (ret 0)           */
/*   rcCreateColorBuffer  0x271C args=5      → 4B (handle)          */
/*   rcOpenColorBuffer    0x271D args=1      → 0B (void)            */
/*   rcCloseColorBuffer   0x271E args=1      → 0B                   */
/*   rcSetWindowColorBuffer 0x271F args=3    → 0B                   */
/*   rcFlushWindowColorBuffer 0x2720 args=1  → 0B                   */
/*   rcMakeCurrent        0x2721 args=2      → 4B (ret 0)           */
/*   rcFBPost             0x2722 args=1      → 0B → **triggers our  */
/*                            BMP pixel pull via the exe's own      */
/*                            setPostCallback / getPixels route**   */
/*   rcFBSetWindowColorBuffer etc → 0B                          and  */
/*   rcSelectChecksummedProtocol 0x272A args=3 → 4B (ret 1)          */
/*                                                                   */
/* Per-op arg counts (packed from the exe's own op handlers per     */
/* Phase-5's rcGet op decode and renderControl_types.h):             */

/* Phase-12 authoritative op table, decoded from the guest's own
 * lib_renderControl_enc.so (numbering = numeral 0x2710 + idx; the
 * exe's renderControl_dec dispatcher 0x18019be80 uses the SAME range
 * 0x2710..0x2751: see NOTES §21)..op name list straight from the
 * .so's .rodata name blob ("rcGetRendererVersion"...),
 * so 0x2710 = rcGetRendererVersion (BST's first op), NOT the
 * AOSP's rcGetProtocolVersion.                                       */

#define RC_OP_GET_RENDERER_VERSION  0x2710
#define RC_OP_GET_EGL_VERSION       0x2711
#define RC_OP_QUERY_EGL_STRING      0x2712
#define RC_OP_GET_GL_STRING         0x2713
#define RC_OP_GET_NUM_CONFIGS       0x2714
#define RC_OP_GET_CONFIGS           0x2715
#define RC_OP_CHOOSE_CONFIG         0x2716
#define RC_OP_GET_FB_PARAM          0x2717
#define RC_OP_CREATE_CONTEXT        0x2718
#define RC_OP_DESTROY_CONTEXT       0x2719
#define RC_OP_CREATE_WINDOW_SURFACE 0x271A
#define RC_OP_DESTROY_WINDOW_SURFACE 0x271B
#define RC_OP_CREATE_COLOR_BUFFER   0x271C
#define RC_OP_OPEN_COLOR_BUFFER     0x271D
#define RC_OP_CLOSE_COLOR_BUFFER    0x271E
#define RC_OP_SET_WINDOW_COLOR_BUF  0x271F
#define RC_OP_FLUSH_WINDOW_COLOR_B  0x2720
#define RC_OP_MAKE_CURRENT          0x2721
#define RC_OP_FB_POST               0x2722
#define RC_OP_FB_SET_SWAP_INTERVAL  0x2723
#define RC_OP_BIND_TEXTURE          0x2724
#define RC_OP_BIND_RENDERBUFFER     0x2725
#define RC_OP_COLORBUFFER_FLUSH     0x2726
#define RC_OP_READ_COLOR_BUFFER     0x2727
#define RC_OP_UPDATE_COLOR_BUFFER   0x2728
#define RC_OP_OPEN_COLOR_BUFFER2    0x2729
#define RC_OP_CREATE_CLIENT_IMAGE   0x272A
#define RC_OP_DESTROY_CLIENT_IMAGE  0x272B
#define RC_OP_SELECT_CHECKSUM_HLP   0x272C
#define RC_OP_UPDATE_COLOR_BUFF_DMA 0x2732
#define RC_OP_WAIT_SYNC_KHR         0x2734
#define RC_OP_COMPOSE               0x2735
#define RC_OP_CREATE_DISPLAY        0x2736
#define RC_OP_DESTROY_DISPLAY       0x2737
#define RC_OP_SET_DISPLAY_COLOR_BUF 0x2738
#define RC_OP_GET_DISPLAY_COLOR_BUF 0x2739
#define RC_OP_GET_COLORBUF_DISPLAY  0x273A
#define RC_OP_GET_DISPLAY_POSE      0x273B
#define RC_OP_SET_DISPLAY_POSE      0x273C
#define RC_OP_SET_COLORBUF_VK_MODE  0x273D
#define RC_OP_CREATE_COLORBUF_HANDLE 0x2740
#define RC_OP_SET_DISPLAY_POSE_DPI  0x274F

/* Per-op arg counts per the .so enc fns' per-op layout (dword args
 * the encoder emits after the numeral; from the AOSP-derived
 * renderControl.enc layout the guest encodes):                        */
static int rcOpArgCount(uint32_t op)
{
    switch (op) {
    case 0x2710: return 0;                       /* rcGetRendererVer    */
    case 0x2711: return 2;                       /* outPtr major, minor */
    case 0x2712: return 3;                       /* name, buf, maxSize  */
    case 0x2713: return 3;                       /* name, buf, maxSize  */
    case 0x2714: return 0;                       /* (void)              */
    case 0x2715: return 1;                       /* bufSize             */
    case 0x2716: return 5;                       /* attrs + bits        */
    case 0x2717: return 2;                       /* param, val          */
    case 0x2718: return 5;                       /* ctx args            */
    case 0x2719: return 1;
    case 0x271A: return 4;
    case 0x271B: return 2;
    case 0x271C: return 5;
    case 0x271D: case 0x271E: case 0x271F: case 0x2720:
        return 1;
    case 0x2721: return 2;
    case 0x2722: return 1;                       /* rcFBPost(colorbuf)  */
    case 0x2723: return 1;
    case 0x2724: case 0x2725: return 1;
    case 0x2726: case 0x2727: case 0x2728: return 8;
    case 0x2729: return 4;
    case 0x272A: return 5;
    case 0x272B: return 1;
    case 0x272C: return 3;                       /* checksumHelper      */
    case 0x272D: case 0x272E: case 0x272F: return 5;
    case 0x2730: return 1;
    case 0x2731: return 5;                       /* SetPuid             */
    case 0x2732: return 9;                       /* UpdateColorBufferDMA*/
    case 0x2733: return 4;                       /* CreateColorBufferDMA*/
    case 0x2734: return 5;                       /* WaitSyncKHR         */
    case 0x2735: return 3;                       /* rcCompose           */
    case 0x2736: return 4;                       /* rcCreateDisplay     */
    case 0x2737: case 0x2738: case 0x2739: case 0x273A: return 1;
    case 0x273B: return 5;                       /* GetDisplayPose      */
    case 0x273C: return 6;                       /* SetDisplayPose      */
    case 0x273D: return 2;
    case 0x273E: return 9;                       /* ReadColorBufferYUV  */
    case 0x273F: return 1;                       /* IsSyncSignaled      */
    case 0x2740: return 5;                       /* ColorbufWithHandle  */
    case 0x2741: return 2;                       /* rcCreateBuffer      */
    case 0x2742: return 1;
    default: return -1;                          /* unknown             */
    }
}

static const char *rcOpName(uint32_t op)
{
    switch (op) {
    case 0x2710: return "rcGetRendererVersion";
    case 0x2711: return "rcGetEGLVersion";
    case 0x2712: return "rcQueryEGLString";
    case 0x2713: return "rcGetGLString";
    case 0x2714: return "rcGetNumConfigs";
    case 0x2715: return "rcGetConfigs";
    case 0x2716: return "rcChooseConfig";
    case 0x2717: return "rcGetFBParam";
    case 0x2718: return "rcCreateContext";
    case 0x2719: return "rcDestroyContext";
    case 0x271A: return "rcCreateWindowSurface";
    case 0x271B: return "rcDestroyWindowSurface";
    case 0x271C: return "rcCreateColorBuffer";
    case 0x271D: return "rcOpenColorBuffer";
    case 0x271E: return "rcCloseColorBuffer";
    case 0x271F: return "rcSetWindowColorBuffer";
    case 0x2720: return "rcFlushWindowColorBuffer";
    case 0x2721: return "rcMakeCurrent";
    case 0x2722: return "rcFBPost";
    case 0x2723: return "rcFBSetSwapInterval";
    case 0x2724: return "rcBindTexture";
    case 0x2725: return "rcBindRenderbuffer";
    case 0x2726: return "rcColorBufferCacheFlush";
    case 0x2727: return "rcReadColorBuffer";
    case 0x2728: return "rcUpdateColorBuffer";
    case 0x2729: return "rcOpenColorBuffer2";
    case 0x272A: return "rcCreateClientImage";
    case 0x272B: return "rcDestroyClientImage";
    case 0x272C: return "rcSelectChecksumHelper";
    case 0x272D: return "rcCreateSyncKHR";
    case 0x272E: return "rcClientWaitSyncKHR";
    case 0x272F: return "rcFlushWindowColorBufferAsync";
    case 0x2730: return "rcDestroySyncKHR";
    case 0x2731: return "rcSetPuid";
    case 0x2732: return "rcUpdateColorBufferDMA";
    case 0x2733: return "rcCreateColorBufferDMA";
    case 0x2734: return "rcWaitSyncKHR";
    case 0x2735: return "rcCompose";
    case 0x2736: return "rcCreateDisplay";
    case 0x2737: return "rcDestroyDisplay";
    case 0x2738: return "rcSetDisplayColorBuffer";
    case 0x2739: return "rcGetDisplayColorBuffer";
    case 0x273A: return "rcGetColorBufferDisplay";
    case 0x273B: return "rcGetDisplayPose";
    case 0x273C: return "rcSetDisplayPose";
    case 0x273D: return "rcSetColorBufferVulkanMode";
    case 0x273E: return "rcReadColorBufferYUV";
    case 0x273F: return "rcIsSyncSignaled";
    case 0x2740: return "rcCreateColorBufferWithHandle";
    case 0x2741: return "rcCreateBuffer";
    case 0x2742: return "rcCloseBuffer";
    case 0x2743: return "rcSetColorBufferVulkanMode2";
    case 0x2744: return "rcMapGpaToBufferHandle";
    case 0x2745: return "rcCreateBuffer2";
    case 0x2746: return "rcMapGpaToBufferHandle2";
    case 0x2747: return "rcFlushWindowColorBufferAsyncWithFrameNumber";
    case 0x2748: return "rcSetTracingForPuid";
    case 0x2749: return "rcMakeCurrentAsync";
    case 0x274A: return "rcComposeAsync";
    case 0x274B: return "rcDestroySyncKHRAsync";
    case 0x274C: return "rcComposeWithoutPost";
    case 0x274D: return "rcComposeAsyncWithoutPost";
    case 0x274E: return "rcCreateDisplayById";
    case 0x274F: return "rcSetDisplayPoseDpi";
    default: return "unknown";
    }
}

/* Stage a blob reply ([bufSize bytes][u32 ret]) — per the .so dec:
 * rcGetConfigs/rcReadColorBuffer read the blob DIRECTLY into the
 * caller's buffer (no length prefix) then a 4-byte scratch ret.     */
/* Phase-14: the renderControl version gate knob (GR_RCVER env):
 * the exe's dispatcher decodes v2 vs v3 differently; the .so's enc
 * may be a v3 build whose client rejects v2 axioms. Default 2.      */
static int grRcVer(void)
{
    static int cached = -2;             /* -2 = not yet probed        */
    if (cached == -2) {
        char buf[16] = "";
        DWORD n = GetEnvironmentVariableA("GR_RCVER", buf, sizeof(buf));
        cached = (n > 0 && n < sizeof(buf)) ? atoi(buf) : 2;
        if (cached < 1 || cached > 9) cached = 2;
        gr_log("rcGetRendererVersion gate reply = %d (GR_RCVER)", cached);
    }
    return cached;
}

static void rcStageBlobReply(GrSock *s, uint32_t blobLen, const char *opName)
{
    if (blobLen > sizeof(s->rcReplyBuf) - 4) {
        blobLen = sizeof(s->rcReplyBuf) - 4;
    }
    /* Phase-43: always room for the out dword (0x2715 out must be > 0) */
    if (blobLen < 4) blobLen = 4;
    memset(s->rcReplyBuf, 0, blobLen);
    /* Phase-43: blob[0] = configId 1 — the responder-lane blob carries no
     * exe rows, and the driver's post-0x18220 walk reads the first config
     * id (0x18254) — keep it a real id (matches 0x2716's placeholder).  */
    stl_le_p(s->rcReplyBuf, 1);
    /* Phase-33 (the driver-test decode): eglDisplay::initialize
     * @0x18220 checks `cmp eax, dword [r15+0x38]; jne fail` — the
     * rcGetConfigs reply's ret dword MUST EQUAL the rcGetNumConfigs
     * reply's ret (phase-43: the driver consumes ret=11 from either the
     * 0x2714 ring or the {11,11} fallback — scratch stays 11). Ret = 0
     * = the fail path at 0x18354 = eglInitialize returns 0.          */
    stl_le_p(s->rcReplyBuf + blobLen, 11);  /* scratch ret = 0x2714 ret (exe=11) */
    s->rcReplyLen = blobLen + 4;
    s->rcReplyPos = 0;
    gr_log("rcBlob reply cid=%u (%s) blob=%u", s->cid, opName, blobLen);
}

/* Stage a length-prefixed string reply ([u32 len][string bytes]):
 * the exe's per-op reply shape for rcQueryEGLString/rcGetGLString per
 * the exe's renderControl decode (reply buffer = [len][string]).    */
static void rcStageStringReplySize(GrSock *s, const char *str,
                                   const char *opName, uint32_t bufSize)
{
    /* Phase-25: the reply class = HER OWN bufSize (clamped [16, 0x400])
     * per her record's bsize dword; the wire = [bsize-byte NUL-
     * terminated payload][u32 ret]; ret = strlen = the class length
     * she decodes (per Phase-20's readFully(caller_buf, bufSize) +
     * readFully(&ret,4) walk). Her readFully consumes EXACTLY her
     * bsize (the read walk completes in ONE walk), the ring drains,
     * and each libEGL string slot receives non-NULL full content.  */
    if (bufSize < 16) bufSize = 16;
    if (bufSize > 0x400) bufSize = 0x400;
    uint32_t n = (uint32_t)strlen(str);
    if (n > bufSize - 1) n = bufSize - 1;
    memset(s->rcReplyBuf, 0, bufSize + 4);
    for (uint32_t i = 0; i < n; i++)
        s->rcReplyBuf[i] = (uint8_t)str[i];
    stl_le_p(s->rcReplyBuf + bufSize, n);   /* ret = the string len   */
    s->rcReplyLen = bufSize + 4;
    s->rcReplyPos = 0;
    gr_log("rcStr reply cid=%u (%s) class=%u pay=%u \"%s\"",
           s->cid, opName, bufSize, n, str);
}

static void rcStageStringReply(GrSock *s, const char *str, const char *opName)
{
    rcStageStringReplySize(s, str, opName, 64);
}

/* Render the batch-tile content (the >=256-class INIT BATCH reply: her
 * whole stage consumption walks THIS).
 * Phase-19 (batch-staging content completion): the tile is NO LONGER
 * the Phase-3 exe-era {1,4,1} repeat — its EGL major=1 cell is the
 * validated v3-axiom STOPPER (NOTES §24.3: with {1,4,1} she stops
 * reading after the first dword; phase-19_bootA's her-side 16B echo
 * ("Open"+{1},{1}) proved the {1,4,1} cells poison her stage walk).
 * The tile assembles the ABI-VALID per-class chain, tiled
 * (Phase-20 20-B string classes via rcBuildReply), 3 op classes in
 * per-op order:
 *   [version=3][12B {3,2,0}][16B EGL string][ret][16B GL string]
 *   [ret][{numConfigs=1},{0}] = 4+12+20+20+8 = 64 bytes per unit —
 * 64 divides 1024 exactly (16 whole units per batch), so her stage
 * cursor never lands mid-class at the batch tail.
 * via rcBuildReply (the same per-class builders the atom lane and the
 * exe pre-fill use — single content source). Applies when her early
 * reads advance past the version/EGL gates. rcGetConfigs (0x2715)
 * stays EXE-OWNED (her enc carries her own bufSize; the batch cannot
 * honest-stage that blob without a bogus size — Phase-15 rule).      */
static void rcServeHelloBytes(GrSock *s, uint32_t wantLen)
{
    /* one chain unit: build each class with rcBuildReply in ORDER     */
    static const uint32_t chainOps[] = {
        0x2710, 0x2711, 0x2712, 0x2713, 0x2714,
    };
    uint8_t  unit[128];
    uint32_t unitLen = 0;
    uint32_t n = wantLen;
    if (n > sizeof(s->rcReplyBuf)) n = sizeof(s->rcReplyBuf);
    for (unsigned i = 0; i < sizeof(chainOps) / sizeof(chainOps[0]); i++) {
        uint32_t built = 0;
        /* Phase-24: the tile keeps the COMPACT 16-B string payloads
         * (4+12+20+20+8 = 64-B unit → 16 whole units per 1024 batch,
         * her stage cursor divides the batch cleanly) — only the
         * per-op console atom/re-assert lane uses the 64-B payload. */
        rcBuildReply(chainOps[i], 16, unit + unitLen,
                     (uint32_t)sizeof(unit) - unitLen, &built);
        unitLen += built;
    }
    if (unitLen == 0) unitLen = 12;   /* degenerate guard */
    /* tile the ABI chain over the batch — exe's own tile pattern
     * (Phase-3's 1024-block protocol), ABI-valid content generation.  */
    for (uint32_t i = 0; i < n; i++) {
        s->rcReplyBuf[i] = unit[i % unitLen];
    }
    s->rcReplyLen = n;
    s->rcReplyPos = 0;
    gr_log("rcPtr helloreply cid=%u len=%u (gr-side staged)",
           s->cid, n);
}

/* Phase-13: the COMPLETE per-op reply ABI per the guest's own
 * lib_renderControl_enc.so dec side (each enc fn's post-header read
 * sequence; numeral + recordLen verified; NOTES §23):
 *   Dn   = n direct u32 reads (server reply = n dwords, no prefix)
 *   S    = [u32 len][len bytes] string/blob reply
 *   V    = void (no reply bytes)
 * exe's Phase-3 values seed the {1,4,1}-class content.             */
static void rcOpApply(uint32_t op, GrSock *s, const uint8_t *recBuf)
{
    uint8_t *rb = s->rcReplyBuf;
    uint32_t n = 0;      /* staged reply byte count                    */
    if (s) s->rcReplyOp = op;   /* Phase-42: track which op staged rcReplyBuf */
    #define ST4(v) do { stl_le_p(rb + n, (uint32_t)(v)); n += 4; } while (0)

    if (op < 0x2710 || op > 0x2750) {
        ST4(0);
        n = 4;
        return;
    }
    switch (op) {
    case 0x2710:                       /* rcGetRendererVersion: D1 */
        ST4(grRcVer());                /* v2 default, GR_RCVER knob */
        break;
    case 0x2711:                       /* rcGetEGLVersion: D3 {maj,min,ret} */
        /* Phase-33 (the driver-test decode): libEGL_emulation's
         * eglDisplay::initialize @0x18060 calls [enc+0x10]
         * (rcGetEGLVersion: &maj=+0x2c, &min=+0x30) and then does
         * `cmp eax, 1; jne 0x1832a(FAIL)` — THE REPLY'S ret DWORD
         * MUST BE 1 (the 0x1832a = the cleanup + return 0). Our
         * {1,4,0} = ret 0 = ALWAYS FAILS that check = the driver's
         * own eglInitialize returns 0 = her arm64-libEGL sees the
         * cbz-fail path = [0x3c] never increments = the phase-31
         * fatal. Then the driver forces *maj=1, *min=4 onto its
         * own record (0x18130/0x18136) and marches into
         * [enc+0x28] rcGetNumConfigs (ret must be > 0 = jle fail,
         * our {1,1} = pass) and [enc+0x30] rcGetConfigs.        */
        ST4(1); ST4(4); ST4(1);
        break;
    case 0x2712:                       /* rcQueryEGLString: S      */
        /* Phase-23 (NOTES §33): the emugl egl_display_t::initialize
         * queries the FOUR string classes THROUGH this op, and her
         * RenderEngine::create feeds the VERSION + EXTENSIONS replies
         * straight into GLExtensions::initWithEGLStrings (sf's own
         * strlen-NULL tombstone chain, phase-21). The reply string
         * MUST key off the record's name dword = the EGL string query
         * enum; the strings match the guest's own libEGL .rodata.    */
        {
            /* Phase-25: parse BOTH dwords — name (+8) AND bsize (+12)
             * per the Phase-20 .so record decode
             * {0x2712, 0x14, name, bufSize, bufSize}. */
            uint32_t name = recBuf ? ldl_le_p(recBuf + 8) : 0x3054;
            uint32_t bsize = recBuf ? ldl_le_p(recBuf + 12) : 0;
            const char *str;
            switch (name) {
            case 0x3053: str = "Android"; break;          /* EGL_VENDOR      */
            case 0x3055: str = "EGL_KHR_get_all_proc_addresses"
                                   " EGL_ANDROID_recordable";  /* EXTENSIONS */
                          break;
            case 0x308D: str = "OpenGL_ES"; break;        /* CLIENT_APIS     */
            default:     str = "1.4 Android META-EGL"; break; /* EGL_VERSION */
            }
            rcStageStringReplySize(s, str, rcOpName(op), bsize);
        }
        return;
    case 0x2713:                       /* rcGetGLString: S         */
        /* Phase-25: her emugl my_glGetString(0x1F00..0x1F03) posts
         * THIS op with the GL enum as name; ALSO per-op bsize
         * (record +12; the default 46 = the exe-frame synth). The exe
         * lane still writes its own real-host string first.          */
        {
            uint32_t name = recBuf ? ldl_le_p(recBuf + 8) : 0;
            uint32_t bsize = recBuf ? ldl_le_p(recBuf + 12) : 0x400;
            const char *str;
            switch (name) {
            case 0x1F00: str = "Android"; break;            /* GL_VENDOR      */
            case 0x1F01: str = "Android Emulator Vulkan-Emulation"; break;
            case 0x1F02: str = "OpenGL ES 3.2 Android Emulation 1.4";      break;
            /* Phase-26: 0x1F03 (GL_EXTENSIONS) = the emugl's own
             * token list — her libOpenglSystemCommon's
             * queryAndSetGLESMaxVersion / native-sync / dma probes
             * memchr+memcmp for these EXACT tokens in the string
             * (the .so .rodata 0x23260..0x233d0). Without them the
             * emugl's own session gate degrades to empty and the
             * libEGL slot stage goes NULL downstream.               */
            case 0x1F03: str = "ANDROID_EMU_native_sync_v4"
                                " ANDROID_EMU_native_sync_v3"
                                " ANDROID_EMU_native_sync_v2"
                                " ANDROID_EMU_dma_v1"
                                " ANDROID_EMU_gles_max_version_2"
                                " ANDROID_EMU_gles_max_version_3_0"
                                " ANDROID_EMU_gles_max_version_3_1"
                                " ANDROID_EMU_gles_max_version_3_2"
                                " ANDROID_EMU_direct_mem"
                                " ANDROID_EMU_vulkan"
                                " ANDROID_EMU_deferred_vulkan_commands"; break;
            default:     str = "OpenGL ES 2.0 (OpenGL ES 3.2 Vulkan-Emulation)";
            }
            rcStageStringReplySize(s, str, rcOpName(op), bsize);
        }
        return;
    case 0x2714:                       /* rcGetNumConfigs: D2      */
        /* Phase-29 (consume-site decode): the driver never posts this
         * op (eglChooseConfig goes STRAIGHT to rcChooseConfig via the
         * encoder's ctx+0x38 slot; verified @0x1b936..0x1b97f in
         * libEGL_emulation). The exe dispatcher class-walks it as the
         * AOSP-P priming lane; our honest pair = {numConfigs=1,
         * ret=1} — the exe bound reply {1,1} then overlays (last-
         * writer wins), which matches the exe shape exactly.       */
        ST4(1); ST4(11);               /* 1 config, ret=11 (align exe) */
        break;
    case 0x2715:                       /* rcGetConfigs: blob(bufSize)+ret */
        rcStageBlobReply(s,
            recBuf ? ldl_le_p(recBuf + 8) : 64, rcOpName(op));
        return;
    case 0x2727:                       /* rcReadColorBuffer: blob(x,y,w,h)+ret */
        rcStageBlobReply(s,
            recBuf ? ldl_le_p(recBuf + 8 + 4 * 8) : 64, rcOpName(op));
        return;
    case 0x2716:                       /* rcChooseConfig: D1       */
        /* Phase-29 (libEGL_emulation @0x1b936: eglChooseConfig walks
         * the attrib list — MUST terminate at 0x3038 (EGL_NONE) or
         * hit 0x3040 (EGL_CONFIG_ID); then rcChooseConfig enc =
         * [ctx+0x38](attribList, attribsSize=walked count (ebx@+8
         * stride), scratch = configsOut, configsSize) with reply =
         * [configId x configsSize][ret]; *numConfigs = the ret;
         * ret<0 = EGL_BAD... error; afterwards she resolves each id
         * via eglDisplay::getConfigAtIndex. So the honest reply =
         * [configId=1] + ret=1 — config 1 = our placeholder id.    */
        ST4(1); ST4(1);                /* configId 1, ret=1        */
        break;
    case 0x2717:                       /* rcGetFBParam: D1         */
        ST4(0x3200);                   /* param value (808x2)      */
        break;
    case 0x2718:                       /* rcCreateContext: D1      */
        ST4(1); break;                 /* ctx handle 1             */
    case 0x271A:                       /* rcCreateWindowSurface: D1 */
        ST4(2); break;                 /* wsurf handle 2           */
    case 0x271C:                       /* rcCreateColorBuffer: D1  */
        ST4(3); break;                 /* cbuf handle 3            */
    case 0x2729:                       /* rcOpenColorBuffer2: D1   */
        ST4(3); break;
    case 0x272A:                       /* rcCreateClientImage: D1  */
        ST4(4); break;                 /* image handle 4           */
    case 0x272D:                       /* rcCreateSyncKHR: D2      */
        ST4(5); ST4(0); break;         /* syncKhr handle 5, ret 0  */
    case 0x2733:                       /* rcCreateColorBufferDMA: D1 */
        ST4(3); break;
    case 0x2736:                       /* rcCreateDisplay: D3      */
        ST4(1); ST4(1600); ST4(900);   /* display 1, w, h          */
        break;
    case 0x2739:                       /* rcGetDisplayColorBuffer: D3 */
        ST4(1600); ST4(900); ST4(1); break;
    case 0x273A:                       /* rcGetColorBufferDisplay: D3 */
        ST4(1); ST4(1600); ST4(900); break;
    case 0x273B:                       /* rcGetDisplayPose: D6     */
        ST4(0); ST4(0); ST4(0); ST4(1600); ST4(900); ST4(0);
        /* {port, x, y, w, h, ret} — the 6th read = the scratch ret   */
        break;
    case 0x2741:                       /* rcCreateBuffer: D2       */
        ST4(6); ST4(0); break;         /* buffer handle 6          */
    case 0x2745:                       /* rcCreateBuffer2: D2      */
        ST4(6); ST4(0); break;
    case 0x274E:                       /* rcCreateDisplayById: D2  */
        ST4(1); ST4(0); break;
    case 0x2722:                       /* rcFBPost: V + pixel pull */
        post_install();
        s->rcReplyLen = 0; s->rcReplyPos = 0; s->rcOpServed++;
        gr_log("rctl op=0x%x (%s) reply=VOID + post_install", op,
               rcOpName(op));
        return;
    case 0x2735:                       /* rcCompose: D1 + pixel pull */
        ST4(0);
        post_install();
        break;
    /* --- direct-single-read int rets (D1) / void (V) groups ------- */
    case 0x2719: case 0x271B: case 0x271D: case 0x271F:          /* V */
    case 0x2723: case 0x2724: case 0x2725: case 0x272C: case 0x2731:
    case 0x2734: case 0x2740: case 0x2742: case 0x2747: case 0x2748:
    case 0x2749: case 0x274A: case 0x274B: case 0x274D:
        s->rcReplyLen = 0; s->rcReplyPos = 0; s->rcOpServed++;
        gr_log("rctl op=0x%x (%s) reply=VOID", op, rcOpName(op));
        return;
    case 0x271E:                       /* rcCloseColorBuffer: V    */
        s->rcReplyLen = 0; s->rcReplyPos = 0; s->rcOpServed++;
        gr_log("rctl op=0x%x (%s) reply=VOID", op, rcOpName(op));
        return;
    case 0x2720: case 0x2721: case 0x2726: case 0x2728:
    case 0x272B: case 0x272E: case 0x272F: case 0x2730: case 0x2732:
    default:                           /* fixed-D2 ret pads        */
        ST4(0); ST4(0);                /* {ret=0, pad=0}           */
        break;
    }
    if (n > sizeof(s->rcReplyBuf)) n = sizeof(s->rcReplyBuf);
    s->rcReplyLen = n;
    s->rcReplyPos = 0;
    s->rcOpServed++;
    if (op < 128 && !s->rcOpNamesLogged[op]) {
        s->rcOpNamesLogged[op] = true;
        gr_log("rctl reply op=0x%x (%s) replyBytes=%u",
               op, rcOpName(op), n);
    } else {
        gr_log("rctl reply op=0x%x (%s) replyBytes=%u",
               op, rcOpName(op), n);
    }
}

/* Examine the record-region chunk sf just wrote into win+rbase;
 * attempt to parse a renderControl op stream and stage replies.
 * Phase-12: the first dword is logged on EVERY record (throttled per
 * distinct value via a 64-slot hash) so the boot log shows exactly
 * when sf's numerals flip to exe-class (0x2710..0x2751).            */
static void rcTryParse(GrSock *s, uint32_t relocBytes, uint8_t *win)
{
    if (!win || relocBytes < 4) return;
    /* first dword = op candidate (this is the exe's framing per our
     * record lane's relocation semantics)                            */
    uint32_t op = ldl_le_p(win + GR_WIN_RBASE);
    if (op < 0x2710 || op > 0x2750) {
        /* per-value throttle: keys 0x60..0x9f hash the low 6 bits of
         * the dword plus the doorbell len class                      */
        gr_once(0x60 | ((op ^ relocBytes) & 0x1f),
                "rctl not-an-op: first dword=0x%x len=%u", op, relocBytes);
        /* Phase-12 low-numeral trial: sf's op records observed under
         * this boot carry SMALL lead dwords ({1}, {4}, {1,0,1,1}).
         * The .so name blob orders rcGetRendererVersion..rcSetDisplayPoseDpi
         * at indices 0..63 (= numerals 0x2710..0x274f); if sf's wire
         * uses the raw op INDEX, {1} = rcGetEGLVersion-class. Route
         * small dwords through the same responder with the index
         * mapped onto the numeral space so sf's decoder gets real
         * exe-shaped replies and her chain can advance.              */
        /* Phase-15: fdw==0 counts as op index 0 = the
         * rcGetRendererVersion-class gate record ({0,8} wants ONE
         * 4-byte reply per the .so decode): unblocks sf's 16B zero
         * record stall (Phase-14 §24.5).                                */
        if (op >= 0 && op <= 64) {
            /* Phase-25: update the exe-lane string-class bookkeeping
             * from her low-numeral records: [idx][arg] where idx 2 =
             * rcQueryEGLString, idx 3 = rcGetGLString), the arg
             * dword = her bufSize claim.                                */
            uint32_t arg = relocBytes >= 8 ? ldl_le_p(win + GR_WIN_RBASE + 4)
                                           : 0;
            if (op == 2) {
                s->exeOpPending = 0x2712;
                s->exeStrName = 0x3053;
                s->exeStrBuf = arg < 16 ? 16 : arg;
                if (s->exeStrBuf > 0x400) s->exeStrBuf = 0x400;
            } else if (op == 3) {
                /* Phase-26: the emugl's queryGLExtensions calls
                 * rcGetGLString FIRST with (0x1F03, buf, 0x400):
                 * the short [3][X] post = that call's short shape;
                 * the canonical class = 0x400.                        */
                s->exeOpPending = 0x2713;
                s->exeStrName = 0x1F03;
                s->exeStrBuf = 0x400;
            }
            rcOpApply(0x2710 + op, s, win + GR_WIN_RBASE);
        }
        return;
    }
    int args = rcOpArgCount(op);
    if (args < 0) {
        gr_once(0x79, "rctl unknown op=0x%x", op);
        return;
    }
    /* Phase-25: the exe-class numeral records carry name/bsize in
     * dwords 3/4 per the .so record shape {op, 0x14, name, bsize,
     * bsize} — bind them for the exe-lane reply class.               */
    if (relocBytes >= 20 && (op == 0x2712 || op == 0x2713)) {
        s->exeOpPending = op;
        s->exeStrName = ldl_le_p(win + GR_WIN_RBASE + 8);
        uint32_t b = ldl_le_p(win + GR_WIN_RBASE + 12);
        if (b < 16) b = 16;
        if (b > 0x400) b = 0x400;
        s->exeStrBuf = b;
    }
    /* Phase-59: RAW exe-class 0x2714 record — arm the numConfigs pair
     * HERE too. The sfIdx==4 machinery only runs for her small-index
     * {4} records, which never occur on this wire (her walk posts raw
     * 0x2714 — phase-58: 12 raw records, 0 index-4) — so the pair
     * never staged, her out/ret reads drained stale ring bytes, out/ret
     * came out garbage, bsize = out*(ret+1)*4 went 262144-class, and
     * the configs read retried forever (the phase-58 formation).
     * Stage {34,11} (the exe's own config-table rows, phase-52) at
     * reply+0 and reserve it for her TWO want=4 pair reads (the
     * serve-site reserve gate + batchWait skip); p48ret then fires
     * verbatim on the second read and the phase-48 commit stages the
     * 1636-B blob (ids 1..34 at 4-stride — the driver walk reads
     * blob[rbx*4] — + pad + ret=11 at [1632]). Re-arm unless the pair
     * is already INTACT (qhead==0 && wpos==8): a mid-pair dead attempt
     * must restage, an intact pair must survive a repeated record. */
    /* Phase-61 (dev/NOTES.md §3): GR_PAIRARM=0 disables this arm. Its
     * premise is false: the guest {4} records DO occur (59b dropped them);
     * once they pass, our sfIdx=4 translation writes a raw 0x2714 at
     * RBASE, this arm stages the pair at reply+0, and the exe's own
     * prefill then lands at reply+8 (wpos=16) - p48ret's wpos==8 gate
     * never matches and the 0x2715 commit never fires (p60B).       */
    if (op == 0x2714 && gr_pairarm() &&
        !(s->rctl2714PairOpen && S_QHEAD(s) == 0 && S_WPOS(s) == 8)) {
        if (s->rctl2715PairOpen || S_QHEAD(s) != 0 || S_WPOS(s) != 0) {
            /* a fresh pair supersedes a staged-but-unconsumed blob */
            s->rctl2715Active = false;
            s->rctl2715PairOpen = false;
        }
        S_QHEAD(s) = 0;
        S_WPOS(s) = 0;
        stl_le_p(win + GR_WIN_REPLY + 0, 34);
        stl_le_p(win + GR_WIN_REPLY + 4, 11);
        S_WPOS(s) = 8;
        s->rctl2714PairOpen = true;
        s->rctl2714Active = true;
        s->rctl2714FbPos = 0;
        stl_le_p(s->rctl2714Fb + 0, 11);
        stl_le_p(s->rctl2714Fb + 4, 11);
        s->rctl2714ServeCount = 0;
        s->rctl2714OutVal = 0;
        gr_log("rctl2714 armed cid=%u (raw record: pair {34,11} "
               "staged at reply+0, reserved for her out/ret reads)",
               s->cid);
    }
    rcOpApply(op, s, win + GR_WIN_RBASE);
}

static bool wndclass_registered;

static LRESULT CALLBACK gr_wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    (void)h; (void)m; (void)w; (void)l;
    return DefWindowProcW(h, m, w, l);
}

static HWND gr_make_window(int w, int h)
{
    static const wchar_t k_class[] = L"gr_renderHostWnd";
    if (!wndclass_registered) {
        WNDCLASSW wc;
        memset(&wc, 0, sizeof(wc));
        wc.lpfnWndProc = gr_wndproc;
        wc.hInstance = GetModuleHandleW(NULL);
        wc.lpszClassName = k_class;
        if (!RegisterClassW(&wc)) {
            gr_log("RegisterClassW failed err=%lu", (unsigned long)GetLastError());
            return NULL;
        }
        wndclass_registered = true;
    }
    RECT r = { 0, 0, w, h };
    AdjustWindowRect(&r, WS_OVERLAPPEDWINDOW, FALSE);
    HWND hwnd = CreateWindowExW(0, k_class, L"gr_render", WS_OVERLAPPEDWINDOW,
                                r.left, r.top, r.right - r.left,
                                r.bottom - r.top, NULL, NULL,
                                GetModuleHandleW(NULL), NULL);
    if (hwnd) ShowWindow(hwnd, SW_HIDE);
    return hwnd;
}

static HANDLE g_uiReady;

static DWORD WINAPI gr_ui_thread(LPVOID arg)
{
    MSG m;
    (void)arg;
    g.hwnd = gr_make_window(GR_W, GR_H);
    SetEvent(g_uiReady);
    while (GetMessageW(&m, NULL, 0, 0) > 0) {
        TranslateMessage(&m);
        DispatchMessageW(&m);
    }
    return 0;
}

/* [dll+0xb25670] post hook state */
typedef struct GrPostCtx { uint8_t *buf; unsigned posts; } GrPostCtx;
static GrPostCtx g_postctx;

/* exe callback hook slot used by the DLL's own readpixel thread:
 *   [dll+0xb25670] = post(void *dst, u32 bytes, u32 start, u32 end) set
 * by the host; [dll+0xb25678] = posted-frame counter.
 * PgaStartReadPixels(start,fps,w,h,dw,dh) starts the loop that pulls
 * frames and posts to this callback.                                  */
typedef int (*PgaStartReadPixels_t)(int start, int fps, int width,
                                    int height, int dstW, int dstH);
static PgaStartReadPixels_t dll_readpix;

static void gr_pixPost(void *dst, unsigned long long a2,
                       unsigned long long a3, unsigned long long a4)
{
    uint32_t bytes = (uint32_t)a2;
    if (!g.fbBuf || !dst) return;
    if (bytes > GR_FB_BYTES) bytes = GR_FB_BYTES;
    memcpy(g.fbBuf, dst, bytes);
    g.postOn = true;
    ++g_postctx.posts;
}

/* register our frame consumer + start the DLL's readpixel loop       */
static void post_install(void)
{
    dll_readpix = (PgaStartReadPixels_t)TAB(0x78);
    if (!dll_readpix) {
        gr_log("PgaStartReadPixels slot missing");
        return;
    }
    *(void **)((uint8_t *)g.dll + 0xb25670) = (void *)gr_pixPost;
    int rc = dll_readpix(1, 30, GR_W, GR_H, GR_W, GR_H);
    gr_once(0x31, "PgaStartReadPixels(1,30,%d,%d,%d,%d) -> %d, pixcb @ +0xb25670",
            GR_W, GR_H, GR_W, GR_H, rc);
}

static bool bmp_write(const char *path, const uint8_t *bg, int w, int h)
{
    uint32_t fsz = 54 + (uint32_t)w * h * 4;
    uint8_t hdr[54];
    memset(hdr, 0, sizeof(hdr));
    hdr[0] = 'B'; hdr[1] = 'M';
    stl_le_p(hdr + 2, fsz);
    stl_le_p(hdr + 10, 54);
    stl_le_p(hdr + 14, 40);
    stl_le_p(hdr + 18, w);
    stl_le_p(hdr + 22, -h);              /* top-down rows */
    stl_le_p(hdr + 26, 1);
    stl_le_p(hdr + 28, 32);
    FILE *f = fopen(path, "wb");
    if (!f) return false;
    fwrite(hdr, 1, sizeof(hdr), f);
    fwrite(bg, 1, (size_t)w * h * 4, f);
    fclose(f);
    return true;
}

static DWORD WINAPI readpix_engine(LPVOID arg)
{
    (void)arg;
    unsigned cnt = 0;
    uint64_t lastMs = 0;
    for (;;) {
        Sleep(2000);
        if (!g.ready) continue;
        bool newPosts = g.postOn && g_postctx.posts != g.lastPosts;
        if (!newPosts) {
            /* route 2: FB getPixels through table +0xa0 (Phase-1 proven
             * safe path; posts no crash if display not yet configured).
             * Throttle to ~every 5th sample so it stays cheap.         */
            if (++cnt % 5) continue;
            if (!g.rgba || !g.fbBuf) continue;
            int rc = g.rgba(0, g.fbBuf, GR_W * 4, 0x8888, NULL, 0);
            if (rc != 0) {
                gr_once(0x11, "getPixels route rc=%d (display 0)", rc);
                continue;
            }
        } else {
            g.lastPosts = g_postctx.posts;
        }
        bool changed = !g.fbHas || memcmp(g.fbBuf, g.fbPrev, GR_FB_BYTES) != 0;
        memcpy(g.fbPrev, g.fbBuf, GR_FB_BYTES);
        g.fbHas = true;
        if (!changed) continue;
        uint64_t now = (uint64_t)GetTickCount64();
        if (now - lastMs < 2000) continue;
        lastMs = now;
        if (bmp_write("screen_pga.bmp", g.fbBuf, GR_W, GR_H)) {
            ++g.bmps;
            gr_once(0x12, "frame BMP #%u written (engine, posts=%u)",
                    g.bmps, g_postctx.posts);
        }
    }
    return 0;
}

static void readpix_spawn(void)
{
    static bool spawned;
    if (!spawned) {
        spawned = true;
        CloseHandle(CreateThread(NULL, 0, readpix_engine, NULL, 0, NULL));
    }
}

/* ================================================================= */
/* VEH: capture violations from renderer-side threads                */

static LONG CALLBACK gr_veh(struct _EXCEPTION_POINTERS *ep)
{
    if (!ep || !ep->ExceptionRecord) return EXCEPTION_CONTINUE_SEARCH;
    DWORD code = ep->ExceptionRecord->ExceptionCode;
    if (code == 0xE06D7363 || code == 0x40010006 || code == 0x40000015 ||
        code == 0x0424C000) return EXCEPTION_CONTINUE_SEARCH;
    void *addr = ep->ExceptionRecord->ExceptionAddress;
    gr_once(0x2f, "first-chance 0x%x at %p (dll=%p dlloff=0x%x)",
            (unsigned)code, addr, (void *)g.dll,
            g.dll ? (unsigned)((uintptr_t)addr - (uintptr_t)g.dll) : 0);
    if (!g.failed) {
        g.failed = true;   /* degrade; worker becomes error replies */
        gr_log("DEGRADE: renderer-side hard fault code=0x%x", (unsigned)code);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

/* ================================================================= */
/* init                                                              */

static bool gr_init_dll_and_host(void)
{
    if (g.ready) return true;
    if (g.failed) return false;

    bool mode1 = gr_renderer_mode() == 1;

    HMODULE dll = LoadLibraryExA(GR_DLL_PATH, NULL,
                                 LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!dll) {
        gr_log("LoadLibraryExA FAILED err=%lu", (unsigned long)GetLastError());
        g.failed = true;
        return false;
    }
    g.dll = dll;
    gr_log("dll base=%p", (void *)dll);

    FARPROC gf = GetProcAddress(dll, "grApiInit");
    if (!gf) {
        gr_log("GetProcAddress(grApiInit) failed");
        g.failed = true;
        return false;
    }

    /* SyncThreadCmd producer record: exe "cell" object placeholder —
     * non-NULL registered entry; the producer fn is installed
     * per-connection in sock_prepare (mode-table row).               */
    g_cellEntry = calloc(1, 0x40);
    if (!g_cellEntry) { g.failed = true; return false; }

    /* Daily objA layout (exe FUN_1402b2d00): +0x00..+0x78 — the exe
     * fills slots +0x08..+0x78 via that registration fn, and the
     * grLibInit objA registration starts at +0x00 ( least common);
     * the DLL derefs objA[+0x08] when hour0x10 runs (decoded our
     * producer fix earlier in 11.3). Leave NO null slot: +0x00
     * gets the generic stub too (per the task-shape "non-NULL" rule). */
    memset(g.objA, 0, sizeof(g.objA));
    for (unsigned i = 0; i < 0x88 / 8; i++) {
        ((void **)g.objA)[i] = (void *)obj_stub_generic;
    }
    memset(g.objB, 0, sizeof(g.objB));
    for (unsigned i = 0; i < 0x58 / 8; i++) {
        ((void **)g.objB)[i] = (void *)obj_stub_generic;
    }
    g.logPair[0] = (void *)gr_logGate;
    g.logPair[1] = (void *)gr_logSink;

    g.table = (uint8_t *)calloc(1, 0xE0);
    if (!g.table) { g.failed = true; return false; }

    /* Phase-64: the render window lives on its own UI thread with a real
     * message loop. Created on the worker (which never pumps messages),
     * any SendMessage from the DLL's present/post path would block forever. */
    g_uiReady = CreateEventW(NULL, TRUE, FALSE, NULL);
    CloseHandle(CreateThread(NULL, 0, gr_ui_thread, NULL, 0, NULL));
    WaitForSingleObject(g_uiReady, 10000);
    if (!g.hwnd) {
        gr_log("CreateWindowExW failed err=%lu", (unsigned long)GetLastError());
        g.failed = true;
        return false;
    }
    gr_log("host HWND=%p (%dx%d) mode=%d", (void *)g.hwnd, GR_W, GR_H,
           mode1 ? 1 : 2);

    vt_table_init();
    vt_pure_table_init();

    gr_log("calling grApiInit...");
    int rc = ((grApiInit_t)(void *)gf)(g.table, g.objA, g.objB, g.logPair);
    gr_log("grApiInit -> %d", rc);
    if (rc != 0) { g.failed = true; return false; }

    /* VEH: log any access violations inside libOpenglRender threads    */
    AddVectoredExceptionHandler(1, gr_veh);

    if (!TAB(0x00)) {
        g.failed = true;
        gr_log("table[+0x00] missing");
        return false;
    }

    /* "vlcn" == mode 1 → setRenderer(1); "dx9" == mode 2 → setRenderer(7)
     * (PgaServerInit maps mode==2 ? 7 : 1 for us).                      */
    int rc2 = ((PgaServerInit_t)TAB(0x00))(g.hwnd, 0, 0,
                                           GR_W, GR_H,
                                           mode1 ? 1 : 2, 0, "plus");
    gr_log("PgaServerInit(1600x900, mode%s) -> %d", mode1 ? "1" : "2", rc2);
    if (rc2 != 0) { g.failed = true; return false; }

    post_install();          /* frame-post consumer (screen_pga.bmp)   */

    g.rgba = (fn_rgba_t)(void (*)(void))TAB(0xa0);
    g.fbBuf = (uint8_t *)calloc(1, GR_FB_BYTES);
    g.fbPrev = (uint8_t *)calloc(1, GR_FB_BYTES);

    g.ready = true;
    readpix_spawn();
    gr_log("READY (HandConn at +0x88)");
    return true;
}

/* ================================================================= */
/* worker dispatch (exe doorbell semantics + mbox/IRQ plumbing)      */

static void worker_dispatch_op(const GrOp *op)
{
    uint32_t cid = op->cid;
    unsigned cmd = op->cmd;
    uint32_t len = op->len;

    if (cid > GR_MAX_CID) return;
    GrSock *s = &g.socks[cid];
    uint8_t *mb = GR_MBOX(cid);
    if (!mb) return;

    if (!g.ready) {                    /* fail path: mbox status=0 etc. */
        mbox_set(mb, (cmd == OP_HYPERCALL) ? 0 : (int32_t)len, 0);
        GR_DONE(cid);
        return;
    }

    switch (cmd) {
    case OP_CONNECT: {
        int rc;
        /* sock+0x208 in the exe is a shared refcount (exe + DLL each
         * hold one); the OLD render thread may still be finishing its
         * protocol with the same object. NEVER free or re-calloc an
         * existing wrapper (Phase-3 wall: freed wrapper = stale deref
         * in the DLL's renderControl decoder, libOpenglRender
         * 0x180125e46 = rcGetEGLVersion bound-fn NULL-out crash).      */
        bool reuse = (s->wrapper != NULL);
        sock_prepare(s, cid);
        if (!reuse) {
            s->wrapper = calloc(1, 16);
            if (!s->wrapper) {
                mbox_set(mb, 0, 0);
                break;
            }
            ((GrWrapper *)s->wrapper)->vtable = gr_pure() ? g_vt_pure : g_vt;
            ((GrWrapper *)s->wrapper)->sock = s;
        }
        PgaHandConn_t hand = (PgaHandConn_t)TAB(0x88);
        rc = hand ? hand(s->wrapper) : -1;
        if (rc != 0) {
            gr_once(0, "HandConn FAIL cid=%u rc=%d "
                    "(failed to create renderer thread)", cid, rc);
            s->used = false;
            mbox_set(mb, 0, 0);
            break;
        }
        if (reuse) {
            gr_once(0x76, "reconnect reusing persistent wrapper cid=%u", cid);
        }
        ++g.connectOK;
        gr_log("connect cid=%u OK (mode %s)",
               cid, gr_renderer_mode() == 1 ? "1:vlcn" : "2:dx9");
        mbox_set(mb, 0, 0);
        break;
    }
    case OP_DISCONNECT:
        /* exe semantics (0x1402ab270 case 2): signal sock destruction
         * and clear the registry pointer only; the object itself is
         * refcounted and dies with the DLL's own close (vt+0x38).      */
        S_LOCK(s);
        S_DISC(s) = 1;
        S_UNLOCK(s);
        s->used = false;
        mbox_set(mb, 0, 0);
        break;
    case OP_WRITE:
        if (gr_pure()) {                 /* Phase-64: acked by the DLL's drain */
            pure_write(s, mb, len);
            return;
        }
        sock_req_prepare(s, mb);
        /* Phase-9: DOORBELL-SCOPED framing (the Phase-3/5 shape, now
         * intended): each sf flush chunk is its own exe-visible record
         * at win[GR_WIN_RBASE + 0 .. + len); the exe's rpos restarts at
         * 0 per doorbell so the exe's record decode always sees the
         * CURRENT chunk. The exe's own state machine (which reads from
         * its stream) then advances per record; this is the shape that
         * provably reached the exe's hstAlloc reply chain in Phase-3/5
         * (Phase-8 proved the exe parses record-region continuity, but
         * hstAlloc only fired there vs. here — with the doorbell scope
         * the exe's own Stream Buffer consumes the chunk prematurely
         * and the exe RE-reads; the exe's own code per the observed
         * Phase-5 logs then emits the connection-name echo reply via
         * hstAlloc).                                                 */
                S_LOCK(s);
                {
                    bool framed = false;   /* Phase-24: exeFrame synth set wcnt */
                    uint8_t *recdst = GR_WIN(cid);
            if (recdst) {
                /* Phase-12/14 guestwrite tap: the guest's flush chunk
                 * bytes VERBATIM (pre-relocation, pre-zero-fill) — the
                 * doorbell truth. First 8 records per session, then
                 * only NEW shapes (per-boot once).                       */
                {
                    unsigned i, n = len < 32 ? len : 32;
                    char hex[3 * 32 + 4];
                    unsigned o = 0;
                    uint32_t fdw = ldl_le_p(recdst);
                    for (i = 0; i < n; i++) {
                        unsigned char ch = recdst[i];
                        o += sprintf(hex + o, "%02x ", ch & 0xff);
                    }
                    hex[o] = 0;
                    gr_gw_serial = s->sessionSerial;   /* session tag   */
                    gr_gw_this++;                      /* per-boot count */
                    if (gr_gw_this <= 24 ||
                        ((fdw ^ len) & 0xffff) != gw_shape_seen[fdw & 31]) {
                        gw_shape_seen[fdw & 31] = (fdw ^ len) & 0xffff;
                        gr_log("guestwrite cid=%u off=%u n=%u first=0x%x: %s",
                               cid, (unsigned)0, len, fdw, hex);
                    }
                    /* Phase-62 window tap (GR_WINDUMP=1, unthrottled for
                     * the first 400 writes): the "index record" first
                     * dword equals the previous read reply (dev/NOTES §6),
                     * so the guest writes somewhere other than +0. Dump
                     * win[0..96) and the nonzero extent of the client
                     * region so the real record position is visible.     */
                    if (gr_windump() && gr_gw_this <= 400) {
                        char h2[3 * 96 + 4];
                        unsigned k, o2 = 0, hi = 0, lo = 0xffffffffu;
                        for (k = 0; k < 96; k++) {
                            o2 += sprintf(h2 + o2, "%02x ", recdst[k]);
                        }
                        h2[o2] = 0;
                        for (k = 0; k < GR_WIN_REPLY; k++) {
                            if (recdst[k]) { if (k < lo) lo = k; hi = k; }
                        }
                        gr_log("windump cid=%u len=%u nz=[%u..%u] w0: %s",
                               cid, len, lo == 0xffffffffu ? 0 : lo, hi, h2);
                        /* Phase-62b: per the HstStream decode the guest's
                         * record must be at ITS window+0 on every WRITE.
                         * Scan all small slot windows (record region
                         * excluded) for goldfish records {op 0x2710..0x2750,
                         * len 8..0x1000, 4-aligned} to find where they are. */
                        {
                            unsigned wi, hits = 0;
                            for (wi = 0; wi < 0x7f && hits < 6; wi++) {
                                uint8_t *w = GR_WIN(wi);
                                uint32_t off;
                                if (!w) continue;
                                for (off = 0; off + 16 <= GR_WIN_RECORD &&
                                     hits < 6; off += 4) {
                                    uint32_t op = ldl_le_p(w + off);
                                    uint32_t rl = ldl_le_p(w + off + 4);
                                    if (op >= 0x2710 && op <= 0x2750 &&
                                        rl >= 8 && rl <= 0x1000 &&
                                        (rl & 3) == 0) {
                                        gr_log("recscan wr-cid=%u len=%u "
                                               "hit win=%u off=0x%x op=0x%x "
                                               "rl=%u +8=%08x +12=%08x",
                                               cid, len, wi, off, op, rl,
                                               ldl_le_p(w + off + 8),
                                               ldl_le_p(w + off + 12));
                                        hits++;
                                    }
                                }
                            }
                            if (!hits)
                                gr_log("recscan wr-cid=%u len=%u: no goldfish "
                                       "record in any slot window", cid, len);
                        }
                    }
                    /* Phase-27: the guest's 276-B name record
                     * ("/system/bin/<proc> (pid N)") — the exe's
                     * createFinish name read has been observing all
                     * zeros because the name post lands after its
                     * wait-window; cache it per-cid so the exe's
                     * strstr(" (pid ") path gets the REAL peer name. */
                    if (len >= 12 && len <= 0x118 && recdst[0] == '/' &&
                        !s->nameCached) {
                        memset(s->nameCopy, 0, sizeof(s->nameCopy));
                        memcpy(s->nameCopy, recdst,
                               len < sizeof(s->nameCopy) ?
                               len : sizeof(s->nameCopy));
                        s->nameCached = true;
                        gr_log("name cached cid=%u len=%u (%.32s)",
                               cid, len, s->nameCopy);
                    }
                }
                /* Phase-57: drop reply-echo records from the record
                 * stream. Her reply-lane reads leak back into her own
                 * record writes (first dword = "ANDR"/"GL E"/spaces =
                 * the GLString bytes she just read): the exe's parser
                 * misreads the bogus length (`hstRecv len=1 got=1
                 * rpos->1`), its rpos goes 1 B off for the rest of the
                 * session, every later field parse shifts — the bsize
                 * magic 0x40000 = bytes +11..14 of a 0x2713 record =
                 * the want formation — and the init handshake never
                 * validates. The framing itself is byte-perfect (the
                 * 0x2713 read-back = {op, len=0x14, name=0x1f03,
                 * bsize@+12}; the 0x2714 = {op, len=0x0c, arg@+8}) —
                 * only the garbage writes must not enter the stream.
                 * KEEP: exe-class ops (0x2710..0x2751), the small
                 * index ABI (0..63, phase-12 routed), the name record
                 * ('/', [12,0x118] — the exe's createFinish read). */
                {
                    uint32_t fdw2 = ldl_le_p(recdst);
                    bool rec_ok = (fdw2 >= 0x2710 && fdw2 <= 0x2751) ||
                                  fdw2 <= 63 ||
                                  (len >= 12 && len <= 0x118 &&
                                   recdst[0] == '/');
                    /* Phase-59b: reply-echo records with a SMALL first
                     * dword slip through the phase-57 filter (0..63 is
                     * the valid small-index range). The fatal one is
                     * her ZERO echo (`00 x 16` = the NUL bytes she just
                     * read): it translates to a spurious sfIdx=0
                     * version frame whose hstAlloc RING-RESET kills her
                     * in-flight string class (proven onset chain, boot
                     * A: zero echo @100406859 -> exeFrame sfIdx=0 ->
                     * ring-reset qhead=28/wpos=1028 (1000 B dropped)
                     * -> batchWait ask=262144 -> the retry loop). Her
                     * REAL small-index records are EXACTLY 8 B
                     * ([idx][arg] — every healthy-52A exeFrame came
                     * from an n=8 write; 52A has ZERO sfIdx=0 frames).
                     * So: small-index records with len != 8 are echoes;
                     * an all-zero 8-B record is an echo too. */
                    /* Phase-60 (dev/NOTES.md §2): GR_ECHO_MODE < 2 turns
                     * this 59b drop OFF. Its premise ("real small-index
                     * records are exactly 8 B") is contradicted by 52A's
                     * own log: the healthy sfIdx=4 frames came from n=16
                     * `04 00 ..` writes, and on Root_p50 this rule drops
                     * the guest {4} record on every reconnect.               */
                    if (rec_ok && fdw2 <= 63 && gr_echo_mode() >= 2) {
                        uint32_t second = (len >= 8)
                                             ? ldl_le_p(recdst + 4) : 1;
                        if (len != 8 || (fdw2 == 0 && second == 0)) {
                            memset(recdst, 0, len);
                            s->relocCount++;
                            gr_log("recDropped cid=%u len=%u first=0x%x "
                                   "(small-index echo, real records "
                                   "are 8 B)",
                                   cid, len, fdw2);
                            goto write_done;
                        }
                    }
                    if (!rec_ok && gr_echo_mode() >= 1) {
                        memset(recdst, 0, len);
                        s->relocCount++;
                        gr_log("recDropped cid=%u len=%u first=0x%x "
                               "(reply-echo, not an op record)",
                               cid, len, fdw2);
                        goto write_done;   /* epilogue unlocks+acks ONCE */
                    }
                }
                memcpy(recdst + GR_WIN_RBASE, recdst, len);
                /* Phase-14 synthetic op stimulus: once our ABI chain
                 * reached seq>=2, feed the exe's record lane ONE real
                 * exe-class op record ({0x2711, 0x10, 4, 4} = the
                 * .so's rcGetEGLVersion wire record byte-exact) and
                 * see whether the exe's dispatch (rcGetEGLVersion @
                 * 0x18019bXXX) consumes it and fires hstAlloc.        */
                /* Phase-15: TRANSLATE EVERY indexed op record into
                 * exe framing: her numerals = the .so name-blob
                 * indexes (0..64); the exe's dispatcher wants
                 * [u32 op = 0x2710+idx][u32 recordLen][args...].
                 * Rewrite the relocated record in place per the exe's
                 * wire shape so the exe's own dec lane (hstAlloc /
                 * rcGetEGLVersion @0x18019bXXX) gets REAL exe-class
                 * op framing every time sf posts an index record.   */
                /* Phase-15/14: TRANSLATE indexed op records into exe
                 * framing PER CLASS (the .so name-blob indexes):
                 *   idx 0 → {0x2710, 8}                      (8B gate)
                 *   idx 1 → {0x2711, 16, 4, 4}               (EGL ver)
                 *   idx 3 → {0x2713, 20, 0x1F00, 46}         (GL string
                 *            with name=GL_VENDOR 0x1F00, maxSize=46;
                 *            the exe's dec reads sizes at +8/+0xc per
                 *            NOTES §13.5 — hstAlloc(len+4) then its
                 *            bound impl fills the reply).
                 * Other classes: translate NOTHING (her raw bytes =
                 * invalid ops to the exe = safely skipped; our own
                 * responder lane carries them).                        */
                 uint32_t sfIdx = ldl_le_p(recdst);
                /* Phase-43: only a COMPETING exe frame clears the 0x2714
                 * active window — a concurrent non-frame record (boot B's
                 * idx-34 rcUpdateColorBufferDMA posted BETWEEN the driver's
                 * out/ret reads) must not kill the ret fallback or the serve
                 * logging before her second read lands.                   */
                if (sfIdx <= 5 && sfIdx != 4) s->rctl2714Active = false;
                if (sfIdx == 0 || sfIdx == 1 || sfIdx == 2 ||
                    sfIdx == 3 || sfIdx == 4 || sfIdx == 5) {
                    uint8_t *rb = recdst + GR_WIN_RBASE;
                    uint32_t wcnt;
                    if (sfIdx == 0) {
                        stl_le_p(rb + 0, 0x2710);
                        stl_le_p(rb + 4, 8);
                        wcnt = 8;
                        s->exeOpPending = 0x2710;
                        s->exeStrName = 0;      /* not a string class    */
                        s->exeStrBuf = 0;
                    } else if (sfIdx == 1) {
                        stl_le_p(rb + 0, 0x2711);
                        stl_le_p(rb + 4, 16);
                        stl_le_p(rb + 8, 4);
                        stl_le_p(rb + 12, 4);
                        wcnt = 16;
                        s->exeOpPending = 0x2711;
                    } else if (sfIdx == 2) {
                        /* Phase-25: per-op bsize = her posted arg (the
                         * [2][X]-style low-numeral record — X = her
                         * own claim; clamp [16, 4096]).               */
                        uint32_t bsize = ldl_le_p(recdst + 4);
                        if (bsize < 16) bsize = 16;
                        if (bsize > 4096) bsize = 4096;
                        stl_le_p(rb + 0, 0x2712);
                        stl_le_p(rb + 4, 20);
                        stl_le_p(rb + 8, 0x3053);   /* EGL_VENDOR    */
                        stl_le_p(rb + 12, bsize);   /* bufSize       */
                        stl_le_p(rb + 16, bsize);
                        wcnt = 20;
                        s->exeOpPending = 0x2712;
                        s->exeStrName = 0x3053;
                        s->exeStrBuf = bsize;
                    } else if (sfIdx == 4) {
                        /* Phase-21: rcGetNumConfigs — the .so's fn
                         * 0x94a0 sends {0x2714, 0xc, 4} (the +8 dword
                         * is the CONSTANT 4) and walks read(out,4)+
                         * read(&ret,4).                                */
                        stl_le_p(rb + 0, 0x2714);
                        stl_le_p(rb + 4, 12);
                        stl_le_p(rb + 8, 4);
                        wcnt = 12;
                        s->exeOpPending = 0x2714;
                        if (s->rctl2715PairOpen && S_QHEAD(s) == 0) {
                            /* Phase-48: a fresh 0x2714 round supersedes the
                             * staged-but-unconsumed 0x2715 class (the prior
                             * attempt died before her config reads). */
                            s->rctl2715Active = false;
                            s->rctl2715PairOpen = false;
                            S_QHEAD(s) = 0;
                            S_WPOS(s) = 0;
                            gr_log("rctl2715 superseded cid=%u (new sfIdx=4)",
                                   s->cid);
                        }
                        /* Phase-52 (the phase-51 strand): a fresh 0x2714
                         * record with a DIRTY reply ring = abandoned bytes
                         * from prior classes (her flow is linear — the
                         * numConfigs record is posted only after the prior
                         * class was consumed). The alloc-time ring-reset
                         * ("op-class change with qhead!=0") is suppressed
                         * for this record because PairOpen is set below
                         * BEFORE the exe's hstAlloc runs, so in phase-51
                         * the pair staged at stale WPOS (observed: reply+24
                         * with qhead=8/wpos=32), the p48ret exact-state
                         * capture gates {q=0,w=8}/{q=4,w=8} never matched,
                         * the 0x2715 class never staged, and her
                         * rcGetConfigs readFully(48)+read(ret) blocked
                         * forever with no serve log -> instance killed ->
                         * restart loop (37/37 phase-51 rounds died right
                         * after the numConfigs pulls). Drop the abandoned
                         * bytes so THIS round stages at reply+0 and the
                         * existing capture/commit machine fires verbatim. */
                        if (S_QHEAD(s) != 0 || S_WPOS(s) != 0) {
                            gr_log("rctl2714 ring-align cid=%u q=%u w=%u "
                                   "-> 0 (phase-52 fresh record, %u stale B "
                                   "dropped)", s->cid, S_QHEAD(s),
                                   S_WPOS(s),
                                   S_WPOS(s) - S_QHEAD(s));
                            S_QHEAD(s) = 0;
                            S_WPOS(s) = 0;
                            s->rctl2714PairOpen = false;
                            s->rctl2714Active = false;
                            s->rctl2715PairOpen = false;
                            s->rctl2715Active = false;
                        }
                        s->rctl2714Active = true;   /* Phase-42: both reads (out+ret) */
                         s->rctl2714PairOpen = true; /* Phase-48: guard mid-pair */
                        /* Phase-43: stage the responder-lane ret fallback
                         * {ret=11, ret=11} BEFORE her walk starts: a
                         * concurrent record's read can drain the 8-B ring
                         * between her out and ret reads (phase-42 boot B:
                         * the idx-34 record interleaved, her ret read fell
                         * to the responder lane and got stale 00 00 00 00
                         * -> 0x181b6 test eax,eax; jle fail). Both dwords
                         * = 11 so ANY interleaving hands her a positive ret
                         * equal to the 0x2715 scratch (the 0x18220 cmp).  */
                        stl_le_p(s->rctl2714Fb + 0, 11);
                        stl_le_p(s->rctl2714Fb + 4, 11);
                        s->rctl2714FbPos = 0;
                        s->rctl2714ServeCount = 0;
                    } else if (sfIdx == 5) {
                        /* Phase-21: rcGetConfigs — the .so's fn
                         * 0x95c0 sends {0x2715, 0x10, bufSize,
                         * bufSize} and walks read(blob, bufSize into
                         * her buffer) + read(&ret,4). Her flushed
                         * record = [idx][X]: X = her bufSize claim;
                         * clamp to the honest [16, 4096] window (junk
                         * sizes never reachable).                     */
                        uint32_t bsize = ldl_le_p(recdst + 4);
                        if (bsize < 16)  bsize = 16;
                        if (bsize > 4096) bsize = 4096;
                        stl_le_p(rb + 0, 0x2715);
                        stl_le_p(rb + 4, 16);
                        stl_le_p(rb + 8, bsize);
                        stl_le_p(rb + 12, bsize);
                        wcnt = 16;
                        s->exeOpPending = 0x2715;
                        gr_log("exeFrame5 cid=%u claim=%u bsize=%u",
                               cid, ldl_le_p(recdst + 4), bsize);
                    } else {
                        /* Phase-26 (the emugl route decode): the emugl's
                         * HostConnection::queryGLExtensions
                         * (libOpenglSystemCommon 0x129e0) calls the
                         * encoder's vt+0x20 = rcGetGLString FIRST with
                         * (name = 0x1F03 GL_EXTENSIONS, buf, 0x400 =
                         * 1024) and caches the ret>0 string. The
                         * low-numeral [3][4] post = THIS call's short
                         * shape; the canonical FIRST-call buffer =
                         * 1024. Our synth record (exe framing)
                         * therefore mirrors the emugl's first call:
                         * name = 0x1F03, bufSize = 1024.                */
                        stl_le_p(rb + 0, 0x2713);
                        stl_le_p(rb + 4, 20);
                        stl_le_p(rb + 8, 0x1F03);   /* GL_EXTENSIONS */
                        stl_le_p(rb + 12, 0x400);   /* 1024 buffer    */
                        wcnt = 20;
                        s->exeOpPending = 0x2713;
                        s->exeStrName = 0x1F03;
                        s->exeStrBuf = 0x400;
                    }
                    S_WCNT(s) = wcnt;
                    framed = true;
                    gr_log("exeFrame cid=%u sfIdx=%u -> op-class frame "
                           "wcnt=%u", cid, sfIdx, wcnt);
                }
                if (s->initSeq >= 2 && !s->synOpSent) {
                    uint8_t *sb = recdst + GR_WIN_RBASE;
                    stl_le_p(sb + 0, 0x2711);
                    stl_le_p(sb + 4, 0x10);
                    stl_le_p(sb + 8, 4);
                    stl_le_p(sb + 12, 4);
                    S_WCNT(s) = 16;
                    S_RPOS(s) = 0;
                    s->synOpSent = true;
                    s->relocCount++;
                    s->relocBytes += 16;
                    gr_log("synOp cid=%u wrote rcGetEGLVersion record "
                           "{0x2711,16,4,4} in the exe record lane",
                           cid);
                }
                /* Phase-12: zero the client-view copy AFTER relocation:
                 * our own OP_READ reply-serve bytes share this window
                 * with sf's write lane; left in place they get
                 * relocated back into the exe's record lane as bogus
                 * records (the Phase-11 `{1}/{4}`-record artifact).    */
                memset(recdst, 0, len);
                /* Phase-24: do NOT let the raw chunk length clobber a
                 * synthetic exe-class frame: the exe's hstRecv(len=20)
                 * record-read reg `hstRecv len=20 got=8 (wc=8)` short
                 * read = the exe never received the full
                 * {0x2713,20,0x1F00,46} frame, its bound impl stalled
                 * and the string-slot stage stayed NULL. The synth
                 * wrote wcnt=20 — it must survive as the ring's count.  */
                if (!framed) S_WCNT(s) = len;
                S_RPOS(s) = 0;
                s->relocCount++;
                s->relocBytes += len;
                gr_log("recSent cid=%u off=0 len=%u (rpos=0 wcnt=%u "
                       "session=%u)",
                       cid, len, S_WCNT(s), s->sessionSerial);
                /* Phase-10: try to parse the record region as a
                 * renderControl op stream and stage replies              */
                if (len >= 4) {
                    rcTryParse(s, len, GR_WIN(cid));
                }
            }
        }
write_done:                            /* Phase-57: dropped-record exit */
        S_UNLOCK(s);
        stl_le_p(mb + 0x08, 0);
        break;
    case OP_READ: {
        if (gr_pure()) {                 /* Phase-64: completed by the DLL's hstSend */
            pure_read(s, len);
            return;
        }
        uint32_t wantLen, pending, served;
        bool batchLane;
        const char *serveSrc = "exe";   /* Phase-42: src of served bytes */
        uint32_t qhead_pre45 = 0;       /* Phase-45: ring cursor at serve */
        sock_req_prepare(s, mb);
        /* Client semantics (libOpenglSystemCommon hstRecv, Phase-4
         * decode of hst_client.elf): the ioctl 0x40087003 inout value
         * round-trips through mbox[+0x04] and its return = the SERVED
         * count; the client then stages win[0 .. served). The KERNEL
         * driver treats mbox[+4] == 0 as "host peer closed connection"
         * (REA_GUEST_MODULES 2.5) and returns -EINVAL, so the ack MUST
         * wait for renderer reply bytes (the exe's own flow blocks
         * here).
         *
         * Phase-19 BATCH-STAGING (Task 1, per the §28.1 hstRecv
         * decode): her refill ioctl asks the FULL remaining length
         * (0x18cee) and `ret == ask` BINDS; `ret > ask` = the instant
         * ~38 ms session abort (Phase-17 negative) and ret < ask =
         * the atom mode that never fills her batch (she replayed op
         * index 3 forever through Phase-18). So: BATCH-FIRST — hold
         * the ack inside this bounded spin (200 x 10 ms = the 2 s
         * per-op-class cap, well inside her connect ioctl's timeout
         * expectations) until the reply ring holds >= the ask for the
         * PENDING class, then ONE serve with ret == min(ask, pending).
         * Staged complete per class (rcReplyClassTotal):
         *   version  = 4 bytes;  EGLVersion = 12 bytes {3,2,0};
         *   string   = [u32 len][bytes] (46-byte GL string = 50);
         *   configs  = the exe's rcGetConfigs blob per ABI (exe-
         *              owned; only the staging gate applies here).
         * pending == 0 with no exe op pending = the gr RESPONDER lane
         * (init chain / hello batch, win[0]) — fall through at once,
         * no stall.                                                       */
        batchLane = (s->exeOpPending != 0);
        wantLen = ldl_le_p(mb + 0x04);
        for (int spin = 0; spin < 200; spin++) {
            S_LOCK(s);
            pending = (S_WPOS(s) > S_QHEAD(s)) ? S_WPOS(s) - S_QHEAD(s) : 0;
            S_UNLOCK(s);
            if (wantLen == 0) break;
            /* Phase-59: a window-class read must never wait on (nor
             * eat) the armed numConfigs pair — skip the staged-spin;
             * the serve site routes it to the responder lane with the
             * ring intact (uint32 field reads are atomic on x86). */
            if (wantLen > 8 && s->rctl2714PairOpen &&
                S_QHEAD(s) == 0 && S_WPOS(s) == 8) break;
            if (pending >= wantLen) break;          /* batch staged    */
            if (pending == 0 && !batchLane) break;  /* responder lane  */
            /* exe lane engaged but the ring is short of the ask: the
             * exe's decode (hstAlloc -> bound impl) is still staging
             * this class; do NOT ack short (S_LOCK is released while
             * sleeping — the exe's own writes stay lock-free but are
             * bounded to its alloc'd slot, never the serve region). */
            if (spin == 0) {
                gr_once(0x14, "batchWait cid=%u op=0x%x ask=%u pending=%u "
                        "(class total=%u; 2 s cap)",
                        cid, s->exeOpPending, wantLen, pending,
                        rcReplyClassTotal(s->exeOpPending));
            }
            Sleep(10);
        }
        S_LOCK(s);
        pending = (S_WPOS(s) > S_QHEAD(s)) ? S_WPOS(s) - S_QHEAD(s) : 0;
        /* Phase-19 BATCH-serve (Task 1): a single serve per doorbell.
         * Normal path = the gate resolved with pending >= ask so
         * served == ask (ret==ask binds, her whole class content in
         * one refill). On the 2 s cap expiry we serve the short
         * pending content (never > pending, so NEVER ret > ask — the
         * Phase-17 fatal over-serve stays impossible).                 */
        served = wantLen;
        if (served > pending) served = pending;
        if (served > 0x8000) served = 0x8000;
        /* Phase-59: RESERVE the armed pair {34,11} for her two
         * want=4 pair reads. Without this a window-class read (her
         * garbage-bsize configs read) would consume the 8-B ring
         * before her out/ret reads arrive and the pair would be lost
         * to stale bytes again. served=0 routes the read to the
         * responder lane; qhead stays 0, the ring stays intact. */
        if (wantLen > 8 && s->rctl2714PairOpen &&
            S_QHEAD(s) == 0 && S_WPOS(s) == 8) {
            served = 0;
        }
        /* Phase-17 re-assert (Task 1b): the exe's own bound impls
         * OVERWRITE our pre-fill with their own (empty GL) values —
         * e.g. rcGetGLString's impl wrote len=0 over our [46] (the
         * phase-15 "empty GL" analysis). Re-assert the ABI content
         * over the pending ring RIGHT BEFORE serve per the class:
         * only the string class needs our content; the exe's own
         * impls fill the rest (Phase-18 §28.3's guard). Phase-19:
         * also require the ring to actually hold the COMPLETE class
         * chain (rcReplyClassTotal) so the re-staged string reply is
         * never partial — the batch gate below serves ret==ask.       */
        if (s->exeOpPending && pending >= 4 && pending <= 0x2000) {
            uint32_t rebuilt = 0;
            /* Phase-18 (Task 2): re-assert discipline — only the
             * string classes need our content (the exe's own GL
             * string impl is a zero-content write). Phase-20: the
             * exe-side clobber is no longer only a len==0 dword —
             * boot20 caught a 1-BYTE clobber at reply+0 (her round 1
             * read `\0pen`) which made the old exeLen==0 gate FALSE
             * and let the clobber through. For OUR OWN string classes
             * the [16B payload][ret=1] wire IS the phase-20 ABI, so
             * the re-assert fires UNCONDITIONALLY for them (a partial
             * repair never happens: pending spans at most the 20-B
             * class under the vt_alloc clamp).                       */
            bool strClass = (s->exeOpPending == 0x2712 ||
                             s->exeOpPending == 0x2713);
            if (strClass && pending <= 0x400 &&
                (pending + S_QHEAD(s)) >=
                    (s->exeStrBuf > 0x400 ? 0x400 :
                     s->exeStrBuf < 16 ? 16 : s->exeStrBuf)) {
                rcBuildReply(s->exeOpPending,
                             s->exeStrBuf < 16 ? 16 :
                             s->exeStrBuf > 0x400 ? 0x400 : s->exeStrBuf,
                             GR_WIN(cid) + GR_WIN_REPLY, pending, &rebuilt);
                gr_log("reassert cid=%u op=0x%x (chain %u/%u bytes "
                       "staged)",
                       cid, s->exeOpPending, pending,
                       rcReplyClassTotal(s->exeOpPending));
            }
        }
        uint8_t *win = GR_WIN(cid);
        bool p48res = false;    /* Phase-48: reserve serve (ring-guard)  */
        if (served && win) {
            if (wantLen <= 4 && s->rctl2715Active &&
                s->rctl2715FbPos < 8 &&
                (S_WPOS(s) - S_QHEAD(s)) > 4) {
                /* Phase-48 ring-guard: the staged 0x2715 class holds
                 * pending>4, so a <=4 B ask is a CONCURRENT consumer —
                 * her own rcGetConfigs ret read is exactly the LAST 4 B
                 * (pending==4). Serve the {11,11} reserve instead of
                 * leaking staged config-id dwords into the other
                 * consumer's reply (the Phase-43 fb lane generalized). */
                uint32_t n = (wantLen < 8u - s->rctl2715FbPos)
                                 ? wantLen : (8u - s->rctl2715FbPos);
                serveSrc = "gr2715fb";
                qhead_pre45 = S_QHEAD(s);
                memcpy(win, s->rctl2715Fb + s->rctl2715FbPos, n);
                s->rctl2715FbPos += n;
                served = n;
                p48res = true;
                gr_log("rctl2715fb serve cid=%u wantLen=%u served=%u "
                       "(pos=%u/8)", cid, wantLen, served,
                       s->rctl2715FbPos);
            } else {
                /* exe reply bytes live in the REPLY region; serve the
                 * client by copying them into win[0..served) (the client's
                 * hstRecv stages win[0..served) immediately after the
                 * doorbell; this does not touch the record region).       */
                /* Phase-53 (task 2a): HARD-CLAMP the 0x2714 pair serve —
                 * whatever the exe overlay / reserve / stale classes
                 * contain, her out+ret reads receive EXACTLY the
                 * validated rows {34,11} (never drifted bytes such as a
                 * shifted 0x10000/0 parse). The clamp lands BEFORE the
                 * Phase-48 capture below, so the host's own out/ret
                 * capture and the 0x2715 commit see {34,11} too.       */
                if ((s->exeOpPending == 0x2714 || s->rctl2714PairOpen) &&
                    S_QHEAD(s) < 8 && S_WPOS(s) >= 8) {
                    uint8_t *pw = win + GR_WIN_REPLY;
                    uint32_t o0 = ldl_le_p(pw), o1 = ldl_le_p(pw + 4);
                    if (o0 != 34 || o1 != 11) {
                        stl_le_p(pw, 34);
                        stl_le_p(pw + 4, 11);
                        gr_log("rctl 0x2714 serve src=clamped (was "
                               "%08x %08x -> 00000022 0000000b) cid=%u",
                               o0, o1, cid);
                    }
                }
                serveSrc = "exe";
                qhead_pre45 = S_QHEAD(s);   /* Phase-45: pre-serve ring offset */
                memcpy(win, win + GR_WIN_REPLY + S_QHEAD(s), served);
            }
            if (!s->firstReplyLogged) {
                s->firstReplyLogged = true;
                unsigned i, n = served < 64 ? served : 64;
                char hex[3 * 64 + 4];
                unsigned o = 0;
                for (i = 0; i < n; i++) {
                    unsigned char ch = win[i];
                    o += sprintf(hex + o, "%02x ", ch & 0xff);
                }
                hex[o] = 0;
                gr_log("first reply dump (cid=%u served=%u): %s",
                       cid, served, hex);
            }
        }
        /* Phase-48: capture her out dword on the FIRST 0x2714-class ring
         * serve and detect her RET serve (the 2nd). Content-based gates:
         * an 8-B class staged at 0 with exactly one 4-B dword consumed IS
         * her rcGetNumConfigs pair — precise even when an interleaved
         * translated frame re-tags exeOpPending mid-pair (the boot-A
         * 6/12 miss), and immune to false hits on other classes (whose
         * ring sizes are 4/12/20+, never 8).                          */
        if (S_QHEAD(s) == 0 && S_WPOS(s) == 8 && served == 4 && win) {
            s->rctl2714OutVal = ldl_le_p(win);
        }
        bool p48ret = (S_QHEAD(s) == 4 && S_WPOS(s) == 8 &&
                       served == 4 && win);
        if (!p48res) S_QHEAD(s) += served;   /* Phase-48: reserve serves
                                              * must not consume the ring */
        if (S_QHEAD(s) == S_WPOS(s)) {
            /* ring drained: exe's next hstAlloc starts back at 0; the
             * exe holds in-flight pointers only within its own decode
             * callback, which already returned by this doorbell.       */
            S_WPOS(s) = 0;
            S_QHEAD(s) = 0;
            /* Phase-20: the op class is FULLY consumed (her walk
             * reached the class end under the ret==ask batches) —
             * clear the pending class so her NEXT read falls through
             * to the responder lane at once (no 2 s staged-spin on an
             * already-drained class; the next exeFrame re-sets the
             * pending class before the next alloc).                   */
            s->exeOpPending = 0;
        }
        if (p48ret) {
            /* Phase-48 THE COMMIT: her rcGetNumConfigs pair just completed
             * (out + ret served). Her driver's NEXT step (with the
             * Phase-47 checks bypassed) is rcGetConfigs: readFully
             * (bsize = out*(ret+1)*4) then read(&ret,4). Stage that class
             * into the reply ring NOW so her first ask breaks the batch
             * gate instantly (no 2 s cap, no heartbeat short-serve /
             * atom-mode), and arm the small-ask reserve so a concurrent
             * consumer's <=4 B reads never steal from it.                */
            uint32_t out = s->rctl2714OutVal;
            int32_t  retv = (int32_t)ldl_le_p(win);
            /* Phase-53 (task 2b): CLAMP instead of skip. A drifted pair
             * must never stall her flow: the old skip left 0x2715
             * unstaged -> her readFully blocked forever (the phase-51/52
             * strand). out/ret are clamped into the validated range and
             * bsize is capped at RC_CLASS_MAX (the real class 1636 B is
             * far below the cap; garbage such as {65536,0} -> 262144
             * becomes a 16 KB bounded class instead of a stall).       */
            if (out < 1 || out > 4096) {
                gr_log("rctl2715 clamp out %u -> %u (cid=%u)",
                       out, out < 1 ? 1u : 4096u, s->cid);
                out = (out < 1) ? 1u : 4096u;
            }
            if (retv < 0 || retv > 0x10000) {
                gr_log("rctl2715 clamp ret %d -> %d (cid=%u)",
                       retv, retv < 0 ? 0 : 0x10000, s->cid);
                retv = (retv < 0) ? 0 : 0x10000;
            }
            uint32_t bsize = out * (uint32_t)(retv + 1) * 4;
            if (bsize > RC_CLASS_MAX) {
                gr_log("rctl2715 clamp bsize %u -> %u (cid=%u)",
                       bsize, (unsigned)RC_CLASS_MAX, s->cid);
                bsize = RC_CLASS_MAX;
            }
            if (bsize < 8) bsize = 8;
            {
                uint8_t *d;
                uint32_t i, ids = (out < bsize / 4) ? out : (bsize / 4);
                if (S_QHEAD(s) != 0 || S_WPOS(s) != 0) {
                    gr_log("rctl2715 commit-drop stale cid=%u q=%u w=%u",
                           s->cid, S_QHEAD(s), S_WPOS(s));
                    S_QHEAD(s) = 0;
                    S_WPOS(s) = 0;
                }
                d = win + GR_WIN_REPLY + S_WPOS(s);
                memset(d, 0, bsize + 4);
                for (i = 0; i < ids; i++)
                    stl_le_p(d + i * 4, i + 1);
                stl_le_p(d + bsize, (uint32_t)retv);
                S_WPOS(s) = bsize + 4;
                s->exeOpPending = 0x2715;
                s->lastAllocOp = 0x2715;
                s->rctl2715Active = true;
                s->rctl2715PairOpen = true;
                /* Phase-59: the pair is CONSUMED — clear PairOpen or
                 * the phase-53 delivery clamp (PairOpen && qhead<8 &&
                 * wpos>=8) would rewrite the blob's first two dwords
                 * (ids 1,2) to {34,11} on her configs read. Active is
                 * cleared too: the configs ret read is covered by the
                 * 0x2715 reserve (rctl2715Active, alive until
                 * 'rctl2715 drained'), and leaving it set would let the
                 * 0x2714 reserve shadow post-init want==4 reads. */
                s->rctl2714PairOpen = false;
                s->rctl2714Active = false;
                s->rctl2715FbPos = 0;
                stl_le_p(s->rctl2715Fb + 0, 11);
                stl_le_p(s->rctl2715Fb + 4, 11);
                gr_log("exePrefill cid=%u op=0x2715 len=%u built=%u",
                       s->cid, bsize + 4, bsize + 4);
            }
        }
        if (s->rctl2715PairOpen && s->exeOpPending == 0 &&
            S_QHEAD(s) == S_WPOS(s)) {
            /* her 0x2715 class fully consumed (the drain block above
             * cleared exeOpPending after resetting the cursors) */
            s->rctl2715Active = false;
            s->rctl2715PairOpen = false;
            gr_log("rctl2715 drained cid=%u (her pair complete)", s->cid);
        }
        S_UNLOCK(s);
        /* ==== Phase-10: stub-level renderControl responder ========= */
        /* ==== Phase-11: full per-op rl-Init reply chain ============= */
        /* When sf is at the FIRST per-init read (per Phase-3's exe's own
         * per-record parse lane), our gr's AOSP-P renderControl full
         * per-op reply chain serves the exe's per-connection render
         * handshake (per Phase-5's exe own per-record framing chain):
         *   op #0: rcGetRenderer → reply 4B {renderer_id=2, "GL"}
         *   op #1: rcGetEGLVersion → reply {EGL_major, EGL_minor}
         *   op #2: rcQueryEGLString → reply length+string
         *   op #3..: exe's own per-op framed AOSP-P renderControl
         *   reply chain, matching the exe's Phase-5 per-record framing
         *   decode (our Op reply bytes = exe's Phase-3 {1,4,1}-style
         *   per-record reply lane)
         *                                                                 */
        if (served == 0) {
            if (wantLen >= 256 && wantLen <= 0x10000 && !s->rcHelloDone) {
                /* Phase-19: the >=256-class INIT BATCH (whole class,
                 * ONE doorbell). Per the §28.1 hstRecv decode, her
                 * FAST PATH consumes the staging ring doorbell-free
                 * once it holds >= ask (0x18bd9), and her first
                 * per-session read class is the 1024-byte
                 * renderControl handshake batch. Serving the init-Seq
                 * #0 atom there (want=1024, served=4) starved her
                 * fast path — she replayed the GL-string op (index 3)
                 * forever. So stage the exe's Phase-3 {1,4,1}-tiled
                 * whole-class batch (the exe's own 1024-byte reply
                 * tile that PROVABLY advanced her state in Phase-3)
                 * and serve ret == ask in one refill: her ring fills,
                 * her fast path takes the whole class doorbell-free.
                 * Then the per-op atom lane (init chain / exe-lane
                 * classes) drives her decoders as before.              */
                rcServeHelloBytes(s, wantLen < 1024 ? wantLen : 1024);
                s->rcHelloDone = true;
                served = (wantLen < s->rcReplyLen - s->rcReplyPos)
                             ? wantLen
                             : (s->rcReplyLen - s->rcReplyPos);
                if (win && served) {
                    serveSrc = "gr"; memcpy(win, s->rcReplyBuf + s->rcReplyPos, served);
                    s->rcReplyPos += served;
                }
                gr_log("rcBatch serve cid=%u wantLen=%u served=%u "
                       "(init batch, whole class, ret==ask)",
                       cid, wantLen, served);
                gr_once(0x15, "rcBatch: the >=256-class init batch "
                        "whole-class staged; her fast path is armed");
            /* Phase-59: the {11,11} reserves are served BEFORE the
             * rcInit step branch — her numConfigs out/ret reads and
             * the configs ret read are want==4; with the ring drained
             * they must get the reserve, never a zero step (ret=0 ->
             * 0x181b6 jle fail). The ring path (pending>0) still
             * serves first, so these only fire on a drained ring. */
            } else if (s->rctl2714Active && s->rctl2714FbPos < 8 &&
                       wantLen <= 4) {
                /* Phase-43: the 0x2714 ret fallback — her rcGetNumConfigs
                 * walk (enc @0x94a0 -> vt+0x28) is exactly read(out,4) then
                 * read(&ret,4); a concurrent record's read can drain the
                 * 8-B ring between the two reads, so the ret read lands
                 * here with stale rcReplyBuf bytes (phase-42 boot B: the
                 * idx-34 record interleaved; ret would be served
                 * 00 00 00 00 -> 0x181b6 jle fail -> no sfIdx=5). The
                 * fallback holds {11, 11} so ANY interleaving hands her a
                 * positive ret equal to the 0x2715 scratch (0x18220 cmp).
                 * Gated on wantLen<=4 so a large ask never eats it.     */
                served = (wantLen < 8 - s->rctl2714FbPos)
                             ? wantLen : (8 - s->rctl2714FbPos);
                if (win && served) {
                    serveSrc = "gr2714fb";
                    qhead_pre45 = S_QHEAD(s);
                    memcpy(win, s->rctl2714Fb + s->rctl2714FbPos, served);
                    s->rctl2714FbPos += served;
                }
                gr_log("rctl2714fb serve cid=%u wantLen=%u served=%u "
                       "(pos=%u/8)", cid, wantLen, served,
                       s->rctl2714FbPos);
            } else if (s->rctl2715Active && s->rctl2715FbPos < 8 &&
                       wantLen <= 4) {
                /* Phase-48: the 0x2715-class small-ask reserve — her
                 * rcGetConfigs walk is readFully(bsize) then read(&ret,4);
                 * while that class sits staged in the reply ring a
                 * concurrent consumer's <=4 B reads must NOT steal from it
                 * (the Phase-43 0x2714 reserve generalized). Reserve =
                 * {11,11}, the same positive placeholder.                */
                served = (wantLen < 8 - s->rctl2715FbPos)
                             ? wantLen : (8 - s->rctl2715FbPos);
                if (win && served) {
                    serveSrc = "gr2715fb";
                    qhead_pre45 = S_QHEAD(s);
                    memcpy(win, s->rctl2715Fb + s->rctl2715FbPos, served);
                    s->rctl2715FbPos += served;
                }
                gr_log("rctl2715fb serve cid=%u wantLen=%u served=%u "
                       "(pos=%u/8)", cid, wantLen, served,
                       s->rctl2715FbPos);
            } else if (s->initSeqOn && wantLen > 0) {
                if (s->rcReplyPos < s->rcReplyLen) {
                    /* Phase-12: a multi-read staged reply (e.g. a
                     * length-prefixed string) is still in flight:
                     * slide through it byte-wise; initSeq advances
                     * only when the staged reply is fully served.   */
                    served = (wantLen < s->rcReplyLen - s->rcReplyPos)
                                 ? wantLen
                                 : (s->rcReplyLen - s->rcReplyPos);
                    if (win && served) {
                        serveSrc = "gr"; memcpy(win, s->rcReplyBuf + s->rcReplyPos, served);
                        s->rcReplyPos += served;
                    }
                    /* Phase-53 bounded drain (task 2b read side): a
                     * pathological want (the drifted record-parse asks
                     * e.g. 262144) must not pin this lane for ~65k
                     * rounds — pad the serve to RC_CLASS_MAX so the
                     * guest's have<want accumulation finishes in ~16
                     * re-issues. Normal wants (<= RC_CLASS_MAX) serve
                     * unchanged; rcReplyPos advances only by the REAL
                     * bytes so the staged reply stays coherent.       */
                    if (win && wantLen > RC_CLASS_MAX &&
                        served > 0 && served < RC_CLASS_MAX) {
                        gr_log("rcInit bounded-drain cid=%u wantLen=%u "
                               "served %u->%u", cid, wantLen, served,
                               (unsigned)RC_CLASS_MAX);
                        memset(win + served, 0, RC_CLASS_MAX - served);
                        served = RC_CLASS_MAX;
                    }
                } else {
                    /* stage the next init-step's reply per the
                     * ABI via rcOpApply itself (op numerals in the
                     * .so's per-op order):
                     *   #0 rcGetRendererVersion → [u32 version] (D1)
                     *   #1 rcGetEGLVersion → {1,4,1} (D3)
                     *   #2 rcQueryEGLString → [len][string]
                     *   #3 rcGetGLString → [len][string]
                     *   #4 rcGetNumConfigs → {1,0} (D2)
                     *   #5+ default D2 {0,0}
                     *                                                  */
                    static const uint32_t chainOps[] = {
                        0x2710, 0x2711, 0x2712, 0x2713, 0x2714,
                    };
                    uint32_t chainOp = (s->initSeq <
                                        (uint32_t)(sizeof(chainOps) /
                                                   sizeof(chainOps[0])))
                                           ? chainOps[s->initSeq]
                                           : 0;
                    if (chainOp) {
                        rcOpApply(chainOp, s, NULL);
                        gr_log("rcInit #%u -> %s",
                               s->initSeq, rcOpName(chainOp));
                    } else {
                        s->rcReplyBuf[0] = 0; s->rcReplyBuf[1] = 0;
                        s->rcReplyBuf[2] = 0; s->rcReplyBuf[3] = 0;
                        s->rcReplyLen = 4; s->rcReplyPos = 0;
                        gr_log("rcInit reply #%u default", s->initSeq);
                    }
                    served = (wantLen < s->rcReplyLen) ? wantLen
                                                       : s->rcReplyLen;
                    if (win && served) {
                        serveSrc = "gr"; memcpy(win, s->rcReplyBuf + s->rcReplyPos, served);
                        s->rcReplyPos += served;
                    }
                    /* Phase-53 bounded drain (task 2b read side): see
                     * the continuation-branch note above — pad a
                     * pathological want to RC_CLASS_MAX per round so the
                     * init-stream loop drains in ~16 rounds instead of
                     * ~65536; normal wants serve unchanged.          */
                    if (win && wantLen > RC_CLASS_MAX &&
                        served > 0 && served < RC_CLASS_MAX) {
                        gr_log("rcInit bounded-drain cid=%u wantLen=%u "
                               "served %u->%u", cid, wantLen, served,
                               (unsigned)RC_CLASS_MAX);
                        memset(win + served, 0, RC_CLASS_MAX - served);
                        served = RC_CLASS_MAX;
                    }
                    if (s->rcReplyPos >= s->rcReplyLen) {
                        s->initSeq++;
                    }
                    gr_log("rcInit serve cid=%u wantLen=%u served=%u "
                           "(pos=%u/%u seq=%u)",
                           cid, wantLen, served,
                           s->rcReplyPos, s->rcReplyLen, s->initSeq);
                }
            } else if (!s->rcHelloDone && s->rcOpServed == 0) {
                rcServeHelloBytes(s, wantLen < 1024 ? wantLen : 1024);
                s->rcHelloDone = true;
                served = (wantLen < s->rcReplyLen) ? wantLen
                                                   : s->rcReplyLen;
                if (win && served) {
                    serveSrc = "gr"; memcpy(win, s->rcReplyBuf + s->rcReplyPos, served);
                    s->rcReplyPos += served;
                }
                gr_log("rcHello serve cid=%u wantLen=%u served=%u",
                       cid, wantLen, served);
            } else if (s->rcReplyLen > s->rcReplyPos) {
                served = (wantLen < s->rcReplyLen - s->rcReplyPos)
                             ? wantLen
                             : (s->rcReplyLen - s->rcReplyPos);
                if (win && served) {
                    serveSrc = "gr"; memcpy(win, s->rcReplyBuf + s->rcReplyPos, served);
                    s->rcReplyPos += served;
                }
                gr_log("rcReply serve cid=%u wantLen=%u served=%u",
                       cid, wantLen, served);
            } else {
                served = 1;   /* heartbeat byte                    */
            }
        }
        if (served == 0) served = 1;
        /* Phase-58: RET==ASK BINDS (the phase-19 contract: her refill
         * ioctl asks the FULL length; ret>ask = instant abort, ret<ask
         * = the atom mode that restarts the read forever). Her big
         * class ask is the window class (wantLen = min(remaining,
         * [hst+0x180]=0x40000) = 262144 — the guest-derived want of
         * phase-57); we served 16384 (bounded drain) => ret<ask =>
         * 195k atom-mode restarts and a frozen walk. Report
         * served==ask for window-class asks so her read binds and
         * completes in ONE round: zero-fill the CLIENT region tail we
         * own (win[served..GR_WIN_REPLY)); the reply ring
         * (0x10000..0x30000) and record region (0x30000..) are NOT
         * touched — she may read them as reply tail, which is safe:
         * struct reads are <= 0x8000 (served==ask already) and the
         * string classes NUL-terminate before the ring. ret is set to
         * EXACTLY wantLen (never > ask, so the phase-17 fatal over-
         * serve stays impossible). */
        if (wantLen > served && wantLen <= 0x40000 && win) {
            uint32_t zf = (wantLen < GR_WIN_REPLY) ? wantLen
                                                   : GR_WIN_REPLY;
            if (zf > served) memset(win + served, 0, zf - served);
            gr_log("serve-bind cid=%u want=%u served %u->%u "
                   "(ret==ask, window class)", cid, wantLen,
                   served, wantLen);
            served = wantLen;
        }
        mbox_set(mb, (int32_t)served, 0);
        g.readsServed += served;
        /* Phase-46: log EVERY read (the old readsServed%8 gate hid
         * reads - absence of a line was NOT absence of a read).      */
        gr_log("hstRead req=%u served=%u (qpre=%u q=%u wpos=%u) readsServed=%u",
               wantLen, served, qhead_pre45, S_QHEAD(s), S_WPOS(s),
               g.readsServed);
        gr_dump("reply", win, served);
        /* Phase-17 per-byte serve trace: every byte our OP_READ lane
         * delivered, tagged with the semantics of the source (exe|
         * chains|staged) and the pending op — the byte-diff vs the
         * .so's per-op readFully contract. Throttled: first 48 serves
         * per boot show the full hex (24B), then only every 16th.     */
        {
            static unsigned serve_seq;
            unsigned ss = ++serve_seq;
            if (served && ss <= 48) {
                unsigned i, n = served < 24 ? served : 24;
                char hex[3 * 24 + 4];
                unsigned o = 0;
                for (i = 0; i < n; i++) {
                    unsigned char ch = win[i];
                    o += sprintf(hex + o, "%02x ", ch & 0xff);
                }
                hex[o] = 0;
                gr_log("serve seq=%u cid=%u op=0x%x want=%u served=%u: %s",
                       ss, cid, s->exeOpPending, wantLen, served, hex);
            } else if (served && (ss % 16) == 0) {
                gr_log("serve seq=%u cid=%u op=0x%x want=%u served=%u "
                       "(datalane)",
                       ss, cid, s->exeOpPending, wantLen, served);
            }
        }
        /* Phase-42: per-reply byte logging for op 0x2714 (both the
         * 4-B out read and the ret read — the sfIdx=4 serve). Log the
         * exact bytes + source (gr prefill vs exe-written vs the
         * phase-43 ret fallback) once per session (first 4 serves: an
         * interleaved steal can sit between her two reads).          */
        if (served && s->rctl2714Active &&
            s->rctl2714ServeCount < 4) {
            unsigned i, n = served < 8 ? served : 8;
            char hex[3 * 8 + 4];
            unsigned o = 0;
            for (i = 0; i < n; i++) {
                o += sprintf(hex + o, "%02x", (unsigned char)win[i]);
                if (i + 1 < n) hex[o++] = ' ';
            }
            hex[o] = 0;
            gr_log("[gr_render] rctl 0x2714 serve bytes=%s src=%s", hex, serveSrc);
            /* Phase-45 (branch a): the ring offset math READ-BACK at her
             * serve #N (the SECOND = her ret read): pre-serve cursor,
             * post-serve cursors (a full drain resets both to 0), the
             * pre-serve pending ask, the fb-lane position, and the RAW
             * reply window as stored - proving whether both dwords sit
             * contiguously at reply+0 and exactly which offset each of
             * her two reads was served from.                          */
            gr_log("[gr_render] rctl 0x2714 ring#%u want=%u served=%u "
                   "qhead %u->%u wpos=%u pending=%u fbpos=%u exeOp=0x%x "
                   "ring8=%02x %02x %02x %02x %02x %02x %02x %02x",
                   s->rctl2714ServeCount + 1, wantLen, served,
                   qhead_pre45, S_QHEAD(s), S_WPOS(s), pending,
                   s->rctl2714FbPos, s->exeOpPending,
                   win ? win[GR_WIN_REPLY + 0] : 0xee,
                   win ? win[GR_WIN_REPLY + 1] : 0xee,
                   win ? win[GR_WIN_REPLY + 2] : 0xee,
                   win ? win[GR_WIN_REPLY + 3] : 0xee,
                   win ? win[GR_WIN_REPLY + 4] : 0xee,
                   win ? win[GR_WIN_REPLY + 5] : 0xee,
                   win ? win[GR_WIN_REPLY + 6] : 0xee,
                   win ? win[GR_WIN_REPLY + 7] : 0xee);
            s->rctl2714ServeCount++;
        }
        stl_le_p(mb + 0x08, 0);
        break; }
    case OP_HYPERCALL:
        mbox_set(mb, 0, 0);
        break;
    case OP_HDIRECT:
        stl_le_p(mb + 0x08, 0);
        break;
    default:
        stl_le_p(mb + 0x08, 0);
        break;
    }
    gr_log("op done cid=%u cmd=%u len=%u", cid, cmd, len);
    GR_DONE(cid);
}

/* ================================================================= */
/* worker thread                                                     */

static void *gr_worker(void *arg)
{
    (void)arg;
    gr_log("worker thread running");
    gr_init_dll_and_host();
    for (;;) {
        GrOp op;
        qemu_mutex_lock(&g.lock);
        while (g.qhead == g.qtail) {
            qemu_cond_wait(&g.cv, &g.lock);
        }
        op = g.q[g.qhead];
        g.qhead = (g.qhead == GR_QSZ - 1) ? 0 : g.qhead + 1;
        qemu_mutex_unlock(&g.lock);
        worker_dispatch_op(&op);
        /* Phase-50 (result-consumption pad): the guest reads the per-cid
         * mbox result [mbox+4] only AFTER its ack flag fires. Any LATER
         * op's completion landing in that wake->read window clobbers the
         * result the waiter is about to check: connect/disconnect
         * completions zero it (mbox_set(mb,0,0); 89+88 session-churn ops
         * per boot), the kernel reads status==0 as "peer closed" ->
         * -EINVAL -> the guest hstRecv js-path exit(1) = the silent stop
         * (no crash row; dies before the D1 marker), and ack aliasing
         * hands a second waiter foreign bytes (the pair theft on the
         * shared cid=1 queue). Hold the next completion ~2 ms so every
         * waiter consumes its own result first; FIFO + this pad also
         * pushes a concurrent reader's pull behind the owning attempt's
         * pair reads. */
        Sleep(2);
    }
    return NULL;
}

/* ================================================================= */
/* push (iothread -> worker)                                         */

static void gr_q_push(uint32_t cid, unsigned cmd, uint32_t len)
{
    qemu_mutex_lock(&g.lock);
    unsigned next = (g.qtail == GR_QSZ - 1) ? 0 : g.qtail + 1;
    if (next == g.qhead) {
        qemu_mutex_unlock(&g.lock);
        /* queue full: inline mbox completion so the guest makes forward
         * progress (matches the phase-1 stub semantics).               */
        uint8_t *mb = GR_MBOX(cid);
        mbox_set(mb, (cmd == OP_HYPERCALL) ? 0 : (int32_t)len, 0);
        GR_DONE(cid);
        return;
    }
    g.q[g.qtail].cid = cid;
    g.q[g.qtail].cmd = cmd;
    g.q[g.qtail].len = len;
    g.q[g.qtail].pad = 0;
    g.qtail = next;
    qemu_cond_signal(&g.cv);
    qemu_mutex_unlock(&g.lock);
}

static void gr_start(void)
{
    if (g.started) return;
    g.started = true;
    qemu_mutex_init(&g.lock);
    qemu_cond_init(&g.cv);
    qemu_thread_create(&g.thread, "gr_render", gr_worker, NULL,
                       QEMU_THREAD_JOINABLE);
}

/* ================================================================= */
/* API                                                               */

/* Phase-60 knob GR_ECHO_MODE (read once):
 *   2 (default) = phase-59 behaviour: phase-57 reply-echo drop + 59b
 *                 small-index drop;
 *   1           = phase-57 reply-echo drop only (59b OFF);
 *   0           = no drops (the 52A record lane).                   */
static int gr_echo_mode(void)
{
    static int cached = -1;
    if (cached < 0) {
        char buf[8] = {0};
        DWORD n = GetEnvironmentVariableA("GR_ECHO_MODE", buf, sizeof(buf));
        cached = (n > 0 && buf[0] >= '0' && buf[0] <= '2') ? buf[0] - '0' : 2;
        gr_log("GR_ECHO_MODE=%d", cached);
    }
    return cached;
}

/* Phase-61 knob GR_PAIRARM (read once): 1 (default) = phase-59 raw-0x2714
 * pair arm in rcTryParse; 0 = off (the exe's own prefill stages the pair,
 * as in 52A).                                                          */
static int gr_pairarm(void)
{
    static int cached = -1;
    if (cached < 0) {
        char buf[8] = {0};
        DWORD n = GetEnvironmentVariableA("GR_PAIRARM", buf, sizeof(buf));
        cached = (n > 0 && buf[0] == '0') ? 0 : 1;
        gr_log("GR_PAIRARM=%d", cached);
    }
    return cached;
}

/* Phase-62 knob GR_WINDUMP (read once): 1 = log the window around every
 * guest write (diagnostic only, no behaviour change).                  */
static int gr_windump(void)
{
    static int cached = -1;
    if (cached < 0) {
        char buf[8] = {0};
        DWORD n = GetEnvironmentVariableA("GR_WINDUMP", buf, sizeof(buf));
        cached = (n > 0 && buf[0] == '1') ? 1 : 0;
        gr_log("GR_WINDUMP=%d", cached);
    }
    return cached;
}

bool gr_render_enabled(void)
{
    char buf[8];
    DWORD n = GetEnvironmentVariableA("GR_BRIDGE", buf, sizeof(buf));
    if (g.devEnabled) return true;
    if (n > 0) return true;
    return false;
}

void gr_render_set_device_enabled(bool on)
{
    g.devEnabled = on;
}

void gr_render_attach(void *dev, const GrRenderHostOps *ops)
{
    g_dev = dev;
    g_ops = *ops;
}

bool gr_render_op_connect(uint32_t cid)
{
    if (!gr_render_enabled()) return false;
    gr_start();
    gr_q_push(cid, OP_CONNECT, 0);
    return true;
}

bool gr_render_op_disconnect(uint32_t cid)
{
    if (!gr_render_enabled()) return false;
    gr_start();
    gr_q_push(cid, OP_DISCONNECT, 0);
    return true;
}

bool gr_render_op_request(uint32_t cid, uint32_t cmd, uint32_t len)
{
    if (!gr_render_enabled()) return false;
    gr_start();
    gr_q_push(cid, cmd, len);
    return true;
}
