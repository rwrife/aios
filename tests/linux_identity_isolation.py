"""Real Linux kernel/storage checks, run only by test-identity-linux.sh.

All files are created in this disposable container's /tmp. No host filesystem is
mounted writable, and no pre-existing block device is formatted.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid

from aios.isolation import LinuxIsolation
from aios.journal import Journal
from aios.provisioning import create


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=60)


class KernelIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or os.environ.get('AIOS_DISPOSABLE_TEST_CONTAINER') != '1':
            raise RuntimeError('Use scripts/test-identity-linux.sh in a disposable container')
        # Container cgroup namespace has its own root. Move the test harness to
        # a leaf before enabling controllers (no-internal-process constraint).
        cg = Path('/sys/fs/cgroup')
        (cg / 'harness').mkdir()
        (cg / 'harness' / 'cgroup.procs').write_text(str(os.getpid()))
        (cg / 'cgroup.subtree_control').write_text('+memory +pids')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aios-isolation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o711)
        self.config = {'runtime': str(self.root / 'runtime'), 'anonymous_uid': 21001, 'principals': {}}
        self.adapter = LinuxIsolation(self.config)

    def test_anonymous_files_environment_and_process_scope(self):
        root, uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, root)
        scope = str(uuid.uuid4())
        self.addCleanup(self.adapter.stop, scope)
        secret = self.root / 'host-secret'
        secret.write_text('must not be visible')
        os.environ['AIOS_TEST_SECRET'] = 'must not be inherited'
        self.addCleanup(os.environ.pop, 'AIOS_TEST_SECRET', None)
        # Test-only trusted command: never added to the broker's app allowlist.
        probe = '''
import json, os, pathlib, socket, signal, time
report = {'uid': os.getuid(), 'secret_env': 'AIOS_TEST_SECRET' in os.environ,
          'host_secret': pathlib.Path(%r).exists(), 'home': pathlib.Path('/home').exists(),
          'broker': pathlib.Path('/run/aios-broker').exists(),
          'network_interfaces': sorted(p.name for p in pathlib.Path('/sys/class/net').glob('*'))}
try:
    connection = socket.create_connection(('1.1.1.1', 443), timeout=.1)
    connection.close()
    report['network'] = True
except OSError:
    report['network'] = False
pathlib.Path('/workspace/report.json').write_text(json.dumps(report))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.fork() == 0:
    os.setsid()
while True:
    time.sleep(.1)
''' % str(secret)
        process = self.adapter._spawn(scope, root, uid, ['/usr/bin/python3', '-I', '-c', probe], diagnostics=True)
        report_path = root / 'artifacts' / 'report.json'
        for _ in range(100):
            if report_path.exists():
                break
            if process.poll() is not None:
                self.fail('Sandbox exited before producing the report')
            time.sleep(.02)
        report = json.loads(report_path.read_text())
        self.assertEqual(report['uid'], uid)
        for field in ('secret_env', 'host_secret', 'home', 'broker', 'network'):
            self.assertFalse(report[field], field)
        group = self.adapter.cgroups / scope
        self.assertEqual((group / 'pids.max').read_text().strip(), '128')
        self.assertEqual((group / 'memory.max').read_text().strip(), '1073741824')
        self.assertGreaterEqual(len((group / 'cgroup.procs').read_text().split()), 3)
        self.adapter.stop(scope)
        self.assertIsNotNone(process.poll())
        self.assertFalse(group.exists())

    def test_encrypted_workspace_reopens_durable_journal(self):
        owner = str(uuid.uuid4())
        image = self.root / 'workspace.luks'
        # Only a newly created regular file in this temporary directory is
        # formatted. Never accept a device or image path from command arguments.
        with image.open('xb') as stream:
            stream.truncate(64 * 1024 * 1024)
        self.assertTrue(image.is_file())
        key = self.root / 'volume.key'
        key.write_bytes(os.urandom(32))
        key.chmod(0o600)
        name = 'aios-' + owner
        run('/sbin/cryptsetup', 'luksFormat', '--batch-mode', '--type', 'luks2',
            '--pbkdf', 'pbkdf2', '--pbkdf-force-iterations', '1000', '--key-file', str(key), str(image))
        # Low KDF iterations are exclusively for this random-key disposable test.
        run('/sbin/cryptsetup', 'open', '--key-file', str(key), str(image), name)
        try:
            run('/sbin/mkfs.ext4', '-q', '/dev/mapper/' + name)
        finally:
            run('/sbin/cryptsetup', 'close', name)
        self.config['principals'][owner] = dict(uid=21002, mount=str(self.root / 'alice'),
                                               device=str(image), key_file=str(key))
        root, uid = self.adapter.activate(owner)
        try:
            journal = Journal(root, owner)
            work = journal.create('Resume after reboot')
            journal.message(work, 'user', 'Private draft')
            journal.transition(work, 'suspended')
            journal.close()
            (root / 'artifacts' / 'Resume.txt').write_text('Durable private resume')
        finally:
            self.adapter.release(owner, root)
        self.assertFalse(Path('/dev/mapper', name).exists())
        self.assertFalse((root / 'artifacts').exists())
        self.assertNotIn(b'Durable private resume', image.read_bytes())
        root, uid = self.adapter.activate(owner)
        try:
            journal = Journal(root, owner)
            self.assertEqual(journal.get(work)['status'], 'suspended')
            journal.close()
            self.assertEqual((root / 'artifacts' / 'Resume.txt').read_text(), 'Durable private resume')
            # A fresh broker also locks a volume left open by its predecessor.
            LinuxIsolation(self.config)
            self.assertFalse(Path('/dev/mapper', name).exists())
            self.assertFalse(os.path.ismount(root))
        finally:
            if os.path.ismount(root):
                self.adapter.release(owner, root)

    def test_provisioning_creates_only_new_encrypted_images(self):
        owner = str(uuid.uuid4())
        self.config.update(volume_store=str(self.root / 'volumes'), workspace_size_mib=64)
        config_path = self.root / 'config.json'
        entry = create(self.config, config_path, owner)
        self.assertEqual(json.loads(config_path.read_text())['principals'][owner], entry)
        self.assertEqual(Path(entry['key_file']).stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path('/dev/mapper', 'aios-create-' + owner).exists())
        root, uid = self.adapter.activate(owner)
        try:
            self.assertEqual(uid, entry['uid'])
            (root / 'artifacts' / 'private.txt').write_text('new encrypted workspace')
        finally:
            self.adapter.release(owner, root)
        with self.assertRaises(PermissionError):
            create(self.config, config_path, owner)
        other = str(uuid.uuid4())
        existing = self.root / 'volumes' / other
        existing.mkdir()
        sentinel = existing / 'workspace.luks'
        sentinel.write_text('pre-existing data must never be formatted')
        with self.assertRaises(FileExistsError):
            create(self.config, config_path, other)
        self.assertEqual(sentinel.read_text(), 'pre-existing data must never be formatted')

    def test_alpine_setup_creates_private_state_and_refuses_overwrite(self):
        run('/usr/sbin/adduser', '-D', '-H', '-u', '1000', 'aios')
        package = Path('/usr/local/share/aios')
        package.parent.mkdir(parents=True, exist_ok=True)
        package.symlink_to('/workspace/apps', target_is_directory=True)
        wrapper = '/workspace/distro/alpine/overlay/usr/local/bin/aios-identity-setup'
        run('/bin/sh', wrapper)
        config = json.loads(Path('/etc/aios/sessiond.json').read_text())
        self.assertFalse(config['display_isolation_validated'])
        self.assertEqual(len({config[role] for role in ('shell_uid', 'identity_uid', 'anonymous_uid')}), 3)
        key = Path(config['master_key'])
        original = key.read_bytes()
        self.assertEqual(len(original), 32)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(subprocess.CalledProcessError):
            run('/bin/sh', wrapper)
        self.assertEqual(key.read_bytes(), original)

    def test_distinct_uids_cannot_read_each_others_artifacts(self):
        alice, alice_uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, alice)
        self.config['anonymous_uid'] = 21002
        bob, bob_uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, bob)
        secret = bob / 'artifacts' / 'private.txt'
        secret.write_text('Bob private artifact')
        result = subprocess.run(['/usr/bin/python3', '-I', '-c',
            'import pathlib,sys; pathlib.Path(sys.argv[1]).read_text()', str(secret)],
            user=alice_uid, group=alice_uid, extra_groups=[], capture_output=True, text=True,
            timeout=5, cwd='/')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('PermissionError', result.stderr)
        self.assertNotIn('Bob private artifact', result.stdout)

    def test_restart_reaps_scope_and_discards_anonymous_tmpfs(self):
        root, uid = self.adapter.anonymous()
        scope = str(uuid.uuid4())
        try:
            process = self.adapter._spawn(scope, root, uid, ['/bin/sleep', '60'], diagnostics=True)
            (root / 'artifacts' / 'temporary.txt').write_text('discard on restart')
            recovered = LinuxIsolation(self.config)
            self.assertFalse(root.exists())
            self.assertFalse((recovered.cgroups / scope).exists())
            self.assertIsNotNone(process.wait(timeout=2))
        finally:
            self.adapter.stop(scope)
            if os.path.ismount(root):
                self.adapter.release(None, root)


if __name__ == '__main__':
    unittest.main(verbosity=2)
