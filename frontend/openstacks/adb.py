"""Thin ADB wrapper for an OpenStacks guest (adb over hostfwd 127.0.0.1:<port>).

The adb binary is auto-discovered from (in order): the explicit path (config
global ``adb_path`` / constructor arg), then config.DEFAULT_ADB_CANDIDATES (adb
on PATH, platform-tools dirs, BlueStacks' HD-Adb.exe last).

Everything is blocking subprocess calls - call from worker threads (the
InputWorker / UI helpers already do). Qt-free on purpose.
"""

from __future__ import annotations

import enum
import shlex
import shutil
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# single source: config.DEFAULT_ADB_CANDIDATES (PATH / platform-tools first,
# BlueStacks' HD-Adb last - the two kill each other's server, NOTES 10h)
from .config import DEFAULT_ADB_CANDIDATES  # noqa: E402


class AdbError(RuntimeError):
    pass


class BootState(enum.Enum):
    """Per-instance control-plane state, as seen over adb (+ host process)."""
    STOPPED = "stopped"            # QEMU not running (caller decides - see snapshot_from)
    STARTING = "starting"          # QEMU running, adbd still not reachable
    OFFLINE = "offline"            # adbd visible but reports 'offline'
    UNAUTHORIZED = "unauthorized"  # adbd up, RSA dialog not accepted
    ADB_ONLINE = "adb-online"      # adb device online, sys.boot_completed != 1
    READY = "ready"                # adb online AND sys.boot_completed == 1

    def label(self) -> str:
        return self.value


def find_adb(explicit: str | None = None) -> str | None:
    """Locate a usable adb binary; returns path or None (callers raise)."""
    if explicit:
        if Path(explicit).exists():
            return explicit
    for cand in DEFAULT_ADB_CANDIDATES:
        if cand and Path(cand).exists():
            return cand
    for name in ("adb",):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Android KEYCODE numbers (for `adb shell input keyevent <code>`), keyed by
# X11 keysym so the same map serves both the VNC path and the adb path.
# ---------------------------------------------------------------------------
# letters a..z -> KEYCODE_A(29)..KEYCODE_Z(54); digits 0..9 -> KEYCODE_0(7)..
def _gen_alnum() -> dict[int, int]:
    m: dict[int, int] = {}
    for c in range(ord("a"), ord("z") + 1):
        m[c] = 29 + (c - ord("a"))
        m[ord(chr(c).upper())] = 29 + (c - ord("a"))
    for c in range(ord("0"), ord("9") + 1):
        m[c] = 7 + (c - ord("0"))
    m[0x0020] = 62   # SPACE
    return m


_KEYSYM_KEYCODE = {
    0xFF08: 67,   # BackSpace -> BACKSPACE
    0xFF09: 61,   # Tab -> TAB
    0xFF0D: 66,   # Return -> ENTER
    0xFF8D: 66,   # KP Enter -> ENTER
    0xFF1B: 4,    # Escape -> BACK
    0xFF1D: 187,  # Menu -> APP_SWITCH (quasi)
    0xFF50: 3,    # Home -> HOME
    0xFF52: 19,   # Up -> DPAD_UP
    0xFF53: 22,   # Right -> DPAD_RIGHT
    0xFF54: 20,   # Down -> DPAD_DOWN
    0xFF51: 21,   # Left -> DPAD_LEFT
    0xFF55: 92,   # PageUp -> PAGE_UP
    0xFF56: 93,   # PageDown -> PAGE_DOWN
    0xFF63: 124,  # Insert -> INSERT
    0xFF60: 111,  # F0-ish escape -> FORWARD
    0xFFFF: 112,  # Delete -> DEL
    0xFF67: 82,   # Menu keysym -> MENU
    0x1008FF2A: 24,  # XF86 raise volume-ish -> VOLUME_UP
    0x1008FF11: 25,  # XF86 lower volume-ish -> VOLUME_DOWN
    0xFFE1: 59, 0xFFE2: 60,    # Shift L/R -> SHIFT_L/R
    0xFFE3: 113, 0xFFE4: 114,  # Ctrl L/R -> CTRL_L/R
    0xFFE9: 57, 0xFFEA: 58,    # Alt L/R -> ALT_L/R
    0xFFEB: 223, 0xFFEC: 227,  # Meta L/R -> META (search/meta)
}
for i in range(12):                       # F1..F12 -> KEYCODE_F1(131)..F12(142)
    _KEYSYM_KEYCODE[0xFFBE + i] = 131 + i
_KEYSYM_KEYCODE.update(_gen_alnum())


def keysym_to_keycode(keysym: int) -> int | None:
    return _KEYSYM_KEYCODE.get(keysym)


