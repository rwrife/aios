"""Reading an installed initramfs and the configuration it was built from.

`mkinitfs` builds `/boot/initramfs-lts` from the features listed in a
`mkinitfs.conf` and the module globs in `features.d/<feature>.modules`. On an
installed system that configuration is the one `setup-disk` generated for the
target's own root filesystem and boot controller, so it is also the only
correct statement of what the installed initramfs must contain.

This module parses both sides with fixed rules — no shell, no subprocess, no
configurable path — so the installer's readback can assert that the image it
just produced really carries the modules that root would not mount without.
A size or timestamp is never accepted as evidence.
"""
import gzip
import io
import os
from pathlib import Path

MKINITFS_CONFIG = "etc/mkinitfs/mkinitfs.conf"
FEATURES_DIRECTORY = "etc/mkinitfs/features.d"
MODULES_ROOT = "lib/modules"
# Alpine's merged-/usr layout means the same module tree is reachable as
# `lib/modules` and `usr/lib/modules`, and mkinitfs writes whichever one the
# kernel package used. Both are the same module to this readback.
MODULE_PREFIXES = (MODULES_ROOT, f"usr/{MODULES_ROOT}")
MODULE_SUFFIX = ".ko"
# A module is the same module whether or not the kernel package compressed it.
# The suffix is stripped on both sides of the comparison so an installed
# `ext4.ko.gz` satisfies a configuration that selected `ext4.ko`.
COMPRESSION_SUFFIXES = (".gz", ".xz", ".zst")
CPIO_MAGIC = (b"070701", b"070702")
CPIO_HEADER = 110
CPIO_TRAILER = "TRAILER!!!"
GZIP_MAGIC = b"\x1f\x8b"
# mkinitfs defaults to gzip. Any other container is reported as missing
# evidence rather than guessed at, which withholds installation success.
COMPRESSION_NAMES = {b"\x28\xb5\x2f\xfd": "zstd", b"\xfd7zXZ": "xz",
                     b"BZh": "bzip2", b"\x04\x22\x4d\x18": "lz4",
                     b"\x02!L\x18": "lz4-legacy"}
MAX_ENTRIES = 200000


class InitramfsError(RuntimeError):
    """An initramfs or its configuration could not be read."""


def read_features(config_path):
    """Feature names from a `mkinitfs.conf`.

    `features="ata base ext4 nvme"` is the only assignment mkinitfs reads, and
    the last one in the file wins, exactly as the shell would evaluate it.
    """
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise InitramfsError(f"The target initramfs configuration could not be read: "
                             f"{error.strerror}.")
    features = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("features="):
            continue
        value = stripped[len("features="):].strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        features = value.split()
    if features is None:
        raise InitramfsError("The target initramfs configuration lists no features.")
    return features


def logical_module(relative):
    """A module path without its outer compression suffix."""
    for suffix in COMPRESSION_SUFFIXES:
        if relative.endswith(suffix):
            return relative[:-len(suffix)]
    return relative


def _expand(directory, pattern):
    """Files a mkinitfs module glob selects, relative to the module directory.

    mkinitfs walks a matched directory and takes a matched file as-is; both
    forms are reproduced here so the expectation is the tool's own behavior.
    """
    selected = []
    for match in sorted(Path(directory).glob(pattern.lstrip("/"))):
        if match.is_dir() and not match.is_symlink():
            for parent, _, filenames in os.walk(str(match)):
                for name in sorted(filenames):
                    selected.append(Path(parent) / name)
        elif match.exists():
            selected.append(match)
    return [logical_module(path.relative_to(directory).as_posix()) for path in selected]


