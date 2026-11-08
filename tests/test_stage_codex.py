"""Offline checks for checksum-pinned Codex staging when release downloads fail."""
import importlib.util
from email.message import Message
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/stage-codex.py'
spec = importlib.util.spec_from_file_location('stage_codex', SCRIPT)
assert spec is not None and spec.loader is not None
stage_codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_codex)


class StageCodexTests(unittest.TestCase):
    def test_retry_discards_partial_and_stages_only_valid_archive(self):
        name = 'codex-x86_64-unknown-linux-musl'
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / 'source.tar.gz'
            with tarfile.open(source, 'w:gz') as archive:
                data = b'fake binary'
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            import hashlib
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            calls = []

            def download(_url, timeout):
                calls.append(timeout)
                if len(calls) == 1:
                    partial = root / 'download' / f'{name}-0.153.4.tar.part'
                    partial.write_bytes(b'incomplete')
                    raise urllib.error.HTTPError(_url, 504, 'Gateway Time-out', Message(), None)
                return source.open('rb')

            with mock.patch.object(stage_codex, 'CHECKSUMS', {'x86_64': digest}), \
                 mock.patch.object(stage_codex.urllib.request, 'urlopen', side_effect=download), \
                 mock.patch.object(stage_codex.time, 'sleep') as sleep, \
                 mock.patch.object(stage_codex.sys, 'argv', [str(SCRIPT), str(root / 'download'), str(root / 'stage')]), \
                 mock.patch.dict(os.environ, {'ARCH': 'x86_64'}):
                stage_codex.main()
            self.assertEqual(calls, [120, 120])
            sleep.assert_called_once_with(1)
            self.assertEqual((root / 'stage/usr/local/bin/codex').read_bytes(), b'fake binary')
            self.assertFalse((root / 'download' / f'{name}-0.153.4.tar.part').exists())

    def test_exhaustion_has_bounded_attempts_and_no_partial_archive(self):
        name = 'codex-x86_64-unknown-linux-musl'
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            def fail(url, timeout):
                (root / 'download' / f'{name}-0.153.4.tar.part').write_bytes(b'partial')
                raise urllib.error.HTTPError(url, 504, 'Gateway Time-out', Message(), None)

            with mock.patch.object(stage_codex.urllib.request, 'urlopen', side_effect=fail) as request, \
                 mock.patch.object(stage_codex.time, 'sleep') as sleep, \
                 mock.patch.object(stage_codex.sys, 'argv', [str(SCRIPT), str(root / 'download'), str(root / 'stage')]), \
                 mock.patch.dict(os.environ, {'ARCH': 'x86_64'}):
                with self.assertRaisesRegex(SystemExit, 'after 4 attempts: HTTP Error 504'):
                    stage_codex.main()
            self.assertEqual(request.call_count, 4)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
            self.assertFalse((root / 'download' / f'{name}-0.153.4.tar.part').exists())
            self.assertFalse((root / 'stage/usr/local/bin/codex').exists())


if __name__ == '__main__':
    unittest.main()
