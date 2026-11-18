"""Stamp a release version into the front-end and the exe's Windows version resource.

    python packaging/set_version.py v0.2.0      (a leading 'v' is dropped; '0.2.0-rc1' ok)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ver = sys.argv[1].lstrip("vV") if len(sys.argv) > 1 else ""
    if not re.fullmatch(r"\d+\.\d+\.\d+([-.+][0-9A-Za-z.-]+)?", ver):
        print(f"bad version {ver!r} (want 1.2.3 or 1.2.3-rc1)", file=sys.stderr)
        return 2
    init = ROOT / "frontend" / "openstacks" / "__init__.py"
    s = init.read_text(encoding="utf-8")
    s, n = re.subn(r'__version__ = "[^"]*"', f'__version__ = "{ver}"', s)
    if n != 1:
        print("__version__ not found", file=sys.stderr)
        return 1
    init.write_text(s, encoding="utf-8")

    nums = [int(x) for x in re.match(r"(\d+)\.(\d+)\.(\d+)", ver).groups()] + [0]
    t = tuple(nums)
    (ROOT / "packaging" / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'OpenStacks'),
      StringStruct('FileDescription', 'OpenStacks'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('ProductVersion', '{ver}'),
      StringStruct('OriginalFilename', 'OpenStacks.exe'),
      StringStruct('LegalCopyright', 'OpenStacks contributors')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
    print(f"version {ver}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
