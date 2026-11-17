/*
 * bstvmsg - BlueStacks HvA virtual 'vmessage' PCI device.
 *
 * Guest kernel driver: initramfs_x/boot/bstmods/bstvmsg.ko
 *   PCI vendor 0xB574, device 0x1000, class 0x0000, INTx, shared IRQ.
 * Guest userspace client: /boot/bin/bstconf opens /dev/bstvmsg with the
 * service token 'hcal' (see REA_BSTCONF.md - full protocol from disassembly).
 *
 * Register map (BAR0, MMIO, 0x1000 bytes, dword file):
 *   +0x00  RO  maxConnections (probe reads it from BAR1+0x00 actually)
 *   +0x10  WO  channel reset (probe writes 0)                    - ignored
 *   +0x18  WO  service attach: 4CC 'hcal'/'inp'/...; the device allocates
 *              a cid and latches the per-cid shm header fields synchronously
 *   +0x20  WO  disconnect(cid)
 *   +0x28  WO  guest ISR "pending clear"                        - ignored
 *   +0x30  WO  process-request(cid); handled synchronously
 *
 * Shared memory (BAR1, RAM-backed, 0x100000):
 *  header dword fields (rewritten at every attach):
 *   +0x00  u32 maxConnections    (8)
 *   +0x04  u32 assigned cid      (0 <= cid < max, kernel panics otherwise)
 *   +0x08  u32 guestOff - in-area absolute bar1 offset
 *   +0x0c  u32 hostOff  - out-area absolute bar1 offset
 *   +0x10  u32 inSize   (0x1000; the guest->host request window)
 *   +0x14  u32 outSize  (0x10000; the host->guest response window)
 *   +0x18  u32 pollOff  - absolute bar1 offset of the per-cid poll word
 *   +0x1c  u32 served-cid (ISR push path; unused for bstconf)
 *  slots: slot_base(cid) = 0x1000 + cid * 0x11100
 *     in area   [slot_base, slot_base+0x1000)
 *     out area  [slot_base+0x1000, slot_base+0x11000)
 *     poll word [slot_base+0x11000)
 *  BAR1 total: 0x1000 header slot 0 base = 0x1000, last slot ends at
 *  0x1000 + 8*0x11100 = 0x89800 <= 0x100000 OK.
 *
 * Request/response wire format (all little-endian; no bswap anywhere):
 *  request  (guest writes at slot guestOff): u32 method; u32 msglen;
 *            u32 bufLen; u32 bufLen2; u32 tail  (method 1 = GetProps)
 *  response (host writes at slot hostOff):   bufLen bytes of payload, then
 *            u32 outWritten at out + bufLen, u32 result at out + bufLen + 4.
 *  poll word must be 0 by the time the process-request ioctl reads it.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/module.h"
#include "qemu/units.h"
#include "hw/pci/pci.h"
#include "hw/pci/pci_device.h"
#include "qom/object.h"
#include "system/memory.h"
#include "ui/input.h"
#include "system/system.h"
#include "hw/misc/gr_render.h"
#include "hw/core/qdev-properties.h"

#define TYPE_BST_VMSG "bstvmsg"
OBJECT_DECLARE_SIMPLE_TYPE(BstVmsgState, BST_VMSG)

#define BST_VMSG_BAR0_SIZE      0x1000
#define BST_VMSG_BAR1_SIZE      0x800000  /* p72: 8 slots x 0x81000 */

#define VMSG_SHM_MAX            0x00
#define VMSG_SHM_CID            0x04
#define VMSG_SHM_GUEST_OFF      0x08
#define VMSG_SHM_HOST_OFF       0x0c
#define VMSG_SHM_PARA1          0x10
#define VMSG_SHM_PARA2          0x14
#define VMSG_SHM_PENDING_OFF    0x18
#define VMSG_SHM_IRQ_PENDING    0x1c

