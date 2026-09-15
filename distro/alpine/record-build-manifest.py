#!/usr/bin/env python3
"""Record a build/package-closure manifest next to a built AIOS ISO.

This is the recorded-evidence half of the stage-1 reproducibility story
(docs/qa/hardware-coverage.md). Alpine's ``v3.23`` main/community
repositories are *moving* repositories: nothing in this repository freezes
which package versions they serve. Instead of claiming a pin that does not
exist, every build records:

* the immutable source pins actually used (``distro/alpine/build.env``
  container digest, aports/llama.cpp/whisper.cpp revisions), including
  whether any of them were overridden in the build environment,
* the effective repository URLs, plus a post-build SHA-256 sample of each
  repository's architecture-specific ``APKINDEX.tar.gz``,
* the exact package closure embedded in the ISO (filename, size, SHA-256),
* the ISO hash and whatever kernel/modloop identity the artifacts expose,
* the package atoms every ``apks/world.*`` file requested (including the
  stage-2 hardware world) plus the license/provenance record for the selected
  hardware packages, so an extracted ISO can be checked offline,
* size metrics for the ISO, modloop, initramfs, kernel, embedded APKs and
  apkovl. Figures that need a booted machine (live root usage, boot time,
  minimum tested RAM) are recorded as ``not_measured``, never estimated.

The exact embedded APK closure is authoritative for what shipped. The
post-build index sample helps diagnose repository movement, but it is not
proof that the repository index stayed unchanged throughout the build. This
does not make package resolution predetermined or the ISO byte-reproducible.
Remaining nondeterminism is listed in the manifest's ``reproducibility``
section.

The manifest is plain JSON written with sorted keys so two builds can be
diffed directly.

Usage (normally invoked by distro/alpine/mkimage.sh):

    python3 record-build-manifest.py --iso out/aios-...iso \\
        --arch x86_64 --release-tag 20260101 \\
        --build-env distro/alpine/build.env \\
        --repository main=https://.../v3.23/main \\
        --repository community=https://.../v3.23/community \\
        --output out/aios-...iso.build-manifest.json

``--extracted-root`` skips the xorriso extraction and reads an already
extracted ISO tree instead; this is what the tests use.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1
CANONICAL_REPO_BASE = "https://dl-cdn.alpinelinux.org/alpine"
PIN_KEYS = ("ALPINE_BRANCH", "IMAGE", "APORTS_REF", "LLAMA_REF", "WHISPER_REF")
IMMUTABLE_PIN_KEYS = ("IMAGE", "APORTS_REF", "LLAMA_REF", "WHISPER_REF")

REPRODUCIBILITY_NOTES = [
    "This manifest records what a build resolved; it does not make the ISO "
    "byte-reproducible and does not pin the moving Alpine repositories.",
    "Alpine main/community for a release branch keep moving. The exact "
    "embedded APK closure records what shipped. The APKINDEX SHA-256 values "
    "are post-build samples that help diagnose repository movement; they do "
    "not prove which index state apk observed during the build.",
    "Known remaining nondeterminism: build timestamps, generated SSH host "
    "keys (--hostkeys), apkovl/squashfs/ISO layout and compression metadata, "
    "the modloop signing key, and locally built AIOS application binaries.",
    "Rebuilding the same ISO bytes would additionally require an immutable "
    "repository snapshot (or a local mirror of the recorded APK closure), "
    "which this build does not use.",
]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_env_file(text: str) -> dict:
    """Parse simple KEY=VALUE lines as used by distro/alpine/build.env."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def parse_pairs(items: list[str], what: str) -> dict:
    pairs = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"invalid {what} (expected KEY=VALUE): {item!r}")
        pairs[key.strip()] = value.strip()
    return pairs


def apkindex_url(repo_url: str, arch: str) -> str:
    return f"{repo_url.rstrip('/')}/{arch}/APKINDEX.tar.gz"