class Adb:
    """Blocking client around `adb -s 127.0.0.1:<port> ...`."""

    def __init__(self, port: int = 5555, adb_path: str | None = None):
        self.port = int(port)
        self.serial = f"127.0.0.1:{self.port}"
        self.path = adb_path or find_adb()
        self._connected = False

    # -- plumbing ------------------------------------------------------------
    def _require(self) -> str:
        if not self.path:
            raise AdbError(
                "adb.exe not found; set global 'adb_path' in instances.conf "
                "(e.g. BlueStacks' C:\\Program Files\\BlueStacks_nxt\\HD-Adb.exe) "
                "or drop platform-tools\\adb.exe next to the repo."
            )
        return self.path

    def _run(self, *args: str, timeout: float = 30.0, device: bool = True) -> str:
        exe = self._require()
        cmd = [exe] + (["-s", self.serial] if device else []) + list(args)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  errors="replace", timeout=timeout, encoding="utf-8")
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb timed out: {' '.join(args)}") from e
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode != 0:
            raise AdbError(f"adb {' '.join(args)} failed rc={proc.returncode}: {out.strip()}")
        return out

    # -- lifecycle -----------------------------------------------------------
    def connect(self) -> str:
        out = self._run("connect", self.serial, device=False, timeout=15)
        if "connected to" in out and "cannot" not in out.lower():
            self._connected = True
        return out.strip()

    def ensure_connected(self) -> None:
        if self._connected:
            return
        out = self.connect()
        self._connected = True  # optimistically; verify() below re-checks
        try:
            self._run("get-state", timeout=15)
        except AdbError:
            self._connected = False
            raise AdbError(f"adb connect {self.serial} not usable: {out}")

    def disconnect(self) -> str:
        self._connected = False
        return self._run("disconnect", self.serial, device=False, timeout=15).strip()

    # -- status --------------------------------------------------------------
    def state(self, autostart: bool = True) -> str:
        try:
            return self._run("get-state", timeout=15).strip()
        except AdbError:
            if autostart:
                self.connect()
                return self._run("get-state", timeout=15).strip()
            raise

    def devices(self) -> str:
        return self._run("devices", device=False, timeout=15)

    def device_status(self) -> str:
        """adb-devices state token for THIS serial.

        Returns ''device'' / ''offline'' / ''unauthorized'' (as printed), or ''
        when the serial is not listed (not connected yet / adbd down).
        Never raises for a missing server: `adb devices` auto-starts it.
        """
        try:
            out = self._run("devices", device=False, timeout=15)
        except AdbError:
            return ""
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self.serial:
                tok = parts[1]
                return tok if tok != "device" else "device"
        return ""

    # -- boot-state model (drives the player's control status bar) ------------
    def snapshot(self, is_running: bool | None = None) -> tuple[BootState, str]:
        """One-shot (BootState, human detail). Never raises - that's the point."""
        if not self.path:
            return (BootState.STOPPED,
                    "adb.exe not found - set adb_path in instances.conf")
        detail = ""
        status = self.device_status()
        if not status:
            # Not listed yet: maybe we simply never connected. Try once, then
            # re-read (this is the cheap probe the watchdog loops on).
            try:
                self.connect()
                status = self.device_status()
            except AdbError as e:
                detail = f"connect failed: {e}"
        if status == "device":
            done = self.boot_completed()
            if done:
                return (BootState.READY, "sys.boot_completed=1")
            if done is False:
                return (BootState.ADB_ONLINE, "waiting for sys.boot_completed")
            return (BootState.OFFLINE, "boot_completed probe failed (adbd dropping)")
        if status == "unauthorized":
            return (BootState.UNAUTHORIZED,
                    "adbd up but unauthorized - accept the RSA prompt on the guest")
        if status == "offline":
            return (BootState.OFFLINE, "adbd offline (guest init still coming up)")
        if not status:
            if is_running:                  # caller said QEMU is up
                return (BootState.STARTING,
                        detail or "adbd not reachable yet (guest booting)")
            if detail:
                return (BootState.STOPPED, detail)
            return (BootState.STOPPED, "QEMU not running; adbd absent")
        return (BootState.STARTING, f"unexpected adb status {status!r}")

    def boot_completed(self) -> bool | None:
        """`getprop sys.boot_completed` -> True / False / None (probe failed)."""
        try:
            out = self._run("shell", "getprop", "sys.boot_completed",
                            timeout=15).strip()
        except AdbError:
            return None
        if not out:
            return False
        return out.splitlines()[-1].strip() == "1"

    def connect_with_retry(self, timeout: float = 120.0, poll: float = 2.0,
                           on_tick=None) -> str:
        """adb connect + status loop until the device presents a state.

        Covers both failure shapes: the adb *server* being offline and the
        guest's adbd not up yet (server reboot every ~2 s per spec). Returns
        the device state token; raises AdbError when the deadline passes.
        """
        deadline = time.monotonic() + max(1.0, timeout)
        self._require()          # adb.exe missing: fail now, no point retrying
        last = ""
        n = 0
        while True:
            try:
                last = self.connect()
            except AdbError as e:
                last = str(e)
            status = self.device_status()
            if status in ("device", "offline", "unauthorized"):
                self._connected = (status == "device")
                return status
            if time.monotonic() >= deadline:
                raise AdbError(
                    f"adb {self.serial} not reachable after {timeout:.0f}s "
                    f"({n} tries): {last or 'no device listed'}")
            if on_tick is not None:
                try:
                    on_tick(n, last)
                except Exception:                      # never let UI hooks kill us
                    pass
            n += 1
            time.sleep(max(0.2, poll))

    def wait_boot(self, timeout: float = 600.0, poll: float = 2.0,
                  on_tick=None) -> bool:
        """After connect_with_retry: poll getprop sys.boot_completed.

        Returns True when boot completes; False when the wait times out.
        Reconnects if adbd drops mid-boot. Raises only AdbError on a hard
        "deadline vs absent adb" condition.
        """
        deadline = time.monotonic() + max(1.0, timeout)
        self.connect_with_retry(timeout=min(timeout, 120.0), poll=poll,
                                on_tick=on_tick)
        n = 0
        while True:
            state = self.device_status()
            if state in ("offline", "unauthorized"):
                time.sleep(poll)
            else:
                done = self.boot_completed()
                if done:
                    return True
                if done is None and state != "device":
                    # adbd dropped mid-boot: re-established by the next loop
                    try:
                        self.connect()
                    except AdbError:
                        pass
            if on_tick is not None:
                try:
                    on_tick(n, None)
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                return False
            n += 1
            time.sleep(poll)

    # -- shell ---------------------------------------------------------------
    # BlueStacks' adbd gates every service on bst.enable_adb_access; bstvmsg
    # now serves it =1 (dev/NOTES.md 10h), so no allow-listed first word is
    # needed any more. Kept as a hook for images without that property.
    SHELL_PREFIX = ""

    def shell(self, *cmd: str, timeout: float = 60.0) -> str:
        self.ensure_connected()
        line = " ".join(shlex.quote(c) for c in cmd)
        return self._run("shell", self.SHELL_PREFIX + line, timeout=timeout)

    def install(self, apk, timeout: float = 600.0) -> str:
        """adb install -r <apk>, or install-multiple -r for a list of split
        APKs (base + splits); returns adb output ('Success' on ok).
        Note: BlueStacks refuses com.android.chrome / com.android.vending /
        com.google.android.gms until a Google account is signed in."""
        self.ensure_connected()
        if isinstance(apk, (list, tuple)):
            if len(apk) > 1:
                return self._run("install-multiple", "-r", *map(str, apk),
                                 timeout=timeout).strip()
            apk = apk[0]
        return self._run("install", "-r", str(apk), timeout=timeout).strip()

    def screencap(self, host_path: str, timeout: float = 60.0) -> str:
        """Screencap to guest then pull into host_path (PNG bytes, adb converts)."""
        self.ensure_connected()
        remote = "/sdcard/openstacks_shot.png"
        self._run("shell", self.SHELL_PREFIX + "screencap -p " + remote, timeout=timeout)
        try:
            self._run("pull", remote, str(host_path), timeout=timeout)
        finally:
            self._run("shell", self.SHELL_PREFIX + "rm -f " + remote, timeout=15)
        return str(host_path)

    def screencap_bytes(self, timeout: float = 60.0) -> bytes:
        """PNG bytes of the current screen (pulled into memory)."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ostshot_") as tmp:
            p = Path(tmp) / "shot.png"
            self.screencap(str(p), timeout=timeout)
            return p.read_bytes()

    # -- input ---------------------------------------------------------------
    def tap(self, x: int, y: int, timeout: float = 15.0) -> None:
        self.shell("input", "tap", str(int(x)), str(int(y)), timeout=timeout)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300,
              timeout: float = 15.0) -> None:
        self.shell("input", "swipe", str(int(x1)), str(int(y1)),
                   str(int(x2)), str(int(y2)), str(int(ms)), timeout=timeout)

    def keyevent(self, keycode: int, timeout: float = 15.0) -> None:
        self.shell("input", "keyevent", str(int(keycode)), timeout=timeout)

    def keysym(self, keysym: int, timeout: float = 15.0) -> None:
        """Send an X11 keysym as a keyevent (used by the InputWorker/adb bridge)."""
        code = keysym_to_keycode(keysym)
        if code is not None:
            self.keyevent(code, timeout=timeout)
        else:
            self.text(chr(keysym & 0xFF) if 0x20 <= (keysym & 0xFF) < 0x7F else "",
                      timeout=timeout)

    def text(self, s: str, timeout: float = 15.0) -> None:
        s = s.strip()
        if not s:
            return
        safe = s.replace(" ", "%s").replace("&", "\\&").replace("<", "\\<")
        try:
            self.shell("input", "text", safe, timeout=timeout)
        except AdbError:
            # 'input text' chokes on exotic chars; retry shell-escaped
            self.shell("input", "text", shlex.quote(s), timeout=timeout)
