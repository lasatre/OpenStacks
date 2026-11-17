/*
 * BlueStacks "vbox-vmmdev" - a minimal VirtualBox-style VMMDev PCI device.
 *
 * Emulates the VMMDev PCI function (vendor 0x80EE, device 0xCAFE) that
 * BlueStacks' Linux guest-additions kernel modules (VBox 6.1.36 guest core)
 * match on and probe.
 *
 * BAR0: I/O ports, 0x10 bytes.  The module performs every VMMDev request with
 *   out Dx, Eax    (Dx = IOPortBase, EAX = guest phys addr of the header)
 * and expects req.rc (offset 0x0c) to become != VERR_INTERNAL_ERROR.
 *
 * BAR1: MMIO, 2 MiB, reserved + ioremap'd by the module.
 *
 * HGCM layer (what this release adds, making vboxsf.ko load):
 *   Every request is answered with rc (header+0x0c) = 0 (VINF_SUCCESS),
 *   written back into guest RAM synchronously in the port-write handler,
 *   exactly like the previous working build.  The HGCM sub-protocol
 *   additionally gets the fields the guest defines in its request filled
 *   (layout == drivers/virt/vboxguest/vmmdev.h == include/VBox/vmmdev.h):
 *
 *     HGCM request sub-header (total 32 bytes incl. the generic header):
 *       +0x18 u32 fu32Flags   - host sets VBOX_HGCM_REQ_DONE (bit 0)
 *       +0x1c s32 result      - host writes 0 (VINF_SUCCESS; this was left
 *                               at the guest's 0xAAAAAAAA filler before,
 *                               which surfaced as rc=-1431655766)
 *
 *     VMMDevHGCMConnect (req 60 = 0x3c, size 0xa8):
 *       +0x20 u32 loc.type (+0x24 char name[128])  IN
 *       +0xa4 u32 client_id                        OUT (assigned here)
 *
 *     VMMDevHGCMDisconnect (req 61 = 0x3d, size 0x24):
 *       +0x20 u32 client_id                        (no-op, result 0)
 *
 *     VMMDevHGCMCall (req 63 = 0x3f Call64 / 62 = 0x3e Call32):
 *       +0x20 u32 client_id
 *       +0x24 u32 function
 *       +0x28 u32 cParms
 *       +0x2c HGCMFunctionParameter64[ i ]  (16 bytes each, packed(4):
 *            { u32 type; u32 value32; u64 value64/ptr @ +4 }, where for
 *            LinAddr pointers: u32 size @ +4, u64 guest-linear/phys @ +8)
 *       Reply: result = 0, REQ_DONE set, plus per-function synthesized OUT
 *       value parameters for the shared-folders svc (include/VBox/shflsvc.h):
 *         SHFL_FN_QUERY_FEATURES (25): p0.value64 = 0 (features),
 *                                      p1.value32 = 29 (SHFL_FN_LAST)
 *         SHFL_FN_QUERY_MAPPINGS (1):  p1.value32 = 0  (no mappings yet)
 *       LinAddr_Out (type 6) buffers are zero-filled through the guest's
 *       page tables (cpu_memory_rw_debug), so OUT data starts defined.
 *
 * Guest Property Service (this release): "VBoxGuestPropSvc" (client 1) is
 * served with the VBox 7.0.8 guest-side protocol (DecoderComments: live wire
 * capture + include/VBox/HostServices/GuestPropertySvc.h + the observed
 * VGDrvCommonProcessOptionsFromHost boot-time call in vboxguest.ko):
 *
 *     GET_PROP (1):     p0 name LinAddr | p1 reply-buffer LinAddr
 *                       | p2 u64 timestamp OUT | p3 u32 size OUT
 *     reply buffer     = "value" NUL "flags" NUL (two strings back to back)
 *     SET_PROP (2):     p0 name | p1 value | p2 flags        (no-op here, rc=0)
 *     SET_VALUE (3):    p0 name | p1 value                    (no-op, rc=0)
 *     DEL_PROP (4):     p0 name                               (no-op, rc=0)
 *     ENUM_PROPS (5):   p0 patterns LinAddr ('|'/NUL-separated, "szz" =
 *                       double-NUL terminated) | p1 strings buffer LinAddr
 *                       | p2 u32 size OUT
 *     strings buffer   = per property:  name NUL value NUL ts-hex NUL flags
 *                       NUL ... finally one empty-string terminator NUL
 *     GET_NOTIFY (6):   answered VERR_NOT_FOUND (nobody ever notifies -
 *                       avoids blocking the synchronous HGCM wait)
 *
 *   Status codes are IPRT values: VINF_SUCCESS=0, VERR_BUFFER_OVERFLOW=-41,
 *   VERR_NOT_FOUND=-78, VERR_INVALID_PARAMETER=-2, VERR_NOT_IMPLEMENTED=-12.
 *   VERR_BUFFER_OVERFLOW + size=needed is handled by the guest kernel core
 *   (it doubles its buffer and retries - observed in the disassembly).
 *   Property flags served = "RDONLYGUEST" (the boot-time options reader in
 *   the driver only accepts properties which have that flag).
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/module.h"
#include "qemu/units.h"
#include "hw/pci/pci.h"
#include "hw/pci/pci_device.h"
#include "qom/object.h"
#include "qemu/bswap.h"
#include "system/memory.h"
#include "system/address-spaces.h"

/*
 * Forward declarations for guest-LINEAR-address access (LinAddr params
 * carry guest kernel virtual addresses).  These come from hw/core/cpu.h
 * which cannot be included from a device in this build, so declare only
 * what is needed; the signatures match QEMU's own exactly.
 */
