import http.server
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from aios import core, scheduled_jobs
from aios.scheduled_execution import BackgroundContext
from aios.scheduling import UnavailableError


FIXTURE = Path(__file__).parent / 'fixtures' / 'scheduled_tool_process.py'


class Provider(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        if self.server.on_request is not None:
            self.server.on_request(body)
        prompt = ' '.join(str(item.get('content', '')) for item in body['messages'] if item['role'] == 'user')
        if 'AUTH_FAILURE' in prompt:
            self.send_error(401)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if 'HANG' in prompt:
            time.sleep(4)
        name = 'browser' if 'BROWSER' in prompt else 'mcp_fixture_ping'
        tools = 'MCP' in prompt or 'BROWSER' in prompt
        used = any(item['role'] == 'tool' for item in body['messages'])
        if tools and (not used or 'LOOP' in prompt):
            arguments = {'action': 'open', 'url': 'https://example.com'} if name == 'browser' else {}
            delta = {'tool_calls': [{'index': 0, 'id': str(uuid.uuid4()), 'type': 'function',
                                     'function': {'name': name, 'arguments': json.dumps(arguments)}}]}
            finish = 'tool_calls'
        else:
            delta = {'content': 'x' * 40000 if 'OVERSIZED' in prompt else 'Saved background answer'}
            finish = 'stop'
        event = {'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        try:
            self.wfile.write(('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def live(pid):
    try:
        state = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[0]
        return state != 'Z'
    except FileNotFoundError:
        return False


class SchedulerProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        cls.provider.daemon_threads = True
        cls.provider.requests = []
        cls.provider.on_request = None
        cls.thread = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.provider.server_close()
        cls.thread.join()

    def setUp(self):
        self.provider.on_request = None
        self.temp = tempfile.TemporaryDirectory(prefix='asj-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runtime = self.root / 'runtime'
        runtime.mkdir(mode=0o700)
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'aios-browser').write_text(FIXTURE.read_text())
        (bin_dir / 'aios-browser').chmod(0o700)
        (bin_dir / 'codex').write_text('#!/usr/bin/env python3\n' + (
            FIXTURE.parent / 'codex_server.py').read_text())
        (bin_dir / 'codex').chmod(0o700)
        self.env = patch.dict(os.environ, {
            'HOME': str(self.root), 'XDG_CONFIG_HOME': str(self.root / 'config'),
            'XDG_DATA_HOME': str(self.root / 'data'), 'XDG_RUNTIME_DIR': str(runtime),
            'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
            'SCHEDULED_TEST_PIDS': str(self.root / 'pids'), 'TZ': 'UTC',
            'AIOS_FAKE_LOG': str(self.root / 'codex-log'), 'AIOS_FAKE_SCENARIO': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = {'mode': 'remote', 'url': f'http://127.0.0.1:{self.provider.server_port}/v1',
                       'model': 'test-model', 'api_key': 'fixture-key', 'agent_mode': 'current'}
        core.write_json(core.config_dir() / 'config.json', self.config)
        self.processes = []
        self.addCleanup(self.stop_services)
        self.start_service()

    def start_service(self):
        process = subprocess.Popen([sys.executable, '-m', 'aios.scheduler'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(process)
        self.service = process
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read().decode())
            result = scheduled_jobs.request({'action': 'health'}, timeout=0.2)
            if result['status'] == 'ok':
                return
            time.sleep(0.05)
        self.fail('Scheduler did not become ready')

    def stop_services(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stderr.close()

    def call(self, action, **args):
        result = scheduled_jobs.request({'action': action, **args})
        self.assertEqual(result['status'], 'ok', result)
        return result['result']

    def job(self, prompt='Summarize the task', **policy):
        execution = {'provider': 'remote', 'profile': 'current', 'model': 'test-model',
                     'capabilities': [], 'timeout_seconds': 10, **policy}
        return self.call('create', config={'title': 'Test job', 'prompt': prompt,
                         'schedule': {'kind': 'cron', 'value': '0 0 * * *', 'zone': 'UTC'},
                         'execution': execution})

    def run_job(self, job):
        return self.call('run_now', job_id=job['id'], expected_revision=job['revision'],
                         request_id=str(uuid.uuid4()))

    def wait_run(self, run, state=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.call('read_result', run_id=run['id'])
            if result['state'] not in ('queued', 'running'):
                if state:
                    self.assertEqual(result['state'], state, result)
                return result
            time.sleep(0.05)
        self.fail('Run did not complete')

    def configure_mcp(self, hang=False, nested=False):
        core.write_json(core.config_dir() / 'mcp.json', {'servers': {'fixture': {
            'command': sys.executable, 'args': [str(FIXTURE.resolve())], 'tools': ['ping'],
            'env': {'SCHEDULED_TEST_PIDS': str(self.root / 'pids'),
                    'SCHEDULED_TEST_HANG': '1' if hang else '0',
                    'SCHEDULED_TEST_SCHEDULER': str(scheduled_jobs.socket_path()) if nested else '',
                    'SCHEDULED_TEST_NESTED_RESULT': str(self.root / 'nested-result')},
        }}})

    def wait_pids(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            path = self.root / 'pids'
            if path.exists() and len(path.read_text().split()) >= 2:
                return list(map(int, path.read_text().split()))
            time.sleep(0.05)
        self.fail('Tool subprocesses did not start')

    def assert_stopped(self, pids):
        self.assertTrue(all(not live(pid) for pid in pids), pids)

    def test_real_worker_agent_host_without_chat_and_no_history(self):
        job = self.job()
        self.assertIn('@', job['execution']['profile'])
        self.assertEqual(len(job['next_occurrences']), 3)
        run = self.run_job(job)
        result = self.wait_run(run, 'succeeded')
        self.assertEqual(result['result'], 'Saved background answer')
        self.assertFalse((core.data_dir() / 'conversation.json').exists())
        self.assertEqual(self.call('unread')[0]['run_id'], run['id'])
        self.call('acknowledge_result', run_id=run['id'])
        self.assertEqual(self.call('unread'), [])
        self.assertNotIn('fixture-key', json.dumps(result))
        self.assertIn('max_tokens', self.provider.requests[-1])

    def test_missing_credentials_pause_once_and_resume(self):
        job = self.job()
        self.config['api_key'] = ''
        core.write_json(core.config_dir() / 'config.json', self.config)
        run = self.run_job(job)
        self.wait_run(run, 'needs_user_action')
        paused = self.call('get', job_id=job['id'])
        self.assertFalse(paused['enabled'])
        time.sleep(1.2)
        self.assertEqual(len(self.call('unread')), 1)
        self.config['api_key'] = 'replacement-key'
        core.write_json(core.config_dir() / 'config.json', self.config)
        self.call('resume', job_id=job['id'], expected_revision=paused['revision'])

    def test_changed_route_never_silently_changes_model(self):
        job = self.job()
        self.config['model'] = 'other-model'
        core.write_json(core.config_dir() / 'config.json', self.config)
        self.wait_run(self.run_job(job), 'needs_user_action')

    def test_agent_profile_and_subscription_run_without_chat(self):
        self.config.update(agent_mode='remote', agent_url=self.config['url'],
                           agent_model='agent-model', agent_api_key='fixture-agent-key')
        core.write_json(core.config_dir() / 'config.json', self.config)
        self.wait_run(self.run_job(self.job(profile='agent', model='agent-model')), 'succeeded')
        self.config.update(mode='chatgpt', subscription_model='test-model')
        core.write_json(core.config_dir() / 'config.json', self.config)
        core.write_json(core.config_dir() / 'codex' / 'auth.json', {'fixture': True})
        run = self.run_job(self.job(provider='subscription'))
        result = self.wait_run(run, 'succeeded')
        self.assertEqual(result['result'], 'Hello 世界')
        requests = [json.loads(line) for line in (self.root / 'codex-log').read_text().splitlines()]
        started = next(item for item in requests if item.get('method') == 'thread/start')
        self.assertEqual(started['params']['model'], 'test-model')
        self.assertTrue(started['params']['ephemeral'])

    def test_local_jobs_share_session_runtime_without_chat_and_survive_timeout(self):
        from aios import local_runtime
        with socket.socket(socket.AF_INET) as probe:
            try:
                probe.bind(('127.0.0.1', 8080))
            except OSError:
                self.skipTest('Local inference port is occupied by another service')
        model = self.root / 'fixture.gguf'
        model.write_bytes(b'GGUF fixture')
        self.config.update(mode='local', model_path=str(model))
        core.write_json(core.config_dir() / 'config.json', self.config)
        pids_path = self.root / 'model-pids'
        runtime = subprocess.Popen(
            [sys.executable, str(FIXTURE.parent / 'scheduled_local_runtime.py')],
            env={**os.environ, 'SCHEDULED_TEST_MODEL_PIDS': str(pids_path)},
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.processes.append(runtime)
        result = self.wait_run(self.run_job(self.job(provider='local', model='local')), 'succeeded')
        self.assertEqual(result['result'], 'Local background answer')
        first_pid = int(pids_path.read_text().strip())
        with core.request('/chat/completions', {'model': 'local', 'messages': [
                {'role': 'user', 'content': 'Interactive task'}], 'stream': True}) as response:
            self.assertTrue(list(core.sse_events(response)))
        self.wait_run(self.run_job(self.job(provider='local', model='local')), 'succeeded')
        self.assertEqual(pids_path.read_text().split(), [str(first_pid)])
        self.wait_run(self.run_job(self.job('LOCAL_HANG', provider='local', model='local',
                                            timeout_seconds=3)), 'failed')
        self.assertIsNone(runtime.poll())
        deadline = time.monotonic() + 5
        while live(first_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(live(first_pid))
        self.wait_run(self.run_job(self.job(provider='local', model='local')), 'succeeded')
        self.assertEqual(len(pids_path.read_text().split()), 2)
        job = self.job(provider='local', model='local')
        model.write_bytes(b'GGUF changed replacement')
        self.wait_run(self.run_job(job), 'needs_user_action')
        local_runtime.stop_service()
        runtime.wait(timeout=5)

    def test_live_capability_revocation_prevents_tool_call(self):
        self.configure_mcp()
        def revoke(_):
            core.write_json(core.config_dir() / 'mcp.json', {'servers': {}})
        self.provider.on_request = revoke
        self.wait_run(self.run_job(self.job('MCP', capabilities=['mcp_fixture_ping'])), 'succeeded')
        self.assertFalse((self.root / 'pids').exists())
        messages = self.provider.requests[-1]['messages']
        self.assertTrue(any(item['role'] == 'tool' and 'unavailable' in item['content'] for item in messages))
        self.provider.on_request = None

    def test_background_mcp_cannot_access_scheduler_socket(self):
        self.configure_mcp(nested=True)
        self.wait_run(self.run_job(self.job('MCP', capabilities=['mcp_fixture_ping'])), 'succeeded')
        self.assertEqual((self.root / 'nested-result').read_text(), 'blocked')

    def test_rejected_credentials_are_action_needed(self):
        self.wait_run(self.run_job(self.job('AUTH_FAILURE')), 'needs_user_action')

    def test_output_and_tool_budgets(self):
        self.wait_run(self.run_job(self.job('OVERSIZED', token_budget=64)), 'failed')
        self.configure_mcp()
        self.wait_run(self.run_job(self.job('MCP LOOP', capabilities=['mcp_fixture_ping'], tool_budget=1)), 'failed')
        self.assert_stopped(self.wait_pids())

    def test_timeout_stops_real_worker(self):
        result = self.wait_run(self.run_job(self.job('HANG', timeout_seconds=1)), 'failed')
        self.assertIn('time limit', result['error'])
        self.assertEqual(self.call('health')['active_runs'], 0)

    def test_mcp_process_group_cleanup_on_success(self):
        self.configure_mcp()
        run = self.run_job(self.job('MCP', capabilities=['mcp_fixture_ping']))
        self.wait_run(run, 'succeeded')
        self.assert_stopped(self.wait_pids())

    @unittest.skipIf(os.getuid() == 0, 'Native browser explicitly refuses root')
    def test_browser_process_group_cleanup_on_success(self):
        run = self.run_job(self.job('BROWSER', capabilities=['browser']))
        self.wait_run(run, 'succeeded')
        self.assert_stopped(self.wait_pids())

    def test_cancel_delete_and_timeout_stop_mcp_descendants(self):
        self.configure_mcp(hang=True)
        for operation in ('cancel_run', 'delete', 'timeout'):
            (self.root / 'pids').unlink(missing_ok=True)
            job = self.job('MCP', capabilities=['mcp_fixture_ping'],
                           timeout_seconds=5 if operation == 'timeout' else 10)
            run = self.run_job(job)
            pids = self.wait_pids()
            if operation == 'cancel_run':
                self.call(operation, run_id=run['id'])
            elif operation == 'delete':
                self.call(operation, job_id=job['id'], expected_revision=job['revision'])
            self.wait_run(run, 'failed' if operation == 'timeout' else 'cancelled')
            self.assert_stopped(pids)

    def test_service_crash_cleans_descendants_before_recovery(self):
        self.configure_mcp(hang=True)
        run = self.run_job(self.job('MCP', capabilities=['mcp_fixture_ping']))
        pids = self.wait_pids()
        self.service.kill()
        self.service.wait(timeout=5)
        self.start_service()
        self.wait_run(run, 'interrupted')
        self.assert_stopped(pids)
        self.assertEqual(len(self.call('unread')), 1)

    def test_worker_and_guardian_crashes_stop_descendants(self):
        from aios.scheduled_runner import descendants
        self.configure_mcp(hang=True)
        for module, state in (('aios.worker', 'failed'), ('aios.scheduled_runner', 'interrupted')):
            (self.root / 'pids').unlink(missing_ok=True)
            run = self.run_job(self.job('MCP', capabilities=['mcp_fixture_ping']))
            pids = self.wait_pids()
            targets = []
            for pid in descendants(self.service.pid):
                try:
                    if module.encode() in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0'):
                        targets.append(pid)
                except FileNotFoundError:
                    pass
            self.assertEqual(len(targets), 1)
            os.kill(targets[0], signal.SIGKILL)
            self.wait_run(run, state)
            self.assert_stopped(pids)

    def test_manual_idempotency_and_active_snapshot_survive_edit(self):
        job = self.job('HANG', timeout_seconds=8)
        request_id = str(uuid.uuid4())
        first = self.call('run_now', job_id=job['id'], expected_revision=1, request_id=request_id)
        repeated = self.call('run_now', job_id=job['id'], expected_revision=1, request_id=request_id)
        self.assertEqual(first['id'], repeated['id'])
        config = {key: job[key] for key in ('title', 'prompt', 'context', 'conversation',
                                           'schedule', 'execution', 'notification')}
        config['prompt'] = 'Replacement prompt'
        self.call('update', job_id=job['id'], expected_revision=1, config=config)
        result = self.wait_run(first, 'succeeded')
        self.assertEqual(result['snapshot']['prompt'], 'HANG')
        self.assertEqual(result['revision'], 1)

    def test_shutdown_preserves_jobs_and_interrupts_work(self):
        self.configure_mcp(hang=True)
        job = self.job('MCP', capabilities=['mcp_fixture_ping'])
        run = self.run_job(job)
        pids = self.wait_pids()
        self.service.terminate()
        self.service.wait(timeout=15)
        self.assert_stopped(pids)
        self.start_service()
        self.wait_run(run, 'interrupted')
        self.assertEqual(self.call('get', job_id=job['id'])['id'], job['id'])

    def test_singleton_socket_permissions_and_peer_identity(self):
        second = subprocess.run([sys.executable, '-m', 'aios.scheduler'], capture_output=True, timeout=5)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn(b'already owns', second.stderr)
        other_runtime = self.root / 'other'
        other_runtime.mkdir(mode=0o700)
        second = subprocess.run([sys.executable, '-m', 'aios.scheduler'], capture_output=True, timeout=5,
                                env={**os.environ, 'XDG_RUNTIME_DIR': str(other_runtime)})
        self.assertNotEqual(second.returncode, 0)
        self.assertIn(b'already owns', second.stderr)
        self.assertEqual(scheduled_jobs.socket_path().stat().st_mode & 0o777, 0o600)
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(str(scheduled_jobs.socket_path()))
            scheduled_jobs.same_user(connection)
        class WrongPeer:
            def getsockopt(self, *_):
                return struct.pack('3i', 1, os.getuid() + 1, 1)
        with self.assertRaises(UnavailableError):
            scheduled_jobs.same_user(WrongPeer())

    def test_binding_preview_and_bounded_protocol(self):
        settings = self.call('binding', prompt='Write a summary')
        self.assertEqual(settings['zone'], 'UTC')
        self.assertEqual(settings['provider'], 'remote')
        self.assertNotIn('scheduled_jobs', settings['capabilities'])
        values = self.call('preview', schedule={'kind': 'cron', 'value': '0 9 * * 1-5', 'zone': 'UTC'})
        self.assertEqual(len(values), 3)
        for value in ({'action': 'shell', 'command': 'true'},
                      {'action': 'health', 'owner': 'someone'},
                      {'action': 'list', 'limit': 101}):
            self.assertEqual(scheduled_jobs.request(value)['status'], 'invalid')
        self.job()
        self.assertNotIn('prompt', self.call('list')[0])
        with socket.socket(socket.AF_UNIX) as slow:
            slow.connect(str(scheduled_jobs.socket_path()))
            slow.sendall(b'{')
            self.assertTrue(self.call('health')['available'])


class BackgroundPolicyTests(unittest.TestCase):
    def test_tool_host_enforces_native_signin_and_nested_schedule_denials(self):
        from aios.toolhost import ToolHost
        host = ToolHost(background={'capabilities': ['os_settings', 'scheduled_jobs'], 'tool_budget': 2})
        self.addCleanup(host.close)
        for name, arguments in (('os_settings', {'action': 'authenticate'}),
                                ('scheduled_jobs', {'action': 'create'})):
            with self.assertRaises(ValueError):
                host.call(name, arguments)

    def test_independent_budget_counts_and_reported_usage(self):
        context = BackgroundContext({'token_budget': 8, 'tool_budget': 1})
        context.output('abc')
        context.reported_usage({'prompt_tokens': 20, 'completion_tokens': 5})
        self.assertEqual(context.remaining_tokens, 3)
        context.tool()
        with self.assertRaises(RuntimeError):
            context.tool()
        with self.assertRaises(RuntimeError):
            context.output('abcdef')

    def test_protected_client_is_unavailable_before_connecting(self):
        with patch('aios.scheduled_execution.principals.current', return_value=object()):
            self.assertEqual(scheduled_jobs.request({'action': 'health'})['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
