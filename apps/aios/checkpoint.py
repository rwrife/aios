"""Audit checkpoints of an installed AIOS system. Not a rollback mechanism.

A *checkpoint* is evidence: a manifest describing what this machine currently
runs — kernel release and Alpine release family, digests of the kernel image
and initramfs, an exact module inventory digest, and every installed package
version — together with a copy of the two boot artifacts it describes. It is
written under `/var/lib/aios/checkpoints/<id>/` and nowhere else.

What this module deliberately does **not** do, and what `status` says out loud:

- it does not switch what the machine boots. No GRUB file, default entry,
  bootloader pointer or `/boot` content is written or read for writing.
- it does not roll anything back. Restoring a previously recorded system
  coherently means restoring the kernel, its modules, firmware and the
  Mesa/Xorg stack together, which on an ext4 root needs a full-system snapshot
  or a signed package bundle. Neither exists here, and copying a kernel back
  over a mismatched module tree produces an unbootable machine, so no such
  operation is offered.
- it does not acquire packages. There is no download, repository, mirror, URL,
  package name or file path input anywhere in this surface.

User data is never read or written: every path touched is under the checkpoint
store.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from . import boot_mode
from .durable import (LockUnavailable, copy_durable, exclusive_lock, free_space_bytes,
                      fsync_directory, remove_stale_temporaries, remove_tree, write_atomic)
from .install_target import (HARDWARE_WORLD, MODULE_SUFFIXES, MODULES_ROOT, atom_name,
                             read_atoms, read_package_versions)

SCHEMA_VERSION = 1
MANIFEST_KIND = "aios-checkpoint-manifest"
STATUS_KIND = "aios-checkpoint-status"

STORE_ROOT = "var/lib/aios/checkpoints"
ALPINE_RELEASE = "etc/alpine-release"
MANIFEST_NAME = "manifest.json"

# Boot artifacts a checkpoint copies, and where they come from on a running
# installed system. Nothing else is copied.
ARTIFACT_SOURCES = {"vmlinuz": "boot/vmlinuz-lts", "initramfs": "boot/initramfs-lts"}
ARTIFACTS = tuple(sorted(ARTIFACT_SOURCES))

# Mesa/Xorg packages recorded alongside the kernel, because those are the
# versions a coherent restoration would have to move together with it.
GRAPHICS_PACKAGES = (
    "libdrm", "mesa", "mesa-dri-gallium", "mesa-egl", "mesa-gbm", "mesa-gl", "mesa-gles",
    "mesa-utils", "xf86-input-libinput", "xf86-video-fbdev", "xf86-video-modesetting",
    "xf86-video-vesa", "xinit", "xorg-server",
)

CHECKPOINT_ID = re.compile(r"^[0-9a-f]{64}$")
# Headroom for the manifest and filesystem metadata.
FREE_SPACE_MARGIN = 64 * 1024 * 1024
DIGEST_CHUNK = 1 << 20
SHORT_ID = 12

OPERATIONS = ("status", "verify", "stage")
# Stated in every status report so no reader can infer a capability from the
# existence of the store.
DISABLED_CAPABILITIES = {
    "activation_enabled": False,
    "rollback_enabled": False,
    "package_acquisition_enabled": False,
}
CAPABILITY_EXPLANATION = (
    "Checkpoints are an audit record only. Coherently restoring a previously recorded "
    "system requires a full-system snapshot or a signed package bundle that returns the "
    "kernel, its modules, firmware and the Mesa/Xorg stack together; that does not exist "
    "here, so boot activation, rollback and package acquisition are not implemented and "
    "this helper never changes what the machine boots."
)

EXIT_OK = 0
EXIT_ERROR = 1


class CheckpointError(RuntimeError):
    """A fixed checkpoint operation refused to run or could not complete."""


def _digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(DIGEST_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _digest_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(content):
    """The exact bytes an identifier is derived from."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"))


def derive_id(content):
    """Content-derived checkpoint identifier. Never supplied from outside."""
    return _digest_text(canonical(content))


