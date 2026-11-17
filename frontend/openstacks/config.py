"""OpenStacks instance store + global settings.

Mirrors BlueStacks' ``bluestacks.conf`` model of flat
``bst.instance.<Name>.<key>=value`` lines, but keeps each instance in its own
INI *section* so the file stays human-editable:

    [global]
    locale=en-US
    acceleration=tcg
    qemu_path=C:\\...\\qemu_bst\\build\\qemu-system-x86_64.exe
    ...
    default=Pie64

    [Pie64]
    cpus=4
    ram_mb=4096
    resolution=1600x900
    dpi=240
    adb_port=5555
    ...

API surface (used by manager/player/engine):
    Store.load() / Store.save()
    Store.instances() -> list[Instance]
    Store.get_instance(name)
    Store.create(name, ...)
    Store.clone(src_name, new_name)
    Store.delete(name)
    Store.set_default(name)
    Store.global_settings
    Store.bst_conf_text()  -- export as BlueStacks-style bst.instance.<Name>.<key>=value lines

The backend file lives next to the package root: ``<repo>/instances.conf``.
Override via ``OPENSTACKS_CONF`` environment variable or a constructor arg.
"""

from __future__ import annotations

import shutil

import configparser
import os
import re
import secrets
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]      # <repo>/frontend
PROJECT_ROOT = REPO_ROOT.parent                      # <repo>
DEFAULT_IMAGE = "Pie64"                              # BlueStacks engine image (Android 9)

RES_RE = re.compile(r"^(\d{1,5})x(\d{1,5})$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,63}$")

# --- BlueStacks-style devices we can expose; a subset is OK -------------------
KNOWN_DEVICES = (
    "bstvmsg", "bstpgaipc", "bstaudio", "hvmem", "vbox-vmmdev",
)

DEFAULT_QEMU_CANDIDATES = (
    PROJECT_ROOT / "build" / "qemu-system-x86_64.exe",                   # qemu/build.cmd
    PROJECT_ROOT.parent / "qemu_bst" / "build" / "qemu-system-x86_64.exe",  # legacy tree
    Path(r"C:\Program Files\qemu\qemu-system-x86_64.exe"),               # stock fallback
)

