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
# world.hardware is the offline firmware/regulatory/diagnostic set. The live
# root installs this world from the ISO's /apks repository during initramfs,
# and setup-disk reuses the same file, so both worlds stay identical.
sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$AIOS_WORLD_HARDWARE" >> "$tmpdir/etc/apk/world"
sort -u "$tmpdir/etc/apk/world" -o "$tmpdir/etc/apk/world"
if [ -n "${AIOS_WORLD_IDENTITY:-}" ]; then
  sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$AIOS_WORLD_IDENTITY" >> "$tmpdir/etc/apk/world"
  sort -u "$tmpdir/etc/apk/world" -o "$tmpdir/etc/apk/world"
fi
printf '%s\n' $AIOS_REPOSITORIES > "$tmpdir/etc/apk/repositories"
printf 'aios\n' > "$tmpdir/etc/hostname"
# Live/installed marker. Everything that must know whether this system can
# persist state (Wi-Fi regulatory country, hardware diagnostics, the chat's
# live badge) reads /etc/aios-mode. The live apkovl ships `live`; aios-install
# overwrites it with `installed` on the target root.
printf 'live\n' > "$tmpdir/etc/aios-mode"
rc_add() {
  mkdir -p "$tmpdir/etc/runlevels/$2"
  ln -sf "/etc/init.d/$1" "$tmpdir/etc/runlevels/$2/$1"
}
for svc in devfs dmesg udev udev-trigger hwdrivers modloop; do rc_add "$svc" sysinit; done
for svc in modules sysctl hostname bootmisc syslog networking; do rc_add "$svc" boot; done
# NetworkManager is the sole intended owner of physical Wi-Fi interfaces.
# /etc/NetworkManager/NetworkManager.conf selects the wpa_supplicant backend,
# and the wpa_supplicant package ships
# /usr/share/dbus-1/system-services/fi.w1.wpa_supplicant1.service, so NM starts
# and stops the supplicant itself over the system bus. Alpine's wpa_supplicant
# OpenRC service is a second, independent owner: its start_pre binds
# -i<first wireless interface> before NetworkManager starts, and it fails with
# "Could not find a wireless interface" on machines that have no radio. It is
# therefore deliberately not in a runlevel. The packages stay installed so the
# NM backend and a manual recovery-console supplicant remain available.
for svc in dbus elogind polkit networkmanager aios-init; do rc_add "$svc" default; done
for svc in mount-ro killprocs savecache; do rc_add "$svc" shutdown; done
# Windows bind mounts do not preserve Unix permission bits. Normalize them so
# configuration (especially doas rules) is never shipped world-writable.
find "$tmpdir" -type d -exec chmod 755 {} +
find "$tmpdir" -type f -exec chmod 644 {} +
chmod +x "$tmpdir"/usr/local/bin/* "$tmpdir"/etc/init.d/aios-init "$tmpdir"/etc/init.d/aios-sessiond
chmod +x "$tmpdir"/usr/local/sbin/*
chmod +x "$tmpdir/etc/X11/xinit/xinitrc"

# mkimage expects <hostname>.apkovl.tar.gz to appear in DESTDIR.
# This script is executed inside DESTDIR by mkimage.
tar --numeric-owner --owner=0 --group=0 -C "$tmpdir" -czf "${host}.apkovl.tar.gz" .
