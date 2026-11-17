# -*- coding: utf-8 -*-
"""Launch one guest boot with the proven QEMU argv (replaces the 51 boot_bst_*.cmd).

  python dev\\boot.py <run-name> [--root Root_p54.vhd] [--data Data_fresh.qcow2]
                     [--persist] [--exe dev|p59] [--env K=V ...]

Every run gets its own folder dev\\runs\\<run-name>\\ holding serial.log,
out.log (QEMU stdout+stderr = the [gr_render] lines), screen_pga.bmp (QEMU's cwd
is the run folder) and meta.txt. Refuses to reuse an existing run folder, so a
log can never be overwritten by the next boot.

Disks: images live in ..\\qemu_test. By default BOTH drives use snapshot=on
(writes go to a temp overlay, the image files stay byte-identical), so every
boot starts from the same disk state. --persist writes through (back up first!).
"""
import argparse
import os
import subprocess
import sys
import time

DEV = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(DEV)
IMAGES = os.path.join(PROJ, "qemu_test")
EXES = {
    "dev": os.path.join(DEV, "build", "qemu-system-x86_64.exe"),
    "p59": os.path.join(PROJ, "qemu_bst", "build", "qemu-system-x86_64.exe"),
}
# Phase 65 defaults = the working path (dev/NOTES.md "Current best configuration").
# Override any of them with --env K=V (e.g. --env GR_PURE=0 for the legacy lane).
BASE_ENV = {"GR_BRIDGE": "1", "GR_SYN_MODE": "1", "GR_RENDERER_MODE": "1",
            "GR_HPP": "0", "GR_RCVER": "3", "GR_PURE": "1", "PGA_SLOTSHIFT": "1"}
APPEND = ("earlyprintk=serial,keep idle=nomwait vmalloc=160M nosmap nosmep "
          "bstandroid=pie64 nokaslr console=ttyS0 BST_ANDROID_ID=bfd0e87474aa4028")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--root", default="Root_p50.vhd")
    ap.add_argument("--data", default="Data_fresh.qcow2")
    ap.add_argument("--initrd", default="initramfs_full_p49bak.bin")  # pristine .ko
    ap.add_argument("--vnc", type=int, default=None,
                    help="VNC display N (127.0.0.1:590N) showing the renderer console")
    ap.add_argument("--audio", default="dsound", choices=["dsound", "wav", "none"],
                    help="bstaudio backend: host speakers, runs/<name>/audio.wav, or silent")
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--cpu", default="qemu64,+ssse3,+sse4.1,+sse4.2,+popcnt",
                    help="QEMU -cpu (SSE4 family for houdini; host/AVX models crash the guest under WHPX)")
    ap.add_argument("--exe", default="dev", choices=sorted(EXES))
    ap.add_argument("--env", action="append", default=[], metavar="K=V")
    ap.add_argument("--props", default="host", choices=["host", "min"],
                    help="host: HD-Player-style bst.* props from bluestacks.conf "
                         "(tools/bstprops.py); min: bstvmsg built-in list only")
    ap.add_argument("--prop", action="append", default=[], metavar="K=V",
                    help="override one bst.* prop (e.g. --prop abi_list=x86,x64)")
    a = ap.parse_args()

    run = os.path.join(DEV, "runs", a.name)
    if os.path.exists(run):
        sys.exit("run folder exists: %s (pick a new name)" % run)
    exe = EXES[a.exe]
    for p in (exe, os.path.join(IMAGES, a.root), os.path.join(IMAGES, a.data)):
        if not os.path.exists(p):
            sys.exit("missing: %s" % p)
    os.makedirs(run)

    subprocess.run(["taskkill", "/f", "/im", "qemu-system-x86_64.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)

    env = dict(os.environ)
    env["PATH"] = r"C:\msys64\ucrt64\bin;" + env.get("PATH", "")
    env.update(BASE_ENV)
    for kv in a.env:
        k, _, v = kv.partition("=")
        env[k] = v

    snap = "" if a.persist else ",snapshot=on"
    vmsg = "bstvmsg,id=bstv0"
    if a.props == "host":
        sys.path.insert(0, os.path.join(DEV, "tools"))
        import bstprops
        ov = {"android_id": "bfd0e87474aa4028", "fb_width": "1600", "fb_height": "900",
              "dpi": "240", "cpus": "2", "ram": "4096", "status.adb_port": "25556"}
        ov.update(kv.split("=", 1) for kv in a.prop)
        pf = bstprops.write_props_file(os.path.join(run, "bst.props"), "Pie64", ov)
        vmsg += ",propsfile=" + str(pf).replace(",", ",,")
    img = lambda f: os.path.join(IMAGES, f).replace(",", ",,")
    argv = [
        exe, "-name", "openstacks-" + a.name,
        "-L", os.path.join(os.path.dirname(exe), "pc-bios"),
        "-machine", "pc", "-accel", "whpx", "-cpu", a.cpu, "-m", "4096", "-smp", "2",
        "-kernel", os.path.join(IMAGES, "kernel.bin"),
        "-initrd", os.path.join(IMAGES, a.initrd),
        "-append", APPEND,
        "-drive", "file=%s,format=vpc,if=ide,index=0%s" % (img(a.root), snap),
        "-drive", "file=%s,format=qcow2,if=ide,index=1%s" % (img(a.data), snap),
        "-nic", "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:25556-10.0.2.15:5555",
        "-device", vmsg,
        "-device", "bstpgaipc,id=bstpgaipc0,gr=on",
    ] + ({"dsound": ["-audiodev", "dsound,id=snd0"],
          "wav": ["-audiodev", "wav,id=snd0,path=" + os.path.join(run, "audio.wav")],
          "none": []}[a.audio]) + [
        "-device", "bstaudio,id=bstaudio0" + ("" if a.audio == "none" else ",audiodev=snd0"),
        "-device", "hvmem,id=hvmem0,ram_size_mib=512",
        "-device", "vbox-vmmdev,id=vmmdev0",
        "-no-reboot", "-no-shutdown", "-display", "none",
    ] + (["-vga", "none", "-parallel", "none", "-vnc", "127.0.0.1:%d" % a.vnc] if a.vnc is not None else []) + [
        "-serial", "file:" + os.path.join(run, "serial.log"),
        "-monitor", "telnet:127.0.0.1:4456,server,nowait",
    ]
    knobs = {k: env[k] for k in sorted(env) if k.startswith("GR_")}
    with open(os.path.join(run, "meta.txt"), "w") as f:
        f.write("started %s\nexe %s\nroot %s\ndata %s\npersist %s\nknobs %s\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), exe, a.root, a.data,
                   a.persist, knobs))
        f.write("argv %s\n" % subprocess.list2cmdline(argv))
    out = open(os.path.join(run, "out.log"), "wb")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    p = subprocess.Popen(argv, cwd=run, env=env, stdout=out,
                         stderr=subprocess.STDOUT, creationflags=flags)
    print("pid %d  run %s  knobs %s" % (p.pid, run, knobs))


if __name__ == "__main__":
    main()
