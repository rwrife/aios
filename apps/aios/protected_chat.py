"""Broker-owned protected foreground chat process and fixed tool relay."""
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from .scheduled_jobs import REQUEST_LIMIT, RESPONSE_LIMIT, encode, validate_request
from .scheduling import SchedulingError


CHAT_EVENT_LIMIT = 2 * 1024 * 1024
CHAT_MESSAGE_LIMIT = 32768
CHAT_POLL_LIMIT = 256


class ProtectedChat:
    def __init__(self, sessions):
        self.sessions = sessions
        self.owner, self.lease, self.work = sessions.owner, sessions.lease, sessions.work
        self.id = str(uuid.uuid4())
        self.scope = str(uuid.uuid4())
        self.process = sessions.isolation.chat(self.scope, sessions.root, sessions.uid)
        self.frame = bytearray()
        self.pending = []
        self.reply = ''
        self.busy = False
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            ready = self._read_until(time.monotonic() + 5)
            if (set(ready) != {'type', 'config'} or ready.get('type') != 'ready'
                    or not isinstance(ready.get('config'), dict)):
                raise RuntimeError('Protected chat could not initialize')
            self.config = ready['config']
        except Exception:
            self.close()
            raise

    def authorize(self):
        self.sessions._present()
        if (self.owner, self.lease, self.work) != (
                self.sessions.owner, self.sessions.lease, self.sessions.work):
            raise PermissionError('Protected chat scope expired')

    def _write(self, value):
        raw = encode(value, REQUEST_LIMIT)
        deadline = time.monotonic() + 2
        while raw:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError('Protected chat stopped')
            with selectors.DefaultSelector() as poll:
                poll.register(self.process.stdin, selectors.EVENT_WRITE)
                if not poll.select(max(0, deadline - time.monotonic())):
                    raise RuntimeError('Protected chat control timed out')
            sent = os.write(self.process.stdin.fileno(), raw)
            raw = raw[sent:]

    def _read_available(self):
        if self.process is None:
            return
        while True:
            try:
                chunk = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                if self.process.poll() is not None:
                    raise RuntimeError('Protected chat stopped')
                break
            self.frame.extend(chunk)
            if len(self.frame) > CHAT_EVENT_LIMIT:
                raise RuntimeError('Protected chat event budget exceeded')
            while b'\n' in self.frame:
                line, _, self.frame = self.frame.partition(b'\n')
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                    raise RuntimeError('Invalid protected chat event')
                self.pending.append(event)

    def _read_until(self, deadline):
        while time.monotonic() < deadline:
            self._read_available()
            if self.pending:
                return self.pending.pop(0)
            with selectors.DefaultSelector() as poll:
                poll.register(self.process.stdout, selectors.EVENT_READ)
                poll.select(min(.05, max(0, deadline - time.monotonic())))
        raise RuntimeError('Protected chat initialization timed out')

    def send(self, messages):
        self.authorize()
        if self.busy:
            raise ValueError('Protected chat is already replying')
        if (not isinstance(messages, list) or not messages or len(messages) > 40
                or any(not isinstance(item, dict) or set(item) != {'role', 'content'}
                       or item['role'] not in ('user', 'assistant')
                       or not isinstance(item['content'], str)
                       or len(item['content']) > CHAT_MESSAGE_LIMIT for item in messages)):
            raise ValueError('Invalid protected conversation')
        self.reply = ''
        self.busy = True
        self._write({'action': 'send', 'messages': messages})

    def stop(self):
        self.authorize()
        if self.busy:
            self._write({'action': 'stop'})

    def poll(self):
        self.authorize()
        self._read_available()
        released = []
        while self.pending and len(released) < CHAT_POLL_LIMIT:
            event = self.pending.pop(0)
            kind = event['type']
            if kind == 'scheduled_jobs':
                request = event.get('request')
                validate_request(request)
                try:
                    result = self.sessions.scheduled_request(self.lease, self.work, request)
                except SchedulingError as error:
                    result = {'status': error.code, 'error': str(error)}
                self._write({'action': 'scheduled_jobs', 'id': event.get('id'), 'response': result})
                continue
            if kind == 'token':
                text = event.get('text')
                if not isinstance(text, str):
                    raise RuntimeError('Invalid protected chat token')
                self.reply += text
                if len(self.reply.encode()) > 65536:
                    raise RuntimeError('Protected chat reply is too large')
            elif kind == 'done':
                self.busy = False
                if self.reply:
                    self.sessions.journal.message(self.work, 'assistant', self.reply)
            elif kind == 'error':
                self.busy = False
                if not isinstance(event.get('text'), str):
                    raise RuntimeError('Invalid protected chat error')
            else:
                raise RuntimeError('Invalid protected chat event')
            released.append(event)
        self.authorize()
        return {'events': released, 'busy': self.busy}

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            self.sessions.isolation.stop(self.scope)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


def _read_line(connection, limit):
    frame = bytearray()
    while b'\n' not in frame:
        chunk = connection.recv(min(4096, limit + 1 - len(frame)))
        if not chunk:
            break
        frame.extend(chunk)
        if len(frame) > limit:
            raise ValueError('Invalid protected relay request')
    if not frame.endswith(b'\n') or frame.count(b'\n') != 1:
        raise ValueError('Invalid protected relay request')
    return json.loads(frame)