typedef struct CPUState CPUState;
extern __thread CPUState *current_cpu;
extern int cpu_memory_rw_debug(CPUState *cpu, uint64_t addr, void *ptr,
                               size_t len, bool is_write);

#define TYPE_VMMDEV "vbox-vmmdev"
OBJECT_DECLARE_SIMPLE_TYPE(VMMDevState, VMMDEV)

#define VMMDEV_IO_SIZE     0x10
#define VMMDEV_MMIO_SIZE   (2 * MiB)
#define VMMDEV_REQ_RC_OFF  0x0c

/* ------------------------------------------------------------------ */
/* VBox / VMMDev protocol definitions (include/VBox/vmmdev.h)          */
/* ------------------------------------------------------------------ */

/* Generic request header (24 bytes). */
#define VR_OFF_TYPE      0x08
#define VR_OFF_REQUESTOR 0x14

/* Request types used here. */
#define VREQ_GET_HOST_VERSION     4
#define VREQ_ACKNOWLEDGE_EVENTS   41
#define VREQ_HGCM_CONNECT         60
#define VREQ_HGCM_DISCONNECT      61
#define VREQ_HGCM_CALL32          62
#define VREQ_HGCM_CALL64          63
#define VREQ_HGCM_CANCEL          64
#define VREQ_HGCM_CANCEL2         65

/* HGCM sub-header. */
#define HG_OFF_FLAGS  0x18           /* u32 fu32Flags */
#define HG_OFF_RESULT 0x1c           /* s32 result (VBox status) */
#define HG_FLAG_REQ_DONE 0x00000001  /* VBOX_HGCM_REQ_DONE */

/* VMMDevHGCMConnect. */
#define HC_OFF_LOCATION 0x20         /* { u32 type; char name[128]; } */
#define HC_OFF_CLIENT   0xa4

/* VMMDevHGCMDisconnect. */
#define HD_OFF_CLIENT 0x20

/* VMMDevHGCMCall. */
#define VC_OFF_FUNCTION 0x24
#define VC_OFF_CPARMS   0x28
#define VC_OFF_PARMS    0x2c
#define VC_PARM64_SIZE  16
#define VC_PARM32_SIZE  12

/* HGCM parameter types (numbering used by this guest build). */
#define HP_32BIT        1
#define HP_64BIT        2
#define HP_LINADDR      4     /* guest linear address, in+out */
#define HP_LINADDR_IN   5     /* guest linear address, in  */
#define HP_LINADDR_OUT  6     /* guest linear address, out */
#define HP_PAGE_LIST    10    /* phys page list descriptor inside request */

/* Shared folders service functions (include/VBox/shflsvc.h). */
#define SHFL_FN_QUERY_MAPPINGS  1
#define SHFL_FN_MAP_FOLDER      17
#define SHFL_FN_QUERY_FEATURES  25
#define SHFL_FN_LAST            29

#define MAX_HGCM_PARMS     32
#define MAX_ZERO_FILL      (64 * KiB)  /* safety cap for LinAddr_Out fill */
#define MAX_CLIENT_TRACKED 16

/* ------------------------------------------------------------------ */

typedef struct VMMDevState {
    PCIDevice parent_obj;

    MemoryRegion io;
    MemoryRegion mmio;
    char         io_name[64];
    char         mmio_name[64];

    /* HGCM client registry for logging: client id -> service name */
    uint32_t next_client_id;
    char     client_service[MAX_CLIENT_TRACKED][52];
} VMMDevState;

static const char *vmmdev_client_service(VMMDevState *s, uint32_t client)
{
    if (s && client > 0 && client < MAX_CLIENT_TRACKED &&
        s->client_service[client][0])
        return s->client_service[client];
    return "?";
}

/* little-endian guest-phys accessors; ALL access goes through
 * address_space_memory (bus_master_as reads ZEROS in this build). */
static void gp_write_u32(AddressSpace *as, hwaddr p, uint32_t v)
{
    v = cpu_to_le32(v);
    address_space_write(as, p, MEMTXATTRS_UNSPECIFIED, &v, 4);
}

static void gp_write_u64(AddressSpace *as, hwaddr p, uint64_t v)
{
    v = cpu_to_le64(v);
    address_space_write(as, p, MEMTXATTRS_UNSPECIFIED, &v, 8);
}

static uint32_t gp_read_u32(AddressSpace *as, hwaddr p)
{
    uint32_t v = 0;
    address_space_read(as, p, MEMTXATTRS_UNSPECIFIED, &v, 4);
    return le32_to_cpu(v);
}

static uint64_t gp_read_u64(AddressSpace *as, hwaddr p)
{
    uint64_t v = 0;
    address_space_read(as, p, MEMTXATTRS_UNSPECIFIED, &v, 8);
    return le64_to_cpu(v);
}

/* Mark HGCM request complete: result @+0x1c and REQ_DONE bit @+0x18. */
static void hgcm_complete(AddressSpace *as, hwaddr gp, int32_t result)
{
    uint32_t flags;

    gp_write_u32(as, gp + HG_OFF_RESULT, (uint32_t)result);
    flags = gp_read_u32(as, gp + HG_OFF_FLAGS);
    if ((flags & HG_FLAG_REQ_DONE) == 0)
        gp_write_u32(as, gp + HG_OFF_FLAGS, flags | HG_FLAG_REQ_DONE);
}

