"""One-click Magisk root for an instance (Kitsune Magisk, "system mode" from the ramdisk).

The image patch patches/all/20_magisk.py makes every boot ramdisk start Magisk when the
instance's /data has /data/adb/.ost_magisk + Magisk's bin dir /data/adb/magisk. install()
fills those over adb (using BlueStacks' own su once), installs the Magisk app, and the
caller restarts the instance. After that boot, finish() pre-grants root to `adb shell`
(uid 2000) so tools keep working, and the instance's BlueStacks su is turned off (props) so
apps ask Magisk instead of getting silent root.

The APK comes from AndnixSH's KitsuneMagisk mirror (the original 1q23lyc45 repo has no
releases any more), pinned by SHA-256 and cached in deps/magisk/. Qt-free.
"""
from __future__ import annotations

import hashlib
import posixpath
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

from .adb import Adb, AdbError

CACHE = Path(__file__).resolve().parents[2] / "deps" / "magisk"   # dev/deps/magisk
FLAG = "/data/adb/.ost_magisk"
BIN = "/data/adb/magisk"
STAGE = "/data/local/tmp/ost_magisk"
# BlueStacks' su (bstk/su stays reachable for adb shell even when xbin/su is hidden)
BST_SUS = ("/system/xbin/bstk/su", "/system/xbin/su")
SHELL_UID = 2000
SERVICE_D = "/data/adb/service.d/ost_adb_root.sh"

Log = Callable[[str], None]


# -- the APK -----------------------------------------------------------------------------
# Kitsune Magisk v27.2-kitsune-4 (release build), AndnixSH mirror, tag "backup". The hash
# is GitHub's asset digest; tested on all five images (NOTES.md 10t).
APK_URL = ("https://github.com/AndnixSH/KitsuneMagisk/releases/download/backup/"
           "Kitsune.Magisk.release.v27.2-kitsune-4.apk")
APK_SHA256 = "818cfa02783ddae573cc953450fbc39ec3e5164b66e517c657ba11cf90963a89"
APK_VERSION = "v27.2-kitsune-4"


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cached_apk() -> Optional[Path]:
    """The pinned APK if it is already in deps/magisk (any file name)."""
    for p in sorted(CACHE.glob("*.apk")):
        if _sha256(p) == APK_SHA256:
            return p
    return None


