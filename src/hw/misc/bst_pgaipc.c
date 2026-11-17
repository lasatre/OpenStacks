/*
 * BlueStacks "bstpgaipc" virtual PCI device - HST (Hyper-Stream Transport) host
 * side.
 *
 * pci 1AF4:10F0, three regions (matches bstpgaipc.ko probe):
 *   BAR0 (MMIO, 0x1000)
 *     +0x00  WO  doorbell: executes the mailbox command of cid    (guest writes cid)
 *     +0x08  WO  IRQ ack (guest writes the served cid back)
 *   BAR1 (host RAM, 32 MiB)
 *     control/mailbox area: 16-byte record per cid at cid*0x10
 *       +0x00 u32 cmd   (guest)
 *       +0x04 u32 ret   (payload len in from the guest ioctl; result copy-back)
 *       +0x08 u32 status(!=0 -> driver's hypercall() fails immediately)
 *       +0x0c u32 pad
 *     +0x870 u32 served-cid report (device writes, guest ISR clears)
 *     small slots: cid 0x00..0x7e  -> guest window at BAR1 + cid*0x40000 (256 KiB)
 *   BAR2 (host RAM, 32 MiB)
 *     big slots: cid 0x7f..0x86 -> (cid-0x7f)*0x400000 (4 MiB each)
 *
 * Decoded protocol (see REA_HST_PROTOCOL.md / tools/hst_dis.py session notes):
 *   cmd 1 = connect   (connect ioctl 0x7001; no wait in caller apart from IRQ)
 *   cmd 2 = disconnect/fail fast (also .release; no wait)
 *   cmd 3 = read      (ioctl 0x40087003; host fills slot window[0..n), mbox ret=n)
 *   cmd 4 = write     (ioctl 0x40087004; payload already in window[0..len))
 *   cmd 6 = hypercall (ioctl 0x7006, hpp "data io hypercall": fire & forget,
 *                      driver checks status==0 right after the doorbell)
 *   cmd 7 = hpp direct write variant (not used in the non-hpp boot path)
 *
 * The mailbox is answered synchronously inside the BAR0 write, exactly like the
 * HstHost mmap'd control record execution on the real device (guest reads its
 * results straight after the doorbell).  The streamed payload lives in the
 * cid's slot window.
 *
 * Emulation policy (v1, capture build):
 *   - every doorbell is logged with cid/cmd/len (throttled);
 *   - the first payloads of each cid are hex-dumped to qemu stderr
 *     (they hold the 256-byte process-name handshake frame);
 *   - connect/log/disconnect are acknowledged faithfully;
 *   - write: payload captured, ret echoes len;
 *   - read: reply zero-filled n=want so the client keeps making forward
 *     progress instead of a busy-wait spin (the actual GLES stream renderer is
 *     a separate host-side project - see the "next step" notes);
 *   - clients that are NOT the graphics host stream (bstconf etc.) keep the
 *     old failure reply (ret = -1) so boot scripts keep their previous
 *     behaviour.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/bswap.h"
#include "qemu/module.h"
#include "qemu/units.h"
#include "qemu/main-loop.h"
#include "hw/pci/pci.h"
#include "hw/pci/pci_device.h"
#include "hw/core/qdev-properties.h"
#include "qom/object.h"
#include "system/memory.h"
#include "gr_render.h"

#define TYPE_BST_PGAIPC "bstpgaipc"
OBJECT_DECLARE_SIMPLE_TYPE(BstPgaipcState, BST_PGAIPC)

#define BST_PGAIPC_BAR0_SIZE   0x1000
#define BST_PGAIPC_BAR1_SIZE   (32 * MiB)
#define BST_PGAIPC_BAR2_SIZE   (32 * MiB)
#define BST_PGAIPC_MAX_CID     0x86

#define PGAIPC_MAILBOX_STRIDE  0x10
#define PGAIPC_SERVED_CID_REG  0x870

/* per-cid slot window sizes */
#define PGAIPC_SMALL_WIN       0x40000
#define PGAIPC_BIG_WIN         0x400000

#define PGAIPC_CMD_CONNECT     1
#define PGAIPC_CMD_DISCONNECT  2
#define PGAIPC_CMD_READ        3
#define PGAIPC_CMD_WRITE       4
#define PGAIPC_CMD_HYPERCALL   6
#define PGAIPC_CMD_HDIRECT     7

