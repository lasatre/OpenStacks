#!/usr/bin/env bash
# ==========================================================================
#  The Linux release in one go (CI and local): QEMU, the open renderer, the front-end
#  (PyInstaller), then the AppImage, and a smoke test of the frozen app.
#
#    packaging/build_linux.sh [version]
#
#  version: default <__version__>-local. STAMP_VERSION=1 writes it into the sources first
#  (CI; set_version.py edits frontend/openstacks/__init__.py). Build dirs (a Linux
#  filesystem is much faster than a Windows drive under WSL): QEMU_BUILD (build-linux/),
#  RENDERER_BUILD (deps/ost-renderer-build-linux/), GFX_SRC (deps/gfxstream), VENV
#  (.venv-linux/), WORK_DIR (dist/, build-pyi/, release/: the repo). Tool downloads (pinned, sha256-checked) are cached in DL_DIR
#  (deps/packaging/). Output: release/OpenStacks-<version>-x86_64.AppImage.
#  Needs what qemu/build.sh and renderer/build_gfxstream.sh need, plus python3-venv,
#  patchelf, file, curl.
# ==========================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# tr: a CRLF checkout (core.autocrlf on Windows, built from WSL) left "\r" in the name
BASE_VERSION=$(tr -d '\r' < frontend/openstacks/__init__.py | sed -n 's/^__version__ = "\(.*\)"/\1/p')
VERSION="${1:-$BASE_VERSION-local}"
VERSION="${VERSION#v}"
QEMU_BUILD="${QEMU_BUILD:-$ROOT/build-linux}"
RENDERER_BUILD="${RENDERER_BUILD:-$ROOT/deps/ost-renderer-build-linux}"
VENV="${VENV:-$ROOT/.venv-linux}"
WORK="${WORK_DIR:-$ROOT}"          # dist/, build-pyi/, release/ go here
DL="${DL_DIR:-$ROOT/deps/packaging}"

# pinned packaging tools
APPIMAGETOOL_URL=https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
APPIMAGETOOL_SHA256=ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0
RUNTIME_URL=https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64
RUNTIME_SHA256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d
SEVENZIP_URL=https://github.com/ip7z/7zip/releases/download/26.06/7z2606-linux-x64.tar.xz
SEVENZIP_SHA256=2b539cf067b2ad48e1baa5093dbaa8818b73542d1222a0ffde9dc5f2922ea62e

fetch() {   # url sha256 file
  if [ ! -s "$3" ] || ! echo "$2  $3" | sha256sum -c --status; then
    echo "[package] downloading $1"
    curl -fsSL -A openstacks-build -o "$3.part" "$1"
    mv "$3.part" "$3"
  fi
  echo "$2  $3" | sha256sum -c --status || { echo "$3: sha256 mismatch"; rm -f "$3"; exit 1; }
}

echo "== OpenStacks $VERSION (Linux) =="
[ "${STAMP_VERSION:-0}" = 1 ] && python3 packaging/set_version.py "$VERSION"

BUILD_DIR="$QEMU_BUILD" bash qemu/build.sh
BUILD_DIR="$RENDERER_BUILD" QEMU_BUILD="$QEMU_BUILD" bash renderer/build_gfxstream.sh
for f in qemu-system-x86_64 libost_renderer.so libshadertranslator.so libc++.so; do
  [ -e "$QEMU_BUILD/$f" ] || { echo "$QEMU_BUILD/$f missing"; exit 1; }
done
"$QEMU_BUILD/qemu-system-x86_64" -device help | grep -q bstpgaipc \
  || { echo "QEMU lacks the OpenStacks devices"; exit 1; }

# front-end
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install -q -r frontend/requirements.txt pyinstaller
"$VENV/bin/python" -m PyInstaller --noconfirm --log-level WARN --distpath "$WORK/dist" \
  --workpath "$WORK/build-pyi" packaging/openstacks.spec

mkdir -p "$DL"
fetch "$APPIMAGETOOL_URL" "$APPIMAGETOOL_SHA256" "$DL/appimagetool-x86_64.AppImage"
fetch "$RUNTIME_URL" "$RUNTIME_SHA256" "$DL/runtime-x86_64"
fetch "$SEVENZIP_URL" "$SEVENZIP_SHA256" "$DL/7z-linux-x64.tar.xz"
chmod +x "$DL/appimagetool-x86_64.AppImage"
tar -xJf "$DL/7z-linux-x64.tar.xz" -C "$DL" 7zzs

python3 packaging/assemble.py --version "$VERSION" --qemu "$QEMU_BUILD" \
  --dist "$WORK/dist/OpenStacks" --out "$WORK/release" \
  --sevenzip "$DL/7zzs" --appimagetool "$DL/appimagetool-x86_64.AppImage" \
  --runtime "$DL/runtime-x86_64"

# smoke test: the frozen front-end in the AppDir, no display, a throwaway data dir
SMOKE_HOME="$(mktemp -d)"
OPENSTACKS_HOME="$SMOKE_HOME" QT_QPA_PLATFORM=offscreen \
  "$WORK/release/OpenStacks.AppDir/OpenStacks/OpenStacks" --smoke "$SMOKE_HOME/smoke.txt" \
  && smoke=0 || smoke=$?
cat "$SMOKE_HOME/smoke.txt" 2>/dev/null || true
rm -rf "$SMOKE_HOME"
[ "$smoke" = 0 ] || { echo "smoke test failed ($smoke)"; exit 1; }
echo
echo "Release: $WORK/release/OpenStacks-$VERSION-x86_64.AppImage"
