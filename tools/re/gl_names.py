"""Name every opcode a guest GLES encoder emits.

libGLESv{1_CM,2}_emulation.so exports each gl* entry point, which calls through the shared
client context table (`ctx->glFoo`, an indirect call at [reg + offset]). The encoder fills the
same table in its constructor and each encoder function writes its opcode. Joining the two
gives name -> table offset -> opcode.

    python tools/re/gl_names.py <lib*_emulation.so> <lib*_enc.so> <base> [gfxstream opcodes.h]
"""
import re
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from capstone.x86 import X86_OP_MEM  # noqa: E402
from encoder_opcodes import Lib, ctor_table, opcode_of  # noqa: E402

emu_path, enc_path, base = sys.argv[1], sys.argv[2], int(sys.argv[3])
header = sys.argv[4] if len(sys.argv) > 4 else None

enc = Lib(enc_path)
off2op = {}
for off, fn in ctor_table(enc):
    op = opcode_of(enc, fn, base, base + 4000)
    if op is not None:
        off2op[off] = op

emu = Lib(emu_path)
name2off = {}
for name, (addr, size) in emu.syms.items():
    if not name.startswith("gl") or size == 0:
        continue
    for ins in emu.insns(addr, 80):
        if ins.mnemonic in ("call", "jmp") and ins.operands and ins.operands[0].type == X86_OP_MEM \
                and ins.operands[0].mem.base != 41 and ins.operands[0].mem.disp in off2op:
            name2off[name] = ins.operands[0].mem.disp
            break
        if ins.mnemonic == "ret":
            break

gfx = {}
if header:
    gfx = {int(v): n for n, v in re.findall(r"#define OP_(\w+)\s+(\d+)", open(header).read())}
rows = sorted((off2op[o], n) for n, o in name2off.items())
print(f"{len(rows)} named opcodes (of {len(off2op)} known)")
for op, n in rows:
    tag = "" if not gfx else ("  ok" if gfx.get(op) == n else f"  GFXSTREAM: {gfx.get(op, '-')}")
    if not gfx or tag != "  ok":
        print(f"{op} {n}{tag}")
