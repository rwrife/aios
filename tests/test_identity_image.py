"""Exercise optional package selection through the real overlay generator."""
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix', 'Alpine image generation requires Linux')
class IdentityImageTests(unittest.TestCase):
    def test_optional_world_is_in_overlay_and_package_profile(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stage = root / 'stage'
                stage.mkdir()
                env = dict(os.environ, AIOS_STAGE_DIR=str(stage),
                           AIOS_OVERLAY_DIR=str(ROOT / 'distro/alpine/overlay'),
                           AIOS_REPOSITORIES='https://example.invalid/main',
                           AIOS_WORLD_IDENTITY=str(ROOT / 'distro/alpine/apks/world.identity') if enabled else '')
                for name in ('BASE', 'X11', 'VM', 'DEVEL', 'AI'):
                    env['AIOS_WORLD_' + name] = str(ROOT / 'distro/alpine/apks' / ('world.' + name.lower()))
                subprocess.run(['sh', str(ROOT / 'distro/alpine/apkovl/genapkovl-aios.sh')],
                               cwd=root, env=env, check=True, capture_output=True)
                with tarfile.open(root / 'aios.apkovl.tar.gz') as archive:
                    world = archive.extractfile('./etc/apk/world').read().decode().splitlines()
                self.assertEqual('bubblewrap' in world, enabled)
                self.assertEqual('cryptsetup' in world, enabled)
                self.assertFalse(any(line.startswith('# Optional') for line in world))
                result = subprocess.run(['sh', '-c',
                    'profile_standard() { :; }; . "$1"; profile_aios; printf "%s" "$apks"',
                    'sh', str(ROOT / 'distro/alpine/profiles/mkimg.aios.sh')],
                    env=env, check=True, capture_output=True, text=True)
                self.assertEqual('bubblewrap' in result.stdout.split(), enabled)
                self.assertNotIn('Optional', result.stdout.split())
