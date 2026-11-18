// SPDX-License-Identifier: GPL-2.0-or-later
//
// ost_renderer.cpp - OpenStacks' renderer plugin: the C API in ost_renderer.h on top of
// gfxstream's emulator-style host renderer (RenderLib / Renderer / RenderChannel).
// One RenderChannel per guest connection; QEMU's gr_render.c moves the bytes.
// docs/renderer_plan.md.

#define OST_RENDERER_BUILD 1
#include "ost_renderer.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <thread>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "gfxstream/host/features.h"
#include "render-utils/RenderChannel.h"
#include "render-utils/RenderLib.h"
#include "render-utils/Renderer.h"
#include "render-utils/render_api.h"

using gfxstream::RenderChannel;

namespace {

struct State {
    std::mutex mu;
    gfxstream::RenderLibPtr lib;
    gfxstream::RendererPtr renderer;
    OstRendererConfig cfg{};
    ost_frame_fn frame = nullptr;
    void* frameUser = nullptr;
    int width = 0, height = 0;
};
State g;

void logf(int level, const char* fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (g.cfg.log) {
        g.cfg.log(g.cfg.user, level, buf);
    } else {
        fprintf(stderr, "[ost_renderer] %s\n", buf);
    }
}

// gfxstream's own log lines -> our callback (errors and warnings by default)
void gfxLog(gfxstream_logging_level level, const char* file, int line, const char* fn,
            const char* msg) {
    int l = level <= GFXSTREAM_LOGGING_LEVEL_ERROR     ? 0
            : level == GFXSTREAM_LOGGING_LEVEL_WARNING ? 1
            : level == GFXSTREAM_LOGGING_LEVEL_INFO    ? 2
                                                       : 3;
    (void)file;
    (void)line;
    logf(l, "gfxstream %s: %s", fn ? fn : "", msg ? msg : "");
}

// gfxstream reads back bottom row first; the C API promises top row first
void deliverFrame(int width, int height, const uint8_t* pixels, bool bottomUp) {
    ost_frame_fn fn;
    void* user;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        fn = g.frame;
        user = g.frameUser;
    }
    if (!fn || !pixels || width <= 0 || height <= 0) return;
    if (!bottomUp) {
        fn(user, width, height, pixels);
        return;
    }
    static thread_local std::vector<uint8_t> flipped;
    const size_t row = (size_t)width * 4;
    flipped.resize(row * (size_t)height);
    for (int y = 0; y < height; y++) {
        memcpy(flipped.data() + (size_t)y * row, pixels + (size_t)(height - 1 - y) * row, row);
    }
    fn(user, width, height, flipped.data());
}

// Async readback (GLES 3 hosts): gfxstream's post callback only says "a frame is ready";
// the pixels have to be pulled with its ReadPixelsCallback, which waits for gfxstream's
// readback thread, so not from inside the callback. A puller thread does it.
struct Puller {
    std::mutex mu;
    std::condition_variable cv;
    bool pending = false, quit = false;
    int width = 0, height = 0;
    gfxstream::Renderer::ReadPixelsCallback read = nullptr;
    std::thread thread;
} gp;

void pullerMain() {
    std::vector<uint8_t> buf;
    for (;;) {
        int w, h;
        {
            std::unique_lock<std::mutex> lk(gp.mu);
            gp.cv.wait(lk, [] { return gp.pending || gp.quit; });
            if (gp.quit) return;
            gp.pending = false;
            w = gp.width;
            h = gp.height;
        }
        buf.resize((size_t)w * h * 4);
        gp.read(buf.data(), (uint32_t)buf.size(), 0);
        deliverFrame(w, h, buf.data(), false);   // getPixels rows are top-down already
    }
}

void onPost(void* ctx, uint32_t displayId, int width, int height, int ydir, int format,
            int type, unsigned char* pixels) {
    (void)ctx;
    (void)displayId;
    (void)format;
    (void)type;
    if (gp.read) {                       // async: hand off to the puller (coalesces)
        {
            std::lock_guard<std::mutex> lk(gp.mu);
            gp.pending = true;
            gp.width = width;
            gp.height = height;
        }
        gp.cv.notify_one();
        return;
    }
    deliverFrame(width, height, pixels, ydir < 0);
}

// The guest is the goldfish-opengl-pie generation (Android emulator stack, BlueStacks fork):
// GLES only, classic rcOpenColorBuffer/rcCloseColorBuffer ref counting, no pipe checksums.
// The caller's guest_api_level picks gfxstream's colour buffer ref counting rules.
// Host GL: desktop OpenGL (WGL), the Android emulator's host-GPU path on Windows. Guest
// GLSL ES goes through libshadertranslator.dll (ANGLE's translator, from the emulator
// package; gfxstream built with USE_ANGLE_SHADER_PARSER). OST_GL=angle runs GLES on ANGLE
// (libEGL.dll + libGLESv2.dll) instead: gfxstream's ANGLE_INDIRECT mode, which crashes inside
// ANGLE's D3D11 backend under gfxstream's many-threaded use (docs/renderer_plan.md).
bool useDesktopGl() {
    const char* v = getenv("OST_GL");
    return !(v && strcmp(v, "angle") == 0);
}

