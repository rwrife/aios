import json
import os
from pathlib import Path
import shutil
import socket
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from aios import core, scheduled_jobs, scheduled_tool, skills, toolhost
from aios.agent import AgentSession, MAX_CALL_BYTES, MAX_CALL_OVERHEAD
from aios.scheduler import Scheduler
from aios.scheduled_store import ScheduledStore
from test_toolhost import FakeApplications, FakeBrowser, FakeMcp


class ScheduledToolSchemaTests(unittest.TestCase):
    def host(self, **options):
        host = toolhost.ToolHost(FakeBrowser(), FakeApplications(), FakeMcp(), **options)
        self.addCleanup(host.close)
        return host

    def config(self):
        return {
            'title': 'Summary', 'prompt': 'Summarize the release notes',
            'schedule': {'kind': 'cron', 'value': '0 9 * * 1-5', 'zone': 'America/Los_Angeles'},
            'execution': {'provider': 'remote', 'profile': 'current@' + 'a' * 64,
                          'model': 'model', 'capabilities': ['browser']},
        }

    def test_registered_closed_action_schemas_and_no_import_cycle(self):
        definitions = self.host().definitions()['tools']
        definition = next(item for item in definitions if item['function']['name'] == 'scheduled_jobs')
        branches = definition['function']['parameters']['oneOf']
        self.assertEqual({branch['properties']['action']['enum'][0] for branch in branches},
                         set(scheduled_jobs.ACTIONS))
        for branch in branches:
            self.assertFalse(branch['additionalProperties'])
            action = branch['properties']['action']['enum'][0]
            self.assertEqual(set(branch['required']), {'action', *scheduled_jobs.ACTIONS[action][0]})
        self.assertEqual(scheduled_tool.definition(), definition)

    def test_rejects_unknown_fields_unbound_routes_and_invalid_nested_policy(self):
        for mutate in (
            lambda value: value.update(owner=str(uuid.uuid4())),
            lambda value: value['config'].update(command='run something'),
            lambda value: value['config']['execution'].update(api_key='secret'),
            lambda value: value['config']['execution'].update(profile='current'),
            lambda value: value['config']['execution'].update(timeout_seconds=True),
            lambda value: value['config']['execution'].update(capabilities=['browser', 'browser']),
            lambda value: value['config']['schedule'].update(zone=None),
            lambda value: value['config'].update(notification={'mode': 'all', 'unknown': True}),
            lambda value: value['config'].update(prompt='x' * 1025),
        ):
            value = {'action': 'create', 'config': self.config()}
            mutate(value)
            with self.subTest(value=value), patch.object(scheduled_jobs, 'request') as request:
                self.assertEqual(self.host().call('scheduled_jobs', value)['status'], 'invalid')
                request.assert_not_called()
        for value in ({'action': 'pause', 'job_id': str(uuid.uuid4())},
                      {'action': 'unread', 'limit': 11},
                      {'action': 'unread', 'after': True},
                      {'action': 'list', 'after': 1},
                      {'action': 'run_now', 'job_id': str(uuid.uuid4()),
                       'expected_revision': 1, 'request_id': 'not-a-uuid'}):
            with self.subTest(value=value):
                self.assertEqual(scheduled_jobs.act(value)['status'], 'invalid')

    def test_worst_case_schema_fields_fit_agent_and_host_transports(self):
        value = {'action': 'update', 'job_id': str(uuid.uuid4()),
                 'expected_revision': 2 ** 63 - 1, 'config': self.config()}
        config = value['config']
        config.update(title='😀' * 50, prompt='😀' * 1024, context='😀' * 384,
                      conversation='😀' * 50)
        config['schedule'].update(value=('0,' * 98) + '0 *', zone='A' * 100)
        config['execution'].update(
            model='😀' * 50, capabilities=['a' * 62 + f'{index:02}' for index in range(32)],
            timeout_seconds=900, token_budget=32768, tool_budget=32, missed_run='coalesce')
        config['notification'] = {
            'mode': 'actionable', 'quiet_hours': {'start': '09:00', 'end': '17:00', 'zone': 'A' * 100},
            'snooze_until': '2' * 40}
        scheduled_tool.validate(value)
        for ascii_only in (False, True):
            raw = json.dumps(value, ensure_ascii=ascii_only).encode()
            self.assertLess(len(raw), MAX_CALL_BYTES - MAX_CALL_OVERHEAD)
            envelope = {'action': 'call', 'name': 'scheduled_jobs', 'arguments': value}
            self.assertLess(len(json.dumps(envelope, ensure_ascii=ascii_only).encode()),
                            toolhost.REQUEST_LIMIT)
        self.assertLess(len(config['prompt'].encode()), 32768)
        self.assertLess(len(config['context'].encode()), 65536)

    def test_background_cannot_advertise_or_call_even_with_widened_allowlist(self):
        host = self.host(background={'capabilities': ['scheduled_jobs'], 'tool_budget': 10})
        with patch('aios.scheduled_execution.permitted_capabilities',
                   return_value={'capabilities': ['scheduled_jobs']}):
            self.assertEqual(host.definitions()['tools'], [])
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                host.call('scheduled_jobs', {'action': 'health'})

    def test_protected_context_without_route_never_uses_desktop_socket(self):
        with patch('aios.principals.current', return_value=object()), \
                patch.object(scheduled_jobs, 'socket_path') as endpoint:
            result = self.host().call('scheduled_jobs', {'action': 'health'})
            self.assertEqual(result['status'], 'unavailable')
            endpoint.assert_not_called()
        for marker in ('AIOS_SESSION_SOCKET', 'AIOS_SESSION_ID'):
            with patch.dict(os.environ, {marker: 'protected-context'}), \
                    patch.object(scheduled_jobs, 'socket_path') as endpoint:
                result = self.host().call('scheduled_jobs', {'action': 'health'})
                self.assertEqual(result['status'], 'unavailable')
                endpoint.assert_not_called()

    def test_readback_limit_and_default_page_preserve_explicit_status(self):
        with patch.object(scheduled_jobs, 'request', return_value={'status': 'ok', 'result': []}) as request:
            self.assertEqual(scheduled_jobs.act({'action': 'unread'}), {'status': 'ok', 'result': []})
            request.assert_called_once_with({'action': 'unread', 'limit': 10})
        with patch.object(scheduled_jobs, 'request',
                          return_value={'status': 'ok', 'result': 'x' * 65536}):
            value = scheduled_jobs.act({'action': 'read_result', 'run_id': str(uuid.uuid4())})
            self.assertEqual(value['status'], 'unavailable')
            self.assertIn('native', value['error'])
        for status in scheduled_jobs.STATUSES - {'ok'}:
            result = {'status': status, 'error': 'Safe service error'}
            with patch.object(scheduled_jobs, 'request', return_value=result):
                self.assertEqual(scheduled_jobs.act({'action': 'health'}), result)

    def test_shipped_skill_discovery_and_triggers(self):
        with patch.object(skills, 'BUILTIN_SKILLS_ROOT', Path('apps') / 'skills'), \
                patch.object(skills, 'USER_SKILLS_ROOT', Path('apps') / 'skills'):
            catalog = skills.load_skills()
        skill = next(item for item in catalog if item.name == 'scheduled-jobs')
        self.assertFalse(skill.allowed_tools)
        for prompt in ('/scheduled-jobs', 'Every weekday at 9 AM summarize release notes',
                       'Schedule a task tomorrow', 'Pause that job'):
            self.assertIn(skill, skills.initial_skills(catalog, prompt))
        self.assertIn('never substitute UTC', skill.instructions)
        self.assertIn('cp -R "$ROOT/apps/skills"', Path('scripts/build-apps.sh').read_text())


