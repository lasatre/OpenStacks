"""Summarise GR_TRACE=1 output (QEMU log) against gfxstream's decoder opcode tables.

    python tools/re/trace_summary.py <out.log or qemu.log> [gfxstream source dir]

Prints every opcode seen (all connections merged, latest census per connection), with
gfxstream's name for it, and flags the ones gfxstream doesn't decode.
"""
import os
import re
import sys

log = sys.argv[1]
gfx = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "..",
                                                           "deps", "gfxstream")
names = {}
for rel in ("host/renderControl_dec/renderControl_opcodes.h", "host/gl/gles1_dec/gles1_opcodes.h",
            "host/gl/gles2_dec/gles2_opcodes.h"):
    p = os.path.join(gfx, rel)
    if os.path.exists(p):
        for n, v in re.findall(r"#define OP_(\w+)\s+(\d+)", open(p).read()):
            if not n.startswith("last"):
                names[int(v)] = n
vk = os.path.join(gfx, "host/vulkan/cereal/common/goldfish_vk_marshaling.h")
if os.path.exists(vk):
    for n, v in re.findall(r"#define OP_(vk\w+)\s+(\d+)", open(vk).read()):
        names[int(v)] = n

# latest census per (cid, client name); a census line follows its "trace ... cid=N 'name'" header
census = {}
cur = None
for line in open(log, encoding="utf-8", errors="replace"):
    m = re.search(r"trace (close|periodic|partial) cid=(\d+) '([^']*)'", line)
    if m:
        cur = (m.group(2), m.group(3).split(" (pid")[0])
        census[cur] = {}
        continue
    m = re.search(r"trace cid=(\d+) ((?:(?:gl1|gl2|rc|vk|\?)\d+:\d+:\d+ ?)+)$", line.strip())
    if m and cur:
        for api, op, cnt, nbytes in re.findall(r"(gl1|gl2|rc|vk|\?)(\d+):(\d+):(\d+)", m.group(2)):
            census[cur][int(op)] = (api, int(cnt), int(nbytes))

total = {}
clients = {}
for (cid, who), ops in census.items():
    for op, (api, cnt, nbytes) in ops.items():
        t = total.setdefault(op, [api, 0, 0])
        t[1] += cnt
        t[2] += nbytes
        clients.setdefault(op, set()).add(who)

print(f"{len(total)} distinct opcodes over {len(census)} connections")
unknown = []
for op in sorted(total):
    api, cnt, nbytes = total[op]
    name = names.get(op)
    if name is None:
        unknown.append(op)
    print(f"{api:3} {op:6} {cnt:8} {nbytes:12}  {name or '*** NOT IN GFXSTREAM ***'}"
          f"{'' if name else '  ' + ', '.join(sorted(clients[op]))[:80]}")
print("\nnot decoded by gfxstream:", unknown or "none")
