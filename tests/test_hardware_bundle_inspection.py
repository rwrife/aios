"""Stage-2 artifact validation: scripts/inspect-image.py --validate-hardware.

These tests build small but structurally real fixtures -- an apkovl tarball, an
embedded .apk closure, an Alpine-layout modloop tree and a gzipped cpio
initramfs -- and check that the hardware bundle validation passes on a complete
image and fails, with a specific reason and a deterministic exit code, on each
way the offline closure can be broken.
"""
import gzip
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'inspect-image.py'

_spec = importlib.util.spec_from_file_location('aios_inspect_image_hardware', SCRIPT)
inspect_image = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inspect_image)

KERNEL_VERSION = '6.18.52-0-lts'
HARDWARE_WORLD = ['linux-firmware-intel', 'sof-firmware', 'wireless-regdb', 'pciutils']
BASE_WORLD = ['alpine-base', 'networkmanager', 'qemu-guest-agent']


def build_cpio_newc(names):
    out = bytearray()

    def write_entry(name, data):
        name_bytes = name.encode('utf-8') + b'\x00'
        fields = [0, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name_bytes), 0]
        out.extend(b'070701' + ''.join(f'{value:08X}' for value in fields).encode('ascii'))
        out.extend(name_bytes)
        out.extend(b'\x00' * ((-len(out)) % 4))
        out.extend(data)
        out.extend(b'\x00' * ((-len(out)) % 4))

    for name in names:
        write_entry(name, b'fake')
    write_entry('TRAILER!!!', b'')
    return bytes(out)


def build_apkovl(path, world, services=None):
    services = services or {'sysinit': ['devfs', 'udev', 'udev-trigger', 'hwdrivers', 'modloop']}
    with tarfile.open(path, 'w:gz') as archive:
        content = ('\n'.join(sorted(world)) + '\n').encode()
        info = tarfile.TarInfo('etc/apk/world')
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
        repos = b'https://dl-cdn.alpinelinux.org/alpine/v3.23/main\n'
        info = tarfile.TarInfo('etc/apk/repositories')
        info.size = len(repos)
        archive.addfile(info, io.BytesIO(repos))
        for level, names in services.items():
            for service in names:
                info = tarfile.TarInfo(f'etc/runlevels/{level}/{service}')
                info.type = tarfile.SYMTYPE
                info.linkname = f'/etc/init.d/{service}'
                archive.addfile(info)


def build_apk(path, files=(), symlinks=()):
    """Build an .apk-shaped gzip tar holding firmware members."""
    with tarfile.open(path, 'w:gz') as archive:
        for name in files:
            info = tarfile.TarInfo(name)
            info.size = 4
            archive.addfile(info, io.BytesIO(b'fake'))
        for name, target in symlinks:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)


def coverage_fixture():
    def entry(entry_id, family, bus, pci_id, modules, deps, requirements, packages, usb_id=None):
        return {
            'id': entry_id,
            'family': family,
            'bus': bus,
            'ids': {'pci_id': pci_id, 'usb_id': usb_id, 'subsystem_id': None,
                    'subsystem_id_status': 'pending_inventory'},
            'kernel_module': modules,
            'module_dependencies': deps,
            'firmware_files': [pattern for group in requirements for pattern in group['any_of']],
            'firmware_requirements': requirements,
            'firmware_package': packages,
            'status': 'untested',
        }

    return {
        'schema_version': 2,
        'alpine_branch': 'v3.23',
        'entries': [
            entry('intel-wifi-ax210', 'wifi', 'pci', '8086:2725', ['iwlwifi', 'iwlmvm'],
                  ['cfg80211', 'mac80211'],
                  [{'id': 'iwlwifi-ty-a0-gf-a0-ucode',
                    'any_of': ['iwlwifi-ty-a0-gf-a0-*.ucode',
                               'intel/iwlwifi/iwlwifi-ty-a0-gf-a0-*.ucode']}],
                  ['linux-firmware-intel']),
            entry('audio-sof-intel', 'audio', 'pci', '8086:9dc8', ['snd_sof'], [],
                  [{'id': 'sof-cnl-firmware', 'any_of': ['intel/sof/sof-cnl.ri']},
                   {'id': 'sof-cnl-topology',
                    'any_of': ['intel/sof-tplg/sof-cnl-nocodec.tplg']}],
                  ['sof-firmware']),
            entry('nvidia-nouveau-turing-tu117', 'gpu', 'pci', '10de:1f82', ['nouveau'], [],
                  [], []),
            entry('storage-nvme', 'storage', 'pci', None, ['nvme', 'nvme_core'], [], [], []),
            entry('usb-xhci', 'usb', 'pci', None, ['xhci_hcd', 'xhci_pci'], [], [], []),
            entry('display-stack-drm-kms', 'display-stack', 'virtual', None,
                  ['drm', 'drm_kms_helper'], [], [], []),
        ],
    }


