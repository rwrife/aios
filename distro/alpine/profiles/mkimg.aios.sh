profile_aios() {
	profile_standard
	title="AI OS"
	desc="AI OS minimal terminal-only live environment"
	profile_abbrev="aios"
	image_ext="iso"
	output_format="iso"

	# Boot optimization for VM-first targets.
	initfs_features="$initfs_features virtio"
	initfs_cmdline="modules=loop,squashfs,sd-mod,usb-storage,virtio_blk,virtio_scsi,ahci,nvme quiet"
	kernel_cmdline="quiet loglevel=3 vt.global_cursor_default=0"

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

	hostname="aios"
	apkovl="$AIOS_APKOVL_SCRIPT"
}
