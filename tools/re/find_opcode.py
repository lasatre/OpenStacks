"""Find the guest encoder function that emits <opcode>, its table slot, and who calls that slot.

    python tools/re/find_opcode.py <enc.so> <opcode> [caller.so ...]
"""
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_OP_REG  # noqa: E402
from encoder_opcodes import Lib, ctor_table  # noqa: E402

enc_path, opcode = sys.argv[1], int(sys.argv[2])
callers = sys.argv[3:]
lib = Lib(enc_path)

# 1. instructions writing the opcode immediate as a dword store
hits = []
for ins in lib.cs.disasm(lib.tdata, lib.taddr):
    if ins.mnemonic == "mov" and len(ins.operands) == 2 and ins.operands[0].type == X86_OP_MEM \
            and ins.operands[1].type == X86_OP_IMM and ins.operands[0].size == 4 \
            and (ins.operands[1].imm & 0xFFFFFFFF) == opcode:
        hits.append(ins.address)
print(f"stores of {opcode}: {[hex(h) for h in hits]}")

# 2. enclosing function = nearest preceding table entry / symbol start
table = ctor_table(lib)
starts = sorted({fn for _, fn in table} | {a for a, _ in lib.syms.values()})
for h in hits:
    fn = max(s for s in starts if s <= h)
    slots = [off for off, f in table if f == fn]
    print(f"  0x{h:x} in fn 0x{fn:x}, table slot(s) {[hex(o) for o in slots] or 'not in captured table'}")

# 3. raw table stores we may have missed: any 'lea reg,[rip+fn]' to that function in the ctor area
for h in hits:
    fn = max(s for s in starts if s <= h)
    for ins in lib.cs.disasm(lib.tdata, lib.taddr):
        if ins.mnemonic == "lea" and len(ins.operands) == 2 and ins.operands[1].type == X86_OP_MEM \
                and ins.operands[1].mem.base == 41 \
                and ins.address + ins.size + ins.operands[1].mem.disp == fn:
            print(f"  referenced (lea) at 0x{ins.address:x}")
