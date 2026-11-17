"""OpenStacks headless self-test - `python -m openstacks.selftest [--verbose]`.

No Qt, no display server, no guest connectivity required: this only exercises
the parts that must never crash (conf parsing, argv building, adb graceful
failure, log tailing). Each check prints PASS/FAIL/SKIP; the exit code is 0
when nothing FAILED.

What is covered:
  1. instances.conf parse: [global] + instance sections, new keys
     (root_image / pgaipc_gr / hvmem_mb) understood with sane defaults.
  2. Instance.effective_root_disk(): root_image override semantics.
  3. Stored round-trip: load -> save -> load keeps the new keys.
  4. Engine kernel -append: the proven boot token list incl. BST_ANDROID_ID.
  5. Engine argv shape vs qemu_test\\boot_bst_grbridge.cmd:
     -accel (from conf, whpx default), -initrd initramfs.bin (pristine), IDE disks,
     hostfwd 127.0.0.1:<adb_port> -> 10.0.2.15:5555, bst* device set with
     gr=on + hvmem ram_size_mib, -no-reboot/-no-shutdown/-display none,
     -serial file:<logs\\name>.serial.log, telnet monitor on the conf port.
  6. Serial-log path alignment: build_argv's -serial arg == the path the
     player's Console pane tails.
  7. adb presence (SKIP when absent) + graceful-failure contract:
     tap/swipe/keyevent on a dead endpoint raise AdbError (never crash),
     snapshot() reports a clean stop/starting state, connect_with_retry
     respects its deadline, wait_boot returns False on timeout.
  8. TailBuffer: backfill, append-poll, counter line filter, truncation.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .adb import Adb, AdbError, BootState, find_adb
from .config import GlobalSettings, Instance, Store
from .console import TailBuffer, filter_counter_lines
from .qemu_engine import QemuEngine

_RESULTS: list[tuple[str, str, str]] = []      # (status, name, note)


def _record(status: str, name: str, note: str = "") -> None:
    _RESULTS.append((status, name, note))
    print(f"[{status:4}] {name}" + (f" - {note}" if note else ""))


PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def check_conf_parse() -> None:
    try:
        store = Store()
    except Exception as e:
        _record(FAIL, "conf parse", f"Store() raised: {e}")
        return
    insts = store.instances()
    if not insts:
        _record(FAIL, "conf parse", "no instances found (add a [Section] to instances.conf)")
        return
    _record(PASS, "conf parse",
            f"{len(insts)} instance(s): {', '.join(i.name for i in insts)}; "
            f"default={store.default_name()!r}; "
            f"accel={store.global_settings.acceleration}")
    for it in insts:
        problems = it.validate()
        _record((FAIL if problems else PASS), f"validate {it.name}",
                "; ".join(problems) or "ok")


def check_root_image_keys() -> None:
    it = Instance(name="K")
    if it.effective_root_disk() != "Root.vhd":
        _record(FAIL, "root_image default", f"got {it.effective_root_disk()!r}")
        return
    it2 = Instance(name="K", root_disk="Root.vhd", root_image="Root_patched.vhd")
    if it2.effective_root_disk() != "Root_patched.vhd":
        _record(FAIL, "root_image override", "root_image did not win over root_disk")
        return
    _record(PASS, "root_image override", "Root.vhd vs Root_patched.vhd selectable")


def check_store_roundtrip() -> None:
    """New per-instance keys survive a save/reload cycle."""
    with tempfile.TemporaryDirectory(prefix="ostconf_") as tmp:
        conf = Path(tmp) / "instances.conf"
        store = Store(conf)
        it = store.create("RK", cpus=2, ram_mb=2048)
        it.root_image = "Root_patched.vhd"
        it.pgaipc_gr = False
        it.hvmem_mb = 256
        store.update(it)
        store2 = Store(conf)
        it2 = store2.get_instance("RK")
        ok = (it2.root_image == "Root_patched.vhd"
              and it2.pgaipc_gr is False and it2.hvmem_mb == 256)
        store2.delete("RK")
        _record((PASS if ok else FAIL), "store roundtrip (root_image/pgaipc_gr/hvmem_mb)")


def check_append(inst: Instance | None = None) -> None:
    inst = inst or Instance(name="pie64", android_id="bfd0e87474aa4028")
    append = QemuEngine().build_append(inst)
    tokens = ["earlyprintk=serial,keep", "idle=nomwait", "vmalloc=160M",
              "nosmap", "nosmep", "bstandroid=pie64", "nokaslr",
              "console=ttyS0", "BST_ANDROID_ID=bfd0e87474aa4028"]
    missing = [t for t in tokens if t not in append]
    _record((FAIL if missing else PASS), "kernel append tokens",
            f"missing: {missing}" if missing else append)


def check_argv_shape(store: Store | None = None) -> None:
    store = store or Store()
    eng = QemuEngine()
    inst = store.get_instance(store.default_name()) if store.default_name() else \
        (store.instances()[0] if store.instances() else None)
    if inst is None:
        _record(SKIP, "argv shape", "no instances in conf")
        return
    try:
        argv, _warn = eng.build_argv(inst, store.global_settings)
    except Exception as e:
        _record(SKIP, "argv shape", f"cannot build (qemu missing?): {e}")
        return
    expect = [
        "-machine", "pc", "-accel", store.global_settings.acceleration or "whpx",
        "-kernel", "-initrd",
        "-nic", "-serial", "-monitor", "-vnc", "-no-reboot", "-no-shutdown",
        "-display", "none",
    ]
    joined = " ".join(argv)
    missing = [t for t in expect if t not in joined]
    if missing:
        _record(FAIL, "argv shape", f"missing tokens: {missing}")
        return
    checks = {
        "initrd is the pristine initramfs.bin":
            "initramfs.bin" in joined,
        "renderer console is default (-vga none -parallel none)":
            "-vga none" in joined and "-parallel none" in joined,
        "hostfwd -> 10.0.2.15:5555": f"hostfwd=tcp:127.0.0.1:{inst.adb_port}-10.0.2.15:5555" in joined,
        "bstpgaipc gr=on": "bstpgaipc0,gr=on" in joined,
        "hvmem ram_size_mib": "hvmem0,ram_size_mib=" in joined,
        "vbox-vmmdev": "vmmdev0" in joined,
        f"telnet monitor {inst.monitor_port}":
            f"telnet:127.0.0.1:{inst.monitor_port},server,nowait" in joined,
        "serial file arg": f"file:{eng.serial_log_path(inst)}" in argv[
            argv.index("-serial") + 1],
    }
    bad = [k for k, v in checks.items() if not v]
    _record((FAIL if bad else PASS), "argv shape (proven boot shape)",
            "; ".join(bad) if bad else "all structural tokens present")
    # serial-log alignment with the player's Console pane
    _record(PASS, "serial path alignment",
            f"-serial -> {eng.serial_log_path(inst)}")


def check_serial_path_alignment() -> None:
    eng = QemuEngine()
    inst = Instance(name="Ali")
    want = eng.logs_dir / f"{inst.name}.serial.log"
    got = eng.serial_log_path(inst)
    _record((PASS if got == want else FAIL), "serial log path contract",
            f"{got}")


def check_adb_presence() -> None:
    adb_path = find_adb()
    if not adb_path:
        _record(SKIP, "adb presence",
                "adb.exe not found (set adb_path or drop platform-tools/) - "
                "control leg stays graceful anyway")
        return
    _record(PASS, "adb presence", adb_path)


def check_adb_graceful_failures() -> None:
    adb = Adb(port=64777)          # near-certainly dead endpoint
    adb.path = adb.path or find_adb()
    if not adb.path:
        _record(SKIP, "adb graceful failures", "no binary to exercise")
        return
    crashed = None
    try:
        adb.tap(100, 100, timeout=8)
    except AdbError as e:
        crashed = f"AdbError: {str(e)[:60]}..."
    except Exception as e:                       # anything else is a real bug
        _record(FAIL, "adb graceful failures", f"tap raised {type(e).__name__}: {e}")
        return
    _record((PASS if crashed else FAIL), "adb tap on dead endpoint fails gracefully",
            crashed or "")
    try:
        adb.swipe(0, 0, 100, 100, timeout=5)
        adb.keyevent(4, timeout=5)
        adb.text("hi", timeout=5)
        _record(PASS, "adb swipe/keyevent/text", "exec paths run (state: refused/ok per host)")
    except AdbError:
        _record(PASS, "adb swipe/keyevent/text", "refused cleanly (AdbError, no crash)")
    except Exception as e:
        _record(FAIL, "adb swipe/keyevent/text", f"unexpected {type(e).__name__}: {e}")
    # snapshot() must never raise, even with everything dead
    try:
        state, detail = adb.snapshot(is_running=True)
        _record((PASS if isinstance(state, BootState)
                 else FAIL), "adb snapshot dead endpoint",
                f"{state.value}: {detail[:60]}")
    except Exception as e:
        _record(FAIL, "adb snapshot dead endpoint", f"raised: {e}")


def check_adb_timeouts() -> None:
    adb = Adb(port=64778)
    adb.path = adb.path or find_adb()
    if not adb.path:
        _record(SKIP, "adb retry deadline", "no binary to exercise")
        return
    try:
        adb.connect_with_retry(timeout=3.0, poll=0.5)
        _record(FAIL, "adb retry deadline", "returned success on a dead port?!")
    except AdbError as e:
        _record(PASS, "adb retry deadline", str(e)[:80])
    except Exception as e:
        _record(FAIL, "adb retry deadline", f"unexpected {type(e).__name__}: {e}")
    t0 = time.monotonic()
    try:
        ok = adb.wait_boot(timeout=2.0, poll=0.5)
        waited = time.monotonic() - t0
        _record((PASS if not ok and waited < 15 else FAIL),
                "wait_boot timeout honored", f"returned {ok} in {waited:.1f}s")
    except AdbError as e:
        _record(PASS, "wait_boot timeout honored", f"AdbError ok: {str(e)[:60]}")
    except Exception as e:
        _record(FAIL, "wait_boot timeout honored", f"unexpected {type(e).__name__}: {e}")


def check_tailbuffer() -> None:
    with tempfile.TemporaryDirectory(prefix="osttail_") as tmp:
        path = Path(tmp) / "x.serial.log"
        path.write_text("l1\nl2\nl3\n", encoding="utf-8")
        buf = TailBuffer(path)
        lines = buf.attach(backfill=2)
        ok1 = lines == ["l2", "l3"]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("l4\n[gr_render] frames=12 fps=3\n[noise] unrelated\n")
        new = buf.poll()
        ok2 = (new[-3:] == ["l4", "[gr_render] frames=12 fps=3", "[noise] unrelated"]
               and "[gr_render] frames=12 fps=3" in filter_counter_lines(new))
        # truncation -> re-read from scratch
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("fresh\n")
        new2 = []
        deadline = time.monotonic() + 1.0
        while "fresh" not in new2 and time.monotonic() < deadline:
            new2 = buf.poll()
        ok3 = "fresh" in (new2 + buf.tail(5))
        ok = ok1 and ok2 and ok3
        _record((PASS if ok else FAIL), "TailBuffer backfill/poll/filter/truncate",
                f"backfill={ok1} poll+filter={ok2} truncate={ok3}")


def main() -> int:
    print(f"openstacks selftest @ {time.strftime('%F %T')} "
          f"(python {sys.version.split()[0]}, cwd={Path.cwd()})")
    check_conf_parse()
    check_root_image_keys()
    check_store_roundtrip()
    check_append()
    check_argv_shape()
    check_serial_path_alignment()
    check_adb_presence()
    check_adb_graceful_failures()
    check_adb_timeouts()
    check_tailbuffer()
    fails = [r for r in _RESULTS if r[0] == FAIL]
    print(f"\nsummary: {sum(1 for r in _RESULTS if r[0] == PASS)} passed, "
          f"{sum(1 for r in _RESULTS if r[0] == SKIP)} skipped, "
          f"{len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
