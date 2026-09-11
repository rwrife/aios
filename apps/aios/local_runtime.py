"""One session-owned llama server and interactive-first, connection-held leases.

Run ``python3 -m aios.local_runtime service`` from the desktop session, and
``python3 -m aios.local_runtime stop`` before ending it. Workers may also start
the fixed service launcher on demand for interactive work. Background workers
require the desktop-owned service: even a detached child of a scheduled run
would remain subject to its guardian's descendant cleanup.

Core's local HTTP transport must hold ``admission(config, background=...,
timeout=...)`` until the HTTP response is closed, not just until headers arrive.
Close the response *before* leaving admission. Remote requests bypass this API.
An abandoned/expired lease restarts llama before admitting another request,
so an orphaned generation cannot continue delaying interactive work.
"""

import argparse
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request


ENDPOINT = "http://127.0.0.1:8080/v1"
MAX_BACKGROUND_SECONDS = 60
MAX_INTERACTIVE_SECONDS = 120
MAX_WAIT_SECONDS = 300
MODEL_START_SECONDS = 180
MAX_CLIENTS = 64
MAX_MESSAGE_BYTES = 8192
POLL_SECONDS = 0.05


class RuntimeUnavailable(RuntimeError):
    """A local runtime request failed; safe to report without raw model logs."""


def runtime_dir():
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value or not Path(value).is_absolute():
        raise RuntimeUnavailable("A private XDG_RUNTIME_DIR is required for local inference.")
    root = Path(value)
    _private_directory(root)
    path = root / "aios-local-runtime"
    path.mkdir(mode=0o700, exist_ok=True)
    _private_directory(path)
    return path


def _private_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeUnavailable("The local runtime directory must be owned by this user with mode 0700.")


def _owned_file(path, kind):
    info = path.lstat()
    if info.st_uid != os.getuid() or not kind(info.st_mode) or info.st_mode & 0o077:
        raise RuntimeUnavailable("The local runtime contains an unsafe socket or lock.")


