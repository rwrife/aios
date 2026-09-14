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
need_cmd python3
need_cmd xorriso
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

# Recorded-evidence strategy (see docs/qa/hardware-coverage.md): Alpine's
# release repositories keep moving, so instead of claiming a pin that does not
# exist, record what this build actually resolved. build-inputs.env holds the
# *effective* pin values (env overrides included), and the per-ISO
# build-manifest.json additionally records the effective repository URLs, the
# post-build SHA-256 sample of each architecture-specific APKINDEX.tar.gz, the
# exact embedded APK closure, the ISO/kernel/initramfs/modloop hashes, and
# whatever modloop kernel identity the artifacts expose. The APK closure is
# authoritative for what shipped; the index sample is diagnostic because a
# moving repository could change while mkimage is running.
{
  echo "# Effective build inputs recorded by distro/alpine/mkimage.sh."
  echo "# Values reflect this build, including environment overrides."
  echo "ALPINE_BRANCH=$ALPINE_BRANCH"
  echo "IMAGE=$IMAGE"
  echo "APORTS_REF=$APORTS_REF"
  echo "LLAMA_REF=$LLAMA_REF"
  echo "WHISPER_REF=$WHISPER_REF"
} > "$OUT_DIR/build-inputs.env"

for iso in "$OUT_DIR"/*-"$RELEASE_TAG"-"$ARCH".iso; do
  [ -f "$iso" ] || continue
  python3 "$ROOT_DIR/record-build-manifest.py" \
    --iso "$iso" \
    --arch "$ARCH" \
    --release-tag "$RELEASE_TAG" \
    --build-env "$ROOT_DIR/build.env" \
    --effective "ALPINE_BRANCH=$ALPINE_BRANCH" \
    --effective "IMAGE=$IMAGE" \
    --effective "APORTS_REF=$APORTS_REF" \
    --effective "LLAMA_REF=$LLAMA_REF" \
    --effective "WHISPER_REF=$WHISPER_REF" \
    --repository "main=$REPO_MAIN" \
    --repository "community=$REPO_COMMUNITY" \
    --build-setting "REPO_BASE=$REPO_BASE" \
    --build-setting "ARCH=$ARCH" \
    --build-setting "AIOS_IDENTITY_BUILD=$AIOS_IDENTITY_BUILD" \
    --work-dir "$WORK_DIR/build-manifest" \
    --packages-txt "$iso.packages.txt" \
    --output "$iso.build-manifest.json"
done
(cd "$OUT_DIR" && sha256sum ./*.iso > SHA256SUMS)
