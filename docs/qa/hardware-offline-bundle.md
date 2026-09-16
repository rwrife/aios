# Stage 2: offline hardware bundle

Stage 2 (issue #97) bundles the driver, firmware, regulatory and diagnostic
support that the devices in `docs/qa/hardware-coverage.json` need, so a first
boot and Wi-Fi setup never has to fetch anything, and adds artifact validation
that fails when the package list, overlay world, firmware, module closure or
cache inputs drift apart.

**Nothing in this stage is hardware evidence.** Every device in the coverage
matrix is still `untested`. Package, module and firmware presence is a claim
about the image, not about a machine. Physical status only changes in stage 5.

## What ships

`distro/alpine/apks/world.hardware` holds the selection. Each package name was
resolved against the real Alpine v3.23 `x86_64` `APKINDEX.tar.gz`; the resolved
repository, version, license, covered coverage-manifest entries and rationale
are recorded in `docs/qa/hardware-packages.json`, together with the digests of
the index samples used to resolve them.

| Role | Packages | Why |
| --- | --- | --- |
| Kernel | `linux-lts` | Makes the same release kernel installable from the ISO repository when `setup-disk` runs with no network |
| Firmware | `linux-firmware-intel`, `linux-firmware-i915`, `linux-firmware-amdgpu`, `linux-firmware-nvidia`, `linux-firmware-ath10k`, `linux-firmware-ath11k`, `linux-firmware-ath12k`, `linux-firmware-mediatek`, `linux-firmware-rtl_nic` | The firmware every `firmware_package` value in the coverage matrix names |
| Regulatory | `wireless-regdb` | `regulatory.db` is loaded by cfg80211 through the firmware loader |
| Audio | `sof-firmware`, `alsa-ucm-conf` | SOF DSP images/topologies, plus the use-case configuration SOF/HDA machines need for a usable default device |
| Diagnostics | `pciutils`, `usbutils`, `iw`, `util-linux-misc`, `mesa-utils` | `lspci`, `lsusb`, `iw`, `rfkill`, `glxinfo` |

Notes on the two non-obvious names, both verified against the v3.23 file index:

- Alpine v3.23 has **no standalone `rfkill` package**. `/usr/sbin/rfkill` is in
  `util-linux-misc`.
- `glxinfo` is in `mesa-utils`, which `apks/world.x11` already requests. It is
  listed here as well so the hardware diagnostic set is self-contained; the
  overlay world is sorted and deduplicated.

`linux-firmware-nvidia` is redistributable firmware for the **in-tree nouveau**
driver. No proprietary NVIDIA driver, out-of-tree module, or edge package is
added; see `docs/architecture/nvidia-decision.md`.

`apks/world.vm` is untouched, still exported, still aggregated and still
recorded per build, so one ISO keeps working in QEMU and on physical machines.

## How it reaches the image

- `distro/alpine/mkimage.sh` exports `AIOS_WORLD_HARDWARE` and passes every
  world file plus `docs/qa/hardware-packages.json` to
  `record-build-manifest.py`.
- `distro/alpine/profiles/mkimg.aios.sh` appends the atoms to `apks`, so
  upstream `section_apks` (`apk fetch --simulate --recursive $apks`) changes
  whenever the selection changes, and adds the file to the apkovl content hash,
  so a package change can never reuse a cached ISO.
- `distro/alpine/apkovl/genapkovl-aios.sh` appends the atoms to
  `/etc/apk/world` in the overlay.

That one overlay world covers both systems. Alpine's initramfs `init` runs
`apk add --root $sysroot --initramfs-diskless-boot ... $(cat etc/apk/world)`
against the ISO's `/apks` boot repository, so the **live** root installs the
same set offline; `aios-install` runs `setup-disk -m sys`, which installs the
same world onto the disk. `scripts/inspect-image.py` compares the overlay world
against `world.hardware` once and that covers both.

Comment lines in `world.hardware` are stripped by the same `sed` expression in
the profile and the overlay generator, exactly as `world.identity` is handled.

## Boot-time behavior: no reprobe logic was added

This was investigated before changing anything, and no change was needed.

- `modloop`'s init script (Alpine `openrc` package, `modloop.initd`) declares
  `before checkfs fsck hwdrivers modules hwclock dev sysfs`, and `hwdrivers`
  declares `after modloop`. OpenRC orders sysinit by those dependencies, not by
  the order services were added to the runlevel, so the modloop is mounted
  before any coldplug pass. The `rc_add` order in `genapkovl-aios.sh` was
  already fine and is unchanged.
- Firmware for the selected devices is installed into the root filesystem by
  the initramfs `apk add`, before `switch_root`. It is therefore present before
  udev, `udev-trigger` and `hwdrivers` run at all.
- `hwdrivers` already runs its coldplug `modprobe` pass twice by design.

So there is no window where a selected driver probes before its firmware
exists, and no bounded-retry or unload/reload service was added. A reprobe
service would be redundant boot behavior with no evidence behind it.

One consequence worth recording: because the live root now really installs
firmware packages, `/lib/firmware` is a populated package-owned directory, so
`modloop.initd`'s fallback (`rmdir /lib/firmware && ln -s
/lib/modules/firmware /lib/`, which only succeeds on an empty directory) no
longer applies. The live system's firmware is the selected set rather than the
modloop's pruned copy of `linux-firmware`. For the selected devices that is
strictly better: see the AX210/AX201 finding below.

## Initramfs: unchanged, on evidence

The initramfs of the stage-1 ISO (`alpine-aios-20260914-x86_64.iso`, kernel
`6.18.52-0-lts`) was parsed directly. It already contains every boot-critical
storage and controller module for an ordinary x86_64 machine: `ahci`,
`libahci`, `nvme`, `nvme-core`, `sd_mod`, `usb-storage`, `uas`, `xhci-hcd`,
`xhci-pci`, `ehci-hcd`, `ehci-pci`, `megaraid_sas`, `mpt3sas`, `squashfs`,
`loop`, `ext4` and the virtio set, plus zstd-compressed storage-controller
firmware. Nothing was added, no early-KMS module or firmware was added, and
`initfs_features`/`initfs_cmdline` are unchanged. `inspect-image.py` now
asserts this rather than assuming it: every module of a coverage entry in the
`storage` or `usb` family must be in the initramfs.

## Current merge hold

PR #126 is not release-approved. The exact-output check now enforces the
inventory's filename and SHA-256 comparison at the validation exit boundary,
including non-hardware APKs and duplicate-name rejection. It reuses the existing
hash scan rather than re-reading every APK for the additional check. Inventory
mode without `--validate-hardware` still reports problems but exits zero.
A matching digest binds bytes to this manifest; it is not package signature or
repository authenticity verification.

Two independent release-gate gaps remain: `firmware_license_provenance` checks
that attribution fields exist but does not bind them to shipped APK metadata
and repository evidence, and `offline_package_availability` checks top-level
filenames but does not solve APK dependencies/provides. Neither check proves
an offline installation will succeed. The extractor's restrictive-directory
cleanup finding also remains unresolved. These gaps and fresh local ISO and
BIOS/UEFI boot evidence must be addressed before merge; historical image runs
do not verify the repaired validator. Physical hardware remains untested.

## Validation

```sh
python3 scripts/inspect-image.py \
  --root <extracted-iso> \
  --modloop-root <unsquashfs-ed modloop> \
  --build-manifest <iso>.build-manifest.json \
  --validate-hardware
```

Exit codes are deterministic: `0` all checks passed, `3` a check failed, `4`
required evidence was missing. Missing evidence never reads as a pass. The
report's `hardware_bundle` section carries every check:

| Check | Fails when |
| --- | --- |
| `exact_output_closure` | any embedded APK filename or SHA-256 digest differs from the recorded build closure, or either side contains duplicate filenames; missing recorded closure or APK directory is incomplete |
| `hardware_world_parity` | the overlay `/etc/apk/world` (live **and** installed world) misses a `world.hardware` package |
| `virtual_hardware_world_preserved` | the build recorded no `world.vm` |
| `offline_package_availability` | a selected package has no `.apk` in the ISO's `/apks` repository |
| `firmware_license_provenance` | a coverage-selected firmware package is missing from `world.hardware`, or has no recorded license **and** repository |
| `firmware_files_present` | a required firmware group of a selected device has no match inside the selected packages (compressed `.zst`/`.xz` variants count) |
| `firmware_symlink_targets` | a selected device's firmware symlink points at something that does not ship |
| `module_dependency_closure` | a selected module, or one of its `modules.dep` dependencies, is neither a shipped `.ko` nor in `modules.builtin`, or a shipped selected module has no `modules.dep` entry at all |
| `boot_critical_modules_in_initramfs` | a storage/USB controller module is missing from the initramfs |
| `device_alias_mapping` | a concrete PCI/USB ID matches no shipped module alias, or matches only modules the coverage manifest does not expect for it |

With `--validate-hardware` the tool also reads the member lists of the selected
embedded `.apk` files (metadata only, nothing is extracted), so firmware
presence is checked against what the live and installed systems actually
install.

Three deliberate scoping rules, all documented in the report:

- A firmware **requirement group** is satisfied by any one of its alternative
  paths, because a manifest entry records alternate packaged layouts for the
  same artifact. Co-required artifacts (an ath10k firmware image and its board
  file, a SOF DSP image and its topology, MediaTek RAM code and its MCU patch)
  are separate groups, so a half-present firmware set fails.
- Only the selected packages' own contents satisfy a requirement. Because
  `world.hardware` populates `/lib/firmware` on the live and the installed
  system, the modloop's pruned copy of `linux-firmware` is not what those
  systems load; modloop matches are still reported per requirement
  (`in_modloop`, `modloop_only_matches`) as diagnostic evidence.
- Only a *selected* device's firmware symlinks can fail. `linux-firmware`
  subpackages link into sibling subpackages, and Alpine's modloop carries a
  pruned copy of `linux-firmware`, so unrelated dangling links are reported in
  `unresolved_unselected_symlinks` instead of failing an image that does not
  need them.

`scripts/verify-iso.sh` runs this for every released ISO. Besides the checksum,
boot-metadata and embedded-closure gates it already had, it extracts `/apks`,
`/boot` and the apkovl into a temporary directory, `unsquashfs`-es the modloop
and runs the command above with the repository's coverage manifest, package
manifest and `world.hardware`, writing `<iso>.hardware-validation.json` next to
the ISO. Exit `3` and exit `4` both fail the build; the extraction directory is
removed either way. It needs `xorriso`, `squashfs-tools` and `python3`.

## Local evidence from this branch

Run against the real stage-1 ISO artifacts (extracted `aios.apkovl.tar.gz`,
`boot/initramfs-lts`, `unsquashfs`-ed `boot/modloop-lts`, real build manifest),
the validation **fails** exactly where stage 2 is needed, which is the point of
the check:

- `hardware_world_parity`: 16 of 17 packages absent from the overlay world.
- `offline_package_availability`: 13 packages absent from the 722-package
  closure (`iw`, `mesa-utils`, `util-linux-misc` and `alsa-ucm-conf` were
  already pulled in transitively).
- `firmware_files_present`: AX210 (`iwlwifi-ty-a0-gf-a0-*.ucode`), AX201
  (`iwlwifi-QuZ-a0-hr-b0-*.ucode`) and the SOF images are **not** in the
  modloop. Alpine's `update-kernel` prunes modloop firmware, and the modloop
  carried only 31 `iwlwifi-*` files, all for older adapters. Without stage 2
  those advertised adapters have no firmware at all. Since firmware coverage is
  decided from the selected packages, every requirement group fails when the
  packages are not in the closure, whatever the modloop happens to carry.

Re-run against the same modloop plus an overlay world generated by the real
`genapkovl-aios.sh` and the 17 real v3.23 `.apk` files, all nine checks pass
and the tool exits `0`. Measured in that run: 336,480,806 bytes of additional
packages (17 packages, 366,631,070 bytes installed), 4,321 compressed firmware
files visible across packages and modloop, 51 of 51 selected modules present
(with `drm` and `drm_kms_helper` satisfied by `modules.builtin`), and all
concrete device IDs matched.

## Corrections this stage made to stage-1 data

Both were demonstrably wrong and are corrected in
`docs/qa/hardware-coverage.json`, verified against the v3.23 file index:

- `mt76_core` -> `mt76`. The in-tree MediaTek core module is `mt76.ko`;
  `mt76_core` does not exist in `linux-lts` 6.18.52.
- `intel/sof-cnl.ri` -> `intel/sof/sof-cnl.ri`. `sof-firmware` installs its DSP
  images under `intel/sof/`. The topology path `intel/sof-tplg/...` was already
  correct.

Two inspector fixes came from the same evidence, and both are validated by
tests:

- Modules compiled into the kernel (`modules.builtin`) count as present. `drm`
  and `drm_kms_helper` are built in, not modules.
- GPU-family probes use PCI base class `03`. `nouveau` registers only a
  vendor+class alias (`pci:v000010DEd*sv*sd*bc03sc*i*`), which a
  class-wildcarded probe can never match in either direction. A display
  controller's base class is a fact, not a guess.

## Metrics

`record-build-manifest.py` writes a `metrics` section per ISO: ISO, modloop,
initramfs, kernel, embedded APK total (with package count) and apkovl size.
The apkovl is measured from the ISO listing rather than extracted, so builds
are not slowed by copying it (verified against the stage-1 ISO: 683,933,493
bytes).

The stage-1 ISO was 2,097,152,000 bytes with a 1,068,364,208-byte closure of
722 packages. This stage adds 336,480,806 bytes of packages, so the ISO will
grow by roughly that much. The actual figure is deliberately left to the next
build's `metrics` section rather than guessed here.

Live root usage, boot time and minimum tested RAM are recorded as
`not_measured` with a reason. They need a booted machine; no value is invented,
and no firmware pruning decision is taken without them.

## Not done here

Wi-Fi association UX, GPU selection policy, Secure Boot, kernel upgrades, the
full offline BIOS/UEFI QEMU regression, and any physical device certification.
Those belong to later stages. Status for every device stays `untested`.
