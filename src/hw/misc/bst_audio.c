/*
 * BlueStacks "bstaudio" virtual PCI audio device (B574:100D), Phase 69.
 *
 * Protocol (bstaudio.ko decode, dev/NOTES.md 10g):
 *
 *   BAR0 (MMIO 0x1000)
 *     +0x00  W  command doorbell: the command is at BAR1+0x400
 *     +0x08  W  IRQ ack (guest ISR, after consuming the event at BAR1+0x000)
 *
 *   BAR1 (RAM 0x80000)
 *     +0x000   event type (host -> guest): 7 = playback period, 13 = capture
 *     +0x400   command mailbox: u32 op, (u32 unused), u32 len, payload @+0x0c
 *     +0x800   u32 capture periods-completed counter (free running)
 *     +0xc00   u32 playback periods-completed counter (free running)
 *     +0x1000  capture ring  (<= 0x20000 bytes, host writes)
 *     +0x21000 playback ring (<= 0x20000 bytes, guest writes)
 *
 *   Ops: 1/8 playback/capture init, 3/0xa config {rate, bits, channels,
 *   buffer_bytes, period_bytes, bytes_per_sec, frame_bytes}, 4/0xb start,
 *   5/0xc stop, 6 playback data available {count, offset} (data already in
 *   the ring). Nothing is ever read back.
 *
 *   Events: one period per event; the guest advances its pointer by one
 *   period per event (pointer callback), so periods are never merged.
 *   The next event is only posted after the BAR0+0x08 ack.
 *
 * Playback goes to a QEMU audio backend when an audiodev is given
 * (-audiodev dsound,id=snd0 -device bstaudio,audiodev=snd0): the backend's
 * pull callback paces consumption. Without one (and for capture, which
 * delivers silence) a real-time period timer keeps the streams moving.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/module.h"
#include "qemu/units.h"
#include "qemu/timer.h"
#include "qemu/audio.h"
#include "hw/pci/pci.h"
#include "hw/pci/pci_device.h"
#include "hw/core/qdev-properties.h"
#include "qom/object.h"
#include "system/memory.h"

#define TYPE_BST_AUDIO "bstaudio"
OBJECT_DECLARE_SIMPLE_TYPE(BstAudioState, BST_AUDIO)

#define BST_AUDIO_BAR0_SIZE 0x1000
#define BST_AUDIO_BAR1_SIZE 0x80000

#define AUD_EVT_TYPE        0x000
#define AUD_CMD             0x400
#define AUD_CAP_COUNT       0x800
#define AUD_PLAY_COUNT      0xc00
#define AUD_CAP_RING        0x1000
#define AUD_PLAY_RING       0x21000
#define AUD_RING_MAX        0x20000

#define EVT_PLAY            7
#define EVT_CAP             13

typedef struct BstAudStream {
    uint32_t rate, bits, channels;
    uint32_t buffer_bytes, period_bytes, bytes_per_sec, frame_bytes;
    bool configured, running;
    uint32_t done;          /* periods completed (counter shown to guest) */
    uint32_t pending;       /* completed periods not yet posted */
    uint64_t n;             /* periods consumed/produced by the host */
    uint32_t in_period;     /* bytes of the current period already played */
    int64_t t0_ns;          /* timer pacing epoch */
    uint64_t t_n;           /* periods paced since t0 */
    QEMUTimer *timer;
} BstAudStream;

struct BstAudioState {
    PCIDevice parent_obj;

    MemoryRegion bar0;
    MemoryRegion bar1;
    char shm_name[64];
    uint8_t *shm;

    AudioBackend *audio_be;
    SWVoiceOut *voice;

    BstAudStream play, cap;
    bool irq_busy;
    unsigned events, cmds;
};

/* ---------------------------------------------------------------- events */

static void bst_audio_deliver(BstAudioState *s)
{
    if (s->irq_busy) {
        return;
    }
    if (s->play.pending) {
        s->play.pending--;
        s->play.done++;
        stl_le_p(s->shm + AUD_PLAY_COUNT, s->play.done);
        stl_le_p(s->shm + AUD_EVT_TYPE, EVT_PLAY);
    } else if (s->cap.pending) {
        s->cap.pending--;
        s->cap.done++;
        stl_le_p(s->shm + AUD_CAP_COUNT, s->cap.done);
        stl_le_p(s->shm + AUD_EVT_TYPE, EVT_CAP);
    } else {
        return;
    }
    s->irq_busy = true;
    s->events++;
    pci_set_irq(PCI_DEVICE(s), 1);
}

