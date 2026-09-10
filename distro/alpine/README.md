# Alpine-based ISO build

This directory contains the AIOS live and installable image:
- hybrid x86_64 ISO for BIOS, UEFI, optical media and USB drives
- autologin + X11 startup path
- guarded whole-disk installer

Host prerequisites for native `scripts/build-iso.sh`:
- Alpine-compatible mkimage toolchain (`abuild`, `apk-tools`, `alpine-conf`, `fakeroot`, `xorriso`, `squashfs-tools`, `mtools`, `syslinux`, `grub`)
- `git`

Portable build option (recommended for non-Alpine hosts):
- `scripts/build-iso-container.sh`
- Requires Docker (or Podman via `RUNTIME=podman`)
- Builds the validated `x86_64` target through a pinned `linux/amd64` container

Release builds run through `.github/workflows/build-iso.yml`. The workflow checks
the generated checksum and package/build manifests, confirms BIOS/UEFI hybrid
boot metadata, and completes offline QEMU boots with both firmware types.
