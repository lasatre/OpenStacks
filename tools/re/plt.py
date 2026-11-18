"""Map PLT stub addresses of a guest .so to symbol names (.rela.plt order).

    python tools/re/plt.py <lib.so> [0xaddr ...]
"""
import sys

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

f = ELFFile(open(sys.argv[1], "rb"))
plt = f.get_section_by_name(".plt")
rel = f.get_section_by_name(".rela.plt")
symtab = f.get_section(rel["sh_link"])
names = [symtab.get_symbol(r["r_info_sym"]).name for r in rel.iter_relocations()]
base = plt["sh_addr"] + 16                      # PLT0 is 16 bytes, entries are 16 bytes
want = [int(a, 16) for a in sys.argv[2:]]
for i, n in enumerate(names):
    a = base + 16 * i
    if not want or a in want:
        print(f"0x{a:x} {n}")
