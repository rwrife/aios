"""The live medium's own APK repository and the package closure it embeds.

An offline installation may only take packages from the repository the booted
ISO carries. That repository is *discovered*, never supplied: the kernel's own
mount table is read, mount points under `/media` are considered, and one is
accepted only when it carries Alpine's `apks/.boot_repository` marker and an
architecture directory with an `APKINDEX.tar.gz`. There is no path, URL, mirror
or package-name input anywhere in this module, and nothing here downloads.

The embedded closure is the exact set of `name=version` packages that ISO can
install. It is read from the repository's own `APKINDEX.tar.gz`, falling back
to the package filenames when an index is absent, and is what proves an
installed system came from the medium rather than from a network mirror.
"""
import argparse
import json
from pathlib import Path
import re
import sys
import tarfile

MOUNTS = "proc/mounts"
MEDIA_ROOT = "/media"
REPOSITORY_NAME = "apks"
MARKER_NAME = ".boot_repository"
INDEX_NAME = "APKINDEX.tar.gz"
INDEX_MEMBER = "APKINDEX"
PACKAGE_SUFFIX = ".apk"
# `name-<version>-r<revision>.apk`, the only filename form apk produces.
PACKAGE_FILENAME = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+-r\d+)$")
MAX_INDEX_BYTES = 64 * 1024 * 1024


class RepositoryError(RuntimeError):
    """The live medium's repository could not be discovered or read."""


def _unescape(field):
    """Undo the octal escaping the kernel applies to mount-table fields."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), field)


def mounted_media(live_root="/"):
    """Block-device mount points under /media, from the kernel's mount table.

    This is the only source of candidate locations. Nothing is taken from an
    argument, an environment variable or a configuration file.
    """
    path = Path(live_root) / MOUNTS
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise RepositoryError(f"The system mount table could not be read: {error.strerror}.")
    points = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        device, point = _unescape(fields[0]), _unescape(fields[1])
        if not device.startswith("/dev/"):
            continue
        if point != MEDIA_ROOT and not point.startswith(MEDIA_ROOT + "/"):
            continue
        points.append(point)
    return points


def _architecture_indexes(directory):
    return sorted(child for child in directory.iterdir()
                  if child.is_dir() and (child / INDEX_NAME).is_file())


def candidates(live_root="/"):
    """Every mounted medium that really carries a boot repository."""
    found = []
    for point in mounted_media(live_root):
        repository = Path(live_root) / point.lstrip("/") / REPOSITORY_NAME
        if not repository.is_dir() or not (repository / MARKER_NAME).is_file():
            continue
        if not _architecture_indexes(repository):
            continue
        found.append((f"{point}/{REPOSITORY_NAME}", repository))
    return found


def discover(live_root="/"):
    """`(system path, filesystem path)` of the one local boot repository.

    Ambiguity is refused rather than resolved: two mounted media carrying a
    boot repository means the installer cannot tell which ISO it booted from.
    """
    found = candidates(live_root)
    if not found:
        raise RepositoryError(
            "No mounted boot medium carries a local package repository; an offline "
            "installation needs the ISO's own /apks repository to be mounted.")
    if len(found) > 1:
        raise RepositoryError(
            f"{len(found)} mounted media carry a local package repository; "
            "unmount all but the booted installation medium.")
    return found[0]


def _index_packages(archive):
    """`{name: {versions}}` from one APKINDEX member."""
    packages, name, version = {}, None, None
    for line in archive.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            if name and version:
                packages.setdefault(name, set()).add(version)
            name, version = None, None
            continue
        key, _, value = line.partition(":")
        if key == "P":
            name = value.strip()
        elif key == "V":
            version = value.strip()
    if name and version:
        packages.setdefault(name, set()).add(version)
    return packages


def _read_index(path):
    with tarfile.open(str(path), "r:gz") as archive:
        member = next((entry for entry in archive.getmembers()
                       if entry.isfile() and entry.name.lstrip("./") == INDEX_MEMBER), None)
        if member is None:
            raise RepositoryError(f"{INDEX_NAME} does not contain an {INDEX_MEMBER}.")
        if member.size > MAX_INDEX_BYTES:
            raise RepositoryError(f"{INDEX_NAME} is implausibly large.")
        return archive.extractfile(member).read()


def filename_identity(filename):
    """`(name, version)` of an apk package file, or None."""
    if not filename.endswith(PACKAGE_SUFFIX):
        return None
    match = PACKAGE_FILENAME.match(filename[:-len(PACKAGE_SUFFIX)])
    if not match:
        return None
    return match.group("name"), match.group("version")


def read_closure(repository):
    """`{name: {versions}}` the medium can install, from its own index.

    The index is authoritative. Package files are used only when a repository
    has no index, so a medium is never credited with packages it cannot serve.
    """
    repository = Path(repository)
    closure = {}
    architectures = _architecture_indexes(repository)
    if architectures:
        for directory in architectures:
            for name, versions in _index_packages(_read_index(directory / INDEX_NAME)).items():
                closure.setdefault(name, set()).update(versions)
        return closure
    for directory in sorted(child for child in repository.iterdir() if child.is_dir()):
        for entry in sorted(directory.iterdir()):
            identity = filename_identity(entry.name) if entry.is_file() else None
            if identity:
                closure.setdefault(identity[0], set()).add(identity[1])
    if not closure:
        raise RepositoryError("The local repository contains no readable package listing.")
    return closure


def discovered_closure(live_root="/"):
    """`(system path, closure)` for the booted medium."""
    system_path, filesystem_path = discover(live_root)
    return system_path, read_closure(filesystem_path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aios.boot_repository",
        description="Report the booted medium's own package repository.")
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("path", "closure"):
        operation = operations.add_parser(name)
        operation.add_argument("--live-root", default="/", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        system_path, filesystem_path = discover(arguments.live_root)
        if arguments.operation == "path":
            print(system_path)
            return 0
        closure = read_closure(filesystem_path)
    except (RepositoryError, OSError, tarfile.TarError) as error:
        message = error.strerror if isinstance(error, OSError) else str(error)
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1
    print(json.dumps({"repository": system_path, "packages": len(closure),
                      "versions": sum(len(versions) for versions in closure.values())},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