#define VMSG_MAX_CONNECTIONS    8
#define VMSG_IN_SIZE            0x40000u  /* p72: was 4K; installed-apps sync (hcallSyncInstalledAppsRpc) needs more, xdbgBreak otherwise */
#define VMSG_OUT_SIZE           0x40000u
#define VMSG_IN_ALIGN           VMSG_IN_SIZE
/* Every slot offset (guest_off/host_off/poll_off) MUST be page-aligned:
 * the guest kernel's remap_pfn_range() uses (bar1_phys + guest_off) >> 12,
 * which truncates non-aligned offsets and shifts the guest's window.
 * stride = 0x1000 (in) + 0x10000 (out) + 0x1000 (poll page) = 0x12000. */
#define VMSG_SLOT_STRIDE        (VMSG_IN_ALIGN + VMSG_OUT_SIZE + 0x1000u)
#define VMSG_SLOT_BASE(cid)     (0x1000u + (cid) * VMSG_SLOT_STRIDE)

typedef struct BstVmsgConn {
    uint32_t guest_off;
    uint32_t host_off;
    uint32_t poll_off;
    uint32_t in_size;
    uint32_t out_size;
    uint32_t service_id;
    uint8_t in_use;
} BstVmsgConn;

/* Phase-68 host->guest push (bstinput "inp" session), dev/NOTES.md 10e:
 * guest ISR reads the cid at shm+0x1c, clears the line via BAR0+0x28,
 * runs cb(cid, shm+host_off) and acks with BAR0+0x38 = cid. One message
 * in flight at a time (a second IRQ before the ack corrupts its list). */
#define VMSG_INP_SID            0x00706e69u     /* "inp" */
#define VMSG_GCAL_SID           0x6c616367u     /* "gcal": host->guest calls */
#define VMSG_PUSH_SLOTS         64
#define VMSG_PUSH_MAX           132             /* touch frame = largest */
#define VMSG_TOUCH_SLOTS        16
#define VMSG_TOUCH_NONE         0x7fffffffu

typedef struct BstVmsgState {
    PCIDevice parent_obj;

    MemoryRegion bar0;
    MemoryRegion bar1;
    char shm_name[64];

    uint32_t max_connections;
    BstVmsgConn conn[VMSG_MAX_CONNECTIONS];
    uint8_t *shm;

    /* Phase-68 push queue + touch state */
    uint8_t pushq[VMSG_PUSH_SLOTS][VMSG_PUSH_MAX];
    uint32_t pushlen[VMSG_PUSH_SLOTS];
    bool pushmove[VMSG_PUSH_SLOTS];
    unsigned qh, qt, pushes, drops;
    bool push_busy;
    QemuInputHandlerState *ihs;
    Notifier init_done;
    int tx, ty;
    bool tdown, tdirty;

    /* host props served to the guest's bstconf (GetProps) */
    char *props;            /* extra 'k=v;k=v' (later lines win) */
    char *propsfile;        /* extra props file, one k=v per line */
    bool touch_mt;          /* Phase-80: bstinput type-5 multi-touch (kernel 5.x images) */
    GString *props_text;
    unsigned reqlog;
    bool gcal_parked_logged;
    bool devmode_logged;
} BstVmsgState;

/* identity/config text bstconf serves (REA_BSTCONF.md 6). No spaces around
 * '=': bstsetconf.awk maps 'bst.android.id' -> exported BST_ANDROID_ID
 * (toupper + gsub dots->underscore, spaces in names would break it). */
static const char bst_vmsg_props_text[] =
    "bst.android.id=bfd0e87474aa4028\n"
    "bst.serialno=cge1f98585bb\n"
    "bst.imei_id=531090189194022\n"
    "bst.device_hash=0503010A090A0108090B090E0A0C0209\n"
    "bst.sim_issuer_id=221\n"
    "bst.sim_suffix=134261319\n"
    "bst.subscriber_id=410134261319\n"
    "bst.wifi_mac_addr=31:A9:A1:89:B9:EA\n"
    "bst.bssid_mac_addr=1A:9A:18:9B:9E:AC\n"
    "bst.bluetooth_addr=64:2B:0B:29:0C:0F\n"
    "bst.device_carrier_code=310_260\n"
    /* adbd gate (_bst_allow_adb_cmd): >0 allows every shell/sync/exec service;
     * real BlueStacks gets it from bluestacks.conf (Settings > Enable ADB). */
    "bst.enable_adb_access=1\n"
    /* BstFilterAppsService rejects every /data/downloads/.config.db rule
     * (NumberFormatException) without these; abi_list enables ARM translation.
     * Full HD-Player set: propsfile= from dev/tools/bstprops.py. */
    "bst.oem=nxt\n"
    "bst.version=5.21.550.1031\n"
    "bst.country=US\n"
    "bst.locale=en-US\n"
    "bst.abi_list=x86,x64,arm,arm64\n"
    "bst.instance=Pie64\n";

