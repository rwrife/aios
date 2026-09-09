import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aios import core, subscription


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

    def test_unsupported_tool_never_executes(self):
        os.environ['AIOS_FAKE_SCENARIO'] = 'unknown-tool'
        with patch('aios.browser.call') as call, self.assertRaisesRegex(RuntimeError, 'unsupported'):
            list(subscription.chat([{'role': 'user', 'content': 'hello'}], 'socket'))
        call.assert_not_called()

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
                    'dynamicTools': [{'type': 'function', 'name': 'browser',
                                      'description': 'AIOS browser', 'inputSchema': {'type': 'object'}}],
                })['thread']['id']
                server.request('thread/inject_items', {'threadId': thread, 'items': [
                    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]},
                    {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'hi'}]},
                ]})