def expected_modules(root, release, features):
    """`(modules, features_without_definition)` for one target root.

    `modules` are paths relative to `lib/modules/<release>`, with the
    compression suffix stripped: every module the configured features select
    that really exists on the target. Their dependencies are additionally
    pulled in by mkinitfs, so the image is a superset of this set and never a
    subset of it.
    """
    root = Path(root)
    directory = root / MODULES_ROOT / release
    undefined, modules = [], set()
    for feature in features:
        definition = root / FEATURES_DIRECTORY / f"{feature}.modules"
        if not definition.is_file():
            # A feature with no module list is legitimate when the feature only
            # contributes files; one with neither definition is not.
            if not (root / FEATURES_DIRECTORY / f"{feature}.files").is_file():
                undefined.append(feature)
            continue
        for line in definition.read_text(encoding="utf-8", errors="replace").splitlines():
            pattern = line.strip()
            if pattern and not pattern.startswith("#"):
                modules.update(_expand(directory, pattern))
    return modules, undefined


def _read_exact(stream, count):
    data = stream.read(count)
    if len(data) != count:
        raise InitramfsError("The initramfs ends inside an archive entry.")
    return data


def _entries(stream):
    """Names in a newc cpio stream, in order."""
    for _ in range(MAX_ENTRIES):
        header = stream.read(CPIO_HEADER)
        if not header:
            return
        if len(header) < CPIO_HEADER or header[:6] not in CPIO_MAGIC:
            raise InitramfsError("The initramfs is not a newc cpio archive.")
        try:
            size = int(header[54:62], 16)
            namesize = int(header[94:102], 16)
        except ValueError:
            raise InitramfsError("The initramfs has an unreadable archive header.")
        name = _read_exact(stream, namesize)[:-1].decode("utf-8", errors="replace")
        _read_exact(stream, -(CPIO_HEADER + namesize) % 4)
        if name == CPIO_TRAILER:
            return
        _read_exact(stream, size)
        _read_exact(stream, -size % 4)
        yield name.lstrip("./") if name.startswith("./") else name
    raise InitramfsError("The initramfs contains an implausible number of entries.")


def compression(path):
    """The container an initramfs uses, or `unknown`."""
    with open(path, "rb") as handle:
        head = handle.read(8)
    if head.startswith(GZIP_MAGIC):
        return "gzip"
    for magic, name in COMPRESSION_NAMES.items():
        if head.startswith(magic):
            return name
    return "unknown"


def entry_names(path):
    """Every path inside a gzip-compressed initramfs."""
    kind = compression(path)
    if kind != "gzip":
        raise InitramfsError(f"The installed initramfs uses {kind} compression, which this "
                             "readback cannot open; mkinitfs is expected to produce gzip.")
    names = set()
    with gzip.open(str(path), "rb") as raw:
        stream = io.BufferedReader(raw, buffer_size=1 << 20)
        for name in _entries(stream):
            names.add(name)
    return names


def module_names(entries, release):
    """`{relative module path}` of the modules an initramfs carries.

    Names are reduced to the same logical form `expected_modules` produces:
    either module root, without the compression suffix.
    """
    prefixes = tuple(f"{root}/{release}/" for root in MODULE_PREFIXES)
    modules = set()
    for name in entries:
        prefix = next((item for item in prefixes if name.startswith(item)), None)
        if prefix is None or MODULE_SUFFIX not in name:
            continue
        modules.add(logical_module(name[len(prefix):]))
    return modules


def inspect(root, release, initramfs_path):
    """Compare an installed initramfs with the configuration it must satisfy.

    Returns the configured features, the modules those features select on this
    target, and the ones the image is missing. An empty expectation is itself
    a failure: an initramfs that carries no configured module could not mount
    this root.
    """
    root = Path(root)
    features = read_features(root / MKINITFS_CONFIG)
    expected, undefined = expected_modules(root, release, features)
    entries = entry_names(initramfs_path)
    present = module_names(entries, release)
    return {
        "features": sorted(features),
        "features_without_definition": sorted(undefined),
        "expected_modules": len(expected),
        "modules_in_initramfs": len(present),
        "missing_modules": sorted(expected - present),
        "has_init": "init" in entries,
    }
