import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from aios.capture_service import Service, decode, allowed_peer, MAX_REQUEST


class Clock:
    def __init__(self):
        self.value = 100.

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def request(action='capture', mode='photo', consumer='1' * 32, **extra):
    value = {'version': 1, 'action': action, 'consumer': consumer, 'request': '2' * 32}
    if action == 'capture':
        value.update(mode=mode, device='', owner='', pin='', consent=False)
    if action == 'configure':
        value.update(active=True, secure=False)
    return {**value, **extra}


class FakeWorker:
    def __init__(self):
        self.code = None
        self.quarantined = False
        self.stdout = Mock()
        self.stdout.read.return_value = None
        self.stdout.fileno.return_value = 99
        self.stdin = Mock()

    def poll(self):
        return self.code

    def kill(self):
        if not self.quarantined:
            self.code = -9

    def wait(self, timeout):
        if self.quarantined:
            raise subprocess.TimeoutExpired('worker', timeout)
        return self.code


class ProtocolTests(unittest.TestCase):
    def test_only_fixed_exact_bounded_operations(self):
        for value in (request(), request('configure'), request('refresh'), request('release')):
            self.assertEqual(decode(json.dumps(value).encode()), value)
        for value in (request(mode=[]), request(device='https://camera'), request(device='/dev/video0'),
                      request(action=[]), request(shell='cat'), request('configure', secure=1),
                      request(pin='1234'), request(mode='enroll', pin='x' * 129), request(version=True)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode(json.dumps(value).encode())
        for raw in (b'{' * (MAX_REQUEST + 1), b'{"version":1,"version":1}', b'{"version":NaN}'):
            with self.assertRaises(ValueError):
                decode(raw)

    def test_peer_requires_parent_pid_and_uid(self):
        left, right = socket.socketpair()
        try:
            self.assertTrue(allowed_peer(left, os.getpid(), os.getuid()))
            self.assertFalse(allowed_peer(left, os.getpid() + 1, os.getuid()))
            self.assertFalse(allowed_peer(left, os.getpid(), os.getuid() + 1))
        finally:
            left.close(); right.close()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.workers = []
        self.devices = {'a' * 64: ('/dev/v4l/by-id/test-video-index0', 1, 81)}
        self.config = {'camera_recognition': False}
        def spawn(*_args, **_kwargs):
            self.assertFalse(any(w.poll() is None for w in self.workers))
            worker = FakeWorker()
            self.workers.append(worker)
            return worker
        self.service = Service(self.events.append, self.clock, spawn, lambda: self.devices.copy(), lambda: self.config)
        self.service.command(request('configure'))
        patcher = patch('aios.capture_service.os.set_blocking')
        patcher.start(); self.addCleanup(patcher.stop)

    def configure(self, **changes):
        self.service.command(request('configure', **changes))

    def test_opt_out_never_starts_background_but_explicit_photo_works(self):
        self.service.tick()
        self.assertEqual(self.workers, [])
        self.service.command(request())
        self.assertEqual(len(self.workers), 1)

    def test_opt_in_cadence_and_immediate_cooldown(self):
        self.config['camera_recognition'] = True
        self.configure()
        self.service.tick()
        self.assertEqual(self.service.job['mode'], 'recognize')
        self.service.result(json.dumps({'kind': 'result', 'sequence': 3, 'captured_at': self.clock(),
                                       'payload': {'state': 'ready', 'suggestion': None}}))
        self.service.command(request(mode='recognize'))
        self.assertEqual(self.events[-1]['reason'], 'cooldown')
        self.clock.advance(14)
        self.service.tick()
        self.assertIsNone(self.service.worker)
        self.clock.advance(1)
        self.service.tick()
        self.assertEqual(len(self.workers), 2)

    def test_failures_back_off_and_device_add_does_not_bypass_opt_in(self):
        self.config['camera_recognition'] = True
        self.configure()
        for delay in (2, 5, 15, 60, 60):
            self.service.tick()
            self.service.fail('timeout')
            self.assertEqual(self.service.schedule.next_capture, self.clock() + delay)
            self.clock.advance(delay)
        self.config['camera_recognition'] = False
        self.configure()
        self.devices['b' * 64] = ('/dev/v4l/by-id/other-video-index0', 2, 82)
        self.service.refresh()
        self.assertFalse(self.service.schedule.due())

    def test_preview_is_preempted_and_never_resumed_implicitly(self):
        self.service.command(request(mode='preview'))
        first = self.workers[-1]
        self.service.command(request(mode='photo', consumer='3' * 32))
        self.assertEqual(first.code, -9)
        self.assertEqual(len(self.workers), 2)
        self.assertEqual(self.service.job['mode'], 'photo')
        self.service.command(request(mode='preview'))
        self.assertEqual(self.events[-1]['reason'], 'busy')
        self.service.command(request('release', consumer='3' * 32))
        self.service.tick()
        self.assertIsNone(self.service.worker)

    def test_other_consumer_cannot_release_explicit_capture(self):
        self.service.command(request(mode='preview'))
        self.service.command(request('release', consumer='3' * 32))
        self.assertIsNotNone(self.service.worker)
        self.service.command(request('release'))
        self.assertIsNone(self.service.worker)

    def test_completed_explicit_job_holds_background_until_owner_releases(self):
        self.config['camera_recognition'] = True
        self.configure()
        self.service.command(request(mode='preview'))
        self.service.result(json.dumps({'kind': 'result', 'sequence': 1, 'captured_at': self.clock(),
                                       'payload': {'state': 'manual-only'}}))
        self.clock.advance(60)
        self.service.tick()
        self.assertIsNone(self.service.worker)
        self.service.command(request('release', consumer='3' * 32))
        self.service.tick()
        self.assertIsNone(self.service.worker)
        self.service.command(request('release'))
        self.service.tick()
        self.assertEqual(self.service.job['mode'], 'recognize')

    def test_gate_and_device_changes_invalidate_generation_and_clear_frames(self):
        self.service.command(request(mode='preview'))
        generation = self.service.generation
        self.service.buffer.extend(b'private frame')
        self.configure(active=False)
        self.assertGreater(self.service.generation, generation)
        self.assertFalse(self.service.buffer)
        self.assertIsNone(self.service.worker)
        self.configure()
        self.service.command(request())
        self.devices.clear()
        self.service.refresh()
        self.assertIsNone(self.service.worker)
        self.assertEqual(self.events[-1]['reason'], 'device_changed')

    def test_deadline_quarantines_unreaped_worker_instead_of_opening_again(self):
        self.service.command(request())
        worker = self.workers[-1]
        worker.quarantined = True
        self.clock.advance(6)
        self.service.tick()
        self.assertIsNone(self.service.job)
        self.assertIs(self.service.worker, worker)
        self.service.command(request())
        self.assertEqual(len(self.workers), 1)
        self.assertEqual(self.events[-1]['reason'], 'busy')
        worker.code = -9
        self.service.tick()
        self.service.command(request())
        self.assertEqual(len(self.workers), 2)

    def test_exited_worker_pipe_is_drained_before_reporting_exit(self):
        self.service.command(request(mode='preview'))
        worker = self.workers[-1]
        worker.code = 0
        raw = json.dumps({'kind': 'result', 'sequence': 1, 'captured_at': 100.,
                          'payload': {'state': 'manual-only'}}).encode() + b'\n'
        worker.stdout.read.side_effect = [raw[:20], raw[20:]]
        self.service.tick()
        self.assertIsNotNone(self.service.job)
        self.service.tick()
        self.assertTrue(any(event['event'] == 'result' for event in self.events))
        self.assertFalse(any(event['reason'] == 'worker_exit' for event in self.events))

    def test_stale_duplicate_oversized_and_private_worker_events_are_rejected(self):
        for event in ({'kind': 'preview', 'sequence': 1, 'captured_at': 1, 'payload': {'image': 'data:image/jpeg;base64,AA=='}},
                      {'kind': 'result', 'sequence': 1, 'captured_at': 100, 'payload': {'embedding': [1, 2]}},
                      {'kind': 'preview', 'sequence': 0, 'captured_at': 100, 'payload': {'image': 'data:image/jpeg;base64,AA=='}},
                      {'kind': 'preview', 'sequence': 1, 'captured_at': float('nan'), 'payload': {}}):
            self.service.command(request(mode='preview'))
            self.assertFalse(self.service.result(json.dumps(event)))
            self.assertIsNone(self.service.worker)
            self.assertNotIn('embedding', json.dumps(self.events))
        self.service.command(request())
        self.workers[-1].stdout.read.return_value = b'x' * 262145
        self.service.tick()
        self.assertEqual(self.events[-1]['reason'], 'oversized')

    def test_purge_preempts_explicit_capture_and_works_without_camera_or_active_desktop(self):
        self.service.command(request())
        self.service.command(request(mode='purge'))
        self.assertEqual(self.service.job['mode'], 'purge')
        self.configure(active=False)
        self.devices.clear()
        self.service.refresh()
        self.service.command(request(mode='purge'))
        self.assertEqual(self.service.job['mode'], 'purge')


@unittest.skipIf(os.geteuid() == 0, 'Production service deliberately rejects root')
class ProcessTests(unittest.TestCase):
    def test_real_authenticated_socket_and_same_uid_unrelated_peer_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            path = str(Path(tmp) / 'camera.sock')
            env = {**os.environ, 'XDG_RUNTIME_DIR': tmp, 'XDG_DATA_HOME': tmp, 'XDG_CONFIG_HOME': tmp}
            server = subprocess.Popen([sys.executable, '-m', 'aios.capture_service', '--socket', path], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not Path(path).exists() and time.monotonic() < deadline:
                    if server.poll() is not None:
                        self.fail(server.stderr.read().decode())
                    time.sleep(.02)
                code = "import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect(" + repr(path) + "); print(repr(s.recv(1)))"
                other = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=3)
                self.assertEqual(other.stdout.strip(), "b''")
                with socket.socket(socket.AF_UNIX) as client:
                    client.settimeout(5); client.connect(path)
                    stream = client.makefile('rb')
                    self.assertEqual(json.loads(stream.readline())['event'], 'state')
                    client.sendall(json.dumps(request(mode='purge')).encode() + b'\n')
                    results = []
                    while len(results) < 8:
                        event = json.loads(stream.readline())
                        results.append(event)
                        if event['event'] == 'result':
                            break
                    self.assertTrue(any(e['payload'] == {'state': 'purged'} for e in results))
                    stream.close()
                server.wait(timeout=5)
                self.assertFalse(Path(path).exists())
            finally:
                if server.poll() is None:
                    server.kill(); server.wait(timeout=5)
                server.stderr.close()
