import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
            'CODEX_API_KEY': 'must-not-inherit',
        })
        self.env.start()
        original = subprocess.Popen
        fixture = str(Path(__file__).parent / 'fixtures/codex_server.py')
        self.processes = []
        def spawn(args, **kwargs):
            process = original([sys.executable, fixture], **kwargs)
            self.processes.append(process)
            return process
        self.spawn = patch.object(subscription.subprocess, 'Popen', side_effect=spawn)
        self.spawn.start()

    def tearDown(self):
        self.spawn.stop()
        self.env.stop()
        self.temp.cleanup()

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

    def test_browser_bridge(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'browser'
        with patch('aios.browser.call', return_value={'text': 'page'}) as call:
            events = list(subscription.chat([{'role': 'user', 'content': 'browse'}], 'private.sock'))
        call.assert_called_once_with('private.sock', {'action': 'snapshot'})
        self.assertEqual(events[-1]['text'], 'Browser done')
        response = next(r for r in self.requests() if r.get('id') == 'tool-request')
        self.assertTrue(response['result']['success'])

    def test_legacy_browser_validation_and_safe_error_are_preserved(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'unknown-generic'
        with patch('aios.browser.call') as call, self.assertRaisesRegex(RuntimeError, 'unsupported browser'):
            list(subscription.chat([{'role': 'user', 'content': 'browse'}], 'private.sock'))
        call.assert_not_called()

        os.environ['AIOS_FAKE_SCENARIO'] = 'browser'
        with patch('aios.browser.call', side_effect=OSError('/secret/browser.sock token=abc')):
            events = list(subscription.chat([{'role': 'user', 'content': 'browse'}], 'private.sock'))
        self.assertEqual(events[-1]['text'], 'Browser done')
        response = [r for r in self.requests() if r.get('id') == 'tool-request'][-1]
        self.assertFalse(response['result']['success'])
        text = response['result']['contentItems'][0]['text']
        self.assertIn('Browser action failed', text)
        self.assertNotIn('secret', text)

    def test_rejects_browser_socket_and_shared_session_together(self):
        with self.assertRaisesRegex(ValueError, 'both'):
            list(subscription.chat([{'role': 'user', 'content': 'hello'}], 'socket', session=Mock()))
        self.assertEqual(self.processes, [])

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
        self.assertEqual(error, 'Tool action failed. Review the request and try again.')

    def test_generic_dispatch_errors_and_invalid_results_are_bounded_and_redacted(self):
        session = Mock()
        session.codex_tools.return_value = [{
            'type': 'function', 'name': 'application', 'description': 'Applications',
            'inputSchema': {'type': 'object'},
        }]
        session.system_prompt.return_value = 'AIOS policy'
        session.progress.return_value = 'Application: search'
        for result in (
            OSError('/home/user/secret.sock token=abc'),
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

    @patch('aios.agent.toolhost.list_tools')
    def test_same_turn_activation_narrows_permissions_without_rewriting_prompt(self, list_tools):
        list_tools.return_value = {
            'tools': [clone(BROWSER_TOOL), clone(APPLICATION_TOOL)], 'warnings': []}
        catalog = [Skill(
            name='application-builder',
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
        self.assertNotIn('SECRET-INSTRUCTION-MARKER',
                         responses[0]['result']['contentItems'][0]['text'])
        thread = next(r for r in self.requests() if r.get('method') == 'thread/start')
        self.assertNotIn('SECRET-INSTRUCTION-MARKER', thread['params']['baseInstructions'])
        self.assertIn('next user turn', thread['params']['baseInstructions'])

    def test_malformed_tool_calls_and_argument_bounds_fail_the_turn(self):
        session = Mock()
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
        session = Mock()
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
        session = Mock()
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
        session = Mock()
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
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'XDG_CONFIG_HOME': temp}):
            with subscription.Server() as server:
                self.assertIsNone(server.request('account/read').get('account'))
                thread = server.request('thread/start', {
                    'ephemeral': True, 'environments': [], 'sandbox': 'read-only',
                    'approvalPolicy': 'never', 'baseInstructions': 'You are AIOS.',
                    'dynamicTools': [{
                        'type': 'function',
                        'name': 'browser',
                        'description': 'AIOS browser',
                        'inputSchema': {
                            'type': 'object',
                            'properties': {'action': {'type': 'string'}},
                            'required': ['action'],
                            'additionalProperties': False,
                        },
                    }],
                })['thread']['id']
                server.request('thread/inject_items', {'threadId': thread, 'items': [
                    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]},
                    {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hi'}]},
                ]})
