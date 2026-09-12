import io
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from aios import agent, core, subscription, toolhost
from aios.applications import APPLICATION_TOOL
from aios.browser import TOOL as BROWSER_TOOL
from aios.skills import Skill


def clone(value):
    return json.loads(json.dumps(value))


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log = Path(self.temp.name) / 'requests.jsonl'
        self.env = patch.dict(os.environ, {
            'XDG_CONFIG_HOME': self.temp.name, 'AIOS_FAKE_LOG': str(self.log),
            'AIOS_FAKE_SCENARIO': '', 'OPENAI_API_KEY': 'must-not-inherit',
            'CODEX_API_KEY': 'must-not-inherit', 'CODEX_HOME': 'must-not-inherit',
        })
        self.env.start()
        original = subprocess.Popen
        fixture = str(Path(__file__).parent / 'fixtures/codex_server.py')
        self.processes = []
        self.popen_args = []
        self.popen_kwargs = []
        def spawn(args, **kwargs):
            self.popen_args.append(list(args))
            self.popen_kwargs.append(dict(kwargs))
            process = original([sys.executable, fixture], **kwargs)
            self.processes.append(process)
            return process
        self.spawn = patch.object(subscription.subprocess, 'Popen', side_effect=spawn)
        self.spawn.start()

    def tearDown(self):
        for process in reversed(self.processes):
            self.kill_process_group(process)
        self.spawn.stop()
        self.env.stop()
        self.temp.cleanup()

    def bounded(self, function, timeout=1.0, cleanup=None):
        outcomes = queue.Queue()

        def invoke():
            try:
                outcomes.put((True, function()))
            except BaseException as error:
                outcomes.put((False, error))

        thread = threading.Thread(target=invoke, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(timeout)
        elapsed = time.monotonic() - started
        finished = not thread.is_alive()
        if not finished and cleanup is not None:
            cleanup()
            thread.join(1)
        outcome = outcomes.get_nowait() if not outcomes.empty() else None
        return finished, outcome, elapsed

    def kill_process_group(self, process, process_group=None):
        if process is None:
            return
        if os.name == 'posix':
            if process_group is None and process.poll() is None:
                try:
                    process_group = os.getpgid(process.pid)
                except ProcessLookupError:
                    return
            if process_group is None:
                return
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_config_and_transport_do_not_use_api_key(self):
        core.save_config({'mode': 'chatgpt', 'subscription_model': 'test', 'api_key': 'retained'})
        self.assertEqual(core.load_config()['api_key'], 'retained')
        with self.assertRaises(ValueError):
            core.request('/chat/completions', {})
        self.assertEqual(list(core.chat([{'role': 'user', 'content': 'hello'}])), ['Hello 世界'])
        thread = next(r for r in self.requests() if r.get('method') == 'thread/start')
        self.assertEqual(thread['params']['dynamicTools'], [])
        self.assertEqual(thread['params']['baseInstructions'], agent.POLICY)

    def test_all_codex_hardening_flags_and_environment_are_applied(self):
        list(subscription.chat([{'role': 'user', 'content': 'hello'}]))
        args = self.popen_args[-1]
        self.assertEqual(args[:2], ['codex', 'app-server'])
        self.assertEqual(args[2::2], ['-c'] * ((len(args) - 2) // 2))
        settings = dict(item.split('=', 1) for item in args[3::2])
        self.assertEqual(settings['forced_login_method'], '"chatgpt"')
        self.assertEqual(settings['cli_auth_credentials_store'], '"file"')
        self.assertEqual(settings['model_provider'], '"openai"')
        self.assertEqual(settings['mcp_servers'], '{}')
        self.assertEqual(settings['web_search'], '"disabled"')
        self.assertEqual(settings['check_for_update_on_startup'], 'false')
        self.assertEqual(settings['project_doc_max_bytes'], '0')
        self.assertEqual(settings['analytics.enabled'], 'false')
        self.assertEqual(settings['tools.update_plan.enabled'], 'false')
        self.assertEqual(settings['tools.experimental_request_user_input.enabled'], 'false')
        self.assertEqual(settings['skip_host_skill_discovery'], 'true')
        for name in subscription.DISABLED_FEATURES:
            self.assertEqual(settings[f'features.{name}'], 'false')
        for name in (
                'shell_tool', 'unified_exec', 'apps', 'plugins', 'browser_use',
                'computer_use', 'multi_agent', 'request_permissions_tool',
                'request_permissions'):
            self.assertEqual(settings[f'features.{name}'], 'false')
        self.assertFalse(any('execution_environment' in item for item in settings))
        env = self.popen_kwargs[-1]['env']
        self.assertFalse(any(name.startswith('OPENAI_') for name in env))
        self.assertEqual([name for name in env if name.startswith('CODEX_')], ['CODEX_HOME'])
        self.assertNotEqual(env['CODEX_HOME'], 'must-not-inherit')
        if os.name == 'posix':
            self.assertTrue(self.popen_kwargs[-1]['start_new_session'])
            self.assertTrue(callable(self.popen_kwargs[-1]['preexec_fn']))
        thread = next(r for r in self.requests() if r.get('method') == 'thread/start')
        self.assertEqual(thread['params']['environments'], [])

    def test_outbound_json_is_compatible_bounded_and_safely_reported(self):
        with subscription.Server() as server:
            self.assertEqual(self.popen_kwargs[-1]['bufsize'], 0)
            if os.name == 'posix':
                self.assertFalse(os.get_blocking(server.process.stdin.fileno()))
            for value in ({'bad': {1}}, {'large': 'x' * (4 * 1024 * 1024)}):
                with self.subTest(value=next(iter(value))):
                    with self.assertRaisesRegex(RuntimeError, 'could not send'):
                        server.send(value)

    def test_reader_queue_saturation_sets_fatal_state_and_wakes_receive(self):
        server = subscription.Server()
        server.events = queue.Queue(maxsize=1)
        server.process = Mock()
        server.process.stdout = io.BytesIO(b'{"event":1}\n{"event":2}\n')
        server._read()
        self.assertTrue(server._reader_fatal.is_set())
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'connection closed'):
            server.receive(timeout=10)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_teardown_preserves_original_error_and_cleans_after_broken_pipe(self):
        server = subscription.Server()
        server.work = tempfile.TemporaryDirectory(prefix='aios-chatgpt-test-')
        work = Path(server.work.name)
        server.process = Mock()
        server.process.poll.return_value = 0
        server.process.stdin.close.side_effect = BrokenPipeError('closed')
        server.process.stdout.close.side_effect = OSError('closed')
        server._reader_thread = Mock()

        def fail():
            try:
                raise RuntimeError('curated ChatGPT failure')
            except RuntimeError:
                server.__exit__(*sys.exc_info())
                raise

        with self.assertRaisesRegex(RuntimeError, 'curated ChatGPT failure'):
            fail()
        self.assertFalse(work.exists())
        server.__exit__(None, None, None)

    def test_teardown_skips_stdout_close_while_reader_still_alive(self):
        server = subscription.Server()
        process = Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.stdin = Mock()
        stdout = Mock()
        process.stdout = stdout
        server.process = process
        server._process_group = 2468
        server._reader_thread = Mock()
        server._reader_thread.is_alive.return_value = True

        with (patch.object(server, '_signal_group', return_value=False),
              patch.object(server, '_group_exists', return_value=False)):
            finished, outcome, elapsed = self.bounded(server.__exit__, timeout=0.5)

        self.assertTrue(finished, f'__exit__ blocked for {elapsed:.3f}s')
        self.assertTrue(outcome[0])
        process.stdin.close.assert_called_once_with()
        stdout.close.assert_not_called()

    def test_partial_startup_failure_cleans_work_directory(self):
        work = []

        def fail(_args, **kwargs):
            work.append(Path(kwargs['cwd']))
            raise OSError('missing runtime')

        with (patch.object(subscription.subprocess, 'Popen', side_effect=fail),
              self.assertRaisesRegex(RuntimeError, 'runtime could not start')):
            with subscription.Server():
                self.fail('Server must not start')
        self.assertEqual(len(work), 1)
        self.assertFalse(work[0].exists())

    def test_blocked_tool_response_obeys_turn_deadline_and_reaps_everything(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'blocked-tool-response'
        session = Mock(verify_application_completion=False)
        session.codex_tools.return_value = [{
            'type': 'function', 'name': 'application', 'description': 'Applications',
            'inputSchema': {'type': 'object'},
        }]
        session.system_prompt.return_value = 'AIOS policy'
        session.progress.return_value = 'Application: search'
        session.dispatch.return_value = {
            'blob': 'x' * (subscription.MAX_TOOL_RESULT_BYTES - 100)}
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'timed out|connection closed'):
            list(subscription.chat(
                [{'role': 'user', 'content': 'hello'}],
                session=session, turn_timeout=0.75))
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(self.processes[-1].poll())
        self.assertFalse(Path(self.popen_kwargs[-1]['cwd']).exists())
        with subscription.account_lock(exclusive=True):
            pass

    def test_account_sanitizes_and_paginates(self):
        events = []
        subscription.account_action('status', lambda kind, **data: events.append(data))
        account = events[0]['account']
        self.assertEqual([m['id'] for m in account['models']], ['test-one', 'test-two'])
        self.assertNotIn('secret', json.dumps(events))
        self.assertEqual(account['limits']['primary']['usedPercent'], 25)
        self.assertEqual(subscription.private_home().stat().st_mode & 0o777, 0o700)

    def test_device_login_buffers_early_completion(self):
        events = []
        subscription.account_action('login', lambda kind, **data: events.append((kind, data)), True)
        self.assertEqual(events[0][1]['code'], 'TEST-CODE')
        self.assertTrue(events[-1][1]['account']['signed_in'])
        request = next(r for r in self.requests() if r.get('method') == 'account/login/start')
        self.assertEqual(request['params']['type'], 'chatgptDeviceCode')

    def test_login_cancelled_on_interruption(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'wait-login'
        def stop(*args, **kwargs):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            subscription.account_action('login', stop)
        self.assertIn('account/login/cancel', [r.get('method') for r in self.requests()])

    def test_logout(self):
        subscription.account_action('logout', lambda *args, **kwargs: None)
        self.assertIn('account/logout', [r.get('method') for r in self.requests()])

    def test_signed_out_chat_never_starts_turn(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'signed-out'
        with self.assertRaisesRegex(RuntimeError, 'Sign in'):
            list(subscription.chat([{'role': 'user', 'content': 'hello'}]))
        self.assertNotIn('turn/start', [r.get('method') for r in self.requests()])

    def test_chat_history_preserves_roles_and_does_not_mix_windows(self):
        first = [{'role': 'user', 'content': 'First'}, {'role': 'assistant', 'content': 'Reply'},
                 {'role': 'user', 'content': 'Continue'}]
        self.assertEqual(list(subscription.chat(first)), [{'type': 'token', 'text': 'Hello 世界'}])
        list(subscription.chat([{'role': 'user', 'content': 'Second window'}]))
        requests = self.requests()
        injected = [r for r in requests if r.get('method') == 'thread/inject_items']
        self.assertEqual(len(injected), 1)
        self.assertEqual([m['role'] for m in injected[0]['params']['items']], ['user', 'assistant'])
        turns = [r for r in requests if r.get('method') == 'turn/start']
        self.assertEqual(turns[-1]['params']['input'][0]['text'], 'Second window')

    @patch('aios.agent.toolhost.list_tools', return_value={'tools': [clone(APPLICATION_TOOL)], 'warnings': []})
    def test_application_completion_buffers_unverified_text_and_recovers_once(self, _tools):
        skill = Skill(name='application-builder', description='Build apps.', instructions='Create then launch.',
                      allowed_tools=('application',), triggers=('create a calculator',))
        for scenario in ('application-no-tools', 'application-recovery'):
            with self.subTest(scenario=scenario):
                os.environ['AIOS_FAKE_SCENARIO'] = scenario
                session = agent.AgentSession(
                    [{'role': 'user', 'content': 'create a calculator application'}], 'tools.sock', catalog=[skill])
                emitted = []
                before = len(self.requests()) if self.log.exists() else 0
                with patch('aios.agent.toolhost.call', return_value={'id': 'calculator-12345678', 'launched': True}) as call:
                    if scenario == 'application-no-tools':
                        with self.assertRaisesRegex(RuntimeError, 'not launched'):
                            emitted.extend(subscription.chat(session.messages, session=session))
                        self.assertEqual(emitted, [])
                        call.assert_not_called()
                    else:
                        emitted.extend(subscription.chat(session.messages, session=session))
                        self.assertEqual([e['text'] for e in emitted if e['type'] == 'token'], ['Calculator is open.'])
                        call.assert_called_once()
                turns = [r for r in self.requests()[before:] if r.get('method') == 'turn/start']
                self.assertEqual(len(turns), 2)
                self.assertEqual(turns[1]['params']['input'][0]['text'], agent.APPLICATION_LAUNCH_CONTINUATION)

    @patch('aios.agent.toolhost.list_tools', return_value={'tools': [clone(APPLICATION_TOOL)], 'warnings': []})
    def test_application_completion_rejects_failed_launch_and_honors_draft_only(self, _tools):
        skill = Skill(name='application-builder', description='Build apps.', instructions='Create then launch.',
                      allowed_tools=('application',), triggers=('create a calculator',))
        os.environ['AIOS_FAKE_SCENARIO'] = 'application-recovery'
        session = agent.AgentSession(
            [{'role': 'user', 'content': 'create a calculator application'}], 'tools.sock', catalog=[skill])
        emitted = []
        with patch('aios.agent.toolhost.call', return_value={
                'id': 'calculator-12345678', 'launched': False, 'reason': 'Application window could not open.'}), \
                self.assertRaisesRegex(RuntimeError, 'could not open'):
            emitted.extend(subscription.chat(session.messages, session=session))
        self.assertFalse(any(e['type'] == 'token' for e in emitted))
        os.environ['AIOS_FAKE_SCENARIO'] = ''
        draft = agent.AgentSession(
            [{'role': 'user', 'content': 'create a calculator application, draft only'}], 'tools.sock', catalog=[skill])
        self.assertEqual(list(subscription.chat(draft.messages, session=draft)), [{'type': 'token', 'text': 'Hello 世界'}])

    @patch('aios.agent.toolhost.list_tools')
    def test_shared_agent_session_tools_prompt_and_application_dispatch(self, list_tools):
        list_tools.return_value = {'tools': [clone(APPLICATION_TOOL)], 'warnings': []}
        os.environ['AIOS_FAKE_SCENARIO'] = 'application'
        session = agent.AgentSession(
            [{'role': 'user', 'content': 'I need a calculator'}], 'tools.sock', catalog=[])
        with patch('aios.agent.toolhost.call', return_value={'matches': []}) as call:
            events = list(subscription.chat(session.messages, session=session))
        call.assert_called_once()
        self.assertEqual(call.call_args.args[:3], (
            'tools.sock', 'application', {'action': 'search', 'query': 'calculator'}))
        self.assertGreater(call.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(call.call_args.kwargs['timeout'], toolhost.SOCKET_TIMEOUT)
        self.assertIn({'type': 'progress', 'text': 'Application: search'}, events)
        self.assertEqual(events[-1], {'type': 'token', 'text': 'Application done'})
        thread = next(r for r in self.requests() if r.get('method') == 'thread/start')
        self.assertEqual([tool['name'] for tool in thread['params']['dynamicTools']],
                         ['activate_skill', 'application'])
        self.assertIn(agent.POLICY, thread['params']['baseInstructions'])
        self.assertIn('next user turn', thread['params']['baseInstructions'])

    @patch('aios.agent.toolhost.list_tools')
    def test_mcp_error_result_sets_success_false_without_rewriting_result(self, list_tools):
        mcp_tool = {
            'type': 'function',
            'function': {
                'name': 'mcp_fixture_echo',
                'description': 'Echo through MCP.',
                'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}},
            },
        }
        list_tools.return_value = {'tools': [mcp_tool], 'warnings': []}
        os.environ['AIOS_FAKE_SCENARIO'] = 'mcp-error'
        session = agent.AgentSession([{'role': 'user', 'content': 'fail'}], 'tools.sock', catalog=[])
        result = {'text': 'failed', 'structured': {'why': 'nope'}, 'is_error': True}
        with patch('aios.agent.toolhost.call', return_value=result):
            events = list(subscription.chat(session.messages, session=session))
        self.assertEqual(events[-1], {'type': 'token', 'text': 'MCP recovered'})
        response = next(r for r in self.requests() if r.get('id') == 'tool-request')
        self.assertFalse(response['result']['success'])
        self.assertEqual(
            json.loads(response['result']['contentItems'][0]['text']),
            result,
        )

    @patch('aios.agent.toolhost.list_tools', return_value={
        'tools': [clone(APPLICATION_TOOL)], 'warnings': []})
    def test_unknown_generic_tool_returns_error_and_model_recovers(self, _list_tools):
        os.environ['AIOS_FAKE_SCENARIO'] = 'unknown-generic'
        session = agent.AgentSession([{'role': 'user', 'content': 'hello'}], 'tools.sock', catalog=[])
        with patch('aios.agent.toolhost.call') as call:
            events = list(subscription.chat(session.messages, session=session))
        call.assert_not_called()
        self.assertEqual(events[-1], {'type': 'token', 'text': 'Unknown tool recovered'})
        response = next(r for r in self.requests() if r.get('id') == 'tool-request')
        self.assertFalse(response['result']['success'])
        error = json.loads(response['result']['contentItems'][0]['text'])['error']
        self.assertEqual(error, 'The model requested an unavailable tool.')

    def test_generic_dispatch_errors_and_invalid_results_are_bounded_and_redacted(self):
        session = Mock(verify_application_completion=False)
        session.codex_tools.return_value = [{
            'type': 'function', 'name': 'application', 'description': 'Applications',
            'inputSchema': {'type': 'object'},
        }]
        session.system_prompt.return_value = 'AIOS policy'
        session.progress.return_value = 'Application: search'
        for result in (
            OSError('/home/user/secret.sock token=abc'),
            RuntimeError('secret-provider-token'),
            {'invalid': {1, 2, 3}},
            {'blob': 'x' * (subscription.MAX_TOOL_RESULT_BYTES + 1)},
        ):
            with self.subTest(result=type(result).__name__):
                os.environ['AIOS_FAKE_SCENARIO'] = 'generic-error'
                session.dispatch.side_effect = result if isinstance(result, Exception) else None
                session.dispatch.return_value = None if isinstance(result, Exception) else result
                events = list(subscription.chat(
                    [{'role': 'user', 'content': 'hello'}], session=session))
                self.assertEqual(events[-1]['text'], 'Generic error recovered')
                response = [r for r in self.requests() if r.get('id') == 'tool-request'][-1]
                self.assertFalse(response['result']['success'])
                text = response['result']['contentItems'][0]['text']
                self.assertEqual(
                    json.loads(text),
                    {'error': 'Tool action failed. Review the request and try again.'},
                )
                self.assertNotIn('secret', text)
                session.dispatch.reset_mock(side_effect=True, return_value=True)

    def test_null_error_field_does_not_mark_tool_result_failed(self):
        self.assertTrue(subscription._chat_result(
            {'value': 1, 'error': None}, 'fallback')['success'])

    def test_activation_note_cannot_push_system_prompt_over_limit(self):
        session = Mock(verify_application_completion=False)
        session.codex_tools.return_value = []
        session.system_prompt.return_value = 'x' * agent.MAX_SYSTEM_PROMPT_BYTES
        with self.assertRaisesRegex(RuntimeError, 'prompt is too large'):
            list(subscription.chat(
                [{'role': 'user', 'content': 'hello'}], session=session))
        self.assertNotIn('thread/start', [r.get('method') for r in self.requests()])
        self.assertFalse(Path(self.popen_kwargs[-1]['cwd']).exists())

    @patch('aios.agent.toolhost.list_tools')
    def test_same_turn_activation_narrows_permissions_without_rewriting_prompt(self, list_tools):
        list_tools.return_value = {
            'tools': [clone(BROWSER_TOOL), clone(APPLICATION_TOOL)], 'warnings': []}
        catalog = [Skill(
            name='application-reader',
            description='Build applications.',
            instructions='Use applications only. SECRET-INSTRUCTION-MARKER',
            allowed_tools=('application',),
            triggers=(),
        )]
        os.environ['AIOS_FAKE_SCENARIO'] = 'activate-narrow'
        session = agent.AgentSession([{'role': 'user', 'content': 'hello'}], 'tools.sock', catalog=catalog)
        with patch('aios.agent.toolhost.call') as call:
            events = list(subscription.chat(session.messages, session=session))
        call.assert_not_called()
        self.assertEqual(events[-1], {'type': 'token', 'text': 'Activation narrowed tools'})
        responses = [r for r in self.requests() if str(r.get('id', '')).startswith('tool-request')]
        self.assertTrue(responses[0]['result']['success'])
        self.assertFalse(responses[1]['result']['success'])
        self.assertEqual(
            json.loads(responses[1]['result']['contentItems'][0]['text'])['error'],
            'The model requested an unavailable tool.',
        )
        self.assertNotIn('SECRET-INSTRUCTION-MARKER',
                         responses[0]['result']['contentItems'][0]['text'])
        thread = next(r for r in self.requests() if r.get('method') == 'thread/start')
        self.assertNotIn('SECRET-INSTRUCTION-MARKER', thread['params']['baseInstructions'])
        self.assertIn('next user turn', thread['params']['baseInstructions'])

    def test_malformed_tool_calls_and_argument_bounds_fail_the_turn(self):
        session = Mock(verify_application_completion=False)
        session.codex_tools.return_value = [{
            'type': 'function', 'name': 'application', 'description': 'Applications',
            'inputSchema': {'type': 'object'},
        }]
        session.system_prompt.return_value = 'AIOS policy'
        for scenario in (
                'malformed-args', 'malformed-tool', 'malformed-params',
                'malformed-method', 'oversized-args'):
            with self.subTest(scenario=scenario):
                os.environ['AIOS_FAKE_SCENARIO'] = scenario
                with self.assertRaises(RuntimeError):
                    list(subscription.chat([{'role': 'user', 'content': 'hello'}], session=session))
        session.dispatch.assert_not_called()

    def test_tool_call_limit_fails_before_thirty_third_dispatch(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'call-limit'
        session = Mock(verify_application_completion=False)
        session.codex_tools.return_value = [{
            'type': 'function', 'name': 'application', 'description': 'Applications',
            'inputSchema': {'type': 'object'},
        }]
        session.system_prompt.return_value = 'AIOS policy'
        session.progress.return_value = 'Application: search'
        session.dispatch.return_value = {'matches': []}
        with self.assertRaisesRegex(RuntimeError, 'action limit'):
            list(subscription.chat([{'role': 'user', 'content': 'hello'}], session=session))
        self.assertEqual(session.dispatch.call_count, 32)

    def test_delta_content_and_deadline_bounds(self):
        session = Mock(verify_application_completion=False)
        session.codex_tools.return_value = []
        session.system_prompt.return_value = 'AIOS policy'
        for scenario in ('oversized-delta', 'non-string-delta', 'malformed-completed'):
            with self.subTest(scenario=scenario):
                os.environ['AIOS_FAKE_SCENARIO'] = scenario
                with self.assertRaises(RuntimeError):
                    list(subscription.chat([{'role': 'user', 'content': 'hello'}], session=session))

        os.environ['AIOS_FAKE_SCENARIO'] = ''
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            list(subscription.chat(
                [{'role': 'user', 'content': 'hello'}],
                session=session,
                turn_timeout=1,
                clock=iter((0, 2)).__next__,
            ))

    def test_cross_thread_tool_request_is_rejected_without_dispatch(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'cross-thread-call'
        session = Mock(verify_application_completion=False)
        session.codex_tools.return_value = [{
            'type': 'function', 'name': 'application', 'description': 'Applications',
            'inputSchema': {'type': 'object'},
        }]
        session.system_prompt.return_value = 'AIOS policy'
        self.assertEqual(
            list(subscription.chat([{'role': 'user', 'content': 'hello'}], session=session)),
            [{'type': 'token', 'text': 'Cross-thread ignored'}],
        )
        session.dispatch.assert_not_called()
        response = next(r for r in self.requests() if r.get('id') == 'tool-request')
        self.assertEqual(response['error']['code'], -32601)

    def test_malformed_thread_and_turn_start_results_are_sanitized(self):
        for scenario in ('malformed-thread', 'malformed-turn'):
            with self.subTest(scenario=scenario):
                os.environ['AIOS_FAKE_SCENARIO'] = scenario
                with self.assertRaises(RuntimeError) as caught:
                    list(subscription.chat([{'role': 'user', 'content': 'hello'}]))
                self.assertNotIn('bad', str(caught.exception))

    def test_unexpected_server_request_is_rejected_without_leaking_or_failing(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'server-request'
        self.assertEqual(
            list(subscription.chat([{'role': 'user', 'content': 'hello'}])),
            [{'type': 'token', 'text': 'Hello 世界'}],
        )
        response = next(r for r in self.requests() if r.get('id') == 'server-request')
        self.assertEqual(response['error']['code'], -32601)

    def test_failed_or_disconnected_turn_is_not_success(self):
        for scenario in ('failed-turn', 'disconnect'):
            os.environ['AIOS_FAKE_SCENARIO'] = scenario
            with self.assertRaises(RuntimeError):
                list(subscription.chat([{'role': 'user', 'content': 'hello'}]))

    def test_provider_error_does_not_leak_diagnostics(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'rpc-error'
        with self.assertRaises(RuntimeError) as caught:
            subscription.account_action('status', lambda *args, **kwargs: None)
        self.assertNotIn('secret', str(caught.exception))

    def test_stop_stream_terminates_server_and_releases_account(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'wait-turn'
        stream = subscription.chat([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(next(stream)['text'], 'Hello 世界')
        self.assertIsNone(self.processes[-1].poll())
        stream.close()
        self.assertIsNotNone(self.processes[-1].poll())
        with subscription.account_lock(exclusive=True):
            pass

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups required')
    def test_stop_stream_kills_process_group_holding_stdout_and_releases_account(self):
        grandchild_path = Path(self.temp.name) / 'grandchild.pid'
        os.environ['AIOS_FAKE_SCENARIO'] = 'grandchild-holds-stdout'
        os.environ['AIOS_FAKE_GRANDCHILD_PID'] = str(grandchild_path)
        stream = subscription.chat([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(next(stream), {'type': 'token', 'text': 'Hello 世界'})
        process = self.processes[-1]
        process_group = os.getpgid(process.pid)
        deadline = time.monotonic() + 1
        while not grandchild_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(grandchild_path.exists())
        grandchild_pid = int(grandchild_path.read_text(encoding='utf-8'))
        os.kill(grandchild_pid, 0)

        finished, outcome, elapsed = self.bounded(
            stream.close,
            timeout=0.75,
            cleanup=lambda: self.kill_process_group(process, process_group),
        )
        self.assertTrue(finished, f'stream.close blocked for {elapsed:.3f}s')
        self.assertTrue(outcome[0])
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(grandchild_pid, 0)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process_group, 0)
        self.assertFalse(Path(self.popen_kwargs[-1]['cwd']).exists())
        with subscription.account_lock(exclusive=True):
            pass

    def test_account_mutation_is_locked_during_chat(self):
        with subscription.account_lock(), self.assertRaisesRegex(RuntimeError, 'Finish'):
            with subscription.account_lock(exclusive=True):
                self.fail('Lock must not be granted')

    def test_login_urls(self):
        for url in ('http://auth.openai.com', 'https://auth.openai.com.evil.test',
                    'https://password@auth.openai.com', 'https://auth.openai.com:8443'):
            with self.assertRaises(RuntimeError):
                subscription.safe_login_url(url)


@unittest.skipUnless(os.environ.get('AIOS_CODEX_SMOKE') == '1', 'optional real Codex runtime check')
class RealRuntimeTests(unittest.TestCase):
    def test_signed_out_runtime_and_thread_schema(self):
        definitions = {'tools': [clone(BROWSER_TOOL), clone(APPLICATION_TOOL)], 'warnings': []}
        with (tempfile.TemporaryDirectory() as temp,
              patch.dict(os.environ, {'XDG_CONFIG_HOME': temp}),
              patch('aios.agent.toolhost.list_tools', return_value=definitions)):
            session = agent.AgentSession(
                [{'role': 'user', 'content': 'hello'}], 'signed-out.sock', catalog=[])
            dynamic_tools = session.codex_tools()
            self.assertIn('browser', [tool['name'] for tool in dynamic_tools])
            self.assertIn('application', [tool['name'] for tool in dynamic_tools])
            with subscription.Server() as server:
                self.assertIsNone(server.request('account/read').get('account'))
                thread = server.request('thread/start', {
                    'ephemeral': True, 'environments': [], 'sandbox': 'read-only',
                    'approvalPolicy': 'never', 'baseInstructions': 'You are AIOS.',
                    'dynamicTools': dynamic_tools,
                })['thread']['id']
                server.request('thread/inject_items', {'threadId': thread, 'items': [
                    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]},
                    {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hi'}]},
                ]})