gfxstream::host::FeatureSet guestFeatures() {
    gfxstream::host::FeatureSet f;
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, EglOnEgl, !useDesktopGl());
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, GlesDynamicVersion, true);
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, HostComposition, true);
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, GlPipeChecksum, false);
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, GlAsyncSwap, false);
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, GlDma, false);
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, GlDirectMem, false);
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, RefCountPipe, false);
    // emulator default: a buffer whose last reference goes away lives ~1 s longer
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, NoDelayCloseColorBuffer, false);
    GFXSTREAM_SET_BOOL_FEATURE_ON_CONDITION(&f, Vulkan, false);
    // OST_GFX_FEATURES="Name:1,Name:0": override boolean features (experiments)
    if (const char* ov = getenv("OST_GFX_FEATURES")) {
        std::string all(ov);
        size_t pos = 0;
        while (pos < all.size()) {
            size_t end = all.find(',', pos);
            if (end == std::string::npos) end = all.size();
            std::string item = all.substr(pos, end - pos);
            size_t colon = item.find(':');
            if (colon != std::string::npos) {
                auto it = f.map.find(item.substr(0, colon));
                auto* b = it == f.map.end() ? nullptr
                                            : dynamic_cast<gfxstream::host::BoolFeatureInfo*>(it->second);
                if (b) {
                    b->setEnabled(item[colon + 1] == '1');
                    logf(2, "feature %s = %d", it->first.c_str(), item[colon + 1] == '1');
                } else {
                    logf(1, "feature %s: unknown or not boolean", item.substr(0, colon).c_str());
                }
            }
            pos = end + 1;
        }
    }
    return f;
}

}  // namespace

struct OstChannel {
    gfxstream::RenderChannelPtr ch;
    RenderChannel::Buffer pending;  // reply bytes not yet handed to the caller
    size_t pendingPos = 0;
};

extern "C" {

OST_API int ost_renderer_init(const OstRendererConfig* cfg) {
    if (!cfg || cfg->api_version != OST_RENDERER_API_VERSION) return -1;
    std::lock_guard<std::mutex> lk(g.mu);
    if (g.renderer) return 0;
    g.cfg = *cfg;
    g.width = cfg->width;
    g.height = cfg->height;
    g.lib = gfxstream::initLibrary();
    if (!g.lib) {
        logf(0, "initLibrary failed");
        return -1;
    }
    g.lib->setLogger(gfxLog);
    const char* verbose = getenv("OST_RENDERER_VERBOSE");
    g.lib->setLogLevel(verbose && *verbose == '1' ? GFXSTREAM_LOGGING_LEVEL_INFO
                                                  : GFXSTREAM_LOGGING_LEVEL_WARNING);
    // Host GPU. On ANGLE this is gfxstream's ANGLE_INDIRECT: HOST means desktop GL to it
    // (core-profile contexts, "#version 330 core" helper shaders, BGRA_REV readback).
    g.lib->setRenderer(useDesktopGl() ? SELECTED_RENDERER_HOST : SELECTED_RENDERER_ANGLE_INDIRECT);
    if (cfg->guest_api_level > 0) g.lib->setGuestAndroidApiLevel(cfg->guest_api_level);
    g.renderer = g.lib->initRenderer(cfg->width, cfg->height, guestFeatures(),
                                     true /* sub-window */);
    if (!g.renderer) {
        logf(0, "initRenderer(%d x %d) failed", cfg->width, cfg->height);
        return -1;
    }
    auto hw = g.renderer->getHardwareStrings();
    logf(2, "renderer up: %s / %s / %s", hw.vendor.c_str(), hw.renderer.c_str(),
         hw.version.c_str());
    return 0;
}

OST_API int ost_renderer_show(void* parent_hwnd, int x, int y, int w, int h,
                              float rotation_deg) {
    gfxstream::RendererPtr r;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        r = g.renderer;
    }
    if (!r || w <= 0 || h <= 0) return -1;
    // fbw/fbh of showOpenGLSubwindow = the GL viewport in window pixels (the guest frame
    // is stretched over it), not the guest framebuffer size: the whole subwindow here.
    bool ok = r->showOpenGLSubwindow((FBNativeWindowType)parent_hwnd, x, y, w, h, w, h, 1.0f,
                                     rotation_deg, false /* keep */, false /* visible */);
    if (!ok) logf(0, "showOpenGLSubwindow failed");
    return ok ? 0 : -1;
}

OST_API void ost_renderer_set_rotation(float rotation_deg) {
    std::lock_guard<std::mutex> lk(g.mu);
    if (g.renderer) g.renderer->setOpenGLDisplayRotation(rotation_deg);
}

