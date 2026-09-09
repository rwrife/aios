"""ChatGPT subscription adapter for the pinned Codex App Server (stdio).

Each operation owns its server. Chat history is seeded into an ephemeral thread,
so there are no durable Codex conversations or shared thread IDs to mix up.
Only managed ChatGPT authentication is used; API keys are never forwarded.
"""
import ctypes
import fcntl
import json
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from urllib.parse import urlsplit

from . import core

DISABLED_FEATURES = (
    'shell_tool', 'unified_exec', 'apply_patch_freeform', 'apps', 'plugins',
    'browser_use', 'computer_use', 'multi_agent', 'collab', 'js_repl',
    'code_mode', 'code_mode_host', 'image_generation', 'imagegenext',
    'memory_tool', 'memories', 'hooks', 'codex_hooks', 'plugin_hooks',
    'request_permissions_tool', 'request_permissions', 'view_image',
    'skill_search', 'tool_suggest', 'workspace_dependencies', 'deferred_executor',
    'goals', 'sleep_tool', 'token_budget', 'default_mode_request_user_input',
)


def private_home():
    path = core.config_dir() / 'codex'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path.resolve()


@contextmanager
def account_lock(exclusive=False):
    with (private_home() / 'account.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Finish the active ChatGPT chat or sign-in before changing accounts.') from None
        yield


def _parent_guard(parent):
    # The Qt worker can be killed on window close. Do not leave its server alive.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
        os._exit(1)


class Server:
    def __init__(self):
        self.process = None
        self.work = None
        self.events = queue.Queue(maxsize=1024)
        self.pending = deque()
        self.sequence = 0

    def __enter__(self):
        self.work = tempfile.TemporaryDirectory(prefix='aios-chatgpt-')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('CODEX_', 'OPENAI_'))}
        env['CODEX_HOME'] = str(private_home())
        args = ['codex', 'app-server']
        settings = {'model_provider': 'openai', 'forced_login_method': 'chatgpt',
                    'cli_auth_credentials_store': 'file', 'web_search': 'disabled',
                    'check_for_update_on_startup': False, 'project_doc_max_bytes': 0,
                    'mcp_servers': {}, 'analytics.enabled': False,
                    'tools.update_plan.enabled': False, 'tools.experimental_request_user_input.enabled': False}
        settings.update({'features.' + name: False for name in DISABLED_FEATURES})
        settings['features.skip_host_skill_discovery'] = True
        for key, value in settings.items():
            # JSON scalar values are also valid TOML values; an empty table is {}.
            args.extend(['-c', key + '=' + json.dumps(value)])
        parent = os.getpid()
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=env, cwd=self.work.name,
                                            preexec_fn=lambda: _parent_guard(parent))
            threading.Thread(target=self._read, daemon=True).start()
            self.request('initialize', {'clientInfo': {'name': 'aios', 'version': '1.0.0'},
                                        'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized', 'params': {}})
            return self
        except OSError:
            self.__exit__(None, None, None)
            raise RuntimeError('ChatGPT runtime could not start. Check that the AIOS Codex package is installed.') from None
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
        if self.work:
            self.work.cleanup()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(4 * 1024 * 1024 + 1)
                if not line or len(line) > 4 * 1024 * 1024:
                    break
                value = json.loads(line)
                if not isinstance(value, dict):
                    break
                self.events.put(value)
        except (ValueError, OSError):
            pass
        finally:
            self.events.put(None)

    def send(self, value):
        try:
            self.process.stdin.write((json.dumps(value) + '\n').encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise RuntimeError('ChatGPT connection closed. Try again.') from None

    def receive(self, timeout=90):
        if timeout <= 0:
            raise RuntimeError('ChatGPT operation timed out. Try again.')
        try:
            value = self.events.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError('ChatGPT did not respond in time. Try again.') from None
        if value is None:
            raise RuntimeError('ChatGPT connection closed. Try again.')
        return value

    def next_event(self, timeout=90):
        if timeout <= 0:
            raise RuntimeError('ChatGPT operation timed out. Try again.')
        return self.pending.popleft() if self.pending else self.receive(timeout)

    def request(self, method, params=None, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params or {}})
        deadline = time.monotonic() + timeout
        while True:
            value = self.receive(deadline - time.monotonic())
            if value.get('id') == request_id and 'method' not in value:
                if 'error' in value:
                    # Provider diagnostics can contain request data or credentials.
                    raise RuntimeError('ChatGPT could not complete this operation. Check your connection, account, and selected model.')
                return value.get('result', {})
            if 'id' in value and 'method' in value:
                self.reject(value)
            else:
                self.pending.append(value)
                if len(self.pending) > 1024:
                    raise RuntimeError('ChatGPT sent too many pending events.')

    def reject(self, value):
        self.send({'id': value['id'], 'error': {'code': -32601,
                   'message': 'This operation is not available in AIOS.'}})


