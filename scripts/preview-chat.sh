#!/bin/sh
# Keep this foreground WSL process alive while WSLg displays the preview.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/distro/alpine/build.env"
exec docker run --rm --name aios-chat-windowed \
  -v "$ROOT:/workspace:ro" -v aios-chat-preview-build:/preview-build \
  -v aios-chat-preview-data:/root/.local/share/aios \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro -v /mnt/wslg:/mnt/wslg \
  -e DISPLAY="${DISPLAY:-:0}" -e QT_QPA_PLATFORM=xcb -e QT_QUICK_BACKEND=software -e QT_MEDIA_BACKEND=gstreamer \
  -e PULSE_SERVER=unix:/mnt/wslg/PulseServer -e PYTHONPATH=/workspace/apps \
  -w /workspace "${AIOS_PREVIEW_IMAGE:-$IMAGE}" sh -ec '
    if ! command -v cmake >/dev/null; then
      apk add --no-cache build-base cmake ninja qt6-qtbase-dev qt6-qtdeclarative-dev qt6-qtmultimedia-dev qt6-qtmultimedia-gstreamer gst-plugins-good qt6-qtwebengine-dev python3 font-dejavu
    fi
    cmake -S apps/shell -B /preview-build -G Ninja -DAIOS_EMBEDDED_DISPLAY=OFF -DAIOS_DISPLAY_TESTS=OFF >/tmp/build.log 2>&1
    cmake --build /preview-build --target aios-shell aios-browser aios-app-host aios-camera >>/tmp/build.log 2>&1
    export AIOS_APP_HOST=/preview-build/aios-app-host
    export AIOS_CAPTURE_LIBRARY=/preview-build/libaios-camera.so
    exec /preview-build/aios-shell --chat
  '