/* how many payload hex dumps to emit per cid (no fast-path spam) */
#define PGA_DUMP_MAX           6
#define PGA_DUMP_BYTES         64

/* gr-render cids own their mbox ret/status + IRQ via the worker MIME */
#define GR_MAX_CID_BR          0x87

/* markers which identify the graphics host-stream client in the 256-byte
 * process-name handshake frame */
static const char *gfx_markers[] = {
    "surfaceflinger", "zygote", "app_process", "bootanim",
    "hwcomposer", "gralloc", "renderControl", "egl",
};

struct pgacid_state {
    bool used;          /* connect doorbell seen */
    bool gfx;           /* identified as the graphics host stream */
    bool ident;         /* handshake payload seen */
    unsigned dumps;     /* hex dumps emitted */
    uint32_t wr_first_len;
};

struct BstPgaipcState {
    PCIDevice parent_obj;

    MemoryRegion bar0;
    MemoryRegion bar1;
    MemoryRegion bar2;
    char shm_name[64];
    char mem_name[64];

    uint8_t *shm;   /* BAR1 */
    uint8_t *mem;   /* BAR2 */
    bool irq_asserted;

    /* Phase-2 render bridge */
    bool gr;
    QEMUBH *gr_bh[GR_MAX_CID_BR];
    unsigned gr_complets;
    struct GrBhCtx { BstPgaipcState *dev; uint32_t cid; } grctx[GR_MAX_CID_BR];

    struct pgacid_state cid[BST_PGAIPC_MAX_CID + 1];
};

/* ------------------------------------------------------------------ */
static uint8_t *pga_mbox(BstPgaipcState *s, uint32_t cid)
{
    return s->shm + cid * PGAIPC_MAILBOX_STRIDE;
}

/* Phase-63 (dev/NOTES.md §7): the guest maps channel c's small window at
 * BAR1 + (c+1)*0x40000, not c*0x40000. Slot 0 is the control page
 * (mailboxes at cid*0x10, served-cid at +0x870). Proof (p62B recscan):
 * channel 1's goldfish records ({0x2710,8}, {0x2711,16,4,4}, and
 * {0x2713,20,0x1F03,0x400} after the 0x100-B handshake) all sit in our
 * slot 2. PGA_SLOTSHIFT=1 applies the +1; default 0 = legacy layout. */
static unsigned pga_slotshift(void)
{
    static int cached = -1;
    if (cached < 0) {
        const char *v = getenv("PGA_SLOTSHIFT");
        cached = (v && v[0] == '1') ? 1 : 0;
        fprintf(stderr, "[bstpgaipc] PGA_SLOTSHIFT=%d\n", cached);
    }
    return (unsigned)cached;
}

static uint8_t *pga_window(BstPgaipcState *s, uint32_t cid)
{
    if (cid < 0x7f) {
        return s->shm + (cid + pga_slotshift()) * PGAIPC_SMALL_WIN;
    }
    return s->mem + (cid - 0x7f) * PGAIPC_BIG_WIN;
}

static size_t pga_window_size(uint32_t cid)
{
    return (cid < 0x7f) ? PGAIPC_SMALL_WIN : PGAIPC_BIG_WIN;
}

static void pga_complete(BstPgaipcState *s, uint32_t cid)
{
    /* served-cid report + shared INTx */
    stl_le_p(s->shm + PGAIPC_SERVED_CID_REG, cid);
    if (!s->irq_asserted) {
        s->irq_asserted = true;
        pci_irq_assert(PCI_DEVICE(s));
    }
}

static void pga_hexline(const uint8_t *p, unsigned n, uint32_t cid,
                        int wr)
{
    char b[PGA_DUMP_BYTES * 3 + 8];
    char a[PGA_DUMP_BYTES + 1];
    unsigned i;
    size_t o = 0;

    if (n > PGA_DUMP_BYTES) {
        n = PGA_DUMP_BYTES;
    }
    for (i = 0; i < n; i++) {
        o += (size_t)snprintf(b + o, sizeof(b) - o, "%02x ", p[i]);
        a[i] = (p[i] >= 0x20 && p[i] < 0x7f) ? (char)p[i] : '.';
    }
    a[n] = 0;
    fprintf(stderr, "[bstpgaipc] cid=%u %s payload: %s%s raw: %s\n",
            cid, wr ? "write" : "read", a, n < PGA_DUMP_BYTES ? "" : "...",
            b);
}