static void bst_audio_period_done(BstAudioState *s, BstAudStream *st)
{
    st->n++;
    /* bound the backlog: a stalled guest must not make us queue forever */
    if (st->pending < 64) {
        st->pending++;
    }
    bst_audio_deliver(s);
}

/* ------------------------------------------------------------- playback */

static uint8_t *play_ptr(BstAudioState *s, uint32_t *contig)
{
    BstAudStream *st = &s->play;
    uint32_t off = (uint32_t)((st->n * st->period_bytes + st->in_period) %
                              st->buffer_bytes);
    uint32_t left_period = st->period_bytes - st->in_period;
    uint32_t left_ring = st->buffer_bytes - off;

    *contig = MIN(left_period, left_ring);
    return s->shm + AUD_PLAY_RING + off;
}

/* audio backend pull: feed what the device "plays", period by period */
static void bst_audio_out_cb(void *opaque, int avail)
{
    BstAudioState *s = opaque;
    BstAudStream *st = &s->play;

    while (avail > 0 && st->running && st->period_bytes) {
        uint32_t contig;
        uint8_t *p = play_ptr(s, &contig);
        size_t n = audio_be_write(s->audio_be, s->voice, p,
                                  MIN((uint32_t)avail, contig));
        if (n == 0) {
            break;
        }
        avail -= n;
        st->in_period += n;
        if (st->in_period >= st->period_bytes) {
            st->in_period = 0;
            bst_audio_period_done(s, st);
        }
    }
}

static AudioFormat bst_audio_fmt(uint32_t bits)
{
    switch (bits) {
    case 8:
        return AUDIO_FORMAT_S8;
    case 32:
        return AUDIO_FORMAT_S32;
    default:
        return AUDIO_FORMAT_S16;
    }
}

static void bst_audio_open_voice(BstAudioState *s)
{
    BstAudStream *st = &s->play;
    struct audsettings as = {
        .freq = (int)st->rate,
        .nchannels = (int)MIN(st->channels, 8u),
        .fmt = bst_audio_fmt(st->bits),
        .big_endian = false,
    };

    if (!s->audio_be) {
        return;
    }
    s->voice = audio_be_open_out(s->audio_be, s->voice, "bstaudio", s,
                                 bst_audio_out_cb, &as);
    fprintf(stderr, "[bstaudio] playback voice %s: %u Hz, %u ch, %u bit\n",
            s->voice ? "open" : "FAILED", st->rate, st->channels, st->bits);
}

/* --------------------------------------------- real-time pacing timer */

static void bst_audio_timer_arm(BstAudStream *st)
{
    if (!st->running || !st->bytes_per_sec || !st->period_bytes) {
        return;
    }
    int64_t per_ns = (int64_t)st->period_bytes * NANOSECONDS_PER_SECOND /
                     st->bytes_per_sec;
    timer_mod(st->timer, st->t0_ns + (int64_t)(st->t_n + 1) * per_ns);
}

static void bst_audio_play_tick(void *opaque)
{
    BstAudioState *s = opaque;
    BstAudStream *st = &s->play;

    if (!st->running) {
        return;
    }
    st->t_n++;
    bst_audio_period_done(s, st);           /* no backend: drop the period */
    bst_audio_timer_arm(st);
}

static void bst_audio_cap_tick(void *opaque)
{
    BstAudioState *s = opaque;
    BstAudStream *st = &s->cap;

    if (!st->running) {
        return;
    }
    /* no capture source yet: produce one period of silence */
    uint32_t off = (uint32_t)((st->n * st->period_bytes) % st->buffer_bytes);
    uint32_t len = MIN(st->period_bytes, st->buffer_bytes - off);
    memset(s->shm + AUD_CAP_RING + off, 0, len);
    if (len < st->period_bytes) {
        memset(s->shm + AUD_CAP_RING, 0, st->period_bytes - len);
    }
    st->t_n++;
    bst_audio_period_done(s, st);
    bst_audio_timer_arm(st);
}

static void bst_audio_start(BstAudStream *st)
{
    st->running = true;
    st->t0_ns = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    st->t_n = 0;
}

/* ------------------------------------------------------------- commands */

