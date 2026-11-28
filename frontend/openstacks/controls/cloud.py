"""BlueStacks' built-in game controls, downloaded the way HD-Player does it.

HD-Player (`cldGetPackageCfgAsync`) asks `<bst.bluestacks_cloud_url>/get_cfg_info` with
`package_name`, `parser_version` (and country, version code, client ids, which we do not
send). The reply is `{"cfg_file_url": {"normal": "https://eb.bluestacks.com/content_keymap/
<package>.cfg?parserver=17..."}, "success": true}`; that file is a ParserVersion 17 `.cfg`
or a 404 when BlueStacks has none for the package. There is no bulk listing, so files are
fetched one game at a time, only when that game is opened and no local file exists.

Everything downloaded is kept in `<controls dir>/bluestacks/` with an index of when each
package was last asked about, misses included, so a package is asked about at most once
per RECHECK (a miss: MISS_RECHECK).
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .. import paths
from .bscfg import PARSER_VERSION

INFO_URL = "https://cloud.bluestacks.com/get_cfg_info"
FILE_URL = "https://eb.bluestacks.com/content_keymap/{pkg}.cfg?parserver={pv}"
RECHECK = 7 * 86400          # a file we have: look for an update weekly
MISS_RECHECK = 3 * 86400     # BlueStacks had none: ask again after three days
ERROR_RECHECK = 3600         # offline or a server error: retry after an hour
TIMEOUT = 20
MAX_SIZE = 8 << 20

_PKG = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")
_UA = {"User-Agent": "OpenStacks"}


def cache_dir() -> Path:
    return paths.CONTROLS_DIR / "bluestacks"


def _index_path() -> Path:
    return cache_dir() / "index.json"


def _index() -> dict:
    try:
        return json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(idx: dict) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "index.json.tmp"
    tmp.write_text(json.dumps(idx, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, _index_path())


# system apps and launchers never have game controls; they are never sent
_SYSTEM = ("android", "com.android.", "com.google.android.", "com.bluestacks.",
           "com.uncube.", "com.now.gg.", "org.lineageos.", "com.topjohnwu.magisk")


def valid_package(pkg: str) -> bool:
    """A package name worth asking BlueStacks about (well formed, not a system app)."""
    return (bool(pkg) and len(pkg) < 256 and bool(_PKG.match(pkg))
            and not (pkg == "android" or pkg.startswith(_SYSTEM[1:])))


def cached(pkg: str) -> Optional[Path]:
    """The downloaded file for a package, if any."""
    if not valid_package(pkg):
        return None
    p = cache_dir() / f"{pkg}.cfg"
    return p if p.is_file() else None


def due(pkg: str, now: Optional[float] = None) -> bool:
    """Whether to ask BlueStacks about this package now."""
    if not valid_package(pkg):
        return False
    e = _index().get(pkg)
    if not e:
        return True
    age = (now or time.time()) - float(e.get("checked", 0))
    wait = {"found": RECHECK, "none": MISS_RECHECK}.get(e.get("result"), ERROR_RECHECK)
    return age >= wait


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        raise ValueError("response too large")
    return data


def _file_url(pkg: str) -> str:
    """Ask get_cfg_info where the file is; fall back to the known location."""
    q = urllib.parse.urlencode({"package_name": pkg, "parser_version": PARSER_VERSION})
    try:
        info = json.loads(_get(f"{INFO_URL}?{q}"))
        url = ((info.get("cfg_file_url") or {}).get("normal") or "")
        u = urllib.parse.urlsplit(url)
        if (info.get("success") and u.scheme == "https"
                and (u.hostname or "").endswith(".bluestacks.com")):
            return url
    except (OSError, ValueError, AttributeError):
        pass
    return FILE_URL.format(pkg=pkg, pv=PARSER_VERSION)


def _stamp(raw: dict) -> str:
    return str((raw.get("MetaData") or {}).get("CloudUpdateTimeUTC", ""))


def fetch(pkg: str) -> str:
    """Download (or refresh) one package's file. Returns 'new', 'updated', 'same', 'none'
    (BlueStacks has none) or 'error'. Blocking: run it off the GUI thread."""
    if not valid_package(pkg):
        return "error"
    result = "error"
    try:
        data = _get(_file_url(pkg))
        raw = json.loads(data.decode("utf-8-sig"))
        if not isinstance(raw, dict) or not isinstance(raw.get("ControlSchemes"), list):
            raise ValueError("not a controls file")
        dst = cache_dir() / f"{pkg}.cfg"
        old = None
        if dst.is_file():
            try:
                old = json.loads(dst.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                old = None
        if old is not None and _stamp(old) == _stamp(raw) and old == raw:
            result = "same"
        else:
            cache_dir().mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".cfg.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dst)
            result = "updated" if old is not None else "new"
    except urllib.error.HTTPError as e:
        result = "none" if e.code == 404 else "error"
    except (OSError, ValueError, UnicodeDecodeError):
        result = "error"
    idx = _index()
    prev = idx.get(pkg) or {}
    entry = {"checked": int(time.time()),
             "result": "found" if result in ("new", "updated", "same") else result}
    if result == "error" and prev.get("result") == "found" and cached(pkg):
        # keep the file and retry in ERROR_RECHECK (backdate the found check to get that)
        entry = {"checked": int(time.time()) - RECHECK + ERROR_RECHECK, "result": "found"}
    try:
        _save_index({**idx, pkg: entry})
    except OSError:
        pass
    return result
