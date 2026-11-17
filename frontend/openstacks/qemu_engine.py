"""QEMU spawn engine for OpenStacks instances.

Responsibilities:
  * build the full QEMU argv for an instance from the Store, mirroring the
    proven boot shape of qemu_test\\boot_bst_grbridge.cmd
    (a.k.a. boot_bst_patched.cmd / boot_bst_full.cmd):
        pc machine + whpx/tcg, -kernel kernel.bin -initrd initramfs_full.bin,
        IDE Root[.vhd|_patched.vhd] + Data.qcow2, virtio-net user with hostfwd
        to 10.0.2.15:5555, the bst* paravirt set
            bstvmsg / bstpgaipc,gr=on / bstaudio / hvmem,ram_size_mib=512 /
            vbox-vmmdev
        -no-reboot -no-shutdown, -display none (-vnc kept for the RFB route),
        -serial file:<log>, -monitor telnet:<port>,server,nowait.
    The GR_* host env knobs (GR_BRIDGE / GR_SYN_MODE / GR_RENDERER_MODE /
    GR_HPP=1 / GR_RCVER=3) are set exactly like the proven .cmd when
    bstpgaipc gr=on is active.
  * start/stop/poll a QEMU process (QemuInstance wrapper).
  * per-instance log files (serial log via -serial file:, stdout/stderr into
    logs/<name>.qemu.log).
  * graceful shutdown through the telnet monitor ("quit"), with terminate()
    and kill() fallbacks.
  * detect whether the QEMU binary supports the bst* devices and transparently
    fall back to a bst-less stock-QEMU argv (with a warning).

Pure stdlib; usable from CLI (`python -m openstacks.qemu_engine <name> --argv`)
and from Qt worker threads.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .config import GlobalSettings, Instance, REPO_ROOT

LOGS_DIR = REPO_ROOT / "logs"
MSYS2_BIN = r"C:\msys64\ucrt64\bin"          # DLLs needed by the patched build
# Phase-73 guest CPU: qemu64 lacks SSSE3/SSE4 (libhoudini aborts ARM apps,
# 0xdead0000); "host"/AVX models crash the initramfs sh under WHPX (AVX
# state), so: the baseline + the SSE4 family. Override with OST_CPU.
# Phase-76: +invtsc + vmware-cpuid-freq (with -accel whpx,hyperv=off) make
# QEMU report VMware and the TSC frequency, so the guest kernel uses the TSC
# clocksource instead of the HPET, whose every read is an MMIO exit. (Real
# BlueStacks gives the guest a paravirtual KVM clock for the same reason.)
QEMU_CPU = "qemu64,+ssse3,+sse4.1,+sse4.2,+popcnt,+invtsc,vmware-cpuid-freq=on"

def disk_format(path: Path) -> str:
    return {".qcow2": "qcow2", ".vhdx": "vhdx", ".vhd": "vpc",
            ".vdi": "vdi"}.get(path.suffix.lower(), "raw")


def resolve_data_disk(inst: Instance, g: GlobalSettings, create: bool = True) -> Path | None:
    """The instance's /data disk. data_disk="" -> <instances_dir>/<name>/Data.vhdx,
    created from the assets' Data_template.vhdx (BlueStacks' blank Data_orig.vhdx)
    on first use. Relative names resolve against the assets dir (legacy layout)."""
    if inst.data_disk:
        p = Path(inst.data_disk)
        return p if p.is_absolute() else Path(g.resolved_assets_dir() or ".") / p
    p = Path(g.resolved_instances_dir()) / inst.name / "Data.vhdx"
    if create and not p.exists():
        tmpl = Path(g.resolved_assets_dir() or ".") / "Data_template.vhdx"
        if tmpl.exists():
            import shutil
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(tmpl, p)
    return p


DEFAULT_BST_DEVICES = ("bstvmsg", "bstpgaipc", "bstaudio", "hvmem", "vbox-vmmdev")

# Kernel args the pristine BlueStacks initramfs needs (per FINDINGS.md:
# nokaslr REQUIRED; no ahci on pc; earlyprintk is the only console). Exact
# token order of the proven boot cmds:
BASE_KERNEL_APPEND = (
    "earlyprintk=serial,keep idle=nomwait vmalloc=160M "
    "nosmap nosmep nokaslr console=ttyS0 "
    # Phase-75: bstaudio shares IRQ 10 with bstpgaipc and its ISR printk()s
    # "Unknown cmd = 0" (KERN_ERR) on every graphics IRQ; at loglevel 3 they
    # stay in dmesg instead of the (slow, trapping) serial console.
    "loglevel=3"
)

# -device id names exactly as boot_bst_grbridge.cmd uses them, so the guest's
# bstvmsg/bstpgaipc consumers see the same PCI identities in every boot.
BST_DEVICE_IDS = {
    "bstvmsg": "bstv0",
    "bstpgaipc": "bstpgaipc0",
    "bstaudio": "bstaudio0",
    "hvmem": "hvmem0",
    "vbox-vmmdev": "vmmdev0",
}

# GR_* host env knobs: the patched QEMU's grbridge render path reads these at
# startup (Phase-2/Phase-19 proven combo, pinned for deterministic boots).
GR_ENV = {
    "GR_BRIDGE": "1",
    "GR_SYN_MODE": "1",
    "GR_RENDERER_MODE": "1",
    "GR_HPP": "0",          # dev/NOTES.md 9: HPP only when the handshake asks (sf: "0 0 0")
    "GR_RCVER": "3",
    "GR_PURE": "1",         # Phase-64 faithful HstSocketHost lane
    "PGA_SLOTSHIFT": "1",   # Phase-63: channel c lives at BAR1 slot c+1
}
DEFAULT_INITRD = "initramfs.bin"   # pristine BlueStacks initramfs (tools/prepare_assets.py)


class QemuNotFoundError(RuntimeError):
    pass


@dataclass
class LaunchResult:
    ok: bool
    argv: list[str]
    warnings: list[str]
    error: str = ""


_DEVICE_CACHE: dict[str, dict[str, str]] = {}


def qemu_device_map(qemu_path: str, timeout: float = 30.0) -> dict[str, str]:
    """`qemu -device help` -> {name: help-line}. Cached; {} on failure."""
    if qemu_path in _DEVICE_CACHE:
        return _DEVICE_CACHE[qemu_path]
    try:
        proc = subprocess.run([qemu_path, "-device", "help"], capture_output=True,
                              text=True, errors="replace", timeout=timeout)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        _DEVICE_CACHE[qemu_path] = {}
        return {}
    found: dict[str, str] = {}
    for m in re.finditer(r'name "([^"]+)"[^\n]*', out, re.IGNORECASE):
        found[m.group(1)] = m.group(0).strip()
    _DEVICE_CACHE[qemu_path] = found
    return found


class QemuEngine:
    """Builds argv and owns the running QemuInstance objects."""

    def __init__(self, logs_dir: str | Path | None = None):
        self.logs_dir = Path(logs_dir) if logs_dir else LOGS_DIR
        self.instances: dict[str, "QemuInstance"] = {}

    # -- argv ----------------------------------------------------------------
    def build_argv(self, inst: Instance, g: GlobalSettings) -> tuple[list[str], list[str]]:
        """Return (argv, warnings); raises QemuNotFoundError if no qemu binary."""
        warnings: list[str] = []
        qemu = g.resolved_qemu_path()
        if not qemu or not Path(qemu).exists():
            raise QemuNotFoundError(
                "qemu-system-x86_64.exe not found; set global 'qemu_path' in "
                "instances.conf (patched build needs C:\\msys64\\ucrt64\\bin on PATH)."
            )
        assets = Path(g.resolved_assets_dir() or ".")
        kernel = assets / inst.kernel
        initrd = assets / (inst.initrd or DEFAULT_INITRD)
        root = assets / inst.effective_root_disk()
        try:
            data = resolve_data_disk(inst, g, create=True)
        except OSError as e:
            warnings.append(f"data disk not created: {e}")
            data = None
        for p, what in ((kernel, "kernel.bin"),
                        (initrd, inst.initrd or DEFAULT_INITRD)):
            if not p.exists():
                warnings.append(f"missing {what} at {p}")
        if not root.exists():
            warnings.append(f"missing root disk {root}")
        if data is None or not data.exists():
            warnings.append(f"missing data disk {data} (run tools/prepare_assets.py)")
        if initrd.name == "initramfs_full.bin":
            warnings.append(
                "initramfs_full.bin carries the Phase-50/51 patched bstpgaipc.ko "
                "(32-bit apps panic the kernel); use the pristine initramfs.bin")

        argv: list[str] = [qemu]
        argv += ["-name", f"openstacks-{inst.name}"]
        # patched build needs its pc-bios dir on -L; derive next to the exe
        bios = g.qemu_bios_dir or str(Path(qemu).parent / "pc-bios")
        if Path(bios).exists():
            argv += ["-L", bios]

        accel = g.acceleration or "whpx"
        if accel == "whpx":
            accel = "whpx,hyperv=off"   # Phase-76: VMware leaf + TSC freq
        argv += ["-machine", "pc", "-accel", accel]
        # Phase-73: QEMU's default qemu64 model has no SSSE3/SSE4/POPCNT/AVX;
        # libhoudini (ARM translation) and games abort without them.
        argv += ["-cpu", getattr(g, "cpu_model", "") or os.environ.get("OST_CPU", QEMU_CPU)]
        argv += ["-m", str(inst.ram_mb), "-smp", str(inst.cpus)]
        argv += ["-kernel", str(kernel), "-initrd", str(initrd)]
        argv += ["-append", self.build_append(inst)]

        # IDE disks exactly like the proven boot shell (never ahci on pc!)
        if root.exists():
            # snapshot=on: the root image stays pristine (guest writes go to a
            # temporary overlay); user data lives on the data disk.
            argv += ["-drive", f"file={root},format=vpc,if=ide,index=0,snapshot=on"]
        if data is not None and data.exists():
            argv += ["-drive", f"file={data},format={disk_format(data)},if=ide,index=1"]

        argv += ["-nic", f"user,model=virtio-net-pci,"
                         f"hostfwd=tcp:127.0.0.1:{inst.adb_port}-10.0.2.15:5555"]

        # bst* paravirt devices, with stock-QEMU fallback. Device options match
        # the proven shapes: bstpgaipc gets gr=on when inst.pgaipc_gr, and
        # hvmem pins ram_size_mib (boot_bst_grbridge.cmd: 512).
        supported = set(qemu_device_map(qemu))
        for dev in (inst.device_list() or list(DEFAULT_BST_DEVICES)):
            if supported and dev not in supported:
                warnings.append(
                    f"-device {dev} unsupported by {qemu}; skipped (stock-QEMU "
                    "fallback; lack of bstvmsg will panic the initramfs at module load)."
                )
                continue
            opts = f",id={BST_DEVICE_IDS.get(dev, 'ost-' + dev)}"
            if dev == "bstpgaipc" and inst.pgaipc_gr:
                opts += ",gr=on"
            # Phase-76: pin the slots so bstaudio (PIRQ via slot 12 -> IRQ 11) never
            # shares bstpgaipc's line (slot 5 -> IRQ 10): its ISR printk()s on every
            # foreign IRQ, flooding dmesg and the serial console.
            if dev == "bstpgaipc":
                opts += ",addr=0x05"
            if dev == "bstaudio":
                opts += ",addr=0x0c"
            if dev == "hvmem":
                opts += f",ram_size_mib={int(inst.hvmem_mb)}"
            if dev == "bstvmsg":
                # HD-Player-style bst.* host props (oem/version/abi_list/adb gate,
                # graphics, locale...) from bluestacks.conf (dev/NOTES.md 10h/10i)
                try:
                    pf = self.write_props(inst)
                    opts += ",propsfile=" + str(pf).replace(",", ",,")
                except OSError as e:
                    warnings.append(f"bst props not written ({e}); using built-ins")
            if dev == "bstaudio":
                # Phase-69: real playback through the host's DirectSound
                argv += ["-audiodev", "dsound,id=snd0"]
                opts += ",audiodev=snd0"
            argv += ["-device", f"{dev}{opts}"]

        # Debug-boot behaviour of the proven shells: freeze the console at a
        # panic instead of re-booting, and keep QEMU on screen after death.
        argv += ["-no-reboot", "-no-shutdown", "-display", "none"]
        # Phase-67: bstpgaipc (gr=on) registers the renderer's frames as a QEMU
        # graphic console. With no VGA and no parallel vc it is the default
        # console, so plain -vnc shows Android's real screen.
        if inst.pgaipc_gr and "bstpgaipc" in (inst.device_list() or DEFAULT_BST_DEVICES):
            argv += ["-vga", "none", "-parallel", "none"]
        # QEMU -vnc takes host:DISPLAY (TCP port = 5900 + DISPLAY)
        argv += ["-vnc", f"127.0.0.1:{int(inst.vnc_port) - 5900}"]
        argv += ["-serial", f"file:{self.serial_log_path(inst)}"]
        argv += ["-monitor", f"telnet:127.0.0.1:{inst.monitor_port},server,nowait"]

        if g.fullscreen:
            warnings.append("global fullscreen flag is handled by the player UI, not QEMU")
        if g.priority == "high":
            warnings.append("global priority=high not wired yet (needs ProcessPriorityClass)")
        return argv, warnings

    def write_props(self, inst: Instance) -> Path:
        from . import bstprops
        w, h = inst.res_wh()
        ov = {"fb_width": w, "fb_height": h, "dpi": inst.dpi, "cpus": inst.cpus,
              "ram": inst.ram_mb, "status.adb_port": inst.adb_port}
        if inst.android_id:
            ov["android_id"] = inst.android_id
        path = self.serial_log_path(inst).with_name(f"{inst.name}.bst.props")
        path.parent.mkdir(parents=True, exist_ok=True)
        return bstprops.write_props_file(path, inst.name, ov)

    def build_append(self, inst: Instance) -> str:
        """Kernel cmdline; includes the BST_ANDROID-style property injection hook.

        BST_ANDROID_ID / BST_SERNO ride the cmdline into populate_environment
        of the pristine initramfs (see FINDINGS.md, 'remaining blocker').
        """
        parts = [BASE_KERNEL_APPEND, f"bstandroid={inst.name.lower()}"]
        if inst.android_id:
            parts.append(f"BST_ANDROID_ID={inst.android_id}")
        if inst.serial_number:
            parts.append(f"BST_SERNO={inst.serial_number}")
        if inst.extra_append:
            parts.append(inst.extra_append.strip())
        return " ".join(p for p in parts if p)

    # -- paths ---------------------------------------------------------------
    def serial_log_path(self, inst: Instance) -> Path:
        return self.logs_dir / f"{inst.name}.serial.log"

    def qemu_log_path(self, name: str) -> Path:
        return self.logs_dir / f"{name}.qemu.log"

    def pid_path(self, name: str) -> Path:
        return self.logs_dir / f"{name}.pid"

    # -- lifecycle -----------------------------------------------------------
    def start(self, inst: Instance, g: GlobalSettings) -> LaunchResult:
        argv, warnings = self.build_argv(inst, g)
        fatal = [w for w in warnings if w.startswith("missing kernel")
                 or w.startswith("missing initramfs")]
        if fatal:
            return LaunchResult(False, argv, warnings,
                                "kernel/initramfs missing - fix assets_dir in instances.conf")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if "qemu_bst" in argv[0] or Path(argv[0]).parent.name == "build":
            env["PATH"] = MSYS2_BIN + os.pathsep + env.get("PATH", "")
            env.setdefault("QEMU_AUDIO_DRV", "none")
        # GR_* knobs: the grbridge render path reads env at QEMU startup; the
        # proven boot cmds pin them so the boot is deterministic regardless of
        # the launching shell. Active only when bstpgaipc gr=on is in play.
        if inst.pgaipc_gr and "bstpgaipc" in inst.device_list():
            env.update(GR_ENV)
            # Phase-75: native = the player embeds the renderer window (GPU
            # present); the 30 fps pixel readback only feeds the VNC console.
            env["GR_READBACK"] = "0" if inst.display == "native" else "1"
            # Phase-77: the renderer framebuffer = the instance resolution
            fbw, fbh = inst.res_wh()
            env["GR_FB_W"], env["GR_FB_H"] = str(fbw), str(fbh)
        try:
            logf = open(self.qemu_log_path(inst.name), "ab")
        except OSError as e:
            return LaunchResult(False, argv, warnings, f"cannot open qemu log: {e}")
        with logf:
            logf.write(f"\n=== openstacks starting {inst.name} "
                       f"@ {time.strftime('%F %T')} ===\n".encode())
            # cwd = QEMU's own dir so -L bios names and relative DLLs resolve
            cwd = str(Path(argv[0]).parent)
            try:
                proc = subprocess.Popen(
                    argv, stdout=logf, stderr=subprocess.STDOUT, cwd=cwd,
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            except OSError as e:
                return LaunchResult(False, argv, warnings, f"failed to spawn QEMU: {e}")
            for w in warnings:
                try:
                    logf.write(f"[openstacks] {w}\n".encode())
                except OSError:
                    pass
        inst_obj = QemuInstance(inst.name, proc, argv, self)
        self.instances[inst.name] = inst_obj
        try:
            self.pid_path(inst.name).write_text(str(proc.pid), encoding="ascii")
        except OSError:
            pass
        return LaunchResult(True, argv, warnings)

    def get(self, name: str) -> "QemuInstance | None":
        return self.instances.get(name)

    def stop(self, name: str, timeout: float = 12.0, force: bool = False) -> bool:
        obj = self.instances.get(name)
        if obj is None:
            return False
        ok = obj.stop(timeout=timeout, force=force)
        if ok:
            self.instances.pop(name, None)
            try:
                self.pid_path(name).unlink()
            except OSError:
                pass
        return ok

    def pid(self, inst: Instance | str) -> int | None:
        """QEMU pid of a running instance (None when stopped)."""
        name = inst.name if isinstance(inst, Instance) else inst
        obj = self.instances.get(name)
        if obj is not None:
            return obj.proc.pid if obj.is_running() else None
        pid = self._read_pidfile(name)
        return pid if pid is not None and _pid_alive(pid) else None

    def monitor(self, inst: Instance, command: str, timeout: float = 3.0) -> str:
        """Run one HMP command on the instance's telnet monitor."""
        with socket.create_connection(("127.0.0.1", int(inst.monitor_port)),
                                      timeout=timeout) as s:
            s.settimeout(timeout)
            buf = b""
            while b"(qemu)" not in buf:          # banner + first prompt
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            s.sendall(command.encode() + b"\r\n")
            out = b""
            try:
                while b"(qemu)" not in out:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    out += chunk
            except socket.timeout:
                pass
            return out.decode(errors="replace")

    def send_key(self, inst: Instance, qcode: str) -> None:
        """Press+release one key on the guest PS/2 keyboard (instant, no adb).
        Android Generic.kl: ac_back (158) BACK, ac_home (172) HOME,
        audionext (163) APP_SWITCH."""
        self.monitor(inst, f"sendkey {qcode}")

    def is_running(self, inst: Instance | str) -> bool:
        name = inst.name if isinstance(inst, Instance) else inst
        obj = self.instances.get(name)
        if obj is not None:
            return obj.is_running()
        pid = self._read_pidfile(name)
        return pid is not None and _pid_alive(pid)

    def _read_pidfile(self, name: str) -> int | None:
        try:
            return int(self.pid_path(name).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# Module-level singleton shared by the manager and the player windows, so
# process launch/stop state lives in exactly one registry.
ENGINE = QemuEngine()


class QemuInstance:
    """A running (or exited) QEMU process for one instance."""

    def __init__(self, name: str, proc: subprocess.Popen, argv: list[str],
                 engine: QemuEngine):
        self.name = name
        self.proc = proc
        self.argv = argv
        self.engine = engine

    # -- polling -------------------------------------------------------------
    def poll(self) -> int | None:
        try:
            return self.proc.poll()
        except OSError:
            return None

    def is_running(self) -> bool:
        return self.poll() is None

    def returncode(self) -> int | None:
        return self.poll()

    def last_log_lines(self, n: int = 40) -> list[str]:
        try:
            text = self.engine.qemu_log_path(self.name).read_text(
                encoding="utf-8", errors="replace")
            return text.splitlines()[-n:]
        except OSError:
            return []

    # -- shutdown ------------------------------------------------------------
    def stop(self, timeout: float = 12.0, force: bool = False) -> bool:
        if not self.is_running():
            return True
        if not force:
            try:
                self.monitor_command("quit")
                for _ in range(int(timeout * 10)):
                    if self.poll() is not None:
                        return True
                    time.sleep(0.1)
            except OSError:
                pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=max(1.0, timeout))
            return True
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
                return True
            except OSError:
                return self.poll() is not None

    # -- monitor bridge (telnet) ---------------------------------------------
    @property
    def monitor_port(self) -> int | None:
        for i, tok in enumerate(self.argv):
            if (i > 0 and self.argv[i - 1] == "-monitor"
                    and tok.startswith("telnet:127.0.0.1:")):
                try:
                    return int(tok.split(":")[2].split(",")[0])
                except (ValueError, IndexError):
                    return None
        return None

    def monitor_command(self, command: str, timeout: float = 3.0) -> str:
        """Best-effort monitor command over the telnet port."""
        port = self.monitor_port
        if port is None:
            raise OSError("no monitor port in argv")
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            try:
                s.recv(4096)          # banner / negotiation junk
            except OSError:
                pass
            s.sendall((command + "\n").encode())
            reply = b""
            try:
                reply = s.recv(65536)
            except socket.timeout:
                pass
            return reply.decode(errors="replace")

    def system_powerdown(self) -> None:
        self.monitor_command("system_powerdown")

    def screendump(self, path: str) -> str:
        """Monitor screendump as an alternative to VNC screenshots (PPM)."""
        return self.monitor_command(f"screendump {path}")


# CLI: python -m openstacks.qemu_engine <instance-name> [start|--argv]
if __name__ == "__main__":  # pragma: no cover
    from .config import Store

    store = Store()
    name = sys.argv[1] if len(sys.argv) > 1 else store.default_name()
    action = sys.argv[2] if len(sys.argv) > 2 else "--argv"
    inst = store.get_instance(name)
    eng = QemuEngine()
    if action == "start":
        res = eng.start(inst, store.global_settings)
        print("started" if res.ok else f"launch failed: {res.error}")
        for w in res.warnings:
            print("warn:", w)
        if res.ok:
            try:
                while eng.is_running(inst):
                    time.sleep(1)
            except KeyboardInterrupt:
                eng.stop(name)
    else:
        argv, warn = eng.build_argv(inst, store.global_settings)
        print(subprocess.list2cmdline(argv))
        for w in warn:
            print("warn:", w, file=sys.stderr)
