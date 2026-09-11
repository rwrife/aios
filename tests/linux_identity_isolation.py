"""Real Linux kernel/storage checks, run only by test-identity-linux.sh.

All files are created in this disposable container's /tmp. No host filesystem is
mounted writable, and no pre-existing block device is formatted.
"""
import json
import http.server
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
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
report['fds'] = []
for descriptor in pathlib.Path('/proc/self/fd').iterdir():
    try:
        descriptor.readlink()
        report['fds'].append(int(descriptor.name))
    except FileNotFoundError:
        pass
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
        self.assertTrue(set(report['fds']) <= {0, 1, 2}, report['fds'])
        for field in ('secret_env', 'host_secret', 'home', 'broker', 'network'):
            self.assertFalse(report[field], field)
        group = self.adapter.cgroups / scope
        self.assertEqual((group / 'pids.max').read_text().strip(), '128')
        self.assertEqual((group / 'memory.max').read_text().strip(), '1073741824')
        self.assertGreaterEqual(len((group / 'cgroup.procs').read_text().split()), 3)
        self.adapter.stop(scope)
        self.assertIsNotNone(process.poll())
        self.assertFalse(group.exists())

    def test_failed_sandbox_start_closes_control_descriptors_and_scope_alias(self):
        root, uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, root)
        before = set(Path('/proc/self/fd').iterdir())
        for _ in range(3):
            scope = str(uuid.uuid4())
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                self.adapter._spawn(scope, root, uid, ['/bin/false'], scheduled=True)
            self.assertFalse((self.adapter.cgroups / scope).exists())
            self.assertFalse((self.adapter.root / scope).exists())
        scope = str(uuid.uuid4())
        original = Path.write_text
        def fail_limits(path, *args, **kwargs):
            if path == self.adapter.cgroups / scope / 'pids.max':
                raise OSError('cgroup limit fixture failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'write_text', fail_limits):
            with self.assertRaisesRegex(OSError, 'cgroup limit fixture'):
                self.adapter._spawn(scope, root, uid, ['/bin/true'], scheduled=True)
        self.assertFalse((self.adapter.cgroups / scope).exists())
        self.assertFalse((self.adapter.root / scope).exists())
        self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)

    def test_artifact_binding_rejects_symlinks_wrong_owner_and_non_directories(self):
        root, uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, root)
        artifacts, saved = root / 'artifacts', root / 'saved-artifacts'
        before = set(Path('/proc/self/fd').iterdir())
        for kind in ('symlink', 'wrong-owner', 'file', 'public'):
            with self.subTest(kind=kind):
                artifacts.rename(saved)
                try:
                    if kind == 'symlink':
                        artifacts.symlink_to(saved, target_is_directory=True)
                    elif kind == 'file':
                        artifacts.write_text('not a directory')
                    else:
                        artifacts.mkdir(mode=0o700 if kind == 'wrong-owner' else 0o755)
                        os.chown(artifacts, uid + 1 if kind == 'wrong-owner' else uid, uid)
                    scope = str(uuid.uuid4())
                    with self.assertRaises(OSError):
                        self.adapter._spawn(scope, root, uid, ['/bin/true'])
                    self.assertFalse((self.adapter.root / scope).exists())
                    self.assertFalse((self.adapter.cgroups / scope).exists())
                    self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)
                finally:
                    if artifacts.is_symlink() or artifacts.is_file():
                        artifacts.unlink()
                    else:
                        artifacts.rmdir()
                    saved.rename(artifacts)
        link = self.root / 'workspace-link'
        link.symlink_to(root, target_is_directory=True)
        try:
            with self.assertRaises(OSError):
                self.adapter._spawn(str(uuid.uuid4()), link, uid, ['/bin/true'])
        finally:
            link.unlink()
        self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)

    def test_artifact_path_swap_is_pinned_then_rejected_without_launch_or_fd_leak(self):
        root, uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, root)
        artifacts, saved = root / 'artifacts', root / 'saved-artifacts'
        (artifacts / 'marker').write_text('original owner directory')
        before = set(Path('/proc/self/fd').iterdir())
        scope = str(uuid.uuid4())
        bind = self.adapter._bind_descriptors
        pinned = []
        def swap(source, target):
            self.assertEqual(os.fstat(source).st_uid, uid)
            self.assertEqual(os.fstat(target).st_uid, 0)
            artifacts.rename(saved)
            artifacts.mkdir(mode=0o700)
            os.chown(artifacts, uid, uid)
            (artifacts / 'marker').write_text('replacement directory')
            bind(source, target)
            pinned.append((self.adapter.root / scope / 'marker').read_text())
        try:
            with patch.object(self.adapter, '_bind_descriptors', side_effect=swap):
                with self.assertRaisesRegex(PermissionError, 'changed during'):
                    self.adapter._spawn(scope, root, uid,
                                        ['/bin/sh', '-c', 'touch /workspace/launched'])
            self.assertEqual(pinned, ['original owner directory'])
            self.assertFalse((saved / 'launched').exists())
            self.assertFalse((artifacts / 'launched').exists())
            self.assertFalse((self.adapter.root / scope).exists())
            self.assertFalse((self.adapter.cgroups / scope).exists())
            self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)
        finally:
            if saved.exists():
                (artifacts / 'marker').unlink()
                artifacts.rmdir()
                saved.rename(artifacts)

    def test_artifact_binding_rejects_foreign_runtime_entry_and_cleans_mount_failure(self):
        root, uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, root)
        before = set(Path('/proc/self/fd').iterdir())
        for kind in ('directory', 'wrong-owner', 'file', 'symlink'):
            scope = str(uuid.uuid4())
            entry = self.adapter.root / scope
            if kind == 'symlink':
                entry.symlink_to(root / 'artifacts', target_is_directory=True)
            elif kind == 'file':
                entry.write_text('preexisting node')
            else:
                entry.mkdir(mode=0o700)
                if kind == 'wrong-owner':
                    os.chown(entry, uid + 1, uid + 1)
            try:
                with self.assertRaises(OSError):
                    self.adapter._spawn(scope, root, uid, ['/bin/true'])
                self.assertFalse((self.adapter.cgroups / scope).exists())
            finally:
                entry.unlink() if kind in ('symlink', 'file') else entry.rmdir()
        scope = str(uuid.uuid4())
        with patch.object(self.adapter, '_bind_descriptors', side_effect=OSError('mount fixture failure')):
            with self.assertRaises(OSError):
                self.adapter._spawn(scope, root, uid, ['/bin/true'])
        self.assertFalse((self.adapter.root / scope).exists())
        self.assertFalse((self.adapter.cgroups / scope).exists())
        self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)

    def test_scope_traversal_and_replaceable_runtime_ancestors_are_rejected(self):
        root, uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, root)
        before = set(Path('/proc/self/fd').iterdir())
        for scope in ('../escape', '/escape', str(uuid.uuid4()) + '/child',
                      str(uuid.uuid4()).upper(), '.', ''):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.adapter._spawn(scope, root, uid, ['/bin/true'])
        unsafe = self.root / 'replaceable-parent'
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        runtime = unsafe / 'runtime'
        runtime.mkdir(mode=0o711)
        for wrong_owner in (False, True):
            if wrong_owner:
                unsafe.chmod(0o700)
                os.chown(unsafe, uid, uid)
            with patch.object(self.adapter, 'root', runtime):
                with self.assertRaisesRegex(PermissionError, 'ancestors'):
                    self.adapter._spawn(str(uuid.uuid4()), root, uid, ['/bin/true'])
            self.assertEqual(list(runtime.iterdir()), [])
        self.assertEqual(set(Path('/proc/self/fd').iterdir()), before)

    def test_owner_cannot_replace_root_artifacts_or_runtime_alias_namespace(self):
        root, uid = self.adapter.anonymous()
        self.addCleanup(self.adapter.release, None, root)
        scope = str(uuid.uuid4())
        process = self.adapter._spawn(scope, root, uid, ['/bin/sleep', '60'])
        self.addCleanup(self.adapter.stop, scope)
        self.assertEqual(root.stat().st_uid, 0)
        self.assertEqual(self.adapter.root.stat().st_uid, 0)
        self.assertEqual(root.stat().st_mode & 0o022, 0)
        self.assertEqual(self.adapter.root.stat().st_mode & 0o022, 0)
        probe = '''
import errno, json, os, sys
root, runtime, scope = sys.argv[1:]
results = []
for source, destination in (
    (root + '/artifacts', root + '/moved-artifacts'),
    (runtime + '/' + scope, runtime + '/moved-alias')):
    try:
        os.rename(source, destination)
        results.append(False)
    except OSError as error:
        results.append(error.errno in (errno.EACCES, errno.EPERM, errno.EBUSY))
try:
    os.mkdir(runtime + '/attacker-node')
    results.append(False)
except OSError as error:
    results.append(error.errno in (errno.EACCES, errno.EPERM))
print(json.dumps(results))
'''
        result = subprocess.run(['/usr/bin/python3', '-I', '-c', probe,
                                 str(root), str(self.adapter.root), scope],
                                user=uid, group=uid, extra_groups=[], cwd='/',
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(json.loads(result.stdout), [True, True, True])
        self.assertIsNone(process.poll())
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
            scope = str(uuid.uuid4())
            process = self.adapter._spawn(scope, root, uid, ['/bin/sleep', '60'], diagnostics=True)
            LinuxIsolation(self.config)
            self.assertIsNotNone(process.wait(timeout=2))
            self.assertFalse((self.adapter.root / scope).exists())
            self.assertFalse(Path('/dev/mapper', name).exists())
            self.assertFalse(os.path.ismount(root))
        finally:
            if os.path.ismount(root):
                self.adapter.release(owner, root)

    def test_protected_scheduler_executes_and_cancels_in_encrypted_workspace(self):
        from aios import core
        from aios.sessiond import Service
        from aios.sessions import Sessions
        from test_identity_sessions import MemoryStore
        from test_scheduler import Provider

        provider = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        provider.daemon_threads = True
        provider.requests, provider.on_request = [], None
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        self.config.update(volume_store=str(self.root / 'volumes'), workspace_size_mib=64)
        self.adapter.config_path = self.root / 'config.json'
        sessions = Sessions(self.adapter, MemoryStore())
        self.addCleanup(sessions.suspend)
        owner = sessions.enroll('Protected fixture', '123456', True, None)['identity']
        sessions.activate_verified(owner, '123456', 'Scheduled private work')
        root = sessions.root
        foreign_root, _ = self.adapter.anonymous()
        try:
            os.chown(foreign_root / 'artifacts', sessions.uid, sessions.uid)
            foreign_scope = str(uuid.uuid4())
            with self.assertRaisesRegex(PermissionError, 'encrypted owner'):
                self.adapter._spawn(foreign_scope, foreign_root, sessions.uid, ['/bin/true'])
            self.assertFalse((self.adapter.root / foreign_scope).exists())
            self.assertFalse((self.adapter.cgroups / foreign_scope).exists())
        finally:
            self.adapter.release(None, foreign_root)
        config_path = root / 'artifacts' / '.aios' / 'config' / 'config.json'
        core.write_json(config_path, {
            'mode': 'remote', 'url': f'http://127.0.0.1:{provider.server_port}/v1',
            'model': 'test-model', 'api_key': 'KERNEL-PRIVATE-CREDENTIAL'})
        for path in [root / 'artifacts' / '.aios', *(root / 'artifacts' / '.aios').rglob('*')]:
            os.chown(path, sessions.uid, sessions.uid)
        service = Service(sessions, 1000, 1001, personal_enabled=True)
        service.dispatch({'action': 'display_attest', 'platform': 'eglfs', 'embedded': True}, 1000, 42)
        spawn = self.adapter._spawn
        diagnostics = patch.object(self.adapter, '_spawn',
                                   side_effect=lambda *args, **kwargs: spawn(*args, **kwargs, diagnostics=True))
        diagnostics.start()
        self.addCleanup(diagnostics.stop)

        def call(action, **fields):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                response = service.dispatch({
                    'action': 'scheduled_jobs', 'lease': sessions.lease, 'scope': sessions.work,
                    'request': {'action': action, **fields}}, 1000, 42)
                if response.get('error') == 'Protected scheduling is starting; retry shortly':
                    time.sleep(.05)
                    continue
                self.assertEqual(response['status'], 'ok', response)
                return response['result']
            self.fail('Protected service failed to initialize')

        job = call('create', config={
            'title': 'Kernel test', 'prompt': 'KERNEL-PRIVATE-PROMPT',
            'schedule': {'kind': 'cron', 'value': '0 0 * * *', 'zone': 'UTC'},
            'execution': {'provider': 'remote', 'profile': 'current', 'model': 'test-model',
                          'capabilities': [], 'timeout_seconds': 10}})
        run = call('run_now', job_id=job['id'], expected_revision=job['revision'],
                   request_id=str(uuid.uuid4()))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            service.tick()
            result = call('read_result', run_id=run['id'])
            if result['state'] not in ('queued', 'running'):
                break
            time.sleep(.05)
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertEqual(result['result'], 'Saved background answer')
        self.assertEqual(result['owner'], owner)
        self.assertNotIn('KERNEL-PRIVATE-CREDENTIAL', json.dumps(result))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual((root / 'artifacts' / '.aios' / 'data' / 'scheduled' /
                          'jobs.sqlite3').stat().st_mode & 0o777, 0o600)
        waiting = call('create', config={
            'title': 'Waiting kernel test', 'prompt': 'HANG KERNEL-PRIVATE-PROMPT',
            'schedule': {'kind': 'cron', 'value': '0 0 * * *', 'zone': 'UTC'},
            'execution': {'provider': 'remote', 'profile': 'current', 'model': 'test-model',
                          'capabilities': [], 'timeout_seconds': 10}})
        waiting_run = call('run_now', job_id=waiting['id'], expected_revision=waiting['revision'],
                           request_id=str(uuid.uuid4()))
        deadline = time.monotonic() + 10
        while len(provider.requests) < 2 and time.monotonic() < deadline:
            service.tick()
            time.sleep(.02)
        self.assertEqual(len(provider.requests), 2)
        scheduler_scope = sessions.scheduled.scope
        process = sessions.scheduled.process
        group = self.adapter.cgroups / scheduler_scope
        self.assertGreaterEqual(len((group / 'cgroup.procs').read_text().split()), 3)
        sessions._shield()
        self.assertIsNotNone(process.poll())
        self.assertFalse((self.adapter.cgroups / scheduler_scope).exists())
        with self.assertRaises(PermissionError):
            call('read_result', run_id=run['id'])
        sessions.suspend()
        image = Path(self.config['principals'][owner]['device'])
        encrypted = image.read_bytes()
        for secret in (b'KERNEL-PRIVATE-PROMPT', b'KERNEL-PRIVATE-CREDENTIAL', b'Saved background answer'):
            self.assertNotIn(secret, encrypted)
        sessions.activate_verified(owner, '123456', 'Reopened work')
        self.assertEqual(call('read_result', run_id=run['id'])['result'], 'Saved background answer')
        self.assertEqual(call('read_result', run_id=waiting_run['id'])['state'], 'cancelled')

    def test_protected_chat_runs_and_schedules_inside_encrypted_workspace(self):
        from aios import core
        from aios.sessiond import Service
        from aios.sessions import Sessions
        from test_identity_sessions import MemoryStore
        from test_scheduler import Provider

        provider = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        provider.daemon_threads = True
        provider.requests, provider.on_request = [], None
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        self.config.update(volume_store=str(self.root / 'volumes'), workspace_size_mib=64,
                           display_isolation_validated=True)
        self.adapter.config_path = self.root / 'config.json'
        sessions = Sessions(self.adapter, MemoryStore())
        self.addCleanup(sessions.suspend)
        owner = sessions.enroll('Protected chat fixture', '123456', True, None)['identity']
        sessions.activate_verified(owner, '123456', 'Private conversation')
        root, uid, work = sessions.root, sessions.uid, sessions.work
        config_path = root / 'artifacts' / '.aios' / 'config' / 'config.json'
        core.write_json(config_path, {
            'mode': 'remote', 'url': f'http://127.0.0.1:{provider.server_port}/v1',
            'model': 'test-model', 'api_key': 'CHAT-KERNEL-PRIVATE-CREDENTIAL'})
        for path in [root / 'artifacts' / '.aios', *(root / 'artifacts' / '.aios').rglob('*')]:
            os.chown(path, uid, uid)
        display_path = self.root / 'private-wayland'
        display = socket.socket(socket.AF_UNIX)
        display.bind(str(display_path))
        os.chown(display_path, uid, uid)
        self.addCleanup(display.close)
        self.config.setdefault('wayland_sockets', {})[str(uid)] = str(display_path)
        self.adapter.ready_displays.add(uid)
        service = Service(sessions, 1000, 1001, personal_enabled=True)
        service.dispatch({'action': 'display_attest', 'platform': 'eglfs', 'embedded': True},
                         1000, 42)

        def request(action, chat=None, key=None, **fields):
            value = {'action': action, 'lease': sessions.lease, 'scope': sessions.work,
                     **fields}
            if chat is not None:
                value.update(chat=chat, key=key)
            return service.dispatch(value, 1000, 42)

        opened = request('chat_open')
        chat, key = opened['chat'], opened['key']
        request('chat_send', chat, key, content='SCHEDULE a protected health check')
        deadline = time.monotonic() + 20
        events = []
        while time.monotonic() < deadline:
            reply = request('chat_poll', chat, key)
            events.extend(reply['events'])
            if any(item['type'] == 'done' for item in events):
                break
            service.tick()
            time.sleep(.05)
        self.assertIn('Protected chat scheduled result',
                      sessions.history()['messages'][-1]['content'])
        self.assertTrue(any(body.get('tools') for body in provider.requests))
        chat_scope = sessions.chats[chat].scope
        process = sessions.chats[chat].process
        sessions._shield()
        self.assertIsNotNone(process.poll())
        self.assertFalse((self.adapter.cgroups / chat_scope).exists())
        sessions.suspend()
        encrypted = Path(self.config['principals'][owner]['device']).read_bytes()
        for secret in (b'SCHEDULE a protected health check',
                       b'Protected chat scheduled result',
                       b'CHAT-KERNEL-PRIVATE-CREDENTIAL'):
            self.assertNotIn(secret, encrypted)
        sessions.activate_verified(owner, '123456', session=work)
        self.assertIn('Protected chat scheduled result',
                      sessions.history()['messages'][-1]['content'])

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
        if not package.exists():
            package.symlink_to('/workspace/apps', target_is_directory=True)
            self.addCleanup(package.unlink)
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
