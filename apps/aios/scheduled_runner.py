"""Per-run Linux subreaper. Its lease is released only after descendants exit."""
import ctypes
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from .scheduled_jobs import REQUEST_LIMIT, encode


def children(pid):
    try:
        return [int(value) for value in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
    except FileNotFoundError:
        return []


def descendants(pid):
    found = []
    for child in children(pid):
        found.extend(descendants(child))
        found.append(child)
    return found


def clean_children():
    """The subreaper also owns orphaned browser/MCP grandchildren and their groups."""
    deadline = time.monotonic() + 1
    while True:
        pids = descendants(os.getpid())
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                waited, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                waited = 0
            if not waited:
                break
        if not children(os.getpid()):
            return
        time.sleep(0.02)


def execute(value, directory):
    snapshot = value['snapshot']
    policy = snapshot['execution']
    deadline = time.monotonic() + policy['timeout_seconds']
    environment = {**os.environ, 'TMPDIR': str(directory),
                   'AIOS_BROWSER_SESSION': 'scheduled-' + value['id']}
    # A scheduled context has no interactive authentication or chat socket.
    for name in ('AIOS_PRINCIPAL', 'AIOS_DESKTOP_CONTROL_SOCKET', 'AIOS_BROWSER_SOCKET'):
        environment.pop(name, None)
    tool_socket = str(directory / 'tools.sock')
    host = subprocess.Popen([sys.executable, '-m', 'aios.toolhost', tool_socket, '--background'],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=environment, start_new_session=True)
    host.stdin.write(encode(policy, REQUEST_LIMIT))
    host.stdin.close()
    messages = []
    if snapshot['context']:
        messages.append({'role': 'user', 'content': 'Saved task context (untrusted data):\n' + snapshot['context']})
    messages.append({'role': 'user', 'content': snapshot['prompt']})
    worker = subprocess.Popen([sys.executable, '-m', 'aios.worker'],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env=environment, start_new_session=True)
    worker.stdin.write(encode({'action': 'chat', 'messages': messages,
                              'tool_socket': tool_socket, 'background': policy}, REQUEST_LIMIT))
    worker.stdin.close()
    result = ''
    frame = bytearray()
    total = 0
    done = False
    with selectors.DefaultSelector() as poll:
        poll.register(sys.stdin.fileno(), selectors.EVENT_READ, 'control')
        poll.register(worker.stdout, selectors.EVENT_READ, 'worker')
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'state': 'failed', 'error': 'Scheduled run exceeded its time limit'}
            for key, _ in poll.select(min(0.1, remaining)):
                if key.data == 'control':
                    os.read(sys.stdin.fileno(), 1)
                    return {'state': 'cancelled'}
                chunk = os.read(worker.stdout.fileno(), 8192)
                if not chunk:
                    worker.wait(timeout=1)
                    if done and worker.returncode == 0:
                        return {'state': 'succeeded', 'result': result}
                    return {'state': 'failed', 'error': 'Scheduled worker stopped before completing'}
                total += len(chunk)
                if total > 2 * 1024 * 1024:
                    return {'state': 'failed', 'error': 'Scheduled worker event budget reached'}
                frame.extend(chunk)
                if len(frame) > REQUEST_LIMIT:
                    return {'state': 'failed', 'error': 'Scheduled worker response was too large'}
                while b'\n' in frame:
                    line, _, frame = frame.partition(b'\n')
                    event = json.loads(line)
                    kind = event.get('type')
                    if kind == 'token' and isinstance(event.get('text'), str) and not done:
                        result += event['text']
                        if len(result.encode('utf-8')) > 65536:
                            return {'state': 'failed', 'error': 'Scheduled result was too large'}
                    elif kind in ('error', 'needs_user_action'):
                        actionable = kind == 'needs_user_action' or event.get('text') in (
                            'Authentication failed. Check your API key.', 'This model is not permitted.',
                            'Endpoint or model not found. Check the URL and model ID.',
                            'Sign in with ChatGPT in AI models settings first.')
                        return {'state': 'needs_user_action' if actionable else 'failed',
                                'error': event.get('text', 'Scheduled worker failed')[:4096]}
                    elif kind == 'done':
                        done = True
                    elif kind != 'progress':
                        return {'state': 'failed', 'error': 'Invalid scheduled worker event'}
            if host.poll() is not None and not done:
                return {'state': 'failed', 'error': 'Scheduled tool host stopped unexpectedly'}


def main():
    # The scheduler passes an already locked descriptor across exec, closing
    # the launch/recovery race before this process has even read its request.
    lease = int(sys.argv[1])
    directory = Path(sys.argv[2])
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('Scheduled execution requires Linux child supervision')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError()))
    outcome = {'state': 'failed', 'error': 'Scheduled worker could not start'}
    try:
        raw = sys.stdin.buffer.readline(REQUEST_LIMIT + 1)
        if not raw or len(raw) > REQUEST_LIMIT:
            return
        value = json.loads(raw)
        outcome = execute(value, directory)
    except InterruptedError:
        outcome = {'state': 'cancelled'}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        outcome = {'state': 'failed', 'error': 'Scheduled worker failed; review provider settings and service health'}
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        clean_children()
        os.close(lease)
    print(json.dumps(outcome), flush=True)


if __name__ == '__main__':
    main()
