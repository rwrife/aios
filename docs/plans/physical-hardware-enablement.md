# Physical hardware enablement plan

Status: proposed; documentation only. No hardware is certified by this PR.

## Outcome and scope

Ship an offline-bootable, installable AIOS image for common x86_64 Intel and AMD
PCs, with Intel, Qualcomm/Atheros, and MediaTek Wi-Fi, and Intel, AMD, and NVIDIA
displays. Drivers, firmware, and required userspace must be on the USB image:
connecting to Wi-Fi must never require first downloading its driver.

Qualcomm and MediaTek here identify wireless adapters in PCs. Snapdragon/ARM
laptops and MediaTek ARM systems require a separate aarch64 platform effort
(boot firmware, device trees/ACPI, SoC display, storage, audio, and packaging).
They are not enabled simply by adding Wi-Fi firmware to the x86_64 ISO.

Define support by exact PCI/USB ID, subsystem ID, GPU generation, and tested
machine, not by vendor name. The first release targets a usable accelerated
X11 desktop where verified, with a clearly identified recovery display mode.
CUDA, ROCm, accelerated local inference, video encode/decode, Wi-Fi 7 features,
and Secure Boot certification are separate milestones. Keep CPU inference and
software media paths working. Secure Boot remains disabled until a complete
signed boot chain has been implemented and tested.

## Current repository baseline

- `distro/alpine/build.env` selects Alpine v3.23 and pins the build container and
  aports commit. APK repositories remain moving release repositories: these
  pins alone do not freeze every kernel, Mesa, or firmware package. Each build
  therefore records its effective repository URLs, post-build
  `APKINDEX.tar.gz` digest samples, and exact APK closure in
  `<iso>.build-manifest.json`.
- `distro/alpine/profiles/mkimg.aios.sh` inherits `profile_standard`, boots
  `vmlinuz-lts`, enables virtio, and requests USB storage, AHCI, and NVMe modules.
  Inherited kernel and modloop contents need inspection before declaring any
  driver absent. There is no explicit hardware coverage manifest today.
- `apks/world.base` already includes NetworkManager Wi-Fi and wpa_supplicant.
  `apks/world.x11` includes Mesa Gallium, Xorg modesetting, libinput, and legacy
  fbdev/VESA fallbacks. `world.vm` is included unconditionally.
- `mkimage.sh`, the profile, and `apkovl/genapkovl-aios.sh` separately consume
  package lists; the profile also hashes them for cache invalidation. All must
  agree when a hardware package list is introduced.
- The overlay enables udev, hwdrivers, and modloop. `usr/local/bin/aios-session`
  starts Openbox/Picom and keeps conservative Qt multimedia defaults. Picom
  uses GLX; successful framebuffer output alone does not establish desktop health.
- `usr/local/sbin/aios-install` uses `setup-disk` and GRUB. The installed kernel,
  firmware, initramfs, packages, and boot configuration need independent checks.
- README documents AVX2 CPU inference and says physical hardware is unvalidated.
  Firmware drivers cannot fix an unsupported CPU instruction set.

Paths above are under `distro/alpine/` unless otherwise specified. Implementation
must recheck these assumptions against its own base commit.

## Proposed driver coverage

These are candidate test families, not compatibility promises. Phase 1 must
resolve module aliases and actual firmware requests against the chosen kernel
and release APKs. Include dependency modules and board-specific firmware files.

| Hardware | Kernel path to verify | Firmware/userspace and initial test candidates |
| --- | --- | --- |
| Intel Wi-Fi | `iwlwifi`, `iwlmvm`; `iwldvm` if older hardware is retained | v3.23 `linux-firmware-intel`; start with AX200/AX210 and an OEM AX201/AX211 system. CNVi adapters require a compatible host platform. Gate newer BE-series devices separately. |
| Qualcomm/Atheros Wi-Fi | `ath9k`, `ath10k_pci`, `ath11k_pci`; evaluate `ath12k` for newer devices | Resolve ath10k/ath11k/ath12k firmware subpackages and board data from the release index. Start with QCA6174 and QCA6390/WCN6855 systems; newer Wi-Fi 7 devices remain conditional. |
| MediaTek Wi-Fi | `mt76` family, including `mt7921e`, `mt7921u`, `mt7925e` when supported | Candidate `linux-firmware-mediatek`; start with MT7921 and MT7922, then MT7925 and a supported USB adapter. Match driver to bus and device ID. |
| Intel graphics | `i915` or `xe`, as appropriate to the device and kernel | Verify Intel display firmware package split, Mesa OpenGL/EGL, and Xorg modesetting. Test an established UHD/Iris Xe iGPU and an Arc/newer device separately. Do not force `xe` globally. |
| AMD graphics | `amdgpu`; `radeon` only for a separately selected legacy tier | Candidate `linux-firmware-amdgpu`, Mesa radeonsi and modesetting; test a Ryzen APU and an RDNA discrete GPU. Resolve any legacy firmware separately. |
| NVIDIA graphics | Nouveau plus compatible Mesa for the baseline | Candidate `linux-firmware-nvidia`; test exact supported generations for display, GLX, power management, and resume. Establish accelerated support per card; retain a recovery route where acceleration fails. |

