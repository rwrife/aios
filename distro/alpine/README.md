# Alpine-based ISO build (Phase 1)

This directory contains the initial implementation for AI OS Phase 1:
- VM-first live ISO build pipeline (QEMU/VMware)
- autologin + X11 startup path
- locked-down Openbox session launching only xterm

Notes:
- Installer flow is intentionally deferred to Phase 2.
- Splash theme integration is staged after live boot path is stable.

Host prerequisites for native `scripts/build-iso.sh`:
- Alpine-compatible mkimage toolchain (`abuild`, `apk-tools`, `alpine-conf`, `fakeroot`, `xorriso`, `squashfs-tools`, `mtools`, `syslinux`, `grub`)
- `git`

Portable build option (recommended for non-Alpine hosts):
- `scripts/build-iso-container.sh`
- Requires Docker (or Podman via `RUNTIME=podman`)
- Works on ARM64 hosts; default target ARCH follows host arch (`aarch64` on ARM64, `x86_64` on x86_64)
- Override target with `ARCH=aarch64` or `ARCH=x86_64`
- Container platform defaults are automatic (`x86_64 -> linux/amd64`, `aarch64 -> linux/arm64`); override with `CONTAINER_PLATFORM=...` if needed