/* Zero a guest linear range (any pages) using the current CPU's MMU.
 * Returns bytes actually cleared (0 if no CPU context / translation fail). */
static uint64_t hgcm_guest_linear_zero(uint64_t gva, uint64_t cb)
{
    static uint8_t zeros[4096];
    CPUState *cpu = current_cpu;
    uint64_t done = 0;

    if (cpu == NULL)
        return 0;
    while (done < cb) {
        uint64_t togo = 4096 - (gva & 0xfff);
        size_t len;
        if (togo > cb - done)
            togo = cb - done;
        if (togo == 0)
            togo = 4096;
        len = togo < sizeof(zeros) ? togo : sizeof(zeros);
        if (cpu_memory_rw_debug(cpu, gva + done, zeros, len, true) < 0)
            break;
        done += len;
    }
    return done;
}

/* bounded hexdump of guest memory for the boot log */
static void gp_dump(AddressSpace *as, hwaddr gp, uint32_t cb)
{
    uint8_t buf[0xb0];
    uint32_t i, n = cb > sizeof(buf) ? sizeof(buf) : cb;

    address_space_read(as, gp, MEMTXATTRS_UNSPECIFIED, buf, n);
    fprintf(stderr, "[vmmdev]   request bytes:");
    for (i = 0; i < n; i++) {
        if ((i % 16) == 0)
            fprintf(stderr, "\n  %06llx:", (unsigned long long)i);
        fprintf(stderr, " %02x", buf[i]);
    }
    fprintf(stderr, "\n");
}

/* ---------------------- HGCM Connect ------------------------------
 * { hdr 0x18 | flags/result | loc{type;name[128]} @+0x20 | client @+0xa4 }.
 * Reply: result=0, REQ_DONE, client_id assigned.  The guest core
 * (vbg_hgcm_connect) reads client_id @+0xa4 and header.result after the
 * synchronous submit:  rc=-1431655766 (0xAAAAAAAA) previously meant the
 * result field was never written by the host.
 */
static void hgcm_do_connect(VMMDevState *s, AddressSpace *as, hwaddr gp,
                            uint32_t size)
{
    char name[130];
    uint32_t loc_type;
    uint32_t client;
    size_t i;

    loc_type = gp_read_u32(as, gp + HC_OFF_LOCATION);
    memset(name, 0, sizeof(name));
    address_space_read(as, gp + HC_OFF_LOCATION + 4, MEMTXATTRS_UNSPECIFIED,
                       name, 128);

    for (i = 0; i < sizeof(name) - 1; i++)
        if ((name[i] != 0) && (name[i] < 0x20))
            name[i] = '.';
    if (!name[0])
        snprintf(name, sizeof(name), "(unnamed)");

    client = s->next_client_id++;
    if (client == 0 || client == 0xffffffffu) {
        client = 1;
        s->next_client_id = 2;
    }
    if (client < MAX_CLIENT_TRACKED) {
        memset(s->client_service[client], 0, sizeof(s->client_service[0]));
        strncpy(s->client_service[client], name,
                sizeof(s->client_service[0]) - 1);
    }

    hgcm_complete(as, gp, 0 /* VINF_SUCCESS */);
    gp_write_u32(as, gp + HC_OFF_CLIENT, client);

    fprintf(stderr, "[vmmdev] HGCMConnect loc=%u svc='%s' size=%u -> client=%u\n",
            loc_type, name, size, client);
}

/* ---------------------- HGCM Disconnect --------------------------- */
static void hgcm_do_disconnect(AddressSpace *as, hwaddr gp)
{
    uint32_t client = gp_read_u32(as, gp + HD_OFF_CLIENT);

    hgcm_complete(as, gp, 0);
    fprintf(stderr, "[vmmdev] HGCMDisconnect client=%u (ok)\n", client);
}

/* ================================================================== */
/* Guest Property Service ("VBoxGuestPropSvc", client_id 1)           */
/* ================================================================== */

#define GPROP_FN_GET_PROP         1
#define GPROP_FN_SET_PROP         2
#define GPROP_FN_SET_PROP_VALUE   3
#define GPROP_FN_DEL_PROP         4
#define GPROP_FN_ENUM_PROPS       5
#define GPROP_FN_GET_NOTIFICATION 6

/* iprt/err.h status codes used on this service. */
#define GP_VINF_SUCCESS           0
#define GP_VERR_INVALID_PARAMETER (-2)
#define GP_VERR_NOT_IMPLEMENTED   (-12)
#define GP_VERR_BUFFER_OVERFLOW   (-41)
#define GP_VERR_NOT_FOUND         (-78)
#define GP_VWRN_NOT_FOUND         78

/* Flags served for every property.  The boot-time options reader in the
 * guest driver (VGDrvCommonProcessOptionsFromHost) only accepts properties
 * whose flags contain RDONLYGUEST ("Ignoring '%s' as it does not have
 * RDONLYGUEST set"). */
#define GPROP_FLAGS_STR "RDONLYGUEST"

/* Fixed base timestamp (ns-scale like the real service); bumped per call. */
static uint64_t g_gprop_ts = 1725302500000000000ULL; /* 0x17e9... hex string */

/* GUEST_PROP_MAX_NAME_LEN is 64 including the terminator; bound reads. */
#define GPROP_NAME_MAX  64
#define GPROP_PAT_MAX   256
#define GPROP_OUT_MAX   (16 * KiB)

