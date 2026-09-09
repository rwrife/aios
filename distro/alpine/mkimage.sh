#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
. "$ROOT_DIR/build.env"
APORTS_DIR="${APORTS_DIR:-$ROOT_DIR/aports}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/out}"
WORK_DIR="${WORK_DIR:-$ROOT_DIR/.work}"
ARCH="${ARCH:-x86_64}"
RELEASE_TAG="${RELEASE_TAG:-$(date -u +%Y%m%d)}"
REPO_BASE="${REPO_BASE:-https://dl-cdn.alpinelinux.org/alpine}"
REPO_MAIN="${REPO_MAIN:-$REPO_BASE/$ALPINE_BRANCH/main}"
REPO_COMMUNITY="${REPO_COMMUNITY:-$REPO_BASE/$ALPINE_BRANCH/community}"

mkdir -p "$OUT_DIR" "$WORK_DIR"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[aios] missing required command: $1" >&2
    exit 1
  }
}

need_cmd git
git config --global --add safe.directory "$APORTS_DIR"

if [ ! -f /usr/share/abuild/functions.sh ]; then
  echo "[aios] missing /usr/share/abuild/functions.sh" >&2
  echo "[aios] install Alpine build deps first (apk add abuild apk-tools alpine-conf fakeroot xorriso squashfs-tools mtools syslinux grub)" >&2
  exit 1
fi

if [ ! -d "$APORTS_DIR/.git" ]; then
  echo "[aios] cloning Alpine aports into $APORTS_DIR"
  git init "$APORTS_DIR"
fi
if ! git -C "$APORTS_DIR" remote get-url origin >/dev/null 2>&1; then
  git -C "$APORTS_DIR" remote add origin https://github.com/alpinelinux/aports.git
fi
git -C "$APORTS_DIR" fetch --depth 1 origin "$APORTS_REF"
git -C "$APORTS_DIR" sparse-checkout set scripts
git -C "$APORTS_DIR" checkout --detach "$APORTS_REF"

# Keep profile/overlay helper scripts in sync with this repo on each build.
cp "$ROOT_DIR/profiles/mkimg.aios.sh" "$APORTS_DIR/scripts/mkimg.aios.sh"
cp "$ROOT_DIR/apkovl/genapkovl-aios.sh" "$APORTS_DIR/scripts/genapkovl-aios.sh"

export AIOS_WORLD_BASE="$ROOT_DIR/apks/world.base"
export AIOS_WORLD_X11="$ROOT_DIR/apks/world.x11"
export AIOS_WORLD_VM="$ROOT_DIR/apks/world.vm"
export AIOS_WORLD_DEVEL="$ROOT_DIR/apks/world.devel"
export AIOS_WORLD_AI="$ROOT_DIR/apks/world.ai"
export AIOS_IDENTITY_BUILD="${AIOS_IDENTITY_BUILD:-0}"
case "$AIOS_IDENTITY_BUILD" in
  0) export AIOS_WORLD_IDENTITY= ;;
  1) export AIOS_WORLD_IDENTITY="$ROOT_DIR/apks/world.identity" ;;
  *) echo 'AIOS_IDENTITY_BUILD must be 0 or 1' >&2; exit 1 ;;
esac
export AIOS_STAGE_DIR="$WORK_DIR/stage"
export AIOS_REPOSITORIES="$REPO_MAIN $REPO_COMMUNITY"
export AIOS_APKOVL_SCRIPT="genapkovl-aios.sh"
export AIOS_OVERLAY_DIR="$ROOT_DIR/overlay"

BUILD_DIR="$WORK_DIR/apps" DESTDIR="$AIOS_STAGE_DIR" "$ROOT_DIR/../../scripts/build-apps.sh"

cd "$APORTS_DIR/scripts"

./mkimage.sh \
  --hostkeys \
  --arch "$ARCH" \
  --outdir "$OUT_DIR" \
  --workdir "$WORK_DIR" \
  --profile aios \
  --repository "$REPO_MAIN" \
  --repository "$REPO_COMMUNITY" \
  --tag "$RELEASE_TAG" \
  "$@"

echo "[aios] build finished. artifacts in: $OUT_DIR"
cp "$ROOT_DIR/build.env" "$OUT_DIR/build-inputs.env"
for iso in "$OUT_DIR"/*-"$RELEASE_TAG"-"$ARCH".iso; do
  xorriso -indev "$iso" -find /apks -type f -name '*.apk' -exec echo -- 2>/dev/null |
    sed "s|.*/||;s/'//g" | sort > "$iso.packages.txt"
done
(cd "$OUT_DIR" && sha256sum ./*.iso > SHA256SUMS)
