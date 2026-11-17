/*
 * BlueStacks "hvmem" virtual PCI device.
 *
 * Emulates vendor 0x1AF4 / device 0x10F2 as expected by hvmem.ko.
 *
 *   BAR0 (MMIO, 0x1000 bytes, first 0x20 used)
 *     +0x00  RW  command dword.  The driver writes 0/1/2/3 here and expects
 *                the command to be completed synchronously (the driver never
 *                requests an IRQ).
 *     +0x08  RW  guest physical address of a scratch page that the driver
 *                allocated with __get_free_pages().
 *     +0x10  RW  bus address of BAR1 (written by the driver; stored only).
 *
 *   BAR1 (RAM-like memory BAR, 512 MiB by default, 64-bit prefetchable)
 *     Mapped by the driver with memremap(..., MEMREMAP_WB) and handed to
 *     userspace through /dev/hvmem mmap.  Backed by a QEMU RAM region so the
 *     contents are real host memory, not an MMIO stub.
 *
 * Hypercall emulation (the real BlueStacks host semantics are unknown, so
 * this is a deterministic "always succeeds" model - see REPORT.md):
 *
 *   cmd 0: in  scratch[0x08]=a, scratch[0x10]=b
 *          out scratch[0x10]=a, scratch[0x18]=b, scratch[0x00]=0
 *   cmd 1: out scratch[0x00]=0
 *   cmd 2: in  scratch[0x08]=a, scratch[0x10]=b
 *          out scratch[0x10]=a, scratch[0x18]=b, scratch[0x00]=0
 *   cmd 3: out scratch[0x00]=0
 *
 * The scratch page is read/written through the PCI address space, exactly the
 * way the guest kernel sees it (it is normal guest RAM).
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/bswap.h"
#include "qemu/module.h"
#include "qemu/units.h"
#include "hw/pci/pci.h"
#include "hw/pci/pci_device.h"
#include "hw/core/qdev.h"
#include "hw/core/qdev-properties.h"
#include "qom/object.h"
#include "system/memory.h"

#define TYPE_HVMEM "hvmem"
OBJECT_DECLARE_SIMPLE_TYPE(HvmemState, HVMEM)

#define HVMEM_BAR0_SIZE     0x1000
#define HVMEM_DEFAULT_MIB   512
#define HVMEM_MIN_MIB       4
#define HVMEM_MAX_MIB       4096

#define HVMEM_CTRL_CMD      0x00
#define HVMEM_CTRL_SCRATCH  0x08
#define HVMEM_CTRL_RAM_ADDR 0x10

struct HvmemState {
    PCIDevice parent_obj;

    MemoryRegion bar0;
    MemoryRegion bar1;
    char ram_name[64];

    uint8_t ctrl[HVMEM_BAR0_SIZE];
    uint64_t scratch_gpa;
    uint64_t ram_bus_addr;
    uint32_t ram_size_mib;
};

static uint64_t hvmem_regs_read(const uint8_t *regs, hwaddr addr,
                                unsigned size)
{
    uint64_t v = 0;
    unsigned i;

    for (i = 0; i < size; i++) {
        v |= (uint64_t)regs[addr + i] << (8 * i);
    }
    return v;
}

static void hvmem_regs_write(uint8_t *regs, hwaddr addr, uint64_t val,
                             unsigned size)
{
    unsigned i;

    for (i = 0; i < size; i++) {
        regs[addr + i] = (val >> (8 * i)) & 0xff;
    }
}

static void hvmem_run_cmd(HvmemState *s, uint32_t cmd)
{
    AddressSpace *as = pci_get_address_space(PCI_DEVICE(s));
    uint64_t a, b;
    uint32_t zero = 0;

    if (!s->scratch_gpa) {
        return;
    }

    switch (cmd) {
    case 0:
    case 2:
        if (address_space_read(as, s->scratch_gpa + 0x08,
                               MEMTXATTRS_UNSPECIFIED, &a, 8) != MEMTX_OK ||
            address_space_read(as, s->scratch_gpa + 0x10,
                               MEMTXATTRS_UNSPECIFIED, &b, 8) != MEMTX_OK) {
            return;
        }
        address_space_write(as, s->scratch_gpa + 0x10,
                            MEMTXATTRS_UNSPECIFIED, &a, 8);
        address_space_write(as, s->scratch_gpa + 0x18,
                            MEMTXATTRS_UNSPECIFIED, &b, 8);
        address_space_write(as, s->scratch_gpa + 0x00,
                            MEMTXATTRS_UNSPECIFIED, &zero, 4);
        break;
    case 1:
    case 3:
        address_space_write(as, s->scratch_gpa + 0x00,
                            MEMTXATTRS_UNSPECIFIED, &zero, 4);
        break;
    default:
        break;
    }
}

static uint64_t hvmem_bar0_read(void *opaque, hwaddr addr, unsigned size)
{
    HvmemState *s = opaque;

    return hvmem_regs_read(s->ctrl, addr, size);
}

static void hvmem_bar0_write(void *opaque, hwaddr addr, uint64_t val,
                             unsigned size)
{
    HvmemState *s = opaque;
    bool hits_cmd = (addr <= HVMEM_CTRL_CMD &&
                     addr + size > HVMEM_CTRL_CMD);

    hvmem_regs_write(s->ctrl, addr, val, size);

    if (addr <= HVMEM_CTRL_SCRATCH &&
        addr + size > HVMEM_CTRL_SCRATCH) {
        s->scratch_gpa = ldq_le_p(s->ctrl + HVMEM_CTRL_SCRATCH);
    }
    if (addr <= HVMEM_CTRL_RAM_ADDR &&
        addr + size > HVMEM_CTRL_RAM_ADDR) {
        s->ram_bus_addr = ldq_le_p(s->ctrl + HVMEM_CTRL_RAM_ADDR);
    }
    if (hits_cmd) {
        hvmem_run_cmd(s, ldl_le_p(s->ctrl + HVMEM_CTRL_CMD));
    }
}

static const MemoryRegionOps hvmem_bar0_ops = {
    .read = hvmem_bar0_read,
    .write = hvmem_bar0_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 8,
    .impl.min_access_size = 1,
    .impl.max_access_size = 8,
};

static void hvmem_realize(PCIDevice *pdev, Error **errp)
{
    HvmemState *s = HVMEM(pdev);
    static unsigned instance;
    uint64_t ram_size;

    if (s->ram_size_mib < HVMEM_MIN_MIB) {
        s->ram_size_mib = HVMEM_MIN_MIB;
    }
    if (s->ram_size_mib > HVMEM_MAX_MIB) {
        s->ram_size_mib = HVMEM_MAX_MIB;
    }
    ram_size = (uint64_t)s->ram_size_mib * MiB;

    memory_region_init_io(&s->bar0, OBJECT(s), &hvmem_bar0_ops, s,
                          "hvmem-ctrl", HVMEM_BAR0_SIZE);
    pci_register_bar(pdev, 0, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar0);

    snprintf(s->ram_name, sizeof(s->ram_name), "hvmem-ram-%u", instance++);
    if (!memory_region_init_ram(&s->bar1, OBJECT(s), s->ram_name,
                                ram_size, errp)) {
        return;
    }
    pci_register_bar(pdev, 1,
                     PCI_BASE_ADDRESS_SPACE_MEMORY |
                     PCI_BASE_ADDRESS_MEM_TYPE_64 |
                     PCI_BASE_ADDRESS_MEM_PREFETCH,
                     &s->bar1);
}

static void hvmem_instance_init(Object *obj)
{
    HvmemState *s = HVMEM(obj);

    s->ram_size_mib = HVMEM_DEFAULT_MIB;
}

static const Property hvmem_props[] = {
    DEFINE_PROP_UINT32("ram_size_mib", HvmemState, ram_size_mib,
                       HVMEM_DEFAULT_MIB),
};

static void hvmem_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *k = PCI_DEVICE_CLASS(klass);

    k->realize = hvmem_realize;
    k->vendor_id = 0x1af4;
    k->device_id = 0x10f2;
    k->revision = 0;
    k->class_id = 0x0000;
    dc->desc = "BlueStacks hvmem shared memory / control device";
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
    device_class_set_props(dc, hvmem_props);
}

static const TypeInfo hvmem_types[] = {
    {
        .name          = TYPE_HVMEM,
        .parent        = TYPE_PCI_DEVICE,
        .instance_size = sizeof(HvmemState),
        .instance_init = hvmem_instance_init,
        .class_init    = hvmem_class_init,
        .interfaces = (const InterfaceInfo[]) {
            { INTERFACE_CONVENTIONAL_PCI_DEVICE },
            { },
        },
    },
};

DEFINE_TYPES(hvmem_types)