def validate_id(value):
    if not isinstance(value, str) or not CHECKPOINT_ID.match(value):
        raise CheckpointError("A checkpoint identifier is a 64-character content digest.")
    return value


def short(checkpoint_id):
    return checkpoint_id[:SHORT_ID] if checkpoint_id else "none"


class Checkpoints:
    """Checkpoint store rooted at a system root. Production uses `/`."""

    def __init__(self, root="/"):
        self.root = Path(root)
        self.store = self.root / STORE_ROOT

    # --- reading -----------------------------------------------------------

    def kernel_release(self):
        modules = self.root / MODULES_ROOT
        releases = sorted(entry.name for entry in modules.iterdir() if entry.is_dir()) \
            if modules.is_dir() else []
        if len(releases) != 1:
            raise CheckpointError("A checkpoint needs exactly one installed kernel module release.")
        return releases[0]

    def alpine_branch(self):
        """The release family, e.g. `v3.23`, from /etc/alpine-release."""
        path = self.root / ALPINE_RELEASE
        if not path.is_file():
            raise CheckpointError("The Alpine release family could not be read from this system.")
        parts = path.read_text(encoding="utf-8", errors="replace").strip().split(".")
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise CheckpointError("The Alpine release family could not be read from this system.")
        return f"v{parts[0]}.{parts[1]}"

    def modules_inventory_digest(self, release):
        """Digest of the module inventory: sorted relative path and content.

        Contents are hashed, not sizes, so a substituted module of the same
        length changes the recorded identity.
        """
        directory = self.root / MODULES_ROOT / release
        entries = []
        for parent, _, filenames in os.walk(str(directory)):
            for name in sorted(filenames):
                if not name.endswith(MODULE_SUFFIXES):
                    continue
                path = Path(parent) / name
                relative = path.relative_to(directory).as_posix()
                entries.append(f"{relative} {_digest_file(path)}")
        return _digest_text("\n".join(sorted(entries))), len(entries)

    def package_sets(self):
        versions = read_package_versions(self.root)
        hardware_world = self.root / HARDWARE_WORLD
        atoms = {atom_name(atom) for atom in read_atoms(hardware_world)} \
            if hardware_world.is_file() else set()
        missing = sorted(atoms - set(versions))
        if missing:
            raise CheckpointError("The hardware package closure is incomplete on this system; "
                                  f"{len(missing)} selected packages are not installed.")
        closure = _digest_text("\n".join(f"{name}={versions[name]}" for name in sorted(versions)))
        return {
            "hardware_packages": {name: versions[name] for name in sorted(atoms)},
            "graphics_packages": {name: versions[name] for name in GRAPHICS_PACKAGES
                                  if name in versions},
            "package_closure_digest": closure,
            "package_count": len(versions),
        }

    def describe_running_system(self, today=None):
        """Manifest content plus identifier for the running installed system."""
        release = self.kernel_release()
        modules_digest, module_count = self.modules_inventory_digest(release)
        artifacts = {}
        for name, relative in sorted(ARTIFACT_SOURCES.items()):
            path = self.root / relative
            if not path.is_file() or path.stat().st_size == 0:
                raise CheckpointError(f"This system has no usable /{relative} to record.")
            artifacts[name] = {"sha256": _digest_file(path), "size": path.stat().st_size}
        content = {
            "kind": MANIFEST_KIND,
            "schema_version": SCHEMA_VERSION,
            "alpine_branch": self.alpine_branch(),
            "kernel_release": release,
            "artifacts": artifacts,
            "modules_inventory_digest": modules_digest,
            "module_count": module_count,
            **self.package_sets(),
        }
        manifest = dict(content)
        manifest["checkpoint_id"] = derive_id(content)
        manifest["created"] = (today or datetime.date.today()).isoformat()
        # A checkpoint is an image-level record. Devices stay untested until a
        # physical machine is revalidated against it.
        manifest["physical_revalidation_required"] = True
        # Restated inside every stored manifest so a copied file cannot be
        # mistaken for a restorable system image.
        manifest["restorable"] = False
        return manifest

    def directory(self, checkpoint_id):
        return self.store / validate_id(checkpoint_id)

    def manifest_path(self, checkpoint_id):
        return self.directory(checkpoint_id) / MANIFEST_NAME

    def read_manifest(self, checkpoint_id):
        path = self.manifest_path(checkpoint_id)
        if not path.is_file():
            raise CheckpointError(f"Checkpoint {short(checkpoint_id)} is not recorded on this system.")
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise CheckpointError(f"Checkpoint {short(checkpoint_id)} has an unreadable manifest.")
        if not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND:
            raise CheckpointError(f"Checkpoint {short(checkpoint_id)} has an unrecognized manifest.")
        content = {key: value for key, value in manifest.items()
                   if key not in ("checkpoint_id", "created", "physical_revalidation_required",
                                  "restorable")}
        if derive_id(content) != checkpoint_id:
            raise CheckpointError(f"Checkpoint {short(checkpoint_id)} does not match its recorded content.")
        return manifest

    def recorded_ids(self):
        if not self.store.is_dir():
            return []
        return sorted(entry.name for entry in self.store.iterdir()
                      if entry.is_dir() and CHECKPOINT_ID.match(entry.name)
                      and (entry / MANIFEST_NAME).is_file())

    def verify_artifacts(self, checkpoint_id):
        """Re-read a checkpoint's stored artifacts against its manifest."""
        manifest = self.read_manifest(checkpoint_id)
        mismatched = []
        for name in ARTIFACTS:
            path = self.directory(checkpoint_id) / name
            recorded = manifest["artifacts"][name]
            if not path.is_file() or path.stat().st_size != recorded["size"]:
                mismatched.append(name)
            elif _digest_file(path) != recorded["sha256"]:
                mismatched.append(name)
        return {"checkpoint_id": checkpoint_id, "artifacts_verified": not mismatched,
                "mismatched_artifacts": mismatched}

    # --- maintenance -------------------------------------------------------

    def reclaim(self):
        """Delete what an interrupted or abandoned run left behind.

        Two kinds of leftovers exist: temporary names from a durable write, and
        checkpoint directories whose manifest was never written or no longer
        matches their own identifier. Neither is ever treated as a checkpoint,
        so removing them cannot lose a valid record. Callers hold the store
        lock, so no concurrent run can be writing what is being reclaimed.
        """
        removed = remove_stale_temporaries(self.store, recursive=True)
        orphans = []
        if self.store.is_dir():
            for entry in sorted(self.store.iterdir()):
                if not entry.is_dir() or entry.is_symlink():
                    continue
                if not CHECKPOINT_ID.match(entry.name):
                    orphans.append(entry.name)
                    remove_tree(entry)
                    continue
                try:
                    self.read_manifest(entry.name)
                except CheckpointError:
                    orphans.append(entry.name)
                    remove_tree(entry)
        return {"removed_temporaries": removed, "removed_incomplete": orphans}

    # --- operations --------------------------------------------------------

    def _require_installed(self):
        if not boot_mode.installed(str(self.root / boot_mode.MODE_FILE.lstrip("/"))):
            raise CheckpointError("Checkpoints exist only on an installed system; a live session "
                                  "has nothing durable to record.")

    def status(self):
        """Read-only inventory of what has been recorded, and of what is not
        implemented. Nothing here is a restoration capability."""
        report = {
            "kind": STATUS_KIND,
            "schema_version": SCHEMA_VERSION,
            "mode": boot_mode.read_mode(str(self.root / boot_mode.MODE_FILE.lstrip("/"))),
            "recorded": len(self.recorded_ids()),
            "checkpoints": [],
            "capability_note": CAPABILITY_EXPLANATION,
            **DISABLED_CAPABILITIES,
        }
        for checkpoint_id in self.recorded_ids():
            try:
                manifest = self.read_manifest(checkpoint_id)
            except CheckpointError as error:
                report["checkpoints"].append({"checkpoint_id": checkpoint_id, "error": str(error)})
                continue
            # Sanitized: content digests, versions and counts only. No device
            # names, serial numbers, file paths or user data.
            report["checkpoints"].append({
                "checkpoint_id": checkpoint_id,
                "alpine_branch": manifest["alpine_branch"],
                "kernel_release": manifest["kernel_release"],
                "modules_inventory_digest": manifest["modules_inventory_digest"],
                "module_count": manifest["module_count"],
                "package_closure_digest": manifest["package_closure_digest"],
                "package_count": manifest["package_count"],
                "hardware_package_count": len(manifest["hardware_packages"]),
                "graphics_packages": manifest["graphics_packages"],
                "created": manifest["created"],
                "physical_revalidation_required": manifest["physical_revalidation_required"],
                "restorable": False,
            })
        return report

    def verify(self):
        """Re-read every recorded checkpoint against its own manifest."""
        report = self.status()
        report["verification"] = []
        for checkpoint_id in self.recorded_ids():
            try:
                report["verification"].append(self.verify_artifacts(checkpoint_id))
            except CheckpointError as error:
                report["verification"].append({"checkpoint_id": checkpoint_id, "error": str(error),
                                               "artifacts_verified": False})
        report["verified"] = bool(report["verification"]) and all(
            result.get("artifacts_verified") for result in report["verification"])
        return report

    def stage(self, today=None):
        """Record the running installed system as an audit checkpoint."""
        self._require_installed()
        try:
            with exclusive_lock(self.store):
                return self._stage_locked(today)
        except LockUnavailable as error:
            raise CheckpointError(str(error))

    def _stage_locked(self, today=None):
        reclaimed = self.reclaim()
        manifest = self.describe_running_system(today=today)
        checkpoint_id = manifest["checkpoint_id"]
        directory = self.directory(checkpoint_id)
        required = sum(artifact["size"] for artifact in manifest["artifacts"].values())
        if self.manifest_path(checkpoint_id).is_file():
            result = self.verify_artifacts(checkpoint_id)
            if result["artifacts_verified"]:
                return {"staged": False, "already_recorded": True,
                        "checkpoint_id": checkpoint_id, **reclaimed}
        available = free_space_bytes(self.store)
        if available < required + FREE_SPACE_MARGIN:
            raise CheckpointError(f"Recording this checkpoint needs {required + FREE_SPACE_MARGIN} "
                                  f"bytes of free space and {available} are available.")
        directory.mkdir(parents=True, exist_ok=True)
        try:
            for name, relative in sorted(ARTIFACT_SOURCES.items()):
                copy_durable(self.root / relative, directory / name)
            fsync_directory(directory)
            # The manifest is written last, so an interrupted run leaves an
            # incomplete directory that no operation treats as a checkpoint and
            # that the next run reclaims.
            write_atomic(self.manifest_path(checkpoint_id),
                         json.dumps(manifest, indent=2, sort_keys=True))
            verification = self.verify_artifacts(checkpoint_id)
            if not verification["artifacts_verified"]:
                raise CheckpointError("The recorded checkpoint's artifacts did not read back correctly.")
        except (CheckpointError, OSError):
            # Nothing partial is left behind: the directory being written here
            # is either a fresh one or a previously invalid one that reclaim
            # would remove anyway.
            remove_stale_temporaries(directory)
            if directory.is_dir():
                remove_tree(directory)
            raise
        return {"staged": True, "already_recorded": False, "checkpoint_id": checkpoint_id,
                "bytes_written": required, "restorable": False, **reclaimed}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aios-checkpoint",
        description="Root-only audit checkpoints of the installed system. "
                    "This is not a rollback mechanism and never changes what the machine boots.")
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in OPERATIONS:
        operations.add_parser(name)
    arguments = parser.parse_args(argv)
    if os.geteuid() != 0:
        print(json.dumps({"error": "The checkpoint helper requires root."}), file=sys.stderr)
        return EXIT_ERROR
    checkpoints = Checkpoints()
    try:
        result = getattr(checkpoints, arguments.operation)()
    except CheckpointError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return EXIT_ERROR
    except OSError as error:
        print(json.dumps({"error": f"The checkpoint operation failed: {error.strerror}."}),
              file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(result, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