typedef struct GPropEntry {
    const char *name;
    const char *value;
} GPropEntry;

/*
 * Property table.  Values mirror what a real BlueStacks 5.21.550Pie64
 * instance feeds its guest:
 *  - graphics engine/renderer: this machine's own bluestacks.conf
 *    (bst.instance.Pie64.graphics_engine="aga", graphics_renderer="vlcn").
 *  - identity/serial/imsi/...: REA_BSTCONF.md section 6 (identical to the
 *    'hcal' props text served by bst_vmsg.c).
 *  - mem pcd/pcr: bst.instance.Pie64.mem_pcd_* / mem_pcr_* values.
 * Anything the guest asks for that is not in this table answers
 * VERR_NOT_FOUND, which the guest treats as "property not set".
 */
static const GPropEntry g_gprop_table[] = {
    { "bst.graphics_engine",   "aga" },
    { "bst.graphics_renderer", "vlcn" },
    { "bst.android.id",        "bfd0e87474aa4028" },
    { "bst.serialno",          "cge1f98585bb" },
    { "bst.imei_id",           "531090189194022" },
    { "bst.device_hash",       "0503010A090A0108090B090E0A0C0209" },
    { "bst.sim_issuer_id",     "221" },
    { "bst.sim_suffix",        "134261319" },
    { "bst.subscriber_id",     "410134261319" },
    { "bst.wifi_mac_addr",     "31:A9:A1:89:B9:EA" },
    { "bst.bssid_mac_addr",    "1A:9A:18:9B:9E:AC" },
    { "bst.bluetooth_addr",    "64:2B:0B:29:0C:0F" },
    { "bst.device_carrier_code", "310_260" },
    { "bst.mem_pcd_enabled",   "0" },
    { "bst.mem_pcd_pclimit",   "40" },
    { "bst.mem_pcr_enabled",   "0" },
    { "bst.mem_pcr_pclimit",   "48" },
};
#define GPROP_TABLE_N (sizeof(g_gprop_table) / sizeof(g_gprop_table[0]))

/* ---- guest LINEAR address data access (LinAddr params hold GVAs) ----
 * Same cpu_memory_rw_debug(current_cpu) route the LinAddr_Out zero-fill
 * uses (extern-declared, no cpu.h include). */

static uint64_t hgcm_gva_read(uint64_t gva, void *buf, uint32_t cb)
{
    CPUState *cpu = current_cpu;
    uint64_t done = 0;

    if (cpu == NULL || cb == 0)
        return 0;
    while (done < cb) {
        uint64_t togo = 4096 - ((gva + done) & 0xfff);
        size_t len;
        if (togo > cb - done)
            togo = cb - done;
        if (togo == 0)
            togo = 4096;
        len = togo < 2048 ? (size_t)togo : 2048;
        if (cpu_memory_rw_debug(cpu, gva + done, (void *)((uint8_t *)buf + done),
                                len, false) < 0)
            break;
        done += len;
    }
    return done;
}

static uint64_t hgcm_gva_write(uint64_t gva, const void *buf, uint32_t cb)
{
    static uint8_t tmp[2048] __attribute__((aligned(4096)));
    CPUState *cpu = current_cpu;
    uint64_t done = 0;
    const uint8_t *src = buf;

    if (cpu == NULL || cb == 0)
        return 0;
    while (done < cb) {
        uint64_t togo = 4096 - ((gva + done) & 0xfff);
        size_t len;
        if (togo > cb - done)
            togo = cb - done;
        if (togo == 0)
            togo = 4096;
        len = togo < sizeof(tmp) ? togo : sizeof(tmp);
        memcpy(tmp, src + done, len);
        if (cpu_memory_rw_debug(cpu, gva + done, tmp, len, true) < 0)
            break;
        done += len;
    }
    return done;
}

/* Read a potentially-not-terminated string (name or patterns), cap it,
 * force NUL. Returns bytes actually read. */
static uint32_t hgcm_gva_read_cstr(uint64_t gva, uint32_t cb, char *buf,
                                   uint32_t cap)
{
    uint32_t rd, i;

    memset(buf, 0, cap);
    if (cb > cap - 1)
        cb = cap - 1;
    if (cb == 0)
        return 0;
    rd = (uint32_t)hgcm_gva_read(gva, buf, cb);
    /* stop at first embedded NUL (keep read short & deterministic) */
    for (i = 0; i < rd; i++) {
        if (buf[i] == '\0') {
            return i + 1;
        }
    }
    buf[rd] = '\0';
    return rd;
}

/* Single-'*' / '?' glob (VBox guest property pattern subset). */
static bool gprop_glob_match(const char *pat, const char *name)
{
    while (*pat) {
        if (*pat == '*') {
            pat++;
            if (*pat == '\0')
                return true;
            while (*name) {
                if (gprop_glob_match(pat, name))
                    return true;
                name++;
            }
            return false;
        }
        if (*name == '\0')
            return false;
        if (*pat != '?' && *pat != *name)
            return false;
        pat++;
        name++;
    }
    return *name == '\0';
}

/*
 * Match a property name against the patterns buffer.  The buffer is a
 * '|' separated pattern list; NUL is accepted as a separator too, and the
 * list conventionally ends with a double NUL ("szz" - as in the observed
 * boot call: "/VirtualBox/GuestAdd/VBoxGuest/" plus "\0\0").  We therefore
 * stop at the first EMPTY separator once at least one pattern was seen, so
 * whatever bytes follow the terminator (guest .rodata neighbors) are never
 * parsed as patterns.  Zero patterns => match everything.
 */
