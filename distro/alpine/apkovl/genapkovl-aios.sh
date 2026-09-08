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
cp -a "$AIOS_STAGE_DIR"/. "$tmpdir"/
mkdir -p "$tmpdir/etc/apk"
cat "$AIOS_WORLD_BASE" "$AIOS_WORLD_X11" "$AIOS_WORLD_VM" "$AIOS_WORLD_DEVEL" "$AIOS_WORLD_AI" | sort -u > "$tmpdir/etc/apk/world"
printf '%s\n' $AIOS_REPOSITORIES > "$tmpdir/etc/apk/repositories"
printf 'aios\n' > "$tmpdir/etc/hostname"
rc_add() {
  mkdir -p "$tmpdir/etc/runlevels/$2"
  ln -sf "/etc/init.d/$1" "$tmpdir/etc/runlevels/$2/$1"
}
for svc in devfs dmesg udev udev-trigger hwdrivers modloop; do rc_add "$svc" sysinit; done
for svc in modules sysctl hostname bootmisc syslog networking; do rc_add "$svc" boot; done
for svc in dbus elogind dhcpcd aios-init; do rc_add "$svc" default; done
for svc in mount-ro killprocs savecache; do rc_add "$svc" shutdown; done
# Windows bind mounts do not preserve Unix permission bits. Normalize them so
# configuration (especially doas rules) is never shipped world-writable.
find "$tmpdir" -type d -exec chmod 755 {} +
find "$tmpdir" -type f -exec chmod 644 {} +
chmod +x "$tmpdir"/usr/local/bin/* "$tmpdir"/etc/init.d/aios-init
chmod +x "$tmpdir"/usr/local/sbin/*
chmod +x "$tmpdir/etc/X11/xinit/xinitrc"

# mkimage expects <hostname>.apkovl.tar.gz to appear in DESTDIR.
# This script is executed inside DESTDIR by mkimage.
tar --numeric-owner --owner=0 --group=0 -C "$tmpdir" -czf "${host}.apkovl.tar.gz" .