def package_manifest_fixture():
    return {
        'schema_version': 1,
        'alpine_branch': 'v3.23',
        'world_file': 'distro/alpine/apks/world.hardware',
        'packages': {
            'linux-firmware-intel': {'role': 'firmware', 'repository': 'main',
                                     'version_resolved': '20251125-r1', 'license': 'custom',
                                     'covers': ['intel-wifi-ax210']},
            'sof-firmware': {'role': 'firmware', 'repository': 'community',
                             'version_resolved': '2025.05-r0',
                             'license': 'BSD-3-Clause AND MIT AND ISC',
                             'covers': ['audio-sof-intel']},
            'wireless-regdb': {'role': 'regulatory', 'repository': 'main',
                               'version_resolved': '2025.07.10-r0', 'license': 'ISC',
                               'covers': ['intel-wifi-ax210']},
            'pciutils': {'role': 'diagnostics', 'repository': 'main',
                         'version_resolved': '3.14.0-r0', 'license': 'GPL-2.0-only',
                         'covers': []},
        },
    }


class HardwareBundleValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'root'
        self.apks = self.root / 'apks' / 'x86_64'
        self.apks.mkdir(parents=True)
        self.modloop = self.base / 'modloop'
        self.modules = self.modloop / 'modules' / KERNEL_VERSION
        self.modules.mkdir(parents=True)
        self.firmware = self.modloop / 'modules' / 'firmware'
        self.firmware.mkdir(parents=True)

        self.write_apkovl(BASE_WORLD + HARDWARE_WORLD)
        self.write_packages()
        self.write_modules()
        self.write_initramfs(['usr/lib/modules/x/nvme.ko', 'usr/lib/modules/x/nvme-core.ko',
                              'usr/lib/modules/x/xhci-hcd.ko', 'usr/lib/modules/x/xhci-pci.ko'])

        self.coverage = self.base / 'hardware-coverage.json'
        self.coverage.write_text(json.dumps(coverage_fixture()), encoding='utf-8')
        self.packages_manifest = self.base / 'hardware-packages.json'
        self.packages_manifest.write_text(json.dumps(package_manifest_fixture()), encoding='utf-8')
        self.world_file = self.base / 'world.hardware'
        self.world_file.write_text('# selected hardware\n' + '\n'.join(HARDWARE_WORLD) + '\n',
                                   encoding='utf-8')
        self.build_manifest = self.base / 'build-manifest.json'
        self.write_build_manifest()

    # -- fixture writers -------------------------------------------------

    def write_apkovl(self, world):
        self.apkovl = self.root / 'aios.apkovl.tar.gz'
        build_apkovl(self.apkovl, world)

    def write_packages(self, skip=(), intel_files=None, sof_files=None, symlinks=()):
        for apk in self.apks.glob('*.apk'):
            apk.unlink()
        catalog = {
            'linux-firmware-intel-20251125-r1.apk': (
                intel_files if intel_files is not None else [
                    'lib/firmware/iwlwifi-ty-a0-gf-a0-89.ucode.zst',
                    'lib/firmware/iwlwifi-cc-a0-77.ucode.zst',
                ],
                symlinks,
            ),
            'sof-firmware-2025.05-r0.apk': (
                sof_files if sof_files is not None else [
                    'lib/firmware/intel/sof/sof-cnl.ri',
                    'lib/firmware/intel/sof-tplg/sof-cnl-nocodec.tplg',
                ],
                (),
            ),
            'wireless-regdb-2025.07.10-r0.apk': (['lib/firmware/regulatory.db'], ()),
            'pciutils-3.14.0-r0.apk': (['usr/bin/lspci'], ()),
            'busybox-1.37.0-r0.apk': (['bin/busybox'], ()),
        }
        for filename, (files, links) in catalog.items():
            if any(filename.startswith(name + '-') for name in skip):
                continue
            build_apk(self.apks / filename, files, links)

    def write_modules(self, present=('iwlwifi', 'iwlmvm', 'cfg80211', 'mac80211', 'snd_sof',
                                     'nouveau', 'nvme', 'nvme-core', 'xhci-hcd', 'xhci-pci'),
                      dep_extra=None, dep_skip=(), builtin=('kernel/drivers/gpu/drm/drm.ko',
                                                            'kernel/drivers/gpu/drm/drm_kms_helper.ko'),
                      aliases=None):
        for path in self.modules.glob('*.ko'):
            path.unlink()
        for name in present:
            (self.modules / f'{name}.ko').write_bytes(b'fake-module')
        # depmod writes one line per module, dependencies or not.
        dependencies = {f'{name}.ko': [] for name in present}
        dependencies.update({
            'iwlwifi.ko': ['cfg80211.ko', 'mac80211.ko'],
            'iwlmvm.ko': ['iwlwifi.ko'],
        })
        if dep_extra:
            dependencies.update(dep_extra)
        for name in dep_skip:
            dependencies.pop(f'{name}.ko', None)
        (self.modules / 'modules.dep').write_text(
            ''.join(f'{target}: {" ".join(deps)}\n' for target, deps in dependencies.items()),
            encoding='utf-8')
        (self.modules / 'modules.builtin').write_text(
            ''.join(f'{name}\n' for name in builtin), encoding='utf-8')
        (self.modules / 'modules.alias').write_text(
            aliases if aliases is not None else
            'alias pci:v00008086d00002725sv*sd*bc*sc*i* iwlwifi\n'
            'alias pci:v00008086d00009DC8sv*sd*bc*sc*i* snd_sof\n'
            'alias pci:v000010DEd*sv*sd*bc03sc*i* nouveau\n',
            encoding='utf-8')

    def write_firmware(self, files=(), symlinks=()):
        for name in files:
            path = self.firmware / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fake-firmware')
        for name, target in symlinks:
            path = self.firmware / name
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.symlink_to(target)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f'symlinks unavailable on this platform: {error}')

    def write_initramfs(self, names):
        boot = self.root / 'boot'
        boot.mkdir(exist_ok=True)
        (boot / 'initramfs-lts').write_bytes(gzip.compress(build_cpio_newc(names)))

    def write_build_manifest(self, worlds=('base', 'vm', 'hardware'), hardware_packages=True):
        recorded = []
        for role in worlds:
            packages = HARDWARE_WORLD if role == 'hardware' else (
                ['qemu-guest-agent'] if role == 'vm' else BASE_WORLD)
            recorded.append({'role': role, 'path': f'world.{role}', 'status': 'recorded',
                             'sha256': '0' * 64, 'package_count': len(packages),
                             'packages': sorted(packages)})
        manifest = {
            'schema_version': 1,
            'kind': 'aios-build-manifest',
            'package_worlds': {'kind': 'requested-world-package-sets', 'worlds': recorded},
            'output_closure': {'status': 'recorded', 'packages': [
                {'filename': apk.name, 'sha256': inspect_image.sha256_file(apk),
                 'size': apk.stat().st_size}
                for apk in sorted(self.apks.glob('*.apk'))
            ]},
        }
        if hardware_packages:
            manifest['hardware_packages'] = {
                'status': 'recorded',
                'source': 'hardware-packages.json',
                'sha256': '2' * 64,
                'packages': package_manifest_fixture()['packages'],
            }
        self.build_manifest.write_text(json.dumps(manifest), encoding='utf-8')

    # -- runner ----------------------------------------------------------

    def run_tool(self, extra=(), validate=True):
        args = [
            sys.executable, str(SCRIPT),
            '--root', str(self.root),
            '--modloop-root', str(self.modloop),
            '--coverage-manifest', str(self.coverage),
            '--hardware-package-manifest', str(self.packages_manifest),
            '--hardware-world', str(self.world_file),
            '--build-manifest', str(self.build_manifest),
            '-o', str(self.base / 'report.json'),
        ]
        if validate:
            args.append('--validate-hardware')
        args.extend(extra)
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        report = json.loads((self.base / 'report.json').read_text(encoding='utf-8'))
        return result, report['sections']['hardware_bundle']

    def checks(self, section):
        return {check['name']: check for check in section['checks']}

    # -- the healthy image -----------------------------------------------

    def test_complete_bundle_passes_with_exit_zero(self):
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_OK, msg=result.stderr)
        self.assertEqual(section['result'], 'passed')
        self.assertEqual(section['failed_checks'], [])
        self.assertEqual(section['incomplete_checks'], [])

    def test_stale_apk_digest_fails_hardware_validation(self):
        manifest = json.loads(self.build_manifest.read_text(encoding='utf-8'))
        manifest['output_closure']['packages'][0]['sha256'] = '0' * 64
        self.build_manifest.write_text(json.dumps(manifest), encoding='utf-8')
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        self.assertEqual(section['result'], 'failed')
        self.assertIn('exact_output_closure', section['failed_checks'])
        self.assertIn('FAILED exact_output_closure', result.stderr)
        check = self.checks(section)['exact_output_closure']
        self.assertEqual(len(check['sha256_mismatches']), 1)

    def test_duplicate_apk_filename_is_rejected_instead_of_overwritten(self):
        apk = sorted(self.apks.glob('*.apk'))[0]
        duplicate_dir = self.apks / 'duplicate'
        duplicate_dir.mkdir()
        (duplicate_dir / apk.name).write_bytes(apk.read_bytes())
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        self.assertEqual(self.checks(section)['exact_output_closure']['duplicate_image_filenames'],
                         [apk.name])

    def test_duplicate_recorded_filename_is_rejected_instead_of_overwritten(self):
        manifest = json.loads(self.build_manifest.read_text(encoding='utf-8'))
        package = manifest['output_closure']['packages'][0]
        manifest['output_closure']['packages'].append(package.copy())
        self.build_manifest.write_text(json.dumps(manifest), encoding='utf-8')
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        self.assertEqual(self.checks(section)['exact_output_closure']['duplicate_recorded_filenames'],
                         [package['filename']])

    def test_nonhardware_package_missing_or_extra_fails_exact_closure(self):
        # The top-level hardware presence check alone cannot catch this.
        apk = self.apks / 'busybox-1.37.0-r0.apk'
        apk.rename(self.apks / 'unrecorded-1.0-r0.apk')
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        self.assertEqual(self.checks(section)['offline_package_availability']['status'], 'ok')
        check = self.checks(section)['exact_output_closure']
        self.assertEqual(check['missing_from_image'], ['busybox-1.37.0-r0.apk'])
        self.assertEqual(check['unexpected_in_image'], ['unrecorded-1.0-r0.apk'])

    def test_missing_recorded_closure_is_incomplete(self):
        original = json.loads(self.build_manifest.read_text(encoding='utf-8'))
        for closure in (None, {}, {'status': 'unavailable', 'packages': []}):
            with self.subTest(closure=closure):
                manifest = dict(original)
                manifest['output_closure'] = closure
                self.build_manifest.write_text(json.dumps(manifest), encoding='utf-8')
                result, section = self.run_tool()
                self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_INCOMPLETE)
                self.assertIn('exact_output_closure', section['incomplete_checks'])
                self.assertIn('INCOMPLETE exact_output_closure', result.stderr)

    def test_missing_apk_directory_is_incomplete_even_with_recorded_hashes(self):
        result, section = self.run_tool(extra=('--apks-dir', str(self.base / 'missing-apks')))
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_INCOMPLETE)
        self.assertIn('exact_output_closure', section['incomplete_checks'])

    def test_inventory_mode_reports_bad_hash_but_preserves_exit_zero(self):
        manifest = json.loads(self.build_manifest.read_text(encoding='utf-8'))
        manifest['output_closure']['packages'][0]['sha256'] = '0' * 64
        self.build_manifest.write_text(json.dumps(manifest), encoding='utf-8')
        result, section = self.run_tool(validate=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn('exact_output_closure', section['failed_checks'])

    def test_validation_never_claims_physical_support(self):
        _, section = self.run_tool()
        self.assertFalse(section['sets_physical_status'])
        self.assertIn('untested', section['note'])
        statuses = {entry['status'] for entry in json.loads(
            self.coverage.read_text(encoding='utf-8'))['entries']}
        self.assertEqual(statuses, {'untested'})

    def test_compressed_firmware_satisfies_an_uncompressed_path(self):
        _, section = self.run_tool()
        devices = {device['id']: device
                   for device in self.checks(section)['firmware_files_present']['devices']}
        self.assertEqual(devices['intel-wifi-ax210']['status'], 'matched')
        requirement = devices['intel-wifi-ax210']['requirements'][0]
        self.assertIn('iwlwifi-ty-a0-gf-a0-89.ucode.zst', requirement['in_packages'])

    def test_every_co_required_firmware_group_is_checked_separately(self):
        _, section = self.run_tool()
        devices = {device['id']: device
                   for device in self.checks(section)['firmware_files_present']['devices']}
        sof = devices['audio-sof-intel']
        self.assertEqual([item['requirement'] for item in sof['requirements']],
                         ['sof-cnl-firmware', 'sof-cnl-topology'])
        self.assertTrue(all(item['status'] == 'matched' for item in sof['requirements']))

    def test_one_missing_co_required_firmware_group_fails(self):
        # The DSP image ships but its topology does not: the device still fails.
        self.write_packages(sof_files=['lib/firmware/intel/sof/sof-cnl.ri'])
        self.write_build_manifest()
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['firmware_files_present']['failures']
        self.assertEqual([(item['id'], item['requirement']) for item in failures],
                         [('audio-sof-intel', 'sof-cnl-topology')])

    def test_firmware_only_in_the_modloop_does_not_satisfy_a_requirement(self):
        # world.hardware populates /lib/firmware on the live and installed
        # system, so the modloop's pruned copy is evidence, not coverage.
        self.write_packages(intel_files=['lib/firmware/iwlwifi-cc-a0-77.ucode.zst'])
        self.write_build_manifest()
        self.write_firmware(files=('iwlwifi-ty-a0-gf-a0-89.ucode',))
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['firmware_files_present']['failures']
        self.assertEqual([item['id'] for item in failures], ['intel-wifi-ax210'])
        self.assertEqual(failures[0]['modloop_only_matches'],
                         ['iwlwifi-ty-a0-gf-a0-89.ucode'])
        self.assertIn('modloop', failures[0]['reason'])

    def test_firmware_coverage_is_incomplete_without_the_package_scan(self):
        result, section = self.run_tool(validate=False)
        check = self.checks(section)['firmware_files_present']
        self.assertEqual(check['status'], 'unavailable')
        self.assertIn('--apks-dir', check['reason'])
        self.assertEqual(result.returncode, 0)

    def test_builtin_modules_count_as_present(self):
        _, section = self.run_tool()
        closure = self.checks(section)['module_dependency_closure']
        self.assertEqual(closure['status'], 'ok')
        self.assertEqual(closure['by_kernel_version'][KERNEL_VERSION]['modules_builtin'],
                         ['drm', 'drm_kms_helper'])

    def test_class_only_gpu_alias_matches_a_recorded_device_id(self):
        _, section = self.run_tool()
        self.assertEqual(self.checks(section)['device_alias_mapping']['status'], 'ok')

    # -- each way the bundle can break -----------------------------------

    def test_overlay_world_missing_a_hardware_package_fails(self):
        self.write_apkovl(BASE_WORLD + ['linux-firmware-intel', 'sof-firmware', 'pciutils'])
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['hardware_world_parity']['failures']
        self.assertEqual([item['package'] for item in failures], ['wireless-regdb'])
        self.assertIn('hardware_world_parity', result.stderr)

    def test_package_missing_from_the_embedded_closure_fails(self):
        self.write_packages(skip=('wireless-regdb',))
        self.write_build_manifest()
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['offline_package_availability']['failures']
        self.assertEqual([item['package'] for item in failures], ['wireless-regdb'])

    def test_firmware_package_without_a_license_record_fails(self):
        manifest = package_manifest_fixture()
        del manifest['packages']['sof-firmware']
        self.packages_manifest.write_text(json.dumps(manifest), encoding='utf-8')
        self.write_build_manifest(hardware_packages=False)
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['firmware_license_provenance']['failures']
        self.assertEqual([item['package'] for item in failures], ['sof-firmware'])
        self.assertIn('license', failures[0]['reason'])

    def test_firmware_package_without_a_repository_record_fails(self):
        manifest = package_manifest_fixture()
        manifest['packages']['sof-firmware']['repository'] = ''
        self.packages_manifest.write_text(json.dumps(manifest), encoding='utf-8')
        self.write_build_manifest(hardware_packages=False)
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['firmware_license_provenance']['failures']
        self.assertEqual([item['package'] for item in failures], ['sof-firmware'])
        self.assertEqual(failures[0]['missing_fields'], ['repository'])

    def test_coverage_package_missing_from_the_hardware_world_fails(self):
        self.world_file.write_text('linux-firmware-intel\nwireless-regdb\npciutils\n',
                                   encoding='utf-8')
        self.write_build_manifest(worlds=('base', 'vm'))
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['firmware_license_provenance']['failures']
        self.assertEqual([item['package'] for item in failures], ['sof-firmware'])
        self.assertIn('world.hardware', failures[0]['reason'])

    def test_missing_firmware_file_fails_for_the_named_device(self):
        self.write_packages(sof_files=['lib/firmware/intel/sof/sof-apl.ri'])
        self.write_build_manifest()
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['firmware_files_present']['failures']
        self.assertEqual({item['id'] for item in failures}, {'audio-sof-intel'})
        self.assertEqual(sorted(item['requirement'] for item in failures),
                         ['sof-cnl-firmware', 'sof-cnl-topology'])
        self.assertEqual(failures[0]['firmware_package'], ['sof-firmware'])

    def test_dangling_symlink_in_a_shipped_package_fails(self):
        self.write_packages(symlinks=(('lib/firmware/iwlwifi-ty-a0-gf-a0-90.ucode.zst',
                                       'missing-target.ucode.zst'),))
        self.write_build_manifest()
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['firmware_symlink_targets']['failures']
        self.assertEqual([item['path'] for item in failures],
                         ['iwlwifi-ty-a0-gf-a0-90.ucode.zst'])
        self.assertEqual(failures[0]['source'], 'package:linux-firmware-intel')

    def test_resolvable_package_symlink_passes(self):
        self.write_packages(symlinks=(('lib/firmware/iwlwifi-ty-a0-gf-a0-90.ucode.zst',
                                       'iwlwifi-ty-a0-gf-a0-89.ucode.zst'),))
        self.write_build_manifest()
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_OK, msg=result.stderr)
        self.assertEqual(self.checks(section)['firmware_symlink_targets']['status'], 'ok')

    def test_directory_symlink_target_resolves(self):
        self.write_packages(intel_files=[
            'lib/firmware/iwlwifi-ty-a0-gf-a0-89.ucode.zst',
            'lib/firmware/nvidia/ad102/gsp/bootloader.bin.zst',
        ], symlinks=(('lib/firmware/nvidia/ad103', 'ad102'),))
        self.write_build_manifest()
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_OK, msg=result.stderr)
        check = self.checks(section)['firmware_symlink_targets']
        self.assertEqual(check['status'], 'ok')
        self.assertEqual(check['unresolved_unselected_symlinks'], [])

    def test_unselected_dangling_package_symlink_is_reported_without_failing(self):
        self.write_packages(symlinks=(('lib/firmware/intel/ish/ish.bin.zst',
                                       '../../HP/ish/ish.bin.zst'),))
        self.write_build_manifest()
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_OK, msg=result.stderr)
        check = self.checks(section)['firmware_symlink_targets']
        self.assertEqual(check['status'], 'ok')
        self.assertEqual([item['path'] for item in check['unresolved_unselected_symlinks']],
                         ['intel/ish/ish.bin.zst'])

    def test_unselected_dangling_modloop_symlink_is_reported_without_failing(self):
        self.write_firmware(symlinks=(('unrelated-vendor/blob.bin', '../nowhere/blob.bin'),))
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_OK, msg=result.stderr)
        check = self.checks(section)['firmware_symlink_targets']
        self.assertEqual(check['status'], 'ok')
        self.assertEqual([item['path'] for item in check['unresolved_unselected_symlinks']],
                         ['unrelated-vendor/blob.bin'])

    def test_missing_module_dependency_fails(self):
        self.write_modules(present=('iwlwifi', 'iwlmvm', 'cfg80211', 'snd_sof', 'nouveau',
                                    'nvme', 'nvme-core', 'xhci-hcd', 'xhci-pci'))
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['module_dependency_closure']['failures']
        reasons = {item['reason'] for item in failures}
        self.assertTrue(any('mac80211' in json.dumps(item) for item in failures))
        self.assertTrue(any('dependency' in reason for reason in reasons))

    def test_selected_module_without_a_modules_dep_entry_fails(self):
        # depmod writes a line per module even when it has no dependencies, so a
        # shipped module with no entry means truncated depmod data.
        self.write_modules(dep_skip=('nouveau',))
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['module_dependency_closure']['failures']
        self.assertEqual([item['module'] for item in failures], ['nouveau'])
        self.assertIn('modules.dep entry', failures[0]['reason'])

    def test_normalized_and_compressed_dep_entries_still_count(self):
        # xhci-pci.ko.zst with an underscored modules.dep target is the same module.
        self.write_modules(present=('iwlwifi', 'iwlmvm', 'cfg80211', 'mac80211', 'snd_sof',
                                    'nouveau', 'nvme', 'nvme-core', 'xhci-hcd'),
                           dep_extra={'kernel/drivers/usb/host/xhci_pci.ko.zst': []})
        (self.modules / 'xhci-pci.ko.zst').write_bytes(b'fake-module')
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_OK, msg=result.stderr)
        self.assertEqual(self.checks(section)['module_dependency_closure']['status'], 'ok')

    def test_boot_critical_module_missing_from_initramfs_fails(self):
        self.write_initramfs(['usr/lib/modules/x/nvme.ko', 'usr/lib/modules/x/xhci-hcd.ko'])
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['boot_critical_modules_in_initramfs']['failures']
        self.assertEqual(sorted(item['module'] for item in failures), ['nvme_core', 'xhci_pci'])

    def test_unmatched_device_id_fails(self):
        (self.modules / 'modules.alias').write_text(
            'alias pci:v00008086d00002725sv*sd*bc*sc*i* iwlwifi\n', encoding='utf-8')
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['device_alias_mapping']['failures']
        self.assertIn('nvidia-nouveau-turing-tu117', [item['id'] for item in failures])

    def test_device_id_claimed_only_by_an_unexpected_module_fails(self):
        self.write_modules(present=('iwlwifi', 'iwlmvm', 'cfg80211', 'mac80211', 'snd_sof',
                                    'nouveau', 'nvme', 'nvme-core', 'xhci-hcd', 'xhci-pci',
                                    'wrong-driver'),
                           aliases='alias pci:v00008086d00002725sv*sd*bc*sc*i* wrong_driver\n'
                                   'alias pci:v00008086d00009DC8sv*sd*bc*sc*i* snd_sof\n'
                                   'alias pci:v000010DEd*sv*sd*bc03sc*i* nouveau\n')
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        failures = self.checks(section)['device_alias_mapping']['failures']
        self.assertEqual([item['id'] for item in failures], ['intel-wifi-ax210'])
        self.assertEqual(failures[0]['matched_modules'], ['wrong_driver'])
        self.assertIn('does not expect', failures[0]['reason'])

    def test_an_extra_alias_next_to_the_expected_one_is_reported_not_failed(self):
        self.write_modules(present=('iwlwifi', 'iwlmvm', 'cfg80211', 'mac80211', 'snd_sof',
                                    'nouveau', 'nvme', 'nvme-core', 'xhci-hcd', 'xhci-pci',
                                    'extra-driver'),
                           aliases='alias pci:v00008086d00002725sv*sd*bc*sc*i* iwlwifi\n'
                                   'alias pci:v00008086d00002725sv*sd*bc*sc*i* extra_driver\n'
                                   'alias pci:v00008086d00009DC8sv*sd*bc*sc*i* snd_sof\n'
                                   'alias pci:v000010DEd*sv*sd*bc03sc*i* nouveau\n')
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_OK, msg=result.stderr)
        check = self.checks(section)['device_alias_mapping']
        self.assertEqual(check['status'], 'ok')
        self.assertEqual([item['id'] for item in check['ambiguous_ids']], ['intel-wifi-ax210'])

    def test_missing_vm_world_fails(self):
        self.write_build_manifest(worlds=('base', 'hardware'))
        result, section = self.run_tool()
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_FAILED)
        self.assertEqual(self.checks(section)['virtual_hardware_world_preserved']['status'],
                         'failed')

    # -- missing evidence is never silently a pass -----------------------

    def test_missing_modloop_is_incomplete_not_passed(self):
        result, section = self.run_tool(extra=('--modloop-root', str(self.base / 'nope')))
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_INCOMPLETE)
        self.assertEqual(section['result'], 'incomplete')
        self.assertIn('module_dependency_closure', section['incomplete_checks'])
        self.assertIn('INCOMPLETE', result.stderr)

    def test_missing_coverage_manifest_is_incomplete(self):
        result, section = self.run_tool(
            extra=('--coverage-manifest', str(self.base / 'missing.json')))
        self.assertEqual(result.returncode, inspect_image.EXIT_VALIDATION_INCOMPLETE)
        self.assertEqual(section['status'], 'unavailable')

    def test_report_without_validation_flag_still_exits_zero(self):
        self.write_apkovl(BASE_WORLD)
        result, section = self.run_tool(validate=False)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(section['result'], 'failed')

    def test_report_is_deterministic(self):
        _, first = self.run_tool()
        _, second = self.run_tool()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


