"""Bounded text artifacts, resolved beneath an open workspace directory.

The broker never follows application-created symlinks, devices or FIFOs. Directory
FD traversal also prevents a rename race from redirecting resolution outside the
workspace. Documents are atomically replaced and synced before acknowledgement.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
import re
from pathlib import Path
import secrets
import stat

from .isolation import artifact_path

MAX_BYTES = 32768


class ArtifactConflict(ValueError):
    """The destination changed or vanished since it was read."""


@contextmanager
def locked(directory):
    # ponytail: serialize cooperating broker writers; this is not a lock on
    # external editors. Integrate owner-bound handles before exposing this API.
    fcntl.flock(directory, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(directory, fcntl.LOCK_UN)


@contextmanager
def parent(root, name):
    name = artifact_path(name)
    if Path(name).suffix.lower() not in ('.txt', '.md', '.csv'):
        raise ValueError('Choose a text, Markdown or CSV document')
    parts = name.split('/')
    if parts[0] == '.aios':
        raise PermissionError('Reserved workspace directory')
    descriptor = os.open(Path(root) / 'artifacts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def read(root, name):
    with parent(root, name) as (directory, leaf):
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                raise PermissionError('Document is not a supported private text file')
            data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError('Document is too large')
    return {'path': artifact_path(name), 'content': data.decode('utf-8'),
            'sha256': hashlib.sha256(data).hexdigest()}


def _sha256_open_fd(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
        raise ArtifactConflict('Destination document is not a supported private regular file')
    pos = os.lseek(descriptor, 0, os.SEEK_CUR)
    os.lseek(descriptor, 0, os.SEEK_SET)
    hasher = hashlib.sha256()
    remaining = MAX_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, min(8192, remaining))
        if not chunk:
            break
        hasher.update(chunk)
        remaining -= len(chunk)
    os.lseek(descriptor, pos, os.SEEK_SET)
    if remaining <= 0:
        raise ArtifactConflict('Destination document is too large')
    return hasher.hexdigest()


def save(root, uid, name, content, expected_sha256=None):
    if not isinstance(content, str):
        raise ValueError('Invalid document text')
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
            raise ValueError('Invalid expected_sha256 digest')
    data = content.encode('utf-8')
    if len(data) > MAX_BYTES:
        raise ValueError('Document is too large')
    with parent(root, name) as (directory, leaf):
        with locked(directory):
            if expected_sha256 is not None:
                try:
                    target_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                except FileNotFoundError:
                    raise ArtifactConflict('Document does not exist to compare version') from None
                except OSError as exc:
                    raise ArtifactConflict('Destination cannot be verified') from exc
                try:
                    current_sha = _sha256_open_fd(target_fd)
                finally:
                    os.close(target_fd)
                if current_sha != expected_sha256:
                    raise ArtifactConflict('Document changed since it was read')
            temporary = '.save-' + secrets.token_hex(16)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    if os.geteuid() == 0:
                        os.fchown(stream.fileno(), uid, uid)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    return {'path': artifact_path(name), 'sha256': hashlib.sha256(data).hexdigest()}
