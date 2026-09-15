"""ChatGPT subscription adapter for the pinned Codex App Server (stdio).

Each operation owns its server. Chat history is seeded into an ephemeral thread,
so there are no durable Codex conversations or shared thread IDs to mix up.
Only managed ChatGPT authentication is used; API keys are never forwarded.
"""
import ctypes
import fcntl
import io
import json
import math
import os
import queue
import select
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from urllib.parse import urlsplit

from . import core
from .agent import MAX_AGENT_SECONDS

DISABLED_FEATURES = (
    'shell_tool', 'unified_exec', 'apply_patch_freeform', 'apps', 'plugins',
    'browser_use', 'computer_use', 'multi_agent', 'collab', 'js_repl',
    'code_mode', 'code_mode_host', 'image_generation', 'imagegenext',
    'memory_tool', 'memories', 'hooks', 'codex_hooks', 'plugin_hooks',
    'request_permissions_tool', 'request_permissions', 'view_image',
    'skill_search', 'tool_suggest', 'workspace_dependencies', 'deferred_executor',
    'goals', 'sleep_tool', 'token_budget', 'default_mode_request_user_input',
)
CHATGPT_ACTIVATION_NOTE = (
    "ChatGPT activation timing: activate_skill changes tool permissions immediately, "
    "but newly activated skill instructions take effect on the next user turn."
)
MAX_TOOL_CALLS = 32
MAX_ARGUMENT_BYTES = 24 * 1024
MAX_EVENT_WAIT = 120
MAX_TOOL_RESULT_BYTES = 64 * 1024
MAX_JSON_LINE_BYTES = 4 * 1024 * 1024
MAX_SEND_WAIT = 2
READER_JOIN_WAIT = 2
RECEIVE_POLL_WAIT = 0.05


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
        self._process_group = None
        self.work = None
        self.events = queue.Queue(maxsize=1024)
        self.pending = deque()
        self.sequence = 0
        self._send_lock = threading.Lock()
        self._exit_lock = threading.Lock()
        self._reader_thread = None
        self._reader_done = threading.Event()
        self._reader_fatal = threading.Event()
        self._closed = False

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
        settings['skip_host_skill_discovery'] = True
        for key, value in settings.items():
            # JSON scalar values are also valid TOML values; an empty table is {}.
            args.extend(['-c', key + '=' + json.dumps(value)])
        parent = os.getpid()
        try:
            options = {
                'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE,
                'stderr': subprocess.DEVNULL, 'env': env, 'cwd': self.work.name,
                'bufsize': 0,
            }
            if os.name == 'posix':
                options['start_new_session'] = True
                options['preexec_fn'] = lambda: _parent_guard(parent)
            self.process = subprocess.Popen(args, **options)
            if os.name == 'posix':
                try:
                    self._process_group = os.getpgid(self.process.pid)
                except OSError:
                    self._process_group = None
                os.set_blocking(self.process.stdin.fileno(), False)
            self.process.stdout = io.BufferedReader(self.process.stdout)
            self._reader_thread = threading.Thread(target=self._read, daemon=True)
            self._reader_thread.start()
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
        with self._exit_lock:
            if self._closed:
                return
            self._closed = True
            process = self.process
            work = self.work
            try:
                if process:
                    stdin = getattr(process, 'stdin', None)
                    stdout = getattr(process, 'stdout', None)
                    if os.name == 'posix':
                        process_group = self._capture_process_group(process)
                        if process_group is not None:
                            self._signal_group(process_group, signal.SIGTERM)
                        self._wait_for_process(process, 0.1)
                        if process_group is not None and self._group_exists(process_group):
                            deadline = time.monotonic() + 0.2
                            while time.monotonic() < deadline:
                                if not self._group_exists(process_group):
                                    break
                                self._wait_for_process(process, 0.05)
                                time.sleep(0.01)
                            if self._group_exists(process_group):
                                self._signal_group(process_group, signal.SIGKILL)
                                deadline = time.monotonic() + 0.2
                                while time.monotonic() < deadline:
                                    if not self._group_exists(process_group):
                                        break
                                    time.sleep(0.01)
                    else:
                        try:
                            running = process.poll() is None
                        except OSError:
                            running = False
                        if running:
                            try:
                                process.terminate()
                            except OSError:
                                pass
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                try:
                                    process.kill()
                                except OSError:
                                    pass
                                try:
                                    process.wait(timeout=2)
                                except (OSError, subprocess.TimeoutExpired, ValueError):
                                    pass
                            except (OSError, ValueError):
                                pass
                    if self._reader_thread is not None:
                        self._reader_thread.join(timeout=READER_JOIN_WAIT)
                    if stdin is not None:
                        try:
                            stdin.close()
                        except (OSError, ValueError):
                            pass
                    if self._reader_thread is not None and self._reader_thread.is_alive():
                        process.stdout = None
                    elif stdout is not None:
                        try:
                            stdout.close()
                        except (OSError, ValueError):
                            pass
                    self._wait_for_process(process, 0.2)
            finally:
                self.process = None
                self._process_group = None
                self.work = None
                if work:
                    try:
                        work.cleanup()
                    except OSError:
                        pass

    def _read(self):
        fatal = False
        try:
            process = self.process
            stream = None if process is None else process.stdout
            if process is None or stream is None:
                fatal = True
                return
            while True:
                line = stream.readline(MAX_JSON_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_JSON_LINE_BYTES:
                    fatal = True
                    break
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    fatal = True
                    break
                if not isinstance(value, dict):
                    fatal = True
                    break
                try:
                    self.events.put_nowait(value)
                except queue.Full:
                    fatal = True
                    break
        except (OSError, ValueError):
            fatal = True
        finally:
            if fatal:
                self._reader_fatal.set()
            try:
                self.events.put_nowait(None)
            except queue.Full:
                self._reader_fatal.set()
            self._reader_done.set()

    @staticmethod
    def _send_error():
        return RuntimeError('ChatGPT could not send this operation. Try again.')

    @staticmethod
    def _connection_error():
        return RuntimeError('ChatGPT connection closed. Try again.')

    @staticmethod
    def _timeout_error():
        return RuntimeError('ChatGPT operation timed out. Try again.')

    def _send_posix(self, data, deadline, clock):
        try:
            stream = self.process.stdin
            descriptor = stream.fileno()
        except (AttributeError, OSError, ValueError):
            raise self._connection_error() from None
        offset = 0
        while offset < len(data):
            remaining = deadline - clock()
            if remaining <= 0:
                raise self._timeout_error()
            try:
                written = os.write(descriptor, data[offset:])
            except BlockingIOError:
                try:
                    _, writable, _ = select.select([], [descriptor], [], remaining)
                except (OSError, ValueError):
                    raise self._connection_error() from None
                if not writable:
                    raise self._timeout_error()
                continue
            except InterruptedError:
                continue
            except (BrokenPipeError, OSError):
                raise self._connection_error() from None
            if written <= 0:
                raise self._connection_error()
            offset += written

    def _send_fallback(self, data, deadline, clock):
        done = threading.Event()
        failed = []

        def write():
            try:
                stream = self.process.stdin
                offset = 0
                while offset < len(data):
                    written = stream.write(data[offset:])
                    if not written:
                        raise BrokenPipeError
                    offset += written
                stream.flush()
            except (AttributeError, BrokenPipeError, OSError, ValueError):
                failed.append(True)
            finally:
                done.set()

        threading.Thread(target=write, daemon=True).start()
        remaining = deadline - clock()
        if remaining <= 0 or not done.wait(remaining):
            raise self._timeout_error()
        if failed:
            raise self._connection_error()

    def send(self, value, deadline=None, clock=time.monotonic):
        try:
            if not isinstance(value, dict):
                raise TypeError
            data = (json.dumps(
                value, ensure_ascii=False, separators=(',', ':'),
                allow_nan=False) + '\n').encode('utf-8')
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise self._send_error() from None
        if len(data) > MAX_JSON_LINE_BYTES:
            raise self._send_error()
        if deadline is None:
            deadline = clock() + MAX_SEND_WAIT
        remaining = deadline - clock()
        if remaining <= 0 or not self._send_lock.acquire(timeout=remaining):
            raise self._timeout_error()
        try:
            if self._closed or self.process is None:
                raise self._connection_error()
            if os.name == 'posix':
                self._send_posix(data, deadline, clock)
            else:
                self._send_fallback(data, deadline, clock)
        finally:
            self._send_lock.release()

    def receive(self, timeout=90):
        if timeout <= 0:
            raise RuntimeError('ChatGPT operation timed out. Try again.')
        deadline = time.monotonic() + timeout
        while True:
            if self._reader_fatal.is_set():
                raise self._connection_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('ChatGPT did not respond in time. Try again.')
            try:
                value = self.events.get(timeout=min(RECEIVE_POLL_WAIT, remaining))
            except queue.Empty:
                if self._reader_done.is_set():
                    raise self._connection_error()
                continue
            if value is None:
                raise self._connection_error()
            return value

    def next_event(self, timeout=90):
        if timeout <= 0:
            raise RuntimeError('ChatGPT operation timed out. Try again.')
        return self.pending.popleft() if self.pending else self.receive(timeout)

    def request(self, method, params=None, timeout=30):
        self.sequence += 1
        request_id = self.sequence
        deadline = time.monotonic() + timeout
        outbound = {'id': request_id, 'method': method, 'params': params or {}}
        if method in ('thread/start', 'thread/inject_items', 'turn/start'):
            core.debug_event('llm.request', outbound)
        self.send(
            outbound,
            deadline=deadline,
        )
        while True:
            value = self.receive(deadline - time.monotonic())
            if value.get('id') == request_id and 'method' not in value:
                if method in ('thread/start', 'thread/inject_items', 'turn/start'):
                    core.debug_event('llm.response', value)
                if 'error' in value:
                    # Provider diagnostics can contain request data or credentials.
                    raise RuntimeError('ChatGPT could not complete this operation. Check your connection, account, and selected model.')
                return value.get('result', {})
            if 'id' in value and 'method' in value:
                self.reject(value, deadline=deadline)
            else:
                self.pending.append(value)
                if len(self.pending) > 1024:
                    raise RuntimeError('ChatGPT sent too many pending events.')

    def reject(self, value, deadline=None, clock=time.monotonic):
        self.send({'id': value['id'], 'error': {'code': -32601,
                   'message': 'This operation is not available in AIOS.'}},
                  deadline=deadline, clock=clock)

    def _capture_process_group(self, process):
        if os.name != 'posix':
            return None
        if self._process_group is not None:
            return self._process_group
        try:
            self._process_group = os.getpgid(process.pid)
        except (AttributeError, OSError, TypeError, ValueError):
            self._process_group = None
        return self._process_group

    @staticmethod
    def _wait_for_process(process, timeout):
        try:
            process.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
        except (AttributeError, OSError, ValueError):
            return False

    @staticmethod
    def _signal_group(process_group, sig):
        try:
            os.killpg(process_group, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return False
        except OSError:
            return False

    @staticmethod
    def _group_exists(process_group):
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


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
                        server.reject(event, deadline=deadline)
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


def _chat_remaining(deadline, clock):
    remaining = deadline - clock()
    if remaining <= 0:
        raise RuntimeError('ChatGPT operation timed out. Try again.')
    return remaining


def _chat_result(value, fallback):
    try:
        text = json.dumps(
            value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(text.encode('utf-8')) > MAX_TOOL_RESULT_BYTES:
            raise ValueError
    except (TypeError, ValueError, RecursionError):
        value = {'error': fallback}
        text = json.dumps(value, separators=(',', ':'))
    failed = isinstance(value, dict) and (
        bool(value.get('error')) or value.get('is_error') is True)
    response = {
        'success': not failed,
        'contentItems': [{'type': 'inputText', 'text': text}],
    }
    if (not isinstance(response['success'], bool)
            or response['contentItems'][0].get('type') != 'inputText'
            or not isinstance(response['contentItems'][0].get('text'), str)):
        raise RuntimeError('ChatGPT could not process a tool result.')
    return response


def _chat_rpc_result(value, kind):
    if kind == 'thread':
        item = value.get('thread') if isinstance(value, dict) else None
    else:
        item = value.get('turn') if isinstance(value, dict) else None
    identifier = item.get('id') if isinstance(item, dict) else None
    if not isinstance(identifier, str) or not identifier:
        raise RuntimeError('ChatGPT returned an invalid response. Try again.')
    return identifier


def chat(messages, *, session=None, turn_timeout=MAX_AGENT_SECONDS,
         clock=time.monotonic):
    from . import agent, toolhost

    if (not messages or messages[-1].get('role') != 'user' or
            any(m.get('role') not in ('user', 'assistant', 'system') or
                not isinstance(m.get('content'), str) for m in messages)):
        raise ValueError('Invalid conversation.')
    try:
        duration = float(turn_timeout)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Choose a valid ChatGPT turn timeout.') from None
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Choose a valid ChatGPT turn timeout.')
    deadline = clock() + duration

    with account_lock(), Server() as server:
        account_result = server.request(
            'account/read', timeout=min(30, _chat_remaining(deadline, clock)))
        account = account_result.get('account') if isinstance(account_result, dict) else None
        if not isinstance(account, dict) or account.get('type') != 'chatgpt':
            raise RuntimeError('Sign in with ChatGPT in AI models settings first.')
        config = core.load_config()
        model = config.get('subscription_model') or None
        if session is not None:
            tools = session.codex_tools()
            base_instructions = session.system_prompt() + '\n\n' + CHATGPT_ACTIVATION_NOTE
            if len(base_instructions.encode('utf-8')) > agent.MAX_SYSTEM_PROMPT_BYTES:
                raise RuntimeError('The configured agent prompt is too large.')
        else:
            tools = []
            base_instructions = agent.POLICY
        started = server.request('thread/start', {
            'model': model, 'modelProvider': 'openai', 'ephemeral': True,
            'cwd': server.work.name, 'sandbox': 'read-only', 'approvalPolicy': 'never',
            'baseInstructions': base_instructions, 'environments': [], 'dynamicTools': tools,
        }, timeout=min(30, _chat_remaining(deadline, clock)))
        thread = _chat_rpc_result(started, 'thread')
        if len(messages) > 1:
            history = [{'type': 'message', 'role': m['role'], 'content': [
                {'type': 'output_text' if m['role'] == 'assistant' else 'input_text',
                 'text': m['content']}]} for m in messages[:-1]]
            server.request(
                'thread/inject_items',
                {'threadId': thread, 'items': history},
                timeout=min(30, _chat_remaining(deadline, clock)),
            )
        turn_result = server.request('turn/start', {'threadId': thread, 'input': [
            {'type': 'text', 'text': messages[-1]['content']}]},
            timeout=min(30, _chat_remaining(deadline, clock)))
        _chat_rpc_result(turn_result, 'turn')
        count = 0
        content_bytes = 0
        buffered_content = ""
        completion_retries = 0
        while True:
            event = server.next_event(min(
                MAX_EVENT_WAIT, _chat_remaining(deadline, clock)))
            core.debug_event('llm.response', event)
            if not isinstance(event, dict):
                raise RuntimeError('ChatGPT returned an invalid event. Try again.')
            method = event.get('method')
            params = event.get('params', {})
            if 'id' in event:
                request_id = event.get('id')
                if (isinstance(request_id, bool)
                        or not isinstance(request_id, (str, int))):
                    raise RuntimeError('ChatGPT returned an invalid tool request. Try again.')
                if not isinstance(method, str):
                    server.reject(event, deadline=deadline, clock=clock)
                    raise RuntimeError('ChatGPT returned an invalid tool request. Try again.')
                if method != 'item/tool/call':
                    server.reject(event, deadline=deadline, clock=clock)
                    continue
                if not isinstance(params, dict):
                    server.reject(event, deadline=deadline, clock=clock)
                    raise RuntimeError('ChatGPT returned an invalid tool request. Try again.')
                event_thread = params.get('threadId')
                if not isinstance(event_thread, str):
                    server.reject(event, deadline=deadline, clock=clock)
                    raise RuntimeError('ChatGPT returned an invalid tool request. Try again.')
                if event_thread != thread:
                    server.reject(event, deadline=deadline, clock=clock)
                    continue
                args = params.get('arguments')
                count += 1
                tool = params.get('tool')
                try:
                    encoded_args = json.dumps(
                        args, ensure_ascii=False, separators=(',', ':'),
                        allow_nan=False).encode('utf-8')
                except (TypeError, ValueError, RecursionError):
                    encoded_args = b''
                if (count > MAX_TOOL_CALLS or not isinstance(tool, str) or not tool
                        or not isinstance(args, dict) or not encoded_args
                        or len(encoded_args) > MAX_ARGUMENT_BYTES):
                    raise RuntimeError(
                        'ChatGPT requested an invalid tool call or reached the action limit.')
                if session is not None:
                    buffered_content = ""
                    yield {'type': 'progress', 'text': session.progress(tool, args)}
                    try:
                        remaining = _chat_remaining(deadline, clock)
                        result = session.dispatch(
                            tool, args, timeout=min(toolhost.SOCKET_TIMEOUT, remaining))
                    except ValueError as error:
                        result = {'error': agent._safe_error_text(
                            str(error),
                            'Tool action failed. Review the request and try again.',
                        )}
                    except (RuntimeError, OSError):
                        result = {
                            'error': 'Tool action failed. Review the request and try again.'}
                    response = _chat_result(
                        result, 'Tool action failed. Review the request and try again.')
                else:
                    raise RuntimeError(
                        'ChatGPT requested an invalid tool call or reached the action limit.')
                server.send(
                    {'id': request_id, 'result': response},
                    deadline=deadline,
                    clock=clock,
                )
                core.debug_event('tool.result', {
                    'name': tool,
                    'result': response,
                })
                continue

            if not isinstance(method, str) or not isinstance(params, dict):
                raise RuntimeError('ChatGPT returned an invalid event. Try again.')
            if 'threadId' not in params:
                continue
            event_thread = params.get('threadId')
            if not isinstance(event_thread, str):
                raise RuntimeError('ChatGPT returned an invalid event. Try again.')
            if event_thread == thread:
                if method == 'item/agentMessage/delta':
                    delta = params.get('delta')
                    if not isinstance(delta, str):
                        raise RuntimeError('ChatGPT returned an invalid reply. Try again.')
                    content_bytes += len(delta.encode('utf-8'))
                    if content_bytes > agent.MAX_CONTENT_BYTES:
                        raise RuntimeError('ChatGPT reply was too large. Try again.')
                    if session is not None and session.verify_application_completion:
                        buffered_content += delta
                    else:
                        yield {'type': 'token', 'text': delta}
                elif method == 'turn/completed':
                    turn = params.get('turn')
                    status = turn.get('status') if isinstance(turn, dict) else None
                    if not isinstance(status, str):
                        raise RuntimeError('ChatGPT returned an invalid completion. Try again.')
                    if status != 'completed':
                        raise RuntimeError('ChatGPT could not finish the reply. Check your subscription limits or try again.')
                    if session is not None and session.verify_application_completion:
                        if session.application_completion_pending():
                            if completion_retries >= agent.MAX_APPLICATION_COMPLETION_RETRIES:
                                raise RuntimeError(agent.APPLICATION_LAUNCH_PENDING)
                            completion_retries += 1
                            buffered_content = ""
                            resumed = server.request('turn/start', {'threadId': thread, 'input': [
                                {'type': 'text', 'text': session.application_completion_prompt()}]},
                                timeout=min(30, _chat_remaining(deadline, clock)))
                            _chat_rpc_result(resumed, 'turn')
                            continue
                        if buffered_content:
                            yield {'type': 'token', 'text': buffered_content}
                    return
                elif method == 'error':
                    will_retry = params.get('willRetry', False)
                    if not isinstance(will_retry, bool):
                        raise RuntimeError('ChatGPT returned an invalid event. Try again.')
                    if will_retry:
                        continue
                    raise RuntimeError('ChatGPT could not finish the reply. Check your account and usage limits.')
