"""Stage-3 predictable startup: service ownership, boot entries, recovery session.

These tests read the real overlay, apkovl generator and image profile. They are
display-independent: the session test replaces every external program with a
stub, so no X server, GPU or compositor is involved.
"""
import functools
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ALPINE = ROOT / 'distro' / 'alpine'
APKS = ALPINE / 'apks'
GENAPKOVL = ALPINE / 'apkovl' / 'genapkovl-aios.sh'
PROFILE = ALPINE / 'profiles' / 'mkimg.aios.sh'
OVERLAY = ALPINE / 'overlay'
SESSION = OVERLAY / 'usr' / 'local' / 'bin' / 'aios-session'
PROFILE_D = OVERLAY / 'etc' / 'profile.d' / 'aios.sh'

STUBS = ('xsetroot', 'openbox', 'picom', 'pulseaudio', 'aios-terminal')


def overlay_env(stage):
    env = dict(
        os.environ,
        AIOS_STAGE_DIR=str(stage),
        AIOS_OVERLAY_DIR=str(OVERLAY),
        AIOS_REPOSITORIES='https://example.invalid/main',
        AIOS_WORLD_HARDWARE=str(APKS / 'world.hardware'),
        AIOS_WORLD_IDENTITY='',
        AIOS_APKOVL_SCRIPT='genapkovl-aios.sh',
    )
    for name in ('BASE', 'X11', 'VM', 'DEVEL', 'AI'):
        env['AIOS_WORLD_' + name] = str(APKS / ('world.' + name.lower()))
    return env


@functools.lru_cache(maxsize=1)
def generated_apkovl():
    """Run the real apkovl generator once and read the archive back.

    Returns the runlevel symlink map, the generated world, and the actual
    archive members for files the image contract depends on.
    """
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / 'stage').mkdir()
        subprocess.run(['sh', str(GENAPKOVL), 'aios'], cwd=work, check=True,
                       env=overlay_env(work / 'stage'), capture_output=True)
        runlevels, world, files = {}, [], {}
        with tarfile.open(work / 'aios.apkovl.tar.gz') as archive:
            for member in archive.getmembers():
                clean = member.name.lstrip('./')
                parts = clean.split('/')
                if parts[:2] == ['etc', 'runlevels'] and len(parts) == 4:
                    runlevels.setdefault(parts[2], []).append(parts[3])
                if clean == 'etc/apk/world':
                    world = archive.extractfile(member).read().decode().split()
                if clean in ('etc/aios-mode', 'etc/hostname'):
                    files[clean] = {
                        'text': archive.extractfile(member).read().decode(),
                        'mode': member.mode,
                        'regular': member.isfile(),
                    }
        return {level: sorted(names) for level, names in runlevels.items()}, world, files


def generated_runlevels():
    runlevels, world, _ = generated_apkovl()
    return runlevels, world


def profile_output(function):
    """Call one generator function from the real image profile."""
    result = subprocess.run(
        ['sh', '-c', f'profile_standard() {{ :; }}; . "$1"; profile_aios; {function}',
         'sh', str(PROFILE)],
        check=True, capture_output=True, text=True,
        env=dict(os.environ, AIOS_WORLD_BASE=str(APKS / 'world.base'),
                 AIOS_WORLD_X11=str(APKS / 'world.x11'), AIOS_WORLD_VM=str(APKS / 'world.vm'),
                 AIOS_WORLD_DEVEL=str(APKS / 'world.devel'), AIOS_WORLD_AI=str(APKS / 'world.ai'),
                 AIOS_WORLD_HARDWARE=str(APKS / 'world.hardware'), AIOS_WORLD_IDENTITY='',
                 AIOS_APKOVL_SCRIPT='genapkovl-aios.sh'))
    return result.stdout