def fetch_index(url: str, timeout: int) -> dict:
    """Fetch an APKINDEX.tar.gz and hash it, or report why it is unavailable.

    Never fabricates a digest: a failed fetch is recorded as
    ``{"status": "unavailable", "reason": ...}``.
    """
    try:
        if url.startswith(("http://", "https://")):
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                data = response.read()
        else:
            local = url[len("file://"):] if url.startswith("file://") else url
            data = Path(local).read_bytes()
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"status": "unavailable", "reason": f"{type(error).__name__}: {error}"}
    return {
        "status": "recorded",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def run_xorriso_extract(iso: Path, iso_path: str, dest: Path) -> str | None:
    """Extract one ISO subtree/file. Returns an error string, or None on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "xorriso", "-osirrox", "on", "-indev", str(iso),
        "-extract", iso_path, str(dest),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        return f"could not run xorriso: {error}"
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()
        return f"xorriso failed for {iso_path}: {tail[-1] if tail else 'unknown error'}"
    if not dest.exists():
        return f"xorriso reported success but {iso_path} was not extracted"
    return None


def parse_world_file(text: str) -> list[str]:
    """Parse an apks/world.* file into sorted package atoms.

    Comment and blank lines are stripped exactly the way
    profiles/mkimg.aios.sh and apkovl/genapkovl-aios.sh strip them, so the
    recorded set is the set those scripts actually request.
    """
    atoms = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        atoms.add(line)
    return sorted(atoms)


def collect_worlds(items: list[str]) -> dict:
    """Record every world file this build requested, by role.

    The hardware world is what makes live and installed systems carry the same
    offline firmware/diagnostic set, so it is recorded as built evidence rather
    than inferred from the repository checkout later.
    """
    worlds = []
    combined: set[str] = set()
    for item in items or []:
        role, sep, raw_path = item.partition("=")
        if not sep or not role.strip():
            raise SystemExit(f"invalid --world (expected ROLE=PATH): {item!r}")
        path = Path(raw_path.strip())
        if not path.is_file():
            worlds.append({
                "role": role.strip(),
                "path": path.name,
                "status": "unavailable",
                "reason": f"world file not found: {path}",
                "packages": [],
            })
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        packages = parse_world_file(text)
        combined.update(packages)
        worlds.append({
            "role": role.strip(),
            "path": path.name,
            "status": "recorded",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "package_count": len(packages),
            "packages": packages,
        })
    return {
        "kind": "requested-world-package-sets",
        "description": (
            "Top-level package atoms each apks/world.* file requested for this build. "
            "The live root installs this same set from the ISO's /apks repository, and "
            "setup-disk reuses it for an installed system."
        ),
        "worlds": sorted(worlds, key=lambda entry: entry["role"]),
        "combined_packages": sorted(combined),
    }


def collect_hardware_packages(path: Path | None) -> dict:
    """Embed the license/provenance record for the selected hardware packages.

    Copying the compact per-package record (repository, resolved version,
    license, covered coverage-manifest entries) into the per-ISO manifest keeps
    offline artifact inspection self-contained: an extracted ISO plus its
    manifest is enough to check licenses and provenance without this
    repository.
    """
    if path is None:
        return {"status": "unavailable", "reason": "no --hardware-package-manifest given"}
    if not path.is_file():
        return {"status": "unavailable", "reason": f"hardware package manifest not found: {path}"}
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    try:
        manifest = json.loads(text)
    except ValueError as error:
        return {"status": "unavailable", "reason": f"could not parse {path.name}: {error}"}
    packages = {}
    for name, record in (manifest.get("packages") or {}).items():
        packages[name] = {
            "role": record.get("role"),
            "repository": record.get("repository"),
            "version_resolved": record.get("version_resolved"),
            "license": record.get("license"),
            "covers": record.get("covers", []),
        }
    return {
        "status": "recorded",
        "source": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "alpine_branch": manifest.get("alpine_branch"),
        "world_file": manifest.get("world_file"),
        "resolution": manifest.get("resolution", {}).get("status"),
        "resolved_on": manifest.get("resolution", {}).get("resolved_on"),
        "packages": packages,
    }


def measured(size: int | None, note: str | None = None) -> dict:
    record = {"status": "recorded", "bytes": size}
    if note:
        record["note"] = note
    return record


def not_measured(reason: str) -> dict:
    return {"status": "not_measured", "reason": reason}


def iso_member_size(iso: Path, name: str) -> dict:
    """Size one top-level ISO file without extracting it.

    Used for the apkovl, which is large enough that copying it out of the ISO
    just to measure it would slow every build down.
    """
    if shutil.which("xorriso") is None:
        return {"status": "unavailable", "reason": "xorriso not available to list the ISO"}
    try:
        result = subprocess.run(
            ["xorriso", "-osirrox", "on", "-indev", str(iso), "-lsl", "/"],
            capture_output=True, text=True, check=False,
        )
    except OSError as error:
        return {"status": "unavailable", "reason": f"could not run xorriso: {error}"}
    if result.returncode != 0:
        return {"status": "unavailable", "reason": f"xorriso could not list {iso.name}"}
    pattern = re.compile(r"^\S+\s+\d+\s+\S+\s+\S+\s+(\d+)\s+.*?'(.+)'\s*$")
    for line in result.stdout.splitlines():
        match = pattern.match(line.strip())
        if match and match.group(2) == name:
            return {"status": "recorded", "bytes": int(match.group(1)), "path": name}
    return {"status": "unavailable", "reason": f"{name} not found in the ISO root listing"}


def collect_closure(apks_root: Path, prefix: str = "apks") -> dict:
    """Hash every embedded .apk. This is the exact output closure evidence."""
    packages = []
    total = 0
    for apk in sorted(apks_root.rglob("*.apk")):
        if not apk.is_file():
            continue
        size = apk.stat().st_size
        total += size
        packages.append({
            "filename": apk.name,
            "path": f"{prefix}/{apk.relative_to(apks_root).as_posix()}",
            "sha256": sha256_path(apk),
            "size": size,
        })
    return {
        "status": "recorded",
        "apk_count": len(packages),
        "total_size": total,
        "packages": packages,
    }


def file_identity(path: Path, name: str) -> dict:
    if not path.is_file():
        return {"status": "unavailable", "reason": f"{name} not present in the ISO"}
    return {
        "status": "recorded",
        "path": name,
        "sha256": sha256_path(path),
        "size": path.stat().st_size,
    }


def modloop_identity(modloop: Path) -> dict:
    """Record modloop kernel-version directories without extracting the squashfs."""
    if not modloop.is_file():
        return {"status": "unavailable", "reason": "no modloop file extracted from the ISO"}
    if shutil.which("unsquashfs") is None:
        return {"status": "unavailable", "reason": "unsquashfs not available to list modloop contents"}
    try:
        result = subprocess.run(
            ["unsquashfs", "-l", str(modloop)],
            capture_output=True, text=True, check=False,
        )
    except OSError as error:
        return {"status": "unavailable", "reason": f"could not run unsquashfs: {error}"}
    if result.returncode != 0:
        return {"status": "unavailable", "reason": "unsquashfs could not list the modloop"}
    versions = set()
    for line in result.stdout.splitlines():
        entry = line.strip()
        for root in ("squashfs-root/modules/", "squashfs-root/lib/modules/"):
            if entry.startswith(root):
                rest = entry[len(root):].split("/")[0]
                if rest and rest != "firmware":
                    versions.add(rest)
    return {"status": "recorded", "module_versions": sorted(versions)}


def remove_extraction_tree(root: Path) -> None:
    """Remove our private Linux extraction tree, never chmod symlink targets.

    Older TemporaryDirectory permission-recovery handlers can follow symlinks.
    Normalize only directories via no-follow, descriptor-relative operations,
    then use rmtree without permission recovery. Unsupported operations or other
    cleanup errors propagate; never retry with symlink-following chmod.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def writable_directories(fd: int) -> None:
        os.fchmod(fd, 0o700)
        with os.scandir(fd) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                os.chmod(entry.name, 0o700, dir_fd=fd, follow_symlinks=False)
                child = os.open(entry.name, flags, dir_fd=fd)
                try:
                    writable_directories(child)
                finally:
                    os.close(child)

    fd = os.open(root, flags)
    try:
        writable_directories(fd)
    finally:
        os.close(fd)
    shutil.rmtree(root)


def build_manifest(args: argparse.Namespace) -> dict:
    # ExitStack also cleans up if extraction, hashing or manifest assembly fails.
    with ExitStack() as cleanup:
        return _build_manifest(args, cleanup)


def _build_manifest(args: argparse.Namespace, cleanup: ExitStack) -> dict:
    file_pins = {}
    build_env_sha = None
    if args.build_env and args.build_env.is_file():
        raw = args.build_env.read_bytes()
        file_pins = parse_env_file(raw.decode("utf-8"))
        build_env_sha = hashlib.sha256(raw).hexdigest()

    effective = parse_pairs(args.effective, "--effective")
    settings = parse_pairs(args.build_setting, "--build-setting")
    repositories = parse_pairs(args.repository, "--repository")

    source_pins = {}
    pin_overrides = {}
    for key in sorted(set(PIN_KEYS) | set(file_pins) | set(effective)):
        recorded = effective.get(key, file_pins.get(key))
        source_pins[key] = recorded
        if key in file_pins and key in effective and file_pins[key] != effective[key]:
            pin_overrides[key] = {"build_env_file": file_pins[key], "effective": effective[key]}

    branch = source_pins.get("ALPINE_BRANCH")
    repo_entries = []
    for role in sorted(repositories):
        url = repositories[role]
        canonical = f"{args.canonical_repo_base.rstrip('/')}/{branch}/{role}" if branch else None
        index_url = apkindex_url(url, args.arch)
        index = fetch_index(index_url, args.index_timeout) if not args.skip_index_fetch else {
            "status": "unavailable",
            "reason": "index fetch explicitly skipped (--skip-index-fetch)",
        }
        repo_entries.append({
            "role": role,
            "url": url,
            "canonical_url": canonical,
            "source": "default" if canonical is not None and url == canonical else "override",
            "apkindex_url": index_url,
            "apkindex": index,
        })

    root = args.extracted_root
    extraction_errors = []
    if root is None:
        work = args.work_dir
        if work is not None:
            work.mkdir(parents=True, exist_ok=True)
        # xorriso preserves restrictive directory modes from the ISO. Reusing
        # one fixed extraction path in the persistent build cache can therefore
        # make the next build unable to remove a prior tree. Use an isolated
        # directory for each recording instead.
        root = Path(tempfile.mkdtemp(prefix="iso-", dir=work))
        if not args.keep_extraction:
            cleanup.callback(remove_extraction_tree, root)
        error = run_xorriso_extract(args.iso, "/apks", root / "apks")
        if error:
            extraction_errors.append(error)
        for boot_file in ("vmlinuz-lts", "modloop-lts", "initramfs-lts"):
            error = run_xorriso_extract(args.iso, f"/boot/{boot_file}", root / "boot" / boot_file)
            if error:
                extraction_errors.append(error)

    apks_root = root / "apks"
    if apks_root.is_dir():
        closure = collect_closure(apks_root)
    else:
        closure = {
            "status": "unavailable",
            "reason": "; ".join(extraction_errors) or "no apks/ directory in the ISO",
            "apk_count": None,
            "total_size": None,
            "packages": [],
        }

    artifacts = {
        "iso": {
            "status": "recorded",
            "filename": args.iso.name,
            "sha256": sha256_path(args.iso),
            "size": args.iso.stat().st_size,
        } if args.iso.is_file() else {"status": "unavailable", "reason": "ISO file not found"},
        "kernel": file_identity(root / "boot" / "vmlinuz-lts", "boot/vmlinuz-lts"),
        "initramfs": file_identity(root / "boot" / "initramfs-lts", "boot/initramfs-lts"),
        "modloop": file_identity(root / "boot" / "modloop-lts", "boot/modloop-lts"),
    }
    kernel_identity = modloop_identity(root / "boot" / "modloop-lts")

    apkovl_in_root = sorted(root.glob("*.apkovl.tar.gz"))
    if apkovl_in_root:
        apkovl_metric = measured(apkovl_in_root[0].stat().st_size)
        apkovl_metric["path"] = apkovl_in_root[0].name
    elif args.iso.is_file():
        apkovl_metric = iso_member_size(args.iso, args.apkovl_name)
    else:
        apkovl_metric = {"status": "unavailable", "reason": "no ISO to read the apkovl size from"}

    def artifact_metric(key: str) -> dict:
        record = artifacts.get(key) or {}
        if record.get("status") == "recorded":
            return measured(record.get("size"))
        return {"status": "unavailable", "reason": record.get("reason", f"{key} not recorded")}

    metrics = {
        "kind": "build-size-metrics",
        "description": (
            "Sizes measured from this build's artifacts. Runtime figures that need a "
            "booted machine are reported as not_measured rather than estimated."
        ),
        "iso": artifact_metric("iso"),
        "modloop": artifact_metric("modloop"),
        "initramfs": artifact_metric("initramfs"),
        "kernel": artifact_metric("kernel"),
        "embedded_apks": (
            measured(closure.get("total_size"), f"{closure.get('apk_count')} packages")
            if closure.get("status") == "recorded"
            else {"status": "unavailable", "reason": closure.get("reason")}
        ),
        "apkovl": apkovl_metric,
        "live_root": not_measured(
            "live root usage depends on the tmpfs a booted machine allocates "
            "(rootflags=size=75%); it cannot be read from the ISO"
        ),
        "boot_time": not_measured("no boot was performed by this build"),
        "minimum_tested_ram": not_measured(
            "no physical or VM RAM floor has been measured for this image"
        ),
    }

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": "aios-build-manifest",
        "generated_utc": args.generated_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build": {
            "arch": args.arch,
            "release_tag": args.release_tag,
            "build_env_file": args.build_env.name if args.build_env else None,
            "build_env_sha256": build_env_sha,
            "settings": settings,
        },
        "source_pins": {
            "kind": "source-selection",
            "description": (
                "IMAGE and git revision values are immutable source pins. "
                "ALPINE_BRANCH selects moving package repositories and is not an "
                "immutable package-resolution pin."
            ),
            "values": source_pins,
            "immutable_keys": list(IMMUTABLE_PIN_KEYS),
            "moving_selection_keys": ["ALPINE_BRANCH"],
            "overrides": pin_overrides,
        },
        "repository_indexes": {
            "kind": "post-build-moving-repository-index-sample",
            "description": (
                "Alpine release repositories keep moving. The APKINDEX SHA-256 "
                "recorded here is sampled after mkimage finishes. It helps identify "
                "repository movement but does not prove the index stayed unchanged "
                "while apk resolved the build."
            ),
            "sampling": "post-build",
            "arch": args.arch,
            "repositories": repo_entries,
        },
        "output_closure": {
            "kind": "exact-output-closure",
            "description": (
                "Every .apk actually embedded in the ISO, with size and SHA-256. "
                "This is the authoritative record of what a release shipped."
            ),
            **closure,
        },
        "artifacts": artifacts,
        "kernel_identity": kernel_identity,
        "metrics": metrics,
        "package_worlds": collect_worlds(args.world),
        "hardware_packages": collect_hardware_packages(args.hardware_package_manifest),
        "reproducibility": {
            "byte_reproducible": False,
            "package_resolution": "output-closure-recorded-repository-index-sampled",
            "notes": REPRODUCIBILITY_NOTES,
        },
    }
    if extraction_errors:
        manifest["artifacts"]["extraction_errors"] = sorted(set(extraction_errors))
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iso", required=True, type=Path, help="built ISO to record")
    parser.add_argument("--arch", default="x86_64", help="target architecture (APKINDEX path component)")
    parser.add_argument("--release-tag", default=None, help="release tag used for this build")
    parser.add_argument("--build-env", type=Path, help="distro/alpine/build.env used for this build")
    parser.add_argument("--effective", action="append", default=[],
                        help="KEY=VALUE effective pin value (repeatable); overrides are recorded")
    parser.add_argument("--build-setting", action="append", default=[],
                        help="KEY=VALUE build setting to record verbatim (repeatable)")
    parser.add_argument("--repository", action="append", default=[],
                        help="ROLE=URL repository actually passed to mkimage (repeatable)")
    parser.add_argument("--world", action="append", default=[],
                        help="ROLE=PATH apks/world.* file requested by this build (repeatable)")
    parser.add_argument("--hardware-package-manifest", type=Path,
                        help="docs/qa/hardware-packages.json to embed license/provenance from")
    parser.add_argument("--apkovl-name", default="aios.apkovl.tar.gz",
                        help="apkovl filename in the ISO root, used for the size metric")
    parser.add_argument("--canonical-repo-base", default=CANONICAL_REPO_BASE,
                        help="base URL treated as the unoverridden default")
    parser.add_argument("--extracted-root", type=Path,
                        help="already extracted ISO tree; skips the xorriso extraction")
    parser.add_argument("--work-dir", type=Path, help="scratch directory for ISO extraction")
    parser.add_argument("--keep-extraction", action="store_true",
                        help="keep the extracted ISO tree instead of deleting it")
    parser.add_argument("--skip-index-fetch", action="store_true",
                        help="record repository indexes as unavailable instead of fetching them")
    parser.add_argument("--index-timeout", type=int, default=60, help="APKINDEX fetch timeout in seconds")
    parser.add_argument("--generated-utc", default=None, help="override the generation timestamp")
    parser.add_argument("--packages-txt", type=Path, help="also write a sorted closure filename list here")
    parser.add_argument("-o", "--output", type=Path, required=True, help="manifest JSON output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = build_manifest(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.packages_txt:
        names = sorted(entry["filename"] for entry in manifest["output_closure"]["packages"])
        args.packages_txt.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
    closure = manifest["output_closure"]
    count = closure.get("apk_count")
    print(f"[aios] recorded build manifest: {args.output} "
          f"({count if count is not None else 'no'} packages)", file=sys.stderr)
    if closure["status"] != "recorded":
        print(f"[aios] warning: package closure not recorded: {closure.get('reason')}", file=sys.stderr)
    for repo in manifest["repository_indexes"]["repositories"]:
        if repo["apkindex"]["status"] != "recorded":
            print(f"[aios] warning: APKINDEX not recorded for {repo['role']}: "
                  f"{repo['apkindex'].get('reason')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