static bool pga_is_gfx(const uint8_t *p, size_t len)
{
    size_t i, j;
    size_t n = len > PGA_DUMP_BYTES ? PGA_DUMP_BYTES : len;

    for (i = 0; i < n; i++) {
        size_t avail = n - i;
        for (j = 0; j < ARRAY_SIZE(gfx_markers); j++) {
            size_t mk = strlen(gfx_markers[j]);
            if (avail >= mk && p[i + mk] == 0 &&
                memcmp(p + i, gfx_markers[j], mk) == 0) {
                fprintf(stderr, "[bstpgaipc] cid gfx marker '%s' matched\n",
                        gfx_markers[j]);
                return true;
            }
        }
    }
    return false;
}

/* ------------------------------------------------------------------ */
/* Phase-2 render-bridge host callbacks (gr_render -> device)         */

struct GrRenderHostOps;   /* via gr_render.h */

static uint8_t *gr_host_mbox(void *dev, uint32_t cid)
{
    BstPgaipcState *s = dev;
    if (cid > BST_PGAIPC_MAX_CID) return NULL;
    return pga_mbox(s, cid);
}

static uint8_t *gr_host_window(void *dev, uint32_t cid)
{
    BstPgaipcState *s = dev;
    if (cid > BST_PGAIPC_MAX_CID) return NULL;
    return pga_window(s, cid);
}

static size_t gr_host_windowsize(uint32_t cid)
{
    return pga_window_size(cid);
}

/* main-loop BH: served-cid report + INTx assert (async completion)   */
static void gr_bh_complete(void *opaque)
{
    struct GrBhCtx *c = opaque;
    pga_complete(c->dev, c->cid);
    fprintf(stderr, "[bstpgaipc] gr ack cid=%u\n", c->cid);
}

static void gr_host_complete(void *dev, uint32_t cid)
{
    BstPgaipcState *s = dev;
    if (cid > BST_PGAIPC_MAX_CID) return;
    qemu_bh_schedule(s->gr_bh[cid]);
}

static const GrRenderHostOps gr_host_ops = {
    .mbox = gr_host_mbox,
    .window = gr_host_window,
    .windowSize = gr_host_windowsize,
    .complete = gr_host_complete,
};

