/*
 * BlueStacks "bstaudio" virtual PCI device.
 *
 * Emulates vendor 0xB574 / device 0x100D as expected by bstaudio.ko.
 *
 * What the guest driver actually touches (verified by disassembling the .ko
 * shipped in the initramfs, see REPORT.md):
 *
 *   BAR0 (MMIO, 0x1000 bytes)
 *     +0x00  WO  command doorbell.  bstaudio_config_cmd() and
 *                bstaudio_playback_data_available_cmd() fill a command
 *                structure at BAR1+0x400 and then write 0 here.
 *     +0x08  WO  interrupt/status ack; bstaudio_isr() writes 0 here after
 *                consuming a message with type 7 or 13 from BAR1+0x400.
 *
 *   BAR1 (host RAM, 0x40000 bytes - the driver needs >= 0x22000)
 *     +0x400   command/event ring header (message type + length + params)
 *     +0x800   playback state / position (read by bstaudio_isr)
 *     +0xc00   capture state / position
 *     +0x1000  further playback bookkeeping
 *     +0x21000 further capture bookkeeping
 *   The probe derives all of these slices from the second ioremapped BAR, so
 *   anything >= 0x21000 + tail works.  0x40000 is used here.
 *
 * No interrupt is ever raised: there is no host audio backend behind this
 * model, so the ALSA PCM will initialize but no samples will flow.  That is
 * intentional ("passthrough, nice-to-have" per the device spec) and avoids
 * any risk of a stuck IRQ during boot.
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
#include "system/memory.h"

#define TYPE_BST_AUDIO "bstaudio"
OBJECT_DECLARE_SIMPLE_TYPE(BstAudioState, BST_AUDIO)

#define BST_AUDIO_BAR0_SIZE 0x1000
#define BST_AUDIO_BAR1_SIZE 0x40000     /* >= 0x22000 required by the driver */

struct BstAudioState {
    PCIDevice parent_obj;

    MemoryRegion bar0;
    MemoryRegion bar1;
    char shm_name[64];

    uint8_t *shm;
    uint32_t last_doorbell;
    uint32_t last_ack;
};

static uint64_t bst_audio_bar0_read(void *opaque, hwaddr addr, unsigned size)
{
    BstAudioState *s = opaque;

    switch (addr) {
    case 0x00:
        /* No readable command/status register is used by the driver. */
        return 0;
    case 0x04:
        return 0;
    case 0x08:
        return s->last_ack;
    default:
        return 0;
    }
}

static void bst_audio_bar0_write(void *opaque, hwaddr addr, uint64_t val,
                                 unsigned size)
{
    BstAudioState *s = opaque;

    switch (addr) {
    case 0x00:
        /*
         * Doorbell.  A real BlueStacks host would consume the command at
         * BAR1+0x400 and answer with an interrupt (message type 7 = playback
         * position update, 13 = capture position update).  Nothing to do in
         * this model.
         */
        s->last_doorbell = val;
        break;
    case 0x08:
        s->last_ack = val;
        break;
    default:
        break;
    }
}

static const MemoryRegionOps bst_audio_bar0_ops = {
    .read = bst_audio_bar0_read,
    .write = bst_audio_bar0_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

static void bst_audio_realize(PCIDevice *pdev, Error **errp)
{
    BstAudioState *s = BST_AUDIO(pdev);
    static unsigned instance;
    uint8_t *pci_conf = pdev->config;

    pci_config_set_interrupt_pin(pci_conf, 1);

    memory_region_init_io(&s->bar0, OBJECT(s), &bst_audio_bar0_ops, s,
                          "bstaudio-bar0", BST_AUDIO_BAR0_SIZE);
    pci_register_bar(pdev, 0, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar0);

    snprintf(s->shm_name, sizeof(s->shm_name), "bstaudio-shm-%u",
             instance++);
    if (!memory_region_init_ram(&s->bar1, OBJECT(s), s->shm_name,
                                BST_AUDIO_BAR1_SIZE, errp)) {
        return;
    }
    pci_register_bar(pdev, 1, PCI_BASE_ADDRESS_SPACE_MEMORY, &s->bar1);

    s->shm = memory_region_get_ram_ptr(&s->bar1);
    memset(s->shm, 0, BST_AUDIO_BAR1_SIZE);
}

static void bst_audio_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *k = PCI_DEVICE_CLASS(klass);

    k->realize = bst_audio_realize;
    k->vendor_id = 0xb574;
    k->device_id = 0x100d;
    k->revision = 0;
    k->class_id = 0x0000;
    dc->desc = "BlueStacks bstaudio PCI audio device (stub)";
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo bst_audio_types[] = {
    {
        .name          = TYPE_BST_AUDIO,
        .parent        = TYPE_PCI_DEVICE,
        .instance_size = sizeof(BstAudioState),
        .class_init    = bst_audio_class_init,
        .interfaces = (const InterfaceInfo[]) {
            { INTERFACE_CONVENTIONAL_PCI_DEVICE },
            { },
        },
    },
};

DEFINE_TYPES(bst_audio_types)