Alpine 3.23 moved Intel wireless firmware into `linux-firmware-intel`; do not
copy an older distribution's package recipe. [Alpine 3.23 release notes](https://wiki.alpinelinux.org/wiki/Release_Notes_for_Alpine_3.23.0)
provide the release-specific change. Upstream family references are
[Intel iwlwifi](https://wireless.docs.kernel.org/en/latest/en/users/drivers/iwlwifi.html),
[Qualcomm ath11k](https://wireless.docs.kernel.org/en/latest/en/users/drivers/ath11k.html),
and [MediaTek](https://wireless.docs.kernel.org/en/latest/en/users/drivers/mediatek.html).
These explain driver families; they do not certify the AIOS image.

### NVIDIA decision gate

Treat the NVIDIA vendor stack as a separate feasibility spike early in the work.
Alpine's musl userspace is a compatibility constraint that must be resolved for
vendor graphics libraries; compiling a kernel module is insufficient. NVIDIA
requires matching kernel modules, GSP firmware, and userspace components in its
[open kernel module documentation](https://github.com/NVIDIA/open-gpu-kernel-modules).
Do not equate open kernel modules with a complete open userspace stack.

First validate Nouveau plus the Mesa version actually shipped in v3.23.
[NVK documentation](https://docs.mesa3d.org/drivers/nvk.html) describes a Vulkan
path whose capabilities depend on kernel/Mesa versions; Vulkan support alone
is not proof that AIOS's OpenGL/GLX desktop works. Do not apply current upstream
capability claims to an older packaged stack without testing.

Produce a decision record with tested GPU IDs, acceleration and suspend results,
libc/ABI feasibility, redistribution requirements, kernel upgrade maintenance,
and image cost. If the baseline cannot meet the selected NVIDIA targets, choose
explicitly between a narrower published hardware list and a separately scoped
glibc-based image/platform migration. Do not silently ship an unmaintained
compatibility shim, download a vendor installer at first boot, or advertise
full NVIDIA support while this decision is unresolved.

## Delivery phases and exit criteria

### 1. Inventory and choose a reproducible baseline

Owner role: image/kernel maintainer. Depends on no implementation changes.

- Inspect the built ISO's APK inventory, modloop, kernel configuration, module
  aliases, firmware symlinks/compression, and initramfs. Trace inherited upstream
  profile behavior. Map each target device to the required module and firmware.
- Record kernel/Mesa/firmware versions, package checksums and repository source;
  retain the exact APK closure or use a controlled repository snapshot so a
  release can be rebuilt. Keep packages on one compatible Alpine release.
- Start with the release LTS kernel. If a selected ID requires a newer kernel or
  Mesa, document evidence and evaluate a coordinated supported release upgrade;
  avoid mixing edge packages into the image or adding ad hoc out-of-tree drivers.
- Select a physical test inventory and classify each candidate as untested,
  verified, degraded, or unsupported. Record CPU features and OEM firmware.
- Run the NVIDIA feasibility spike before promising broad accelerated coverage.

Exit: reviewed device/driver/firmware matrix, baseline package closure, and NVIDIA
path decision. Record gaps rather than treating package presence as support.

Implementation status (stage 1, issue #98): this stage delivers the
inventory/matrix **mechanics**, not hardware evidence.
`scripts/inspect-image.py` performs the read-only artifact inspection
described above (APK closure/checksums, kernel module versions and relevant
`.config` flags, initramfs contents, modloop modules and the complete
firmware inventory from `modules/firmware`, module aliases matched against
the coverage manifest's concrete PCI/USB IDs, requested Mesa/Xorg packages,
bootloader entries, service enablement) and reports each section as
explicitly available or unavailable. `docs/qa/hardware-coverage.json` (schema
in `docs/schemas/hardware-coverage.schema.json`) holds the structured matrix,
with every entry `untested` and every representative machine recorded as an
unconfirmed candidate rather than owned hardware.
`docs/architecture/nvidia-decision.md` records the NVIDIA path decision:
Nouveau-only for this baseline, NVK tracked separately, no vendor
installer/shim, broader vendor-stack coverage gated on a separately approved
glibc/platform migration.

For the "controlled repository snapshot" requirement above: Alpine publishes
no immutable snapshot URL for a release branch, so this stage records rather
than freezes. `distro/alpine/mkimage.sh` calls
`distro/alpine/record-build-manifest.py` to write, next to every ISO, the
effective source selections (including overrides), the effective repository
URLs with a post-build architecture-specific `APKINDEX.tar.gz` SHA-256 sample,
the exact embedded APK closure (filename, size, SHA-256), the ISO hash, and the
kernel/initramfs/modloop identity. That makes moving-repository resolution
visible and diffable after the fact through the authoritative output closure.
The index sample is diagnostic and does not prove the repository stayed
unchanged during the build. This is not byte reproducibility, and the manifest
says so explicitly. Rebuilding an identical closure would still need a local
mirror of the recorded APKs.

No physical machine has been tested and no candidate device has been sourced
or inventoried; the exit criterion above is not yet met and phase 1 continues
with actual hardware evidence.

### 2. Bundle hardware support offline

Owner role: image/kernel maintainer. Depends on phase 1.

- Add `distro/alpine/apks/world.hardware` (proposed) with resolved firmware,
  regulatory database, and diagnostic utilities such as `pciutils`, `usbutils`,
  `iw`, rfkill tooling, and an OpenGL renderer probe. Verify exact APK names.
- Wire it into `mkimage.sh`, profile package aggregation, apkovl installed world,
  and content hashes. Keep VM drivers so one image can serve both environments.
- Inspect kernel configuration for Wi-Fi, cfg80211/mac80211, DRM/KMS, PCIe,
  USB controllers, NVMe/AHCI, HID/I2C input, Ethernet, ACPI, and sound support.
  Include required firmware for ordinary wired Ethernet and audio/SOF too.
- Ensure boot-critical storage/controller modules are in initramfs, and normal
  hardware modules/firmware are available from modloop before device probing.
  If early KMS is enabled, include its firmware in initramfs as well. Handle
  reprobe if firmware becomes available after the initial udev event.
- Ship required firmware licenses/notices and provenance. Measure ISO size,
  modloop size, live RAM usage, and boot time before pruning any firmware.
  Establish a measured minimum-RAM target and keep all advertised devices
  usable without network downloads.

Exit: automated ISO inspection proves module/firmware dependency closure and
cache invalidation; offline BIOS/UEFI VM boot still works. Hardware claims wait
for phase 5.

### 3. Make first boot and graphics predictable

Owner role: desktop/network maintainer. Depends on phase 2.

- Verify NetworkManager has sole intended ownership of Wi-Fi interfaces and its
  wpa_supplicant integration is correct; audit standalone service startup for
  conflicts. Exercise DHCP, DNS, rfkill, country/regulatory handling, WPA2/WPA3,
  reconnect, and credential persistence only where intended on installed disks.
  Distinguish missing firmware, radio blocked, and authentication failure in UI.
- Prefer automatic DRM/KMS and Xorg modesetting detection. Verify GPU permissions,
  Mesa DRI loading, GLX/Picom, Qt Quick, and WebEngine on each target. Never force
  a vendor-specific Xorg configuration across all machines.
- Test hybrid Intel/AMD plus NVIDIA laptops, the default rendering GPU, and
  display connectors wired to a discrete GPU; do not assume iGPU fallback can
  drive every port. GPU offload is an additional tested capability.
- Add an explicit boot-menu recovery entry and documented console access for
  failed KMS/graphics. Verify a usable software-rendered desktop when available;
  `nomodeset` is a diagnostic fallback, not the normal launch configuration.
- Keep existing software media defaults until per-device validation justifies
  changes. Separate desktop acceleration from video and inference acceleration.
- Check brightness, external display hotplug, scaling, lid close, suspend/resume,
  keyboard/touchpad, audio, and Wi-Fi recovery. Preserve AIOS theme and accessible
  controls; validate visual changes with Ocean and a generated palette.
- Resolve the AVX2 floor: either publish and detect it with an actionable boot
  message, or build a tested baseline/dispatch inference path for older CPUs.

Exit: live USB reaches a usable desktop and Wi-Fi setup on representative machines
from all requested vendors, with useful recovery and diagnostic paths.

### 4. Make installation and updates preserve support

Owner role: installer/release maintainer. Depends on phases 2 and 3.

- Verify `aios-install`/`setup-disk` install the complete hardware APK closure
  offline and regenerate an appropriate installed initramfs, module dependency
  data, and GRUB entries. Boot without the USB and without network access.
- Validate USB boot on UEFI and legacy BIOS, NVMe and SATA installation, and
  recovery boot entries. Retain exact erase confirmation and live-media guards;
  use dedicated sacrificial test disks. Assess eMMC explicitly before claiming it.
- Define coherent kernel/firmware/Mesa updates with a retained known-good kernel
  and recovery image, available disk-space checks, and a tested rollback path.
  Revalidate affected physical devices for every baseline update.
- Keep Secure Boot limitations visible. A later Secure Boot project must cover
  trusted bootloaders, kernel/module signing, key lifecycle, firmware enrollment,
  and recovery; modloop signing alone is not Secure Boot support.

Exit: offline installation, reboot, baseline update, and rollback pass on test
hardware without losing display, input, storage, or Wi-Fi support.

### 5. Certify and publish a bounded hardware list

Owner role: QA/release maintainer. Depends on phases 1–4.

Build locally through WSL/container tooling; do not use GitHub to build ISO images
for this effort. Batch image changes and reuse one candidate across tests. Use
`scripts/run.ps1` for QEMU smoke checks, allow sufficient boot time, and give each
VM a unique `-name`. QEMU emulated adapters do not validate real Wi-Fi or GPUs.

| Gate | Required evidence |
| --- | --- |
| Artifact checks | Exact versions/checksums, firmware licenses, module/firmware mapping, initramfs/modloop inspection, offline package closure, changed-list cache test. |
| VM regression | Offline BIOS and UEFI boot; desktop, installer and installed reboot on disposable disks; no-network first boot. |
| Wireless matrix | At least one physical device from each requested vendor; separate evidence for each additional advertised driver family/bus. Scan, WPA2/WPA3 on suitable APs, applicable bands, DHCP/DNS, 30-minute transfer, rfkill, reconnect, and five suspend/resume cycles. Test 6 GHz only where legal and supported. |
| Graphics matrix | Intel iGPU, AMD APU/discrete, selected NVIDIA generations, and one hybrid laptop. Record active module and GL renderer; verify native resolution, GLX compositor, Qt windows, hotplug, video fallback, 30-minute desktop use, and five suspend/resume cycles. Software rendering is recorded as degraded, not accelerated. |
| Browser regression | Real Alpine image with Chromium sandbox enabled: page loading, text input, navigation, scrolling, process cleanup, and two concurrent isolated chat sessions. |
| System/installer | USB boot, NVMe/SATA install, USB removed reboot, input, audio, wired fallback, brightness, shutdown/restart, update and rollback. At least three cold boots per certified machine. |
| Visual and diagnostics | Ocean and generated-palette screenshots; sanitized device report with ISO hash, machine/OEM firmware, PCI/USB IDs, module, firmware, kernel/Mesa, renderer, boot time, RAM, and results. Remove passwords, tokens, SSIDs, MAC addresses, and serial numbers before sharing. |

Publish results in `docs/qa/hardware-compatibility.md` (proposed), including known
failures, workarounds, support tier, and test date. Update README's requirements
and download/install guidance only when evidence exists. A failed device remains
untested/degraded/unsupported; coverage of another card from its vendor does not
promote it. No advertised baseline device may require internet to obtain its
own Wi-Fi/display driver, and no unresolved boot, data-loss, or recovery blocker
may remain on certified hardware.

## Suggested implementation PRs

1. Baseline inventory, exact device matrix, and NVIDIA feasibility decision.
2. Hardware package manifest, firmware/module closure checks, and boot integration.
3. Network ownership, hardware diagnostics, graphics selection, and recovery mode.
4. Offline installer parity and coordinated update/rollback support.
5. Physical test evidence, compatibility list, minimum requirements, release docs.

The current PR delivers only this plan. Implementation PRs should attach their
phase exit evidence; physical hardware access is a prerequisite for certification.
