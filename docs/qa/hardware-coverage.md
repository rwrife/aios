# Hardware coverage manifest

This is the stage-1 hardware baseline deliverable for
[issue #98](https://github.com/rwrife/aios/issues/98): a structured
device/driver/firmware matrix, kept separate from prose so status can be
machine-checked. It does not certify any hardware; see
[Acceptance intent](#acceptance-intent) below and
[docs/plans/physical-hardware-enablement.md](../plans/physical-hardware-enablement.md)
for the full multi-phase plan this stage belongs to.

- Data: [`hardware-coverage.json`](hardware-coverage.json)
- Schema: [`docs/schemas/hardware-coverage.schema.json`](../schemas/hardware-coverage.schema.json)
- Inspection tool that produced/cross-checks the underlying artifact evidence:
  [`scripts/inspect-image.py`](../../scripts/inspect-image.py)
- Build-side recorder for repository/closure evidence:
  [`distro/alpine/record-build-manifest.py`](../../distro/alpine/record-build-manifest.py),
  invoked by [`distro/alpine/mkimage.sh`](../../distro/alpine/mkimage.sh)
- Stage 2, which bundles the firmware/driver/userspace closure for these
  entries offline and validates it:
  [`hardware-offline-bundle.md`](hardware-offline-bundle.md), with the selected
  package set in [`hardware-packages.json`](hardware-packages.json)

Two module/firmware names in this matrix were corrected by stage 2 against the
v3.23 file index: the MediaTek core module is `mt76` (not `mt76_core`), and
`sof-firmware` installs `intel/sof/sof-cnl.ri` (not `intel/sof-cnl.ri`).
Correcting a demonstrably wrong mapping is not a support claim; both entries
remain `untested`.

## Status values

Only four states are allowed, and only `untested`/`verified`/`degraded`/
`unsupported` mean what they say:

| Status | Meaning |
| --- | --- |
| `untested` | No physical-hardware evidence has been collected. This is the default and the only status any entry may hold in this stage. |
| `verified` | A representative physical machine passed the applicable phase-5 certification gates in the physical-hardware-enablement plan, with a recorded `evidence_date`. |
| `degraded` | Hardware boots/works but with a known limitation (e.g. software rendering only, reduced Wi-Fi band support). |
| `unsupported` | Evidence shows the device does not work on this baseline (e.g. missing firmware in the packaged Alpine release, kernel too old). |

**Package, module, or firmware presence in the image is never sufficient by
itself to mark an entry anything other than `untested`.** Every entry in
`hardware-coverage.json` is `untested` today; this PR does not run or claim
any physical-device test.

## Fields

Each entry follows `hardware-coverage.schema.json`: `id`, `family`, `vendor`,
`device`, `bus`, `ids` (`pci_id`/`usb_id`/`subsystem_id` plus
`subsystem_id_status`), `kernel_module`, `module_dependencies`,
`firmware_files`, `firmware_requirements`, `firmware_package`,
`firmware_license`,
`firmware_provenance`, `minimum_kernel`, `minimum_mesa`,
`representative_machine`, `status`, `evidence_date`, `evidence_sources`,
`supporting_subsystems`, and `notes`.

`firmware_requirements` is the field validation uses. It is a list of required
groups (`{"id": ..., "any_of": [...]}`): every group must be satisfied, and the
patterns inside one group are alternatives for the same artifact (an alternate
packaged layout or a version wildcard). Artifacts that are needed together --
an ath10k firmware image and its board file, a SOF DSP image and its topology,
MediaTek RAM code and its MCU patch -- are therefore separate groups, so one of
them going missing fails the entry. `firmware_files` stays as the flattened
list of the same patterns for readers that only want the paths.

The schema enforces the evidence contract with conditionals rather than prose:

- `status: untested` requires `evidence_date: null`.
- Any other status requires a real `evidence_date` **and** a non-empty
  `evidence_sources` list.
- `ids` must always carry all three identifier keys, even when they are null.
- `subsystem_id_status` distinguishes `not_applicable` (class-bound or
  core-stack entries that have no single subsystem ID) from
  `pending_inventory` (a concrete device is targeted, but no physical unit
  has been inventoried yet). A null subsystem ID therefore means something
  specific instead of "unknown".
- `minimum_kernel`/`minimum_mesa` are structured records
  (`status`, `version`, `reason`, `evidence_source`, `meets_baseline`)
  rather than free text, and only `status: required` may carry a version.
- `representative_machine` names a *candidate* class or model with an
  `availability` flag. `unconfirmed` means nobody has confirmed the hardware
  is on hand; naming a model is a shopping/test target, not a claim of
  ownership or of a test having been run.
- `firmware_license` and `firmware_provenance` record where a firmware blob
  comes from (Alpine repository, declaring APKBUILD, upstream license
  reference) or explicitly mark the path as `not_applicable`/`unresolved`.

`baseline_versions` records the package versions declared by the aports tree
pinned in `distro/alpine/build.env` (`linux-lts`, `mesa`, `linux-firmware`,
`sof-firmware`, `wireless-regdb`). Those are *source* facts: Alpine's release
repositories keep moving, so the versions a given build actually installed are
recorded per ISO in `<iso>.build-manifest.json`, not here.

The manifest's top-level `alpine_branch` must match `ALPINE_BRANCH` in
[`distro/alpine/build.env`](../../distro/alpine/build.env). If that pin ever
moves, every version, firmware-package, and provenance claim here needs
re-review before the branch bump is treated as a routine dependency update.

## Coverage in this stage

Families represented: Intel `iwlwifi`/`iwlmvm` (+ retained `iwldvm`),
Qualcomm/Atheros `ath9k`/`ath10k_pci`/`ath11k_pci` (+ conditional `ath12k`),
MediaTek `mt76` family, Intel `i915`/`xe`, AMD `amdgpu`, selected NVIDIA
Nouveau generations, plus storage/controller, USB, HID/I2C input, Ethernet,
ACPI, audio/SOF, cfg80211/mac80211, and DRM/KMS. See
[`docs/architecture/nvidia-decision.md`](../architecture/nvidia-decision.md)
for why NVIDIA entries are Nouveau-only in this stage.

This stage implements the inventory and matrix mechanics only. Each entry
names a candidate representative machine with `availability: unconfirmed`;
no physical candidate has been sourced, inventoried, or tested, and every
entry is therefore `untested`.

## CPU baseline (AVX2 floor)

The current CPU inference build targets x86_64 with AVX2 (already documented
in README.md). This manifest does not add a per-CPU entry because
AVX2 is a build-time compiler target, not a discoverable PCI/USB device;
recording it here would duplicate the README rather than adding evidence.
The candidate resolution paths for pre-AVX2 hardware, recorded but **not**
implemented by this stage, are:

1. Detect the missing instruction set at boot/first run and show an
   actionable message instead of a crash (lowest engineering cost, still
   excludes older CPUs from local inference).
2. Ship a runtime-dispatched or separately built baseline (SSE4.2) llama.cpp
   path for CPUs without AVX2, at the cost of extra build/QA matrix and
   image size.

This decision is deferred to the phase-3 desktop-usability work in
`docs/plans/physical-hardware-enablement.md` and is not resolved here.

## Reproducing the underlying evidence fields

`scripts/inspect-image.py` reads a built image's actual artifacts (APK
closure and checksums, kernel module directory versions and relevant
`.config` flags, initramfs contents, modloop modules and the complete
firmware inventory, module aliases matched against this manifest's device
IDs, requested Mesa/Xorg packages, bootloader entries, and enabled
services) and reports each section as `available` or an explicit
`unavailable` with a reason. It is the mechanism future updates to this
manifest should cite as the evidence source for module/firmware/package
fields; it does not itself assign `verified`/`degraded`/`unsupported` status,
which requires an actual physical machine.

```sh
python3 scripts/inspect-image.py --root <extracted-iso-dir> \
  --modloop-root <unsquashfs-extracted-modloop-dir> \
  --build-inputs <out-dir>/build-inputs.env \
  --repo-build-env distro/alpine/build.env \
  --build-manifest <out-dir>/<iso>.build-manifest.json \
  --coverage-manifest docs/qa/hardware-coverage.json
```

`--coverage-manifest` defaults to this directory's `hardware-coverage.json`.
Every concrete `pci_id`/`usb_id` is converted into a modalias probe
(vendor/device fixed, PCI subsystem/class and USB version/interface
wildcarded) and matched case-insensitively against the image's
`modules.alias`. The report lists expected modules, matched aliases and
modules, unmatched IDs, and ambiguity (an ID resolving to more than one
module, or to a module this manifest does not list). A match proves the
shipped modules claim the ID; it is not evidence the device works.

Firmware is read from the real Alpine modloop layout
(`<modloop-root>/modules/firmware`, with `lib/firmware` still accepted for
rootfs-style trees) and the **complete** file inventory is reported with
symlink targets and compression; `highlights` is only a convenience subset.

See `tests/test_inspect_image.py` for fixture-driven examples of every
section, including the explicit-unavailable behavior when an artifact is
missing.

## What a build records, and what it does not pin

`distro/alpine/mkimage.sh` calls `distro/alpine/record-build-manifest.py`
after every build and writes, next to each ISO:

- `build-inputs.env` -- the effective source selections for that build,
  including the moving `ALPINE_BRANCH` selector and immutable container/git
  pins, with any environment override recorded rather than a blind copy of
  `build.env`.
- `<iso>.build-manifest.json` -- the recorded evidence, split into three
  deliberately separate kinds:
  1. `source_pins`: immutable content-addressed/revision-pinned inputs plus
     the explicitly identified moving `ALPINE_BRANCH` selection, with any
     override recorded.
  2. `repository_indexes`: the effective `REPO_MAIN`/`REPO_COMMUNITY` URLs
     (flagged `default` or `override`) and a post-build SHA-256 sample of each
     architecture-specific `APKINDEX.tar.gz`.
  3. `output_closure`: every `.apk` embedded in the ISO with filename, size,
     and SHA-256, plus the ISO hash and the kernel/initramfs/modloop
     identity (including modloop kernel-version directories when
     `unsquashfs` is available).
- `<iso>.packages.txt` and `SHA256SUMS`, as before.

`scripts/inspect-image.py --build-manifest` re-reads that manifest, keeps the
three kinds separate in its `recorded_inputs` section, re-hashes the image's
APKs against the recorded closure, flags repository overrides and URL drift
against the current `build.env`, and -- with `--compare-build-manifest` --
diffs APKINDEX digests and the package closure between two builds.

### Remaining nondeterminism

This is **not** a byte-reproducibility claim, and the manifest says so
(`reproducibility.byte_reproducible: false`,
`package_resolution: "output-closure-recorded-repository-index-sampled"`).
Known remaining sources of
nondeterminism: build timestamps, generated SSH host keys (`--hostkeys`), the
apkovl/squashfs/ISO layout and compression metadata, the modloop signing key,
locally built AIOS application binaries, and -- most importantly -- the moving
Alpine release repositories themselves. Immutable snapshot URLs for
`v3.23/main` and `v3.23/community` are not available upstream, so rebuilding
the same closure would require a local mirror of the recorded APKs.

## Acceptance intent

- No hardware in this manifest is `verified` based on package, module, or
  firmware presence alone.
- Unknown or not-yet-tested hardware remains `untested` rather than being
  inferred from what the build happens to include.
- Named representative machines are candidates, not inventory. This stage
  delivers the inventory/matrix mechanics; no physical candidate has been
  sourced or tested.
- Moving Alpine `v3.23` repository state is **made visible, not
  predetermined.** Alpine does not publish immutable snapshot URLs for a
  release branch, so nothing here freezes which package versions
  `v3.23/main` and `v3.23/community` serve. What a release used is instead
  recorded: `distro/alpine/build.env` pins the container digest and the
  aports/llama.cpp/whisper.cpp revisions, and every build records the
  effective repository URLs, post-build `APKINDEX.tar.gz` SHA-256 samples,
  and the exact embedded APK closure (filename, size, SHA-256) in
  `<iso>.build-manifest.json`. The closure is authoritative for what shipped;
  the index samples are diagnostic and do not prove the indexes stayed
  unchanged while packages were resolved. A later difference in what the repositories
  serve therefore shows up as a reportable diff -- through
  `scripts/inspect-image.py`'s `recorded_inputs` section, and directly
  between two manifests with `--compare-build-manifest` -- instead of being
  an invisible change.
- Byte-for-byte reproducibility is explicitly **not** claimed; see
  [Remaining nondeterminism](#remaining-nondeterminism).