@unittest.skipUnless(os.name == 'posix', 'the image generators need a POSIX shell')
class NetworkOwnershipTests(unittest.TestCase):
    """NetworkManager is the only service that owns a Wi-Fi interface."""

    @classmethod
    def setUpClass(cls):
        cls.runlevels, cls.world = generated_runlevels()

    def test_network_manager_is_enabled_and_wpa_supplicant_is_not_started_separately(self):
        self.assertIn('networkmanager', self.runlevels['default'])
        for level, services in self.runlevels.items():
            self.assertNotIn('wpa_supplicant', services, f'duplicate Wi-Fi owner in {level}')
            self.assertNotIn('wpa_cli', services, f'duplicate Wi-Fi owner in {level}')
        # Alpine's supplicant init script binds -i<first wireless interface>
        # before NetworkManager starts and fails outright when no radio exists.
        self.assertRegex(GENAPKOVL.read_text(encoding='utf-8'),
                         r'fi\.w1\.wpa_supplicant1\.service')

    def test_supplicant_packages_stay_installed_for_the_network_manager_backend(self):
        for package in ('wpa_supplicant', 'wpa_supplicant-openrc', 'networkmanager',
                        'networkmanager-wifi', 'networkmanager-openrc'):
            self.assertIn(package, self.world)
        configuration = (OVERLAY / 'etc/NetworkManager/NetworkManager.conf').read_text(encoding='utf-8')
        self.assertIn('wifi.backend=wpa_supplicant', configuration)

    def test_no_other_service_claims_the_network(self):
        for level, services in self.runlevels.items():
            for conflicting in ('dhcpcd', 'wicd', 'iwd', 'connman'):
                self.assertNotIn(conflicting, services, f'{conflicting} in {level}')


@unittest.skipUnless(os.name == 'posix', 'the image generators need a POSIX shell')
class BootEntryTests(unittest.TestCase):
    """Normal, install and recovery entries exist on both bootloaders."""

    @classmethod
    def setUpClass(cls):
        cls.syslinux = profile_output('syslinux_gen_config')
        cls.grub = profile_output('grub_gen_config')

    def syslinux_entries(self):
        entries, current = {}, None
        for line in self.syslinux.splitlines():
            stripped = line.strip()
            if stripped.startswith('LABEL '):
                current = stripped.split(None, 1)[1]
            elif current and stripped.startswith('APPEND '):
                entries[current] = stripped.split(None, 1)[1]
        return entries

    def grub_entries(self):
        entries, title = {}, None
        for line in self.grub.splitlines():
            stripped = line.strip()
            if stripped.startswith('menuentry '):
                title = stripped.split('"')[1]
            elif title and stripped.startswith('linux '):
                entries[title] = stripped.split(None, 2)[2]
        return entries

    def test_syslinux_offers_three_entries_and_defaults_to_live(self):
        entries = self.syslinux_entries()
        self.assertEqual(sorted(entries), ['install', 'live', 'recovery'])
        self.assertIn('\nDEFAULT live\n', '\n' + self.syslinux)
        self.assertIn('SERIAL 0 115200', self.syslinux)
        self.assertIn('PROMPT 1', self.syslinux)
        # The recovery entry must be discoverable without editing arguments.
        self.assertRegex(self.syslinux, r'SAY .*recovery')

    def test_grub_offers_three_entries_and_defaults_to_the_first(self):
        entries = self.grub_entries()
        self.assertEqual(len(entries), 3)
        titles = list(entries)
        self.assertEqual(titles[0], 'AIOS live')
        self.assertIn('AIOS install', titles)
        self.assertEqual(titles[2], 'AIOS recovery (safe graphics)')
        self.assertIn('set default=0', self.grub)

    def test_recovery_uses_a_conservative_graphics_path_and_is_never_default(self):
        for entries, recovery in ((self.syslinux_entries(), 'recovery'),
                                  (self.grub_entries(), 'AIOS recovery (safe graphics)')):
            arguments = entries[recovery].split()
            self.assertIn('nomodeset', arguments)
            self.assertIn('aios.recovery', arguments)
            self.assertNotIn('aios.install', arguments)
        self.assertNotIn('nomodeset', self.syslinux_entries()['live'].split())
        self.assertNotIn('aios.recovery', self.syslinux_entries()['live'].split())
        self.assertNotIn('nomodeset', self.grub_entries()['AIOS live'].split())

    def test_every_entry_keeps_serial_and_vga_console_access(self):
        for entries in (self.syslinux_entries(), self.grub_entries()):
            for name, arguments in entries.items():
                self.assertIn('console=ttyS0,115200', arguments, name)
                self.assertIn('console=tty0', arguments, name)

    def test_install_entry_is_unchanged(self):
        self.assertIn('aios.install', self.syslinux_entries()['install'].split())
        self.assertIn('aios.install', self.grub_entries()['AIOS install'].split())


