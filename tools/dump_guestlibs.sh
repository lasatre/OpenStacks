#!/bin/sh
# Dump guest files from a Root_*.vhd (dynamic VHD, ext4 at +1 MiB) without touching it.
# usage (WSL Debian, as root; needs qemu-utils + e2fsprogs):
#   dump_guestlibs.sh <image.vhd> <outdir> <guest path>...
IMG=$1; OUT=$2; shift 2
B=/root/$(basename "$IMG" .vhd)
P=$B.part.ext4
if [ ! -s "$P" ]; then
  rm -f "$B"_part.ext4
  qemu-img convert -f vpc -O raw "$IMG" "$B.raw" || exit 1
  dd if="$B.raw" of="$P" bs=1M skip=1 conv=sparse status=none && rm -f "$B.raw"
fi
mkdir -p "$OUT"
for p in "$@"; do
  f=$(echo "$p" | tr / _)
  debugfs -R "dump $p $OUT/$f" "$P" >/dev/null 2>&1
  if [ -s "$OUT/$f" ]; then echo "ok $p"; else echo "missing $p"; rm -f "$OUT/$f"; fi
done
