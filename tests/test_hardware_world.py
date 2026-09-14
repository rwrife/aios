"""Stage-2 offline hardware bundle: package selection and build wiring.

These tests read the real world files, profile script, overlay generator and
build scripts. They prove that apks/world.hardware reaches the profile package
list, the overlay /etc/apk/world (which is also the installed world), the
apkovl cache hash and the per-ISO build manifest, and that the selection stays
inside one Alpine release with no proprietary drivers.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APKS = ROOT / 'distro' / 'alpine' / 'apks'
WORLD_HARDWARE = APKS / 'world.hardware'
PACKAGE_MANIFEST = ROOT / 'docs' / 'qa' / 'hardware-packages.json'
COVERAGE_MANIFEST = ROOT / 'docs' / 'qa' / 'hardware-coverage.json'
PROFILE = ROOT / 'distro' / 'alpine' / 'profiles' / 'mkimg.aios.sh'
GENAPKOVL = ROOT / 'distro' / 'alpine' / 'apkovl' / 'genapkovl-aios.sh'
MKIMAGE = ROOT / 'distro' / 'alpine' / 'mkimage.sh'
BUILD_ENV = ROOT / 'distro' / 'alpine' / 'build.env'

# An apk "world" atom: a bare package name. Anything with a version constraint
# (=, <, >, ~) or a repository tag (@edge) would break the one-release rule.
ATOM = re.compile(r'^[a-z0-9][a-z0-9._+-]*$')


def read_atoms(path):
    return [
        line.strip() for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.strip().startswith('#')
    ]


def parse_env(path):
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
    return values


def world_env(directory, hardware=WORLD_HARDWARE, identity=None):
    env = dict(
        os.environ,
        AIOS_STAGE_DIR=str(directory),
        AIOS_OVERLAY_DIR=str(ROOT / 'distro/alpine/overlay'),
        AIOS_REPOSITORIES='https://example.invalid/main',
        AIOS_WORLD_HARDWARE=str(hardware),
        AIOS_WORLD_IDENTITY=str(identity) if identity else '',
        AIOS_APKOVL_SCRIPT='genapkovl-aios.sh',
    )
    for name in ('BASE', 'X11', 'VM', 'DEVEL', 'AI'):
        env['AIOS_WORLD_' + name] = str(APKS / ('world.' + name.lower()))
    return env


def profile_value(env, variable='apks'):
    result = subprocess.run(
        ['sh', '-c',
         f'profile_standard() {{ :; }}; . "$1"; profile_aios; printf "%s" "${variable}"',
         'sh', str(PROFILE)],
        env=env, check=True, capture_output=True, text=True,
    )
    return result.stdout


class HardwareWorldSelectionTests(unittest.TestCase):
    """The package list itself: real names, one release, no proprietary drivers."""

    def setUp(self):
        self.atoms = read_atoms(WORLD_HARDWARE)
        self.manifest = json.loads(PACKAGE_MANIFEST.read_text(encoding='utf-8'))
        self.coverage = json.loads(COVERAGE_MANIFEST.read_text(encoding='utf-8'))

    def test_world_file_holds_plain_atoms_only(self):
        self.assertTrue(self.atoms)
        self.assertEqual(len(self.atoms), len(set(self.atoms)), 'duplicate atom')
        for atom in self.atoms:
            self.assertRegex(atom, ATOM, f'{atom} is not a plain package atom')

    def test_one_release_family_with_no_repository_tags_or_pins(self):
        self.assertEqual(parse_env(BUILD_ENV)['ALPINE_BRANCH'], 'v3.23')
        self.assertEqual(self.manifest['alpine_branch'], 'v3.23')
        self.assertEqual(self.coverage['alpine_branch'], 'v3.23')
        for atom in self.atoms:
            self.assertNotIn('@', atom)
            for operator in ('=', '<', '>', '~'):
                self.assertNotIn(operator, atom)
        for name, record in self.manifest['packages'].items():
            self.assertIn(record['repository'], ('main', 'community'),
                          f'{name} is not in a v3.23 repository')

    def test_every_atom_has_a_resolved_repository_version_and_license(self):
        self.assertEqual(set(self.atoms), set(self.manifest['packages']))
        for name, record in self.manifest['packages'].items():
            self.assertTrue(record['version_resolved'], f'{name} has no resolved version')
            self.assertTrue(record['license'], f'{name} has no recorded license')
            self.assertIn(record['role'],
                          ('firmware', 'regulatory', 'userspace', 'diagnostics'))
        resolution = self.manifest['resolution']
        self.assertEqual(resolution['status'], 'resolved-from-repository-index')
        self.assertEqual(len(resolution['indexes']), 2)
        for index in resolution['indexes']:
            self.assertRegex(index['apkindex_sha256'], r'^[0-9a-f]{64}$')

    def test_every_coverage_firmware_package_is_selected(self):
        required = {
            package
            for entry in self.coverage['entries']
            for package in entry.get('firmware_package') or []
        }
        self.assertTrue(required)
        self.assertLessEqual(required, set(self.atoms))
        for package in required:
            self.assertTrue(self.manifest['packages'][package]['covers'],
                            f'{package} does not record which devices it covers')

    def test_regulatory_and_bounded_diagnostics_are_present(self):
        for package in ('wireless-regdb', 'pciutils', 'usbutils', 'iw',
                        'util-linux-misc', 'mesa-utils'):
            self.assertIn(package, self.atoms)
        # rfkill and glxinfo have no standalone package in v3.23.
        self.assertIn('/usr/sbin/rfkill',
                      self.manifest['packages']['util-linux-misc']['verified_paths'])
        self.assertIn('/usr/bin/glxinfo',
                      self.manifest['packages']['mesa-utils']['verified_paths'])

    def test_no_proprietary_or_out_of_tree_drivers_are_selected(self):
        for atom in self.atoms:
            self.assertFalse(atom.startswith('nvidia'), f'{atom} looks like a vendor driver')
            self.assertNotIn('-dkms', atom)
            self.assertNotIn('-src', atom)
        # The NVIDIA entry is redistributable firmware for the in-tree driver.
        self.assertIn('nouveau', self.manifest['packages']['linux-firmware-nvidia']['rationale'])

    def test_manifest_makes_no_hardware_support_claim(self):
        statuses = {entry['status'] for entry in self.coverage['entries']}
        self.assertEqual(statuses, {'untested'})
        joined = ' '.join(self.manifest['notes']).lower()
        self.assertIn('not a hardware support claim', joined)


class BuildWiringTests(unittest.TestCase):
    """world.hardware must be exported, aggregated, staged and recorded."""

    def test_mkimage_exports_and_records_the_hardware_world(self):
        text = MKIMAGE.read_text(encoding='utf-8')
        self.assertIn('export AIOS_WORLD_HARDWARE="$ROOT_DIR/apks/world.hardware"', text)
        self.assertIn('--world "hardware=$AIOS_WORLD_HARDWARE"', text)
        self.assertIn('--world "vm=$AIOS_WORLD_VM"', text)
        self.assertIn('--hardware-package-manifest', text)
        # world.vm stays exported so one ISO still boots in QEMU.
        self.assertIn('export AIOS_WORLD_VM="$ROOT_DIR/apks/world.vm"', text)

    def test_profile_and_overlay_consume_the_hardware_world(self):
        profile = PROFILE.read_text(encoding='utf-8')
        overlay = GENAPKOVL.read_text(encoding='utf-8')
        self.assertIn('AIOS_WORLD_HARDWARE', profile)
        self.assertIn('AIOS_WORLD_HARDWARE', overlay)
        # The apkovl cache hash must cover the file, or a package change could
        # reuse a stale ISO.
        section = profile.split('aios_apkovl_section()')[1].split('build_section')[0]
        self.assertIn('"$AIOS_WORLD_HARDWARE"', section)
        self.assertIn('"$AIOS_WORLD_VM"', section)

    def test_iso_verification_gates_the_offline_hardware_closure(self):
        verify = (ROOT / 'scripts' / 'verify-iso.sh').read_text(encoding='utf-8')
        self.assertIn('package_worlds', verify)
        self.assertIn('hardware packages missing from the embedded closure', verify)
        self.assertIn('hardware_packages', verify)

    def test_iso_verification_runs_the_full_hardware_validation(self):
        verify = (ROOT / 'scripts' / 'verify-iso.sh').read_text(encoding='utf-8')
        # The release gate extracts the evidence and runs the real inspector.
        for fragment in ('mktemp -d', 'trap cleanup EXIT', 'xorriso -osirrox on',
                         'unsquashfs', 'scripts/inspect-image.py', '--validate-hardware',
                         '--modloop-root', '--coverage-manifest',
                         '--hardware-package-manifest', '--hardware-world'):
            self.assertIn(fragment, verify)
        # ...and the checksum/boot metadata checks it already did are still there.
        for fragment in ('sha256sum', '-report_el_torito', '-isohybrid-gpt-basdat'):
            self.assertIn(fragment, verify)

    def test_boot_verification_tooling_is_installed_by_the_workflow(self):
        workflow = (ROOT / '.github' / 'workflows' / 'build-iso.yml').read_text(encoding='utf-8')
        install = next(line for line in workflow.splitlines() if 'apt-get install' in line)
        for tool in ('xorriso', 'squashfs-tools'):
            self.assertIn(tool, install)


class VerifyIsoHardwareGateTests(unittest.TestCase):
    """The verify-iso.sh closure gate, exercised as the script runs it."""

    SNIPPET_START = "python3 - \"$iso.build-manifest.json\" \"$iso.packages.txt\" <<'PY'\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        text = (ROOT / 'scripts' / 'verify-iso.sh').read_text(encoding='utf-8')
        body = text.split(self.SNIPPET_START)[1].split('\nPY\n')[0]
        self.script = self.base / 'gate.py'
        self.script.write_text(body, encoding='utf-8')

    def run_gate(self, worlds, listing, hardware_packages=True, records=None):
        manifest = self.base / 'manifest.json'
        payload = {'package_worlds': {'worlds': worlds}}
        if hardware_packages:
            hardware = next(
                (world for world in worlds if world.get('role') == 'hardware'), {})
            if records is None:
                records = {
                    name: {'license': 'custom', 'repository': 'main'}
                    for name in hardware.get('packages', [])
                }
            payload['hardware_packages'] = {'status': 'recorded', 'packages': records}
        manifest.write_text(json.dumps(payload), encoding='utf-8')
        packages = self.base / 'packages.txt'
        packages.write_text(''.join(f'{name}\n' for name in listing), encoding='utf-8')
        return subprocess.run(
            [sys.executable, str(self.script), str(manifest), str(packages)],
            capture_output=True, text=True, check=False)

    def recorded_worlds(self, hardware=('linux-firmware-intel', 'wireless-regdb')):
        return [
            {'role': 'hardware', 'status': 'recorded', 'packages': list(hardware)},
            {'role': 'vm', 'status': 'recorded', 'packages': ['qemu-guest-agent']},
        ]

    def test_passes_when_every_hardware_package_ships(self):
        result = self.run_gate(
            self.recorded_worlds(),
            ['linux-firmware-intel-20251125-r1.apk', 'wireless-regdb-2025.07.10-r0.apk',
             'busybox-1.37.0-r0.apk'])
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_fails_when_a_hardware_package_is_absent_from_the_closure(self):
        result = self.run_gate(
            self.recorded_worlds(), ['linux-firmware-intel-20251125-r1.apk'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('wireless-regdb', result.stderr)

    def test_fails_when_a_world_was_not_recorded(self):
        result = self.run_gate(
            [{'role': 'hardware', 'status': 'recorded', 'packages': []}], [])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('vm', result.stderr)

    def test_fails_without_license_provenance(self):
        result = self.run_gate(
            self.recorded_worlds(('linux-firmware-intel',)),
            ['linux-firmware-intel-20251125-r1.apk'], hardware_packages=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('license', result.stderr)

    def test_fails_when_a_selected_package_has_no_license_or_repository(self):
        for field in ('license', 'repository'):
            with self.subTest(field=field):
                record = {'license': 'custom', 'repository': 'main'}
                record[field] = ''
                result = self.run_gate(
                    self.recorded_worlds(('linux-firmware-intel',)),
                    ['linux-firmware-intel-20251125-r1.apk'],
                    records={'linux-firmware-intel': record})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(field, result.stderr)
                self.assertIn('linux-firmware-intel', result.stderr)

    def test_fails_when_a_selected_package_has_no_record_at_all(self):
        result = self.run_gate(
            self.recorded_worlds(('linux-firmware-intel',)),
            ['linux-firmware-intel-20251125-r1.apk'], records={})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('linux-firmware-intel', result.stderr)


@unittest.skipUnless(os.name == 'posix', 'verify-iso.sh needs a POSIX shell')
class VerifyIsoHardwareValidationTests(unittest.TestCase):
    """verify-iso.sh must really run inspect-image.py --validate-hardware.

    The ISO tooling is stubbed (no image is built or booted here); what is
    tested is that the release gate extracts the evidence, unpacks the modloop,
    passes the repository manifests to the inspector and propagates its exit
    code, cleaning up deterministically either way.
    """

    VERIFY = ROOT / 'scripts' / 'verify-iso.sh'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.out = self.base / 'out'
        self.out.mkdir()
        self.iso = self.out / 'aios-20260101-x86_64.iso'
        self.iso.write_bytes(b'fake-iso')
        digest = hashlib.sha256(self.iso.read_bytes()).hexdigest()
        (self.out / 'SHA256SUMS').write_text(f'{digest}  {self.iso.name}\n', encoding='utf-8')
        (self.out / 'build-inputs.env').write_text('ALPINE_BRANCH=v3.23\n', encoding='utf-8')
        self.packages_txt = Path(f'{self.iso}.packages.txt')
        self.packages_txt.write_text(
            'linux-firmware-intel-20251125-r1.apk\n', encoding='utf-8')
        Path(f'{self.iso}.build-manifest.json').write_text(
            json.dumps({'package_worlds': {'worlds': []}}), encoding='utf-8')

        self.stubs = self.base / 'stubs'
        self.stubs.mkdir()
        self.write_stub('xorriso', """
