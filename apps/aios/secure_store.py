"""Authenticated encrypted local records. No fallback to plaintext storage."""
import json
import os
import tempfile
from pathlib import Path


def atomic_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == 'posix':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class EncryptedStore:
    def __init__(self, root, key):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = AESGCM(key)

    def _path(self, name):
        if not isinstance(name, str) or not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in name):
            raise ValueError("Invalid record name")
        return self.root / (name + '.enc')

    def put(self, name, value):
        nonce = os.urandom(12)
        payload = json.dumps(value, allow_nan=False).encode()
        atomic_bytes(self._path(name), nonce + self.cipher.encrypt(nonce, payload, name.encode()))

    def get(self, name, default=None):
        path = self._path(name)
        if not path.exists():
            return default
        value = path.read_bytes()
        return json.loads(self.cipher.decrypt(value[:12], value[12:], name.encode()))

    def delete(self, name):
        self._path(name).unlink(missing_ok=True)
