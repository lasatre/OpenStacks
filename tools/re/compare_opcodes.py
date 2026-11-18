"""Compare a guest encoder's opcode range with gfxstream's decoder opcode table.

    python tools/re/compare_opcodes.py <guest_enc.so> <base> <gfxstream *_opcodes.h>
"""
import re
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from encoder_opcodes import Lib, ctor_table, opcode_of  # noqa: E402

lib_path, base, header = sys.argv[1], int(sys.argv[2]), sys.argv[3]
lib = Lib(lib_path)
table = ctor_table(lib)
found = {}
for off, fn in table:
    op = opcode_of(lib, fn, base, base + 4000)
    if op is not None:
        found[op] = off
# the constructor stores one pointer per call: field slot -> opcode by position
offs = sorted(o for o, _ in table)
slot_ops = sorted({base + (o - offs[0]) // 8 for o in offs})
gfx = {int(v): n for n, v in re.findall(r"#define OP_(\w+)\s+(\d+)", open(header).read())
       if not n.startswith("last")}
gfx_ops = sorted(v for v in gfx if v < base + 4000)
print(f"guest: {len(table)} table stores, {len(found)} opcodes read directly, "
      f"range {min(found)}..{max(found)}; slots imply {slot_ops[0]}..{slot_ops[-1]}")
print(f"gfxstream: {len(gfx_ops)} opcodes, range {gfx_ops[0]}..{gfx_ops[-1]}")
mism = [(off, op) for op, off in found.items() if base + (off - offs[0]) // 8 != op]
print("slot/opcode mismatches (non-sequential table):", mism[:10] or "none")
beyond = [op for op in found if op not in gfx]
print("guest opcodes gfxstream doesn't know:", beyond or "none")
