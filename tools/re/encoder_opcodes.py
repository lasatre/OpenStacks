"""Opcode tables of a guest goldfish-opengl encoder (stripped .so), for comparing BlueStacks'
guest against gfxstream's decoders.

The encoder's constructor (`*_encoder_context_t::*_encoder_context_t`, exported) stores one
function pointer per API call, in API order. Each encoder function writes its opcode as an
immediate (`mov dword [reg], OP`). This lists (slot, function, opcode) in constructor order.

    python tools/re/encoder_opcodes.py <lib.so> [base]     base: 10000 rc, 1024 gles1, 2048 gles2
Needs pyelftools + capstone.
"""
from __future__ import annotations

import sys

from capstone import CS_ARCH_X86, CS_MODE_64, Cs
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_OP_REG
from elftools.elf.elffile import ELFFile


class Lib:
    def __init__(self, path: str):
        self.f = ELFFile(open(path, "rb"))
        self.syms = {}
        for s in self.f.get_section_by_name(".dynsym").iter_symbols():
            if s.name and s["st_value"]:
                self.syms[s.name] = (s["st_value"], s["st_size"])
        self.text = self.f.get_section_by_name(".text")
        self.tdata, self.taddr = self.text.data(), self.text["sh_addr"]
        self.cs = Cs(CS_ARCH_X86, CS_MODE_64)
        self.cs.detail = True

    def code(self, addr: int, n: int = 4096) -> bytes:
        o = addr - self.taddr
        return self.tdata[o:o + n]

    def insns(self, addr: int, limit: int = 3000):
        for i, ins in enumerate(self.cs.disasm(self.code(addr, 16384), addr)):
            if i >= limit:
                return
            yield ins
            if ins.mnemonic in ("ret", "jmp") and ins.op_str.startswith("0x") is False and ins.mnemonic == "ret":
                return


def ctor_table(lib: Lib):
    """[(field_offset, fn_addr)] in store order from the encoder constructor."""
    ctor = next((v for k, v in lib.syms.items() if "encoder_context_t" in k and ("C2E" in k or "C1E" in k)), None)
    if ctor is None:
        raise SystemExit("encoder constructor not found in .dynsym")
    lea = {}
    out = []
    for ins in lib.insns(ctor[0], 4000):
        if ins.mnemonic == "lea" and len(ins.operands) == 2 and ins.operands[1].type == X86_OP_MEM \
                and ins.operands[1].mem.base == 41:   # x86 RIP register id in capstone = 41
            target = ins.address + ins.size + ins.operands[1].mem.disp
            lea[ins.operands[0].reg] = target
        elif ins.mnemonic == "mov" and len(ins.operands) == 2 and ins.operands[0].type == X86_OP_MEM \
                and ins.operands[1].type == X86_OP_REG and ins.operands[1].reg in lea:
            out.append((ins.operands[0].mem.disp, lea[ins.operands[1].reg]))
        if ins.mnemonic == "ret":
            break
    return out


def opcode_of(lib: Lib, fn: int, lo: int, hi: int):
    """First `mov dword ptr [..], imm` with lo <= imm < hi in the function."""
    for ins in lib.insns(fn, 1200):
        if ins.mnemonic == "mov" and len(ins.operands) == 2 and ins.operands[0].type == X86_OP_MEM \
                and ins.operands[1].type == X86_OP_IMM and ins.operands[0].size == 4:
            v = ins.operands[1].imm & 0xFFFFFFFF
            if lo <= v < hi:
                return v
        if ins.mnemonic == "ret":
            break
    return None


def main() -> int:
    path = sys.argv[1]
    base = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
    lib = Lib(path)
    table = ctor_table(lib)
    for slot, (off, fn) in enumerate(table):
        op = opcode_of(lib, fn, base, base + 2000)
        print(f"{slot:4} +0x{off:04x} fn=0x{fn:06x} op={op}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