/* ------------------------------------------------------------------ */
static void pga_doorbell(BstPgaipcState *s, uint32_t cid)
{
    uint8_t *mb;
    uint8_t *win;
    size_t winlen;
    uint32_t cmd, len, ret;

    if (cid > BST_PGAIPC_MAX_CID) {
        return;
    }
    mb = pga_mbox(s, cid);
    win = pga_window(s, cid);
    winlen = pga_window_size(cid);

    cmd = ldl_le_p(mb + 0x00);
    len = ldl_le_p(mb + 0x04);
    ret = len;

    if (s->gr && gr_render_enabled()) {
        switch (cmd) {
        case PGAIPC_CMD_CONNECT:
            fprintf(stderr, "[bstpgaipc] cid=%u cmd=1 connect -> gr bridge\n",
                    cid);
            s->cid[cid].used = true;
            if (gr_render_op_connect(cid)) {
                return;    /* mbox ret/status + IRQ lands from the worker */
            }
            /* fall through to the legacy connect reply on bridge failure */
            stl_le_p(mb + 0x04, 0);
            stl_le_p(mb + 0x08, 0);
            pga_complete(s, cid);
            return;
        case PGAIPC_CMD_DISCONNECT:
            fprintf(stderr, "[bstpgaipc] cid=%u cmd=2 disconnect -> gr bridge\n",
                    cid);
            if (gr_render_op_disconnect(cid)) {
                return;
            }
            s->cid[cid].used = false;
            s->cid[cid].gfx = false;
            s->cid[cid].ident = false;
            s->cid[cid].dumps = 0;
            stl_le_p(mb + 0x04, 0);
            stl_le_p(mb + 0x08, 0);
            pga_complete(s, cid);
            return;
        case PGAIPC_CMD_WRITE:
            if (gr_render_op_request(cid, cmd, len)) {
                return;
            }
            break;         /* legacy write path below */
        case PGAIPC_CMD_READ:
            if (gr_render_op_request(cid, cmd, len)) {
                return;
            }
            break;         /* legacy read path below */
        case PGAIPC_CMD_HYPERCALL:
        case PGAIPC_CMD_HDIRECT:
            if (gr_render_op_request(cid, cmd, len)) {
                return;
            }
            break;
        default:
            break;
        }
    }

    switch (cmd) {
    case PGAIPC_CMD_CONNECT:
        /* HstHost: create the connection record; result = 0 */
        fprintf(stderr, "[bstpgaipc] cid=%u cmd=1 connect\n", cid);
        s->cid[cid].used = true;
        stl_le_p(mb + 0x04, 0);
        stl_le_p(mb + 0x08, 0);
        pga_complete(s, cid);
        return;

    case PGAIPC_CMD_DISCONNECT:
        fprintf(stderr, "[bstpgaipc] cid=%u cmd=2 disconnect\n", cid);
        s->cid[cid].used = false;
        s->cid[cid].gfx = false;
        s->cid[cid].ident = false;
        s->cid[cid].dumps = 0;
        stl_le_p(mb + 0x04, 0);
        stl_le_p(mb + 0x08, 0);
        pga_complete(s, cid);
        return;

    case PGAIPC_CMD_WRITE:
        if (!s->cid[cid].ident && len && len <= winlen) {
            s->cid[cid].ident = true;
            s->cid[cid].wr_first_len = len;
            s->cid[cid].gfx = pga_is_gfx(win, len);
            if (s->cid[cid].dumps < PGA_DUMP_MAX) {
                s->cid[cid].dumps++;
                pga_hexline(win, len, cid, 1);
            }
        } else if (s->cid[cid].dumps < PGA_DUMP_MAX) {
            s->cid[cid].dumps++;
            fprintf(stderr, "[bstpgaipc] cid=%u cmd=4 wo len=%u "
                    "(gfx=%d last=%u)\n", cid, len, s->cid[cid].gfx,
                    s->cid[cid].wr_first_len);
        }
        if (!s->cid[cid].gfx) {
            /* keep non-graphics clients (bstconf etc.) on the old
             * failure reply so bstsetconf.sh keeps working */
            stl_le_p(mb + 0x04, 0xffffffffu);
        }
        stl_le_p(mb + 0x08, 0);
        pga_complete(s, cid);
        return;

    case PGAIPC_CMD_READ:
        if (!s->cid[cid].gfx) {
            fprintf(stderr, "[bstpgaipc] cid=%u cmd=3 read want=%u "
                    "(not gfx -> failing)\n", cid, len);
            stl_le_p(mb + 0x04, 0xffffffffu);
            stl_le_p(mb + 0x08, 0);
            pga_complete(s, cid);
            return;
        }
        if (s->cid[cid].dumps < PGA_DUMP_MAX) {
            s->cid[cid].dumps++;
            fprintf(stderr, "[bstpgaipc] cid=%u cmd=3 read want=%u -> "
                    "zero-fill reply\n", cid, len);
        }
        if (len && len <= winlen) {
            memset(win, 0, len);
            stl_le_p(mb + 0x04, len);
        } else {
            stl_le_p(mb + 0x04, 0);
        }
        stl_le_p(mb + 0x08, 0);
        pga_complete(s, cid);
        return;

    case PGAIPC_CMD_HYPERCALL:
    case PGAIPC_CMD_HDIRECT:
        /* hpp doorbells: fire & forget, driver only checks status==0 */
        if (s->cid[cid].dumps < PGA_DUMP_MAX) {
            s->cid[cid].dumps++;
            fprintf(stderr, "[bstpgaipc] cid=%u cmd=%u hyp len=%u\n", cid,
                    cmd, len);
        }
        stl_le_p(mb + 0x04, cmd == PGAIPC_CMD_HYPERCALL ? 0 : ret);
        stl_le_p(mb + 0x08, 0);
        pga_complete(s, cid);
        return;

    default:
        fprintf(stderr, "[bstpgaipc] cid=%u UNKNOWN cmd=%u len=%u\n", cid,
                cmd, len);
        stl_le_p(mb + 0x08, 0);
        pga_complete(s, cid);
        return;
    }
}