static BstVmsgConn *bst_vmsg_get_conn(BstVmsgState *s, uint32_t cid)
{
    if (cid >= s->max_connections || !s->conn[cid].in_use) {
        return NULL;
    }
    return &s->conn[cid];
}

static void bst_vmsg_process_request(BstVmsgState *s, BstVmsgConn *c)
{
    uint8_t *in;
    uint8_t *out;
    uint32_t method;
    uint32_t buf_len;
    uint32_t resp_len;
    size_t props_len = s->props_text->len;

    if (c->guest_off + VMSG_IN_SIZE > BST_VMSG_BAR1_SIZE ||
        c->host_off + c->out_size > BST_VMSG_BAR1_SIZE) {
        return;
    }

    in = s->shm + c->guest_off;
    out = s->shm + c->host_off;
    method = ldl_le_p(in + 0x00);
    buf_len = ldl_le_p(in + 0x08);

    /* Phase-76: on 'gcal' (BstCommandProcessor's host->guest call channel)
     * method 0 = "wait for the next host call". HD-Player keeps it pending
     * until it has a call; answering at once made the guest spin (about two
     * vCPUs in BstCommandProcessor + logd: "gcallDecode: invalid opcode: 0").
     * Poll word 1 = pending: bstvmsg.ko vmsgIoctlProcessRequest sleeps on the
     * connection's waitqueue until an IRQ names this cid (vmsgIsr). We have
     * no host calls to deliver, so it simply stays parked.              */
    if (method == 0 && c->service_id == VMSG_GCAL_SID) {
        if (!s->gcal_parked_logged) {
            s->gcal_parked_logged = true;
            fprintf(stderr, "[bstvmsg] gcal cid=%u: wait-for-host-call parked\n",
                    (unsigned)(c - s->conn));
        }
        stl_le_p(s->shm + c->poll_off, 1);
        return;
    }

    if (method != 0 || s->reqlog < 20) {
        s->reqlog += (method == 0);
        fprintf(stderr, "[bstvmsg] processRequest cid=%u goff=0x%x hoff=0x%x "
            "method=%u msglen=%u bufLen=%u d3=%u tail=%u\n", c - s->conn,
            c->guest_off, c->host_off, method, ldl_le_p(in + 0x04),
            buf_len, ldl_le_p(in + 0x0c), ldl_le_p(in + 0x10));
    }

    if (method == 24) {
        /* hcallIsAppLaunchAllowedRpc (libhostcall_jni 0x15ea0): request
         * [24][msglen][strlen+1][package\0][u8 flag]; the guest reads the
         * FIRST BYTE of the out window, non-zero = allowed. Unhandled, the
         * answer was whatever byte 0 held from an earlier reply, so launches
         * from the launcher broke once hcal 7 wrote 0 there (Phase 77). */
        stl_le_p(out + 0, 1);
    } else if (method == 68) {
        /* Phase-83: Android 13's BlueStacks su ("isDeveloperMode") asks the
         * host over /dev/bstvmsg before granting root (no payload, bufLen 0;
         * first u32 of the out window, non-zero = developer mode / root on).
         * Unanswered, out+0 held a stale 0 and su silently refused. Follow
         * the instance's bst.enable_root_access prop like the older images'
         * su does (off for Magisk-rooted instances). */
        bool root = strstr(s->props_text->str, "bst.enable_root_access=1") != NULL;
        if (!s->devmode_logged) {
            s->devmode_logged = true;
            fprintf(stderr, "[bstvmsg] hcal 68 isDeveloperMode -> %d\n", root);
        }
        stl_le_p(out + 0, root ? 1 : 0);
    } else if (method == 7) {
        /* Phase-77: hcallOnOrientationChangedRpc: [7][0xc][rotation 0..3]
         * (the generic "bufLen" slot carries the rotation). Reply: u32 0
         * at out+0 (the guest reads the first u32 of the out window).  */
        uint32_t o = ldl_le_p(in + 0x08);
        fprintf(stderr, "[bstvmsg] hcal OnOrientationChanged rotation=%u\n", o);
        stl_le_p(out + 0, 0);
        stl_le_p(out + 4, 0);
        if (o <= 3) {
            gr_render_set_orientation((int)o);
        }
    } else if (method == 1 && buf_len != 0 && buf_len <= (VMSG_OUT_SIZE - 0x100u)) {
        memset(out, 0, c->out_size);
        resp_len = (uint32_t)MIN(props_len, buf_len);
        memcpy(out, s->props_text->str, resp_len);
        stl_le_p(out + buf_len, resp_len);
        stl_le_p(out + buf_len + 4, 0);
        fprintf(stderr, "[bstvmsg] served GetProps respLen=%u -> hoff 0x%x\n",
                resp_len, c->host_off);
    } else if (buf_len != 0 && buf_len + 8 <= c->out_size) {
        stl_le_p(out + buf_len, 0);
        stl_le_p(out + buf_len + 4, 0xffffffffu);
        fprintf(stderr, "[bstvmsg] rejected method=%u -> result=-1\n", method);
        {   /* Phase-77: payload dump (printable) to identify host calls */
            char txt[161];
            uint32_t ml = ldl_le_p(in + 0x04), i, n = 0;
            for (i = 0x14; i < 0x14 + ml && i < VMSG_IN_SIZE && n < 160; i++, n++) {
                uint8_t ch = in[i];
                txt[n] = (ch >= 0x20 && ch < 0x7f) ? (char)ch : '.';
            }
            txt[n] = 0;
            fprintf(stderr, "[bstvmsg]   method=%u payload: %s\n", method, txt);
        }
    }
    /* unblock the waiting guest ioctl */
    stl_le_p(s->shm + c->poll_off, 0);
}

