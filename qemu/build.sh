#!/usr/bin/env bash
# ==========================================================================
#  OpenStacks QEMU build (Linux). The counterpart of build.cmd:
#   1. fetch QEMU 11.1.0 into deps/ (once; reuses a local tarball if present)
#   2. copy qemu/hw/misc/* into the tree + hook Kconfig/meson (apply_to_tree.py)
#   3. configure (once) + ninja into build-linux/ (BUILD_DIR overrides; a Linux
#      filesystem is much faster than a Windows drive under WSL)
#   4. copy pc-bios next to the binary
#  Needs: gcc, ninja, python3 (+ venv, python3-tomli before 3.11), pkg-config,
#         libglib2.0-dev, libpixman-1-dev, libslirp-dev, libfdt-dev, zlib1g-dev,
#         libpulse-dev, libx11-dev; ccache is used when installed. QEMU_TREE overrides
#         the source tree.
#  Audio: PulseAudio (PipeWire desktops serve it through pipewire-pulse; QEMU's own
#         PipeWire backend needs libpipewire >= 0.3.60, newer than Ubuntu 22.04's).
# ==========================================================================
set -euo pipefail
QDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$QDIR")"
QVER=11.1.0
QEMU_TREE="${QEMU_TREE:-$ROOT/deps/qemu-$QVER}"
BUILD="${BUILD_DIR:-$ROOT/build-linux}"

mkdir -p "$ROOT/deps"
if [ ! -f "$QEMU_TREE/meson.build" ]; then
  tarball="$ROOT/deps/qemu-$QVER.tar.xz"
  if [ ! -f "$tarball" ]; then
    echo "[build] fetching QEMU $QVER"
    curl -fL -o "$tarball" "https://download.qemu.org/qemu-$QVER.tar.xz"
  fi
  tar -xJf "$tarball" -C "$(dirname "$QEMU_TREE")"
fi

python3 "$QDIR/apply_to_tree.py" "$QEMU_TREE"

mkdir -p "$BUILD"
# reconfigure when the build dir was configured against another source tree
if [ -f "$BUILD/build.ninja" ] && ! grep -q "$QEMU_TREE" "$BUILD/build.ninja"; then
  echo "[build] $BUILD belongs to another QEMU tree: reconfiguring"
  rm -rf "$BUILD"; mkdir -p "$BUILD"
fi
if [ ! -f "$BUILD/build.ninja" ]; then
  cc=gcc; command -v ccache >/dev/null && cc="ccache gcc"
  if ! (cd "$BUILD" && "$QEMU_TREE/configure" --target-list=x86_64-softmmu --cc="$cc" \
      --enable-kvm --enable-slirp --audio-drv-list=pa \
      --disable-werror --disable-docs --disable-tools --disable-sdl --disable-gtk); then
    echo "configure FAILED - see the messages above"
    rm -f "$BUILD/build.ninja"
    exit 1
  fi
fi

ninja -C "$BUILD" qemu-system-x86_64 -j "$(nproc)"

# firmware next to the binary (merged into meson's own pc-bios output dir, like build.cmd)
mkdir -p "$BUILD/pc-bios"
cp -rL --update "$QEMU_TREE/pc-bios/." "$BUILD/pc-bios/"

echo
echo "Build OK: $BUILD/qemu-system-x86_64"
