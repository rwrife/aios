#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/distro/alpine/build.env"
# Private cgroup/mount namespaces; repository is read-only. Privilege is required
# to exercise real namespaces, tmpfs, cgroups and a disposable LUKS image.
docker run --rm --privileged --cgroupns=private \
  -e AIOS_DISPOSABLE_TEST_CONTAINER=1 \
  -v "$ROOT:/workspace:ro" -w /workspace "$IMAGE" sh -ec '
    apk add --no-cache python3 py3-pip tzdata bubblewrap cryptsetup e2fsprogs >/dev/null
    grep "^cronsim==" apps/requirements.txt > /tmp/scheduled-requirements.txt
    python3 -m pip install --break-system-packages --require-hashes --no-deps \
      -r /tmp/scheduled-requirements.txt >/dev/null
    mkdir -p /usr/local/share/aios
    cp -r apps/aios /usr/local/share/aios/
    mkdir /tmp/aios-cgroup
    mount -t cgroup2 none /tmp/aios-cgroup
    mount --bind /tmp/aios-cgroup /sys/fs/cgroup
    export PYTHONPATH=/workspace/apps:/workspace/tests PYTHONDONTWRITEBYTECODE=1
    if [ "$#" -eq 0 ]; then set -- linux_identity_isolation; fi
    exec python3 -m unittest "$@" -v
  ' sh "$@"
