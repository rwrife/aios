#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
source "$ROOT_DIR/distro/alpine/build.env"
RUNTIME="${RUNTIME:-docker}"
ARCH="${ARCH:-x86_64}"
AIOS_COMMIT="${AIOS_COMMIT:-$(git -C "$ROOT_DIR" rev-parse --short=12 HEAD 2>/dev/null || printf unknown)}"
AIOS_BUILD_NUMBER="${AIOS_BUILD_NUMBER:-${GITHUB_RUN_NUMBER:-$(date -u +%Y%m%d.%H%M)}}"
[[ "$ARCH" == x86_64 ]] || { echo 'Only x86_64 is validated by this build profile.' >&2; exit 1; }
command -v "$RUNTIME" >/dev/null || { echo "Container runtime not found: $RUNTIME" >&2; exit 1; }
mkdir -p "$ROOT_DIR/distro/alpine/out"
exec "$RUNTIME" run --rm --platform linux/amd64 \
  -e ARCH="$ARCH" -e RELEASE_TAG="${RELEASE_TAG:-$(date -u +%Y%m%d)}" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -e AIOS_IDENTITY_BUILD="${AIOS_IDENTITY_BUILD:-0}" \
  -e AIOS_COMMIT="$AIOS_COMMIT" -e AIOS_BUILD_NUMBER="$AIOS_BUILD_NUMBER" \
  -v "$ROOT_DIR:/workspace" -v aios-build-cache:/build -w /workspace \
  "${AIOS_BUILDER_IMAGE:-$IMAGE}" sh /workspace/scripts/container-build.sh "$@"