def _model_identity(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise RuntimeUnavailable("Choose an existing GGUF model file.")
    path = Path(value).expanduser().resolve(strict=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or path.suffix.lower() != ".gguf":
        raise RuntimeUnavailable("Choose an existing GGUF model file.")
    return str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _seconds(value, maximum):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise RuntimeUnavailable("Local inference timeouts must be positive finite numbers.")
    return min(float(value), maximum)


def _die_with_parent(parent):
    # Only used by the single-threaded service immediately before exec.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Could not bind model lifetime to runtime")
    if os.getppid() != parent:
        os._exit(1)


class ModelProcess:
    def __init__(self):
        self.process = None
        self.identity = None
        self.started = 0
        self.ready = False
        self.last_probe = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self, identity):
        self.stop()
        executable = next((p for p in ("/usr/local/bin/llama-server", "/usr/bin/llama-server")
                           if os.path.isfile(p) and os.access(p, os.X_OK)), None)
        if executable is None:
            raise RuntimeUnavailable("The local model server is not installed.")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", 8080)) == 0:
                raise RuntimeUnavailable("The local model port is already in use by another service.")
        parent = os.getpid()
        self.process = subprocess.Popen(
            [executable, "--model", identity[0], "--alias", "local",
             "--host", "127.0.0.1", "--port", "8080", "--ctx-size", "8192",
             "--parallel", "1", "--jinja", "--chat-template-kwargs",
             '{"enable_thinking":false}'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, preexec_fn=lambda: _die_with_parent(parent))
        self.identity = identity
        self.started = time.monotonic()
        self.last_probe = 0
        self.ready = False

    def check(self):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeUnavailable("The local model stopped. Check model compatibility and available memory.")
        now = time.monotonic()
        if not self.ready and now - self.started >= MODEL_START_SECONDS:
            raise RuntimeUnavailable("The local model did not become ready within the startup limit.")
        if not self.ready and now - self.last_probe >= 0.25:
            self.last_probe = now
            try:
                with self.opener.open("http://127.0.0.1:8080/health", timeout=0.2) as response:
                    self.ready = response.status == 200
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code != 503:
                    raise RuntimeUnavailable("The local model health check failed.") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                # Connection refusal and 503 are expected only during bounded startup.
                pass
        return self.ready

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process = None
        self.identity = None
        self.ready = False


@dataclass
class Client:
    connection: socket.socket
    sequence: int
    deadline: float
    buffer: bytearray = field(default_factory=bytearray)
    state: str = "hello"
    identity: tuple = ()
    background: bool = False
    lease_seconds: float = 0
    operation: str = ""


class RuntimeService:
    def __init__(self, *, model=None):
        self.directory = runtime_dir()
        self.model = ModelProcess() if model is None else model
        self.selector = selectors.DefaultSelector()
        self.clients = {}
        self.active = None
        self.stopping = False
        self.sequence = 0
        self.lock_fd = None
        self.listener = None
        self.bound = False

    def open(self):
        lock = self.directory / "service.lock"
        self.lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        _owned_file(lock, stat.S_ISREG)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            self.lock_fd = None
            return False
        path = self.directory / "service.sock"
        if path.exists() or path.is_symlink():
            _owned_file(path, stat.S_ISSOCK)
            path.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.bound = True
        path.chmod(0o600)
        self.listener.listen(MAX_CLIENTS)
        self.listener.setblocking(False)
        self.selector.register(self.listener, selectors.EVENT_READ)
        return True

    def _reply(self, client, value):
        try:
            client.connection.sendall(json.dumps(value).encode() + b"\n")
            return True
        except OSError:
            return False

    def _close(self, client, *, abandoned=False):
        if client is self.active:
            if abandoned:
                self.model.stop()
            self.active = None
        self.selector.unregister(client.connection)
        self.clients.pop(client.connection)
        client.connection.close()

    def _fail(self, client, message):
        self._reply(client, {"ok": False, "error": message})
        self._close(client, abandoned=True)

    def _accept(self):
        connection, _ = self.listener.accept()
        uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if uid != os.getuid() or len(self.clients) >= MAX_CLIENTS:
            connection.close()
            return
        connection.setblocking(False)
        self.sequence += 1
        client = Client(connection, self.sequence, time.monotonic() + 5)
        self.clients[connection] = client
        self.selector.register(connection, selectors.EVENT_READ, client)

    def _read(self, client):
        try:
            chunk = client.connection.recv(MAX_MESSAGE_BYTES + 1)
        except OSError:
            self._close(client, abandoned=True)
            return
        if not chunk:
            self._close(client, abandoned=True)
            return
        client.buffer.extend(chunk)
        if len(client.buffer) > MAX_MESSAGE_BYTES:
            self._fail(client, "The local runtime request is too large.")
            return
        if b"\n" not in client.buffer:
            return
        raw, _, extra = client.buffer.partition(b"\n")
        client.buffer.clear()
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or extra:
                raise RuntimeUnavailable("Invalid local runtime request.")
            operation = value.get("op")
            if client.state == "active" and value == {"op": "release"}:
                self._reply(client, {"ok": True})
                self._close(client)
                return
            if client.state != "hello":
                raise RuntimeUnavailable("Unexpected local runtime request.")
            if operation in ("ping", "stop") and value == {"op": operation}:
                self._reply(client, {"ok": True})
                self._close(client)
                if operation == "stop":
                    self.stopping = True
                return
            if operation not in ("acquire", "ready") or set(value) != {
                    "op", "model_path", "background", "wait_seconds", "lease_seconds"}:
                raise RuntimeUnavailable("Unknown local runtime operation.")
            if type(value["background"]) is not bool:
                raise RuntimeUnavailable("The local runtime background flag must be a boolean.")
            client.identity = _model_identity(value["model_path"])
            client.background = value["background"]
            client.deadline = time.monotonic() + _seconds(value["wait_seconds"], MAX_WAIT_SECONDS)
            bound = MAX_BACKGROUND_SECONDS if client.background else MAX_INTERACTIVE_SECONDS
            client.lease_seconds = _seconds(value["lease_seconds"], bound)
            client.operation = operation
            client.state = "queued"
        except (ValueError, OSError, RuntimeUnavailable) as exc:
            message = str(exc) if isinstance(exc, RuntimeUnavailable) else "Invalid local runtime request or model file."
            self._fail(client, message)

    def _tick(self):
        now = time.monotonic()
        for client in list(self.clients.values()):
            if now >= client.deadline:
                self._fail(client, "The local inference lease expired." if client is self.active
                           else "Timed out waiting for local inference.")
        if self.active is not None:
            try:
                self.model.check()
            except RuntimeUnavailable as exc:
                self._fail(self.active, str(exc))
            return
        queued = [client for client in self.clients.values() if client.state == "queued"]
        if not queued:
            if self.model.identity is not None:
                try:
                    self.model.check()
                except RuntimeUnavailable:
                    # Idle model crashes have no caller to notify; reap them so
                    # the next caller loads a new process, not a dead PID.
                    self.model.stop()
            return
        client = min(queued, key=lambda item: (item.background, item.sequence))
        try:
            # Recheck at dispatch: an install may replace a file while a worker waits.
            identity = _model_identity(client.identity[0])
            if client.background and identity != client.identity:
                raise RuntimeUnavailable("The scheduled local model changed while waiting; review the job binding.")
            if self.model.identity != identity:
                self.model.start(identity)
            if not self.model.check():
                return
        except (OSError, RuntimeUnavailable) as exc:
            self.model.stop()
            self._fail(client, str(exc) if isinstance(exc, RuntimeUnavailable)
                       else "Could not open the local model or start its service.")
            return
        if time.monotonic() >= client.deadline:
            self._fail(client, "Timed out waiting for local inference.")
            return
        if client.operation == "ready":
            self._reply(client, {"ok": True})
            self._close(client)
            return
        client.state = "active"
        client.deadline = time.monotonic() + client.lease_seconds
        self.active = client
        if not self._reply(client, {"ok": True, "lease_seconds": client.lease_seconds}):
            self._close(client, abandoned=True)

    def serve_forever(self):
        try:
            if not self.open():
                return
            while not self.stopping:
                for key, _ in self.selector.select(POLL_SECONDS):
                    if key.data is None:
                        self._accept()
                    else:
                        self._read(key.data)
                if not self.stopping:
                    self._tick()
        finally:
            self.close()

    def close(self):
        for client in list(self.clients.values()):
            self._fail(client, "The desktop local runtime is stopping.")
        self.model.stop()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.bound:
            (self.directory / "service.sock").unlink(missing_ok=True)
            self.bound = False
        self.selector.close()
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


def _connect():
    path = runtime_dir() / "service.sock"
    _owned_file(path, stat.S_ISSOCK)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(str(path))
        uid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
        if uid != os.getuid():
            raise RuntimeUnavailable("The local runtime belongs to a different user.")
        return connection
    except (OSError, RuntimeUnavailable):
        connection.close()
        raise


def _receive(connection):
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(MAX_MESSAGE_BYTES + 1)
        if not chunk or len(data) + len(chunk) > MAX_MESSAGE_BYTES:
            raise RuntimeUnavailable("The local runtime disconnected or sent an invalid reply.")
        data.extend(chunk)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise RuntimeUnavailable("The local runtime sent an invalid reply.") from None
    if not isinstance(value, dict) or value.get("ok") is not True:
        # The daemon emits only fixed diagnostics; never trust arbitrary peer strings.
        raise RuntimeUnavailable("The local runtime request failed or its wait/lease limit expired.")
    return value


def _send(connection, value):
    connection.sendall(json.dumps(value).encode() + b"\n")


def ensure_service(*, allow_start=True):
    """Connect; only interactive callers may launch the fixed desktop helper."""
    try:
        return _connect()
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
            raise RuntimeUnavailable("Cannot connect to the local runtime.") from None
    if not allow_start:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
            try:
                return _connect()
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise RuntimeUnavailable("Cannot connect to the local runtime.") from None
        raise RuntimeUnavailable("The desktop local runtime is not running.")
    directory = runtime_dir()
    log = directory / "service.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        _owned_file(log, stat.S_ISREG)
        child = subprocess.Popen(
            [sys.executable, "-m", "aios.local_runtime", "service"],
            stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, close_fds=True, start_new_session=True)
    finally:
        os.close(fd)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            connection = _connect()
            # Reap a losing singleton launcher, without waiting for the winning daemon.
            child.poll()
            return connection
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                raise RuntimeUnavailable("Cannot connect to the local runtime.") from None
        if child.poll() not in (None, 0):
            raise RuntimeUnavailable("The local runtime service could not start.")
        time.sleep(POLL_SECONDS)
    raise RuntimeUnavailable("The local runtime service did not start within five seconds.")


def _acquire(config, background, timeout, operation):
    if type(background) is not bool:
        raise ValueError("background must be a boolean")
    if config.get("mode") != "local":
        raise ValueError("Local admission requires a local model configuration.")
    identity = _model_identity(config.get("model_path"))
    seconds = _seconds(timeout, MAX_WAIT_SECONDS)
    connection = ensure_service(allow_start=not background)
    try:
        connection.settimeout(seconds + 1)
        _send(connection, {"op": operation, "model_path": identity[0],
                           "background": background, "wait_seconds": seconds,
                           "lease_seconds": seconds})
        reply = _receive(connection)
        return connection, reply
    except (OSError, RuntimeUnavailable):
        connection.close()
        raise


@contextmanager
def admission(config, *, background=False, timeout=90):
    """Hold one inference slot through response.close(); yield its hard seconds cap.

    Interactive queued callers always precede background callers (FIFO within
    each class). Active background inference is capped at 60 seconds, active
    interactive inference at 120. Timeout separately bounds queue/loading wait
    and active use, with queue wait capped at 300 seconds. On exceptional exit
    the daemon kills the abandoned generation before granting the next lease.
    """
    connection, reply = _acquire(config, background, timeout, "acquire")
    try:
        yield reply["lease_seconds"]
    except BaseException:
        # No release: the peer observes EOF and cancels the generation.
        raise
    else:
        connection.settimeout(5)
        _send(connection, {"op": "release"})
        _receive(connection)
    finally:
        connection.close()


def ensure_ready(config, *, background=False, timeout=MODEL_START_SECONDS):
    """Load/probe without an inference lease; jobs must pass background=True."""
    connection, _ = _acquire(config, background, timeout, "ready")
    connection.close()


def stop_service():
    """Stop the desktop service and its model, without starting a missing service."""
    try:
        connection = _connect()
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ECONNREFUSED):
            return
        raise
    with connection:
        _send(connection, {"op": "stop"})
        _receive(connection)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("service", "ready", "stop"))
    args = parser.parse_args()
    try:
        if args.operation == "service":
            if os.geteuid() == 0:
                raise RuntimeUnavailable("Run the local runtime as the unprivileged desktop user.")
            service = RuntimeService()
            def stop(_signum, _frame):
                service.stopping = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            service.serve_forever()
        elif args.operation == "stop":
            stop_service()
        else:
            from .core import load_config
            ensure_ready(load_config())
    except (OSError, RuntimeUnavailable, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
