from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import os
from threading import Event
from unittest.mock import patch
from pathlib import Path
import tempfile
import unittest

from aios import artifacts


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'artifacts').mkdir()

    def test_atomic_roundtrip_and_size_bound(self):
        saved = artifacts.save(self.root, os.getuid(), 'Resume.md', '# Résumé')
        loaded = artifacts.read(self.root, 'Resume.md')
        self.assertEqual(loaded['sha256'], saved['sha256'])
        self.assertEqual(loaded['content'], '# Résumé')
        with self.assertRaises(ValueError):
            artifacts.save(self.root, os.getuid(), 'Resume.md', 'x' * 32769)
        self.assertEqual(artifacts.read(self.root, 'Resume.md')['content'], '# Résumé')

    def test_symlink_directory_file_and_fifo_denied(self):
        outside = self.root / 'private.txt'
        outside.write_text('outside workspace')
        (self.root / 'artifacts' / 'link.txt').symlink_to(outside)
        (self.root / 'artifacts' / 'escape').symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.root / 'artifacts' / 'pipe.txt')
        for name in ('link.txt', 'escape/private.txt', 'pipe.txt', '../private.txt'):
            with self.subTest(name=name), self.assertRaises((ValueError, OSError, PermissionError)):
                artifacts.read(self.root, name)
        with self.assertRaises(OSError):
            artifacts.save(self.root, os.getuid(), 'escape/private.txt', 'replacement')
        self.assertEqual(outside.read_text(), 'outside workspace')

    def test_csv_result_roundtrip_and_stale_save_is_rejected(self):
        original = artifacts.save(self.root, os.getuid(), 'results.csv', 'item,count\nA,1\n')
        self.assertEqual(artifacts.read(self.root, 'results.csv')['content'], 'item,count\nA,1\n')
        latest = artifacts.save(self.root, os.getuid(), 'results.csv', 'item,count\nA,2\n',
                                expected_sha256=original['sha256'])
        with self.assertRaises(artifacts.ArtifactConflict):
            artifacts.save(self.root, os.getuid(), 'results.csv', 'stale',
                           expected_sha256=original['sha256'])
        self.assertEqual(artifacts.read(self.root, 'results.csv')['sha256'], latest['sha256'])

    def test_conflict_guard_rejects_missing_and_symlink_destinations(self):
        with self.assertRaises(artifacts.ArtifactConflict):
            artifacts.save(self.root, os.getuid(), 'missing.txt', 'data',
                           expected_sha256='0' * 64)
        outside = self.root / 'outside.txt'
        outside.write_text('outside')
        link = self.root / 'artifacts' / 'link.txt'
        link.symlink_to(outside)
        with self.assertRaises(artifacts.ArtifactConflict):
            artifacts.save(self.root, os.getuid(), 'link.txt', 'data',
                           expected_sha256=hashlib.sha256(b'outside').hexdigest())
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_text(), 'outside')

    def test_concurrent_writers_with_same_digest_have_one_winner(self):
        saved = artifacts.save(self.root, os.getuid(), 'shared.txt', 'original')
        first_entered = Event()
        first_can_finish = Event()
        original_sha_open_fd = artifacts._sha256_open_fd
        calls = 0

        def synced_sha(fd):
            nonlocal calls
            calls += 1
            probe = os.open(self.root / 'artifacts', os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
            res = original_sha_open_fd(fd)
            if calls == 1:
                first_entered.set()
                first_can_finish.wait(timeout=5)
            return res

        def worker(val):
            try:
                artifacts.save(self.root, os.getuid(), 'shared.txt', val,
                               expected_sha256=saved['sha256'])
                return val
            except artifacts.ArtifactConflict:
                return None

        with patch('aios.artifacts._sha256_open_fd', side_effect=synced_sha):
            with ThreadPoolExecutor(max_workers=2) as pool:
                fut1 = pool.submit(worker, 'writer1')
                self.assertTrue(first_entered.wait(timeout=5))
                fut2 = pool.submit(worker, 'writer2')
                first_can_finish.set()
                winners = [f.result() for f in (fut1, fut2) if f.result() is not None]

        self.assertEqual(winners, ['writer1'])
        self.assertEqual(artifacts.read(self.root, 'shared.txt')['content'], 'writer1')
        self.assertFalse(list((self.root / 'artifacts').glob('.save-*')))

    def test_invalid_digest_does_not_modify_destination(self):
        artifacts.save(self.root, os.getuid(), 'safe.txt', 'original')
        for value in ('', 'x' * 64, 123, True, '0' * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                artifacts.save(self.root, os.getuid(), 'safe.txt', 'replacement',
                               expected_sha256=value)
        self.assertEqual(artifacts.read(self.root, 'safe.txt')['content'], 'original')

    def test_save_replaces_link_without_touching_target(self):
        outside = self.root / 'private.txt'
        outside.write_text('unchanged')
        (self.root / 'artifacts' / 'Resume.txt').symlink_to(outside)
        artifacts.save(self.root, os.getuid(), 'Resume.txt', 'new document')
        self.assertEqual(outside.read_text(), 'unchanged')
        self.assertFalse((self.root / 'artifacts' / 'Resume.txt').is_symlink())
