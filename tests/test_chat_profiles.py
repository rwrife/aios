import json
from pathlib import Path
import tempfile
import unittest
from aios.chat_profiles import dispatch


class ChatProfileTests(unittest.TestCase):
    def test_create_verify_and_reject_wrong_pin_without_returning_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = dispatch({'action': 'enroll_manual', 'name': 'Alice', 'pin': '1234', 'consent': True}, root)
            self.assertEqual(created['profile']['name'], 'Alice')
            self.assertNotIn('1234', (root / 'profiles.json').read_text())
            self.assertNotIn('pin', json.dumps(created))
            self.assertEqual(dispatch({'action': 'profiles'}, root)['profiles'][0]['name'], 'Alice')
            good = dispatch({'action': 'activate_verified', 'owner': 'alice', 'pin': '1234'}, root)
            self.assertEqual(good['profile']['id'], created['profile']['id'])
            with self.assertRaises(ValueError):
                dispatch({'action': 'activate_verified', 'owner': 'Alice', 'pin': '654321'}, root)
            with self.assertRaises(ValueError):
                dispatch({'action': 'activate_verified', 'owner': 'Alice', 'pin': '1234'}, root)
            self.assertEqual(json.loads((root / 'profiles.json').read_text())[created['profile']['id']]['pin']['failures'], 1)
            with self.assertRaises(ValueError):
                dispatch({'action': 'enroll_manual', 'name': 'ALICE', 'pin': '1234', 'consent': True}, root)
