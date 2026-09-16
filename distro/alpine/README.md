# Alpine-based ISO build

This directory contains the AIOS live and installable image:
- hybrid x86_64 ISO for BIOS, UEFI, optical media and USB drives
- autologin + X11 startup path
- guarded whole-disk installer

Host prerequisites for native `scripts/build-iso.sh`:
- Alpine-compatible mkimage toolchain (`abuild`, `apk-tools`, `alpine-conf`, `fakeroot`, `xorriso`, `squashfs-tools`, `mtools`, `syslinux`, `grub`)
- `git`
- `python3` (used for the staged apps and for `record-build-manifest.py`)

Every build writes recorded evidence next to each ISO:
- `build-inputs.env` -- the effective source selections for that build,
  including the moving Alpine branch selector, immutable source pins, and any
  environment override
- `<iso>.build-manifest.json` -- effective repository URLs, a post-build
  SHA-256 sample of each architecture-specific `APKINDEX.tar.gz`, the exact
  embedded APK closure (filename, size, SHA-256), the ISO hash,
  kernel/initramfs/modloop identity, the package atoms every `apks/world.*`
  file requested, the license/provenance record for the selected hardware
  packages, and size metrics, written by `record-build-manifest.py`
- `<iso>.packages.txt` and `SHA256SUMS`

Package lists live in `apks/world.*` and are aggregated by
`profiles/mkimg.aios.sh` (ISO closure) and `apkovl/genapkovl-aios.sh`
(`/etc/apk/world`, which is both the live and the installed world):
`world.base`, `world.x11`, `world.vm` (virtual hardware),
`world.hardware` (offline firmware, regulatory database and bounded hardware
diagnostics -- see [`docs/qa/hardware-offline-bundle.md`](../../docs/qa/hardware-offline-bundle.md)),
`world.devel`, `world.ai`, and the optional `world.identity`. Changing any of
them changes the profile package list and the apkovl content hash, so a cached
ISO is never reused across a package change.

Alpine's release repositories keep moving. The APK closure is the authoritative
record of what shipped; the index samples are diagnostic and do not prove the
indexes stayed unchanged throughout the build. This is not a pin or a
byte-reproducibility claim. See
`docs/qa/hardware-coverage.md` for how `scripts/inspect-image.py` consumes it.

Portable build option (recommended for non-Alpine hosts):
- `scripts/build-iso-container.sh`
- Requires Docker (or Podman via `RUNTIME=podman`)
- Builds the validated `x86_64` target through a pinned `linux/amd64` container

Release builds run through `.github/workflows/build-iso.yml`. The workflow checks
the generated checksum and package/build manifests, confirms BIOS/UEFI hybrid
boot metadata, and completes offline QEMU boots with both firmware types.
