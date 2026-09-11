"""Display-independent local runtime protocol, admission, and lifecycle tests."""

from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import select
import socket
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

from aios import local_runtime as runtime


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for the runtime test condition")


class FakeModel:
    def __init__(self):
        self.identity = None
        self.starts = []
        self.stops = 0
        self.ready = True
        self.crashed = False

    def start(self, identity):
        if self.identity is not None:
            self.stop()
        self.identity = identity
        self.starts.append(identity)
        self.crashed = False

    def stop(self):
        if self.identity is not None:
            self.stops += 1
        self.identity = None

    def check(self):
        if self.crashed:
            raise runtime.RuntimeUnavailable("The local model stopped.")
        return self.ready


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(".local-runtime-test-" + uuid.uuid4().hex[:8]).resolve()
        self.directory.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        self.model_path = self.directory / "model.gguf"
        self.model_path.write_bytes(b"GGUF fixture")
        self.config = {"mode": "local", "model_path": str(self.model_path)}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # DrvFS worktrees do not preserve Unix permissions. Exercise the real
        # IPC/locking there; permission rejection is tested separately below.
        self.stack.enter_context(patch.object(runtime, "runtime_dir", return_value=self.directory))
        self.stack.enter_context(patch.object(runtime, "_owned_file"))
        native_socket = socket.socket
        directory = self.directory

        class WorkspaceSocket(native_socket):
            # DrvFS also lacks pathname sockets. Keep real kernel Unix IPC and
            # peer credentials, translating only this test's endpoint to the
            # Linux abstract namespace; all fixture files stay in the worktree.
            def bind(self, address):
                if self.family == socket.AF_UNIX:
                    Path(address).touch()
                    address = "\0" + directory.name
                return super().bind(address)

            def connect(self, address):
                if self.family == socket.AF_UNIX:
                    address = "\0" + directory.name
                return super().connect(address)

        self.stack.enter_context(patch.object(runtime.socket, "socket", WorkspaceSocket))
        self.model = FakeModel()
        self.service = runtime.RuntimeService(model=self.model)
        self.errors = []

        def run():
            try:
                self.service.serve_forever()
            except Exception as exc:
                self.errors.append(exc)

        self.thread = threading.Thread(target=run)
        self.thread.start()
        self.addCleanup(self.finish)
        wait_for(lambda: bool(self.service.selector.get_map()) or self.errors)
        self.assertEqual(self.errors, [])
        self.ping()

    def finish(self):
        self.service.stopping = True
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "runtime service did not shut down")
        self.assertEqual(self.errors, [])

    def ping(self):
        with runtime._connect() as connection:
            runtime._send(connection, {"op": "ping"})
            self.assertTrue(runtime._receive(connection)["ok"])

    def acquire(self, *, background=False, seconds=2, model_path=None, operation="acquire"):
        connection = runtime._connect()
        self.addCleanup(connection.close)
        runtime._send(connection, {"op": operation, "model_path": str(model_path or self.model_path),
                                   "background": background, "wait_seconds": seconds,
                                   "lease_seconds": seconds})
        return connection

    def release(self, connection):
        runtime._send(connection, {"op": "release"})
        self.assertTrue(runtime._receive(connection)["ok"])
        connection.close()

    def test_headless_readiness_and_chat_share_single_model(self):
        runtime.ensure_ready(self.config)
        with runtime.admission(self.config) as seconds:
            self.assertEqual(seconds, 90)
            self.assertIsNotNone(self.service.active)
        with runtime.admission(self.config, background=True) as seconds:
            self.assertEqual(seconds, runtime.MAX_BACKGROUND_SECONDS)
        self.assertEqual(len(self.model.starts), 1)
        self.assertEqual(self.model.stops, 0)

    def test_interactive_overtakes_queued_background_fifo(self):
        active = self.acquire(background=True)
        runtime._receive(active)
        background = self.acquire(background=True)
        interactive1 = self.acquire()
        interactive2 = self.acquire()
        wait_for(lambda: sum(c.state == "queued" for c in self.service.clients.copy().values()) == 3)
        self.release(active)
        runtime._receive(interactive1)
        self.assertFalse(self.service.active.background)
        self.release(interactive1)
        runtime._receive(interactive2)
        self.release(interactive2)
        runtime._receive(background)
        self.assertTrue(self.service.active.background)
        self.release(background)
        self.assertEqual(len(self.model.starts), 1)

    def test_disconnect_cancels_active_generation_before_next_lease(self):
        first = self.acquire()
        runtime._receive(first)
        waiting = self.acquire()
        first.close()
        runtime._receive(waiting)
        self.assertEqual(self.model.stops, 1)
        self.assertEqual(len(self.model.starts), 2)
        self.release(waiting)

    def test_context_exception_abandons_generation(self):
        with self.assertRaisesRegex(ValueError, "test failure"):
            with runtime.admission(self.config):
                raise ValueError("test failure")
        wait_for(lambda: self.model.stops == 1)
        with runtime.admission(self.config):
            self.assertEqual(len(self.model.starts), 2)

    def test_expired_active_lease_restarts_before_interactive(self):
        first = self.acquire(background=True, seconds=0.12)
        runtime._receive(first)
        interactive = self.acquire()
        runtime._receive(interactive)
        self.assertEqual(self.model.stops, 1)
        self.assertEqual(len(self.model.starts), 2)
        with self.assertRaises(runtime.RuntimeUnavailable):
            runtime._receive(first)
        self.release(interactive)

    def test_queued_timeout_does_not_interrupt_active(self):
        first = self.acquire()
        runtime._receive(first)
        queued = self.acquire(seconds=0.1)
        with self.assertRaises(runtime.RuntimeUnavailable):
            runtime._receive(queued)
        self.assertEqual(self.model.stops, 0)
        self.assertIsNotNone(self.service.active)
        self.release(first)

    def test_disconnected_queued_client_does_not_restart_model(self):
        first = self.acquire()
        runtime._receive(first)
        queued = self.acquire()
        wait_for(lambda: len(self.service.clients) == 2)
        queued.close()
        wait_for(lambda: len(self.service.clients) == 1)
        self.release(first)
        self.assertEqual(self.model.stops, 0)

    def test_model_path_change_waits_for_current_response(self):
        other = self.directory / "other.gguf"
        other.write_bytes(b"GGUF another")
        first = self.acquire()
        runtime._receive(first)
        queued = self.acquire(model_path=other)
        wait_for(lambda: len(self.service.clients) == 2)
        self.assertEqual(self.model.identity[0], str(self.model_path))
        self.release(first)
        runtime._receive(queued)
        self.assertEqual(self.model.identity[0], str(other))
        self.assertEqual(len(self.model.starts), 2)
        self.release(queued)

    def test_replaced_model_file_reloads_same_path(self):
        runtime.ensure_ready(self.config)
        self.model_path.write_bytes(b"GGUF replacement model with different size")
        runtime.ensure_ready(self.config)
        self.assertEqual(len(self.model.starts), 2)
        self.assertNotEqual(self.model.starts[0], self.model.starts[1])

    def test_failed_start_reports_error_then_recovers(self):
        with patch.object(self.model, "start", side_effect=runtime.RuntimeUnavailable("Cannot load model")):
            with self.assertRaises(runtime.RuntimeUnavailable):
                runtime.ensure_ready(self.config)
        runtime.ensure_ready(self.config)
        self.assertEqual(len(self.model.starts), 1)

    def test_idle_crash_reaped_and_next_request_recovers(self):
        runtime.ensure_ready(self.config)
        self.model.crashed = True
        wait_for(lambda: self.model.identity is None)
        runtime.ensure_ready(self.config)
        self.assertEqual(len(self.model.starts), 2)

    def test_active_crash_fails_lease_and_recovers(self):
        connection = self.acquire()
        runtime._receive(connection)
        self.model.crashed = True
        with self.assertRaises(runtime.RuntimeUnavailable):
            runtime._receive(connection)
        runtime.ensure_ready(self.config)
        self.assertEqual(len(self.model.starts), 2)

    def test_loading_does_not_block_ping_or_interactive_priority(self):
        self.model.ready = False
        background = self.acquire(background=True)
        wait_for(lambda: len(self.model.starts) == 1)
        interactive = self.acquire()
        wait_for(lambda: len(self.service.clients) == 2)
        self.ping()
        self.model.ready = True
        runtime._receive(interactive)
        self.assertFalse(self.service.active.background)
        self.release(interactive)
        runtime._receive(background)
        self.release(background)

    def test_model_loading_timeout_is_bounded(self):
        self.model.ready = False
        with self.assertRaises(runtime.RuntimeUnavailable):
            runtime.ensure_ready(self.config, timeout=0.1)
        self.assertIsNone(self.service.active)

    def test_singleton_does_not_remove_live_socket(self):
        other = runtime.RuntimeService(model=FakeModel())
        try:
            self.assertFalse(other.open())
        finally:
            other.close()
        self.ping()
        self.assertTrue((self.directory / "service.sock").exists())

    def test_stale_socket_reclaimed_after_lock_owner_exits(self):
        runtime.stop_service()
        self.thread.join(timeout=3)
        (self.directory / "service.sock").touch()
        replacement = runtime.RuntimeService(model=FakeModel())
        try:
            self.assertTrue(replacement.open())
            self.assertTrue((self.directory / "service.sock").exists())
        finally:
            replacement.close()
        self.assertFalse((self.directory / "service.sock").exists())

    def test_headless_launcher_uses_fixed_detached_module(self):
        connection = runtime._connect()
        self.addCleanup(connection.close)
        with patch.object(runtime, "_connect", side_effect=[
                FileNotFoundError(errno.ENOENT, "missing"), connection]), \
                patch.object(runtime.subprocess, "Popen") as launcher:
            launcher.return_value.poll.return_value = None
            self.assertIs(runtime.ensure_service(), connection)
        self.assertEqual(launcher.call_args.args[0],
                         [sys.executable, "-m", "aios.local_runtime", "service"])
        self.assertTrue(launcher.call_args.kwargs["start_new_session"])
        self.assertTrue(launcher.call_args.kwargs["close_fds"])
        self.assertNotIn("shell", launcher.call_args.kwargs)

    def test_background_worker_never_launches_service_under_its_guardian(self):
        with patch.object(runtime, "_connect", side_effect=FileNotFoundError(errno.ENOENT, "missing")), \
                patch.object(runtime.subprocess, "Popen") as launcher:
            with self.assertRaisesRegex(runtime.RuntimeUnavailable, "desktop local runtime"):
                runtime.ensure_ready(self.config, background=True)
            with self.assertRaisesRegex(runtime.RuntimeUnavailable, "desktop local runtime"):
                with runtime.admission(self.config, background=True):
                    self.fail("A background request started without the desktop service")
            launcher.assert_not_called()

    def test_worker_process_death_releases_lease(self):
        source = (
            "import json,socket,sys,time; "
            "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); "
            "s.connect('\\0'+sys.argv[1]); "
            "s.sendall((json.dumps({'op':'acquire','model_path':sys.argv[2],"
            "'background':True,'wait_seconds':3,'lease_seconds':3})+'\\n').encode()); "
            "print(s.recv(8192).decode(),flush=True); time.sleep(60)"
        )
        worker = subprocess.Popen([sys.executable, "-c", source, self.directory.name, str(self.model_path)],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([worker.stdout], [], [], 3)[0])
            self.assertTrue(json.loads(worker.stdout.readline())["ok"])
            interactive = self.acquire()
            worker.kill()
            worker.wait(timeout=3)
            runtime._receive(interactive)
            self.assertEqual(self.model.stops, 1)
            self.assertEqual(len(self.model.starts), 2)
            self.release(interactive)
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=3)
            worker.stdout.close()

    def test_stop_closes_active_and_queued_and_model(self):
        first = self.acquire()
        runtime._receive(first)
        queued = self.acquire()
        runtime.stop_service()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        for connection in (first, queued):
            with self.assertRaises(runtime.RuntimeUnavailable):
                runtime._receive(connection)
        self.assertEqual(self.model.stops, 1)
        self.assertFalse((self.directory / "service.sock").exists())
        runtime.stop_service()

    def test_rejects_invalid_protocol_and_explicit_background_type(self):
        values = [
            {"op": "exec", "command": "untrusted"},
            {"op": "acquire", "model_path": str(self.model_path), "background": "false",
             "wait_seconds": 5, "lease_seconds": 5},
            {"op": "acquire", "model_path": str(self.model_path), "background": False,
             "wait_seconds": float("nan"), "lease_seconds": 5},
            {"op": "ready", "model_path": str(self.model_path), "background": False,
             "wait_seconds": 5, "lease_seconds": 5, "command": "untrusted"},
        ]
        for value in values:
            with self.subTest(value=value), runtime._connect() as connection:
                runtime._send(connection, value)
                with self.assertRaises(runtime.RuntimeUnavailable):
                    runtime._receive(connection)
        self.assertEqual(self.model.starts, [])

    def test_rejects_oversized_message(self):
        with runtime._connect() as connection:
            connection.sendall(b"x" * (runtime.MAX_MESSAGE_BYTES + 1))
            with self.assertRaises(runtime.RuntimeUnavailable):
                runtime._receive(connection)

    def test_client_rejects_invalid_configuration_before_launcher(self):
        with patch.object(runtime, "ensure_service") as launcher:
            for config, background, timeout in (
                ({"mode": "remote"}, False, 10), (self.config, "false", 10),
                (self.config, False, -1), ({"mode": "local", "model_path": ""}, False, 10),
            ):
                with self.subTest(config=config, background=background, timeout=timeout):
                    with self.assertRaises((ValueError, runtime.RuntimeUnavailable)):
                        with runtime.admission(config, background=background, timeout=timeout):
                            self.fail("Invalid input admitted")
            launcher.assert_not_called()


