/* ost_renderer smoke test: start the renderer, open a channel, speak renderControl.
 *   ost_renderer_smoke.exe        (prints the GL strings and rcGetRendererVersion's reply)
 * The guest normally sends these from libEGL_emulation; byte layout per the encoders:
 * [u32 opcode][u32 total size][args...], replies are raw. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ost_renderer.h"

static void log_cb(void *user, int level, const char *msg) {
    (void)user;
    printf("[%c] %s\n", "EWID"[level & 3], msg);
}

static int read_full(OstChannel *ch, void *buf, size_t n) {
    size_t got = 0;
    while (got < n) {
        long r = ost_channel_read(ch, (char *)buf + got, n - got, 3000);
        if (r <= 0) return -1;
        got += (size_t)r;
    }
    return 0;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    OstRendererConfig cfg = {OST_RENDERER_API_VERSION, 1280, 720, 28, log_cb, NULL};
    if (ost_renderer_init(&cfg)) { puts("FAIL init"); return 1; }
    OstChannel *ch = ost_channel_open();
    if (!ch) { puts("FAIL channel"); return 1; }

    /* gfxstream's render thread first reads a u32 flags word (IOSTREAM_CLIENT_EXIT_SERVER
     * etc.) from a new channel, like the emulator's pipe; 0 = normal client. */
    unsigned flags = 0;
    ost_channel_write(ch, &flags, 4);

    unsigned req[2] = {10000, 8};               /* rcGetRendererVersion */
    ost_channel_write(ch, req, sizeof req);
    int ver = 0;
    if (read_full(ch, &ver, 4)) { puts("FAIL no reply to rcGetRendererVersion"); return 1; }
    printf("rcGetRendererVersion = %d\n", ver);

    /* rcGetGLString(GL_RENDERER=0x1F01, buf, 256): [op][size][name][__size_buffer][bufferSize] */
    unsigned req2[5] = {10003, 4 * 5, 0x1F01, 256, 256};
    ost_channel_write(ch, req2, sizeof req2);
    char s[256];
    int len = 0;
    if (read_full(ch, s, 256) || read_full(ch, &len, 4)) { puts("FAIL rcGetGLString"); return 1; }
    s[255] = 0;
    printf("rcGetGLString(GL_RENDERER) = %d \"%s\"\n", len, s);

    ost_channel_close(ch);
    ost_renderer_shutdown();
    puts(ver > 0 ? "OK" : "FAIL");
    return ver > 0 ? 0 : 1;
}
