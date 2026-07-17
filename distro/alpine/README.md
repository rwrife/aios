# Alpine-based ISO build (Phase 1)

This directory contains the initial implementation for AI OS Phase 1:
- VM-first live ISO build pipeline (QEMU/VMware)
- autologin + X11 startup path
- locked-down Openbox session launching only xterm

Notes:
- Installer flow is intentionally deferred to Phase 2.
- Splash theme integration is staged after live boot path is stable.

Host prerequisites for `scripts/build-iso.sh`:
- Alpine-compatible mkimage toolchain (`abuild`, `apk-tools`, `alpine-conf`, `fakeroot`, `xorriso`, `squashfs-tools`, `mtools`, `syslinux`, `grub`)
- `git`

If building from non-Alpine hosts, use an Alpine container/VM for the build step.
