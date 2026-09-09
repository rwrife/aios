"""Root-created, per-lease Wayland listeners; descriptors go only to the shell."""
import os
from pathlib import Path
import socket

from .identity import identity_id


class DisplaySocket:
    def __init__(self, runtime, lease, uid):
        identity_id(lease)
        base = Path(runtime) / '.displays'
        base.mkdir(mode=0o711, exist_ok=True)
        if base.is_symlink() or base.stat().st_uid != 0 or base.stat().st_mode & 0o022:
            raise PermissionError('Invalid display directory')
        base.chmod(0o711)  # The broker's 0077 umask must not block UID traversal.
        self.directory = base / lease
        self.directory.mkdir(mode=0o700)
        os.chown(self.directory, uid, uid)
        self.path = self.directory / 'wayland-0'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(self.path))
            self.path.chmod(0o600)
            os.chown(self.path, uid, uid)
            self.listener.listen(8)
        except Exception:
            self.close()
            raise

    def close(self):
        self.listener.close()
        self.path.unlink(missing_ok=True)
        self.directory.rmdir()


def recover(runtime):
    base = Path(runtime) / '.displays'
    if not base.exists():
        return
    if base.is_symlink():
        raise PermissionError('Invalid display directory')
    for directory in base.iterdir():
        identity_id(directory.name)
        if directory.is_symlink() or not directory.is_dir():
            raise PermissionError('Invalid stale display directory')
        (directory / 'wayland-0').unlink(missing_ok=True)
        directory.rmdir()


class DescriptorReply:
    def __init__(self, descriptor, result):
        self.descriptor, self.result = descriptor, result
