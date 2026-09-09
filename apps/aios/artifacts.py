"""Bounded text artifacts, resolved beneath an open workspace directory.

The broker never follows application-created symlinks, devices or FIFOs. Directory
FD traversal also prevents a rename race from redirecting resolution outside the
workspace. Documents are atomically replaced and synced before acknowledgement.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import secrets
import stat

from .isolation import artifact_path

MAX_BYTES = 32768


@contextmanager
def parent(root, name):
    name = artifact_path(name)
    if Path(name).suffix.lower() not in ('.txt', '.md'):
        raise ValueError('Choose a text or Markdown document')
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


def save(root, uid, name, content):
    if not isinstance(content, str):
        raise ValueError('Invalid document text')
    data = content.encode('utf-8')
    if len(data) > MAX_BYTES:
        raise ValueError('Document is too large')
    with parent(root, name) as (directory, leaf):
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