OST_API void ost_renderer_repaint(void) {
    std::lock_guard<std::mutex> lk(g.mu);
    if (g.renderer) g.renderer->repaintOpenGLDisplay();
}

OST_API void ost_renderer_set_frame_callback(ost_frame_fn fn, void* user) {
    gfxstream::RendererPtr r;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        g.frame = fn;
        g.frameUser = user;
        r = g.renderer;
    }
    if (r) {
        if (fn) {
            if (r->asyncReadbackSupported() && !gp.thread.joinable()) {
                gp.read = r->getReadPixelsCallback();
                gp.thread = std::thread(pullerMain);
            }
            r->setPostCallback(onPost, nullptr, false /* RGBA */, 0);
        } else {
            r->setPostCallback(nullptr, nullptr, false, 0);
        }
    }
}

OST_API OstChannel* ost_channel_open(void) {
    gfxstream::RendererPtr r;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        r = g.renderer;
    }
    if (!r) return nullptr;
    auto ch = r->createRenderChannel(nullptr);
    if (!ch) return nullptr;
    auto* o = new OstChannel;
    o->ch = std::move(ch);
    return o;
}

OST_API int ost_channel_write(OstChannel* o, const void* data, size_t len) {
    if (!o || !o->ch) return -1;
    const char* p = static_cast<const char*>(data);
    while (len) {
        // keep each buffer moderate: the render thread consumes it as one chunk
        size_t n = std::min<size_t>(len, 1u << 20);
        RenderChannel::Buffer buf;
        buf.resize_noinit(n);
        memcpy(buf.data(), p, n);
        for (;;) {
            auto res = o->ch->tryWrite(std::move(buf));
            if (res == RenderChannel::IoResult::Ok) break;
            if (res == RenderChannel::IoResult::Error) return -1;
            o->ch->waitUntilWritable();  // TryAgain: queue full
            if ((int)o->ch->state() & (int)RenderChannel::State::Stopped) return -1;
            buf.resize_noinit(n);  // tryWrite may have moved from it
            memcpy(buf.data(), p, n);
        }
        p += n;
        len -= n;
    }
    return 0;
}

OST_API long ost_channel_read(OstChannel* o, void* out, size_t cap, int timeout_ms) {
    if (!o || !o->ch || !cap) return -1;
    if (o->pendingPos >= o->pending.size()) {
        o->pending.clear();
        o->pendingPos = 0;
        RenderChannel::IoResult res;
        if (timeout_ms == 0) {
            res = o->ch->tryRead(&o->pending);
        } else if (timeout_ms < 0) {
            o->ch->waitUntilReadable();
            res = o->ch->tryRead(&o->pending);
            while (res == RenderChannel::IoResult::TryAgain) {
                o->ch->waitUntilReadable();
                res = o->ch->tryRead(&o->pending);
            }
        } else {
            // readBefore takes an absolute deadline in Unix-time microseconds
            uint64_t nowUs = (uint64_t)std::chrono::duration_cast<std::chrono::microseconds>(
                                 std::chrono::system_clock::now().time_since_epoch())
                                 .count();
            res = o->ch->readBefore(&o->pending, nowUs + (uint64_t)timeout_ms * 1000ull);
        }
        if (res == RenderChannel::IoResult::Timeout || res == RenderChannel::IoResult::TryAgain)
            return 0;
        if (res != RenderChannel::IoResult::Ok) return -1;
    }
    size_t n = std::min(cap, o->pending.size() - o->pendingPos);
    memcpy(out, o->pending.data() + o->pendingPos, n);
    o->pendingPos += n;
    return (long)n;
}

OST_API void ost_channel_set_notify(OstChannel* o, ost_notify_fn fn, void* user) {
    if (!o || !o->ch) return;
    if (fn) {
        o->ch->setEventCallback([fn, user](RenderChannel::State) { fn(user); });
    } else {
        o->ch->setEventCallback([](RenderChannel::State) {});
    }
}

OST_API void ost_channel_arm(OstChannel* o) {
    if (o && o->ch) o->ch->setWantedEvents(RenderChannel::State::CanRead);
}

OST_API void ost_channel_stop(OstChannel* o) {
    if (o && o->ch) o->ch->stop();
}

OST_API void ost_channel_close(OstChannel* o) {
    if (!o) return;
    if (o->ch) o->ch->stop();
    delete o;
}

OST_API void ost_renderer_shutdown(void) {
    gfxstream::RendererPtr r;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        r = std::move(g.renderer);
        g.frame = nullptr;
    }
    if (gp.thread.joinable()) {
        {
            std::lock_guard<std::mutex> lk(gp.mu);
            gp.quit = true;
        }
        gp.cv.notify_one();
        gp.thread.join();
    }
    if (r) r->stop(true);
}

}  // extern "C"