# Prefer the user's platform-tools adb: BlueStacks' HD-Adb (v36) and a newer
# adb server kill each other's server on every call, which shows up as
# "offline"/"device not found" flapping. HD-Adb is only the last resort.
DEFAULT_ADB_CANDIDATES = tuple(p for p in (
    shutil.which("adb") and Path(shutil.which("adb")),
    Path(r"C:\Path\platform-tools\adb.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
    REPO_ROOT / "platform-tools" / "adb.exe",
    Path(r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe"),
) if p)


def _first_existing(paths) -> str:
    for p in paths:
        try:
            if p and Path(str(p)).exists():
                return str(p)
        except OSError:
            continue
    return ""


@dataclass
class GlobalSettings:
    """[global] section. Kept intentionally small."""
    locale: str = "en-US"
    fullscreen: bool = False
    priority: str = "normal"           # normal | high
    acceleration: str = "whpx"         # whpx (default: ~15x faster) | tcg
    qemu_path: str = ""
    qemu_bios_dir: str = ""            # -L dir; auto-derived when empty
    assets_dir: str = ""               # kernel.bin / initramfs.bin / Root.vhd ...
    instances_dir: str = ""            # per-instance data disks (default <repo>/instances)
    adb_path: str = ""
    screenshots_dir: str = ""          # default: <repo>/screenshots

    def resolved_qemu_path(self) -> str:
        if self.qemu_path:
            return self.qemu_path
        return _first_existing(DEFAULT_QEMU_CANDIDATES)

    def resolved_adb_path(self) -> str:
        if self.adb_path:
            return self.adb_path
        return _first_existing(DEFAULT_ADB_CANDIDATES)

    def resolved_assets_dir(self, image: str = "") -> str:
        """Asset dir of one engine image (assets/<image>/, made by prepare_assets.py or
        the manager's download). A global assets_dir is either a root holding <image>/
        subdirs or (legacy) one image's files directly."""
        image = image or DEFAULT_IMAGE
        if self.assets_dir:
            base = Path(self.assets_dir)
            base = base if base.is_absolute() else base.resolve()
            return str(base / image if (base / image).is_dir() else base)
        cand = PROJECT_ROOT / "assets" / image
        if cand.exists():
            return str(cand)
        legacy = PROJECT_ROOT.parent / "qemu_test"   # pre-repo workspace
        return str(legacy) if legacy.exists() else str(cand)

    def resolved_instances_dir(self) -> str:
        return self.instances_dir or str(PROJECT_ROOT / "instances")

    def resolved_screenshots_dir(self) -> str:
        return self.screenshots_dir or str(REPO_ROOT / "screenshots")


@dataclass
class Instance:
    """One [section] of instances.conf. Field names == on-disk keys."""
    name: str = ""
    cpus: int = 4
    ram_mb: int = 4096
    resolution: str = "1600x900"
    dpi: int = 240
    adb_port: int = 5555
    vnc_port: int = 5901
    monitor_port: int = 4446
    android_id: str = ""               # BST_ANDROID_ID injected via -append
    serial_number: str = ""
    kernel: str = "kernel.bin"
    initrd: str = "initramfs.bin"      # pristine BlueStacks initramfs (prepare_assets.py)
    root_disk: str = "Root.vhd"        # legacy key, still honored
    root_image: str = ""               # optional override: Root.vhd | Root_patched.vhd
    data_disk: str = ""                # "" = <instances_dir>/<name>/Data.vhdx (auto-created)
    devices: str = "bstvmsg,bstpgaipc,bstaudio,hvmem,vbox-vmmdev"
    pgaipc_gr: bool = True             # bstpgaipc gr=on (the proven grbridge knob)
    hvmem_mb: int = 512                # hvmem ram_size_mib= (proven shape uses 512)
    extra_append: str = ""             # extra KEY=VALUE / kernel args for -append
    display: str = "native"            # native (GPU window embed) | vnc (readback)
    image: str = "Pie64"               # engine image: Pie64 (Android 9), Rvc64 (11), Nougat*
    magisk: bool = False               # rooted with Magisk (player Root button; magisk.py)
    is_default: bool = False

    # -- helpers ------------------------------------------------------------
    def res_wh(self) -> tuple[int, int]:
        m = RES_RE.match(self.resolution or "")
        if not m:
            return (1600, 900)
        return int(m.group(1)), int(m.group(2))

    def device_list(self) -> list[str]:
        return [d.strip() for d in self.devices.split(",") if d.strip()]

    def effective_root_disk(self) -> str:
        """Which VHD the engine passes at IDE index 0.

        ``root_image`` (new per-instance key) wins when set, so instances can
        select ``Root.vhd`` (pristine) vs ``Root_patched.vhd`` (GL-render
        patched guest) without losing the legacy ``root_disk`` key.
        """
        return self.root_image or self.root_disk

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == ok to launch)."""
        errs: list[str] = []
        if not NAME_RE.match(self.name or ""):
            errs.append(f"bad instance name {self.name!r}")
        if not RES_RE.match(self.resolution or ""):
            errs.append(f"bad resolution {self.resolution!r} (want WxH)")
        if self.ram_mb < 256:
            errs.append("ram_mb too small")
        if self.cpus < 1:
            errs.append("cpus too small")
        if self.android_id and not re.match(r"^[0-9a-fA-F]{8,32}$", self.android_id):
            errs.append("android_id should be hex")
        known = set(KNOWN_DEVICES)
        unknown = [d for d in self.device_list() if d not in known]
        if unknown:
            errs.append(f"unknown devices: {', '.join(unknown)}")
        return errs


def _snd(a: str, b: str) -> str:
    """bool-ish parser tolerant of true/false/1/0/yes/no."""
    t = a.strip().lower()
    if t in ("1", "true", "yes", "on"):
        return True
    if t in ("0", "false", "no", "off", ""):
        return False
    return {"true": True}.get(a.strip().lower(), b) if isinstance(b, bool) else b


class Store:
    """instances.conf loader/saver with typed accessors."""

    def __init__(self, path: str | Path | None = None):
        env = os.environ.get("OPENSTACKS_CONF")
        self.path = Path(path or env or (REPO_ROOT / "instances.conf"))
        self.global_settings = GlobalSettings()
        self._instances: dict[str, Instance] = {}
        self._default_name = ""
        self.load()

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"),
                                       strict=False, allow_no_value=False)
        cp.optionxform = str  # keep key case (BlueStacks keys are CamelCase-ish)
        if self.path.exists():
            cp.read(self.path, encoding="utf-8")
        gs = GlobalSettings()
        if cp.has_section("global"):
            for f in fields(gs):
                if cp.has_option("global", f.name):
                    val = cp.get("global", f.name)
                    if f.type == "bool":
                        val = _snd(val, getattr(gs, f.name))
                    elif f.type in ("int",):
                        val = int(val)
                    setattr(gs, f.name, val)
        self.global_settings = gs
        inst: dict[str, Instance] = {}
        default_name = ""
        if cp.has_option("global", "default"):
            default_name = cp.get("global", "default").strip()
        for sect in cp.sections():
            if sect == "global":
                continue
            it = Instance(name=sect)
            for f in fields(it):
                if f.name == "name" or not cp.has_option(sect, f.name):
                    continue
                val = cp.get(sect, f.name)
                if f.type == "bool":
                    val = _snd(val, getattr(it, f.name))
                elif f.type == "int":
                    try:
                        val = int(val)
                    except ValueError:
                        continue
                setattr(it, f.name, val)
            inst[sect] = it
        self._instances = inst
        if default_name in inst:
            self._default_name = default_name
        elif inst:
            self._default_name = sorted(inst)[0]
        else:
            self._default_name = ""

    def save(self) -> None:
        cp = configparser.ConfigParser()
        cp.optionxform = str
        g = self.global_settings
        cp.add_section("global")
        gfields = {f.name: getattr(g, f.name) for f in fields(g)}
        gfields["default"] = self._default_name
        for k, v in gfields.items():
            if v in ("", None) and k != "default":
                continue
            cp.set("global", k, str(v) if not isinstance(v, bool) else ("true" if v else "false"))
        for name, it in sorted(self._instances.items()):
            cp.add_section(name)
            for f in fields(it):
                if f.name == "name":
                    continue
                v = getattr(it, f.name)
                if f.type == "bool":
                    v = "true" if v else "false"
                cp.set(name, f.name, str(v))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("; OpenStacks instances.conf - INI: [global] + one section per instance.\n")
            fh.write("; The API speaks BlueStacks-style bst.instance.<Name>.<key> (see Store.bst_conf_text()).\n\n")
            cp.write(fh, space_around_delimiters=False)

    # -- queries -------------------------------------------------------------
    def instances(self) -> list[Instance]:
        return list(self._instances.values())

    def names(self) -> list[str]:
        return sorted(self._instances)

    def has_instance(self, name: str) -> bool:
        return name in self._instances

    def get_instance(self, name: str) -> Instance:
        try:
            return self._instances[name]
        except KeyError as e:
            raise KeyError(f"no instance named {name!r}") from e

    def default_name(self) -> str:
        return self._default_name

    def set_default(self, name: str) -> None:
        if name not in self._instances:
            raise KeyError(f"no instance named {name!r}")
        self._default_name = name
        self.save()

    # -- mutations -----------------------------------------------------------
    def create(self, name: str, *, cpus: int = 4, ram_mb: int = 4096,
               dpi: int = 240, resolution: str = "1600x900",
               adb_port: int | None = None, devices: str | None = None,
               android_id: str | None = None, **extra) -> Instance:
        """Fresh instance; ports auto-assigned when omitted; name must be unique."""
        if not NAME_RE.match(name or ""):
            raise ValueError(f"invalid instance name {name!r}")
        if name in self._instances:
            raise ValueError(f"instance {name!r} already exists")
        it = Instance(
            name=name, cpus=int(cpus), ram_mb=int(ram_mb), dpi=int(dpi),
            resolution=resolution,
            adb_port=adb_port if adb_port else self._next_port(5555, "adb_port"),
            vnc_port=self._next_port(5901, "vnc_port"),
            monitor_port=self._next_port(4446, "monitor_port"),
            devices=devices if devices else ",".join(KNOWN_DEVICES),
            android_id=android_id if android_id else secrets.token_hex(8),
        )
        for k, v in extra.items():
            if hasattr(it, k):
                setattr(it, k, v)
        self._instances[name] = it
        if not self._default_name:
            self._default_name = name
        self.save()
        return it

    def clone(self, src_name: str, new_name: str, *, copy_data_disk: bool = False) -> Instance:
        """Config clone + fresh ports (+optional data-disk copy)."""
        src = self.get_instance(src_name)
        dst = replace(src, name=new_name)
        dst.adb_port = self._next_port(5555, "adb_port")
        dst.vnc_port = self._next_port(5901, "vnc_port")
        dst.monitor_port = self._next_port(4446, "monitor_port")
        dst.android_id = secrets.token_hex(8)
        dst.serial_number = secrets.token_hex(6)
        if copy_data_disk:
            from .qemu_engine import resolve_data_disk
            src_data = resolve_data_disk(src, self.global_settings, create=False)
            if src_data and src_data.exists():
                import shutil
                new_disk = (Path(self.global_settings.resolved_instances_dir())
                            / new_name / ("Data" + src_data.suffix))
                new_disk.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_data, new_disk)
                dst.data_disk = str(new_disk)
        self._instances[new_name] = dst
        self.save()
        return dst

    def delete(self, name: str) -> None:
        if name not in self._instances:
            raise KeyError(f"no instance named {name!r}")
        del self._instances[name]
        if self._default_name == name:
            self._default_name = next(iter(sorted(self._instances)), "")
        self.save()

    def update(self, inst: Instance) -> None:
        """Replace an instance config and persist."""
        if inst.name not in self._instances:
            raise KeyError(f"no instance named {inst.name!r}")
        self._instances[inst.name] = inst
        self.save()

    # -- helpers -------------------------------------------------------------
    def _next_port(self, base: int, key: str) -> int:
        used = {getattr(i, key) for i in self._instances.values()}
        p = base
        while p in used:
            p += 1
        return p

    def bst_conf_text(self) -> str:
        """Export as BlueStacks-style flat key=value (bst.instance.<Name>.<key>)."""
        out = ["; BlueStacks-compatible view of instances.conf"]
        for name, it in sorted(self._instances.items()):
            for f in fields(it):
                if f.name == "name":
                    continue
                v = getattr(it, f.name)
                out.append(f"bst.instance.{name}.{f.name}={v if not isinstance(v, bool) else ('true' if v else 'false')}")
        for f in fields(self.global_settings):
            out.append(f"bst.{f.name}={getattr(self.global_settings, f.name)}")
        return "\n".join(out) + "\n"
