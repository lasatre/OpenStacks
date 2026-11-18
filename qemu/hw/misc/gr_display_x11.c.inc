/*
 * gr_display_x11.c.inc - the screen window of gr_render.c off Windows (included by
 * gr_render.c, shares its statics). The display interface: grd_start, grd_kick_layout,
 * grd_orientation (see gr_render.c).
 *
 * Headless for now (docs/linux_port_plan.md phase 2/3): no window; gfxstream renders
 * off-screen and frames reach the VNC console through its readback. Phase 4 adds the
 * X11 window the front-end embeds by XID.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

static bool grd_start(void)
{
    g.hwnd = 0;
    g_readback = true;               /* the only way to see the screen while headless */
    gr_log("display: headless (no X11 window yet), frames via readback");
    return true;
}

static void grd_kick_layout(void)
{
}

static void grd_orientation(int o)
{
    (void)o;
}