static int bst_vmsg_find_sid(BstVmsgState *s, uint32_t sid)
{
    for (uint32_t i = 0; i < s->max_connections; i++) {
        if (s->conn[i].in_use && s->conn[i].service_id == sid) {
            return (int)i;
        }
    }
    return -1;
}

/* deliver the next queued message if the channel is idle */
static void bst_vmsg_push_kick(BstVmsgState *s)
{
    if (s->push_busy || s->qh == s->qt) {
        return;
    }
    int cid = bst_vmsg_find_sid(s, VMSG_INP_SID);
    if (cid < 0) {
        return;                         /* bstinput not attached yet: keep */
    }
    unsigned k = s->qh++ % VMSG_PUSH_SLOTS;
    uint8_t *dst = s->shm + s->conn[cid].host_off;
    memset(dst, 0, 0x100);
    memcpy(dst, s->pushq[k], s->pushlen[k]);
    stl_le_p(s->shm + VMSG_SHM_IRQ_PENDING, (uint32_t)cid);
    s->push_busy = true;
    s->pushes++;
    if (s->pushes <= 20 || (s->pushes & 0xff) == 0) {
        fprintf(stderr, "[bstvmsg] push #%u cid=%d type=%u x=%u y=%u (q=%u)\n",
                s->pushes, cid, ldl_le_p(dst), ldl_le_p(dst + 4),
                ldl_le_p(dst + 8), s->qt - s->qh);
    }
    pci_set_irq(PCI_DEVICE(s), 1);
}

