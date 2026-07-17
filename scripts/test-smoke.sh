#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

echo "[smoke] checking required scripts"
for f in \
  "$ROOT_DIR/distro/alpine/mkimage.sh" \
  "$ROOT_DIR/distro/alpine/profiles/mkimg.aios.sh" \
  "$ROOT_DIR/distro/alpine/apkovl/genapkovl-aios.sh" \
  "$ROOT_DIR/distro/alpine/overlay/etc/inittab" \
  "$ROOT_DIR/distro/alpine/overlay/etc/X11/xinit/xinitrc" \
  "$ROOT_DIR/distro/alpine/overlay/etc/xdg/openbox/rc.xml" \
  "$ROOT_DIR/distro/alpine/overlay/etc/xdg/openbox/menu.xml" \
  "$ROOT_DIR/scripts/build-iso-container.sh"; do
  [ -f "$f" ] || { echo "missing: $f"; exit 1; }
done

echo "[smoke] basic file presence checks passed"