static uint64_t bst_pgaipc_bar0_read(void *opaque, hwaddr addr, unsigned size)
{
    return 0;
}

static void bst_pgaipc_bar0_write(void *opaque, hwaddr addr, uint64_t val,
                                  unsigned size)
{
    BstPgaipcState *s = opaque;

    switch (addr) {
    case 0x00:
        pga_doorbell(s, (uint32_t)val);
        break;
    case 0x08:
        /*
         * IRQ ack.  Level-triggered INTx stays asserted until the guest has
         * serviced the doorbell, so drop the line here.
         */
        s->irq_asserted = false;
        pci_irq_deassert(PCI_DEVICE(s));
        break;
    default:
        break;
    }
}

static const MemoryRegionOps bst_pgaipc_bar0_ops = {
    .read = bst_pgaipc_bar0_read,
    .write = bst_pgaipc_bar0_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

static void bst_pgaipc_realize(PCIDevice *pdev, Error **errp)
{
    BstPgaipcState *s = BST_PGAIPC(pdev);
    static unsigned instance;
    uint8_t *pci_conf = pdev->config;

    pci_config_set_interrupt_pin(pci_conf, 1);

    memory_region_init_io(&s->bar0, OBJECT(s), &bst_pgaipc_bar0_ops, s,
                          "bstpgaipc-bar0", BST_PGAIPC_BAR0_SIZE);
    pci_register_bar(pdev, 0, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar0);

    snprintf(s->shm_name, sizeof(s->shm_name), "bstpgaipc-shm-%u", instance);
    if (!memory_region_init_ram(&s->bar1, OBJECT(s), s->shm_name,
                                BST_PGAIPC_BAR1_SIZE, errp)) {
        return;
    }
    pci_register_bar(pdev, 1, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar1);

    snprintf(s->mem_name, sizeof(s->mem_name), "bstpgaipc-mem-%u",
             instance++);
    if (!memory_region_init_ram(&s->bar2, OBJECT(s), s->mem_name,
                                BST_PGAIPC_BAR2_SIZE, errp)) {
        return;
    }
    pci_register_bar(pdev, 2, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar2);

    s->shm = memory_region_get_ram_ptr(&s->bar1);
    s->mem = memory_region_get_ram_ptr(&s->bar2);
    memset(s->shm, 0, BST_PGAIPC_BAR1_SIZE);
    memset(s->mem, 0, BST_PGAIPC_BAR2_SIZE);

    /* "no cid served" marker; the guest ISR only accepts values <= 0x86. */
    stl_le_p(s->shm + PGAIPC_SERVED_CID_REG, 0xffffffffu);

    /* Phase-2 render bridge hookups */
    for (unsigned i = 0; i <= BST_PGAIPC_MAX_CID; i++) {
        s->grctx[i].dev = s;
        s->grctx[i].cid = (uint32_t)i;
        s->gr_bh[i] = qemu_bh_new(gr_bh_complete, &s->grctx[i]);
    }
    gr_render_set_device_enabled(s->gr);
    gr_render_attach(s, &gr_host_ops);
    fprintf(stderr, "[bstpgaipc] gr render bridge %s\n",
            s->gr ? "ENABLED (prop gr=on)" : "off (prop gr=off; env may enable)");
}

static const Property bst_pgaipc_props[] = {
    DEFINE_PROP_BOOL("gr", BstPgaipcState, gr, false),
};

static void bst_pgaipc_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *k = PCI_DEVICE_CLASS(klass);

    k->realize = bst_pgaipc_realize;
    k->vendor_id = 0x1af4;
    k->device_id = 0x10f0;
    k->revision = 0;
    k->class_id = 0x0000;
    dc->desc = "BlueStacks bstpgaipc hypercall channel";
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
    device_class_set_props(dc, bst_pgaipc_props);
}

static const TypeInfo bst_pgaipc_types[] = {
    {
        .name          = TYPE_BST_PGAIPC,
        .parent        = TYPE_PCI_DEVICE,
        .instance_size = sizeof(BstPgaipcState),
        .class_init    = bst_pgaipc_class_init,
        .interfaces = (const InterfaceInfo[]) {
            { INTERFACE_CONVENTIONAL_PCI_DEVICE },
            { },
        },
    },
};

DEFINE_TYPES(bst_pgaipc_types)
