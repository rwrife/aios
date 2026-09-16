"""Durable, crash-safe filesystem primitives shared by the installer verifier
and the audit-checkpoint helper.

Every write here is either fully visible or not visible at all after a power
loss: content is written to a temporary name in the destination directory,
flushed to stable storage, and then renamed over the destination. A rename
within one directory is atomic, so an interrupted operation always leaves the
previous content in place rather than a half-written file.

The lock below serializes the mutating operations of one root-owned store, so
two concurrent helpers cannot interleave their reclamation and their writes.
"""
import contextlib
import os
from pathlib import Path
import shutil

try:
    import fcntl
except ImportError:  # Windows development hosts; the store itself is POSIX-only.
    fcntl = None

TEMP_PREFIX = ".aios-tmp-"
LOCK_NAME = ".lock"
LOCK_MODE = 0o600
COPY_CHUNK = 1 << 20


class LockUnavailable(RuntimeError):
    """Another operation already holds the store's exclusive lock."""


@contextlib.contextmanager
def exclusive_lock(directory, name=LOCK_NAME):
    """Hold one store's exclusive lock, or refuse immediately.

    The lock file is created with owner-only permissions by a process that
    already has to be root, and the lock is advisory between the helper's own
    operations. It is never waited on: a second mutating run reports that one
    is already in progress instead of queueing behind it.
    """
    if fcntl is None:
        raise LockUnavailable("Exclusive locking is not available on this platform.")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, LOCK_MODE)
    try:
        os.fchmod(descriptor, LOCK_MODE)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockUnavailable("Another checkpoint operation is already running.")
        try:
            yield path
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)



def fsync_directory(directory):
    """Flush a directory entry so a rename survives a power loss."""
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temporary_name(path):
    path = Path(path)
    return path.with_name(f"{TEMP_PREFIX}{os.getpid()}-{path.name}")


def write_atomic(path, text, mode=0o644):
    """Replace `path` with `text` atomically and durably."""
    path = Path(path)
    temporary = _temporary_name(path)
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def copy_durable(source, destination, mode=0o644):
    """Copy a file so the destination is either complete or absent."""
    source, destination = Path(source), Path(destination)
    temporary = _temporary_name(destination)
    with open(source, "rb") as reader, open(temporary, "wb") as writer:
        while True:
            chunk = reader.read(COPY_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, destination)
    fsync_directory(destination.parent)


def remove_stale_temporaries(directory, recursive=False):
    """Delete leftovers from an interrupted write. Returns their names.

    With `recursive`, nested directories are swept too, so a temporary file
    left inside a half-written checkpoint directory is reclaimed as well.
    """
    directory = Path(directory)
    removed = []
    if not directory.is_dir():
        return removed
    for entry in sorted(directory.iterdir()):
        if not entry.name.startswith(TEMP_PREFIX):
            if recursive and entry.is_dir() and not entry.is_symlink():
                removed.extend(f"{entry.name}/{name}"
                               for name in remove_stale_temporaries(entry, recursive=True))
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)
        removed.append(entry.name)
    if removed:
        fsync_directory(directory)
    return removed


def remove_tree(path):
    """Delete a directory and make its disappearance durable."""
    path = Path(path)
    shutil.rmtree(path)
    fsync_directory(path.parent)


def free_space_bytes(path):
    """Bytes available to a non-root writer on the filesystem holding `path`."""
    usage = os.statvfs(str(path))
    return usage.f_bavail * usage.f_frsize
