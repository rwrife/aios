#!/bin/sh
set -eu
apk add --no-cache abuild apk-tools alpine-conf busybox fakeroot xorriso squashfs-tools \
  mtools grub grub-efi syslinux git bash coreutils tar findutils build-base cmake ninja \
  qt6-qtbase-dev qt6-qtdeclarative-dev curl-dev linux-headers python3 font-dejavu
mkdir -p /build/home /build/signing /build/work /workspace/distro/alpine/out
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
su builder -s /bin/sh -c 'exec "$@"' -- sh /workspace/scripts/build-iso.sh "$@"
chown -R "$HOST_UID:$HOST_GID" /workspace/distro/alpine/out
