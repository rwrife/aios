"""Tests for distro/alpine/record-build-manifest.py.

The recorder is the build-side half of the recorded-evidence strategy: it makes
a moving Alpine repository's resolution auditable after the fact (repository
URLs + APKINDEX digests + the exact embedded APK closure). These tests drive it
with `--extracted-root`, so no xorriso/ISO tooling is required.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'distro' / 'alpine' / 'record-build-manifest.py'
INSPECT = ROOT / 'scripts' / 'inspect-image.py'

_spec = importlib.util.spec_from_file_location('aios_record_build_manifest', SCRIPT)
recorder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorder)

CANONICAL_MAIN = 'https://dl-cdn.alpinelinux.org/alpine/v3.23/main'
CANONICAL_COMMUNITY = 'https://dl-cdn.alpinelinux.org/alpine/v3.23/community'


class ManifestRecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

        self.iso = self.base / 'aios-20260101-x86_64.iso'
        self.iso.write_bytes(b'fake-iso-bytes')

        self.extracted = self.base / 'extracted'
        self.apks = self.extracted / 'apks' / 'x86_64'
        self.apks.mkdir(parents=True)
        (self.apks / 'busybox-1.36.1-r15.apk').write_bytes(b'busybox-bytes')
        (self.apks / 'linux-firmware-intel-20251125-r1.apk').write_bytes(b'firmware-bytes')
        boot = self.extracted / 'boot'
        boot.mkdir()
        (boot / 'vmlinuz-lts').write_bytes(b'kernel-bytes')
        (boot / 'initramfs-lts').write_bytes(b'initramfs-bytes')

        self.build_env = self.base / 'build.env'
        self.build_env.write_text(
            '# pins\n'
            'ALPINE_BRANCH=v3.23\n'
            'IMAGE=alpine:3.23@sha256:abc\n'
            'APORTS_REF=aaaa\n'
            'LLAMA_REF=bbbb\n'
            'WHISPER_REF=cccc\n',
            encoding='utf-8',
        )

        # A local "repository" whose APKINDEX can actually be hashed offline.
        self.local_repo = self.base / 'mirror' / 'v3.23' / 'main'
        (self.local_repo / 'x86_64').mkdir(parents=True)
        self.index_bytes = b'fake-apkindex-bytes'
        (self.local_repo / 'x86_64' / 'APKINDEX.tar.gz').write_bytes(self.index_bytes)

        self.output = self.base / 'manifest.json'

    def run_recorder(self, extra_args=(), repositories=None):
        repositories = repositories or [f'main={CANONICAL_MAIN}',
                                        f'community={CANONICAL_COMMUNITY}']
        args = [
            sys.executable, str(SCRIPT),
            '--iso', str(self.iso),
            '--arch', 'x86_64',
            '--release-tag', '20260101',
            '--build-env', str(self.build_env),
            '--effective', 'ALPINE_BRANCH=v3.23',
            '--effective', 'IMAGE=alpine:3.23@sha256:abc',
            '--effective', 'APORTS_REF=aaaa',
            '--effective', 'LLAMA_REF=bbbb',
            '--effective', 'WHISPER_REF=cccc',
            '--build-setting', 'AIOS_IDENTITY_BUILD=0',
            '--extracted-root', str(self.extracted),
            '--generated-utc', '2026-01-01T00:00:00Z',
            '--output', str(self.output),
        ]
        for repo in repositories:
            args.extend(['--repository', repo])
        args.extend(extra_args)
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return json.loads(self.output.read_text(encoding='utf-8'))

    # -- output closure --------------------------------------------------

    def test_records_the_exact_apk_closure(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        closure = manifest['output_closure']
        self.assertEqual(closure['status'], 'recorded')
        self.assertEqual(closure['apk_count'], 2)
        by_name = {entry['filename']: entry for entry in closure['packages']}
        self.assertEqual(
            set(by_name), {'busybox-1.36.1-r15.apk', 'linux-firmware-intel-20251125-r1.apk'})
        expected = hashlib.sha256(b'busybox-bytes').hexdigest()
        self.assertEqual(by_name['busybox-1.36.1-r15.apk']['sha256'], expected)
        self.assertEqual(by_name['busybox-1.36.1-r15.apk']['size'], len(b'busybox-bytes'))
        self.assertEqual(by_name['busybox-1.36.1-r15.apk']['path'],
                         'apks/x86_64/busybox-1.36.1-r15.apk')

    def test_writes_a_sorted_packages_list(self):
        packages_txt = self.base / 'packages.txt'
        self.run_recorder(['--skip-index-fetch', '--packages-txt', str(packages_txt)])
        lines = packages_txt.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines, sorted(lines))
        self.assertIn('busybox-1.36.1-r15.apk', lines)

    def test_missing_apks_directory_is_reported_not_faked(self):
        empty = self.base / 'empty-root'
        empty.mkdir()
        manifest = self.run_recorder(['--skip-index-fetch', '--extracted-root', str(empty)])
        self.assertEqual(manifest['output_closure']['status'], 'unavailable')
        self.assertEqual(manifest['output_closure']['packages'], [])

    def test_records_the_requested_package_worlds(self):
        hardware = self.base / 'world.hardware'
        # Exact bytes: the recorder must hash the file as it ships, so the test
        # writes LF explicitly instead of letting the platform translate them.
        hardware.write_bytes(b'# firmware\nlinux-firmware-intel\nwireless-regdb\n\n')
        vm = self.base / 'world.vm'
        vm.write_bytes(b'open-vm-tools\nqemu-guest-agent\n')
        manifest = self.run_recorder([
            '--skip-index-fetch',
            '--world', f'hardware={hardware}',
            '--world', f'vm={vm}',
        ])
        worlds = {entry['role']: entry for entry in manifest['package_worlds']['worlds']}
        self.assertEqual(worlds['hardware']['packages'],
                         ['linux-firmware-intel', 'wireless-regdb'])
        self.assertEqual(worlds['hardware']['package_count'], 2)
        self.assertEqual(
            worlds['hardware']['sha256'],
            hashlib.sha256(hardware.read_bytes()).hexdigest())
        # world.vm must stay recorded so virtual hardware support is auditable.
        self.assertEqual(worlds['vm']['packages'], ['open-vm-tools', 'qemu-guest-agent'])
        self.assertEqual(manifest['package_worlds']['combined_packages'],
                         ['linux-firmware-intel', 'open-vm-tools', 'qemu-guest-agent',
                          'wireless-regdb'])

    def test_missing_world_file_is_reported_not_faked(self):
        manifest = self.run_recorder([
            '--skip-index-fetch', '--world', f'hardware={self.base / "absent"}'])
        world = manifest['package_worlds']['worlds'][0]
        self.assertEqual(world['status'], 'unavailable')
        self.assertEqual(world['packages'], [])
        self.assertNotIn('sha256', world)

    def test_world_content_change_is_visible_in_the_manifest(self):
        hardware = self.base / 'world.hardware'
        hardware.write_bytes(b'linux-firmware-intel\n')
        first = self.run_recorder(['--skip-index-fetch', '--world', f'hardware={hardware}'])
        hardware.write_bytes(b'linux-firmware-intel\nsof-firmware\n')
        second = self.run_recorder(['--skip-index-fetch', '--world', f'hardware={hardware}'])
        self.assertNotEqual(first['package_worlds'], second['package_worlds'])

    def test_a_world_file_is_hashed_byte_for_byte(self):
        # Comment stripping decides the package list; the digest must still be
        # the digest of the file itself, newlines included.
        hardware = self.base / 'world.hardware'
        hardware.write_bytes(b'linux-firmware-intel\r\nwireless-regdb\r\n')
        manifest = self.run_recorder(
            ['--skip-index-fetch', '--world', f'hardware={hardware}'])
        world = manifest['package_worlds']['worlds'][0]
        self.assertEqual(world['sha256'], hashlib.sha256(hardware.read_bytes()).hexdigest())
        self.assertEqual(world['packages'], ['linux-firmware-intel', 'wireless-regdb'])

    def test_embeds_hardware_package_licenses_and_provenance(self):
        package_manifest = self.base / 'hardware-packages.json'
        package_manifest.write_text(json.dumps({
            'alpine_branch': 'v3.23',
            'world_file': 'distro/alpine/apks/world.hardware',
            'resolution': {'status': 'resolved-from-repository-index', 'resolved_on': '2026-09-14'},
            'packages': {
                'linux-firmware-intel': {
                    'role': 'firmware', 'repository': 'main',
                    'version_resolved': '20251125-r1', 'license': 'custom',
                    'covers': ['intel-wifi-ax210'], 'rationale': 'iwlwifi microcode',
                },
            },
        }), encoding='utf-8')
        manifest = self.run_recorder([
            '--skip-index-fetch', '--hardware-package-manifest', str(package_manifest)])
        embedded = manifest['hardware_packages']
        self.assertEqual(embedded['status'], 'recorded')
        self.assertEqual(embedded['alpine_branch'], 'v3.23')
        record = embedded['packages']['linux-firmware-intel']
        self.assertEqual(record['license'], 'custom')
        self.assertEqual(record['repository'], 'main')
        self.assertEqual(record['covers'], ['intel-wifi-ax210'])

    def test_missing_hardware_package_manifest_is_unavailable(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        self.assertEqual(manifest['hardware_packages']['status'], 'unavailable')
        self.assertIn('reason', manifest['hardware_packages'])

    # -- metrics ---------------------------------------------------------

    def test_records_size_metrics_from_the_artifacts(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        metrics = manifest['metrics']
        self.assertEqual(metrics['iso']['bytes'], len(b'fake-iso-bytes'))
        self.assertEqual(metrics['initramfs']['bytes'], len(b'initramfs-bytes'))
        self.assertEqual(metrics['embedded_apks']['bytes'],
                         manifest['output_closure']['total_size'])
        self.assertEqual(metrics['embedded_apks']['note'], '2 packages')
        # No modloop in this fixture: reported as unavailable, never guessed.
        self.assertEqual(metrics['modloop']['status'], 'unavailable')

    def test_apkovl_size_is_measured_when_present(self):
        apkovl = self.extracted / 'aios.apkovl.tar.gz'
        apkovl.write_bytes(b'apkovl-bytes')
        manifest = self.run_recorder(['--skip-index-fetch'])
        self.assertEqual(manifest['metrics']['apkovl']['bytes'], len(b'apkovl-bytes'))
        self.assertEqual(manifest['metrics']['apkovl']['path'], 'aios.apkovl.tar.gz')

    def test_runtime_metrics_are_not_measured_rather_than_invented(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        for key in ('live_root', 'boot_time', 'minimum_tested_ram'):
            metric = manifest['metrics'][key]
            self.assertEqual(metric['status'], 'not_measured', key)
            self.assertTrue(metric['reason'])
            self.assertNotIn('bytes', metric)

    # -- artifacts -------------------------------------------------------

    def test_records_iso_and_kernel_identity(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        artifacts = manifest['artifacts']
        self.assertEqual(artifacts['iso']['sha256'], hashlib.sha256(b'fake-iso-bytes').hexdigest())
        self.assertEqual(artifacts['kernel']['sha256'],
                         hashlib.sha256(b'kernel-bytes').hexdigest())
        self.assertEqual(artifacts['initramfs']['status'], 'recorded')
        # No modloop file in this fixture: reported as unavailable, never guessed.
        self.assertEqual(artifacts['modloop']['status'], 'unavailable')
        self.assertEqual(manifest['kernel_identity']['status'], 'unavailable')

    # -- repositories ----------------------------------------------------

    def test_default_repositories_are_recorded_as_default(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        repos = {repo['role']: repo for repo in manifest['repository_indexes']['repositories']}
        self.assertEqual(repos['main']['source'], 'default')
        self.assertEqual(repos['community']['source'], 'default')
        self.assertEqual(repos['main']['apkindex_url'],
                         f'{CANONICAL_MAIN}/x86_64/APKINDEX.tar.gz')

    def test_repository_override_is_captured(self):
        override = 'https://mirror.example.invalid/alpine/v3.23/main'
        manifest = self.run_recorder(
            ['--skip-index-fetch'],
            repositories=[f'main={override}', f'community={CANONICAL_COMMUNITY}'],
        )
        repos = {repo['role']: repo for repo in manifest['repository_indexes']['repositories']}
        self.assertEqual(repos['main']['source'], 'override')
        self.assertEqual(repos['main']['url'], override)
        self.assertEqual(repos['main']['canonical_url'], CANONICAL_MAIN)
        self.assertEqual(repos['community']['source'], 'default')

    def test_apkindex_digest_is_recorded_for_a_reachable_repository(self):
        manifest = self.run_recorder(
            repositories=[f'main={self.local_repo.as_posix()}'])
        repo = manifest['repository_indexes']['repositories'][0]
        self.assertEqual(repo['apkindex']['status'], 'recorded')
        self.assertEqual(repo['apkindex']['sha256'],
                         hashlib.sha256(self.index_bytes).hexdigest())
        self.assertEqual(repo['apkindex']['size'], len(self.index_bytes))

    def test_index_drift_is_visible_between_two_recordings(self):
        first = self.run_recorder(repositories=[f'main={self.local_repo.as_posix()}'])
        (self.local_repo / 'x86_64' / 'APKINDEX.tar.gz').write_bytes(b'moved-repository-bytes')
        second = self.run_recorder(repositories=[f'main={self.local_repo.as_posix()}'])
        self.assertNotEqual(
            first['repository_indexes']['repositories'][0]['apkindex']['sha256'],
            second['repository_indexes']['repositories'][0]['apkindex']['sha256'],
        )

    def test_unreachable_index_is_reported_with_a_reason(self):
        missing = self.base / 'no-such-repo'
        manifest = self.run_recorder(repositories=[f'main={missing.as_posix()}'])
        index = manifest['repository_indexes']['repositories'][0]['apkindex']
        self.assertEqual(index['status'], 'unavailable')
        self.assertTrue(index['reason'])
        self.assertNotIn('sha256', index)

    # -- pins and claims -------------------------------------------------

    def test_source_pins_are_recorded_and_overrides_flagged(self):
        manifest = self.run_recorder(['--skip-index-fetch', '--effective', 'APORTS_REF=dddd'])
        pins = manifest['source_pins']
        self.assertEqual(pins['values']['APORTS_REF'], 'dddd')
        self.assertIn('APORTS_REF', pins['immutable_keys'])
        self.assertNotIn('ALPINE_BRANCH', pins['immutable_keys'])
        self.assertEqual(pins['moving_selection_keys'], ['ALPINE_BRANCH'])
        self.assertIn('APORTS_REF', pins['overrides'])
        self.assertEqual(pins['overrides']['APORTS_REF']['build_env_file'], 'aaaa')
        self.assertEqual(pins['overrides']['APORTS_REF']['effective'], 'dddd')

    def test_unoverridden_pins_have_no_override_record(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        self.assertEqual(manifest['source_pins']['overrides'], {})
        self.assertEqual(manifest['source_pins']['values']['ALPINE_BRANCH'], 'v3.23')

    def test_does_not_claim_byte_reproducibility(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        reproducibility = manifest['reproducibility']
        self.assertFalse(reproducibility['byte_reproducible'])
        self.assertEqual(
            reproducibility['package_resolution'],
            'output-closure-recorded-repository-index-sampled',
        )
        self.assertTrue(reproducibility['notes'])
        joined = ' '.join(reproducibility['notes']).lower()
        self.assertIn('post-build samples', joined)

    def test_repository_index_is_labeled_as_post_build_sample(self):
        manifest = self.run_recorder(['--skip-index-fetch'])
        indexes = manifest['repository_indexes']
        self.assertEqual(
            indexes['kind'],
            'post-build-moving-repository-index-sample',
        )
        self.assertEqual(indexes['sampling'], 'post-build')

    def test_output_is_deterministic_for_the_same_inputs(self):
        self.run_recorder(['--skip-index-fetch'])
        first = self.output.read_text(encoding='utf-8')
        self.run_recorder(['--skip-index-fetch'])
        self.assertEqual(first, self.output.read_text(encoding='utf-8'))

    def test_repeated_recording_uses_isolated_extraction_directories(self):
        text = SCRIPT.read_text(encoding='utf-8')
        self.assertIn('tempfile.mkdtemp(prefix="iso-", dir=work)', text)
        self.assertNotIn('root = work / "iso"', text)

    def extraction_args(self, *extra):
        return recorder.parse_args([
            '--iso', str(self.iso), '--work-dir', str(self.base / 'work'),
            '--skip-index-fetch', '--output', str(self.output), *extra,
        ])

    def readonly_extractor(self, iso, iso_path, dest):
        # Model xorriso's directory permissions, not its extraction internals.
        if iso_path != '/apks':
            return 'fixture has no boot artifacts'
        packages = dest / 'x86_64'
        packages.mkdir(parents=True)
        (packages / 'busybox-1.0-r0.apk').write_bytes(b'fixture package')
        (packages / 'busybox-1.0-r0.apk').chmod(0o444)
        packages.chmod(0o555)
        dest.chmod(0o555)
        return None

    def test_recording_removes_readonly_extraction_tree(self):
        args = self.extraction_args()
        with mock.patch.object(recorder, 'run_xorriso_extract', self.readonly_extractor), \
                mock.patch.object(recorder, 'iso_member_size', return_value={'status': 'unavailable'}):
            for _ in range(2):
                manifest = recorder.build_manifest(args)
                self.assertEqual(manifest['output_closure']['apk_count'], 1)
                self.assertEqual(list(args.work_dir.iterdir()), [],
                                 'successful recording leaked the read-only APK tree')

    def test_failed_recording_cleans_up_and_preserves_original_error(self):
        args = self.extraction_args()
        with mock.patch.object(recorder, 'run_xorriso_extract', self.readonly_extractor), \
                mock.patch.object(recorder, 'collect_closure', side_effect=OSError('hash read failed')):
            with self.assertRaisesRegex(OSError, 'hash read failed'):
                recorder.build_manifest(args)
        self.assertEqual(list(args.work_dir.iterdir()), [])

    def test_extractor_exception_cleans_up_partial_tree(self):
        args = self.extraction_args()

        def interrupted(iso, iso_path, dest):
            self.readonly_extractor(iso, iso_path, dest)
            (dest / 'x86_64').chmod(0o000)
            dest.chmod(0o000)
            raise OSError('extraction interrupted')

        with mock.patch.object(recorder, 'run_xorriso_extract', interrupted):
            with self.assertRaisesRegex(OSError, 'extraction interrupted'):
                recorder.build_manifest(args)
        self.assertEqual(list(args.work_dir.iterdir()), [])

    def test_keep_extraction_preserves_tree_and_permissions(self):
        args = self.extraction_args('--keep-extraction')
        with mock.patch.object(recorder, 'run_xorriso_extract', self.readonly_extractor), \
                mock.patch.object(recorder, 'iso_member_size', return_value={'status': 'unavailable'}):
            recorder.build_manifest(args)
        trees = list(args.work_dir.iterdir())
        self.assertEqual(len(trees), 1)
        packages = trees[0] / 'apks' / 'x86_64'
        self.assertEqual(packages.stat().st_mode & 0o777, 0o555)
        self.assertEqual((packages / 'busybox-1.0-r0.apk').read_bytes(), b'fixture package')

    def test_caller_extracted_root_is_never_cleaned_or_chmodded(self):
        args = self.extraction_args('--extracted-root', str(self.extracted))
        self.apks.chmod(0o555)
        with mock.patch.object(recorder, 'run_xorriso_extract') as extract, \
                mock.patch.object(recorder, 'iso_member_size', return_value={'status': 'unavailable'}):
            recorder.build_manifest(args)
        extract.assert_not_called()
        self.assertEqual(self.apks.stat().st_mode & 0o777, 0o555)
        self.assertEqual(len(list(self.apks.glob('*.apk'))), 2)
        self.assertFalse(args.work_dir.exists())

    def test_cleanup_does_not_follow_symlinks_outside_extraction(self):
        args = self.extraction_args()
        outside = self.base / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep'
        sentinel.write_bytes(b'not extraction data')
        sentinel.chmod(0o400)
        outside.chmod(0o500)

        def extract_with_links(iso, iso_path, dest):
            error = self.readonly_extractor(iso, iso_path, dest)
            if iso_path == '/apks':
                dest.chmod(0o755)
                (dest / 'external-directory').symlink_to(outside, target_is_directory=True)
                (dest / 'external-file').symlink_to(sentinel)
                (dest / 'broken-link').symlink_to(self.base / 'absent')
                dest.chmod(0o555)
            return error

        def legacy_resetperms(path):
            # Simulate older CPython's symlink-following permission recovery.
            # Cleanup must not delegate permission repair to that implementation.
            os.chmod(path, 0o700)

        with mock.patch.object(recorder, 'run_xorriso_extract', extract_with_links), \
                mock.patch.object(recorder, 'iso_member_size', return_value={'status': 'unavailable'}), \
                mock.patch.object(tempfile, '_resetperms', legacy_resetperms):
            recorder.build_manifest(args)
        self.assertEqual(list(args.work_dir.iterdir()), [])
        self.assertEqual(outside.stat().st_mode & 0o777, 0o500)
        self.assertEqual(sentinel.stat().st_mode & 0o777, 0o400)
        self.assertEqual(sentinel.read_bytes(), b'not extraction data')

    def test_default_scratch_directory_is_removed_without_empty_parent_leak(self):
        args = self.extraction_args()
        args.work_dir = None
        scratch = self.base / 'system-tmp'
        scratch.mkdir()
        with mock.patch.object(recorder.tempfile, 'tempdir', str(scratch)), \
                mock.patch.object(recorder, 'run_xorriso_extract', self.readonly_extractor), \
                mock.patch.object(recorder, 'iso_member_size', return_value={'status': 'unavailable'}):
            recorder.build_manifest(args)
        self.assertEqual(list(scratch.iterdir()), [])

    def test_cleanup_failure_is_not_reported_as_success(self):
        args = self.extraction_args()
        with mock.patch.object(recorder, 'run_xorriso_extract', self.readonly_extractor), \
                mock.patch.object(recorder, 'iso_member_size', return_value={'status': 'unavailable'}), \
                mock.patch.object(recorder.shutil, 'rmtree',
                                  side_effect=PermissionError('cleanup denied')):
            with self.assertRaisesRegex(PermissionError, 'cleanup denied'):
                recorder.main([
                    '--iso', str(args.iso), '--work-dir', str(args.work_dir),
                    '--skip-index-fetch', '--output', str(args.output),
                ])
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(shutil.which('xorriso'), 'optional real ISO extraction requires xorriso')
    def test_real_xorriso_readonly_tree_is_removed(self):
        self.apks.chmod(0o555)
        self.apks.parent.chmod(0o555)
        image = self.base / 'readonly.iso'
        created = subprocess.run([
            'xorriso', '-as', 'mkisofs', '-R', '-o', str(image), str(self.extracted),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(created.returncode, 0, created.stderr)
        work = self.base / 'real-extraction'
        result = subprocess.run([
            sys.executable, str(SCRIPT), '--iso', str(image), '--work-dir', str(work),
            '--skip-index-fetch', '--output', str(self.output),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.output.read_text())
        self.assertEqual(manifest['output_closure']['apk_count'], 2)
        self.assertEqual(list(work.iterdir()), [])

    # -- integration with the inspector ----------------------------------

    def test_inspector_verifies_a_freshly_recorded_manifest(self):
        self.run_recorder(['--skip-index-fetch'])
        args = [
            sys.executable, str(INSPECT),
            '--root', str(self.extracted),
            '--build-manifest', str(self.output),
            '--repo-build-env', str(self.build_env),
        ]
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        recorded = json.loads(result.stdout)['sections']['recorded_inputs']
        self.assertTrue(recorded['output_closure']['closure_matches_image'])
        self.assertTrue(recorded['source_pins']['matches_current_repo_pins'])


class MkimageWiringTests(unittest.TestCase):
    """mkimage.sh must actually invoke the recorder with the effective values."""

    def setUp(self):
        self.text = (ROOT / 'distro' / 'alpine' / 'mkimage.sh').read_text(encoding='utf-8')

    def test_recorder_is_invoked_per_iso(self):
        self.assertIn('record-build-manifest.py', self.text)
        self.assertIn('--output "$iso.build-manifest.json"', self.text)

    def test_effective_repositories_are_passed(self):
        self.assertIn('--repository "main=$REPO_MAIN"', self.text)
        self.assertIn('--repository "community=$REPO_COMMUNITY"', self.text)

    def test_effective_pins_are_passed(self):
        for key in ('ALPINE_BRANCH', 'IMAGE', 'APORTS_REF', 'LLAMA_REF', 'WHISPER_REF'):
            self.assertIn(f'--effective "{key}=${key}"', self.text)

    def test_every_world_file_is_recorded_per_iso(self):
        for role in ('base', 'x11', 'vm', 'devel', 'ai', 'hardware'):
            self.assertIn(f'--world "{role}=$AIOS_WORLD_{role.upper()}"', self.text)
        # The optional identity world is only recorded when it is built in.
        self.assertIn('${AIOS_WORLD_IDENTITY:+--world "identity=$AIOS_WORLD_IDENTITY"}', self.text)
        self.assertIn('--hardware-package-manifest', self.text)

    def test_build_inputs_env_records_effective_values(self):
        self.assertIn('ALPINE_BRANCH=$ALPINE_BRANCH', self.text)
        self.assertIn('> "$OUT_DIR/build-inputs.env"', self.text)
        self.assertNotIn('cp "$ROOT_DIR/build.env" "$OUT_DIR/build-inputs.env"', self.text)


if __name__ == '__main__':
    unittest.main()