class ScheduledToolServiceTests(unittest.TestCase):
    """Real tool/scheduler protocol and store; process startup is tested separately."""

    def setUp(self):
        self.root = Path('sj-' + uuid.uuid4().hex[:8])
        self.root.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.root)
        environment = patch.dict(os.environ, {
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_DATA_HOME': str(self.root / 'data'), 'TZ': 'UTC',
        })
        environment.start()
        self.addCleanup(environment.stop)
        core.write_json(core.config_dir() / 'config.json', {
            'mode': 'remote', 'url': 'https://provider.example/v1',
            'model': 'test-model', 'api_key': 'fixture-key', 'agent_mode': 'current',
        })
        endpoint = patch.object(scheduled_jobs, 'socket_path', return_value=self.root / 'service.sock')
        endpoint.start()
        self.addCleanup(endpoint.stop)
        self.tool_socket = self.root / 'tools.sock'
        addresses = {
            str(self.root / 'service.sock'): '\0aios-scheduled-' + uuid.uuid4().hex,
            str(self.tool_socket): '\0aios-tools-' + uuid.uuid4().hex,
        }
        # Linux abstract sockets preserve framing and peer credentials without
        # requiring Unix socket inodes on the Windows-backed worktree filesystem.
        class FixtureSocket(socket.socket):
            def bind(self, address):
                return super().bind(addresses.get(address, address))

            def connect(self, address):
                return super().connect(addresses.get(address, address))

        sockets = patch.object(socket, 'socket', FixtureSocket)
        sockets.start()
        self.addCleanup(sockets.stop)
        chmod = os.chmod
        permissions = patch.object(os, 'chmod', side_effect=lambda path, *args, **kwargs:
                                   None if str(path) in addresses else chmod(path, *args, **kwargs))
        permissions.start()
        self.addCleanup(permissions.stop)
        self.start_service()
        self.addCleanup(self.stop_service)
        host = toolhost.ToolHost(FakeBrowser(), FakeApplications(), FakeMcp())
        self.host_thread = threading.Thread(target=toolhost.serve, args=(self.tool_socket, host))
        self.host_thread.start()
        self.addCleanup(self.stop_host)
        definitions = toolhost.list_tools(self.tool_socket)
        self.assertIn('scheduled_jobs', [item['function']['name'] for item in definitions['tools']])

    def start_service(self):
        ready = threading.Event()
        self.service_errors = []

        def serve():
            try:
                # Exercise the real protocol/dispatcher on filesystems without
                # POSIX private-directory modes. Do not launch workers here:
                # startup isolation and execution have dedicated process tests.
                service = Scheduler.__new__(Scheduler)
                service.root = self.root
                service.protected = False
                service.running = {}
                service.stopping = False
                service.health_error = None
                service.store = ScheduledStore(root=self.root / 'store')
                service.tick = lambda: None
                self.service = service
                ready.set()
                try:
                    service.serve()
                finally:
                    service.store.close()
            except BaseException as error:
                self.service_errors.append(error)
                ready.set()

        self.service_thread = threading.Thread(target=serve)
        self.service_thread.start()
        self.assertTrue(ready.wait(5))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.service_errors:
                raise self.service_errors[0]
            if scheduled_jobs.request({'action': 'health'})['status'] == 'ok':
                return
            time.sleep(0.01)
        self.fail('Scheduler protocol fixture failed to start')

    def stop_service(self):
        self.service.stopping = True
        self.service_thread.join(timeout=5)
        self.assertFalse(self.service_thread.is_alive())
        self.assertEqual(self.service_errors, [])

    def stop_host(self):
        if self.host_thread.is_alive():
            toolhost.close_service(self.tool_socket)
            self.host_thread.join(timeout=5)
        self.assertFalse(self.host_thread.is_alive())

    def response(self, action, **arguments):
        return toolhost.call(self.tool_socket, 'scheduled_jobs', {'action': action, **arguments})

    def call(self, action, **arguments):
        result = self.response(action, **arguments)
        self.assertEqual(result['status'], 'ok', result)
        return result['result']

    def config(self, prompt='Summarize the task'):
        binding = self.call('binding', prompt=prompt)
        self.assertEqual(binding['zone'], 'UTC')
        return {
            'title': 'Tool integration', 'prompt': prompt,
            'schedule': {'kind': 'cron', 'value': '0 9 * * 1-5', 'zone': binding['zone']},
            'execution': {key: binding[key] for key in ('provider', 'profile', 'model')}
                         | {'capabilities': [], 'timeout_seconds': 10},
        }

    def test_crud_preview_conflict_and_job_pagination(self):
        config = self.config()
        preview = self.call('preview', schedule=config['schedule'])
        first = self.call('create', config=config)
        self.assertEqual(first['next_occurrences'], preview)
        self.assertEqual(first['next_due'], preview[0]['utc'])
        self.assertTrue(first['enabled'])
        self.assertEqual(first['execution']['missed_run'], 'coalesce')
        second = self.call('create', config=config)
        page = self.call('list', limit=1)
        self.assertNotIn('prompt', page[0])
        remaining = self.call('list', limit=1, after=page[0]['id'])
        self.assertEqual({page[0]['id'], remaining[0]['id']}, {first['id'], second['id']})
        paused = self.call('pause', job_id=first['id'], expected_revision=first['revision'])
        self.assertFalse(paused['enabled'])
        self.assertEqual(self.response('resume', job_id=first['id'],
                                      expected_revision=first['revision'])['status'], 'conflict')
        resumed = self.call('resume', job_id=first['id'], expected_revision=paused['revision'])
        config['title'] = 'Edited'
        edited = self.call('update', job_id=first['id'], expected_revision=resumed['revision'], config=config)
        self.assertEqual(edited['title'], 'Edited')
        self.assertEqual(self.call('get', job_id=first['id'])['revision'], edited['revision'])
        self.assertEqual(self.call('delete', job_id=first['id'], expected_revision=edited['revision']),
                         {'deleted': True})
        self.assertEqual([item['id'] for item in self.call('list')], [second['id']])

    def test_agent_session_advertises_and_dispatches_shipped_skill_tool(self):
        with patch.object(skills, 'BUILTIN_SKILLS_ROOT', Path('apps') / 'skills'), \
                patch.object(skills, 'USER_SKILLS_ROOT', Path('apps') / 'skills'):
            catalog = skills.load_skills()
        session = AgentSession(
            [{'role': 'user', 'content': 'Every weekday at 9 AM summarize release notes'}],
            self.tool_socket, catalog=catalog)
        self.assertIn('scheduled-jobs', session.active)
        self.assertIn('scheduled_jobs', [item['function']['name'] for item in session.tools()])
        result = session.dispatch('scheduled_jobs', {'action': 'binding', 'prompt': 'Summarize release notes'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['result']['model'], 'test-model')
        self.assertNotIn('fixture-key', json.dumps(result))

    def test_run_now_durable_retry_snapshot_and_unread_acknowledgement(self):
        config = self.config()
        job = self.call('create', config=config)
        arguments = {'job_id': job['id'], 'expected_revision': job['revision'],
                     'request_id': str(uuid.uuid4())}
        run = self.call('run_now', **arguments)
        config['prompt'] = 'A replacement prompt'
        edited = self.call('update', job_id=job['id'], expected_revision=job['revision'], config=config)
        self.assertGreater(edited['revision'], job['revision'])
        result = self.call('read_result', run_id=run['id'])
        self.assertEqual(result['snapshot']['prompt'], 'Summarize the task')
        self.assertEqual(result['revision'], job['revision'])
        self.call('cancel_run', run_id=run['id'])
        self.stop_service()
        self.start_service()
        self.assertEqual(self.call('run_now', **arguments)['id'], run['id'])
        self.assertEqual(len(self.call('list_runs', job_id=job['id'])), 1)
        unread = self.call('unread')
        self.assertEqual([item['run_id'] for item in unread], [run['id']])
        self.call('read_result', run_id=run['id'])
        self.assertEqual(self.call('unread'), unread)
        self.assertEqual(self.call('unread', after=unread[0]['sequence']), [])
        self.assertEqual(self.call('list_runs', job_id=job['id'], before=result['sequence']), [])
        self.call('acknowledge_result', run_id=run['id'])
        self.assertEqual(self.call('unread'), [])

    def test_service_rejects_invented_capability_and_binding(self):
        config = self.config()
        config['execution']['capabilities'] = ['scheduled_jobs']
        self.assertEqual(self.response('create', config=config)['status'], 'invalid')
        config['execution']['capabilities'] = ['mcp_unknown_execute']
        self.assertEqual(self.response('create', config=config)['status'], 'invalid')
        config['execution']['capabilities'] = []
        config['execution']['model'] = 'other-model'
        self.assertEqual(self.response('create', config=config)['status'], 'needs_user_action')
        self.assertEqual(self.call('list'), [])

    def test_binding_keeps_null_zone_and_service_validates_timing(self):
        with patch('aios.scheduler.configured_zone', return_value=None):
            self.assertIsNone(self.call('binding', prompt='Summarize release notes')['zone'])
            self.assertIsNone(self.call('health')['zone'])
        config = self.config()
        config['schedule']['zone'] = 'NoSuch/Zone'
        self.assertEqual(self.response('preview', schedule=config['schedule'])['status'], 'invalid')
        config['schedule'] = {'kind': 'cron', 'value': '0 9 * * *', 'zone': 'UTC'}
        config['notification'] = {'mode': 'all', 'quiet_hours': {
            'start': '09:00', 'end': '09:00', 'zone': 'UTC'}}
        self.assertEqual(self.response('create', config=config)['status'], 'invalid')

    def test_cancel_run_reports_actual_terminal_readback(self):
        job = self.call('create', config=self.config('HANG'))
        run = self.call('run_now', job_id=job['id'], expected_revision=job['revision'],
                        request_id=str(uuid.uuid4()))
        cancelled = self.call('cancel_run', run_id=run['id'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(self.call('read_result', run_id=run['id'])['state'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
