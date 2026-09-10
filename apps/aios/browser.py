"""Client helpers for the private AIOS WebEngine browser control socket."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse

ACTIONS = (
    'open', 'navigate', 'snapshot', 'click', 'type', 'press', 'scroll',
    'back', 'forward', 'reload', 'stop', 'tabs', 'switch', 'close',
)


def web_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError('Enter a valid web URL.')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError('Browser navigation requires an HTTP(S) URL without embedded credentials.')
    return value


def call(path, arguments):
    deadline = time.monotonic() + 5
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(45)
        while True:
            try:
                client.connect(path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise RuntimeError('Browser service is unavailable.') from None
                time.sleep(.05)
        client.sendall(json.dumps(arguments).encode() + b'\n')
        with client.makefile('rb') as stream:
            raw = stream.readline(128 * 1024)
        if not raw.endswith(b'\n'):
            raise RuntimeError('Browser response was incomplete.')
        result = json.loads(raw)
        if isinstance(result, dict) and result.get('error'):
            raise RuntimeError(result['error'])
        return result


def registry_directory(runtime_dir=None):
    runtime = runtime_dir or os.environ.get('XDG_RUNTIME_DIR')
    if not runtime:
        return None
    return Path(runtime) / 'aios' / 'browsers'


def discover(runtime_dir=None):
    """Return same-user browser registrations suitable for a local MCP or skill."""
    directory = registry_directory(runtime_dir)
    if not directory or not directory.is_dir():
        return []
    browsers = []
    for path in sorted(directory.glob('*.json')):
        try:
            stat = path.stat()
            if hasattr(os, 'geteuid') and stat.st_uid != os.geteuid():
                continue
            if stat.st_mode & 0o077:
                continue
            value = json.loads(path.read_text()[:16384])
            if (value.get('version') == 1 and isinstance(value.get('session'), str)
                    and isinstance(value.get('socket'), str)
                    and isinstance(value.get('actions'), list)):
                browsers.append(value)
        except (OSError, ValueError, TypeError):
            continue
    return browsers


class Browser:
    """Developer helper that launches the same browser used by desktop chat."""

    def __init__(self, executable='aios-browser', theme='blue'):
        self.executable = executable
        self.theme = theme
        self.process = None
        self.directory = None
        self.socket = None

    def start(self):
        if self.process and self.process.poll() is None:
            return
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            raise RuntimeError('Run the browser as the desktop user, not root.')
        self.close()
        self.directory = tempfile.TemporaryDirectory(prefix='aios-browser-')
        self.socket = str(Path(self.directory.name) / 'browser.sock')
        session = 'developer-' + str(os.getpid())
        try:
            self.process = subprocess.Popen([
                self.executable, '--socket', self.socket, '--session', session,
                '--theme', self.theme,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            deadline = time.monotonic() + 10
            while not Path(self.socket).exists():
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('AIOS browser could not start.')
                time.sleep(.05)
        except Exception:
            self.close()
            raise

    def act(self, arguments):
        if not isinstance(arguments, dict) or arguments.get('action') not in ACTIONS:
            raise ValueError('Unknown browser action.')
        action = arguments['action']
        if action == 'close':
            self.close()
            return {'closed': True}
        if action in ('open', 'navigate'):
            arguments = dict(arguments)
            arguments['url'] = web_url(arguments.get('url'))
        if action == 'open':
            self.start()
        elif not self.process or self.process.poll() is not None:
            raise ValueError('Open the browser first.')
        return call(self.socket, arguments)

    def close(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=3)
        self.process = None
        self.socket = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None


if __name__ == '__main__':
    print(json.dumps(discover()))
