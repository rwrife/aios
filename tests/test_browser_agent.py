import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aios import agent
from aios.browser import Browser, discover, web_url


def stream(delta, finish='stop'):
    events = [{'choices': [{'index': 0, 'delta': value}]} for value in delta]
    events.append({'choices': [{'delta': {}, 'finish_reason': finish}]})
    return io.BytesIO((''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n').encode())


class BrowserAgentTests(unittest.TestCase):
    def test_web_urls_and_lazy_start(self):
        for url in ('file:///etc/passwd', 'javascript:alert(1)', 'https://user:password@example.com', '--no-sandbox', None):
            with self.assertRaises(ValueError): web_url(url)
        self.assertEqual(web_url('http://localhost:8000/?q=test#section'), 'http://localhost:8000/?q=test#section')
        browser = Browser()
        with self.assertRaises(ValueError): browser.act({'action': 'snapshot'})
        self.assertIsNone(browser.process)
        self.assertEqual(browser.act({'action': 'close'}), {'closed': True})

    def test_local_browser_discovery_requires_live_registration(self):
        with tempfile.TemporaryDirectory() as runtime:
            directory = Path(runtime) / 'aios' / 'browsers'
            directory.mkdir(parents=True)
            socket_path = Path(runtime) / 'browser.sock'
            socket_path.touch()
            private = directory / 'private.json'
            private.write_text(json.dumps({'version': 1, 'session': 'private',
                'socket': str(socket_path), 'pid': os.getpid(),
                'actions': ['snapshot']}))
            os.chmod(private, 0o600)
            self.assertEqual([entry['session'] for entry in discover(runtime)], ['private'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes are required')
    def test_local_browser_discovery_rejects_public_registration(self):
        with tempfile.TemporaryDirectory() as runtime:
            directory = Path(runtime) / 'aios' / 'browsers'
            directory.mkdir(parents=True)
            socket_path = Path(runtime) / 'browser.sock'
            socket_path.touch()
            public = directory / 'public.json'
            public.write_text(json.dumps({'version': 1, 'session': 'public',
                'socket': str(socket_path), 'pid': os.getpid(),
                'actions': ['snapshot']}))
            os.chmod(public, 0o644)
            self.assertEqual(discover(runtime), [])

    @patch('aios.agent.core.load_config', return_value={'mode': 'remote', 'model': 'test'})
    def test_fragmented_tool_call_then_grounded_response(self, _):
        bodies = []
        def request(route, body):
            bodies.append(json.loads(json.dumps(body)))
            if len(bodies) == 1:
                return stream([
                    {'tool_calls': [{'index': 0, 'id': 'call_1', 'function': {'name': 'browser', 'arguments': '{"action":"op'}}]},
                    {'tool_calls': [{'index': 0, 'function': {'arguments': 'en","url":"https://example.com"}'}}]}], 'tool_calls')
            return stream([{'content': 'The page is open.'}])
        with patch('aios.agent.core.request', side_effect=request), patch('aios.agent.call', return_value={'title': 'Example', 'untrusted_page_content': True}) as call:
            events = list(agent.chat([{'role': 'user', 'content': 'Open example.com'}], '/private/socket'))
        call.assert_called_once_with('/private/socket', {'action': 'open', 'url': 'https://example.com'})
        self.assertEqual(bodies[1]['messages'][-1]['role'], 'tool')
        self.assertEqual(bodies[1]['messages'][-1]['tool_call_id'], 'call_1')
        self.assertIn('untrusted', bodies[0]['messages'][0]['content'])
        self.assertEqual(events[-1]['text'], 'The page is open.')

    @patch('aios.agent.core.load_config', return_value={'mode': 'local'})
    def test_incomplete_call_never_executes(self, _):
        with patch('aios.agent.core.request', return_value=stream([{'tool_calls': [{'index': 0, 'id': 'x', 'function': {'name': 'browser', 'arguments': '{}'}}]}], 'length')), patch('aios.agent.call') as call:
            with self.assertRaises(RuntimeError): list(agent.chat([{'role': 'user', 'content': 'Hi'}], '/socket'))
            call.assert_not_called()

    @patch('aios.agent.core.load_config', return_value={'mode': 'local'})
    def test_plain_text_is_not_executed(self, _):
        with patch('aios.agent.core.request', return_value=stream([{'content': '{"action":"open","url":"https://example.com"}'}])), patch('aios.agent.call') as call:
            list(agent.chat([{'role': 'user', 'content': 'Hi'}], '/socket'))
            call.assert_not_called()

    @patch('aios.agent.core.load_config', return_value={'mode': 'local'})
    def test_action_loop_is_bounded(self, _):
        def reply(*_):
            return stream([{'tool_calls': [{'index': 0, 'id': 'x', 'function': {'name': 'browser', 'arguments': '{"action":"snapshot"}'}}]}], 'tool_calls')
        with patch('aios.agent.core.request', side_effect=reply), patch('aios.agent.call', return_value={'text': 'page'}) as call:
            with self.assertRaisesRegex(RuntimeError, 'limit'): list(agent.chat([{'role': 'user', 'content': 'Browse'}], '/socket'))
            self.assertEqual(call.call_count, 8)