class FirmwareMatchingTests(unittest.TestCase):
    def test_compression_suffixes_are_stripped_for_matching(self):
        paths = ['iwlwifi-cc-a0-77.ucode.zst', 'intel/sof/sof-cnl.ri', 'other.bin.xz']
        self.assertEqual(inspect_image.firmware_matches('iwlwifi-cc-a0-*.ucode', paths),
                         ['iwlwifi-cc-a0-77.ucode.zst'])
        self.assertEqual(inspect_image.firmware_matches('intel/sof/sof-cnl.ri', paths),
                         ['intel/sof/sof-cnl.ri'])
        self.assertEqual(inspect_image.firmware_matches('other.bin', paths), ['other.bin.xz'])
        self.assertEqual(inspect_image.firmware_matches('absent/*.bin', paths), [])

    def test_apk_filenames_split_into_name_and_version(self):
        self.assertEqual(inspect_image.apk_atom('linux-firmware-intel-20251125-r1.apk'),
                         ('linux-firmware-intel', '20251125-r1'))
        self.assertEqual(inspect_image.apk_atom('py3-cryptography-42.0.5-r0.apk'),
                         ('py3-cryptography', '42.0.5-r0'))
        self.assertEqual(inspect_image.apk_atom('not-a-package.txt'), (None, None))

    def test_gpu_probes_use_the_display_base_class(self):
        self.assertIn('bc03', inspect_image.pci_modalias_probe('10de:1f82', '03'))
        self.assertIn('bc*', inspect_image.pci_modalias_probe('8086:2725'))
        self.assertTrue(inspect_image.alias_matches_probe(
            'pci:v000010DEd*sv*sd*bc03sc*i*', inspect_image.pci_modalias_probe('10de:1f82', '03')))


if __name__ == '__main__':
    unittest.main()
