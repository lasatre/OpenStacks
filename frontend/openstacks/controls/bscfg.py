"""BlueStacks game-controls files (`<package>.cfg`, ParserVersion 17): load, save, schemes.

The JSON is kept as plain dicts and lists, so fields we do not interpret (Guidance,
Strings, Images, Tweaks, ...) survive a load and save unchanged. BlueStacks writes it
as Newtonsoft does: 2-space indent, CRLF, no BOM (it reads a BOM fine).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PARSER_VERSION = "17"

# fields every control has (BlueStacks' base class), with its defaults
BASE_FIELDS: dict[str, Any] = {
    "Tweaks": 0, "Exclusive": False, "ExclusiveDelay": 200, "XExpr": "", "YExpr": "",
    "XOverlayOffset": "", "YOverlayOffset": "", "StartCondition": "", "EnableCondition": "",
    "ShowOnOverlay": True,
}


@dataclass
class Scheme:
    raw: dict

    @property
    def name(self) -> str:
        return self.raw.get("Name", "")

    @property
    def selected(self) -> bool:
        return bool(self.raw.get("Selected"))

    @property
    def builtin(self) -> bool:
        return bool(self.raw.get("BuiltIn"))

    @property
    def controls(self) -> list[dict]:
        return self.raw.setdefault("GameControls", [])

    @property
    def images(self) -> list[dict]:
        return self.raw.get("Images") or []

    @property
    def smart(self) -> bool:
        """Uses on-screen detection (image ids in its conditions)."""
        return bool(self.images)


@dataclass
class ControlsFile:
    raw: dict
    path: Optional[Path] = None
    schemes: list[Scheme] = field(default_factory=list)

    @classmethod
    def new(cls, package: str = "") -> "ControlsFile":
        raw = {"MetaData": {"ParserVersion": PARSER_VERSION, "UpdateVersion": "",
                            "UpdateArticleKey": "", "CloudUpdateTimeUTC": ""},
               "ControlSchemes": [{"Name": "Default", "BuiltIn": False, "Selected": True,
                                   "IsBookMarked": False, "IsCategoryVisible": True,
                                   "KeyboardLayout": "", "GameControls": [], "Images": []}],
               "Strings": {"User-Defined": {}}}
        return cls.from_raw(raw)

    @classmethod
    def from_raw(cls, raw: dict, path: Optional[Path] = None) -> "ControlsFile":
        if not isinstance(raw, dict) or not isinstance(raw.get("ControlSchemes"), list):
            raise ValueError("not a BlueStacks controls file (no ControlSchemes)")
        return cls(raw=raw, path=path,
                   schemes=[Scheme(s) for s in raw["ControlSchemes"] if isinstance(s, dict)])

    @classmethod
    def load(cls, path: Path) -> "ControlsFile":
        text = Path(path).read_text(encoding="utf-8-sig")
        return cls.from_raw(json.loads(text), Path(path))

    def save(self, path: Optional[Path] = None) -> Path:
        path = Path(path or self.path)
        self.raw["ControlSchemes"] = [s.raw for s in self.schemes]
        text = json.dumps(self.raw, indent=2, ensure_ascii=False).replace("\n", "\r\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(text.encode("utf-8"))
        tmp.replace(path)
        self.path = path
        return path

    @property
    def parser_version(self) -> str:
        return str((self.raw.get("MetaData") or {}).get("ParserVersion", ""))

    def selected(self) -> Optional[Scheme]:
        for s in self.schemes:
            if s.selected:
                return s
        return self.schemes[0] if self.schemes else None

    def select(self, name: str) -> Optional[Scheme]:
        hit = None
        for s in self.schemes:
            s.raw["Selected"] = s.name == name
            if s.name == name:
                hit = s
        return hit

    def scheme(self, name: str) -> Optional[Scheme]:
        return next((s for s in self.schemes if s.name == name), None)


def control_type(c: dict) -> str:
    t = c.get("Type") or str(c.get("$type", "")).split(",")[0]
    return t.strip()


def new_control(type_name: str, **fields) -> dict:
    """A control dict as BlueStacks writes it ("$type" first, then Type, base fields)."""
    c = {"$type": f"{type_name}, Bluestacks", "Type": type_name, **BASE_FIELDS}
    c.update(fields)
    c.setdefault("GuidanceCategory", "")
    c.setdefault("Guidance", {})
    return c
