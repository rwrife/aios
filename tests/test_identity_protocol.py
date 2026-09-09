import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest

from aios.biometrics import Tracker, verified_model
from aios.isolation import SimulatorIsolation
from aios.session_client import request
from aios.sessiond import Service, serve
from aios.sessions import Sessions
from test_identity_sessions import Clock, MemoryStore


def run_service(root):
    sessions = Sessions(SimulatorIsolation(Path(root) / 'workspaces'), MemoryStore(),
                        anonymous_timeout=.5)
    serve(Path(root) / 'session.sock', Service(sessions, os.getuid(), os.getuid()))


@unittest.skipUnless(os.name == 'posix', 'Linux Unix credentials required')
class SocketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.socket = Path(self.tmp.name) / 'session.sock'
        self.process = multiprocessing.get_context('fork').Process(target=run_service, args=(self.tmp.name,))
        self.process.start()
        self.addCleanup(self.stop)
        for _ in range(100):
            if self.socket.exists():
                break
            time.sleep(.01)
        self.assertTrue(self.socket.exists())

    def stop(self):
        self.process.terminate()
        self.process.join(3)
        if self.process.is_alive():
            self.process.kill()
            self.process.join()

    def test_real_roundtrip_permissions_and_idle_cleanup(self):
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        reply = request(self.socket, {'action': 'anonymous'})
        self.assertTrue(reply['ok'])
        self.assertTrue(list((Path(self.tmp.name) / 'workspaces').iterdir()))
        time.sleep(.8)
        self.assertEqual(list((Path(self.tmp.name) / 'workspaces').iterdir()), [])

    def test_malformed_request_does_not_break_next_client(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(self.socket))
            client.sendall(b'{"action":"status","uid":0}\n')
            self.assertFalse(json.loads(client.recv(4096))['ok'])
        self.assertTrue(request(self.socket, {'action': 'status'})['ok'])

    def test_slow_client_has_bounded_deadline(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(self.socket))
            client.sendall(b'{')
            started = time.monotonic()
            self.assertFalse(json.loads(client.recv(4096))['ok'])
            self.assertLess(time.monotonic() - started, .8)
        self.assertTrue(request(self.socket, {'action': 'status'})['ok'])


class ModelIntegrityTests(unittest.TestCase):
    def test_hash_checked_before_loading(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'model.onnx'
            path.write_bytes(b'model bytes')
            record = dict(path=str(path.resolve()), sha256=hashlib.sha256(b'model bytes').hexdigest(),
                          source='https://example.invalid/model', license='review-required', revision='test',
                          input='test', output='test')
            self.assertEqual(verified_model({'model': record}, 'model'), str(path.resolve()))
            path.write_bytes(b'tampered')
            with self.assertRaises(ValueError):
                verified_model({'model': record}, 'model')

    def test_tracker_does_not_resume_expired_track(self):
        clock = Clock()
        tracker = Tracker(clock)
        detection = [([0, 0, 100, 100], [1, 0])]
        first = tracker.update(detection)[0][0]
        clock.advance(2)
        second, stable, _ = tracker.update(detection)[0]
        self.assertNotEqual(first, second)
        self.assertFalse(stable)


class EncryptionTests(unittest.TestCase):
    def test_ciphertext_authentication_and_record_binding(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            self.skipTest('Optional cryptography package is not installed')
        from aios.secure_store import EncryptedStore
        with tempfile.TemporaryDirectory() as root:
            store = EncryptedStore(root, AESGCM.generate_key(bit_length=256))
            store.put('alice', {'secret': 'never in plaintext'})
            payload = (Path(root) / 'alice.enc').read_bytes()
            self.assertNotIn(b'never in plaintext', payload)
            self.assertEqual(store.get('alice')['secret'], 'never in plaintext')
            (Path(root) / 'bob.enc').write_bytes(payload)
            from cryptography.exceptions import InvalidTag
            with self.assertRaises(InvalidTag):
                store.get('bob')
            (Path(root) / 'alice.enc').write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
            with self.assertRaises(InvalidTag):
                store.get('alice')
