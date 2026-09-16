import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from aios.face_store import FaceStore, NAMESPACE


class FaceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = str(uuid.uuid4())
        self.store = FaceStore(self.root)
        self.record = {'owner': self.owner, 'namespace': NAMESPACE, 'binding': {'schema': 2},
                       'created': 1., 'updated': 2., 'samples': [[1.] + [0.] * 127] * 3}

    def save(self):
        self.store.replace(self.owner, self.record, self.store.snapshot()[0])

    def test_exact_namespace_uuid_and_protected_session_fail_closed(self):
        for namespace in ('protected', 'broker', '', '../local-greeting'):
            with self.assertRaises(ValueError):
                FaceStore(self.root, namespace)
        with patch.dict(os.environ, AIOS_SESSION_SOCKET='/private/broker'):
            with self.assertRaises(ValueError):
                FaceStore(self.root)
        with self.assertRaises(ValueError):
            self.store.replace('Alice', self.record, '')
        self.assertFalse((self.root / 'biometrics').exists())

    def test_records_are_encrypted_and_key_rotation_retires_old_generation(self):
        self.save()
        epoch, records = self.store.snapshot()
        self.assertEqual(records[self.owner], self.record)
        directory = self.store.root / epoch
        key = (directory / 'key').read_bytes()
        ciphertext = (directory / 'records/templates.enc').read_bytes()
        self.assertNotIn(self.owner.encode(), ciphertext)
        self.assertNotIn(b'samples', ciphertext)
        self.store.rotate()
        next_epoch, next_records = self.store.snapshot()
        self.assertNotEqual(next_epoch, epoch)
        self.assertNotEqual((self.store.root / next_epoch / 'key').read_bytes(), key)
        self.assertFalse(directory.exists())
        self.assertEqual(next_records, records)

    def test_purge_invalidates_inflight_write_and_preserves_unrelated_files(self):
        self.save()
        epoch = self.store.snapshot()[0]
        unrelated = self.root / 'portrait.png'
        unrelated.write_bytes(b'not a template')
        self.store.revoke()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.store.replace(self.owner, self.record, epoch)
        self.assertEqual(FaceStore(self.root).snapshot()[1], {})
        self.assertEqual(unrelated.read_bytes(), b'not a template')
        self.assertFalse(list(self.store.root.rglob('key')))

    def test_account_revoke_retains_other_accounts_and_invalidates_old_epoch(self):
        self.save()
        other = str(uuid.uuid4())
        self.store.replace(other, {**self.record, 'owner': other}, self.store.snapshot()[0])
        self.store.revoke(self.owner)
        self.assertEqual(set(FaceStore(self.root).snapshot()[1]), {other})

    def test_incompatible_binding_or_owner_is_durably_removed(self):
        for change in ({'schema': 3}, {'schema': 2, 'consent': 9}, {'model': 'different'}):
            self.save()
            self.assertEqual(self.store.compatible({self.owner}, change), {})
            self.assertEqual(FaceStore(self.root).snapshot()[1], {})
        self.save()
        self.assertEqual(self.store.compatible(set(), self.record['binding']), {})

    def test_unsafe_key_permissions_and_tamper_do_not_load_templates(self):
        self.save()
        epoch = self.store.snapshot()[0]
        key = self.store.root / epoch / 'key'
        key.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.snapshot()
        key.chmod(0o600)
        encrypted = self.store.root / epoch / 'records/templates.enc'
        value = bytearray(encrypted.read_bytes()); value[-1] ^= 1
        encrypted.write_bytes(value)
        with self.assertRaises(Exception):
            self.store.snapshot()
        self.store.revoke()
        self.assertEqual(self.store.snapshot()[1], {})

    def test_purge_recovers_corrupt_pointer_and_legacy_records(self):
        self.save()
        (self.store.root / 'current').write_bytes(b'broken')
        (self.root / 'recognition-key').write_bytes(b'legacy')
        legacy = self.root / 'recognition-records'; legacy.mkdir()
        (legacy / ('face-template-' + self.owner + '.enc')).write_bytes(b'opaque')
        self.store.revoke()
        self.assertEqual(self.store.snapshot()[1], {})
        self.assertFalse((self.root / 'recognition-key').exists())
        self.assertFalse(list(legacy.iterdir()))

    def test_process_crash_before_and_after_commit_point(self):
        code = '''
import os,sys
from pathlib import Path
import aios.face_store as module
store = module.FaceStore(Path(sys.argv[1]))
operation, phase, owner = sys.argv[2:]
original = module.atomic_bytes
def interrupted(path, payload):
    if path.name == 'current' and phase == 'before': os._exit(71)
    original(path, payload)
    if path.name == 'current' and phase == 'after': os._exit(72)
module.atomic_bytes = interrupted
if operation == 'purge': store.revoke()
else: store.replace(owner, {'replacement': True}, store.snapshot()[0])
'''
        for operation in ('purge', 'replace'):
            for phase in ('before', 'after'):
                with self.subTest(operation=operation, phase=phase):
                    self.save()
                    result = subprocess.run([sys.executable, '-c', code, str(self.root), operation,
                                             phase, self.owner], capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 71 if phase == 'before' else 72)
                    records = FaceStore(self.root).snapshot()[1]
                    expected = {self.owner: self.record} if phase == 'before' else (
                        {} if operation == 'purge' else {self.owner: {'replacement': True}})
                    self.assertEqual(records, expected)
                    generations = [p for p in self.store.root.iterdir() if len(p.name) == 32]
                    self.assertEqual(len(generations), 1 if records else 0)


if __name__ == '__main__':
    unittest.main()
