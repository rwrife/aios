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
    def test_live_profile_and_boot_gate_match_current_capacity(self):
        result = subprocess.run(
            ['sh', '-c',
             'profile_standard() { :; }; . "$1"; profile_aios; printf "%s" "$kernel_cmdline"',
             'sh', str(ROOT / 'distro/alpine/profiles/mkimg.aios.sh')],
            env=dict(
                os.environ,
                AIOS_WORLD_BASE=str(ROOT / 'distro/alpine/apks/world.base'),
                AIOS_WORLD_X11=str(ROOT / 'distro/alpine/apks/world.x11'),
                AIOS_WORLD_VM=str(ROOT / 'distro/alpine/apks/world.vm'),
                AIOS_WORLD_DEVEL=str(ROOT / 'distro/alpine/apks/world.devel'),
                AIOS_WORLD_AI=str(ROOT / 'distro/alpine/apks/world.ai'),
                AIOS_WORLD_HARDWARE=str(ROOT / 'distro/alpine/apks/world.hardware'),
                AIOS_WORLD_IDENTITY='',
                AIOS_APKOVL_SCRIPT='genapkovl-aios.sh',
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn('rootflags=size=75%', result.stdout)
        workflow = (ROOT / '.github/workflows/build-iso.yml').read_text()
        self.assertEqual(workflow.count('--memory-mb 8192'), 2)
        self.assertEqual(workflow.count('--cpus 4'), 2)
        self.assertEqual(workflow.count('--timeout 900'), 2)

    def test_optional_world_is_in_overlay_and_package_profile(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stage = root / 'stage'
                stage.mkdir()
                env = dict(os.environ, AIOS_STAGE_DIR=str(stage),
                           AIOS_OVERLAY_DIR=str(ROOT / 'distro/alpine/overlay'),
                           AIOS_REPOSITORIES='https://example.invalid/main',
                           AIOS_WORLD_HARDWARE=str(ROOT / 'distro/alpine/apks/world.hardware'),
                           AIOS_WORLD_IDENTITY=str(ROOT / 'distro/alpine/apks/world.identity') if enabled else '')
                for name in ('BASE', 'X11', 'VM', 'DEVEL', 'AI'):
                    env['AIOS_WORLD_' + name] = str(ROOT / 'distro/alpine/apks' / ('world.' + name.lower()))
                subprocess.run(['sh', str(ROOT / 'distro/alpine/apkovl/genapkovl-aios.sh')],
                               cwd=root, env=env, check=True, capture_output=True)
                with tarfile.open(root / 'aios.apkovl.tar.gz') as archive:
                    world = archive.extractfile('./etc/apk/world').read().decode().splitlines()
                    clock = archive.getmember('./usr/local/sbin/aios-clock')
                    self.assertEqual((clock.uid, clock.gid, clock.mode), (0, 0, 0o755))
                    self.assertTrue(archive.extractfile(clock).read().startswith(b'#!/usr/bin/python3 -I\n'))
                    policy = archive.getmember('./etc/doas.d/aios.conf')
                    self.assertEqual((policy.uid, policy.gid, policy.mode), (0, 0, 0o644))
                self.assertEqual('bubblewrap' in world, enabled)
                self.assertEqual('cryptsetup' in world, enabled)
                self.assertIn('tzdata', world)
                self.assertFalse(any(line.startswith('# Optional') for line in world))
                result = subprocess.run(['sh', '-c',
                    'profile_standard() { :; }; . "$1"; profile_aios; printf "%s" "$apks"',
                    'sh', str(ROOT / 'distro/alpine/profiles/mkimg.aios.sh')],
                    env=env, check=True, capture_output=True, text=True)
                self.assertEqual('bubblewrap' in result.stdout.split(), enabled)
                self.assertIn('tzdata', result.stdout.split())
                self.assertNotIn('Optional', result.stdout.split())
