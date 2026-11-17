# -*- coding: utf-8 -*-
"""Build the host props text HD-Player serves to the guest's bstconf (GetProps).

Real BlueStacks: bstconf (initramfs) asks the host for its props, bstsetconf.sh
writes them to /.bstconf.prop -> /data/.bstconf.prop and init loads them.
HD-Player sends a fixed whitelist of bluestacks.conf keys: the global bst.*
keys, the instance's bst.instance.<name>.* keys with the prefix dropped, plus
bst.oem / bst.version / bst.instance. The whitelist below is the exact key set
of a real /data/.bstconf.prop (BlueStacks 5.21.550, Pie64), minus the identity
keys bstsetconf.sh generates itself (serialno, imei_id, ...).
Secrets in bluestacks.conf (refresh_token, nowgg_*, ...) are never sent.

  python dev\\tools\\bstprops.py [--instance Pie64] [--out file] [k=v ...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CONF = Path(r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf")
ENGINE = Path(r"C:\ProgramData\BlueStacks_nxt\Engine")
PLAYER = Path(r"C:\Program Files\BlueStacks_nxt\HD-Player.exe")

# fallbacks when no BlueStacks install is readable (values of 5.21.550 / nxt)
DEFAULTS = {
    "bst.oem": "nxt",
    "bst.version": "5.21.550.1031",
    "bst.country": "US",
    "bst.locale": "en-US",
    "bst.abi_list": "x86,x64,arm,arm64",
    "bst.enable_adb_access": "1",
    "bst.enable_root_access": "1",
    "bst.graphics_engine": "aga",
    "bst.graphics_renderer": "vlcn",
    "bst.dns_server": "8.8.8.8",
    "bst.dns_server2": "10.0.2.3",
    "bst.status.ip_addr_prefix_len": "24",
    "bst.status.ip_gateway_addr": "10.0.2.2",
    "bst.status.ip_guest_addr": "10.0.2.15",
    "bst.status.hypervisor": "hyperv",
    "bst.status.raw_mode": "0",
    "bst.status.sse4_available": "1",
    "bst.status.ssse3_available": "1",
    "bst.max_fps": "60",
    "bst.enable_statusbar": "1",
    "bst.enable_navigationbar": "0",
}

# global (non-instance) keys HD-Player forwards
GLOBAL_KEYS = """
bluestacks_cdn_url bluestacks_cloud_url bluestacks_cloud_url2 bluestacks_eb_url country
dns_server dns_server2 enable_adb_access enable_adb_remote_access
enable_android_ads_test_app enable_esc_fullscreen enable_esc_mousecursor
enable_gamepad_detection enable_gamepad_vibration enable_image_detection enable_mem_dedup
enable_native_gamepad enable_navigationbar enable_programmatic_ads enable_sigin_gamelaunch
enable_statusbar feature.app_install_stats feature.creator_studio feature.send_offer_stats
feature.show_cloud_instance force_raw_mode fresh_cpu_core fresh_cpu_ram guid
host_user_agent iap_date install_date install_id installed_images locale log_levels
machine_id mem_opt_mode mem_pcd_enabled mem_pcd_pclimit mem_pcr_enabled mem_pcr_pclimit
mim_win_height mim_win_screen mim_win_x mim_win_y mute_all_instances next_vm_id
prefer_dedicated_gpu qt_renderer shared_folders sso_host_env status.hypervisor
status.raw_mode status.sse4_available status.ssse3_available version_machine_id
""".split()

# per-instance keys (bst.instance.<name>.<key> -> bst.<key>)
INSTANCE_KEYS = """
abi_list adb_port ads_app_activity ads_app_package ads_limit_min_pixels ads_screen_width
ads_screen_width_percentage airplane_mode_active android_google_ad_id android_id
android_sound_while_tapping app_launch_count autohide_notifications boot_duration cpus
device_carrier_code device_country_code device_profile_code dpi enable_fps_display
enable_fullscreen_all_apps enable_high_fps enable_logcat_redirection enable_notifications
enable_root_access enable_vsync fb_height fb_width first_boot game_controls_enabled
gl_win_height gl_win_screen gl_win_x gl_win_y google_login_popup_shown graphics_engine
graphics_renderer libc_mem_allocator max_fps nowbux_signin_completed pin_to_top ram
split_ad_enabled split_ad_show_times status.adb_port status.ip_addr_prefix_len
status.ip_gateway_addr status.ip_guest_addr status.session_id
""".split()

_LINE = re.compile(r'^(bst\.[^=\s]+)="(.*)"\s*$')


def read_conf(path: Path = CONF) -> dict[str, str]:
    kv: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return kv
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            kv[m.group(1)] = m.group(2)
    return kv


def _oem(instance: str) -> str | None:
    for cfg in (ENGINE / instance / f"oem_{instance}.cfg", ENGINE / "UserData" / "oem.cfg"):
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                k, _, v = line.partition("=")
                if k.strip().lower() == "oem" and v.strip():
                    return v.strip()
        except OSError:
            pass
    return None


def _version() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\BlueStacks_nxt") as k:
            return winreg.QueryValueEx(k, "Version")[0]
    except (OSError, ImportError):
        return None


def build_props(instance: str = "Pie64", overrides: dict[str, str] | None = None,
                conf: dict[str, str] | None = None) -> dict[str, str]:
    """Ordered bst.* props as HD-Player would serve them for `instance`.
    `instance` picks the bluestacks.conf section (falls back to Pie64)."""
    conf = read_conf() if conf is None else conf
    sect = instance
    if not any(k.startswith(f"bst.instance.{sect}.") for k in conf):
        sect = "Pie64"
    out: dict[str, str] = dict(DEFAULTS)
    for k in GLOBAL_KEYS:
        if f"bst.{k}" in conf:
            out[f"bst.{k}"] = conf[f"bst.{k}"]
    for k in INSTANCE_KEYS:
        v = conf.get(f"bst.instance.{sect}.{k}")
        if v is not None:
            out[f"bst.{k}"] = v
    out["bst.oem"] = _oem(sect) or out["bst.oem"]
    out["bst.version"] = _version() or out["bst.version"]
    out["bst.instance"] = sect
    out["bst.enable_adb_access"] = "1"          # adbd gate (NOTES 10h)
    for k, v in (overrides or {}).items():
        out[k if k.startswith("bst.") else f"bst.{k}"] = str(v)
    return out


def props_text(props: dict[str, str]) -> str:
    # bstsetconf.awk splits on '=' and exports NAME=value; keep values single-line
    return "".join(f"{k}={str(v).replace(chr(10), ' ')}\n" for k, v in props.items())


def write_props_file(path, instance: str = "Pie64",
                     overrides: dict[str, str] | None = None) -> Path:
    path = Path(path)
    path.write_text(props_text(build_props(instance, overrides)), encoding="utf-8",
                    newline="\n")
    return path


def main() -> None:
    a = sys.argv[1:]
    inst = a[a.index("--instance") + 1] if "--instance" in a else "Pie64"
    out = a[a.index("--out") + 1] if "--out" in a else None
    ov = dict(x.split("=", 1) for x in a if "=" in x and not x.startswith("--"))
    text = props_text(build_props(inst, ov))
    if out:
        Path(out).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {out} ({text.count(chr(10))} props)")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
