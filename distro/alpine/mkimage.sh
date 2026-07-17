#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APORTS_DIR="${APORTS_DIR:-$ROOT_DIR/aports}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/out}"
WORK_DIR="${WORK_DIR:-$ROOT_DIR/.work}"
ARCH="${ARCH:-x86_64}"
RELEASE_TAG="${RELEASE_TAG:-$(date -u +%Y%m%d)}"

mkdir -p "$OUT_DIR" "$WORK_DIR"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[aios] missing required command: $1" >&2
    exit 1
  }
}

need_cmd git

if [ ! -f /usr/share/abuild/functions.sh ]; then
  echo "[aios] missing /usr/share/abuild/functions.sh" >&2
  echo "[aios] install Alpine build deps first (apk add abuild apk-tools alpine-conf fakeroot xorriso squashfs-tools mtools syslinux grub)" >&2
  exit 1
fi

if [ ! -d "$APORTS_DIR/.git" ]; then
  echo "[aios] cloning Alpine aports into $APORTS_DIR"
  git clone --depth 1 https://gitlab.alpinelinux.org/alpine/aports.git "$APORTS_DIR"
fi

# Keep profile in sync with this repo on each build.
cp "$ROOT_DIR/profiles/mkimg.aios.sh" "$APORTS_DIR/scripts/mkimg.aios.sh"

export AIOS_WORLD_BASE="$ROOT_DIR/apks/world.base"
export AIOS_WORLD_X11="$ROOT_DIR/apks/world.x11"
export AIOS_WORLD_VM="$ROOT_DIR/apks/world.vm"
export AIOS_APKOVL_SCRIPT="$ROOT_DIR/apkovl/genapkovl-aios.sh"
export AIOS_OVERLAY_DIR="$ROOT_DIR/overlay"

cd "$APORTS_DIR/scripts"

./mkimage.sh \
  --arch "$ARCH" \
  --outdir "$OUT_DIR" \
  --workdir "$WORK_DIR" \
  --profile aios \
  --tag "$RELEASE_TAG" \
  "$@"

echo "[aios] build finished. artifacts in: $OUT_DIR"
