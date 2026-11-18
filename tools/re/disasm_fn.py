"""Disassemble one function of a guest .so with symbol names and string literals annotated.

    python tools/re/disasm_fn.py <lib.so> <symbol substring | 0xaddr> [max_bytes]
"""
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from encoder_opcodes import Lib  # noqa: E402

lib = Lib(sys.argv[1])
key = sys.argv[2]
if key.startswith("0x"):
    addr, size = int(key, 16), int(sys.argv[3]) if len(sys.argv) > 3 else 600
else:
    name, (addr, size) = next((n, v) for n, v in lib.syms.items() if key in n)
    print(f"; {name} @0x{addr:x} size {size}")
inv = {a: n for n, (a, s) in lib.syms.items()}
secs = [lib.f.get_section_by_name(n) for n in (".rodata", ".data.rel.ro", ".data")]
secs = [(s.data(), s["sh_addr"]) for s in secs if s]


def cstr(t):
    for d, a in secs:
        if a <= t < a + len(d):
            e = d.find(b"\0", t - a)
            raw = d[t - a:e if e >= 0 else t - a + 80]
            if raw and all(32 <= c < 127 for c in raw[:40]):
                return raw[:100].decode("latin1")
    return None


for ins in lib.cs.disasm(lib.code(addr, size), addr):
    extra = ""
    if ins.mnemonic == "call" and ins.op_str.startswith("0x"):
        extra = "  ; " + inv.get(int(ins.op_str, 16), "?")
    elif ins.mnemonic == "lea" and "rip" in ins.op_str:
        t = ins.address + ins.size + ins.operands[1].mem.disp
        s = cstr(t)
        extra = f'  ; "{s}"' if s else f"  ; 0x{t:x}"
    print(f"{ins.address:x}: {ins.mnemonic:6} {ins.op_str}{extra}")
