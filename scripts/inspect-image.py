#!/usr/bin/env python3
"""Read-only inventory of an AIOS/Alpine image build's artifacts.

This is stage-1 hardware baseline tooling (see distro/alpine and
docs/qa/hardware-coverage.json). It reports what is actually baked into a
built image -- APK closure and checksums, kernel/module versions, initramfs
contents, modloop modules/firmware, module aliases, Mesa/DRM userspace
package presence, bootloader entries, and enabled services -- from
artifacts given on the command line. It never boots the image, loads a
kernel module, or contacts a network repository.

Every section is either {"status": "available", ...data} or
{"status": "unavailable", "reason": "..."}. Missing inputs are reported as
unavailable, never fabricated or inferred from silence. Package or module
presence is not a hardware support claim: see docs/qa/hardware-coverage.json
for the separate untested/verified/degraded/unsupported matrix.

Because distro/alpine/profiles/mkimg.aios.sh calls upstream profile_standard
before layering AIOS-specific settings, an option missing from that script
does not mean the built image lacks it. This tool reads the generated
artifacts that actually ship (bootloader append lines, the apkovl world and
service runlevels, the real APK closure under /apks) so inherited
profile_standard behavior is reflected without re-deriving it from source.

Usage:
    python3 scripts/inspect-image.py --root <extracted-iso-dir> [options]

`--root` is a directory containing the top-level contents of a built ISO
(for example the output of `xorriso -osirrox on -indev aios.iso -extract / <dir>`).
Modules/firmware normally ship inside the modloop squashfs; pass
`--modloop-root <dir>` pointing at an already-`unsquashfs -d`-extracted
directory (this tool never invokes unsquashfs itself, to stay dependency-free
and deterministic for fixtures/tests). Alpine's `update-kernel` builds that
squashfs with modules under `<modloop-root>/modules/<version>` and firmware
under `<modloop-root>/modules/firmware`; the older `lib/modules`/`lib/firmware`
layout is still accepted when present.

Reproducibility evidence comes from the per-ISO manifest written by
`distro/alpine/record-build-manifest.py` (`--build-manifest`). Three distinct
kinds of evidence are reported separately and never conflated: immutable
source pins plus the moving Alpine branch selection, post-build repository
APKINDEX samples, and the exact output closure. The closure is authoritative
for what shipped. Index samples help diagnose repository movement but do not
prove which index state apk observed during the build.

The `hardware_bundle` section validates the stage-2 offline bundle: that the
overlay world requests every `apks/world.hardware` package, that each of those
packages ships inside the ISO's `/apks` repository, that every redistributed
firmware package has a recorded license and repository, that every firmware
requirement group the coverage manifest records is satisfied by the selected
packages' own contents (compressed variants and symlink targets included; the
modloop's pruned copy is reported as evidence but never satisfies a
requirement, because `world.hardware` populates `/lib/firmware` on the live and
the installed system), that every selected non-builtin module has a
`modules.dep` entry and its dependencies ship, that boot-critical modules are
in the initramfs, and that every device ID maps to a module the manifest
expects for it. With `--validate-hardware` the tool additionally reads the
member lists of the selected embedded `.apk` files and exits 3 on a failed
check or 4 when required evidence was missing. Passing validation still never
promotes a device out of `untested`.
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import json
import re
import sys
import tarfile
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 3

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COVERAGE_MANIFEST = REPO_ROOT / "docs" / "qa" / "hardware-coverage.json"
DEFAULT_HARDWARE_PACKAGE_MANIFEST = REPO_ROOT / "docs" / "qa" / "hardware-packages.json"
DEFAULT_HARDWARE_WORLD = REPO_ROOT / "distro" / "alpine" / "apks" / "world.hardware"
CANONICAL_REPO_BASE = "https://dl-cdn.alpinelinux.org/alpine"

# Alpine compresses most firmware in linux-firmware; the kernel loads
# "foo.bin" from "foo.bin.zst". sof-firmware ships uncompressed, so a firmware
# path in the coverage manifest may legitimately appear either way.
FIRMWARE_COMPRESSION_SUFFIXES = (".zst", ".xz")

# PCI base classes that follow from a coverage family by definition. A display
# controller is always base class 0x03, and nouveau only registers a
# vendor+class alias, so a class-wildcarded probe would never match it.
PCI_BASE_CLASS_BY_FAMILY = {"gpu": "03"}

# Coverage families whose modules must be in the initramfs, not only the
# modloop: the kernel needs them to reach the boot media itself.
BOOT_CRITICAL_FAMILIES = ("storage", "usb")

# Deterministic exit codes for --validate-hardware.
EXIT_VALIDATION_OK = 0
EXIT_VALIDATION_FAILED = 3
EXIT_VALIDATION_INCOMPLETE = 4

# Firmware path prefixes surfaced in the "highlights" list. The complete
# inventory is always reported in "files"; highlights are only a convenience.
FIRMWARE_HIGHLIGHT_PREFIXES = (
    "iwlwifi", "intel/iwlwifi/", "ath9k_htc/", "ath10k/", "ath11k/", "ath12k/",
    "mediatek/", "mt7", "amdgpu/", "i915/", "xe/", "nouveau/", "nvidia/",
    "rtl_nic/", "intel/sof", "regulatory.db",
)

# Kernel .config options relevant to the stage-1 hardware matrix families.
RELEVANT_KERNEL_CONFIG = [
    "CONFIG_IWLWIFI", "CONFIG_IWLMVM", "CONFIG_IWLDVM",
    "CONFIG_ATH9K", "CONFIG_ATH10K", "CONFIG_ATH10K_PCI",
    "CONFIG_ATH11K", "CONFIG_ATH11K_PCI", "CONFIG_ATH12K",
    "CONFIG_MT76_CORE", "CONFIG_MT7921E", "CONFIG_MT7921U", "CONFIG_MT7925E",
    "CONFIG_DRM_I915", "CONFIG_DRM_XE", "CONFIG_DRM_AMDGPU", "CONFIG_DRM_NOUVEAU",
    "CONFIG_DRM", "CONFIG_DRM_KMS_HELPER",
    "CONFIG_NVME_CORE", "CONFIG_SATA_AHCI", "CONFIG_USB_STORAGE",
    "CONFIG_VIRTIO_BLK", "CONFIG_VIRTIO_SCSI", "CONFIG_USB_XHCI_HCD",
    "CONFIG_I2C_HID", "CONFIG_HID_GENERIC", "CONFIG_HID_MULTITOUCH",
    "CONFIG_E1000E", "CONFIG_R8169", "CONFIG_VIRTIO_NET",
    "CONFIG_SND_HDA_INTEL", "CONFIG_SND_SOC_SOF_TOPLEVEL",
    "CONFIG_CFG80211", "CONFIG_MAC80211",
    "CONFIG_ACPI",
]


def unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def available(**data) -> dict:
    return {"status": "available", **data}


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _align4(n: int) -> int:
    return (n + 3) & ~3


def parse_cpio_newc(data: bytes) -> list[dict]:
    """Parse a "newc"/"crc" format cpio archive (as produced by mkinitfs).

    Returns a list of {"path", "size", "mode"} in archive order, stopping at
    the TRAILER!!! end marker. Raises ValueError on any other cpio format or
    truncated/corrupt input, rather than guessing.
    """
    entries = []
    offset = 0
    length = len(data)
    while offset < length:
        if offset + 110 > length:
            raise ValueError(f"truncated cpio header at offset {offset}")
        magic = data[offset:offset + 6]
        if magic not in (b"070701", b"070702"):
            raise ValueError(f"unsupported cpio magic {magic!r} at offset {offset}")
        fields = data[offset + 6:offset + 110]

        def field(index: int) -> int:
            return int(fields[index * 8:(index + 1) * 8], 16)

        mode = field(1)
        filesize = field(6)
        namesize = field(11)
        header_end = offset + 110
        name_end = header_end + namesize
        if name_end > length:
            raise ValueError(f"truncated cpio filename at offset {offset}")
        name = data[header_end:name_end - 1].decode("utf-8", "replace")
        data_start = _align4(name_end)
        data_end = data_start + filesize
        if data_end > length:
            raise ValueError(f"truncated cpio file data for {name!r}")
        if name == "TRAILER!!!":
            break
        entries.append({"path": name, "size": filesize, "mode": oct(mode)})
        offset = _align4(data_end)
    return entries


def parse_modules_alias(text: str) -> list[dict]:
    """Parse a depmod-generated modules.alias file into alias/module pairs."""
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) != 3 or parts[0] != "alias":
            continue
        _, pattern, module = parts
        entries.append({"alias": pattern, "module": module})
    return entries


def module_key(module: str) -> str:
    """Normalize a module name for comparison (depmod uses underscores)."""
    return module.strip().replace("-", "_").lower()


def pci_modalias_probe(pci_id: str, base_class: str = "*") -> str:
    """Build a modalias probe string for a concrete `vendor:device` PCI ID.

    A real PCI modalias is
    `pci:v<VENDOR>d<DEVICE>sv<SUBVENDOR>sd<SUBDEVICE>bc<CLASS>sc<SUBCLASS>i<IFACE>`.
    The coverage manifest records vendor/device only, so subsystem and class
    fields stay wildcards; matching is done in both directions (see
    `alias_matches_probe`) so a subsystem- or class-specific alias is not
    silently missed.

    `base_class` is supplied for families whose PCI base class is a fact rather
    than a guess (a GPU is always base class 0x03). Some drivers -- nouveau is
    the example in this matrix -- only register a vendor+class alias, which a
    fully class-wildcarded probe can never match in either direction.
    """
    vendor, _, device = pci_id.partition(":")
    return f"pci:v0000{vendor.upper()}d0000{device.upper()}sv*sd*bc{base_class}sc*i*"


def usb_modalias_probe(usb_id: str) -> str:
    """Build a modalias probe string for a concrete `vendor:product` USB ID.

    A real USB modalias is
    `usb:v<VENDOR>p<PRODUCT>d<BCD>dc<CLASS>dsc<SUBCLASS>dp<PROTO>ic<ICLASS>isc<ISUBCLASS>ip<IPROTO>in<INUM>`.
    Device version and the device/interface class triplets stay wildcards.
    """
    vendor, _, product = usb_id.partition(":")
    return f"usb:v{vendor.upper()}p{product.upper()}d*dc*dsc*dp*ic*isc*ip*in*"


def alias_matches_probe(alias: str, probe: str) -> bool:
    """Case-insensitive glob match between a modules.alias pattern and a probe.

    Both sides may contain wildcards: the alias wildcards the fields a driver
    does not care about, and the probe wildcards the fields the coverage
    manifest does not record. Matching in both directions keeps a
    subsystem-specific alias (`...sv00001028sd...`) matchable against a
    vendor/device-only probe without inventing subsystem IDs.
    """
    alias_l = alias.lower()
    probe_l = probe.lower()
    return fnmatch.fnmatchcase(probe_l, alias_l) or fnmatch.fnmatchcase(alias_l, probe_l)


def load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as error:
        return None, f"{type(error).__name__}: {error}"


def coverage_expectations(manifest: dict) -> dict:
    """Derive the relevant modules and concrete device IDs from the matrix.

    Nothing here is hard-coded: the modules that matter are exactly the ones
    the coverage manifest names, so adding an entry there automatically
    widens what this tool validates.
    """
    modules: set[str] = set()
    devices = []
    for entry in manifest.get("entries", []):
        entry_modules = [module_key(m) for m in entry.get("kernel_module", [])]
        modules.update(entry_modules)
        modules.update(module_key(m) for m in entry.get("module_dependencies", []))
        ids = entry.get("ids") or {}
        base_class = PCI_BASE_CLASS_BY_FAMILY.get(entry.get("family"), "*")
        for bus, key, probe_builder in (
            ("pci", "pci_id", lambda value: pci_modalias_probe(value, base_class)),
            ("usb", "usb_id", usb_modalias_probe),
        ):
            value = ids.get(key)
            if not value:
                continue
            devices.append({
                "id": entry.get("id"),
                "bus": bus,
                "device_id": value,
                "probe": probe_builder(value),
                "expected_modules": entry_modules,
            })
    return {"modules": modules, "devices": sorted(devices, key=lambda d: (d["id"] or "", d["bus"]))}


def parse_env_file(text: str) -> dict:
    """Parse simple KEY=VALUE lines as used by distro/alpine/build.env."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def parse_kernel_config(text: str, relevant_prefixes: tuple[str, ...] = ("CONFIG_",)) -> dict:
    """Parse a kernel .config file (boot/config-<version>) into name -> value.

    Handles both "CONFIG_FOO=y" and the commented-out "# CONFIG_FOO is not
    set" form (recorded as "n"), matching how the kernel build itself
    documents disabled options.
    """
    values = {}
    not_set = re.compile(r"^#\s*(CONFIG_\w+)\s+is not set\s*$")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = not_set.match(line)
        if match:
            values[match.group(1)] = "n"
            continue
        if line.startswith("#"):
            continue
        if "=" not in line or not line.startswith(relevant_prefixes):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def parse_syslinux_cfg(text: str) -> list[dict]:
    """Parse a SYSLINUX config into LABEL entries with kernel/initrd/append."""
    entries = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("LABEL "):
            current = {"label": line[len("LABEL "):].strip()}
            entries.append(current)
        elif current is not None and upper.startswith("KERNEL "):
            current["kernel"] = line[len("KERNEL "):].strip()
        elif current is not None and upper.startswith("INITRD "):
            current["initrd"] = line[len("INITRD "):].strip()
        elif current is not None and upper.startswith("APPEND "):
            current["append"] = line[len("APPEND "):].strip()
    return entries


