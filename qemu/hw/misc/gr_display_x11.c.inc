/*
 * gr_display_x11.c.inc - the screen window of gr_render.c on X11 (included by
 * gr_render.c, shares its statics). The display interface: grd_start, grd_kick_layout,
 * grd_orientation (see gr_render.c); the X11 counterpart of the Win32 display.
 *
 * A UI thread owns an unmapped top-level window (WM_CLASS "gr_renderHostWnd",
 * _NET_WM_PID = QEMU's pid, title "OpenStacks display r<rotation>"); the front-end
 * finds it and embeds it by XID (XWayland on Wayland desktops). gfxstream shows its GL
 * subwindow inside it (gx_show_in, letterboxed); its subwindow selects no input, so
 * pointer and key events propagate to our window: mouse -> the guest's absolute pointer
 * + left button, keys (evdev keycode - 8) -> QEMU qcodes. When our window dies (its
 * embedding parent went away: X destroys a window's inferiors with it) a new one is
 * created and gfxstream's subwindow re-shown, as on Windows.
 *
 * Xlib is loaded at run time (dlopen): QEMU's link line stays unchanged and QEMU still
 * runs without libX11 (headless: frames only through the readback / VNC console).
 * GR_SHOW=1: map it as a normal top-level window (debug / standalone use).
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/Xatom.h>
#include <poll.h>

static void gx_drop_window(void);    /* gfxstream backend, below */
static void gx_repaint(void);

#define GRX_FUNCS(f) \
    f(XInitThreads) f(XOpenDisplay) f(XCreateWindow) f(XSelectInput) f(XStoreName) \
    f(XSetClassHint) f(XChangeProperty) f(XInternAtom) f(XMapWindow) f(XPending) \
    f(XNextEvent) f(XFlush) f(XSetErrorHandler) f(XSetInputFocus) f(XResizeWindow) \
    f(XGetErrorText) f(XSetWMProtocols) f(XUnmapWindow) f(XQueryTree) f(XFree)     f(XGrabPointer) f(XUngrabPointer) f(XWarpPointer) f(XCreateBitmapFromData)     f(XCreatePixmapCursor) f(XFreePixmap)

static struct {
#define GRX_PTR(n) __typeof__(n) *n;
    GRX_FUNCS(GRX_PTR)
#undef GRX_PTR
} x11;

static Display *g_xdpy;
static int g_xpipe[2] = { -1, -1 };  /* wakes the UI thread for requests */
static int g_xw, g_xh;               /* our window's size */
static int g_xReqLayout, g_xReqOrient = -1;
static int g_xReqLock = -1;          /* game controls pointer lock request (0/1) */
static bool g_xLockOn;
static Cursor g_xBlank;
static void grx_wake(void);
static bool g_xShow, g_xMouseDown;
static Atom g_xWmDelete;
static QemuThread g_xThread;

static int grx_error(Display *d, XErrorEvent *e)
{
    /* Xlib's default handler exits the process: BadWindow et al. are expected here
     * (the embedding parent took our window down, gfxstream touching its dead subwindow) */
    char t[128] = "";
    x11.XGetErrorText(d, e->error_code, t, sizeof(t));
    gr_once(0xe0 + (e->error_code & 0xf), "X error %s (request %u, resource 0x%lx)", t,
            (unsigned)e->request_code, (unsigned long)e->resourceid);
    return 0;
}

