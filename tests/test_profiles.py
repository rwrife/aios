import base64
import json
import struct
import tempfile
import unittest
import zlib

from aios.isolation import SimulatorIsolation
from aios.portraits import portrait
from aios.sessions import Sessions
from aios.sessiond import Service
from test_identity_sessions import Clock, MemoryStore, evidence


class ProfileTests(unittest.TestCase):
    def test_portrait_only_accepts_fixed_raw_pixels_and_emits_metadata_free_png(self):
        pixels = bytes([120, 80, 40]) * 4096
        encoded = portrait(base64.b64encode(pixels).decode())
        png = base64.b64decode(encoded.split(',')[1])
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        offset, kinds = 8, []
        while offset < len(png):
            length = struct.unpack('>I', png[offset:offset+4])[0]
            kind = png[offset+4:offset+8]
            kinds.append(kind)
            payload = png[offset+8:offset+8+length]
            if kind == b'IDAT':
                self.assertEqual(zlib.decompress(payload), (b'\0' + bytes([120, 80, 40])*64)*64)
            offset += length + 12
        self.assertEqual(kinds, [b'IHDR', b'IDAT', b'IEND'])
        for value in ('https://example.com/photo', '/private/image.png', '!'*16384, 'A'*16380, 5):
            with self.assertRaises(ValueError):
                portrait(value)

    def test_greeting_does_not_unlock_and_stale_photo_disappears(self):
        with tempfile.TemporaryDirectory() as root:
            clock, store = Clock(), MemoryStore()
            sessions = Sessions(SimulatorIsolation(root), store, clock, clock)
            rgb = base64.b64encode(bytes([42])*12288).decode()
            owner = sessions.enroll('Alice', '123456', True, None, rgb)['identity']
            self.assertEqual(sessions.profiles(), [{'id': owner, 'name': 'Alice'}])
            self.assertEqual(set(sessions.profiles()[0]), {'id', 'name'})
            sessions.evidence([evidence(owner)])
            clock.advance(1.1)
            sessions.evidence([evidence(owner)])
            profile = sessions.profile_status()
            self.assertEqual(profile['name'], 'Alice')
            self.assertTrue(profile['photo'].startswith('data:image/png;base64,'))
            self.assertIsNone(sessions.owner)
            clock.advance(4)
            self.assertEqual(sessions.profile_status()['photo'], '')
            self.assertEqual(sessions.profile_status()['name'], '')
            sessions.activate_verified('Alice', '123456')
            self.assertEqual(sessions.profile_status()['name'], 'Alice')
            self.assertEqual(sessions.profile_status()['photo'], '')
            sessions.suspend()
            service = Service(sessions, 1000, 1001)
            self.assertEqual(service.dispatch({'action': 'status'}, 1000)['profile'], {})
            with self.assertRaises(PermissionError):
                service.dispatch({'action': 'profiles'}, 1000)
            self.assertNotIn('pin', json.dumps(sessions.profiles()))
