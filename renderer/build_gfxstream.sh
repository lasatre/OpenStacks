#!/usr/bin/env bash
# ==========================================================================
#  libost_renderer.so: gfxstream host renderer for OpenStacks (Linux). The counterpart
#  of build_gfxstream.cmd:
#   1. fetch gfxstream at the pinned commit into deps/gfxstream (shallow, once)
#   2. apply renderer/gfxstream-patches/*.patch (once per patch)
#   3. configure + build (CMake, Ninja, clang) into deps/ost-renderer-build-linux
#      (BUILD_DIR overrides): libost_renderer.so, ost_renderer_smoke, st_probe
#   4. fetch libshadertranslator.so + its libc++.so (fetch_shadertranslator.py, pinned), and
#      install both next to the Linux QEMU build when it exists (QEMU_BUILD, default
#      build-linux/)
#  Needs: clang, cmake, ninja, git, python3, libx11-dev, libgl1-mesa-dev,
#         libegl1-mesa-dev, libdrm-dev; ccache when installed. GFX_SRC overrides the tree.
# ==========================================================================
set -euo pipefail
RDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$RDIR")"
GFX_COMMIT=cf47bd2660e7217f5729c91664d36d81c1b3282a
SRC="${GFX_SRC:-$ROOT/deps/gfxstream}"
BLD="${BUILD_DIR:-$ROOT/deps/ost-renderer-build-linux}"
QEMU_BUILD="${QEMU_BUILD:-$ROOT/build-linux}"

# shallow fetch of the pinned commit only (the full history is large)
if [ ! -d "$SRC/.git" ]; then
  echo "[gfxstream] fetching $GFX_COMMIT"
  git init -q "$SRC"
  git -C "$SRC" remote add origin https://github.com/google/gfxstream
fi
git -C "$SRC" cat-file -e "$GFX_COMMIT" 2>/dev/null || git -C "$SRC" fetch -q --depth 1 origin "$GFX_COMMIT"
if [ "$(git -C "$SRC" rev-parse HEAD 2>/dev/null || true)" != "$GFX_COMMIT" ]; then
  git -C "$SRC" -c advice.detachedHead=false checkout -q "$GFX_COMMIT"
fi
# (patches made on a CRLF Windows checkout apply here with the CRs stripped)
for p in "$RDIR"/gfxstream-patches/*.patch; do
  if sed 's/\r$//' "$p" | git -C "$SRC" apply --check - >/dev/null 2>&1; then
    echo "[gfxstream] applying $(basename "$p")"
    sed 's/\r$//' "$p" | git -C "$SRC" apply -
  elif ! sed 's/\r$//' "$p" | git -C "$SRC" apply --reverse --check - >/dev/null 2>&1; then
    echo "[gfxstream] $(basename "$p") neither applies nor is applied"; exit 1
  fi
done

if [ ! -f "$BLD/build.ninja" ]; then
  launcher=()
  command -v ccache >/dev/null && launcher=(-DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache)
  cmake -S "$RDIR" -B "$BLD" -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ -DGFXSTREAM_SRC="$SRC" "${launcher[@]}"
fi
cmake --build "$BLD" --target ost_renderer ost_renderer_smoke st_probe -j "$(nproc)"

python3 "$RDIR/fetch_shadertranslator.py" --dest "$BLD"
if [ -x "$QEMU_BUILD/qemu-system-x86_64" ]; then
  cp -f "$BLD/libost_renderer.so" "$BLD/libshadertranslator.so" "$BLD/libc++.so" "$QEMU_BUILD/"
  echo "[gfxstream] installed libost_renderer.so, libshadertranslator.so, libc++.so into $QEMU_BUILD"
fi
echo
echo "Build OK: $BLD/libost_renderer.so (smoke test: $BLD/ost_renderer_smoke)"
