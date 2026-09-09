#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/distro/alpine/build.env"
# Private cgroup/mount namespaces; repository is read-only. Privilege is required
# to exercise real namespaces, tmpfs, cgroups and a disposable LUKS image.
docker run --rm --privileged --cgroupns=private \
  -e AIOS_DISPOSABLE_TEST_CONTAINER=1 \
  -v "$ROOT:/workspace:ro" -w /workspace "$IMAGE" sh -ec '
    apk add --no-cache python3 bubblewrap cryptsetup e2fsprogs >/dev/null
    mkdir /tmp/aios-cgroup
    mount -t cgroup2 none /tmp/aios-cgroup
    mount --bind /tmp/aios-cgroup /sys/fs/cgroup
    export PYTHONPATH=/workspace/apps PYTHONDONTWRITEBYTECODE=1
    exec python3 tests/linux_identity_isolation.py
  '