static bool bst_audio_config(BstAudioState *s, BstAudStream *st,
                             uint32_t cnt_off, const uint8_t *c)
{
    st->rate = ldl_le_p(c + 0x0c);
    st->bits = ldl_le_p(c + 0x10);
    st->channels = ldl_le_p(c + 0x14);
    st->buffer_bytes = ldl_le_p(c + 0x18);
    st->period_bytes = ldl_le_p(c + 0x1c);
    st->bytes_per_sec = ldl_le_p(c + 0x20);
    st->frame_bytes = ldl_le_p(c + 0x24);
    if (!st->buffer_bytes || st->buffer_bytes > AUD_RING_MAX ||
        !st->period_bytes || st->period_bytes > st->buffer_bytes) {
        fprintf(stderr, "[bstaudio] bad config buffer=%u period=%u\n",
                st->buffer_bytes, st->period_bytes);
        st->configured = false;
        return false;
    }
    st->configured = true;
    st->running = false;
    st->n = st->done = st->pending = st->in_period = 0;
    stl_le_p(s->shm + cnt_off, 0);
    timer_del(st->timer);
    return true;
}

static void bst_audio_command(BstAudioState *s)
{
    const uint8_t *c = s->shm + AUD_CMD;
    uint32_t op = ldl_le_p(c);

    s->cmds++;
    switch (op) {
    case 3:     /* playback config */
        if (bst_audio_config(s, &s->play, AUD_PLAY_COUNT, c)) {
            bst_audio_open_voice(s);
            /* park: a spurious shared-IRQ ISR run now sees delta 0 */
            stl_le_p(s->shm + AUD_EVT_TYPE, EVT_PLAY);
        }
        break;
    case 4:     /* playback start */
        if (s->play.configured) {
            bst_audio_start(&s->play);
            if (s->voice) {
                audio_be_set_active_out(s->audio_be, s->voice, true);
            } else {
                bst_audio_timer_arm(&s->play);
            }
        }
        break;
    case 5:     /* playback stop */
        s->play.running = false;
        timer_del(s->play.timer);
        if (s->voice) {
            audio_be_set_active_out(s->audio_be, s->voice, false);
        }
        break;
    case 0xa:   /* capture config */
        if (bst_audio_config(s, &s->cap, AUD_CAP_COUNT, c)) {
            stl_le_p(s->shm + AUD_EVT_TYPE, EVT_CAP);
        }
        break;
    case 0xb:   /* capture start */
        if (s->cap.configured) {
            bst_audio_start(&s->cap);
            bst_audio_timer_arm(&s->cap);
        }
        break;
    case 0xc:   /* capture stop */
        s->cap.running = false;
        timer_del(s->cap.timer);
        break;
    default:    /* 1/8 init, 2/9 fini, 6 data available: nothing to do */
        break;
    }
    if (s->cmds <= 12 && op != 6) {
        fprintf(stderr, "[bstaudio] cmd op=%u\n", op);
    }
}

/* ------------------------------------------------------------------ BAR0 */

static uint64_t bst_audio_bar0_read(void *opaque, hwaddr addr, unsigned size)
{
    return 0;   /* the driver reads nothing back */
}

static void bst_audio_bar0_write(void *opaque, hwaddr addr, uint64_t val,
                                 unsigned size)
{
    BstAudioState *s = opaque;

    switch (addr) {
    case 0x00:
        bst_audio_command(s);
        break;
    case 0x08:
        pci_set_irq(PCI_DEVICE(s), 0);
        s->irq_busy = false;
        bst_audio_deliver(s);
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

    s->play.timer = timer_new_ns(QEMU_CLOCK_REALTIME, bst_audio_play_tick, s);
    s->cap.timer = timer_new_ns(QEMU_CLOCK_REALTIME, bst_audio_cap_tick, s);

    if (s->audio_be && !audio_be_check(&s->audio_be, errp)) {
        return;
    }
    fprintf(stderr, "[bstaudio] realized (%s)\n",
            s->audio_be ? "audio backend attached" : "no audiodev: silent pacing");
}

static const Property bst_audio_props[] = {
    DEFINE_AUDIO_PROPERTIES(BstAudioState, audio_be),
};

static void bst_audio_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    PCIDeviceClass *k = PCI_DEVICE_CLASS(klass);

    k->realize = bst_audio_realize;
    k->vendor_id = 0xb574;
    k->device_id = 0x100d;
    k->revision = 0;
    k->class_id = 0x0000;
    dc->desc = "BlueStacks bstaudio PCI audio device";
    device_class_set_props(dc, bst_audio_props);
    set_bit(DEVICE_CATEGORY_SOUND, dc->categories);
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