def ensure_apk(log: Log = print) -> Path:
    """The pinned Kitsune APK: from the cache, else downloaded (and verified)."""
    p = cached_apk()
    if p is not None:
        return p
    dest = CACHE / Path(APK_URL).name
    log(f"downloading {dest.name}")
    CACHE.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(APK_URL, headers={"User-Agent": "OpenStacks"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != APK_SHA256:
        raise ValueError(f"{dest.name}: sha256 {got} != pinned {APK_SHA256}")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


def _bin_files(apk: Path, is64: bool) -> dict[str, bytes]:
    """Magisk's /data/adb/magisk layout (what the app's 'additional setup' would write)."""
    abi = "x86_64" if is64 else "x86"
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(apk) as z:
        lib = lambda a, n: z.read(f"lib/{a}/lib{n}.so")
        out["magisk32"] = lib("x86", "magisk32")
        if is64:
            out["magisk64"] = lib("x86_64", "magisk64")
        for n in ("magiskinit", "magiskpolicy", "magiskboot", "busybox"):
            out[n] = lib(abi, n)
        for n in z.namelist():
            if n.startswith("assets/") and (n.endswith(".sh") or n == "assets/stub.apk"
                                            or n.startswith("assets/chromeos/")):
                out[n[len("assets/"):]] = z.read(n)
    return out


# -- guest side ------------------------------------------------------------------------
def root_sh(adb: Adb, script: str, timeout: float = 120.0) -> str:
    """Run a root shell script: BlueStacks' su first, then whatever su is in PATH
    (Magisk's, which grants adb shell once finish() ran)."""
    last = ""
    for su in BST_SUS + ("su",):
        try:
            out = adb.shell(su, "-c", f"id -u; {script}", timeout=timeout)
        except AdbError as e:
            last = str(e)
            continue
        lines = out.strip().splitlines()
        if lines and lines[0].strip() == "0":
            return "\n".join(lines[1:])
        last = out.strip()
    raise AdbError(f"no root shell available ({last[:200]})")


def status(adb: Adb) -> str:
    """Magisk version string if the daemon is running, else ''."""
    try:
        out = adb.shell("sh", "-c", "magisk -v 2>/dev/null; true", timeout=15).strip()
    except AdbError:
        return ""
    return out if ":" in out or out[:1].isdigit() else ""


def install(adb: Adb, apk: Path, log: Log = print) -> None:
    """Stage Magisk into /data/adb/magisk, set the flag, install the app. The instance
    must be restarted afterwards (the ramdisk starts Magisk at boot)."""
    adb.ensure_connected()
    abilist = adb.shell("getprop", "ro.product.cpu.abilist").strip()
    is64 = "x86_64" in abilist
    log(f"guest ABIs: {abilist or '?'}; using {'x86_64 + x86' if is64 else 'x86'} binaries")
    files = _bin_files(apk, is64)
    with tempfile.TemporaryDirectory(prefix="ost_magisk_") as td:
        d = Path(td) / "ost_magisk"
        for name, data in files.items():
            p = d / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        log(f"pushing {len(files)} files")
        adb.shell("rm", "-rf", STAGE)
        adb.push(d, posixpath.dirname(STAGE) + "/")
    log("installing into /data/adb/magisk (root)")
    # adb shell keeps root: a service.d script re-grants uid 2000 on every boot. It has to
    # be written now, while BlueStacks' su still works (rooted instances turn it off), and
    # per boot because Magisk's db read fails early in boot here ("sqlite3_exec: disk I/O
    # error"): the shell's first su request then times out and DENY gets stored.
    grant = ("magisk --sqlite \"REPLACE INTO policies (uid,policy,until,logging,"
             f"notification) VALUES({SHELL_UID},2,0,1,1)\"")
    script = f"#!/system/bin/sh\n# OpenStacks: keep adb shell root (magisk.py)\n{grant}\n"
    root_sh(adb, f"rm -rf {BIN} && mkdir -p {BIN} && cp -r {STAGE}/* {BIN}/ && "
                 f"mkdir -p /data/adb/service.d && cat > {SERVICE_D} <<'EOF'\n{script}EOF\n"
                 f"chown -R 0:0 /data/adb && chmod 700 /data/adb && chmod -R 755 {BIN} && "
                 f"chmod 755 /data/adb/service.d {SERVICE_D} && "
                 f"touch {FLAG} && rm -rf {STAGE} && ls {BIN}")
    log("installing the Magisk app")
    out = adb.install(str(apk), timeout=600)
    if "Success" not in out:
        raise AdbError(f"Magisk app install: {out}")


def finish(adb: Adb, log: Log = print) -> str:
    """After the first rooted boot: check the daemon, grant root to adb shell."""
    ver = status(adb)
    if not ver:
        raise AdbError("Magisk is not running after the restart "
                       "(was the image re-patched? prepare_assets.py --repatch all)")
    log(f"Magisk {ver} running; checking adb shell root")
    out = adb.shell("su", "-c", "id -u", timeout=30).strip()
    if out != "0":
        raise AdbError(f"Magisk {ver} runs, but adb shell su was refused ({out[:80]}); "
                       "allow it in the Magisk app (Superuser)")
    return ver


def uninstall(adb: Adb, log: Log = print) -> None:
    """Remove the flag + bin dir and the app; takes effect on the next restart."""
    log("removing Magisk from /data/adb")
    root_sh(adb, f"rm -rf {FLAG} {BIN} /data/adb/magisk.db /data/adb/modules {SERVICE_D}")
    pkgs = adb.shell("pm", "list", "packages").split()
    for p in (x.split(":", 1)[1] for x in pkgs if x.startswith("package:")):
        if p in ("io.github.huskydg.magisk", "com.topjohnwu.magisk") or "kitsune" in p:
            log(f"uninstalling {p}")
            adb.shell("pm", "uninstall", p)
