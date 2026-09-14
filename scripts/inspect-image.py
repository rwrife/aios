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
from pathlib import Path

SCHEMA_VERSION = 2

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COVERAGE_MANIFEST = REPO_ROOT / "docs" / "qa" / "hardware-coverage.json"
CANONICAL_REPO_BASE = "https://dl-cdn.alpinelinux.org/alpine"

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


def pci_modalias_probe(pci_id: str) -> str:
    """Build a modalias probe string for a concrete `vendor:device` PCI ID.

    A real PCI modalias is
    `pci:v<VENDOR>d<DEVICE>sv<SUBVENDOR>sd<SUBDEVICE>bc<CLASS>sc<SUBCLASS>i<IFACE>`.
    The coverage manifest records vendor/device only, so subsystem and class
    fields stay wildcards; matching is done in both directions (see
    `alias_matches_probe`) so a subsystem- or class-specific alias is not
    silently missed.
    """
    vendor, _, device = pci_id.partition(":")
    return f"pci:v0000{vendor.upper()}d0000{device.upper()}sv*sd*bc*sc*i*"


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
        for bus, key, probe_builder in (
            ("pci", "pci_id", pci_modalias_probe),
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
    for device in expectations["devices"]:
        matched_aliases = [
            entry for entry in aliases if alias_matches_probe(entry["alias"], device["probe"])
        ]
        matched_modules = sorted({module_key(entry["module"]) for entry in matched_aliases})
        expected = sorted(set(device["expected_modules"]))
        unexpected = sorted(set(matched_modules) - set(expected))
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
            "matched_aliases": sorted(
                ({"alias": entry["alias"], "module": entry["module"]} for entry in matched_aliases),
                key=lambda item: (item["module"], item["alias"]),
            ),
            "unexpected_modules": unexpected,
            "expected_modules_without_matching_alias": missing_expected,
            "status": "matched" if matched_modules else "unmatched",
        }
        devices.append(record)
        if not matched_modules:
            unmatched.append({"id": device["id"], "device_id": device["device_id"], "probe": device["probe"]})
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
        "matched_id_count": sum(1 for device in devices if device["status"] == "matched"),
        "checked_id_count": len(devices),
        "relevant_aliases": sorted(
            relevant_aliases, key=lambda entry: (entry["module"], entry["alias"])
        ),
        "relevant_alias_count": len(relevant_aliases),
        "coverage_modules_without_any_alias": sorted(relevant_modules - alias_modules),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
