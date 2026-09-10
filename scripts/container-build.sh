#!/bin/sh
set -eu
apk add --no-cache abuild apk-tools alpine-conf busybox fakeroot xorriso squashfs-tools \
  mtools grub grub-efi syslinux git bash coreutils tar findutils build-base cmake ninja \
  qt6-qtbase-dev qt6-qtdeclarative-dev qt6-qtmultimedia-dev curl-dev linux-headers python3 font-dejavu
case "${AIOS_BUILD_CACHE_NAMESPACE:-}" in
  *[!A-Za-z0-9._-]*|'') echo 'Invalid AIOS build cache namespace' >&2; exit 1 ;;
esac
shell_build="/build/worktrees/$AIOS_BUILD_CACHE_NAMESPACE/shell"
mkdir -p /build/home /build/signing /build/work "$shell_build" /workspace/distro/alpine/out
case "${AIOS_IDENTITY_BUILD:-0}" in
  0) ;;
  1) apk add --no-cache qt6-qtwayland-dev ;;
  *) echo 'AIOS_IDENTITY_BUILD must be 0 or 1' >&2; exit 1 ;;
esac
export AIOS_IDENTITY_BUILD="${AIOS_IDENTITY_BUILD:-0}"
id builder >/dev/null 2>&1 || adduser -D -h /build/home builder
addgroup builder abuild
if [ ! -f /build/signing/aios.rsa ]; then
  openssl genrsa -out /build/signing/aios.rsa 4096
  openssl rsa -in /build/signing/aios.rsa -pubout -out /build/signing/aios.rsa.pub
fi
cp /build/signing/aios.rsa.pub /etc/apk/keys/
chown -R builder:builder /build /workspace/distro/alpine/out
chmod 600 /build/signing/aios.rsa
chmod +x /workspace/scripts/*.sh /workspace/distro/alpine/mkimage.sh /workspace/distro/alpine/apkovl/genapkovl-aios.sh
export PACKAGER_PRIVKEY=/build/signing/aios.rsa
export APORTS_DIR=/build/aports OUT_DIR=/workspace/distro/alpine/out WORK_DIR=/build/work
export AIOS_SHELL_BUILD_DIR="$shell_build"
# A Windows worktree's .git pointer is not valid inside this Linux container.
# Build scripts use absolute paths; keep Git's working directory outside it.
cd /build
su builder -s /bin/sh -c 'exec "$@"' -- sh /workspace/scripts/build-iso.sh "$@"
chown -R "$HOST_UID:$HOST_GID" /workspace/distro/alpine/out
