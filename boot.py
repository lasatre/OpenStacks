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
BASE_ENV = {"GR_BRIDGE": "1", "GR_SYN_MODE": "1", "GR_RENDERER_MODE": "1",
            "GR_HPP": "1", "GR_RCVER": "3"}   # GR_RCVER MUST stay 3
APPEND = ("earlyprintk=serial,keep idle=nomwait vmalloc=160M nosmap nosmep "
          "bstandroid=pie64 nokaslr console=ttyS0 BST_ANDROID_ID=bfd0e87474aa4028")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--root", default="Root_p54.vhd")
    ap.add_argument("--data", default="Data_fresh.qcow2")
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--exe", default="dev", choices=sorted(EXES))
    ap.add_argument("--env", action="append", default=[], metavar="K=V")
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
    img = lambda f: os.path.join(IMAGES, f).replace(",", ",,")
    argv = [
        exe, "-name", "openstacks-" + a.name,
        "-L", os.path.join(os.path.dirname(exe), "pc-bios"),
        "-machine", "pc", "-accel", "whpx", "-m", "4096", "-smp", "2",
        "-kernel", os.path.join(IMAGES, "kernel.bin"),
        "-initrd", os.path.join(IMAGES, "initramfs_full.bin"),
        "-append", APPEND,
        "-drive", "file=%s,format=vpc,if=ide,index=0%s" % (img(a.root), snap),
        "-drive", "file=%s,format=qcow2,if=ide,index=1%s" % (img(a.data), snap),
        "-nic", "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:25556-10.0.2.15:5555",
        "-device", "bstvmsg,id=bstv0",
        "-device", "bstpgaipc,id=bstpgaipc0,gr=on",
        "-device", "bstaudio,id=bstaudio0",
        "-device", "hvmem,id=hvmem0,ram_size_mib=512",
        "-device", "vbox-vmmdev,id=vmmdev0",
        "-no-reboot", "-no-shutdown", "-display", "none",
        "-serial", "file:" + os.path.join(run, "serial.log"),
        "-monitor", "telnet:127.0.0.1:4446,server,nowait",
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
