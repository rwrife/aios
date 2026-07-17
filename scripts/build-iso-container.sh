#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
RUNTIME="${RUNTIME:-docker}"
IMAGE="${IMAGE:-alpine:3.22}"
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  arm64) HOST_ARCH="aarch64" ;;
  amd64) HOST_ARCH="x86_64" ;;
esac
ARCH="${ARCH:-$HOST_ARCH}"
CONTAINER_PLATFORM="${CONTAINER_PLATFORM:-}"
RELEASE_TAG="${RELEASE_TAG:-$(date -u +%Y%m%d)}"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

if [ -z "$CONTAINER_PLATFORM" ]; then
  case "$ARCH" in
    x86|x86_64) CONTAINER_PLATFORM="linux/amd64" ;;
    aarch64|arm64) CONTAINER_PLATFORM="linux/arm64" ;;
  esac
fi

if ! command -v "$RUNTIME" >/dev/null 2>&1; then
  echo "[aios] container runtime '$RUNTIME' not found" >&2
  echo "[aios] install Docker or set RUNTIME=podman" >&2
  exit 1
fi

mkdir -p \
  "$ROOT_DIR/distro/alpine/out" \
  "$ROOT_DIR/distro/alpine/.work" \
  "$ROOT_DIR/distro/alpine/aports"

exec "$RUNTIME" run --rm \
  --platform "$CONTAINER_PLATFORM" \
  -e ARCH="$ARCH" \
  -e RELEASE_TAG="$RELEASE_TAG" \
  -e HOST_UID="$HOST_UID" \
  -e HOST_GID="$HOST_GID" \
  -v "$ROOT_DIR:/workspace" \
  -w /workspace \
  "$IMAGE" /bin/sh -eu -c '
    pkgs="abuild apk-tools alpine-conf busybox fakeroot xorriso squashfs-tools mtools grub grub-efi git bash coreutils tar findutils"
    case "$ARCH" in
      x86|x86_64) pkgs="$pkgs syslinux" ;;
    esac
    apk add --no-cache $pkgs

    # mkimage signs local APKINDEX; generate ephemeral key in container.
    abuild-keygen -a -i -n >/dev/null 2>&1 || true
    if [ -z "${PACKAGER_PRIVKEY:-}" ]; then
      key="$(ls /root/.abuild/*.rsa 2>/dev/null | head -n 1 || true)"
      if [ -n "$key" ]; then
        export PACKAGER_PRIVKEY="$key"
      fi
    fi

    chmod +x \
      /workspace/scripts/build-iso.sh \
      /workspace/distro/alpine/mkimage.sh \
      /workspace/distro/alpine/apkovl/genapkovl-aios.sh

    ARCH="$ARCH" \
    RELEASE_TAG="$RELEASE_TAG" \
    APORTS_DIR="/workspace/distro/alpine/aports" \
    OUT_DIR="/workspace/distro/alpine/out" \
    WORK_DIR="/workspace/distro/alpine/.work" \
    /workspace/scripts/build-iso.sh "$@"

    chown -R "$HOST_UID:$HOST_GID" \
      /workspace/distro/alpine/out \
      /workspace/distro/alpine/.work \
      /workspace/distro/alpine/aports || true
  ' -- "$@"