static bool gprop_name_matches(const char *pats, uint32_t cbpats,
                               const char *name)
{
    char tok[GPROP_PAT_MAX + 1];
    bool any_pat = false;
    uint32_t i, start = 0;

    if (cbpats == 0)
        return true;
    for (i = 0; i <= cbpats; i++) {
        bool brk = (i == cbpats) || pats[i] == '|' || pats[i] == '\0';
        if (brk) {
            uint32_t len = i - start;
            if (len > 0 && len < sizeof(tok)) {
                memcpy(tok, pats + start, len);
                tok[len] = '\0';
                any_pat = true;
                if (gprop_glob_match(tok, name))
                    return true;
            } else if (len == 0 && any_pat) {
                break;  /* double-NUL terminator */
            }
            start = i + 1;
        }
    }
    /* no (non-empty) patterns at all means match everything */
    return !any_pat;
}

static const GPropEntry *gprop_find(const char *name)
{
    unsigned i;

    if (name[0] == '\0')
        return NULL;
    for (i = 0; i < GPROP_TABLE_N; i++) {
        if (strcmp(g_gprop_table[i].name, name) == 0)
            return &g_gprop_table[i];
    }
    return NULL;
}

static const char *gprop_func_name(uint32_t func)
{
    switch (func) {
    case GPROP_FN_GET_PROP:         return "GET";
    case GPROP_FN_SET_PROP:         return "SET";
    case GPROP_FN_SET_PROP_VALUE:   return "SET_VALUE";
    case GPROP_FN_DEL_PROP:         return "DEL";
    case GPROP_FN_ENUM_PROPS:       return "ENUM";
    case GPROP_FN_GET_NOTIFICATION: return "GET_NOTIFY";
    default:                        return "?";
    }
}

/* Build one enum entry "name\0value\0ts-hex\0flags\0" into out. */
static uint32_t gprop_enum_entry(char *out, uint32_t outcap, uint32_t opos,
                                 const GPropEntry *e, uint64_t ts,
                                 unsigned *pnmatched)
{
    char tsbuf[24];
    const char *pieces[6];
    uint32_t lens[6];
    uint32_t need = 0;
    unsigned j, k;

    snprintf(tsbuf, sizeof(tsbuf), "%llx", (unsigned long long)ts);
    pieces[0] = e->name;      pieces[1] = e->value;
    pieces[2] = tsbuf;        pieces[3] = GPROP_FLAGS_STR;
    for (j = 0; j < 4; j++)
        lens[j] = (uint32_t)strlen(pieces[j]) + 1;
    for (j = 0; j < 4; j++)
        need += lens[j];
    if (opos + need > outcap)
        return opos;
    for (k = 0; k < 4; k++) {
        memcpy(out + opos, pieces[k], lens[k]);
        opos += lens[k];
    }
    (*pnmatched)++;
    return opos;
}

/*
 * Serve one HGCMCall for the guest property service.  Every branch ends
 * with hgcm_complete() (which writes the rc @+0x1c +REQ_DONE @+0x18) so the
 * guest never sees the 0xAAAAAAAA unfilled reply again.
 */