static void bst_vmsg_push(BstVmsgState *s, const uint8_t *msg, uint32_t len,
                          bool is_move)
{
    /* coalesce consecutive touch moves still waiting in the queue */
    if (is_move && s->qt != s->qh &&
        s->pushmove[(s->qt - 1) % VMSG_PUSH_SLOTS]) {
        unsigned k = (s->qt - 1) % VMSG_PUSH_SLOTS;
        memcpy(s->pushq[k], msg, len);
        s->pushlen[k] = len;
    } else if (s->qt - s->qh >= VMSG_PUSH_SLOTS) {
        s->drops++;
        return;
    } else {
        unsigned k = s->qt++ % VMSG_PUSH_SLOTS;
        memcpy(s->pushq[k], msg, len);
        s->pushlen[k] = len;
        s->pushmove[k] = is_move;
    }
    bst_vmsg_push_kick(s);
}

/* QEMU absolute pointer -> "BlueStacks Virtual Touch" frames.
 * QEMU's abs range (0..0x7fff) equals the device's ABS_MT range. */
static void bst_vmsg_input_event(DeviceState *dev, QemuConsole *src,
                                 QemuInputEvent *evt)
{
    BstVmsgState *s = BST_VMSG(dev);
    static unsigned nlog;

    if (nlog < 12) {
        nlog++;
        fprintf(stderr, "[bstvmsg] touch: input event kind=%d\n", (int)evt->type);
    }
    switch (evt->type) {
    case INPUT_EVENT_KIND_ABS: {
        InputMoveEvent *m = &evt->abs;
        if (m->axis == INPUT_AXIS_X) {
            s->tx = m->value;
        } else if (m->axis == INPUT_AXIS_Y) {
            s->ty = m->value;
        }
        if (s->tdown) {
            s->tdirty = true;
        }
        break;
    }
    case INPUT_EVENT_KIND_BTN: {
        InputBtnEvent *b = &evt->btn;
        if (b->button == INPUT_BUTTON_LEFT && b->down != s->tdown) {
            s->tdown = b->down;
            s->tdirty = true;
        }
        break;
    }
    default:
        break;
    }
}

static void bst_vmsg_input_sync(DeviceState *dev)
{
    BstVmsgState *s = BST_VMSG(dev);
    uint8_t m[VMSG_PUSH_MAX];
    static bool was_down;

    if (!s->tdirty) {
        return;
    }
    s->tdirty = false;
    if (s->touch_mt) {
        /* Phase-80: kernel 5.15 bstinput (Tiramisu64) inpMtTouchDispatch:
         * type 5, [u32 count] + count x {u32 slot, u32 state, u32 x, u32 y};
         * state 2 = finger up (ABS_MT_TRACKING_ID -1), else down/move at x,y
         * (0..32767), then BTN_TOUCH + SYN. Type 2 is "unknown" there.  */
        memset(m, 0, 24);
        stl_le_p(m + 0, 5);
        stl_le_p(m + 4, 1);
        stl_le_p(m + 8, 0);                          /* slot 0 */
        stl_le_p(m + 12, s->tdown ? 1 : 2);
        stl_le_p(m + 16, (uint32_t)s->tx);
        stl_le_p(m + 20, (uint32_t)s->ty);
        bst_vmsg_push(s, m, 24, s->tdown && was_down);
        was_down = s->tdown;
        return;
    }
    stl_le_p(m, 2);                                  /* type 2 = touch */
    for (int i = 0; i < VMSG_TOUCH_SLOTS; i++) {
        stl_le_p(m + 4 + 8 * i, VMSG_TOUCH_NONE);
        stl_le_p(m + 8 + 8 * i, VMSG_TOUCH_NONE);
    }
    if (s->tdown) {
        stl_le_p(m + 4, (uint32_t)s->tx);
        stl_le_p(m + 8, (uint32_t)s->ty);
    }
    /* only a move while already down may be coalesced */
    bst_vmsg_push(s, m, sizeof(m), s->tdown && was_down);
    was_down = s->tdown;
}

static const QemuInputHandler bst_vmsg_touch_handler = {
    .name  = "BlueStacks Virtual Touch",
    .mask  = INPUT_EVENT_MASK_BTN | INPUT_EVENT_MASK_ABS,
    .event = bst_vmsg_input_event,
    .sync  = bst_vmsg_input_sync,
};

/* Bind the touch handler to the renderer console (bstpgaipc0) once every
 * device exists: otherwise button events go to the most recently
 * activated BTN handler (the PS/2 mouse) and touches never start.     */