def _relay_request(server, events, control):
    connection, _ = server.accept()
    with connection:
        request = _read_line(connection, REQUEST_LIMIT)
        if not isinstance(request, dict) or set(request) != {'action', 'request'} \
                or request['action'] != 'scheduled_jobs':
            raise ValueError('Invalid protected relay request')
        validate_request(request['request'])
        correlation = str(uuid.uuid4())
        events({'type': 'scheduled_jobs', 'id': correlation, 'request': request['request']})
        while True:
            command = control()
            if (command.get('action') == 'scheduled_jobs'
                    and command.get('id') == correlation
                    and set(command) == {'action', 'id', 'response'}):
                raw = encode(command['response'], RESPONSE_LIMIT)
                connection.sendall(raw)
                return
            if command.get('action') == 'stop':
                raise InterruptedError
            raise ValueError('Invalid protected relay response')


def serve():
    from .core import load_config
    os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix='aios-protected-chat-', dir='/tmp'))
    relay = directory / 'scheduled.sock'
    worker = host = None
    frame = bytearray()

    def event(value):
        sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode() + b'\n')
        sys.stdout.buffer.flush()

    def command():
        raw = sys.stdin.buffer.readline(REQUEST_LIMIT + 1)
        if not raw or len(raw) > REQUEST_LIMIT or not raw.endswith(b'\n'):
            raise InterruptedError
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get('action'), str):
            raise ValueError('Invalid protected chat command')
        return value

    def cleanup_turn():
        nonlocal worker, host
        for process in (worker, host):
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
        worker = host = None

    with socket.socket(socket.AF_UNIX) as relay_server:
        relay_server.bind(str(relay))
        relay.chmod(0o600)
        relay_server.listen(1)
        config = load_config()
        event({'type': 'ready', 'config': {key: config.get(key) for key in (
            'mode', 'model', 'model_path', 'subscription_model',
            'theme_color', 'reduced_motion', 'voice_mode', 'voice_url')}})
        try:
            while True:
                value = command()
                if value.get('action') != 'send' or set(value) != {'action', 'messages'}:
                    raise ValueError('Invalid protected chat command')
                environment = {**os.environ,
                               'AIOS_BROWSER_SESSION': 'protected-' + str(uuid.uuid4()),
                               'AIOS_PROTECTED_CHAT_SOCKET': str(relay)}
                for name in ('AIOS_SESSION_SOCKET', 'AIOS_SESSION_ID',
                             'AIOS_DESKTOP_CONTROL_SOCKET', 'AIOS_BACKGROUND_RUN'):
                    environment.pop(name, None)
                tool_socket = directory / 'tools.sock'
                host = subprocess.Popen(
                    [sys.executable, '-m', 'aios.toolhost', str(tool_socket)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=environment, start_new_session=True)
                worker = subprocess.Popen(
                    [sys.executable, '-m', 'aios.worker'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=environment, start_new_session=True)
                worker.stdin.write(encode({'action': 'chat', 'messages': value['messages'],
                                           'tool_socket': str(tool_socket)}, REQUEST_LIMIT))
                worker.stdin.close()
                with selectors.DefaultSelector() as poll:
                    poll.register(worker.stdout, selectors.EVENT_READ, 'worker')
                    poll.register(sys.stdin.buffer, selectors.EVENT_READ, 'control')
                    poll.register(relay_server, selectors.EVENT_READ, 'relay')
                    done = False
                    while not done:
                        for key, _ in poll.select(.1):
                            if key.data == 'relay':
                                _relay_request(relay_server, event, command)
                                continue
                            if key.data == 'control':
                                control = command()
                                if control.get('action') == 'stop' and set(control) == {'action'}:
                                    cleanup_turn()
                                    event({'type': 'error', 'text': 'Stopped'})
                                    done = True
                                    break
                                raise ValueError('Invalid protected chat command')
                            chunk = os.read(worker.stdout.fileno(), 8192)
                            if not chunk:
                                worker.wait(timeout=1)
                                if not done:
                                    event({'type': 'error',
                                           'text': 'Protected chat stopped before completing'})
                                done = True
                                break
                            frame.extend(chunk)
                            if len(frame) > REQUEST_LIMIT:
                                raise ValueError('Protected worker event is too large')
                            while b'\n' in frame:
                                line, _, frame = frame.partition(b'\n')
                                item = json.loads(line)
                                kind = item.get('type')
                                if kind in ('token', 'progress'):
                                    event(item)
                                elif kind == 'done':
                                    event({'type': 'done'})
                                    done = True
                                    break
                                elif kind in ('error', 'needs_user_action'):
                                    event({'type': 'error',
                                           'text': item.get('text', 'Protected chat failed')})
                                    done = True
                                    break
                                else:
                                    raise ValueError('Invalid protected worker event')
                            if host.poll() is not None and not done:
                                event({'type': 'error', 'text': 'Protected tool host stopped'})
                                done = True
                    cleanup_turn()
        except (BrokenPipeError, InterruptedError):
            pass
        finally:
            cleanup_turn()
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == '__main__':
    serve()
