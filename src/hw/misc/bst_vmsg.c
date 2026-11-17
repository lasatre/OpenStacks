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

#define TYPE_BST_VMSG "bstvmsg"
OBJECT_DECLARE_SIMPLE_TYPE(BstVmsgState, BST_VMSG)

#define BST_VMSG_BAR0_SIZE      0x1000
#define BST_VMSG_BAR1_SIZE      0x100000

#define VMSG_SHM_MAX            0x00
#define VMSG_SHM_CID            0x04
#define VMSG_SHM_GUEST_OFF      0x08
#define VMSG_SHM_HOST_OFF       0x0c
#define VMSG_SHM_PARA1          0x10
#define VMSG_SHM_PARA2          0x14
#define VMSG_SHM_PENDING_OFF    0x18
#define VMSG_SHM_IRQ_PENDING    0x1c

#define VMSG_MAX_CONNECTIONS    8
#define VMSG_IN_SIZE            0x1000u
#define VMSG_OUT_SIZE           0x10000u
#define VMSG_IN_ALIGN           0x1000u
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

typedef struct BstVmsgState {
    PCIDevice parent_obj;

    MemoryRegion bar0;
    MemoryRegion bar1;
    char shm_name[64];

    uint32_t max_connections;
    BstVmsgConn conn[VMSG_MAX_CONNECTIONS];
    uint8_t *shm;
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
    "bst.device_carrier_code=310_260\n";

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
    size_t props_len = sizeof(bst_vmsg_props_text) - 1;

    if (c->guest_off + VMSG_IN_SIZE > BST_VMSG_BAR1_SIZE ||
        c->host_off + c->out_size > BST_VMSG_BAR1_SIZE) {
        return;
    }

    in = s->shm + c->guest_off;
    out = s->shm + c->host_off;
    method = ldl_le_p(in + 0x00);
    buf_len = ldl_le_p(in + 0x08);

    fprintf(stderr, "[bstvmsg] processRequest cid=%u goff=0x%x hoff=0x%x "
            "method=%u msglen=%u bufLen=%u d3=%u tail=%u\n", c - s->conn,
            c->guest_off, c->host_off, method, ldl_le_p(in + 0x04),
            buf_len, ldl_le_p(in + 0x0c), ldl_le_p(in + 0x10));

    if (method == 1 && buf_len != 0 && buf_len <= (VMSG_OUT_SIZE - 0x100u)) {
        memset(out, 0, c->out_size);
        resp_len = (uint32_t)MIN(props_len, buf_len);
        memcpy(out, bst_vmsg_props_text, resp_len);
        stl_le_p(out + buf_len, resp_len);
        stl_le_p(out + buf_len + 4, 0);
        fprintf(stderr, "[bstvmsg] served GetProps respLen=%u -> hoff 0x%x\n",
                resp_len, c->host_off);
    } else if (buf_len != 0 && buf_len + 8 <= c->out_size) {
        stl_le_p(out + buf_len, 0);
        stl_le_p(out + buf_len + 4, 0xffffffffu);
        fprintf(stderr, "[bstvmsg] rejected method=%u -> result=-1\n", method);
    }
    /* unblock the waiting guest ioctl */
    stl_le_p(s->shm + c->poll_off, 0);
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
    stl_le_p(s->shm + VMSG_SHM_IRQ_PENDING, 0);
    stl_le_p(s->shm + VMSG_SHM_CID, 0xffffffffu);
}

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