static void bst_vmsg_init_done(Notifier *n, void *data)
{
    BstVmsgState *s = container_of(n, BstVmsgState, init_done);

    /* Re-activate once every device exists, so this unbound abs+btn handler
     * is first in QEMU's list: the VNC server then runs in absolute mode
     * and both positions and clicks come here (not to the PS/2 mouse).
     * (Binding to a console hides the handler from the unbound absolute-mode
     * check and VNC falls back to relative motion.)                      */
    qemu_input_handler_activate(s->ihs);
    fprintf(stderr, "[bstvmsg] touch: handler activated (abs=%d)\n",
            qemu_input_is_absolute(NULL));
}

static void bst_vmsg_bar0_write(void *opaque, hwaddr addr, uint64_t val,
                                unsigned size)
{
    BstVmsgState *s = opaque;
    uint32_t cid = (uint32_t)val;
    uint32_t goff;
    uint32_t hoff;
    uint32_t poff;
    uint32_t i;

    switch (addr) {
    case 0x10:
        break;
    case 0x18: {
        /* service attach: value is a 4CC ('hcal' / 'inp' / ...) */
        for (i = 0; i < s->max_connections; i++) {
            if (!s->conn[i].in_use) {
                break;
            }
        }
        if (i == s->max_connections) {
            i = 0;
        }
        goff = VMSG_SLOT_BASE(i);
        hoff = goff + VMSG_IN_ALIGN;
        poff = goff + VMSG_IN_ALIGN + VMSG_OUT_SIZE;
        s->conn[i].guest_off = goff;
        s->conn[i].host_off = hoff;
        s->conn[i].poll_off = poff;
        s->conn[i].in_size = VMSG_IN_SIZE;
        s->conn[i].out_size = VMSG_OUT_SIZE;
        s->conn[i].service_id = (uint32_t)val;
        s->conn[i].in_use = 1;
        stl_le_p(s->shm + VMSG_SHM_CID, i);
        stl_le_p(s->shm + VMSG_SHM_GUEST_OFF, goff);
        stl_le_p(s->shm + VMSG_SHM_HOST_OFF, hoff);
        stl_le_p(s->shm + VMSG_SHM_PARA1, VMSG_IN_SIZE);
        stl_le_p(s->shm + VMSG_SHM_PARA2, VMSG_OUT_SIZE);
        stl_le_p(s->shm + VMSG_SHM_PENDING_OFF, poff);
        stl_le_p(s->shm + poff, 0);
        fprintf(stderr, "[bstvmsg] attach sid=0x%08x cid=%u goff=0x%x hoff=0x%x "
                "poll=0x%x\n", (uint32_t)val, i, goff, hoff, poff);
        break;
    }
    case 0x20: {
        BstVmsgConn *c = bst_vmsg_get_conn(s, cid);
        if (c) {
            c->in_use = 0;
        }
        break;
    }
    case 0x28:
        /* guest hard ISR consumed shm+0x1c: drop the line, mark idle cid */
        pci_set_irq(PCI_DEVICE(s), 0);
        stl_le_p(s->shm + VMSG_SHM_IRQ_PENDING, 0xffffffffu);
        break;
    case 0x38:
        /* guest callback done (val = cid): channel free for the next push */
        if (s->pushes <= 20) {
            fprintf(stderr, "[bstvmsg] push ack cid=%u\n", cid);
        }
        s->push_busy = false;
        bst_vmsg_push_kick(s);
        break;
    case 0x30: {
        BstVmsgConn *c = bst_vmsg_get_conn(s, cid);
        if (c) {
            bst_vmsg_process_request(s, c);
        }
        break;
    }
    default:
        break;
    }
}

static uint64_t bst_vmsg_bar0_read(void *opaque, hwaddr addr, unsigned size)
{
    BstVmsgState *s = opaque;

    switch (addr) {
    case 0x00:
        return s->max_connections;
    default:
        return 0;
    }
}

