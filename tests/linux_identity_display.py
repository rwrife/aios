"""Real broker FD transfer and Wayland client checks in a disposable container.

Offscreen rendering is a protocol/UID check, not physical input/display assurance.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SHELL = sys.argv.pop(1)


class DisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or os.environ.get('AIOS_DISPOSABLE_TEST_CONTAINER') != '1':
            raise RuntimeError('Use scripts/test-identity-display.sh')
        cg = Path('/sys/fs/cgroup')
        (cg / 'harness').mkdir()
        (cg / 'harness/cgroup.procs').write_text(str(os.getpid()))
        (cg / 'cgroup.subtree_control').write_text('+memory +pids')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aios-display-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o711)
        self.shell_uid, self.anon_uid, self.other_uid = 22000, 22001, 22003
        self.socket = self.root / 'broker.sock'
        self.runtime = self.root / 'workspaces'
        key = self.root / 'master.key'
        key.write_bytes(os.urandom(32)); key.chmod(0o600)
        config = {'master_key': str(key), 'state': str(self.root / 'records'),
                  'runtime': str(self.runtime), 'socket': str(self.socket), 'socket_gid': self.shell_uid,
                  'shell_uid': self.shell_uid, 'identity_uid': 22002, 'anonymous_uid': self.anon_uid,
                  'principals': {}, 'embedded_display': True, 'display_isolation_validated': False}
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
        self.env = {**os.environ, 'PYTHONPATH': '/workspace/apps'}
        self.logs = []
        self.broker = self.spawn('broker', [sys.executable, '-m', 'aios.sessiond', '--config', str(config_path)])
        self.wait(lambda: self.socket.exists())
        self.shell = self.start_shell()

    def runtime_for(self, uid):
        runtime = self.root / ('user-' + str(uid))
        runtime.mkdir(mode=0o700, exist_ok=True)
        os.chown(runtime, uid, uid)
        return runtime

    def spawn(self, name, arguments, **options):
        log = self.root / (name + '.log')
        self.logs.append(log)
        stream = log.open('wb')
        self.addCleanup(stream.close)
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                   env=options.pop('env', self.env), **options)
        self.addCleanup(self.stop, process)
        return process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.05)
        self.fail('Display condition timed out')

    def start_shell(self, capture=False):
        runtime = self.runtime_for(self.shell_uid)
        env = {**self.env, 'AIOS_SESSION_SOCKET': str(self.socket), 'XDG_RUNTIME_DIR': str(runtime),
               'HOME': str(runtime), 'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software'}
        arguments = [SHELL, '--prepare-display']
        if capture:
            arguments += ['--capture', str(runtime / 'surface.png')]
        return self.spawn('shell-' + str(len(self.logs)), arguments, env=env,
                          user=self.shell_uid, group=self.shell_uid, extra_groups=[])

    def request(self, payload):
        code = 'import json,sys; from aios.session_client import request; print(json.dumps(request(sys.argv[1],json.load(sys.stdin))))'
        result = subprocess.run([sys.executable, '-c', code, str(self.socket)], input=json.dumps(payload),
                                env=self.env, user=self.shell_uid, group=self.shell_uid, extra_groups=[],
                                capture_output=True, text=True, timeout=5, check=True)
        response = json.loads(result.stdout)
        self.assertTrue(response['ok'], response)
        return response['result']

    def inspect_display(self, path, uid):
        env = {**self.env, 'XDG_RUNTIME_DIR': str(self.runtime_for(uid)), 'WAYLAND_DISPLAY': str(path)}
        return subprocess.run(['wayland-info'], env=env, user=uid, group=uid, extra_groups=[],
                              capture_output=True, text=True, timeout=5)

    def test_private_listener_protocol_and_lease_teardown(self):
        path = self.wait(lambda: next(self.runtime.glob('.displays/*/wayland-0'), None))
        self.assertEqual(path.stat().st_uid, self.anon_uid)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        info = self.inspect_display(path, self.anon_uid)
        self.assertEqual(info.returncode, 0, info.stderr)
        self.assertIn('xdg_wm_base', info.stdout)
        self.assertIn('wl_compositor', info.stdout)
        for extension in ('screencopy', 'virtual_keyboard', 'virtual_pointer', 'screencast'):
            self.assertNotIn(extension, info.stdout)
        denied = self.inspect_display(path, self.other_uid)
        self.assertNotEqual(denied.returncode, 0)
        self.request({'action': 'suspend'})
        self.wait(lambda: not path.exists())
        self.stop(self.shell)
        self.shell = self.start_shell()
        replacement = self.wait(lambda: next(self.runtime.glob('.displays/*/wayland-0'), None))
        self.assertNotEqual(path, replacement)
        self.assertEqual(self.inspect_display(replacement, self.anon_uid).returncode, 0)

    def test_allowlisted_app_surface_is_rendered(self):
        from PIL import Image
        self.stop(self.shell)
        self.request({'action': 'suspend'})
        self.shell = self.start_shell(capture=True)
        self.wait(lambda: next(self.runtime.glob('.displays/*/wayland-0'), None))
        # Descriptor acceptance precedes launch; readiness is acknowledged by
        # the same shell UID that owns the compositor.
        self.wait(lambda: self.request({'action': 'status'})['display_ready'])
        self.request({'action': 'launch', 'app': 'calculator', 'arguments': []})
        report_path = self.wait(lambda: next(self.runtime.glob('*/artifacts/display-test.json'), None), timeout=4)
        self.assertEqual(json.loads(report_path.read_text()), {'painted': True, 'uid': self.anon_uid})
        screenshot = self.runtime_for(self.shell_uid) / 'surface.png'
        self.wait(lambda: screenshot.exists())
        with Image.open(screenshot) as image:
            pixels = image.convert('RGB')
            area = pixels.crop((410, 84, pixels.width, pixels.height))
            red = sum(r > 200 and g < 70 and b < 70 for r, g, b in area.getdata())
        self.assertGreater(red, 1000, 'Client painted but its pixels were not composited')

    def test_pin_overlay_receives_keys_and_application_does_not(self):
        self.stop(self.shell)
        self.request({'action': 'suspend'})
        runtime = self.runtime_for(self.shell_uid)
        report = runtime / 'input.json'
        env = {**self.env, 'AIOS_SESSION_SOCKET': str(self.socket), 'XDG_RUNTIME_DIR': str(runtime),
               'HOME': str(runtime), 'QT_QPA_PLATFORM': 'offscreen', 'QT_QUICK_BACKEND': 'software',
               'AIOS_TEST_REPORT': str(report)}
        self.shell = self.spawn('input-shell', [str(Path(SHELL).with_name('aios-display-input'))], env=env,
                                user=self.shell_uid, group=self.shell_uid, extra_groups=[])
        self.wait(lambda: self.request({'action': 'status'})['display_ready'])
        self.request({'action': 'launch', 'app': 'calculator', 'arguments': []})
        self.wait(lambda: report.exists())
        self.assertTrue(json.loads(report.read_text())['pin_received'])
        keys = self.wait(lambda: next(self.runtime.glob('*/artifacts/display-keys.txt'), None))
        self.assertEqual(len(keys.read_text().splitlines()), 1, 'Application received input intended for PIN overlay')
        self.assertEqual(keys.with_name('display-clipboard.txt').read_text(), 'isolated')

    def tearDown(self):
        result = self._outcome.result
        if result.failures or result.errors:
            for log in self.logs:
                print(log.name + ':\n' + log.read_text(errors='replace')[-12000:])
            for log in self.runtime.glob('*/artifacts/display-client.log'):
                print('client:\n' + log.read_text(errors='replace')[-12000:])


if __name__ == '__main__':
    unittest.main(verbosity=2)
