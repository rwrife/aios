import os
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

    def test_save_replaces_link_without_touching_target(self):
        outside = self.root / 'private.txt'
        outside.write_text('unchanged')
        (self.root / 'artifacts' / 'Resume.txt').symlink_to(outside)
        artifacts.save(self.root, os.getuid(), 'Resume.txt', 'new document')
        self.assertEqual(outside.read_text(), 'unchanged')
        self.assertFalse((self.root / 'artifacts' / 'Resume.txt').is_symlink())