def parse_grub_cfg(text: str) -> list[dict]:
    """Parse a GRUB config into menuentry blocks with linux/initrd lines."""
    entries = []
    current = None
    depth = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        match = re.match(r'menuentry\s+"([^"]*)"', line)
        if match and depth == 0:
            current = {"title": match.group(1)}
            entries.append(current)
            depth = line.count("{") - line.count("}")
            continue
        if current is None:
            continue
        depth += line.count("{") - line.count("}")
        if line.startswith("linux "):
            current["linux"] = line[len("linux "):].strip()
        elif line.startswith("initrd "):
            current["initrd"] = line[len("initrd "):].strip()
        if depth <= 0:
            current = None
            depth = 0
    return entries


def read_apkovl(path: Path) -> dict:
    """Read an AIOS apkovl tarball (see distro/alpine/apkovl/genapkovl-aios.sh).

    Returns requested world packages, configured repositories, and the
    service-to-runlevel enablement map exactly as staged onto the image;
    this is the actual profile_standard-inherited service/package set, not
    a re-derivation from the profile script source.
    """
    world: list[str] = []
    repositories: list[str] = []
    services: dict[str, list[str]] = {}
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        member_index = {name.lstrip("./"): name for name in names}
        if "etc/apk/world" in member_index:
            data = archive.extractfile(member_index["etc/apk/world"]).read()
            world = sorted(data.decode("utf-8", "replace").split())
        if "etc/apk/repositories" in member_index:
            data = archive.extractfile(member_index["etc/apk/repositories"]).read()
            repositories = [line for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
        for member in archive.getmembers():
            clean = member.name.lstrip("./")
            match = re.match(r"etc/runlevels/([^/]+)/([^/]+)$", clean)
            if match and (member.issym() or member.islnk() or member.isfile()):
                level, service = match.groups()
                services.setdefault(level, []).append(service)
    for level in services:
        services[level].sort()
    return {"world": world, "repositories": repositories, "services": services}


def _compare_env(recorded: dict, current: dict | None) -> dict:
    if current is None:
        return {
            "current": None,
            "matches_current_repo_pins": None,
            "differences": None,
            "note": "no --repo-build-env given; drift against distro/alpine/build.env was not checked",
        }
    differences = {
        key: {"recorded": recorded.get(key), "current": current.get(key)}
        for key in sorted(set(recorded) | set(current))
        if recorded.get(key) != current.get(key)
    }
    return {
        "current": current,
        "matches_current_repo_pins": not differences,
        "differences": differences,
    }


def _repository_section(manifest: dict, current_env: dict | None, compare: dict | None) -> dict:
    indexes = manifest.get("repository_indexes") or {}
    repositories = []
    for repo in indexes.get("repositories", []):
        index = repo.get("apkindex") or {}
        repositories.append({
            "role": repo.get("role"),
            "url": repo.get("url"),
            "canonical_url": repo.get("canonical_url"),
            "source": repo.get("source"),
            "apkindex_url": repo.get("apkindex_url"),
            "apkindex_status": index.get("status"),
            "apkindex_sha256": index.get("sha256"),
            "apkindex_size": index.get("size"),
            "apkindex_reason": index.get("reason"),
        })
    overrides = sorted(repo["role"] for repo in repositories if repo["source"] == "override")
    missing = sorted(repo["role"] for repo in repositories if repo["apkindex_status"] != "recorded")

    url_drift = None
    if current_env is not None and current_env.get("ALPINE_BRANCH"):
        branch = current_env["ALPINE_BRANCH"]
        url_drift = {}
        for repo in repositories:
            expected = f"{CANONICAL_REPO_BASE}/{branch}/{repo['role']}"
            if repo["url"] != expected:
                url_drift[repo["role"]] = {"recorded": repo["url"], "expected_for_current_branch": expected}

    index_drift = None
    if compare is not None:
        compared = {
            repo.get("role"): (repo.get("apkindex") or {}).get("sha256")
            for repo in (compare.get("repository_indexes") or {}).get("repositories", [])
        }
        index_drift = {}
        for repo in repositories:
            other = compared.get(repo["role"])
            index_drift[repo["role"]] = {
                "recorded": repo["apkindex_sha256"],
                "compared": other,
                "changed": repo["apkindex_sha256"] != other,
            }

    return {
        "kind": "post-build-moving-repository-index-sample",
        "repositories": repositories,
        "overridden_roles": overrides,
        "has_repository_overrides": bool(overrides),
        "roles_without_recorded_index": missing,
        "repository_url_drift_vs_current_build_env": url_drift,
        "apkindex_drift_vs_compared_build": index_drift,
        "note": (
            "Alpine release repositories keep moving. These APKINDEX digests are "
            "post-build samples: they help diagnose later repository movement but "
            "do not prove which index state apk observed during the build."
        ),
    }


def _closure_section(manifest: dict, apks_dir: Path | None, compare: dict | None) -> dict:
    recorded = manifest.get("output_closure") or {}
    packages = recorded.get("packages", [])
    by_name = {entry.get("filename"): entry for entry in packages}
    result = {
        "kind": "exact-output-closure",
        "status": recorded.get("status"),
        "recorded_apk_count": recorded.get("apk_count"),
        "recorded_total_size": recorded.get("total_size"),
        "reason": recorded.get("reason"),
    }

    if apks_dir is not None and apks_dir.is_dir():
        actual = {}
        for apk in sorted(apks_dir.rglob("*.apk")):
            actual[apk.name] = {"sha256": sha256_file(apk), "size": apk.stat().st_size}
        missing = sorted(set(by_name) - set(actual))
        unexpected = sorted(set(actual) - set(by_name))
        mismatches = [
            {
                "filename": name,
                "recorded_sha256": by_name[name].get("sha256"),
                "image_sha256": actual[name]["sha256"],
            }
            for name in sorted(set(by_name) & set(actual))
            if by_name[name].get("sha256") != actual[name]["sha256"]
        ]
        result["verified_against_image"] = True
        result["image_apk_count"] = len(actual)
        result["missing_from_image"] = missing
        result["unexpected_in_image"] = unexpected
        result["sha256_mismatches"] = mismatches
        result["closure_matches_image"] = not (missing or unexpected or mismatches)
    else:
        result["verified_against_image"] = False
        result["closure_matches_image"] = None
        result["note"] = "no apks directory available; the recorded closure was not re-hashed"

    if compare is not None:
        other = {
            entry.get("filename"): entry.get("sha256")
            for entry in (compare.get("output_closure") or {}).get("packages", [])
        }
        added = sorted(set(by_name) - set(other))
        removed = sorted(set(other) - set(by_name))
        changed = sorted(
            name for name in set(by_name) & set(other)
            if by_name[name].get("sha256") != other[name]
        )
        result["closure_drift_vs_compared_build"] = {
            "added": added,
            "removed": removed,
            "changed_sha256": changed,
            "identical": not (added or removed or changed),
        }
    return result


def section_recorded_inputs(
    build_inputs: Path | None,
    repo_build_env: Path | None,
    build_manifest: Path | None,
    compare_manifest: Path | None,
    apks_dir: Path | None,
) -> dict:
    """Report the three separate kinds of recorded build evidence.

    * `source_pins` -- immutable, content-addressed or revision-pinned inputs.
    * `repository_indexes` -- digests of the moving repositories' APKINDEX files.
    * `output_closure` -- the exact package closure the ISO actually shipped.

    They are never merged into a single "reproducible" claim.
    """
    if (build_inputs is None or not build_inputs.is_file()) and (
            build_manifest is None or not build_manifest.is_file()):
        return unavailable(
            "no recorded build evidence given; pass --build-inputs <out>/build-inputs.env "
            "and/or --build-manifest <iso>.build-manifest.json (both are written by "
            "distro/alpine/mkimage.sh next to every built ISO)"
        )

    current_env = None
    if repo_build_env is not None and repo_build_env.is_file():
        current_env = parse_env_file(repo_build_env.read_text(encoding="utf-8"))

    manifest: dict | None = None
    manifest_error = None
    if build_manifest is not None and build_manifest.is_file():
        manifest, manifest_error = load_json(build_manifest)

    compare: dict | None = None
    compare_error = None
    if compare_manifest is not None and compare_manifest.is_file():
        compare, compare_error = load_json(compare_manifest)

    recorded_env = {}
    if build_inputs is not None and build_inputs.is_file():
        recorded_env = parse_env_file(build_inputs.read_text(encoding="utf-8"))
    manifest_pins = ((manifest or {}).get("source_pins") or {}).get("values") or {}
    recorded_pins = recorded_env or {
        key: value for key, value in manifest_pins.items() if value is not None
    }

    source_pins = {
        "kind": "source-selection",
        "recorded": recorded_pins,
        "source": "build-inputs.env" if recorded_env else ("build-manifest" if manifest_pins else None),
        "immutable_keys": ((manifest or {}).get("source_pins") or {}).get("immutable_keys"),
        "moving_selection_keys": (
            ((manifest or {}).get("source_pins") or {}).get("moving_selection_keys")
        ),
        "overrides": ((manifest or {}).get("source_pins") or {}).get("overrides"),
        "note": (
            "Container digest and git revisions are immutable. ALPINE_BRANCH is "
            "a moving repository selection and does not pin package versions."
        ),
    }
    source_pins.update(_compare_env(recorded_pins, current_env))
    if recorded_env and manifest_pins:
        differing = sorted(
            key for key in set(recorded_env) | set(manifest_pins)
            if recorded_env.get(key) != manifest_pins.get(key)
        )
        source_pins["build_manifest_agrees_with_build_inputs_env"] = not differing
        source_pins["build_manifest_disagreements"] = differing

    result = {"source_pins": source_pins}
    if manifest is None:
        reason = manifest_error or (
            "no --build-manifest given; repository index digests and the exact "
            "output closure were not checked"
        )
        result["repository_indexes"] = unavailable(reason)
        result["output_closure"] = unavailable(reason)
        result["reproducibility"] = unavailable(reason)
    else:
        result["repository_indexes"] = available(**_repository_section(manifest, current_env, compare))
        result["output_closure"] = available(**_closure_section(manifest, apks_dir, compare))
        result["reproducibility"] = available(**(manifest.get("reproducibility") or {}))
        result["artifacts"] = manifest.get("artifacts")
        result["kernel_identity"] = manifest.get("kernel_identity")
        result["build"] = manifest.get("build")
    if compare_error:
        result["compare_manifest_error"] = compare_error
    return available(**result)


def section_packages(apks_dir: Path | None, apkovl_path: Path | None) -> dict:
    if apks_dir is None or not apks_dir.is_dir():
        return unavailable("apks directory not found (expected <root>/apks with the embedded package closure)")
    packages = []
    for apk_file in sorted(apks_dir.rglob("*.apk")):
        packages.append({
            "filename": apk_file.name,
            "path": str(apk_file.relative_to(apks_dir)),
            "sha256": sha256_file(apk_file),
            "size": apk_file.stat().st_size,
        })
    result = {"closure_count": len(packages), "packages": packages}
    if apkovl_path is not None and apkovl_path.is_file():
        overlay = read_apkovl(apkovl_path)
        result["requested_world"] = overlay["world"]
        result["repositories"] = overlay["repositories"]
        result["note"] = (
            "requested_world lists top-level atoms (name/version constraints); "
            "closure filenames include resolved version-release suffixes, so "
            "matching a specific atom to a filename needs the release APKINDEX "
            "and is not attempted here to avoid a false-positive/negative match."
        )
    else:
        result["requested_world"] = None
        result["repositories"] = None
        result["note"] = "no apkovl given; requested (world) packages were not cross-checked against the closure"
    return available(**result)


def find_modules_root(modloop_root: Path | None) -> Path | None:
    """Locate the kernel-module tree inside an extracted modloop.

    Alpine's `update-kernel` copies `lib/modules` to `<modloop>/modules`, so a
    real modloop has `modules/<version>`. The `lib/modules` layout is still
    accepted for rootfs-style trees and older fixtures.
    """
    if modloop_root is None:
        return None
    for relative in ("modules", "lib/modules"):
        candidate = modloop_root / relative
        if candidate.is_dir() and any(_is_version_dir(child) for child in candidate.iterdir()):
            return candidate
    return None


def _is_version_dir(path: Path) -> bool:
    return path.is_dir() and path.name != "firmware"


def find_firmware_root(modloop_root: Path | None) -> Path | None:
    """Locate the firmware tree inside an extracted modloop.

    Alpine puts modloop firmware under `<modloop>/modules/firmware`, not
    `lib/firmware`; the latter is only correct for a rootfs tree.
    """
    if modloop_root is None:
        return None
    for relative in ("modules/firmware", "lib/firmware"):
        candidate = modloop_root / relative
        if candidate.is_dir():
            return candidate
    return None


def _missing_modules_reason(modloop_root: Path | None) -> str:
    if modloop_root is None:
        return "no --modloop-root given"
    return (
        "--modloop-root has no modules/<version> (Alpine modloop layout) or "
        "lib/modules/<version> directory"
    )


def section_kernel(modloop_root: Path | None, root: Path | None) -> dict:
    versions = []
    modules_root = find_modules_root(modloop_root)
    if modules_root is not None:
        versions = sorted(p.name for p in modules_root.iterdir() if _is_version_dir(p))
    config = None
    config_source = None
    for base in filter(None, (root, modloop_root)):
        boot_dir = base / "boot"
        if not boot_dir.is_dir():
            continue
        candidates = sorted(boot_dir.glob("config-*"))
        if candidates:
            config_source = str(candidates[0])
            full_config = parse_kernel_config(candidates[0].read_text(encoding="utf-8", errors="replace"))
            config = {name: full_config.get(name, "unknown") for name in RELEVANT_KERNEL_CONFIG}
            break
    if not versions and config is None:
        return unavailable(
            "no modules/<version> (or lib/modules/<version>) directory and no "
            "boot/config-* file found; pass --modloop-root"
        )
    return available(
        module_versions=versions,
        modules_root=str(modules_root) if modules_root else None,
        config_source=config_source,
        relevant_config=config,
    )


def section_initramfs(initramfs_path: Path | None) -> dict:
    if initramfs_path is None or not initramfs_path.is_file():
        return unavailable("initramfs file not found (expected boot/initramfs-lts)")
    raw = initramfs_path.read_bytes()
    try:
        data = gzip.decompress(raw)
    except OSError:
        data = raw  # allow an already-decompressed/raw cpio for fixtures
    try:
        entries = parse_cpio_newc(data)
    except ValueError as error:
        return unavailable(f"could not parse cpio contents: {error}")
    return available(
        entry_count=len(entries),
        entries=sorted(entries, key=lambda entry: entry["path"]),
    )


def section_modloop(modloop_root: Path | None) -> dict:
    if modloop_root is None or not modloop_root.is_dir():
        return unavailable(
            "no --modloop-root given; this tool does not invoke unsquashfs itself. "
            "Extract the boot/modloop-lts squashfs first, e.g. "
            "`unsquashfs -d <dir> boot/modloop-lts`, then pass --modloop-root <dir>."
        )
    modules_root = find_modules_root(modloop_root)
    if modules_root is None:
        return unavailable(_missing_modules_reason(modloop_root))
    modules = []
    for version_dir in sorted(p for p in modules_root.iterdir() if _is_version_dir(p)):
        kos = sorted(
            str(p.relative_to(version_dir)).replace("\\", "/")
            for p in version_dir.rglob("*.ko*") if p.is_file()
        )
        modules.append({"kernel_version": version_dir.name, "module_count": len(kos), "modules": kos})
    return available(modules_root=str(modules_root), module_versions=modules)


def _validate_aliases(aliases: list[dict], expectations: dict) -> dict:
    """Match every concrete coverage-manifest device ID against modules.alias."""
    relevant_modules = expectations["modules"]
    relevant_aliases = [
        entry for entry in aliases if module_key(entry["module"]) in relevant_modules
    ]
    alias_modules = {module_key(entry["module"]) for entry in aliases}

    devices = []
    unmatched = []
    ambiguous = []
    wrong_module = []
    for device in expectations["devices"]:
        matched_aliases = [
            entry for entry in aliases if alias_matches_probe(entry["alias"], device["probe"])
        ]
        matched_modules = sorted({module_key(entry["module"]) for entry in matched_aliases})
        expected = sorted(set(device["expected_modules"]))
        unexpected = sorted(set(matched_modules) - set(expected))
        matched_expected = sorted(set(matched_modules) & set(expected))
        missing_expected = sorted(
            module for module in expected
            if module not in matched_modules and module in alias_modules
        )
        record = {
            "id": device["id"],
            "bus": device["bus"],
            "device_id": device["device_id"],
            "probe": device["probe"],
            "expected_modules": expected,
            "matched_modules": matched_modules,
            "matched_expected_modules": matched_expected,
            "matched_aliases": sorted(
                ({"alias": entry["alias"], "module": entry["module"]} for entry in matched_aliases),
                key=lambda item: (item["module"], item["alias"]),
            ),
            "unexpected_modules": unexpected,
            "expected_modules_without_matching_alias": missing_expected,
            "status": "matched" if matched_expected else "wrong-module" if matched_modules
                      else "unmatched",
        }
        devices.append(record)
        if not matched_modules:
            unmatched.append({"id": device["id"], "device_id": device["device_id"], "probe": device["probe"]})
        elif not matched_expected:
            wrong_module.append({
                "id": device["id"],
                "device_id": device["device_id"],
                "probe": device["probe"],
                "matched_modules": matched_modules,
                "expected_modules": expected,
            })
        if len(matched_modules) > 1 or unexpected:
            ambiguous.append({
                "id": device["id"],
                "device_id": device["device_id"],
                "matched_modules": matched_modules,
                "expected_modules": expected,
                "reason": "matches more than one module" if len(matched_modules) > 1
                          else "matches a module the coverage manifest does not list",
            })
    return {
        "devices": devices,
        "unmatched_ids": unmatched,
        "ambiguous_ids": ambiguous,
        "wrong_module_ids": wrong_module,
        "matched_id_count": sum(1 for device in devices if device["status"] == "matched"),
        "checked_id_count": len(devices),
        "relevant_aliases": sorted(
            relevant_aliases, key=lambda entry: (entry["module"], entry["alias"])
        ),
        "relevant_alias_count": len(relevant_aliases),
        "coverage_modules_without_any_alias": sorted(relevant_modules - alias_modules),
        "note": (
            "A device is 'matched' only when one of the modules the coverage manifest "
            "expects claims the ID. Additional modules matching the same ID are "
            "reported in ambiguous_ids; an ID claimed only by unexpected modules is "
            "'wrong-module' and is a failure, not a match."
        ),
    }


def section_module_aliases(modloop_root: Path | None, coverage_manifest: Path | None) -> dict:
    """Validate coverage-manifest device IDs against the image's modules.alias.

    Relevant modules and device IDs come from the coverage manifest, not from
    a hard-coded list, so the check widens automatically as the matrix grows.
    """
    modules_root = find_modules_root(modloop_root)
    if modules_root is None:
        return unavailable(_missing_modules_reason(modloop_root))
    if coverage_manifest is None or not coverage_manifest.is_file():
        return unavailable(
            "no coverage manifest found; pass --coverage-manifest "
            "(default: docs/qa/hardware-coverage.json)"
        )
    manifest, error = load_json(coverage_manifest)
    if manifest is None:
        return unavailable(f"could not read coverage manifest: {error}")
    expectations = coverage_expectations(manifest)

    by_version = {}
    for version_dir in sorted(p for p in modules_root.iterdir() if _is_version_dir(p)):
        alias_file = version_dir / "modules.alias"
        if not alias_file.is_file():
            continue
        aliases = parse_modules_alias(alias_file.read_text(encoding="utf-8", errors="replace"))
        by_version[version_dir.name] = _validate_aliases(aliases, expectations)
    if not by_version:
        return unavailable("no modules.alias file found under any modules/<version> directory")
    return available(
        coverage_manifest=str(coverage_manifest),
        coverage_module_count=len(expectations["modules"]),
        coverage_device_id_count=len(expectations["devices"]),
        by_kernel_version=by_version,
        note=(
            "A matched alias proves the shipped modules claim that device ID; it is "
            "not evidence the device works. Probes wildcard PCI subsystem/class and "
            "USB version/interface fields because the coverage manifest records "
            "vendor/device IDs only."
        ),
    )


def section_firmware(modloop_root: Path | None) -> dict:
    if modloop_root is None:
        return unavailable("no --modloop-root given")
    firmware_root = find_firmware_root(modloop_root)
    if firmware_root is None:
        return unavailable(
            "--modloop-root has no modules/firmware (Alpine modloop layout) or "
            "lib/firmware directory"
        )
    all_files = []
    for path in sorted(firmware_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        rel = str(path.relative_to(firmware_root)).replace("\\", "/")
        entry = {
            "path": rel,
            "is_symlink": path.is_symlink(),
            "symlink_target": str(path.readlink()).replace("\\", "/") if path.is_symlink() else None,
            "compressed": "zstd" if rel.endswith(".zst") else "xz" if rel.endswith(".xz") else None,
        }
        all_files.append(entry)
    highlights = [f for f in all_files if f["path"].startswith(FIRMWARE_HIGHLIGHT_PREFIXES)]
    return available(
        firmware_root=str(firmware_root),
        file_count=len(all_files),
        symlink_count=sum(1 for f in all_files if f["is_symlink"]),
        compressed_count=sum(1 for f in all_files if f["compressed"]),
        files=all_files,
        highlights=highlights,
        note=(
            "Complete firmware inventory from the modloop tree. 'highlights' is a "
            "convenience subset of the same records, not a separate source of truth."
        ),
    )


APK_FILENAME = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+-r\d+)\.apk$")
FIRMWARE_MEMBER_PREFIXES = ("lib/firmware/", "usr/lib/firmware/")


def apk_atom(filename: str) -> tuple[str | None, str | None]:
    """Split `name-<version>-r<rel>.apk` into (name, version)."""
    match = APK_FILENAME.match(filename)
    if not match:
        return None, None
    return match.group("name"), match.group("version")


def firmware_name_variants(path: str) -> set[str]:
    """Names the kernel can satisfy with this file.

    Alpine ships most firmware zstd-compressed; the kernel loads `foo.bin`
    from `foo.bin.zst`. The uncompressed name is therefore an equally valid
    answer to a coverage-manifest firmware path.
    """
    variants = {path}
    for suffix in FIRMWARE_COMPRESSION_SUFFIXES:
        if path.endswith(suffix):
            variants.add(path[: -len(suffix)])
    return variants


def firmware_matches(pattern: str, paths: list[str]) -> list[str]:
    """Match a coverage-manifest firmware path/glob against shipped files."""
    hits = []
    for candidate in paths:
        if any(fnmatch.fnmatchcase(variant, pattern) for variant in firmware_name_variants(candidate)):
            hits.append(candidate)
    return sorted(hits)


def firmware_requirement_groups(entry: dict) -> list[dict]:
    """Return a coverage entry's required firmware groups.

    Every group must be satisfied; the patterns inside one group are
    alternatives (alternate packaged layouts or version wildcards for the same
    artifact). An entry that predates `firmware_requirements` is read the
    strict way -- one group per recorded path -- so an old manifest is never
    validated more loosely than a migrated one.
    """
    groups = entry.get("firmware_requirements")
    if groups is None:
        return [
            {"id": pattern, "any_of": [pattern]}
            for pattern in entry.get("firmware_files") or []
        ]
    return [
        {"id": group.get("id") or "", "any_of": list(group.get("any_of") or [])}
        for group in groups
    ]


def scan_apk_members(apk: Path) -> tuple[list[dict], str | None]:
    """List an .apk's members without extracting anything.

    An APKv2 file is concatenated gzip streams holding tar segments, which
    tarfile reads in order. Only member metadata is read; no file content is
    written anywhere.
    """
    members = []
    try:
        with tarfile.open(apk, "r:gz") as archive:
            for member in archive:
                if member.isdir():
                    continue
                members.append({
                    "path": member.name.lstrip("./"),
                    "is_symlink": member.issym() or member.islnk(),
                    "linkname": member.linkname or None,
                    "size": member.size,
                })
    except (tarfile.TarError, OSError, EOFError) as error:
        return members, f"{type(error).__name__}: {error}"
    return members, None


def collect_package_firmware(apks_dir: Path | None, wanted: set[str]) -> dict:
    """Index the firmware files shipped inside the selected embedded packages.

    This is the offline-availability evidence that matters for both the live
    root and an installed system: the live boot installs the overlay world from
    the ISO's /apks repository, and setup-disk reuses the same world.
    """
    if apks_dir is None or not apks_dir.is_dir():
        return {"status": "unavailable", "reason": "no apks directory available to scan"}
    files: dict[str, dict] = {}
    scanned = {}
    errors = []
    for apk in sorted(apks_dir.rglob("*.apk")):
        name, version = apk_atom(apk.name)
        if name not in wanted:
            continue
        members, error = scan_apk_members(apk)
        if error:
            errors.append(f"{apk.name}: {error}")
        count = 0
        for member in members:
            path = member["path"]
            for prefix in FIRMWARE_MEMBER_PREFIXES:
                if path.startswith(prefix):
                    relative = path[len(prefix):]
                    files[relative] = {
                        "package": name,
                        "version": version,
                        "is_symlink": member["is_symlink"],
                        "linkname": member["linkname"],
                        "size": member["size"],
                        "compressed": (
                            "zstd" if relative.endswith(".zst")
                            else "xz" if relative.endswith(".xz") else None
                        ),
                    }
                    count += 1
                    break
        scanned[name] = {"version": version, "firmware_file_count": count}
    return {
        "status": "recorded",
        "scanned_packages": scanned,
        "files": files,
        "errors": sorted(errors),
    }


def parse_modules_dep(text: str) -> dict:
    """Parse modules.dep into module path -> list of dependency paths."""
    dependencies = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        target, _, rest = line.partition(":")
        dependencies[target.strip()] = rest.split()
    return dependencies


def module_basename(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    for suffix in (".ko.zst", ".ko.xz", ".ko.gz", ".ko"):
        if name.endswith(suffix):
            return module_key(name[: -len(suffix)])
    return module_key(name)


def load_hardware_world(
    build_manifest: dict | None, world_file: Path | None,
) -> tuple[list[str], str, str | None]:
    """Prefer the world the build recorded; fall back to the repository file."""
    worlds = ((build_manifest or {}).get("package_worlds") or {}).get("worlds") or []
    for entry in worlds:
        if entry.get("role") == "hardware" and entry.get("status") == "recorded":
            return list(entry.get("packages") or []), "build-manifest", None
    if world_file is not None and world_file.is_file():
        atoms = [
            line.strip()
            for line in world_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return sorted(set(atoms)), f"repository:{world_file.name}", None
    return [], "none", (
        "no hardware world available; pass --build-manifest from a build that recorded "
        "one, or --hardware-world distro/alpine/apks/world.hardware"
    )


def _check(name: str, description: str, failures: list, **data) -> dict:
    return {
        "name": name,
        "description": description,
        "status": "failed" if failures else "ok",
        "failures": failures,
        **data,
    }


def _unavailable_check(name: str, description: str, reason: str) -> dict:
    return {
        "name": name,
        "description": description,
        "status": "unavailable",
        "reason": reason,
        "failures": [],
    }


def section_hardware_bundle(
    coverage_manifest: Path | None,
    hardware_package_manifest: Path | None,
    hardware_world_file: Path | None,
    build_manifest_data: dict | None,
    apkovl_path: Path | None,
    apks_dir: Path | None,
    modloop_root: Path | None,
    initramfs_path: Path | None,
    scan_packages: bool,
) -> dict:
    """Validate the offline hardware bundle against the built artifacts.

    Every check answers one question about the artifact in front of it:
    does the image really carry what docs/qa/hardware-coverage.json says the
    selected devices need, and do the live and installed package worlds agree?
    A check is "ok", "failed" (an inconsistency was found) or "unavailable"
    (the evidence needed to decide was not supplied). Nothing here makes a
    device "verified": presence is not a hardware support claim.
    """
    coverage, coverage_error = (None, "no coverage manifest given")
    if coverage_manifest is not None and coverage_manifest.is_file():
        coverage, coverage_error = load_json(coverage_manifest)
        if coverage is None:
            coverage_error = f"could not read coverage manifest: {coverage_error}"
    if coverage is None:
        return unavailable(coverage_error)

    package_records = {}
    package_manifest_source = None
    embedded = (build_manifest_data or {}).get("hardware_packages") or {}
    if embedded.get("status") == "recorded":
        package_records = embedded.get("packages") or {}
        package_manifest_source = f"build-manifest:{embedded.get('source')}"
    elif hardware_package_manifest is not None and hardware_package_manifest.is_file():
        loaded, error = load_json(hardware_package_manifest)
        if loaded is not None:
            package_records = loaded.get("packages") or {}
            package_manifest_source = f"repository:{hardware_package_manifest.name}"

    world, world_source, world_error = load_hardware_world(build_manifest_data, hardware_world_file)
    checks = []

    # 1. Live and installed worlds carry the same hardware set, and world.vm survives.
    if world_error:
        checks.append(_unavailable_check(
            "hardware_world_parity",
            "the overlay /etc/apk/world requests every world.hardware package",
            world_error,
        ))
    elif apkovl_path is None or not apkovl_path.is_file():
        checks.append(_unavailable_check(
            "hardware_world_parity",
            "the overlay /etc/apk/world requests every world.hardware package",
            "no apkovl given; the installed/live world could not be compared",
        ))
    else:
        overlay_world = set(read_apkovl(apkovl_path)["world"])
        missing = sorted(set(world) - overlay_world)
        checks.append(_check(
            "hardware_world_parity",
            "the overlay /etc/apk/world requests every world.hardware package",
            [{"package": name, "reason": "absent from the overlay /etc/apk/world"} for name in missing],
            world_source=world_source,
            hardware_package_count=len(world),
            overlay_world_size=len(overlay_world),
            note=(
                "The live root installs this overlay world from the ISO's /apks "
                "repository during initramfs and setup-disk reuses the same file, so "
                "one comparison covers both the live and the installed world."
            ),
        ))

    recorded_worlds = ((build_manifest_data or {}).get("package_worlds") or {}).get("worlds") or []
    if recorded_worlds:
        roles = {entry.get("role") for entry in recorded_worlds}
        checks.append(_check(
            "virtual_hardware_world_preserved",
            "world.vm is still part of the build",
            [] if "vm" in roles else [{"world": "vm", "reason": "no vm world recorded for this build"}],
            recorded_roles=sorted(role for role in roles if role),
        ))
    else:
        checks.append(_unavailable_check(
            "virtual_hardware_world_preserved",
            "world.vm is still part of the build",
            "no build manifest with recorded package worlds given",
        ))

    # 2. Offline availability: each atom resolves to an embedded .apk.
    closure_names = None
    if apks_dir is not None and apks_dir.is_dir():
        closure_names = sorted(p.name for p in apks_dir.rglob("*.apk"))
    else:
        recorded_closure = (build_manifest_data or {}).get("output_closure") or {}
        if recorded_closure.get("status") == "recorded":
            closure_names = sorted(entry["filename"] for entry in recorded_closure.get("packages", []))
    if closure_names is None or world_error:
        checks.append(_unavailable_check(
            "offline_package_availability",
            "every world.hardware package ships inside the ISO's /apks repository",
            world_error or "no embedded package closure available (pass --apks-dir or --build-manifest)",
        ))
        resolved = {}
    else:
        by_atom = {}
        for filename in closure_names:
            name, version = apk_atom(filename)
            if name:
                by_atom.setdefault(name, []).append({"filename": filename, "version": version})
        resolved = {name: by_atom[name] for name in world if name in by_atom}
        checks.append(_check(
            "offline_package_availability",
            "every world.hardware package ships inside the ISO's /apks repository",
            [
                {"package": name, "reason": "no matching .apk in the embedded closure"}
                for name in world if name not in by_atom
            ],
            resolved_packages=resolved,
            closure_size=len(closure_names),
            note="Resolved versions come from the shipped filenames, so no repository is contacted.",
        ))

    # 3. Coverage-selected firmware packages are present, licensed and attributed.
    selected_packages = {}
    for entry in coverage.get("entries", []):
        for package in entry.get("firmware_package") or []:
            selected_packages.setdefault(package, []).append(entry.get("id"))
    license_failures = []
    for package, entry_ids in sorted(selected_packages.items()):
        if world and package not in world:
            license_failures.append({
                "package": package,
                "reason": "selected by the coverage manifest but missing from world.hardware",
                "coverage_entries": sorted(entry_ids),
            })
            continue
        record = package_records.get(package) or {}
        missing_fields = [
            field for field in ("license", "repository") if not record.get(field)
        ]
        if missing_fields:
            license_failures.append({
                "package": package,
                "reason": (
                    "no license/provenance record for a redistributed firmware package: "
                    f"missing {', '.join(missing_fields)}"
                ),
                "missing_fields": missing_fields,
                "coverage_entries": sorted(entry_ids),
            })
    if package_manifest_source is None:
        checks.append(_unavailable_check(
            "firmware_license_provenance",
            "every redistributed firmware package has a recorded license and repository",
            "no hardware package manifest available (pass --hardware-package-manifest "
            "or a --build-manifest that embedded one)",
        ))
    else:
        checks.append(_check(
            "firmware_license_provenance",
            "every redistributed firmware package has a recorded license and repository",
            license_failures,
            source=package_manifest_source,
            licensed_packages={
                name: {"license": record.get("license"), "repository": record.get("repository")}
                for name, record in sorted(package_records.items())
            },
        ))

    # 4. Firmware files, compression and symlink targets.
    package_firmware = (
        collect_package_firmware(apks_dir, set(world))
        if scan_packages else {"status": "unavailable", "reason": "package contents were not scanned"}
    )
    modloop_firmware_root = find_firmware_root(modloop_root)
    modloop_paths = []
    modloop_symlinks = {}
    if modloop_firmware_root is not None:
        for path in sorted(modloop_firmware_root.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = str(path.relative_to(modloop_firmware_root)).replace("\\", "/")
            modloop_paths.append(relative)
            if path.is_symlink():
                modloop_symlinks[relative] = str(path.readlink()).replace("\\", "/")

    package_paths = sorted((package_firmware.get("files") or {}))
    package_scan_recorded = package_firmware.get("status") == "recorded"
    all_paths = sorted(set(package_paths) | set(modloop_paths))
    sources = []
    if package_scan_recorded:
        sources.append("embedded-packages")
    if modloop_paths:
        sources.append("modloop")
    selected_paths: set[str] = set()
    if not package_scan_recorded:
        checks.append(_unavailable_check(
            "firmware_files_present",
            "every selected device's firmware ships in the selected packages",
            "the selected packages' contents were not read: "
            f"{package_firmware.get('reason', 'no package scan')}. Firmware coverage is "
            "decided from the packages the live root and setup-disk install, so pass "
            "--apks-dir with --validate-hardware.",
        ))
    else:
        firmware_failures = []
        devices = []
        for entry in coverage.get("entries", []):
            groups = firmware_requirement_groups(entry)
            if not groups:
                continue
            group_records = []
            for group in groups:
                in_packages = sorted({
                    hit for pattern in group["any_of"]
                    for hit in firmware_matches(pattern, package_paths)
                })
                in_modloop = sorted({
                    hit for pattern in group["any_of"]
                    for hit in firmware_matches(pattern, modloop_paths)
                })
                selected_paths.update(in_packages)
                selected_paths.update(in_modloop)
                group_records.append({
                    "requirement": group["id"],
                    "any_of": group["any_of"],
                    "status": "matched" if in_packages else "unmatched",
                    "in_packages": in_packages[:8],
                    "in_modloop": in_modloop[:8],
                })
                if in_packages:
                    continue
                firmware_failures.append({
                    "id": entry.get("id"),
                    "requirement": group["id"],
                    "any_of": group["any_of"],
                    "reason": (
                        "the only matching firmware ships in the modloop, which an "
                        "installed system does not use"
                        if in_modloop else
                        "no firmware file in the selected packages matches this requirement"
                    ),
                    "modloop_only_matches": in_modloop[:8],
                    "firmware_package": entry.get("firmware_package") or [],
                })
            devices.append({
                "id": entry.get("id"),
                "requirements": group_records,
                "requirement_count": len(group_records),
                "status": "matched" if all(
                    record["status"] == "matched" for record in group_records) else "unmatched",
            })
        checks.append(_check(
            "firmware_files_present",
            "every selected device's firmware ships in the selected packages",
            firmware_failures,
            sources=sources,
            devices=devices,
            package_firmware_file_count=len(package_paths),
            modloop_firmware_file_count=len(modloop_paths),
            compressed_file_count=sum(
                1 for path in all_paths if path.endswith(FIRMWARE_COMPRESSION_SUFFIXES)
            ),
            note=(
                "A requirement is satisfied by at least one of its alternative paths "
                "inside the selected packages; compressed variants count because the "
                "kernel decompresses them. Co-required artifacts are separate "
                "requirements. Modloop matches are reported as diagnostic evidence "
                "only: world.hardware populates /lib/firmware on the live and the "
                "installed system, and the modloop's pruned copy is not what those "
                "systems load."
            ),
        ))

    if not sources:
        checks.append(_unavailable_check(
            "firmware_symlink_targets",
            "firmware symlinks resolve to a file that also ships",
            "no firmware inventory available; pass --modloop-root and/or "
            "--apks-dir with --validate-hardware",
        ))
    else:
        symlink_failures = []
        symlinks = []
        unresolved_unselected = []
        shipped = set(modloop_paths) | set(package_paths)
        shipped_dirs = {
            parent for path in shipped
            for parent in (ancestor.as_posix() for ancestor in PurePosixPath(path).parents)
            if parent not in (".", "")
        }

        def resolve(path: str, target: str) -> str:
            if target.startswith("/"):
                resolved = target.lstrip("/")
            else:
                resolved = f"{PurePosixPath(path).parent.as_posix()}/{target}"
            parts: list[str] = []
            for part in resolved.split("/"):
                if part in ("", "."):
                    continue
                if part == ".." and parts:
                    parts.pop()
                else:
                    parts.append(part)
            resolved = "/".join(parts)
            return resolved.removeprefix("lib/firmware/").removeprefix("usr/lib/firmware/")

        def record_symlink(path: str, target: str, source: str) -> None:
            resolved_target = resolve(path, target)
            ok = resolved_target in shipped or resolved_target in shipped_dirs
            selected = path in selected_paths
            symlinks.append({
                "path": path, "target": target, "resolves": ok,
                "selected": selected, "source": source,
            })
            if ok:
                return
            failure = {
                "path": path, "target": target, "source": source,
                "reason": "symlink target does not ship in the image",
            }
            if selected:
                symlink_failures.append(failure)
            else:
                unresolved_unselected.append(failure)

        for path, target in sorted(modloop_symlinks.items()):
            record_symlink(path, target, "modloop")
        for path, record in sorted((package_firmware.get("files") or {}).items()):
            if record.get("is_symlink"):
                record_symlink(path, record.get("linkname") or "", f"package:{record.get('package')}")
        checks.append(_check(
            "firmware_symlink_targets",
            "firmware symlinks resolve to a file that also ships",
            symlink_failures,
            symlink_count=len(symlinks),
            unresolved_unselected_symlinks=unresolved_unselected,
            note=(
                "A symlink fails only when it belongs to a selected device's firmware "
                "set. linux-firmware subpackages link to sibling subpackages and the "
                "modloop carries a pruned copy of linux-firmware, so links that no "
                "selected device needs are reported without failing the image."
            ),
        ))

    # 5. Module dependency closure in the modloop.
    modules_root = find_modules_root(modloop_root)
    expectations = coverage_expectations(coverage)
    if modules_root is None:
        checks.append(_unavailable_check(
            "module_dependency_closure",
            "every selected module and its modules.dep dependencies ship in the modloop",
            _missing_modules_reason(modloop_root),
        ))
    else:
        module_failures = []
        by_version = {}
        for version_dir in sorted(p for p in modules_root.iterdir() if _is_version_dir(p)):
            present = {}
            for path in version_dir.rglob("*.ko*"):
                if path.is_file():
                    present[module_basename(path.name)] = str(
                        path.relative_to(version_dir)).replace("\\", "/")
            builtin_file = version_dir / "modules.builtin"
            builtin = set()
            if builtin_file.is_file():
                for line in builtin_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line:
                        builtin.add(module_basename(line))
                        present.setdefault(module_basename(line), "builtin")
            dep_file = version_dir / "modules.dep"
            dependencies = (
                parse_modules_dep(dep_file.read_text(encoding="utf-8", errors="replace"))
                if dep_file.is_file() else {}
            )
            dep_by_module = {
                module_basename(target): [module_basename(dep) for dep in deps]
                for target, deps in dependencies.items()
            }
            missing_modules = sorted(name for name in expectations["modules"] if name not in present)
            missing_dependencies = []
            missing_dep_entries = []
            for name in sorted(expectations["modules"]):
                for dependency in dep_by_module.get(name, []):
                    if dependency not in present:
                        missing_dependencies.append({"module": name, "missing_dependency": dependency})
                if name in builtin or name not in present:
                    continue
                if name not in dep_by_module:
                    missing_dep_entries.append({"module": name, "module_path": present[name]})
            by_version[version_dir.name] = {
                "modules_present": len(expectations["modules"]) - len(missing_modules),
                "modules_expected": len(expectations["modules"]),
                "modules_builtin": sorted(expectations["modules"] & builtin),
                "modules_dep_entries": len(dep_by_module),
                "missing_modules": missing_modules,
                "missing_dependencies": missing_dependencies,
                "modules_without_a_dep_entry": missing_dep_entries,
            }
            module_failures.extend(
                {"kernel_version": version_dir.name, "module": name,
                 "reason": "selected module is neither a shipped .ko nor built into the kernel"}
                for name in missing_modules
            )
            module_failures.extend(
                {"kernel_version": version_dir.name, **item,
                 "reason": "modules.dep dependency is not in the modloop"}
                for item in missing_dependencies
            )
            module_failures.extend(
                {"kernel_version": version_dir.name, **item,
                 "reason": "no modules.dep entry for a selected module; depmod data is "
                           "missing or truncated, so modprobe would not load it"}
                for item in missing_dep_entries
            )
            if not dep_file.is_file():
                module_failures.append({
                    "kernel_version": version_dir.name,
                    "reason": "no modules.dep file; the dependency closure cannot be checked",
                })
        checks.append(_check(
            "module_dependency_closure",
            "every selected module and its modules.dep dependencies ship in the modloop",
            module_failures,
            by_kernel_version=by_version,
            note=(
                "depmod writes one modules.dep line per module, dependencies or not, so "
                "a selected module with no line means the dependency data is missing or "
                "truncated. Module names are normalized ('-' and '_' are the same name) "
                "and compressed .ko.zst/.ko.xz/.ko.gz modules count; modules.builtin "
                "modules are satisfied by the kernel image and have no dep entry."
            ),
        ))

    # 6. Boot-critical modules are in the initramfs, not only the modloop.
    boot_critical = sorted({
        module_key(module)
        for entry in coverage.get("entries", [])
        if entry.get("family") in BOOT_CRITICAL_FAMILIES
        for module in (entry.get("kernel_module") or []) + (entry.get("module_dependencies") or [])
    })
    initramfs_modules = None
    if initramfs_path is not None and initramfs_path.is_file():
        report = section_initramfs(initramfs_path)
        if report["status"] == "available":
            initramfs_modules = {
                module_basename(entry["path"])
                for entry in report["entries"] if ".ko" in entry["path"]
            }
    if initramfs_modules is None:
        checks.append(_unavailable_check(
            "boot_critical_modules_in_initramfs",
            "storage and USB controller modules load before the boot media is mounted",
            "no readable initramfs given (expected boot/initramfs-lts)",
        ))
    else:
        checks.append(_check(
            "boot_critical_modules_in_initramfs",
            "storage and USB controller modules load before the boot media is mounted",
            [
                {"module": name, "reason": "boot-critical module missing from the initramfs"}
                for name in boot_critical if name not in initramfs_modules
            ],
            boot_critical_modules=boot_critical,
            initramfs_module_count=len(initramfs_modules),
            note=(
                "Only families the coverage manifest marks storage/usb are required "
                "here. Early-KMS modules and firmware are deliberately not added "
                "without booted evidence that they are needed."
            ),
        ))

    # 7. Device alias mapping, reusing the stage-1 matcher.
    alias_section = section_module_aliases(modloop_root, coverage_manifest)
    if alias_section["status"] != "available":
        checks.append(_unavailable_check(
            "device_alias_mapping",
            "every concrete device ID maps to a shipped module alias",
            alias_section.get("reason", "module aliases unavailable"),
        ))
    else:
        alias_failures = []
        for version, result in alias_section["by_kernel_version"].items():
            alias_failures.extend(
                {"kernel_version": version, **item,
                 "reason": "no shipped module claims this device ID"}
                for item in result["unmatched_ids"]
            )
            alias_failures.extend(
                {"kernel_version": version, **item,
                 "reason": "this device ID is claimed only by modules the coverage "
                           "manifest does not expect for it"}
                for item in result["wrong_module_ids"]
            )
        checks.append(_check(
            "device_alias_mapping",
            "every concrete device ID maps to an expected module's shipped alias",
            alias_failures,
            checked_id_count=sum(
                result["checked_id_count"] for result in alias_section["by_kernel_version"].values()),
            ambiguous_ids=[
                {"kernel_version": version, **item}
                for version, result in alias_section["by_kernel_version"].items()
                for item in result["ambiguous_ids"]
            ],
            note=(
                "An ID matched by an expected module passes even when other modules "
                "claim it too; those extra claims are reported in ambiguous_ids. An ID "
                "matched only by unexpected modules fails: the manifest's driver "
                "mapping would be wrong."
            ),
        ))

    failed = [check["name"] for check in checks if check["status"] == "failed"]
    incomplete = [check["name"] for check in checks if check["status"] == "unavailable"]
    return available(
        coverage_manifest=str(coverage_manifest),
        hardware_world_source=world_source,
        hardware_world=world,
        package_manifest_source=package_manifest_source,
        package_firmware_scan=(
            {key: value for key, value in package_firmware.items() if key != "files"}
            if package_firmware.get("status") == "recorded" else package_firmware
        ),
        checks=checks,
        failed_checks=failed,
        incomplete_checks=incomplete,
        result=("failed" if failed else "incomplete" if incomplete else "passed"),
        sets_physical_status=False,
        note=(
            "This validates that the artifacts are internally consistent and offline "
            "complete. It never promotes a device out of 'untested' in "
            "docs/qa/hardware-coverage.json; only physical evidence does that."
        ),
    )


def hardware_validation_exit_code(section: dict) -> int:
    if section.get("status") != "available":
        return EXIT_VALIDATION_INCOMPLETE
    if section.get("failed_checks"):
        return EXIT_VALIDATION_FAILED
    if section.get("incomplete_checks"):
        return EXIT_VALIDATION_INCOMPLETE
    return EXIT_VALIDATION_OK


def section_mesa_userspace(apkovl_path: Path | None, apks_dir: Path | None) -> dict:
    if apkovl_path is None or not apkovl_path.is_file():
        return unavailable("no apkovl given; the requested (world) Mesa/Xorg package set was not checked")
    overlay = read_apkovl(apkovl_path)
    mesa_related = sorted(
        atom for atom in overlay["world"]
        if atom.startswith(("mesa", "xf86-video", "xorg-server"))
    )
    closure_present = None
    if apks_dir is not None and apks_dir.is_dir():
        closure_names = {p.name for p in apks_dir.rglob("*.apk")}
        closure_present = sorted(
            name for name in closure_names
            if name.startswith(("mesa", "xf86-video", "xorg-server"))
        )
    return available(
        requested_mesa_related_world_atoms=mesa_related,
        mesa_related_packages_in_closure=closure_present,
        note="package presence only; this does not confirm a working DRM/GLX renderer on real hardware",
    )


def section_bootloader(syslinux_cfg: Path | None, grub_cfg: Path | None) -> dict:
    if (syslinux_cfg is None or not syslinux_cfg.is_file()) and (grub_cfg is None or not grub_cfg.is_file()):
        return unavailable("neither a syslinux nor grub config file was found/given")
    result = {}
    if syslinux_cfg is not None and syslinux_cfg.is_file():
        result["syslinux"] = parse_syslinux_cfg(syslinux_cfg.read_text(encoding="utf-8", errors="replace"))
    else:
        result["syslinux"] = None
    if grub_cfg is not None and grub_cfg.is_file():
        result["grub"] = parse_grub_cfg(grub_cfg.read_text(encoding="utf-8", errors="replace"))
    else:
        result["grub"] = None
    return available(**result)


def section_services(apkovl_path: Path | None) -> dict:
    if apkovl_path is None or not apkovl_path.is_file():
        return unavailable("no apkovl given; service/runlevel enablement was not checked")
    overlay = read_apkovl(apkovl_path)
    return available(services_by_runlevel=overlay["services"])


def find_one(root: Path, pattern: str) -> Path | None:
    matches = sorted(root.glob(pattern))
    return matches[0] if matches else None


def build_report(args: argparse.Namespace) -> dict:
    root = args.root
    apkovl_path = args.apkovl or find_one(root, "*.apkovl.tar.gz")
    apks_dir = args.apks_dir or (root / "apks" if (root / "apks").is_dir() else None)
    initramfs_path = args.initramfs or find_one(root, "boot/initramfs-*")
    syslinux_cfg = args.syslinux_cfg or find_one(root, "boot/syslinux/syslinux.cfg")
    grub_cfg = args.grub_cfg or find_one(root, "boot/grub/grub.cfg") or find_one(root, "boot/grub.cfg")
    modloop_root = args.modloop_root
    coverage_manifest = args.coverage_manifest
    if coverage_manifest is None and DEFAULT_COVERAGE_MANIFEST.is_file():
        coverage_manifest = DEFAULT_COVERAGE_MANIFEST
    hardware_package_manifest = args.hardware_package_manifest
    if hardware_package_manifest is None and DEFAULT_HARDWARE_PACKAGE_MANIFEST.is_file():
        hardware_package_manifest = DEFAULT_HARDWARE_PACKAGE_MANIFEST
    hardware_world = args.hardware_world
    if hardware_world is None and DEFAULT_HARDWARE_WORLD.is_file():
        hardware_world = DEFAULT_HARDWARE_WORLD
    build_manifest_data = None
    if args.build_manifest is not None and args.build_manifest.is_file():
        build_manifest_data, _ = load_json(args.build_manifest)

    report = {
        "tool": "inspect-image.py",
        "schema_version": SCHEMA_VERSION,
        "inputs": {
            "root": str(root),
            "apkovl": str(apkovl_path) if apkovl_path else None,
            "apks_dir": str(apks_dir) if apks_dir else None,
            "initramfs": str(initramfs_path) if initramfs_path else None,
            "modloop_root": str(modloop_root) if modloop_root else None,
            "syslinux_cfg": str(syslinux_cfg) if syslinux_cfg else None,
            "grub_cfg": str(grub_cfg) if grub_cfg else None,
            "build_inputs": str(args.build_inputs) if args.build_inputs else None,
            "repo_build_env": str(args.repo_build_env) if args.repo_build_env else None,
            "build_manifest": str(args.build_manifest) if args.build_manifest else None,
            "compare_build_manifest": str(args.compare_build_manifest) if args.compare_build_manifest else None,
            "coverage_manifest": str(coverage_manifest) if coverage_manifest else None,
            "hardware_package_manifest": str(hardware_package_manifest) if hardware_package_manifest else None,
            "hardware_world": str(hardware_world) if hardware_world else None,
            "validate_hardware": bool(args.validate_hardware),
        },
        "sections": {
            "recorded_inputs": section_recorded_inputs(
                args.build_inputs, args.repo_build_env, args.build_manifest,
                args.compare_build_manifest, apks_dir,
            ),
            "packages": section_packages(apks_dir, apkovl_path),
            "kernel": section_kernel(modloop_root, root),
            "initramfs": section_initramfs(initramfs_path),
            "modloop": section_modloop(modloop_root),
            "module_aliases": section_module_aliases(modloop_root, coverage_manifest),
            "firmware": section_firmware(modloop_root),
            "hardware_bundle": section_hardware_bundle(
                coverage_manifest, hardware_package_manifest, hardware_world,
                build_manifest_data, apkovl_path, apks_dir, modloop_root,
                initramfs_path, scan_packages=bool(args.validate_hardware),
            ),
            "mesa_userspace": section_mesa_userspace(apkovl_path, apks_dir),
            "bootloader": section_bootloader(syslinux_cfg, grub_cfg),
            "services": section_services(apkovl_path),
        },
        "notes": [
            "Package, module, or firmware presence is not a hardware support claim; "
            "see docs/qa/hardware-coverage.json for tested status per device.",
            "Sections reflect artifacts actually shipped in the image, including any "
            "behavior inherited from Alpine's profile_standard, not the AIOS profile "
            "script source alone.",
            "recorded_inputs separates immutable source pins and the moving branch "
            "selection, post-build repository APKINDEX samples, and the exact output "
            "closure. The closure records what shipped; this is not a "
            "byte-reproducibility claim.",
            "hardware_bundle validates the offline driver/firmware/userspace closure "
            "against the artifacts. With --validate-hardware the exit code is "
            f"{EXIT_VALIDATION_OK} when every check passes, {EXIT_VALIDATION_FAILED} "
            f"when a check fails, and {EXIT_VALIDATION_INCOMPLETE} when required "
            "evidence was missing.",
        ],
    }
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, type=Path, help="extracted top-level ISO directory")
    parser.add_argument("--apkovl", type=Path, help="path to the *.apkovl.tar.gz (default: autodetect under --root)")
    parser.add_argument("--apks-dir", type=Path, help="path to the embedded apk closure directory (default: <root>/apks)")
    parser.add_argument("--initramfs", type=Path, help="path to boot/initramfs-lts (default: autodetect under --root)")
    parser.add_argument("--modloop-root", type=Path, help="already unsquashfs-extracted modloop directory")
    parser.add_argument("--syslinux-cfg", type=Path, help="path to syslinux.cfg (default: autodetect under --root)")
    parser.add_argument("--grub-cfg", type=Path, help="path to grub.cfg (default: autodetect under --root)")
    parser.add_argument("--build-inputs", type=Path, help="recorded build-inputs.env sibling to a built ISO")
    parser.add_argument("--repo-build-env", type=Path, help="current distro/alpine/build.env to diff against --build-inputs")
    parser.add_argument("--build-manifest", type=Path,
                        help="recorded <iso>.build-manifest.json (repository index digests + output closure)")
    parser.add_argument("--compare-build-manifest", type=Path,
                        help="a second build manifest to diff repository index digests and closure against")
    parser.add_argument("--coverage-manifest", type=Path,
                        help=f"hardware coverage manifest (default: {DEFAULT_COVERAGE_MANIFEST} when present)")
    parser.add_argument("--hardware-package-manifest", type=Path,
                        help="hardware package license/provenance manifest "
                             f"(default: {DEFAULT_HARDWARE_PACKAGE_MANIFEST} when present; a "
                             "--build-manifest that embedded one takes precedence)")
    parser.add_argument("--hardware-world", type=Path,
                        help="apks/world.hardware to fall back on when the build manifest "
                             f"recorded none (default: {DEFAULT_HARDWARE_WORLD} when present)")
    parser.add_argument("--validate-hardware", action="store_true",
                        help="scan the embedded hardware packages and exit non-zero when the "
                             f"offline hardware closure fails ({EXIT_VALIDATION_FAILED}) or "
                             f"could not be checked ({EXIT_VALIDATION_INCOMPLETE})")
    parser.add_argument("-o", "--output", type=Path, help="write JSON report here instead of stdout")
    args = parser.parse_args(argv)
    if not args.root.is_dir():
        parser.error(f"--root is not a directory: {args.root}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    if not args.validate_hardware:
        return 0
    section = report["sections"]["hardware_bundle"]
    code = hardware_validation_exit_code(section)
    if code == EXIT_VALIDATION_OK:
        print("[aios] hardware bundle validation passed", file=sys.stderr)
        return code
    if section.get("status") != "available":
        print(f"[aios] hardware bundle validation incomplete: {section.get('reason')}", file=sys.stderr)
        return code
    for check in section["checks"]:
        if check["status"] == "failed":
            print(f"[aios] FAILED {check['name']}: {check['description']}", file=sys.stderr)
            for failure in check["failures"]:
                print(f"[aios]   {json.dumps(failure, sort_keys=True)}", file=sys.stderr)
        elif check["status"] == "unavailable":
            print(f"[aios] INCOMPLETE {check['name']}: {check['reason']}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