static const MemoryRegionOps bst_vmsg_bar0_ops = {
    .read = bst_vmsg_bar0_read,
    .write = bst_vmsg_bar0_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

/*
 * BAR1 is RAM-backed; the guest mmap()s windows of it directly (requests are
 * written by guest code), so no MDIO-style ops object is needed.
 */

static void bst_vmsg_realize(PCIDevice *pdev, Error **errp)
{
    BstVmsgState *s = BST_VMSG(pdev);
    uint8_t *pci_conf = pdev->config;
    static unsigned instance;

    pci_config_set_interrupt_pin(pci_conf, 1);

    memory_region_init_io(&s->bar0, OBJECT(s), &bst_vmsg_bar0_ops, s,
                          "bstvmsg-bar0", BST_VMSG_BAR0_SIZE);
    pci_register_bar(pdev, 0, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar0);

    snprintf(s->shm_name, sizeof(s->shm_name), "bstvmsg-shm-%u", instance++);
    if (!memory_region_init_ram(&s->bar1, OBJECT(s), s->shm_name,
                                BST_VMSG_BAR1_SIZE, errp)) {
        return;
    }
    pci_register_bar(pdev, 1, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar1);

    s->shm = memory_region_get_ram_ptr(&s->bar1);
    memset(s->shm, 0, BST_VMSG_BAR1_SIZE);

    s->max_connections = VMSG_MAX_CONNECTIONS;
    stl_le_p(s->shm + VMSG_SHM_MAX, s->max_connections);
    /* Phase-68: 0 here read as "cid 0 pending" by the guest ISR on every
     * shared-line interrupt (spurious bstkbd scancode 0 / list corruption). */
    stl_le_p(s->shm + VMSG_SHM_IRQ_PENDING, 0xffffffffu);
    stl_le_p(s->shm + VMSG_SHM_CID, 0xffffffffu);

    s->props_text = g_string_new(bst_vmsg_props_text);
    if (s->propsfile && *s->propsfile) {
        g_autofree char *txt = NULL;
        g_autoptr(GError) gerr = NULL;
        if (!g_file_get_contents(s->propsfile, &txt, NULL, &gerr)) {
            error_setg(errp, "bstvmsg: propsfile: %s", gerr->message);
            return;
        }
        for (char *c = txt; *c; c++) {
            if (*c == '\r') {
                *c = '\n';
            }
        }
        g_string_append(s->props_text, txt);
        g_string_append_c(s->props_text, '\n');
    }
    if (s->props && *s->props) {
        g_auto(GStrv) kv = g_strsplit(s->props, ";", -1);
        for (char **k = kv; *k; k++) {
            if (**k) {
                g_string_append_printf(s->props_text, "%s\n", *k);
            }
        }
    }
    fprintf(stderr, "[bstvmsg] props text %zu bytes\n", s->props_text->len);

    s->ihs = qemu_input_handler_register(DEVICE(s), &bst_vmsg_touch_handler);
    qemu_input_handler_activate(s->ihs);
    s->init_done.notify = bst_vmsg_init_done;
    qemu_add_machine_init_done_notifier(&s->init_done);
}

static const Property bst_vmsg_props[] = {
    DEFINE_PROP_STRING("props", BstVmsgState, props),
    DEFINE_PROP_STRING("propsfile", BstVmsgState, propsfile),
    DEFINE_PROP_BOOL("touch-mt", BstVmsgState, touch_mt, false),
};

static void bst_vmsg_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *k = PCI_DEVICE_CLASS(klass);

    k->realize = bst_vmsg_realize;
    k->vendor_id = 0xB574;
    k->device_id = 0x1000;
    k->revision = 0;
    k->class_id = 0x0000;
    dc->desc = "BlueStacks bstvmsg PCI device (hcal props service)";
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
    device_class_set_props(dc, bst_vmsg_props);
}

static const TypeInfo bst_vmsg_types[] = {
    {
        .name          = TYPE_BST_VMSG,
        .parent        = TYPE_PCI_DEVICE,
        .instance_size = sizeof(BstVmsgState),
        .class_init    = bst_vmsg_class_init,
        .interfaces = (const InterfaceInfo[]) {
            { INTERFACE_CONVENTIONAL_PCI_DEVICE },
            { },
        },
    },
};

DEFINE_TYPES(bst_vmsg_types)
