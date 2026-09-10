#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/distro/alpine/build.env"
docker run --rm --privileged --cgroupns=private \
  -e AIOS_DISPOSABLE_TEST_CONTAINER=1 \
  -v "$ROOT:/workspace:ro" -w /workspace "${AIOS_DISPLAY_TEST_IMAGE:-$IMAGE}" sh -ec '
    apk add --no-cache build-base cmake ninja pkgconf qt6-qtbase-dev qt6-qtdeclarative-dev \
      qt6-qtmultimedia-dev qt6-qtmultimedia-gstreamer qt6-qtwebengine-dev qt6-qtwayland-dev python3 py3-cryptography bubblewrap \
      cryptsetup e2fsprogs wayland-utils font-dejavu py3-pillow >/dev/null
    cmake -S apps/shell -B /tmp/display-shell -G Ninja -DAIOS_EMBEDDED_DISPLAY=ON -DAIOS_DISPLAY_TESTS=ON -DAIOS_PROFILE_TESTS=ON >/dev/null
    cmake --build /tmp/display-shell
    # Exercise the installed layout without an inherited Python module path.
    mkdir -p /usr/local/share/aios
    cp -r apps/aios /usr/local/share/aios/
    env -u PYTHONPATH -u AIOS_PYTHONPATH /tmp/display-shell/aios-profile-test
    env PYTHONPATH=/nonexistent AIOS_PYTHONPATH=/workspace/apps /tmp/display-shell/aios-profile-test
    c++ tests/wayland_surface.cpp -o /usr/bin/gnome-calculator $(pkg-config --cflags --libs Qt6Gui)
    QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software QT_MEDIA_BACKEND=gstreamer \
      /usr/lib/qt6/bin/qmltestrunner -input tests/qml
    mkdir /tmp/aios-cgroup
    mount -t cgroup2 none /tmp/aios-cgroup
    mount --bind /tmp/aios-cgroup /sys/fs/cgroup
    export PYTHONPATH=/workspace/apps PYTHONDONTWRITEBYTECODE=1
    exec python3 tests/linux_identity_display.py /tmp/display-shell/aios-shell
  '
