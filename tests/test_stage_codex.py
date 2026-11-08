"""Offline checks for checksum-pinned Codex staging and release-asset fallback."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error
from email.message import Message


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/stage-codex.py'
spec = importlib.util.spec_from_file_location('stage_codex', SCRIPT)
assert spec is not None and spec.loader is not None
stage_codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_codex)


class StageCodexTests(unittest.TestCase):
    def setUp(self):
        self.name = 'codex-x86_64-unknown-linux-musl'
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.tar.gz'
        with tarfile.open(self.source, 'w:gz') as archive:
            data = b'fake binary'
            info = tarfile.TarInfo(self.name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.partial = self.root / 'download' / f'{self.name}-0.153.4.tar.part'
        self.api_url = 'https://api.github.com/repos/openai/codex/releases/assets/545043410'
        self.metadata = json.dumps({'assets': [{'name': f'{self.name}.tar.gz', 'url': self.api_url}]}).encode()

    def run_stage(self, download):
        with mock.patch.object(stage_codex, 'CHECKSUMS', {'x86_64': self.digest}), \
             mock.patch.object(stage_codex.urllib.request, 'urlopen', side_effect=download) as request, \
             mock.patch.object(stage_codex.time, 'sleep') as sleep, \
             mock.patch.object(stage_codex.sys, 'argv', [str(SCRIPT), str(self.root / 'download'), str(self.root / 'stage')]), \
             mock.patch.dict(os.environ, {'ARCH': 'x86_64'}, clear=True):
            stage_codex.main()
        return request, sleep

    def test_api_asset_stages_verified_archive_without_browser_endpoint(self):
        urls = []
        def download(request, timeout):
            urls.append(request.full_url)
            if '/tags/' in request.full_url:
                return io.BytesIO(self.metadata)
            return self.source.open('rb')

        request, sleep = self.run_stage(download)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(urls[-1], self.api_url)
        self.assertEqual(request.call_args_list[-1].args[0].get_header('Accept'), 'application/octet-stream')
        sleep.assert_not_called()
        self.assertEqual((self.root / 'stage/usr/local/bin/codex').read_bytes(), b'fake binary')

    def test_api_504_falls_back_to_release_download_and_discards_partial(self):
        urls = []
        def download(request, timeout):
            urls.append(request.full_url)
            if '/tags/' in request.full_url:
                return io.BytesIO(self.metadata)
            if request.full_url == self.api_url:
                self.partial.write_bytes(b'incomplete')
                raise urllib.error.HTTPError(request.full_url, 504, 'Gateway Time-out', Message(), None)
            return self.source.open('rb')

        _, sleep = self.run_stage(download)
        self.assertIn('/releases/download/', urls[-1])
        sleep.assert_called_once_with(1)
        self.assertEqual((self.root / 'stage/usr/local/bin/codex').read_bytes(), b'fake binary')
        self.assertFalse(self.partial.exists())

    def test_exhaustion_has_bounded_attempts_and_no_partial_archive(self):
        def fail(request, timeout):
            if '/tags/' in request.full_url:
                raise urllib.error.HTTPError(request.full_url, 503, 'Unavailable', Message(), None)
            self.partial.write_bytes(b'partial')
            raise urllib.error.HTTPError(request.full_url, 504, 'Gateway Time-out', Message(), None)

        with mock.patch.object(stage_codex.urllib.request, 'urlopen', side_effect=fail) as request, \
             mock.patch.object(stage_codex.time, 'sleep') as sleep, \
             mock.patch.object(stage_codex.sys, 'argv', [str(SCRIPT), str(self.root / 'download'), str(self.root / 'stage')]), \
             mock.patch.dict(os.environ, {'ARCH': 'x86_64'}, clear=True):
            with self.assertRaisesRegex(SystemExit, 'after 4 attempts: HTTP Error 504'):
                stage_codex.main()
        self.assertEqual(request.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
        self.assertFalse(self.partial.exists())
        self.assertFalse((self.root / 'stage/usr/local/bin/codex').exists())


if __name__ == '__main__':
    unittest.main()
