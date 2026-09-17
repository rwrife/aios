import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aios.chat_profiles import dispatch


class ChatProfileTests(unittest.TestCase):
    def test_delete_requires_target_pin_confirmation_and_preserves_other_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alice = dispatch({'action': 'enroll_manual', 'name': 'Alice', 'pin': '1234', 'consent': True}, root)['profile']['id']
            bob = dispatch({'action': 'enroll_manual', 'name': 'Bob', 'pin': '5678', 'consent': True}, root)['profile']['id']
            request = {'action': 'delete_profile', 'owner': alice, 'pin': '5678', 'confirmed': True}
            with patch('aios.chat_profiles.time.time', return_value=100):
                with self.assertRaises(ValueError): dispatch(request, root)
            self.assertEqual(len(dispatch({'action': 'profiles'}, root)['profiles']), 2)
            request['pin'] = '1234'
            with patch('aios.chat_profiles.time.time', return_value=103):
                with self.assertRaises(ValueError): dispatch({**request, 'confirmed': False}, root)
                self.assertEqual(dispatch(request, root), {'deleted': alice})
            self.assertEqual(dispatch({'action': 'profiles'}, root)['profiles'],
                             [{'id': bob, 'name': 'Bob', 'photo': ''}])
            with self.assertRaises(ValueError): dispatch(request, root)

    def test_create_verify_and_reject_wrong_pin_without_returning_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = dispatch({'action': 'enroll_manual', 'name': 'Alice', 'pin': '1234', 'consent': True}, root)
            self.assertEqual(created['profile']['name'], 'Alice')
            stored = json.loads((root / 'profiles.json').read_text())[created['profile']['id']]
            self.assertNotEqual(stored['pin']['digest'], '1234')
            self.assertNotEqual(stored['pin']['salt'], '1234')
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

    def test_unlocked_profile_photo_can_be_added(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = dispatch({'action': 'enroll_manual', 'name': 'Alice', 'pin': '1234', 'consent': True}, root)
            owner = created['profile']['id']
            rgb = base64.b64encode(bytes([42]) * 12288).decode()
            updated = dispatch({'action': 'update_profile_photo', 'owner': owner, 'photo': rgb}, root)
            self.assertEqual(updated['profile']['id'], owner)
            self.assertTrue(updated['profile']['photo'].startswith('data:image/png;base64,'))
            self.assertEqual(dispatch({'action': 'profiles'}, root)['profiles'][0]['photo'], updated['profile']['photo'])