case "$*" in
  *-report_el_torito*)
    echo "-b '/boot/syslinux/isolinux.bin'"
    echo "-e '/boot/grub/efi.img'"
    echo "-isohybrid-mbr /usr/share/syslinux/isohdpfx.bin"
    echo "-isohybrid-gpt-basdat"
    ;;
  *-find*) echo "'/aios.apkovl.tar.gz'" ;;
  *-osirrox*)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "-extract" ]; then
        case "$2" in
          /boot) mkdir -p "$3"; : > "$3/modloop-lts"; : > "$3/initramfs-lts" ;;
          /apks) mkdir -p "$3" ;;
          *) mkdir -p "$(dirname "$3")"; : > "$3" ;;
        esac
        shift 3
      else
        shift
      fi
    done
    ;;
esac
exit 0
""")
        self.write_stub('unsquashfs', """
dest=.
while [ "$#" -gt 0 ]; do
  [ "$1" = "-d" ] && dest=$2
  shift
done
mkdir -p "$dest"
exit 0
""")
        self.write_stub('python3', """
case "$*" in
  *inspect-image.py*)
    printf '%s\\n' "$*" > "$STUB_DIR/inspect-command"
    exit "${INSPECT_EXIT:-0}"
    ;;
esac
cat > /dev/null
exit 0
""")

    def write_stub(self, name, body):
        path = self.stubs / name
        path.write_text('#!/bin/sh\n' + body.lstrip(), encoding='utf-8')
        path.chmod(0o755)

    def run_verify(self, inspect_exit=0):
        env = dict(os.environ,
                   PATH=f'{self.stubs}:{os.environ["PATH"]}',
                   STUB_DIR=str(self.stubs),
                   INSPECT_EXIT=str(inspect_exit))
        return subprocess.run(['sh', str(self.VERIFY), str(self.iso)],
                              env=env, capture_output=True, text=True, check=False)

    def inspect_command(self):
        return (self.stubs / 'inspect-command').read_text(encoding='utf-8')

    def test_the_gate_invokes_the_full_validation_with_the_repository_manifests(self):
        result = self.run_verify()
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        command = self.inspect_command()
        self.assertIn('scripts/inspect-image.py', command)
        self.assertIn('--validate-hardware', command)
        self.assertIn('--modloop-root', command)
        self.assertIn('--apks-dir', command)
        self.assertIn('--apkovl', command)
        self.assertIn('docs/qa/hardware-coverage.json', command)
        self.assertIn('docs/qa/hardware-packages.json', command)
        self.assertIn('distro/alpine/apks/world.hardware', command)
        self.assertIn(f'{self.iso}.build-manifest.json', command)
        self.assertIn('PASS', result.stdout)

    def test_a_failed_check_fails_the_gate_with_the_validator_exit_code(self):
        result = self.run_verify(inspect_exit=3)
        self.assertEqual(result.returncode, 3)
        self.assertIn('Hardware bundle validation failed', result.stderr)

    def test_missing_evidence_fails_the_gate_too(self):
        result = self.run_verify(inspect_exit=4)
        self.assertEqual(result.returncode, 4)
        self.assertIn('Hardware bundle validation failed', result.stderr)

    def test_the_extraction_directory_is_removed_whatever_the_outcome(self):
        for code in (0, 3):
            with self.subTest(exit=code):
                self.run_verify(inspect_exit=code)
                work = re.search(r'--modloop-root (\S+)', self.inspect_command()).group(1)
                self.assertFalse(Path(work).exists(), 'temporary extraction was left behind')


@unittest.skipUnless(os.name == 'posix', 'profile and overlay generation require a POSIX shell')
class GeneratedImageInputTests(unittest.TestCase):
    """Run the real profile/overlay scripts and check what they produce."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.stage = self.base / 'stage'
        self.stage.mkdir()
        self.atoms = read_atoms(WORLD_HARDWARE)

    def overlay_world(self, env):
        directory = self.base / 'overlay-run'
        directory.mkdir(exist_ok=True)
        subprocess.run(['sh', str(GENAPKOVL)], cwd=directory, env=env,
                       check=True, capture_output=True)
        with tarfile.open(directory / 'aios.apkovl.tar.gz') as archive:
            return archive.extractfile('./etc/apk/world').read().decode().split()

    def test_profile_packages_include_every_hardware_atom(self):
        packages = profile_value(world_env(self.stage)).split()
        for atom in self.atoms:
            self.assertIn(atom, packages)
        self.assertNotIn('#', ' '.join(packages), 'comments leaked into the package list')
        # Virtual hardware support is not replaced by physical hardware support.
        for atom in read_atoms(APKS / 'world.vm'):
            self.assertIn(atom, packages)

    def test_overlay_world_matches_the_profile_for_both_identity_modes(self):
        for identity in (None, APKS / 'world.identity'):
            with self.subTest(identity=bool(identity)):
                env = world_env(self.stage, identity=identity)
                world = self.overlay_world(env)
                packages = profile_value(env).split()
                for atom in self.atoms:
                    self.assertIn(atom, world, 'live/installed world is missing a hardware package')
                    self.assertIn(atom, packages, 'the ISO closure would not carry it')
                self.assertEqual(world, sorted(world))
                self.assertEqual(len(world), len(set(world)))
                self.assertEqual('bubblewrap' in world, bool(identity))
                for atom in read_atoms(APKS / 'world.vm'):
                    self.assertIn(atom, world)

    def test_changing_the_hardware_world_changes_the_cache_inputs(self):
        modified = self.base / 'world.hardware.modified'
        modified.write_text(
            WORLD_HARDWARE.read_text(encoding='utf-8') + 'linux-firmware-other\n',
            encoding='utf-8')

        def apkovl_hash_input(hardware):
            env = world_env(self.stage, hardware=hardware)
            env['AIOS_OVERLAY_DIR'] = str(self.stage)
            script = (
                'profile_standard() { :; }; '
                'checksum() { cat; }; '
                'build_section() { shift 3; printf "%s" "$1"; }; '
                '. "$1"; profile_aios; apkovl=/dev/null; hostname=aios; '
                'section_apkovl'
            )
            result = subprocess.run(['sh', '-c', script, 'sh', str(PROFILE)],
                                    env=env, check=True, capture_output=True, text=True)
            return result.stdout

        baseline_packages = profile_value(world_env(self.stage)).split()
        changed_packages = profile_value(world_env(self.stage, hardware=modified)).split()
        self.assertNotEqual(baseline_packages, changed_packages)
        self.assertIn('linux-firmware-other', changed_packages)

        baseline_hash = apkovl_hash_input(WORLD_HARDWARE)
        changed_hash = apkovl_hash_input(modified)
        self.assertNotEqual(baseline_hash, changed_hash,
                            'apkovl cache hash ignored a world.hardware change')
        self.assertIn('linux-firmware-intel', baseline_hash)


if __name__ == '__main__':
    unittest.main()