class PrivacyTests(unittest.TestCase):
    def test_runtime_requires_absolute_private_xdg_directory(self):
        for value in ("", "relative"):
            with self.subTest(value=value), patch.dict(os.environ, {"XDG_RUNTIME_DIR": value}):
                with self.assertRaises(runtime.RuntimeUnavailable):
                    runtime.runtime_dir()

    def test_rejects_directory_symlink_wrong_owner_or_permissions(self):
        for mode, uid in ((stat.S_IFLNK | 0o700, os.getuid()),
                          (stat.S_IFDIR | 0o700, os.getuid() + 1),
                          (stat.S_IFDIR | 0o777, os.getuid()),
                          (stat.S_IFREG | 0o700, os.getuid())):
            with self.subTest(mode=mode, uid=uid):
                path = MagicMock()
                path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
                with self.assertRaises(runtime.RuntimeUnavailable):
                    runtime._private_directory(path)
        path.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid())
        runtime._private_directory(path)

    def test_rejects_socket_symlink_wrong_owner_or_permissions(self):
        for mode, uid in ((stat.S_IFLNK | 0o600, os.getuid()),
                          (stat.S_IFSOCK | 0o600, os.getuid() + 1),
                          (stat.S_IFSOCK | 0o666, os.getuid())):
            with self.subTest(mode=mode, uid=uid):
                path = MagicMock()
                path.lstat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
                with self.assertRaises(runtime.RuntimeUnavailable):
                    runtime._owned_file(path, stat.S_ISSOCK)

    def test_fixed_model_command_and_loopback_single_slot(self):
        model = runtime.ModelProcess()
        identity = ("/models/model with spaces.gguf", 1, 2, 3, 4)
        with patch.object(runtime.os.path, "isfile", return_value=True), \
                patch.object(runtime.os, "access", return_value=True), \
                patch.object(runtime.socket, "socket") as sock, \
                patch.object(runtime.subprocess, "Popen") as popen:
            sock.return_value.__enter__.return_value.connect_ex.return_value = errno.ECONNREFUSED
            model.start(identity)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "/usr/local/bin/llama-server")
        self.assertEqual(argv[argv.index("--model") + 1], identity[0])
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--parallel") + 1], "1")
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        self.assertIn("preexec_fn", popen.call_args.kwargs)

    def test_rejects_foreign_listener_before_model_launch(self):
        model = runtime.ModelProcess()
        with patch.object(runtime.os.path, "isfile", return_value=True), \
                patch.object(runtime.os, "access", return_value=True), \
                patch.object(runtime.socket, "socket") as sock, \
                patch.object(runtime.subprocess, "Popen") as popen:
            sock.return_value.__enter__.return_value.connect_ex.return_value = 0
            with self.assertRaises(runtime.RuntimeUnavailable):
                model.start(("/models/model.gguf", 1))
            popen.assert_not_called()

    def test_model_termination_escalates_and_reaps(self):
        model = runtime.ModelProcess()
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("llama", 2), 0]
        model.process = process
        model.stop()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertIsNone(model.process)

    def test_model_start_deadline_and_crash(self):
        model = runtime.ModelProcess()
        model.process = MagicMock()
        model.process.poll.return_value = None
        model.started = time.monotonic() - runtime.MODEL_START_SECONDS - 1
        with self.assertRaisesRegex(runtime.RuntimeUnavailable, "startup limit"):
            model.check()
        model.process.poll.return_value = 1
        with self.assertRaisesRegex(runtime.RuntimeUnavailable, "stopped"):
            model.check()

    def test_parent_death_signal_really_kills_model_child(self):
        supervisor = subprocess.Popen(
            [sys.executable, "-c",
             "import os,subprocess,sys,time; from aios.local_runtime import _die_with_parent; "
             "parent=os.getpid(); "
             "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
             "preexec_fn=lambda:_die_with_parent(parent)); "
             "print(p.pid,flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, text=True)
        child_pid = int(supervisor.stdout.readline())
        try:
            supervisor.kill()
            supervisor.wait(timeout=3)

            def dead():
                path = Path(f"/proc/{child_pid}/stat")
                try:
                    return path.read_text().split(") ", 1)[1].split()[0] == "Z"
                except FileNotFoundError:
                    return True
            wait_for(dead)
        finally:
            supervisor.stdout.close()
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=3)
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main()