static bool grx_load(void)
{
    void *lib = gr_dlopen("libX11.so.6");
    char err[256];
    if (!lib) {
        gr_log("display: libX11.so.6: %s", gr_dlerror(err, sizeof(err)));
        return false;
    }
#define GRX_SYM(n) \
    if (!(*(void **)&x11.n = gr_dlsym(lib, #n))) { \
        gr_log("display: libX11 lacks %s", #n); \
        return false; \
    }
    GRX_FUNCS(GRX_SYM)
#undef GRX_SYM
    return true;
}

static void grx_title(void)
{
    char t[64];
    snprintf(t, sizeof(t), "OpenStacks display r%d", qatomic_read(&g_orient));
    x11.XStoreName(g_xdpy, (Window)g.hwnd, t);
}

static Window grx_make_window(void)
{
    int scr = DefaultScreen(g_xdpy);
    XSetWindowAttributes wa = {
        .background_pixel = BlackPixel(g_xdpy, scr),       /* the letterbox bars */
        .event_mask = StructureNotifyMask | SubstructureNotifyMask | ExposureMask |
                      ButtonPressMask | ButtonReleaseMask | PointerMotionMask |
                      KeyPressMask | KeyReleaseMask | FocusChangeMask,
    };
    Window w = x11.XCreateWindow(g_xdpy, RootWindow(g_xdpy, scr), 0, 0, GR_W, GR_H, 0,
                                 CopyFromParent, InputOutput, CopyFromParent,
                                 CWBackPixel | CWEventMask, &wa);
    if (!w) return 0;
    {
        XClassHint ch = { (char *)"gr_renderHostWnd", (char *)"OpenStacks" };
        long pid = (long)getpid();
        x11.XSetClassHint(g_xdpy, w, &ch);
        x11.XChangeProperty(g_xdpy, w, x11.XInternAtom(g_xdpy, "_NET_WM_PID", False),
                            XA_CARDINAL, 32, PropModeReplace, (unsigned char *)&pid, 1);
        x11.XSetWMProtocols(g_xdpy, w, &g_xWmDelete, 1);   /* close: ignored */
    }
    g_xw = GR_W;
    g_xh = GR_H;
    g.hwnd = (unsigned long)w;
    grx_title();
    if (g_xShow) x11.XMapWindow(g_xdpy, w);
    x11.XFlush(g_xdpy);
    return w;
}

/* Expose on gfxstream's GL subwindow too: when the picture fills our window (no
 * letterbox bars) the subwindow covers it completely and only the subwindow gets
 * Expose. Selecting events on another client's window is allowed (masks are per
 * client); its events come in on our connection. */
static void grx_watch_children(void)
{
    Window root, parent, *kids = NULL;
    unsigned n = 0, i;
    if (!g.hwnd || !x11.XQueryTree(g_xdpy, (Window)g.hwnd, &root, &parent, &kids, &n)) {
        return;
    }
    for (i = 0; i < n; i++) {
        x11.XSelectInput(g_xdpy, kids[i], ExposureMask);
    }
    if (kids) x11.XFree(kids);
}

static void grx_layout(void)
{
    if (gr_gfx() && g.hwnd) {
        gx_show_in((void *)(uintptr_t)g.hwnd, g_xw, g_xh);
        grx_watch_children();
    }
}

/* GR_SHOW: a top-level window follows the guest's aspect, like the Win32 one */
static void grx_fit_shown(int o)
{
    int w = (o & 1) ? GR_H : GR_W, h = (o & 1) ? GR_W : GR_H;
    int mx = DisplayHeight(g_xdpy, DefaultScreen(g_xdpy)) * 85 / 100;
    if (h > mx) {
        w = w * mx / h;
        h = mx;
    }
    x11.XResizeWindow(g_xdpy, (Window)g.hwnd, w, h);
}

static void grx_pointer(int x, int y, int down)
{
    /* relative to the guest's picture (gx_show_in's letterbox), not our whole window */
    int cw = g_cw > 0 ? g_cw : g_xw, ch = g_ch > 0 ? g_ch : g_xh;
    gr_pointer_view(x - (g_cw > 0 ? g_cx : 0), y - (g_ch > 0 ? g_cy : 0), cw, ch, down);
}

/* another button / the wheel, relative to the picture like grx_pointer (kind 3 / 4) */
static void grx_button(int kind, int x, int y, int qnum, int down)
{
    int cw = g_cw > 0 ? g_cw : g_xw, ch = g_ch > 0 ? g_ch : g_xh;
    gr_button_view(kind, x - (g_cw > 0 ? g_cx : 0), y - (g_ch > 0 ? g_cy : 0), cw, ch,
                   qnum, down);
}

/* Pointer lock (game controls aim mode), on the UI thread: grab the pointer with an
 * invisible cursor, confined to our window, and keep warping it back to the centre;
 * each move away from it is a relative motion. XWayland turns the hidden, grabbed,
 * warped pointer into Wayland relative motion + pointer confinement. */
static void grx_lock_apply(bool on)
{
    if (on == g_xLockOn || !g.hwnd) return;
    if (on) {
        if (!g_xBlank) {
            static char zero[8];
            XColor c = { 0 };
            Pixmap pm = x11.XCreateBitmapFromData(g_xdpy, (Window)g.hwnd, zero, 8, 8);
            g_xBlank = x11.XCreatePixmapCursor(g_xdpy, pm, pm, &c, &c, 0, 0);
            x11.XFreePixmap(g_xdpy, pm);
        }
        if (x11.XGrabPointer(g_xdpy, (Window)g.hwnd, True,
                             ButtonPressMask | ButtonReleaseMask | PointerMotionMask,
                             GrabModeAsync, GrabModeAsync, (Window)g.hwnd, g_xBlank,
                             CurrentTime) != GrabSuccess) {
            gr_log("game controls: pointer grab failed");
            return;
        }
        x11.XWarpPointer(g_xdpy, None, (Window)g.hwnd, 0, 0, 0, 0, g_xw / 2, g_xh / 2);
        g_xLockOn = true;
    } else {
        x11.XUngrabPointer(g_xdpy, CurrentTime);
        g_xLockOn = false;
    }
}

void gr_set_pointer_lock(bool on)
{
    qatomic_set(&g_xReqLock, on ? 1 : 0);
    grx_wake();
}

static void grx_event(XEvent *e)
{
    if (e->type == Expose && e->xany.window != (Window)g.hwnd) {
        /* gfxstream's subwindow (grx_watch_children) */
        if (e->xexpose.count == 0 && gr_gfx()) gx_repaint();
        return;
    }
    if (e->xany.window != (Window)g.hwnd) return;      /* a previous window's leftovers */
    switch (e->type) {
    case CreateNotify:                       /* gfxstream made its subwindow */
        if (e->xcreatewindow.parent == (Window)g.hwnd) {
            x11.XSelectInput(g_xdpy, e->xcreatewindow.window, ExposureMask);
        }
        break;
    case ConfigureNotify:
        /* SubstructureNotify also reports the subwindow's: only ours count */
        if (e->xconfigure.window != (Window)g.hwnd) break;
        if (e->xconfigure.width != g_xw || e->xconfigure.height != g_xh) {
            g_xw = e->xconfigure.width;
            g_xh = e->xconfigure.height;
            grx_layout();
        }
        break;
    case Expose:
        /* mapped again (the player's Screen tab shown after another tab: Qt unmaps
         * the hidden container) or uncovered: the GL subwindow keeps stale pixels
         * until the next guest frame, which an idle screen never sends */
        if (e->xexpose.count == 0 && gr_gfx()) gx_repaint();
        break;
    case ButtonPress:
    case ButtonRelease: {
        bool down = e->type == ButtonPress;
        unsigned b = e->xbutton.button;
        if (down) x11.XSetInputFocus(g_xdpy, (Window)g.hwnd, RevertToParent, CurrentTime);
        if (b == Button1) {
            g_xMouseDown = down;
            grx_pointer(e->xbutton.x, e->xbutton.y, down);
        } else if (b == Button4 || b == Button5) {          /* wheel: presses only */
            if (down) grx_button(4, e->xbutton.x, e->xbutton.y, b == Button4 ? 120 : -120, 0);
        } else if (b == Button2 || b == Button3 || b == 8 || b == 9) {
            grx_button(3, e->xbutton.x, e->xbutton.y,
                       b == Button3 ? OST_BTN_RIGHT : b == Button2 ? OST_BTN_MIDDLE
                       : b == 8 ? OST_BTN_X1 : OST_BTN_X2, down);
        }
        break;
    }
    case MotionNotify:
        if (g_xLockOn) {
            int dx = e->xmotion.x - g_xw / 2, dy = e->xmotion.y - g_xh / 2;
            if (dx || dy) {
                gr_input_post((GrInputEv){ .kind = 5, .x = dx, .y = dy });
                x11.XWarpPointer(g_xdpy, None, (Window)g.hwnd, 0, 0, 0, 0,
                                 g_xw / 2, g_xh / 2);
            }
        } else if (g_xMouseDown) {
            grx_pointer(e->xmotion.x, e->xmotion.y, -1);    /* touch move */
        } else if (gr_hover_wanted()) {
            grx_pointer(e->xmotion.x, e->xmotion.y, -2);
        }
        break;
    case FocusIn:
    case FocusOut:
        /* focus moving inside our window or the pointer-root dance: not a change */
        if (e->xfocus.detail == NotifyInferior || e->xfocus.detail == NotifyPointer) break;
        if (e->type == FocusOut) grx_lock_apply(false);
        gr_input_post((GrInputEv){ .kind = 6, .down = e->type == FocusIn });
        break;
    case KeyPress:
    case KeyRelease: {
        if (e->xkey.keycode >= 8) {          /* evdev-based servers: keycode = evdev + 8 */
            gr_input_post((GrInputEv){ .kind = 2, .qnum = (int)e->xkey.keycode - 8,
                                       .down = e->type == KeyPress });
        }
        break;
    }
    case ReparentNotify:
        if (e->xreparent.window != (Window)g.hwnd) break;
        /* back on the root: the embedder let go of us (or exited with us in its
         * save-set, which also maps us). Hidden again until embedded anew. */
        if (e->xreparent.parent == RootWindow(g_xdpy, DefaultScreen(g_xdpy))) {
            gr_log("display window 0x%lx released by its embedder", g.hwnd);
            if (!g_xShow) x11.XUnmapWindow(g_xdpy, (Window)g.hwnd);
        } else {
            gr_log("display window 0x%lx embedded in 0x%lx", g.hwnd,
                   (unsigned long)e->xreparent.parent);
        }
        break;
    case DestroyNotify:
        if (e->xdestroywindow.window == (Window)g.hwnd) {
            gr_log("display window 0x%lx destroyed%s", g.hwnd,
                   gr_gfx() ? ": recreating it" : "");
            g.hwnd = 0;
            g_recreate = gr_gfx();
        }
        break;
    }
}

static void *grx_thread(void *arg)
{
    (void)arg;
    for (;;) {
        struct pollfd p[2] = {
            { .fd = ConnectionNumber(g_xdpy), .events = POLLIN },
            { .fd = g_xpipe[0], .events = POLLIN },
        };
        int o;
        poll(p, 2, 500);
        if (p[1].revents & POLLIN) {
            char buf[64];
            while (read(g_xpipe[0], buf, sizeof(buf)) == (ssize_t)sizeof(buf)) {
            }
        }
        while (x11.XPending(g_xdpy)) {
            XEvent e;
            x11.XNextEvent(g_xdpy, &e);
            grx_event(&e);
        }
        if (g_recreate && g.ready) {
            g_recreate = false;
            gx_drop_window();                /* its subwindow died with ours */
            if (grx_make_window()) {
                gr_log("display window recreated: 0x%lx", g.hwnd);
                grx_layout();
            } else {
                gr_log("display window recreate FAILED");
            }
        }
        o = qatomic_xchg(&g_xReqOrient, -1);
        if (o >= 0 && g.hwnd) {
            grx_title();
            if (g_xShow) grx_fit_shown(o);
            grx_layout();                    /* the content aspect flipped */
        }
        if (qatomic_xchg(&g_xReqLayout, 0)) grx_layout();
        o = qatomic_xchg(&g_xReqLock, -1);
        if (o >= 0) grx_lock_apply(o != 0);
        x11.XFlush(g_xdpy);
    }
    return NULL;
}

static void grx_wake(void)
{
    if (g_xpipe[1] >= 0) {
        char c = 1;
        if (write(g_xpipe[1], &c, 1) < 0) {
            /* full pipe: the thread is awake anyway */
        }
    }
}

static bool grd_start(void)
{
    char b[4] = {0};
    g.hwnd = 0;
    g_xShow = gr_getenv("GR_SHOW", b, sizeof(b)) > 0 && b[0] == '1';
    if (!grx_load() || !x11.XInitThreads() || !(g_xdpy = x11.XOpenDisplay(NULL))) {
        g_readback = true;               /* the only way to see the screen */
        gr_log("display: no X11 display (DISPLAY unset or unreachable): headless, "
               "frames via readback");
        return true;
    }
    x11.XSetErrorHandler(grx_error);
    g_xWmDelete = x11.XInternAtom(g_xdpy, "WM_DELETE_WINDOW", False);
    if (!grx_make_window()) {
        gr_log("display: XCreateWindow failed");
        return false;
    }
    if (pipe(g_xpipe) == 0) {
        fcntl(g_xpipe[0], F_SETFL, O_NONBLOCK);
        fcntl(g_xpipe[1], F_SETFL, O_NONBLOCK);
    }
    qemu_thread_create(&g_xThread, "gr-display", grx_thread, NULL, QEMU_THREAD_DETACHED);
    gr_log("display: X11 window 0x%lx (%s)", g.hwnd, g_xShow ? "shown" : "for embedding");
    return true;
}

static void grd_kick_layout(void)
{
    qatomic_set(&g_xReqLayout, 1);
    grx_wake();
}

static void grd_orientation(int o)
{
    qatomic_set(&g_xReqOrient, o);
    grx_wake();
}
