import json
import io
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from aios import core, principals, scheduled_jobs
from aios.isolation import LinuxIsolation, SimulatorIsolation
from aios.scheduled_store import ScheduledStore
from aios.sessiond import Service, decode
from aios.sessions import Sessions
from aios.scheduling import UnavailableError
from test_identity_sessions import Clock, MemoryStore, evidence


FIXTURE = Path(__file__).parent / 'fixtures' / 'protected_scheduler_process.py'
CHAT_FIXTURE = Path(__file__).parent / 'fixtures' / 'protected_chat_process.py'


class PipeIsolation(SimulatorIsolation):
    """Executes only the deterministic fixture, never claims kernel isolation."""
    def __init__(self, root):
        super().__init__(root)
        self.processes = {}

    def scheduled(self, scope, root, uid):
        process = subprocess.Popen(
            [sys.executable, str(FIXTURE.resolve()), str(root), root.name, scope],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes[scope] = process
        return process

    def chat(self, scope, root, uid):
        process = subprocess.Popen(
            [sys.executable, str(CHAT_FIXTURE.resolve())],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes[scope] = process
        return process

    def stop(self, scope):
        super().stop(scope)
        process = self.processes.pop(scope, None)
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            error = process.stderr.read()
            process.stderr.close()
            if process.returncode not in (0, -15):
                raise RuntimeError(error.decode())


@unittest.skipUnless(os.name == 'posix' and os.getuid() != 0, 'Unprivileged Linux process tests')
class ProtectedSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='.protected-tests-', dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = patch.dict(os.environ, {
            'HOME': str(self.root / 'desktop'), 'XDG_DATA_HOME': str(self.root / 'desktop-data'),
            'XDG_CONFIG_HOME': str(self.root / 'desktop-config'),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.clock = Clock()
        self.isolation = PipeIsolation(self.root / 'owners')
        self.sessions = Sessions(self.isolation, MemoryStore(), self.clock, self.clock)
        self.a = self.sessions.enroll('Alice', '123456', True, None)['identity']
        self.b = self.sessions.enroll('Bob', '987654', True, None)['identity']
        self.service = Service(self.sessions, 1000, 1001, simulator=True)
        self.service.dispatch({'action': 'display_attest', 'platform': 'eglfs', 'embedded': True}, 1000, 42)
        self.addCleanup(self.sessions.suspend)
        for owner in (self.a, self.b):
            config = self.isolation.root / owner / 'artifacts' / '.aios' / 'config' / 'config.json'
            core.write_json(config, {'mode': 'remote', 'url': 'https://example.invalid/v1',
                                    'model': 'private-model', 'api_key': 'PRIVATE CREDENTIAL ' + owner})
        self.activate(self.a)

    def activate(self, owner):
        self.sessions.activate_verified(owner, '123456' if owner == self.a else '987654', 'Private work')

    def request(self, action, **fields):
        return {'action': 'scheduled_jobs', 'lease': self.sessions.lease,
                'scope': self.sessions.work, 'request': {'action': action, **fields}}

    def call(self, action, **fields):
        deadline = time.monotonic() + 10
        while True:
            response = self.service.dispatch(self.request(action, **fields), 1000, 42)
            if response.get('error') != 'Protected scheduling is starting; retry shortly':
                break
            if time.monotonic() >= deadline:
                self.fail('Fixture did not initialize')
            time.sleep(.05)
        self.assertEqual(response['status'], 'ok', response)
        return response['result']

    def create(self, prompt='PRIVATE OWNER PROMPT'):
        return self.call('create', config={
            'title': 'Private job', 'prompt': prompt,
            'schedule': {'kind': 'cron', 'value': '* * * * *', 'zone': 'UTC'},
            'execution': {'provider': 'remote', 'profile': 'current', 'model': 'private-model',
                          'capabilities': [], 'timeout_seconds': 10}})

    def run_now(self, job):
        return self.call('run_now', job_id=job['id'], expected_revision=job['revision'],
                         request_id=str(uuid.uuid4()))

    def test_owner_storage_binding_results_and_desktop_separation(self):
        job = self.create()
        self.assertEqual(job['owner'], self.a)
        run = self.run_now(job)
        self.service.tick()
        result = self.call('read_result', run_id=run['id'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['result'], 'PRIVATE OWNER RESULT')
        self.assertNotIn('PRIVATE CREDENTIAL', json.dumps(result))
        self.assertEqual(len(self.call('unread')), 1)
        owner_path = self.sessions.root / 'artifacts' / '.aios' / 'data' / 'scheduled' / 'jobs.sqlite3'
        self.assertTrue(owner_path.is_file())
        self.assertFalse((core.data_dir() / 'scheduled').exists())
        desktop = ScheduledStore()
        try:
            self.assertEqual(desktop.list_jobs(), [])
            self.assertEqual(desktop.unread(), [])
            self.assertNotIn(b'PRIVATE', desktop.path.read_bytes())
        finally:
            desktop.close()

    def test_cross_owner_ids_and_old_chat_scope_cannot_read_or_mutate(self):
        job = self.create()
        run = self.run_now(job)
        self.service.tick()
        old_request = self.request('read_result', run_id=run['id'])
        self.sessions.suspend()
        self.activate(self.b)
        with self.assertRaises(PermissionError):
            self.service.dispatch(old_request, 1000, 42)
        self.assertEqual(self.call('list'), [])
        self.assertEqual(self.call('unread'), [])
        for action, fields in (
                ('get', {'job_id': job['id']}),
                ('read_result', {'run_id': run['id']}),
                ('acknowledge_result', {'run_id': run['id']}),
                ('pause', {'job_id': job['id'], 'expected_revision': 1})):
            with self.subTest(action=action):
                denied = self.service.dispatch(self.request(action, **fields), 1000, 42)
                self.assertEqual(denied['status'], 'unavailable')
                self.assertNotIn('PRIVATE', json.dumps(denied))
        self.sessions.suspend()
        self.activate(self.a)
        self.assertEqual(self.call('read_result', run_id=run['id'])['result'], 'PRIVATE OWNER RESULT')

    def test_wrong_display_pid_peer_and_informational_authority_are_not_capabilities(self):
        job = self.create()
        request = self.request('get', job_id=job['id'])
        for uid, pid in ((1000, 43), (1000, None), (1001, 42), (self.sessions.uid, 42)):
            with self.subTest(uid=uid, pid=pid), self.assertRaises(PermissionError):
                self.service.dispatch(request, uid, pid)
        for key in ('lease', 'scope'):
            stale = {**request, key: str(uuid.uuid4())}
            with self.assertRaises(PermissionError):
                self.service.dispatch(stale, 1000, 42)
        self.assertEqual(self.sessions.status()['authority'], 'verified')
        self.sessions._shield()
        with self.assertRaises(PermissionError):
            self.service.dispatch(request, 1000, 42)

    def test_privacy_loss_stops_process_revokes_feedback_and_cancels_run_on_reopen(self):
        job = self.create('WAIT')
        run = self.run_now(job)
        self.service.tick()
        self.assertEqual(self.call('read_result', run_id=run['id'])['state'], 'running')
        process = self.sessions.scheduled.process
        scope = self.sessions.scheduled.scope
        self.sessions.evidence([evidence(self.b)])
        self.clock.advance(1.1)
        self.sessions.evidence([evidence(self.b)])
        self.assertTrue(self.sessions.shield or self.sessions.owner is None)
        self.assertIsNotNone(process.poll())
        self.assertIn(scope, self.isolation.stopped)
        self.assertIsNone(self.sessions.scheduled)
        with self.assertRaises(PermissionError):
            self.call('unread')
        self.sessions.suspend()
        self.activate(self.a)
        self.assertEqual(self.call('read_result', run_id=run['id'])['state'], 'cancelled')

    def test_privacy_loss_cancels_queued_work_without_dispatch(self):
        job = self.create('WAIT')
        run = self.run_now(job)
        self.assertEqual(run['state'], 'queued')
        process = self.sessions.scheduled.process
        stale = self.request('read_result', run_id=run['id'])
        self.sessions._shield()
        self.assertIsNotNone(process.poll())
        with self.assertRaises(PermissionError):
            self.service.dispatch(stale, 1000, 42)
        self.sessions.suspend()
        self.activate(self.a)
        result = self.call('read_result', run_id=run['id'])
        self.assertEqual(result['state'], 'cancelled')
        self.assertIsNone(result['started'])
        self.assertEqual(result['result'], '')

    def test_same_owner_replacement_does_not_reauthorize_old_chat_scope(self):
        job = self.create()
        stale = self.request('get', job_id=job['id'])
        self.sessions.suspend()
        self.activate(self.a)
        for request in (stale, {**stale, 'lease': self.sessions.lease}):
            with self.assertRaises(PermissionError):
                self.service.dispatch(request, 1000, 42)
        self.assertEqual(self.call('get', job_id=job['id'])['owner'], self.a)

    def test_catalog_only_and_expired_presence_never_dispatch(self):
        self.sessions.suspend()
        self.sessions.activate_verified(self.a, '123456')
        with self.assertRaises(PermissionError):
            self.call('health')
        self.sessions.suspend()
        self.activate(self.a)
        self.create()
        process = self.sessions.scheduled.process
        self.clock.advance(121)
        self.service.tick()
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.sessions.scheduled)

    def test_broker_heartbeat_loss_stops_service_without_unattended_dispatch(self):
        job = self.create('WAIT')
        run = self.run_now(job)
        process = self.sessions.scheduled.process
        process.wait(timeout=4)
        self.assertEqual(process.returncode, 0)
        self.sessions._stop_scheduled()
        self.assertEqual(self.call('read_result', run_id=run['id'])['state'], 'cancelled')

    def test_authorization_is_rechecked_after_read_before_disclosing_result(self):
        job = self.create()
        adapter = self.sessions.scheduled
        real = adapter.authorize
        checks = 0
        def expire():
            nonlocal checks
            checks += 1
            if checks == 2:
                self.clock.advance(121)
            real()
        with patch.object(adapter, 'authorize', side_effect=expire):
            with self.assertRaises(PermissionError):
                self.call('get', job_id=job['id'])
        self.assertIsNone(self.sessions.scheduled)

    def test_inflight_result_and_ack_responses_are_discarded_on_privacy_loss(self):
        for action in ('read_result', 'acknowledge_result'):
            with self.subTest(action=action):
                job = self.create()
                run = self.run_now(job)
                self.service.tick()
                adapter = self.sessions.scheduled
                real = adapter.authorize
                checks = 0
                def expire():
                    nonlocal checks
                    checks += 1
                    if checks == 2:
                        self.clock.advance(121)
                    real()
                with patch.object(adapter, 'authorize', side_effect=expire):
                    with self.assertRaises(PermissionError):
                        self.call(action, run_id=run['id'])
                self.assertTrue(self.sessions.shield or self.sessions.owner is None)
                self.assertIsNone(self.sessions.scheduled)
                self.sessions.suspend()
                self.activate(self.a)

    def test_registered_display_replacement_cancels_and_hides_old_feedback(self):
        self.create()
        request = self.request('unread')
        process = self.sessions.scheduled.process
        self.service.dispatch({'action': 'display_attest', 'platform': 'eglfs', 'embedded': True},
                              1000, 43)
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.sessions.owner)
        for pid in (42, 43):
            with self.assertRaises(PermissionError):
                self.service.dispatch(request, 1000, pid)

    def test_missing_shell_heartbeat_cancels_scope(self):
        self.create()
        process = self.sessions.scheduled.process
        self.service.last_shell -= 4
        self.service.tick()
        self.assertIsNone(self.sessions.owner)
        self.assertIsNotNone(process.poll())

    def test_owner_activation_starts_saved_dispatch_without_opening_jobs_ui(self):
        self.assertIsNone(self.sessions.scheduled)
        self.service.tick()
        self.assertIsNotNone(self.sessions.scheduled)
        self.call('health')
        self.sessions._stop_scheduled()
        self.service.personal_enabled = False
        self.service.tick()
        self.assertIsNone(self.sessions.scheduled)

    def test_status_session_has_display_and_privacy_boundary(self):
        with patch.object(self.isolation, 'requires_display', True, create=True):
            current = self.service.dispatch({'action': 'status'}, 1000, 42)
            self.assertEqual(current['session'], self.sessions.work)
            self.assertNotIn('scope', current)
            self.assertEqual(current['lease'], self.sessions.lease)
            other_display = self.service.dispatch({'action': 'status'}, 1000, 43)
            for field in ('session', 'lease'):
                self.assertIsNone(other_display[field])
            self.sessions._shield()
            locked = self.service.dispatch({'action': 'status'}, 1000, 42)
            for field in ('session', 'lease'):
                self.assertIsNone(locked[field])

    def test_shield_during_async_startup_kills_scope_without_admitting_requests(self):
        with patch('aios.scheduled_protected.ProtectedScheduler.is_ready', return_value=False):
            reply = self.service.dispatch(self.request('health'), 1000, 42)
            self.assertEqual(reply, {'status': 'unavailable',
                                    'error': 'Protected scheduling is starting; retry shortly'})
            process = self.sessions.scheduled.process
            scope = self.sessions.scheduled.scope
            self.sessions._shield()
            self.assertIsNotNone(process.poll())
            self.assertIn(scope, self.isolation.stopped)
            self.assertIsNone(self.sessions.scheduled)
            status = self.service.dispatch({'action': 'status'}, 1000, 42)
            self.assertIsNone(status['session'])
            with self.assertRaises(PermissionError):
                self.call('health')
        self.sessions.suspend()
        self.activate(self.a)
        health = self.call('health')
        self.assertIs(health['available'], True)
        self.assertEqual(health['active_runs'], 0)

    def test_bad_startup_descriptor_stops_unattached_process_scope(self):
        original = self.isolation.scheduled
        started = []
        def broken_descriptor(*args):
            process = original(*args)
            started.append(process)
            process.stdout.close()
            return process
        with patch.object(self.isolation, 'scheduled', side_effect=broken_descriptor):
            response = self.service.dispatch(self.request('health'), 1000, 42)
        self.assertEqual(response['status'], 'unavailable')
        self.assertIsNone(self.sessions.scheduled)
        self.assertEqual(self.isolation.processes, {})
        self.assertIsNotNone(started[0].poll())
        self.assertTrue(started[0].stdin.closed)
        self.assertTrue(started[0].stdout.closed)

    def test_direct_principal_and_unvalidated_display_fail_closed(self):
        descriptor = principals.Principal(self.a, os.getuid(), self.sessions.work, self.sessions.root)
        with patch('aios.principals.current', return_value=descriptor):
            with self.assertRaises(UnavailableError):
                ScheduledStore()
            self.assertEqual(scheduled_jobs.request({'action': 'list'})['status'], 'unavailable')
        service = Service(self.sessions, 1000, 1001)
        service.dispatch({'action': 'display_attest', 'platform': 'eglfs', 'embedded': True}, 1000, 42)
        with self.assertRaises(PermissionError):
            service.dispatch(self.request('list'), 1000, 42)

    def test_wire_fields_cannot_override_owner_credentials_or_command(self):
        request = self.request('health')
        self.assertEqual(decode(json.dumps(request)), request)
        for key in ('owner', 'uid', 'command', 'pin', 'token'):
            with self.assertRaises(ValueError):
                decode(json.dumps({**request, key: 'untrusted'}))
            response = self.service.dispatch({**request, 'request': {
                'action': 'health', key: 'untrusted'}}, 1000, 42)
            self.assertEqual(response['status'], 'invalid')

    def chat_request(self, action, chat=None, key=None, **fields):
        request = {'action': action, 'lease': self.sessions.lease, 'scope': self.sessions.work,
                   **fields}
        if chat is not None:
            request['chat'] = chat
            request['key'] = key
        return request

    def test_protected_chat_streams_persists_and_reopens_only_same_work(self):
        opened = self.service.dispatch(self.chat_request('chat_open'), 1000, 42)
        chat, key = opened['chat'], opened['key']
        self.assertEqual(opened['history']['messages'], [])
        self.service.dispatch(self.chat_request(
            'chat_send', chat, key, content='PRIVATE CHAT PROMPT'), 1000, 42)
        deadline = time.monotonic() + 5
        reply = {'events': []}
        while time.monotonic() < deadline:
            reply = self.service.dispatch(self.chat_request('chat_poll', chat, key), 1000, 42)
            if any(item['type'] == 'done' for item in reply['events']):
                break
            time.sleep(.02)
        self.assertEqual([item['type'] for item in reply['events']], ['token', 'done'])
        self.assertEqual(self.sessions.history()['messages'][-2:],
                         [{'id': self.sessions.history()['messages'][-2]['id'],
                           'created': self.sessions.history()['messages'][-2]['created'],
                           'role': 'user', 'content': 'PRIVATE CHAT PROMPT'},
                          {'id': self.sessions.history()['messages'][-1]['id'],
                           'created': self.sessions.history()['messages'][-1]['created'],
                           'role': 'assistant', 'content': 'PRIVATE REPLY'}])
        self.service.dispatch(self.chat_request('chat_close', chat, key), 1000, 42)
        reopened = self.service.dispatch(self.chat_request('chat_open'), 1000, 42)
        self.assertEqual([item['content'] for item in reopened['history']['messages']],
                         ['PRIVATE CHAT PROMPT', 'PRIVATE REPLY'])
        self.assertFalse((core.data_dir() / 'conversation.json').exists())

    def test_protected_chat_scheduler_relay_and_stale_scope_rejection(self):
        self.call('health')
        opened = self.service.dispatch(self.chat_request('chat_open'), 1000, 42)
        chat, key = opened['chat'], opened['key']
        self.service.dispatch(self.chat_request('chat_send', chat, key, content='SCHEDULE'), 1000, 42)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reply = self.service.dispatch(self.chat_request('chat_poll', chat, key), 1000, 42)
            if any(item['type'] == 'done' for item in reply['events']):
                break
            time.sleep(.05)
        self.assertEqual(self.sessions.history()['messages'][-1]['content'], 'ok')
        stale = self.chat_request('chat_poll', chat, key)
        process = self.sessions.chats[chat].process
        scope = self.sessions.chats[chat].scope
        self.sessions._shield()
        self.assertIsNotNone(process.poll())
        self.assertIn(scope, self.isolation.stopped)
        self.assertEqual(self.sessions.chats, {})
        with self.assertRaises(PermissionError):
            self.service.dispatch(stale, 1000, 42)

    def test_protected_chat_rejects_cross_pid_chat_and_generation(self):
        opened = self.service.dispatch(self.chat_request('chat_open'), 1000, 42)
        chat, key = opened['chat'], opened['key']
        request = self.chat_request('chat_poll', chat, key)
        with self.assertRaises(PermissionError):
            self.service.dispatch(request, 1000, 43)
        old_lease, old_work = self.sessions.lease, self.sessions.work
        self.sessions.suspend()
        self.activate(self.a)
        for changed in (
                {**request, 'lease': old_lease, 'scope': old_work},
                {**request, 'lease': self.sessions.lease, 'scope': old_work},
                {**request, 'lease': old_lease, 'scope': self.sessions.work}):
            with self.assertRaises(PermissionError):
                self.service.dispatch(changed, 1000, 42)
        first = self.service.dispatch(self.chat_request('chat_open'), 1000, 42)
        second = self.service.dispatch(self.chat_request('chat_open'), 1000, 42)
        with self.assertRaises(PermissionError):
            self.service.dispatch(
                self.chat_request('chat_poll', first['chat'], second['key']), 1000, 42)

    def test_chat_protocol_is_fixed_and_bounded(self):
        base = self.chat_request('chat_open')
        self.assertEqual(decode(json.dumps(base)), base)
        for key in ('owner', 'pid', 'socket', 'command'):
            with self.assertRaises(ValueError):
                decode(json.dumps({**base, key: 'untrusted'}))
        with self.assertRaises(ValueError):
            decode(json.dumps({'action': 'chat_send', 'lease': self.sessions.lease,
                               'scope': self.sessions.work, 'chat': str(uuid.uuid4()),
                               'key': str(uuid.uuid4()), 'content': 'x', 'request': {}}))


class ProtectedIsolationContractTests(unittest.TestCase):
    def test_runner_preserves_owner_storage_but_removes_interactive_authority(self):
        from aios.scheduled_runner import execute
        host = Mock(stdin=io.BytesIO())
        descriptor = json.dumps({'owner': str(uuid.uuid4()), 'uid': 1234,
                                 'scope': str(uuid.uuid4()), 'workspace': '/workspace'})
        with patch.dict(os.environ, {'AIOS_PRINCIPAL': descriptor,
                'AIOS_DESKTOP_CONTROL_SOCKET': 'private-control', 'AIOS_BROWSER_SOCKET': 'private-browser',
                'AIOS_SESSION_SOCKET': 'private-broker', 'AIOS_SESSION_ID': 'private-session'}), \
                patch('aios.scheduled_runner.subprocess.Popen',
                      side_effect=[host, RuntimeError('fixture reached worker')]) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'fixture reached worker'):
                execute({'id': str(uuid.uuid4()), 'snapshot': {
                    'execution': {'timeout_seconds': 10}, 'context': '', 'prompt': 'Private prompt'}},
                    Path.cwd())
        for call in spawn.call_args_list:
            environment = call.kwargs['env']
            self.assertEqual(environment['AIOS_PRINCIPAL'], descriptor)
            self.assertNotIn('AIOS_DESKTOP_CONTROL_SOCKET', environment)
            self.assertNotIn('AIOS_BROWSER_SOCKET', environment)
            self.assertNotIn('AIOS_SESSION_SOCKET', environment)
            self.assertNotIn('AIOS_SESSION_ID', environment)

    def test_fixed_adapter_requires_unlocked_owner_mount(self):
        isolation = LinuxIsolation.__new__(LinuxIsolation)
        owner = str(uuid.uuid4())
        root = Path('owner-workspace')
        isolation.config = {'principals': {owner: {'uid': 1234}}}
        isolation.mounts = {}
        with patch.object(isolation, '_spawn') as spawn, patch('os.path.ismount', return_value=True):
            with self.assertRaises(PermissionError):
                isolation.scheduled(str(uuid.uuid4()), root, 1234)
            isolation.mounts[owner] = root
            scope = str(uuid.uuid4())
            isolation.scheduled(scope, root, 1234)
            spawn.assert_called_once_with(scope, root, 1234,
                ['/usr/bin/python3', '-m', 'aios.scheduled_protected'], scheduled=True)
            with self.assertRaises(PermissionError):
                isolation.scheduled(scope, root, 4321)

    def test_chat_adapter_requires_ready_private_display(self):
        isolation = LinuxIsolation.__new__(LinuxIsolation)
        owner = str(uuid.uuid4())
        root = Path('owner-workspace')
        isolation.config = {'principals': {owner: {'uid': 1234}},
                            'wayland_sockets': {'1234': '/private/display'}}
        isolation.mounts = {owner: root}
        isolation.ready_displays = set()
        with patch.object(isolation, '_spawn') as spawn, patch('os.path.ismount', return_value=True), \
                patch.object(Path, 'is_socket', return_value=True), \
                patch.object(Path, 'stat', return_value=Mock(st_uid=1234)):
            with self.assertRaises(PermissionError):
                isolation.chat(str(uuid.uuid4()), root, 1234)
            isolation.ready_displays.add(1234)
            scope = str(uuid.uuid4())
            isolation.chat(scope, root, 1234)
            spawn.assert_called_once_with(
                scope, root, 1234, ['/usr/bin/python3', '-m', 'aios.protected_chat'],
                Path('/private/display'), scheduled=True)


class ProtectedPipeProtocolTests(unittest.TestCase):
    def test_relay_stop_is_turn_cancellation_not_session_eof(self):
        from aios.protected_chat import TurnStopped, _relay_request
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        connection.recv.side_effect = [
            json.dumps({'action': 'scheduled_jobs',
                        'request': {'action': 'health'}}).encode() + b'\n']
        server = Mock()
        server.accept.return_value = connection, None
        with self.assertRaises(TurnStopped):
            _relay_request(server, Mock(), lambda: {'action': 'stop'})

    def test_malformed_control_frames_and_eof_never_dispatch(self):
        from aios.scheduled_protected import serve
        for frame in (b'', b'{"tick":1}\n', b'{"tick":false}\n',
                      b'{"tick":true}\n{"tick":true}\n',
                      b'{"request":{"action":"health"},"owner":"forged"}\n'):
            with self.subTest(frame=frame):
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd, 'rb') as source, io.BytesIO() as output:
                    with os.fdopen(write_fd, 'wb') as sender:
                        sender.write(frame)
                    scheduler = Mock(stopping=False)
                    serve(scheduler, source, output)
                    scheduler.tick.assert_not_called()
                    scheduler.dispatch.assert_not_called()
                    self.assertEqual(output.getvalue(), b'')

    def test_malformed_sandbox_reply_is_never_returned_to_native_display(self):
        from aios.scheduled_protected import ProtectedScheduler
        for value in (['private-data'], {'status': 'unexpected', 'result': 'private-data'},
                      {'status': 'ok', 'result': {}, 'lease': 'forged'},
                      {'status': 'unavailable', 'error': {'private-data': True}}):
            with self.subTest(value=value), ExitStack() as stack:
                request_read, request_write = os.pipe()
                reply_read, reply_write = os.pipe()
                stack.enter_context(os.fdopen(request_read, 'rb'))
                sender = stack.enter_context(os.fdopen(request_write, 'wb'))
                receiver = stack.enter_context(os.fdopen(reply_read, 'rb'))
                reply = stack.enter_context(os.fdopen(reply_write, 'wb'))
                reply.write(json.dumps(value).encode() + b'\n')
                reply.flush()
                adapter = ProtectedScheduler.__new__(ProtectedScheduler)
                adapter.authorize = Mock()
                adapter.is_ready = Mock(return_value=True)
                adapter.process = Mock(stdin=sender, stdout=receiver)
                adapter.process.poll.return_value = None
                with self.assertRaises(UnavailableError):
                    adapter.exchange({'request': {'action': 'health'}})
