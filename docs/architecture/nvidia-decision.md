# NVIDIA graphics: architecture and decision record

Status: decided for this baseline (Alpine v3.23 on musl, using the kernel and
Mesa packages the v3.23 repositories serve). Scope: desktop OpenGL/GLX
acceleration only. This record does not certify any GPU; see
`docs/qa/hardware-coverage.json` for the untested Nouveau entries this
decision permits testing toward.

## What is actually pinned

`distro/alpine/build.env` pins the build container image digest and the
aports, llama.cpp, and whisper.cpp revisions. It does **not** pin the kernel
or Mesa package versions: `v3.23/main` and `v3.23/community` are moving
release repositories. What a given build resolved is recorded afterwards, per
ISO, in `<iso>.build-manifest.json`: the effective repository URLs, the
SHA-256 of each architecture-specific `APKINDEX.tar.gz`, and the exact
embedded APK closure with sizes and SHA-256s. `docs/qa/hardware-coverage.json`
additionally records the `linux-lts` and `mesa` versions *declared by the
pinned aports tree*, which is a source fact rather than proof of what any
build installed. Any NVIDIA claim below must therefore be read against the
recorded closure of the image that was tested, not against an assumed
version.

## Context

AIOS's desktop is X11 + Openbox + Picom using GLX (see
`apks/world.x11` and `docs/plans/physical-hardware-enablement.md`), not a
Wayland/Vulkan-first compositor. The image is built on musl libc with the
kernel and Mesa packages from the Alpine v3.23 release repositories, and no
glibc compatibility layer. Any NVIDIA graphics decision has to be evaluated
against that actual stack, not against generic upstream capability claims for
a newer/glibc distribution.

## Decision

1. **Nouveau + Mesa OpenGL/GLX is the only in-scope NVIDIA acceleration path
   for this baseline.** It uses the same in-tree kernel driver and Mesa
   userspace already required for Intel/AMD, needs no additional packaging
   or ABI bridge, and matches AIOS's existing GLX/X11 desktop.
2. **NVK (Mesa's Vulkan driver for NVIDIA hardware via Nouveau) is tracked
   separately and is not part of this stage's acceleration claim.** AIOS's
   desktop does not currently render through Vulkan; NVK capability on a
   given GPU generation does not by itself imply a working GLX/OpenGL
   desktop on the Mesa version actually packaged in v3.23. A future Vulkan
   compositor path may reconsider NVK, but that is a separate decision.
3. **The NVIDIA proprietary driver and the NVIDIA open-gpu-kernel-modules
   stack are out of scope for this baseline release.** Reasons, all specific
   to this stack rather than generic:
   - NVIDIA's userspace libraries (`libnvidia-glcore`, etc.) are built and
     validated against glibc; musl is not a supported target, and matching
     an open kernel module to a musl-linked, license-compliant userspace is
     unresolved upstream work, not a packaging detail.
   - Redistribution: NVIDIA's userspace components are proprietary-licensed
     and are not present in Alpine's `main`/`community` repositories; adding
     them would mean shipping or fetching binary blobs AIOS does not control
     the update cadence or licensing terms for.
   - Maintenance: the open kernel modules must track NVIDIA's GSP firmware
     and release cadence independently of Alpine's kernel/Mesa release
     cadence, which is a second, uncoordinated upgrade treadmill on top of
     the one release branch selected in `distro/alpine/build.env`.
4. **No vendor installer or compatibility shim is added by this or any
   future stage under this decision.** A first-boot `.run` installer, a
   bundled proprietary blob, or an unmaintained shim would silently change
   what "the image" means per machine, and would not appear in the recorded
   package closure every build writes (`docs/qa/hardware-coverage.md`).
5. **NVIDIA support claims are narrowed to tested Nouveau generations.**
   Presence of the `nouveau` kernel module or a `linux-firmware-nvidia`
   package is not evidence of anything; only entries in
   `docs/qa/hardware-coverage.json` that pass the phase-5 certification
   gates in `docs/plans/physical-hardware-enablement.md` and carry a real
   `evidence_date` may be described as working.
6. **Broader NVIDIA vendor-stack coverage requires a separately approved
   glibc/platform migration.** This record does not authorize or schedule
   that migration; it only states that vendor-stack support is blocked on
   it. A future proposal to move to a glibc-compatible base image is a
   distinct architectural decision with its own tradeoffs (toolchain, image
   size, musl-specific fixes accumulated elsewhere in this repo) that must
   be evaluated on its own, not smuggled in as an NVIDIA driver fix.

## Feasibility summary

| Path | ABI/libc fit on this baseline | Desktop path it serves | Status here |
| --- | --- | --- | --- |
| Nouveau + Mesa (OpenGL/GLX) | Native musl, no bridge needed | AIOS's current X11/GLX desktop | In scope; tested generations tracked in `hardware-coverage.json` |
| NVK (Mesa Vulkan via Nouveau) | Native musl, no bridge needed | A future Vulkan-based compositor, not the current desktop | Tracked separately; not claimed for this stage |
| NVIDIA proprietary / open-gpu-kernel-modules userspace | Requires glibc-linked vendor userspace; no musl support | Any desktop, plus CUDA (already out of scope) | Out of scope for this baseline; requires a separately approved platform migration |

## Evidence fields required before any NVIDIA entry can move past `untested`

Matching `hardware-coverage.schema.json`, plus the NVIDIA-specific evidence
called out in `docs/plans/physical-hardware-enablement.md`:

- Exact PCI ID and GPU codename/generation (e.g. `10de:1f82`, TU117/Turing).
- Loaded kernel module and any firmware files actually requested
  (`nouveau`, and on GSP-firmware generations the `nvidia/<chip>/gsp/*.bin`
  files, if the packaged nouveau/firmware combination requires them). Note
  that the v3.23 `linux-firmware-nvidia` package has no `nvidia/tu117/`
  directory, so a TU117 test must record what the driver actually requested
  rather than assuming a path.
- Mesa version and the specific Nouveau Gallium driver in use (`nvXX`/`nv50`/
  `nvc0` family as applicable) from the `mesa-dri-gallium` package recorded
  in that image's `<iso>.build-manifest.json` closure, not an upstream/newer
  Mesa's capabilities.
- The actual GLX renderer string reported by the desktop (`glxinfo`/
  `mesa-utils`, already in `apks/world.x11`).
- Suspend/resume result (Nouveau's power management maturity varies sharply
  by generation).
- Test date and the ISO's recorded build manifest, so the manifest's
  `evidence_date` is tied to a specific recorded kernel/Mesa closure rather
  than an undated claim.

## Non-goals

CUDA, an NVIDIA-compute-equivalent ROCm path, hardware video encode/decode
acceleration, and Secure Boot signing of any GPU firmware or module remain
out of scope, consistent with the existing plan's exclusions. This record
does not change boot configuration or add any package; it only sets the
criteria and boundary for future NVIDIA-related work under this repository.