static void hgcm_do_guestprop(AddressSpace *as, hwaddr gp, uint32_t parm_size,
                              hwaddr pbase, uint32_t client, uint32_t func,
                              uint32_t cparms, bool f64)
{
    /* throttled instrumentation: full detail once per (call-kind, func) */
    static uint8_t gprop_seen;
    uint8_t seen_bit = (uint8_t)(f64 ? 0x20 : 0x10) | (uint8_t)(func & 0x0f);
    bool detail = (gprop_seen & seen_bit) == 0;
    uint32_t i;

    if (detail)
        gprop_seen |= seen_bit;

    fprintf(stderr, "[vmmdev] gprop %s func=%u (%s) cparms=%u client=%u%s\n",
            f64 ? "call64" : "call32", func, gprop_func_name(func), cparms,
            client, detail ? " (first call, full detail below)" : "");
    if (detail) {
        for (i = 0; i < cparms && i < MAX_HGCM_PARMS; i++) {
            hwaddr p = pbase + (hwaddr)i * parm_size;
            uint32_t type = gp_read_u32(as, p);
            if (type == HP_32BIT)
                fprintf(stderr, "[vmmdev]   parm[%u] t=1 v=0x%x\n", i,
                        gp_read_u32(as, p + 4));
            else if (type == HP_64BIT)
                fprintf(stderr, "[vmmdev]   parm[%u] t=2 v=0x%llx\n", i,
                        (unsigned long long)gp_read_u64(as, p + 4));
            else if (type >= HP_LINADDR && type <= HP_LINADDR_OUT)
                fprintf(stderr, "[vmmdev]   parm[%u] t=%u cb=%u gva=0x%llx\n",
                        i, type, gp_read_u32(as, p + 4),
                        (unsigned long long)gp_read_u64(as, p + 8));
            else
                fprintf(stderr, "[vmmdev]   parm[%u] t=%u ?\n", i, type);
        }
    }

    switch (func) {
    case GPROP_FN_GET_PROP: {
        hwaddr p0 = pbase, p1 = pbase + parm_size;
        hwaddr p2 = pbase + 2 * parm_size, p3 = pbase + 3 * parm_size;
        char name[GPROP_NAME_MAX + 8];
        const GPropEntry *e;
        uint32_t cb_name, cb_buf, need, rd, wlen;
        uint64_t gva_buf;
        char rep[1400];

        if (cparms < 4) {
            hgcm_complete(as, gp, GP_VERR_INVALID_PARAMETER);
            return;
        }
        cb_name = gp_read_u32(as, p0 + 4);
        rd = hgcm_gva_read_cstr(gp_read_u64(as, p0 + 8), cb_name,
                                name, sizeof(name) - 1);
        name[rd < sizeof(name) - 1 ? rd : sizeof(name) - 2] = '\0';

        e = gprop_find(name);
        if (e == NULL) {
            fprintf(stderr, "[vmmdev] gprop GET '%s' -> NOT_FOUND\n", name);
            hgcm_complete(as, gp, GP_VERR_NOT_FOUND);
            return;
        }
        cb_buf = gp_read_u32(as, p1 + 4);
        gva_buf = gp_read_u64(as, p1 + 8);
        /* reply = value NUL flags NUL */
        need = (uint32_t)strlen(e->value) + 1 + strlen(GPROP_FLAGS_STR) + 1;
        if (cb_buf < need) {
            gp_write_u32(as, p3 + 4, need);
            fprintf(stderr, "[vmmdev] gprop GET '%s' -> OVERFLOW (need=%u have=%u)\n",
                    name, need, cb_buf);
            hgcm_complete(as, gp, GP_VERR_BUFFER_OVERFLOW);
            return;
        }
        memset(rep, 0, (size_t)need);
        /* strlen(value) bounded by table data (all tables ups 64B value) */
        memcpy(rep, e->value, strlen(e->value));
        memcpy(rep + strlen(e->value) + 1, GPROP_FLAGS_STR,
               strlen(GPROP_FLAGS_STR));
        wlen = (uint32_t)hgcm_gva_write(gva_buf, rep, need);

        gp_write_u64(as, p2 + 4, g_gprop_ts);
        gp_write_u32(as, p3 + 4, need);
        hgcm_complete(as, gp, GP_VINF_SUCCESS);
        fprintf(stderr, "[vmmdev] gprop GET '%s' -> '%s' (%u B written, rc=0)\n",
                name, e->value, wlen);
        return;
    }

    case GPROP_FN_SET_PROP:
    case GPROP_FN_SET_PROP_VALUE:
        /* read-only-from-guest is not enforced here; accept and ignore. */
        hgcm_complete(as, gp, GP_VINF_SUCCESS);
        fprintf(stderr, "[vmmdev] gprop SET noop (rc=0)\n");
        return;

    case GPROP_FN_DEL_PROP:
        hgcm_complete(as, gp, GP_VINF_SUCCESS);
        fprintf(stderr, "[vmmdev] gprop DEL noop (rc=0)\n");
        return;

    case GPROP_FN_ENUM_PROPS: {
        hwaddr p0 = pbase, p1 = pbase + parm_size, p2 = pbase + 2 * parm_size;
        char pats[GPROP_PAT_MAX + 8];
        char out[GPROP_OUT_MAX];
        uint32_t cbp, opos = 0, nm = 0, total, rd, wlen;
        int32_t rc = GP_VINF_SUCCESS;
        uint64_t gva_out;
        unsigned i2;

        if (cparms < 3) {
            hgcm_complete(as, gp, GP_VERR_INVALID_PARAMETER);
            return;
        }
        cbp = gp_read_u32(as, p0 + 4);
        if (cbp > GPROP_PAT_MAX)
            cbp = GPROP_PAT_MAX;
        /* raw read: patterns may be '|' separated and/or szz (double NUL). */
        rd = (uint32_t)hgcm_gva_read(gp_read_u64(as, p0 + 8), pats, cbp);
        pats[rd] = '\0';

        memset(out, 0, sizeof(out));
        for (i2 = 0; i2 < GPROP_TABLE_N; i2++) {
            if (!gprop_name_matches(pats, rd, g_gprop_table[i2].name))
                continue;
            opos = gprop_enum_entry(out, sizeof(out) - 2, opos,
                                    &g_gprop_table[i2], ++g_gprop_ts, &nm);
        }
        /* terminating empty string */
        out[opos++] = '\0';
        total = opos;
        gva_out = gp_read_u64(as, p1 + 8);
        if (gp_read_u32(as, p1 + 4) < total) {
            gp_write_u32(as, p2 + 4, total);
            rc = GP_VERR_BUFFER_OVERFLOW;
            wlen = 0;
        } else {
            wlen = (uint32_t)hgcm_gva_write(gva_out, out, total);
            gp_write_u32(as, p2 + 4, total);
        }
        hgcm_complete(as, gp, rc);
        fprintf(stderr,
                "[vmmdev] gprop ENUM '%s' -> matched=%u total=%u B written=%u rc=%d\n",
                pats, nm, total, wlen, rc);
        return;
    }

    case GPROP_FN_GET_NOTIFICATION:
        /* Nobody ever produces notifications in this emulation; fail the
         * wait immediately so the guest never blocks on it. */
        hgcm_complete(as, gp, GP_VERR_NOT_FOUND);
        fprintf(stderr, "[vmmdev] gprop GET_NOTIFY -> NOT_FOUND\n");
        return;

    default:
        hgcm_complete(as, gp, GP_VERR_NOT_IMPLEMENTED);
        fprintf(stderr, "[vmmdev] gprop func=%u -> NOT_IMPLEMENTED\n", func);
        return;
    }
}

