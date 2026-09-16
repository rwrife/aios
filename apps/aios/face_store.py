"""Crash-recoverable greeting templates. This does not protect against the UID.

Protected identity storage deliberately has no local fallback. Each transaction
publishes one encrypted generation, then retires older keys and ciphertext.
"""
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from .secure_store import EncryptedStore, atomic_bytes

NAMESPACE = 'local-greeting'
SCHEMA = 2
GENERATION = re.compile(r'^[a-f0-9]{32}$')


def account_uuid(owner):
    if type(owner) is not str or str(uuid.UUID(owner)) != owner:
        raise ValueError('An exact account UUID is required')
    return owner


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Facial data directory permissions are unsafe')


def private_read(path, maximum=2097152):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > maximum):
            raise ValueError('Facial data file permissions or size are unsafe')
        return stream.read(maximum + 1)


class FaceStore:
    def __init__(self, profile_root, namespace=NAMESPACE):
        if namespace != NAMESPACE or os.environ.get('AIOS_SESSION_SOCKET'):
            raise ValueError('Protected face enrollment requires broker-owned storage and trusted input')
        self.profile_root = Path(profile_root)
        self.root = self.profile_root / 'biometrics' / NAMESPACE

    @contextmanager
    def locked(self):
        private_directory(self.profile_root)
        private_directory(self.root.parent)
        private_directory(self.root)
        descriptor = os.open(self.root / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError('Facial data lock is unsafe')
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self):
        pointer = self.root / 'current'
        if pointer.is_symlink():
            raise ValueError('Facial data pointer is unsafe')
        if not pointer.exists():
            self._retire('')
            return '', {}
        current = json.loads(private_read(pointer, 256))
        if (set(current) != {'schema', 'epoch', 'generation'} or current['schema'] != SCHEMA or
                type(current['epoch']) is not str or not GENERATION.fullmatch(current['epoch']) or
                current['generation'] not in ('', current['epoch'])):
            raise ValueError('Facial data generation is invalid')
        generation = current['generation']
        if not generation:
            self._retire('')
            return current['epoch'], {}
        directory = self.root / generation
        private_directory(directory)
        key = private_read(directory / 'key', 32)
        if len(key) != 32:
            raise ValueError('Facial data key is invalid')
        private_directory(directory / 'records')
        encrypted = private_read(directory / 'records' / 'templates.enc')
        store = EncryptedStore(directory / 'records', key)
        records = json.loads(store.cipher.decrypt(encrypted[:12], encrypted[12:], b'templates'))
        if type(records) is not dict or len(records) > 128:
            raise ValueError('Facial data records are invalid')
        for owner in records:
            account_uuid(owner)
        self._retire(generation)
        return current['epoch'], records

    def _retire(self, current):
        for path in self.root.iterdir():
            if GENERATION.fullmatch(path.name) and path.name != current:
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                else:
                    shutil.rmtree(path)
        descriptor = os.open(self.root, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _commit(self, records):
        epoch = uuid.uuid4().hex
        generation = epoch if records else ''
        if records:
            directory = self.root / generation
            directory.mkdir(mode=0o700)
            key = os.urandom(32)
            atomic_bytes(directory / 'key', key)
            EncryptedStore(directory / 'records', key).put('templates', records)
        # This fsync-backed pointer is the only commit point. A crash before it
        # retains the previous generation; a crash after it cannot reload it.
        atomic_bytes(self.root / 'current', json.dumps({
            'schema': SCHEMA, 'epoch': epoch, 'generation': generation}).encode())
        self._retire(generation)
        return epoch

    def snapshot(self):
        with self.locked():
            return self._read()

    def replace(self, owner, record, expected_epoch):
        account_uuid(owner)
        with self.locked():
            epoch, records = self._read()
            if epoch != expected_epoch:
                raise ValueError('Facial data changed; restart enrollment')
            records[owner] = record
            self._commit(records)

    def revoke(self, owner=None):
        if owner is not None:
            account_uuid(owner)
        with self.locked():
            # Global purge also recovers from a corrupt/missing key or pointer.
            records = self._read()[1] if owner is not None else {}
            records.pop(owner, None)
            self._commit(records)
            self._remove_legacy(owner)

    def rotate(self):
        with self.locked():
            self._commit(self._read()[1])

    def compatible(self, profiles, binding):
        with self.locked():
            _, records = self._read()
            valid = {owner: value for owner, value in records.items()
                     if owner in profiles and type(value) is dict and value.get('binding') == binding and
                     value.get('owner') == owner and value.get('namespace') == NAMESPACE and
                     all(type(value.get(key)) in (float, int) and math.isfinite(value[key]) and value[key] >= 0
                         for key in ('created', 'updated')) and value['created'] <= value['updated']}
            if valid != records:
                self._commit(valid)
            return valid

    def _remove_legacy(self, owner):
        records = self.profile_root / 'recognition-records'
        if records.is_symlink():
            if owner is None:
                records.unlink()
            return
        if records.is_dir():
            paths = records.glob('face-template-*.enc') if owner is None else [records / ('face-template-' + owner + '.enc')]
            for path in paths:
                path.unlink(missing_ok=True)
            descriptor = os.open(records, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        if owner is None:
            (self.profile_root / 'recognition-key').unlink(missing_ok=True)
            descriptor = os.open(self.profile_root, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