def account_info(server):
    account = server.request('account/read', {'refreshToken': True}).get('account')
    if not account or account.get('type') != 'chatgpt':
        return {'signed_in': False, 'models': [], 'limits': None}
    models, cursor = [], None
    for _ in range(20):
        page = server.request('model/list', {'limit': 100, 'cursor': cursor})
        models.extend({'id': m['model'], 'name': m.get('displayName', m['model'])}
                      for m in page.get('data', []) if not m.get('hidden'))
        cursor = page.get('nextCursor')
        if not cursor:
            break
    try:
        limits = server.request('account/rateLimits/read').get('rateLimits')
    except RuntimeError:
        limits = None
    return {'signed_in': True, 'email': account.get('email', ''),
            'plan': account.get('planType', ''), 'models': models, 'limits': limits}


def safe_login_url(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in ('auth.openai.com', 'chatgpt.com')
            or parts.username or parts.password or parts.port not in (None, 443)):
        raise RuntimeError('ChatGPT returned an invalid sign-in address.')
    return url


def account_action(action, emit, device=False):
    with account_lock(action != 'status'), Server() as server:
        if action == 'login':
            result = server.request('account/login/start',
                                    {'type': 'chatgptDeviceCode' if device else 'chatgpt'})
            login_id = result['loginId']
            completed = False
            try:
                url = safe_login_url(result['verificationUrl'] if device else result['authUrl'])
                emit('subscription-login', url=url, code=result.get('userCode', ''))
                deadline = time.monotonic() + 600
                while True:
                    event = server.next_event(deadline - time.monotonic())
                    if event.get('method') == 'account/login/completed' and event.get('params', {}).get('loginId') == login_id:
                        if not event['params'].get('success'):
                            raise RuntimeError('ChatGPT sign-in was cancelled or failed. Try again.')
                        completed = True
                        break
                    if 'id' in event:
                        server.reject(event)
            finally:
                if not completed:
                    try:
                        server.request('account/login/cancel', {'loginId': login_id}, timeout=2)
                    except RuntimeError:
                        pass
        elif action == 'logout':
            server.request('account/logout')
        elif action != 'status':
            raise ValueError('Unknown ChatGPT account action.')
        emit('subscription-account', account=account_info(server))


def chat(messages, browser_socket=None):
    from .agent import POLICY, TOOL
    from .browser import ACTIONS, call
    if (not messages or messages[-1].get('role') != 'user' or
            any(m.get('role') not in ('user', 'assistant', 'system') or
                not isinstance(m.get('content'), str) for m in messages)):
        raise ValueError('Invalid conversation.')
    with account_lock(), Server() as server:
        account = server.request('account/read').get('account')
        if not account or account.get('type') != 'chatgpt':
            raise RuntimeError('Sign in with ChatGPT in AI models settings first.')
        config = core.load_config()
        model = config.get('subscription_model') or None
        function = TOOL['function']
        tools = [{'type': 'function', 'name': 'browser', 'description': function['description'],
                  'inputSchema': function['parameters']}] if browser_socket else []
        thread = server.request('thread/start', {
            'model': model, 'modelProvider': 'openai', 'ephemeral': True,
            'cwd': server.work.name, 'sandbox': 'read-only', 'approvalPolicy': 'never',
            'baseInstructions': POLICY, 'environments': [], 'dynamicTools': tools,
        })['thread']['id']
        if len(messages) > 1:
            history = [{'type': 'message', 'role': m['role'], 'content': [
                {'type': 'output_text' if m['role'] == 'assistant' else 'input_text',
                 'text': m['content']}]} for m in messages[:-1]]
            server.request('thread/inject_items', {'threadId': thread, 'items': history})
        server.request('turn/start', {'threadId': thread, 'input': [
            {'type': 'text', 'text': messages[-1]['content']}]})
        count = 0
        deadline = time.monotonic() + 1800
        while True:
            event = server.next_event(min(120, deadline - time.monotonic()))
            method, params = event.get('method'), event.get('params', {})
            if 'id' in event:
                if method != 'item/tool/call' or params.get('threadId') != thread:
                    server.reject(event)
                    continue
                args = params.get('arguments')
                count += 1
                if (params.get('tool') != 'browser' or not browser_socket or count > 32
                        or not isinstance(args, dict) or args.get('action') not in ACTIONS
                        or len(json.dumps(args)) > 24000):
                    raise RuntimeError('ChatGPT requested an unsupported browser action or reached the action limit.')
                yield {'type': 'progress', 'text': 'Browser · ' + args['action']}
                try:
                    result = call(browser_socket, args)
                except (ValueError, RuntimeError, OSError):
                    result = {'error': 'Browser action failed. Take a new snapshot before trying again.'}
                server.send({'id': event['id'], 'result': {'success': 'error' not in result,
                             'contentItems': [{'type': 'inputText', 'text': json.dumps(result)}]}})
            elif params.get('threadId') == thread:
                if method == 'item/agentMessage/delta':
                    yield {'type': 'token', 'text': params['delta']}
                elif method == 'turn/completed':
                    if params['turn']['status'] != 'completed':
                        raise RuntimeError('ChatGPT could not finish the reply. Check your subscription limits or try again.')
                    return
                elif method == 'error' and not params.get('willRetry'):
                    raise RuntimeError('ChatGPT could not finish the reply. Check your account and usage limits.')
