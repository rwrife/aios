#!/bin/sh
# Keep this foreground WSL process alive while WSLg displays the preview.
#
# Modes:
#   (default)        preview the chat window
#   --chat           same as the default
#   --desktop        preview the full desktop shell (orb) without rebuilding
#                    an ISO, so the orb and future shared voice coordinator
#                    can be exercised from the existing preview container.
#
# DRY_RUN=1 prints the selected mode and exits without touching Docker.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
. "$ROOT/distro/alpine/build.env"

MODE="chat"
case "${1:-}" in
  '') ;;
  --chat) ;;
  --desktop) MODE="desktop" ;;
  *)
    echo "usage: $0 [--chat|--desktop]   (DRY_RUN=1 to print the mode only)" >&2
    exit 2
    ;;
esac

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRYRUN mode=$MODE"
  exit 0
fi

SHELL_ARGS="--chat"
[ "$MODE" = "desktop" ] && SHELL_ARGS=""

exec docker run --rm --name "aios-chat-${MODE}-preview" \
  -v "$ROOT:/workspace:ro" -v aios-chat-preview-build:/preview-build \
  -v aios-chat-preview-data:/root/.local/share/aios \
  -v /tmp/.X11-unix:/tmp/.X11-unix:ro -v /mnt/wslg:/mnt/wslg \
  -e DISPLAY="${DISPLAY:-:0}" -e QT_QPA_PLATFORM=xcb -e QT_QUICK_BACKEND=software -e QT_MEDIA_BACKEND=gstreamer \
  -e PULSE_SERVER="${PULSE_SERVER:-unix:/mnt/wslg/PulseServer}" -e PYTHONPATH=/workspace/apps \
  -e AIOS_SHELL_ARGS="$SHELL_ARGS" \
  -w /workspace "${AIOS_PREVIEW_IMAGE:-$IMAGE}" sh -ec '
    # Verify every runtime dependency by name; an existing cmake must never
    # hide missing Qt Multimedia, GStreamer, PulseAudio client, or font
    # packages. Missing names are added, and STT/TTS binaries are reported
    # separately because the container cannot build them.
    needed="build-base cmake ninja qt6-qtbase-dev qt6-qtdeclarative-dev qt6-qtmultimedia-dev qt6-qtmultimedia-gstreamer gst-plugins-good qt6-qtwebengine-dev pulseaudio-utils python3 font-dejavu"
    missing=""
    for package in $needed; do
      apk info -e "$package" >/dev/null 2>&1 || missing="$missing $package"
    done
    if [ -n "$missing" ]; then
      echo "[aios] preview adding missing packages:$missing"
      apk add --no-cache $missing
    fi
    for tool in pactl pw-play; do
      command -v "$tool" >/dev/null 2>&1 || echo "[aios] warning: audio client $tool is unavailable; WSLg audio checks will be limited"
    done
    for tool in whisper-cli espeak-ng; do
      command -v "$tool" >/dev/null 2>&1 || echo "[aios] note: $tool is not bundled in the preview container; local voice is unavailable in preview only"
    done
    cmake -S apps/shell -B /preview-build -G Ninja -DAIOS_EMBEDDED_DISPLAY=OFF -DAIOS_DISPLAY_TESTS=OFF >/tmp/build.log 2>&1
    cmake --build /preview-build --target aios-shell aios-browser aios-app-host aios-camera >>/tmp/build.log 2>&1
    export AIOS_APP_HOST=/preview-build/aios-app-host
    export AIOS_CAPTURE_LIBRARY=/preview-build/libaios-camera.so
    # shellcheck disable=SC2086
    exec /preview-build/aios-shell $AIOS_SHELL_ARGS
  '
