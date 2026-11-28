"""On-screen image detection for "Smart" schemes: the scheme's `Images` as QEMU lines.

BlueStacks recognises game UI by texture (HD-Player Imgd.cpp, docs/re/kmm_controls.md 3.20):
the renderer hashes texture uploads (CRC32) and, per draw, tests the texture bound to the
game's sampler against each image's `TextureCRC`, then looks for a drawn triangle whose
texture coordinates (vertex attribute `TextureIndex`) contain `TextureCoord`. With a
`VertexRect`, the triangle's centre on screen (attribute `VertexIndex`, through the game's
vertex transform) must also fall inside it. Each frame's matches become the scheme's tags.

Two per-game tables from HD-Player pick the sampler uniform and the vertex transform; both
are keyed by package.

Lines to QEMU (gr_render.c gr_imgd_line):
    I b <gen> <emu> <sampler|-> <sampler2|->
    I i <crc hex> <tex attr> <pos attr> <u> <v> [x1 y1 x2 y2]
    I c
QEMU answers `I <gen> <1|0>` (detecting or not), then `T <gen> <index...>` whenever the
set of matched images changes (indices into the `I i` lines).
"""
from __future__ import annotations

from typing import Iterable

EMU_NONE, EMU_UNITY = 0, 1

# sampler uniform whose texture unit is checked (Imgd.cpp, 0x1402b2470); others: unit 0
SAMPLERS = {
    "com.dts.freefireth": "_AlphaTex",
    "com.dts.freefiremax": "_AlphaTex",
    "com.activision.callofduty.shooter": "_MainTex",
    "com.tencent.tmgp.kr.codm": "_MainTex",
    "com.garena.game.codm": "_MainTex",
    "com.vng.codmvn": "_MainTex",
    "com.pixonic.wwr": "_MainTex",
    "com.tencent.ig": "ps0",
    "com.pubg.imobile": "ps0",
    "com.supercell.brawlstars": "s_texture",
    "jp.pokemon.pokemonunite": "_MainTex",
}

# the screen-position emulation per package (table 0x140acf600). Only GroupA (Unity
# HLSLcc matrices) is implemented; the others (War Robots, Roblox, PUBG/Apex, Undawn-like,
# Brawl Stars) fall back to no VertexRect test.
UNITY_GROUP = {
    "com.dts.freefireth", "com.dts.freefiremax", "com.axlebolt.standoff2",
    "com.kitkagames.fallbuddies", "jp.pokemon.pokemonunite",
    "com.activision.callofduty.shooter", "com.tencent.tmgp.kr.codm", "com.garena.game.codm",
    "com.vng.codmvn", "com.HoYoverse.hkrpgoversea",
}


def sampler_for(package: str) -> tuple[str, str]:
    """(sampler uniform, fallback): Free Fire falls back to _NormalTex (FUN_1402b1400)."""
    s1 = SAMPLERS.get(package, "")
    s2 = "_NormalTex" if s1 and "com.dts.freefire" in package else ""
    return s1, s2


def _crc(v) -> int:
    if isinstance(v, (int, float)):
        return int(v) & 0xFFFFFFFF
    return int(str(v).strip(), 0) & 0xFFFFFFFF


def config_lines(gen: int, package: str, images: Iterable[dict]) -> tuple[list[str], list[str]]:
    """The `I` lines for a scheme's Images and the image id of each `I i` line, in order.
    Images HD-Player would not detect (TextureCoord not 2 numbers) are left out, and so are
    DetectColor images (colour matching is not implemented)."""
    s1, s2 = sampler_for(package)
    emu = EMU_UNITY if package in UNITY_GROUP else EMU_NONE
    lines = [f"I b {gen} {emu} {s1 or '-'} {s2 or '-'}"]
    ids: list[str] = []
    for im in images:
        try:
            if im.get("DetectColor"):
                continue
            tc = im.get("TextureCoord") or []
            if len(tc) != 2:
                continue                  # "Bad cfg. TextureCoordLen = %d" (imageDetect)
            crc = _crc(im.get("TextureCRC", 0))
            if not crc:
                continue
            line = (f"I i {crc:08x} {int(im.get('TextureIndex', 0))} "
                    f"{int(im.get('VertexIndex', 0))} {float(tc[0]):.6f} {float(tc[1]):.6f}")
            rect = im.get("VertexRect") or []
            if len(rect) == 4:
                line += " " + " ".join(f"{float(r):.4f}" for r in rect)
        except (TypeError, ValueError):
            continue
        lines.append(line)
        ids.append(str(im.get("ImageId", "")))
    if not ids:
        lines = [f"I b {gen} 0 - -"]      # no images: detection off
    lines.append("I c")
    return lines, ids