/* ---------------------- HGCM Call --------------------------------- */
static void hgcm_do_call(VMMDevState *s, AddressSpace *as, hwaddr gp,
                         uint32_t size, bool f64)
{
    uint32_t client, func, cparms, parm_size;
    hwaddr pbase;
    uint32_t i, nlogged = 0;

    client = gp_read_u32(as, gp + 0x20);
    func   = gp_read_u32(as, gp + VC_OFF_FUNCTION);
    cparms = gp_read_u32(as, gp + VC_OFF_CPARMS);
    if (cparms > MAX_HGCM_PARMS)
        cparms = MAX_HGCM_PARMS;
    (void)size;

    parm_size = f64 ? VC_PARM64_SIZE : VC_PARM32_SIZE;
    pbase = gp + VC_OFF_PARMS;

    /*
     * "VBoxGuestPropSvc" (client 1 in the boot stream): serve the guest
     * property protocol (GET/SET/ENUM...).  Everything else below keeps
     * the shared-folders behavior.
     */
    if (client && strcmp(vmmdev_client_service(s, client),
                         "VBoxGuestPropSvc") == 0) {
        hgcm_do_guestprop(as, gp, parm_size, pbase, client, func, cparms, f64);
        return;
    }

    /* Generic reply: result = 0 (VINF_SUCCESS) + REQ_DONE. */
    hgcm_complete(as, gp, 0);

    /*
     * Synthesized OUT value parameters for the SHFL service calls the boot
     * performs.  Everything else keeps the guest's pre-filled zeros as its
     * OUT value params (i.e. NIL handles / zero counts), which is a valid
     * "no mappings / no special features" answer for the load path.
     */
    if (func == SHFL_FN_QUERY_FEATURES) {
        if (cparms >= 2) {
            /* p0: u64 f64Features -> 0 (no special features) */
            gp_write_u64(as, pbase + 4, 0);
            /* p1: u32 u32LastFunction -> SHFL_FN_LAST (29) */
            gp_write_u32(as, pbase + parm_size + 4, SHFL_FN_LAST);
        }
        fprintf(stderr, "[vmmdev] SHFL_FN_QUERY_FEATURES -> features=0 lastFn=%u\n",
                (unsigned)SHFL_FN_LAST);
    } else if (func == SHFL_FN_QUERY_MAPPINGS) {
        if (cparms >= 2)
            /* p1: u32 numberOfMappings (in slots / out written) -> 0 */
            gp_write_u32(as, pbase + parm_size + 4, 0);
    } else if (func == SHFL_FN_MAP_FOLDER) {
        if (cparms >= 2)
            /* p1: u32 id32Root (out) -> root handle 1 */
            gp_write_u32(as, pbase + parm_size + 4, 1);
    }

    /* LinAddr_Out buffers: zero-fill via the guest page tables. */
    for (i = 0; i < cparms; i++) {
        hwaddr p = pbase + (hwaddr)i * parm_size;
        uint32_t type = gp_read_u32(as, p);
        if (type == HP_LINADDR_OUT) {
            uint32_t cb = gp_read_u32(as, p + 4);
            uint64_t gva = gp_read_u64(as, p + 8);
            if (cb && cb <= MAX_ZERO_FILL) {
                uint64_t done = hgcm_guest_linear_zero(gva, cb);
                fprintf(stderr, "[vmmdev]   zero-fill param[%u] gva=0x%llx cb=%u -> %llu\n",
                        i, (unsigned long long)gva, cb, (unsigned long long)done);
            }
        }
    }

    /* Per-call log (one line per request, bounded parameter detail). */
    fprintf(stderr, "[vmmdev] HGCMCall%s func=%u cparms=%u client=%u svc=%s",
            f64 ? "64" : "32", func, cparms, client,
            vmmdev_client_service(s, client));
    for (i = 0; i < cparms; i++) {
        hwaddr p = pbase + (hwaddr)i * parm_size;
        uint32_t type = gp_read_u32(as, p);
        fprintf(stderr, " [t=%u", type);
        if (type == HP_32BIT) {
            fprintf(stderr, " v=0x%x", gp_read_u32(as, p + 4));
        } else if (type == HP_64BIT) {
            fprintf(stderr, " v=0x%llx",
                    (unsigned long long)gp_read_u64(as, p + 4));
        } else if (type >= HP_LINADDR && type <= HP_LINADDR_OUT) {
            fprintf(stderr, " cb=%u gva=0x%llx", gp_read_u32(as, p + 4),
                    (unsigned long long)gp_read_u64(as, p + 8));
        } else if (type == HP_PAGE_LIST) {
            fprintf(stderr, " cb=%u pglist@+%u", gp_read_u32(as, p + 4),
                    f64 ? gp_read_u32(as, p + 8) : gp_read_u32(as, p + 8));
        } else {
            fprintf(stderr, " ?");
        }
        fprintf(stderr, "]");
        if (++nlogged > 6) {
            fprintf(stderr, " ...(%u more)", cparms - i - 1);
            break;
        }
    }
    fprintf(stderr, "\n");

    if (getenv("VMMDEV_DUMP_HGCM"))
        gp_dump(as, gp, size ? (size > 0xc8 ? 0xc8 : size) : 0xc8);
}

