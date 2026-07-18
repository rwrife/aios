#!/bin/sh
set -eu

host="${1:-aios}"
: "${AIOS_OVERLAY_DIR:?AIOS_OVERLAY_DIR not set}"

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT INT TERM

cp -a "$AIOS_OVERLAY_DIR"/. "$tmpdir"/

# mkimage expects <hostname>.apkovl.tar.gz to appear in DESTDIR.
# This script is executed inside DESTDIR by mkimage.
tar --numeric-owner --owner=0 --group=0 -C "$tmpdir" -czf "${host}.apkovl.tar.gz" .
