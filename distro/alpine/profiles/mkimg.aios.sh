# Upstream caches apkovl by generator script alone. Include staged binaries and
# overlay content so a frontend/config change cannot silently reuse an old ISO.
aios_apkovl_section() {
	[ -n "$apkovl" ] && [ -n "$hostname" ] || return 0
	local content_hash
	content_hash=$({ find "$AIOS_OVERLAY_DIR" "$AIOS_STAGE_DIR" -type f -exec sha256sum {} \;;
		cat "$AIOS_WORLD_BASE" "$AIOS_WORLD_X11" "$AIOS_WORLD_VM" "$AIOS_WORLD_DEVEL" "$AIOS_WORLD_AI" "$AIOS_WORLD_HARDWARE";
		[ -z "${AIOS_WORLD_IDENTITY:-}" ] || cat "$AIOS_WORLD_IDENTITY";
	} | sort | checksum)
	build_section apkovl "$hostname" "$(checksum < "$apkovl")" "$content_hash"
}

# Boot entries shared by both bootloader generators: "<label>|<extra args>".
# "live" stays first so it remains the default on both bootloaders. "recovery"
# is selectable without editing kernel arguments and is never the default; it
# adds nomodeset as a diagnostic graphics fallback plus the aios.recovery flag
# that /usr/local/bin/aios-session reads to force a software/XRender desktop.
# Serial and VGA console arguments come from $kernel_cmdline for every entry.
aios_boot_entries() {
	printf '%s\n' 'live|' 'install|aios.install' 'recovery|aios.recovery nomodeset'
}

aios_syslinux_config() {
	printf 'SERIAL 0 115200\nTIMEOUT 30\nPROMPT 1\nDEFAULT live\n'
	printf 'SAY AIOS boot entries: live (default), install, recovery\n'
	printf 'SAY recovery = conservative graphics (nomodeset); serial console stays enabled\n'
	aios_boot_entries | while IFS='|' read -r entry extra; do
		printf '\nLABEL %s\n KERNEL /boot/vmlinuz-lts\n INITRD /boot/initramfs-lts\n APPEND %s %s %s\n' "$entry" "$initfs_cmdline" "$kernel_cmdline" "$extra"
	done
}

aios_grub_config() {
	printf 'set timeout=3\nset default=0\n'
	aios_boot_entries | while IFS='|' read -r entry extra; do
		title="AIOS $entry"
		[ "$entry" != recovery ] || title="AIOS recovery (safe graphics)"
		printf 'menuentry "%s" {\n linux /boot/vmlinuz-lts %s %s %s\n initrd /boot/initramfs-lts\n}\n' "$title" "$initfs_cmdline" "$kernel_cmdline" "$extra"
	done
}

profile_aios() {
	profile_standard
	# NetworkManager manages this desktop. Upstream network-extras pulls in
	# legacy vlan scripts that conflict with Alpine's ifupdown-ng package.
	apks="$(printf '%s\n' "$apks" | tr '[:space:]' '\n' | sed '/^network-extras$/d' | tr '\n' ' ')"
	section_apkovl() { aios_apkovl_section; }
	syslinux_gen_config() { aios_syslinux_config; }
	grub_gen_config() { aios_grub_config; }
	title="AI OS"
	desc="AIOS chat-first desktop and development environment"
	profile_abbrev="aios"
	image_ext="iso"
	output_format="iso"

	# Boot optimization for VM-first targets.
	initfs_features="$initfs_features virtio"
	initfs_cmdline="modules=loop,squashfs,sd-mod,usb-storage,virtio_blk,virtio_scsi,ahci,nvme quiet"
	# Alpine's default tmpfs root is half of RAM. The development packages and
	# bundled model, voice stack, and WebEngine browser need more room in the
	# live VM; this is a ceiling, not an up-front allocation. Installed ext4
	# systems do not use it.
	kernel_cmdline="console=tty0 console=ttyS0,115200 loglevel=4 rootflags=size=75%"
	syslinux_serial="0 115200"

	# Keep signing optional for local/dev builds.
	modloop_sign="${AIOS_MODLOOP_SIGN:-no}"

	if [ -n "$AIOS_WORLD_BASE" ] && [ -f "$AIOS_WORLD_BASE" ]; then
		apks="$apks $(tr '\n' ' ' < "$AIOS_WORLD_BASE")"
	fi
	if [ -n "$AIOS_WORLD_X11" ] && [ -f "$AIOS_WORLD_X11" ]; then
		apks="$apks $(tr '\n' ' ' < "$AIOS_WORLD_X11")"
	fi
	if [ -n "$AIOS_WORLD_VM" ] && [ -f "$AIOS_WORLD_VM" ]; then
		apks="$apks $(tr '\n' ' ' < "$AIOS_WORLD_VM")"
	fi
	# world.hardware documents why each package is selected, so strip comments
	# before they reach the package list.
	if [ -n "$AIOS_WORLD_HARDWARE" ] && [ -f "$AIOS_WORLD_HARDWARE" ]; then
		apks="$apks $(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$AIOS_WORLD_HARDWARE" | tr '\n' ' ')"
	fi

	hostname="aios"
	apks="$apks $(cat "$AIOS_WORLD_DEVEL" "$AIOS_WORLD_AI" | tr '\n' ' ')"
	if [ -n "${AIOS_WORLD_IDENTITY:-}" ]; then
		apks="$apks $(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$AIOS_WORLD_IDENTITY" | tr '\n' ' ')"
	fi
	kernel_addons=
	apkovl="$AIOS_APKOVL_SCRIPT"
}