@unittest.skipUnless(os.name == 'posix', 'the session script needs a POSIX shell')
class RecoverySessionTests(unittest.TestCase):
    """aios-session renderer selection and readiness, recorded for diagnostics.

    Every external program is a stub, so no X server, GPU or compositor is
    involved. The startup checks are bounded by AIOS_STARTUP_CHECK_DELAY, which
    these tests shorten; they assert recorded phases, never wall-clock timing.
    """

    ACCELERATED = ('name of display: :0\nOpenGL vendor string: Intel\n'
                   'OpenGL renderer string: Mesa Intel(R) UHD Graphics\n')
    SOFTWARE = ('name of display: :0\n'
                'OpenGL renderer string: llvmpipe (LLVM 17.0.6, 256 bits)\n')
    BOOT_ID = '2c9f3f1a-6a1d-4de1-9b4a-7f0c5a1e2b33'
    PREVIOUS_BOOT_ID = 'ffffffff-0000-4000-8000-0123456789ab'

    def run_session(self, glxinfo_output='', glxinfo_status=0,
                    cmdline='BOOT_IMAGE=/boot/vmlinuz-lts quiet', compositor_survives=True,
                    shell_status=0, shell_lifetime='0.6', prior_state=None):
        workspace = tempfile.TemporaryDirectory(prefix='aios-session-')
        self.addCleanup(workspace.cleanup)
        directory = workspace.name
        home = Path(directory) / 'home'
        home.mkdir()
        state = home / '.local/state/aios'
        if prior_state is not None:
            state.mkdir(parents=True)
            (state / 'renderer.json').write_text(prior_state)
        binaries = Path(directory) / 'bin'
        binaries.mkdir()
        for name in STUBS:
            script = binaries / name
            script.write_text('#!/bin/sh\nexit 0\n')
            script.chmod(0o755)
        picom = binaries / 'picom'
        picom.write_text('#!/bin/sh\nexec sleep 5\n' if compositor_survives
                         else '#!/bin/sh\nexit 1\n')
        picom.chmod(0o755)
        glxinfo = binaries / 'glxinfo'
        # Records whether the previous renderer record still existed when the
        # renderer probe ran, i.e. whether the session invalidated it first.
        glxinfo.write_text(
            '#!/bin/sh\n'
            'if [ -e "$HOME/.local/state/aios/renderer.json" ]; then\n'
            '  printf stale > "$HOME/state-at-probe"\nelse\n'
            '  printf absent > "$HOME/state-at-probe"\nfi\n'
            'printf %s "$AIOS_TEST_GLXINFO"\nexit ' + str(glxinfo_status) + '\n')
        glxinfo.chmod(0o755)
        shell = binaries / 'aios-shell'
        shell.write_text('#!/bin/sh\nprintf %s "${LIBGL_ALWAYS_SOFTWARE:-unset}" > "$HOME/libgl"\n'
                         f'sleep {shell_lifetime}\nexit {shell_status}\n')
        shell.chmod(0o755)
        terminal = binaries / 'aios-terminal'
        terminal.write_text('#!/bin/sh\nprintf started > "$HOME/terminal"\nexit 0\n')
        terminal.chmod(0o755)
        cmdline_file = Path(directory) / 'cmdline'
        cmdline_file.write_text(cmdline + '\n')
        boot_id_file = Path(directory) / 'boot_id'
        boot_id_file.write_text(self.BOOT_ID + '\n')
        environment = {
            'HOME': str(home), 'PATH': f'{binaries}:/usr/bin:/bin:/usr/sbin:/sbin',
            'AIOS_CMDLINE_PATH': str(cmdline_file), 'AIOS_TEST_GLXINFO': glxinfo_output,
            'AIOS_BOOT_ID_PATH': str(boot_id_file), 'AIOS_STARTUP_CHECK_DELAY': '0.2',
        }
        subprocess.run(['sh', str(SESSION)], env=environment, check=True,
                       capture_output=True, timeout=60)
        probe_marker = home / 'state-at-probe'
        return {
            'renderer': json.loads((state / 'renderer.json').read_text()),
            'compositor': (state / 'compositor.log').read_text(),
            'libgl': (home / 'libgl').read_text(),
            'graphics': (state / 'graphics.log').read_text(),
            'state_at_probe': probe_marker.read_text() if probe_marker.exists() else None,
            'terminal': (home / 'terminal').exists(),
        }

    def test_accelerated_probe_selects_glx_and_reports_a_ready_session(self):
        result = self.run_session(self.ACCELERATED)
        self.assertEqual(result['renderer'],
                         {'version': 2, 'boot_id': self.BOOT_ID, 'status': 'ready',
                          'phase': 'shell_ready', 'backend': 'glx', 'renderer': 'accelerated',
                          'accelerated': True, 'recovery': False})
        self.assertIn('AIOS compositor: glx', result['compositor'])
        self.assertEqual(result['libgl'], 'unset')
        self.assertFalse(result['terminal'])

    def test_software_renderer_keeps_the_xrender_fallback(self):
        result = self.run_session(self.SOFTWARE)
        self.assertEqual(result['renderer']['backend'], 'xrender')
        self.assertEqual(result['renderer']['renderer'], 'software')
        self.assertFalse(result['renderer']['accelerated'])
        self.assertEqual(result['renderer']['phase'], 'shell_ready')

    def test_failed_probe_is_reported_as_unavailable_not_accelerated(self):
        result = self.run_session('', glxinfo_status=1)
        self.assertEqual(result['renderer']['backend'], 'xrender')
        self.assertEqual(result['renderer']['renderer'], 'unavailable')
        self.assertIs(result['renderer']['accelerated'], False)

    def test_recovery_boot_forces_software_rendering_without_probing(self):
        result = self.run_session(self.ACCELERATED,
                                  cmdline='BOOT_IMAGE=/boot/vmlinuz-lts aios.recovery nomodeset')
        self.assertEqual(result['renderer'],
                         {'version': 2, 'boot_id': self.BOOT_ID, 'status': 'ready',
                          'phase': 'shell_ready', 'backend': 'xrender', 'renderer': 'software',
                          'accelerated': False, 'recovery': True})
        self.assertEqual(result['libgl'], '1')
        self.assertIn('recovery mode', result['graphics'])
        self.assertNotIn('OpenGL renderer string', result['graphics'])

    def test_a_previous_records_state_is_invalidated_before_the_probe(self):
        stale = json.dumps({'version': 2, 'boot_id': self.PREVIOUS_BOOT_ID, 'status': 'ready',
                            'phase': 'shell_ready', 'backend': 'glx', 'renderer': 'accelerated',
                            'accelerated': True, 'recovery': False})
        result = self.run_session(self.SOFTWARE, prior_state=stale)
        self.assertEqual(result['state_at_probe'], 'absent')
        self.assertEqual(result['renderer']['boot_id'], self.BOOT_ID)
        self.assertEqual(result['renderer']['renderer'], 'software')

    def test_a_compositor_that_does_not_stay_running_is_recorded_as_a_failure(self):
        result = self.run_session(self.ACCELERATED, compositor_survives=False)
        self.assertEqual(result['renderer']['status'], 'failed')
        self.assertEqual(result['renderer']['phase'], 'compositor_failed')
        self.assertIn('did not stay running', result['compositor'])

    def test_a_shell_that_fails_is_recorded_and_leaves_a_recovery_terminal(self):
        for lifetime in ('0.6', '0'):
            with self.subTest(lifetime=lifetime):
                result = self.run_session(self.ACCELERATED, shell_status=1,
                                          shell_lifetime=lifetime)
                self.assertEqual(result['renderer']['status'], 'failed')
                self.assertEqual(result['renderer']['phase'], 'shell_failed')
                self.assertTrue(result['terminal'])

    def test_the_record_carries_this_boot_and_only_fixed_enums(self):
        recorded = self.run_session(self.ACCELERATED)['renderer']
        self.assertEqual(set(recorded), {'version', 'boot_id', 'status', 'phase', 'backend',
                                         'renderer', 'accelerated', 'recovery'})
        self.assertEqual(recorded['boot_id'], self.BOOT_ID)
        self.assertIn(recorded['status'], ('starting', 'ready', 'failed'))
        self.assertIn(recorded['phase'], ('selected', 'compositor_ready', 'shell_ready',
                                          'compositor_failed', 'shell_failed'))


class ConsoleRecoveryPathTests(unittest.TestCase):
    """A failed desktop must leave written instructions, not a black screen."""

    def test_login_profile_explains_recovery_and_diagnostics(self):
        text = PROFILE_D.read_text(encoding='utf-8')
        self.assertIn('aios.recovery', text)
        self.assertIn('aios-hardware-report --human', text)
        self.assertIn('recovery boot entry', text)

    def test_session_failure_points_at_the_report_and_logs(self):
        text = SESSION.read_text(encoding='utf-8')
        self.assertIn('aios-hardware-report --human', text)
        self.assertIn('renderer.json', text)

    def test_no_vendor_xorg_configuration_is_shipped(self):
        self.assertFalse(list(OVERLAY.glob('etc/X11/xorg.conf*')))
        self.assertFalse(list(OVERLAY.glob('etc/X11/xorg.conf.d/*')))


if __name__ == '__main__':
    unittest.main()