/* ---------------------- generic request submit -------------------- */
static void vmmdev_submit(VMMDevState *s, hwaddr gp)
{
    AddressSpace *as = &address_space_memory;
    uint32_t size, type;
    static unsigned long seq;

    size = gp_read_u32(as, gp);
    type = gp_read_u32(as, gp + VR_OFF_TYPE);

    if (size == 0 || size > (1u << 20)) {
        fprintf(stderr, "[vmmdev] submit #%lu gp=0x%llx: bad size=%u type=%u (not touching payload)\n",
                ++seq, (unsigned long long)gp, size, type);
        size = 0;
    } else {
        fprintf(stderr, "[vmmdev] submit #%lu gp=0x%llx type=%u size=0x%x\n",
                ++seq, (unsigned long long)gp, type, size);
    }

    /*
     * Protocol is synchronous: the guest does "out dx, eax" then reads
     * req->rc.  Write rc = 0 (VINF_SUCCESS) for everything like today; do
     * NOT assert PCI INTx at all (the guest never waits).
     */
    gp_write_u32(as, gp + VMMDEV_REQ_RC_OFF, 0);

    switch (type) {
    case VREQ_GET_HOST_VERSION:
        /* host_version payload (major/minor/build/revision/features) stays
         * as the guest filled it (all-0xAA): features & PHYS_PAGE_LIST bit
         * reads 0, which keeps the guest on the direct LinAddr variant the
         * observed request stream uses. */
        break;

    case VREQ_ACKNOWLEDGE_EVENTS:
        /* events mask output @+0x18: nothing pending */
        gp_write_u32(as, gp + 0x18, 0);
        break;

    case VREQ_HGCM_CONNECT:
        gp_dump(as, gp, size ? (size > 0xc8 ? 0xc8 : size) : 0xc8);
        hgcm_do_connect(s, as, gp, size);
        break;

    case VREQ_HGCM_DISCONNECT:
        hgcm_do_disconnect(as, gp);
        break;

    case VREQ_HGCM_CALL32:
    case VREQ_HGCM_CALL64:
        gp_dump(as, gp, size ? (size > 0xc8 ? 0xc8 : size) : 0xc8);
        hgcm_do_call(s, as, gp, size, type == VREQ_HGCM_CALL64);
        break;

    case VREQ_HGCM_CANCEL:
    case VREQ_HGCM_CANCEL2:
        fprintf(stderr, "[vmmdev] HGCMCancel2 suppressed (no pending async calls, rc=0)\n");
        break;

    default:
        /* vboxguest.ko load path (ReportGuestInfo2, GuestCapabilities,
         * filter mask, mouse status, hypervisor info, heartbeat, status
         * ...) - rc=0 only, exactly as before. */
        break;
    }
}

static void vmmdev_port_write(void *opaque, hwaddr addr, uint64_t val,
                              unsigned size)
{
    VMMDevState *s = opaque;

    switch (addr) {
    case 0x00:
        vmmdev_submit(s, val & 0xffffffffu);
        break;
    default:
        break;
    }
}

static uint64_t vmmdev_port_read(void *opaque, hwaddr addr, unsigned size)
{
    fprintf(stderr, "[vmmdev] port_read 0x%llx\n", (unsigned long long)addr);
    return 0xffffffff;
}

static const MemoryRegionOps vmmdev_io_ops = {
    .read  = vmmdev_port_read,
    .write = vmmdev_port_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 4,
        .max_access_size = 4,
    },
};

static void vmmdev_realize(PCIDevice *pdev, Error **errp)
{
    VMMDevState *s = VMMDEV(pdev);
    uint8_t *pci_conf = pdev->config;
    static unsigned instance;

    pci_config_set_interrupt_pin(pci_conf, 1);
    /* The vboxguest.ko id table requires subsystem vendor/device == 0/0
     * (it was built with sub=0/0, not PCI_ANY_ID).  QEMU's default
     * subsystem (1af4:1100) makes the PCI match fail. */
    pci_set_word(pdev->config + PCI_SUBSYSTEM_VENDOR_ID, 0x0000);
    pci_set_word(pdev->config + PCI_SUBSYSTEM_ID, 0x0000);

    snprintf(s->io_name, sizeof(s->io_name), "vbox-vmmdev-io-%u", instance);
    memory_region_init_io(&s->io, OBJECT(s), &vmmdev_io_ops, s,
                          s->io_name, VMMDEV_IO_SIZE);
    pci_register_bar(pdev, 0, PCI_BASE_ADDRESS_SPACE_IO, &s->io);

    snprintf(s->mmio_name, sizeof(s->mmio_name), "vbox-vmmdev-mem-%u",
             instance++);
    if (!memory_region_init_ram(&s->mmio, OBJECT(s), s->mmio_name,
                                VMMDEV_MMIO_SIZE, errp)) {
        return;
    }
    pci_register_bar(pdev, 1, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->mmio);

    s->next_client_id = 1;
}

static void vmmdev_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *k = PCI_DEVICE_CLASS(klass);

    k->realize = vmmdev_realize;
    k->vendor_id = 0x80EE;
    k->device_id = 0xCAFE;
    k->revision = 0;
    k->class_id = 0xff00;
    dc->desc = "VirtualBox VMMDev (BlueStacks vboxguest + HGCM)";
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo vmmdev_types[] = {
    {
        .name          = TYPE_VMMDEV,
        .parent        = TYPE_PCI_DEVICE,
        .instance_size = sizeof(VMMDevState),
        .class_init    = vmmdev_class_init,
        .interfaces = (const InterfaceInfo[]) {
            { INTERFACE_CONVENTIONAL_PCI_DEVICE },
            { },
        },
    },
};

DEFINE_TYPES(vmmdev_types)
