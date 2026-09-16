import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aios.chat_profiles import dispatch as profile_dispatch
from aios.face_store import FaceStore
from aios.recognition import CaptureSchedule, dispatch, enroll, recognize, revoke


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


CALIBRATION = {
    'hardware': 'brio-101', 'id': 'test-calibration-v1',
    'match_threshold': .8,
    'runner_up_margin': .1,
    'enrollment_consistency': .8,
    'minimum_brightness': 20,
    'maximum_brightness': 240,
    'minimum_sharpness': 10,
}
MANIFEST = {
    'sface': {'revision': 'test-model', 'sha256': 'a' * 64},
    'yunet': {'revision': 'test-detector', 'sha256': 'b' * 64},
    'calibration': CALIBRATION,
}


class RecognitionScheduleTests(unittest.TestCase):
    def test_opt_in_cadence_cooldown_backoff_and_device_reset(self):
        clock = Clock()
        schedule = CaptureSchedule(clock)
        self.assertFalse(schedule.due())
        schedule.configure(True)
        generation = schedule.start()
        self.assertIsNotNone(generation)
        schedule.finish(generation, True)
        self.assertFalse(schedule.due())
        clock.advance(14)
        self.assertFalse(schedule.due())
        clock.advance(1)
        generation = schedule.start()
        schedule.finish(generation, False)
        clock.advance(1)
        self.assertFalse(schedule.due())
        clock.advance(1)
        self.assertTrue(schedule.due())
        schedule.device_added()
        self.assertTrue(schedule.due())

    def test_disable_and_activity_change_invalidate_inflight_capture(self):
        clock = Clock()
        schedule = CaptureSchedule(clock)
        schedule.configure(True)
        generation = schedule.start()
        schedule.configure(False)
        schedule.finish(generation, True)
        self.assertFalse(schedule.due(immediate=True))
        schedule.configure(True)
        schedule.set_active(False)
        self.assertFalse(schedule.due(immediate=True))


class RecognitionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = profile_dispatch({
            'action': 'enroll_manual', 'name': 'Alice', 'pin': '1234', 'consent': True
        }, self.root)['profile']

    def test_enrollment_requires_pin_and_stores_only_encrypted_versioned_templates(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest('Optional cryptography package is not installed')
        capture = lambda *_: [
            {'embedding': [1.0, 0.0] + [0.] * 126}, {'embedding': [.99, .01] + [0.] * 126}, {'embedding': [.98, .02] + [0.] * 126}
        ]
        with patch('aios.recognition._manifest', return_value=(MANIFEST, CALIBRATION)), \
                patch('aios.recognition.FaceEncoder'), \
                patch('aios.recognition.load_config', return_value={
                    'camera_recognition': True,
                    'camera_device': '/dev/v4l/by-id/test-video-index0'
                }), patch('aios.chat_profiles.time.time', side_effect=iter(range(100, 200, 3))):
            with self.assertRaisesRegex(ValueError, 'PIN'):
                enroll(self.profile['id'], '9999', self.root, capture=capture, consent=True)
            result = enroll(self.profile['id'], '1234', self.root, capture=capture, consent=True)
        self.assertEqual(result, {'enrolled': self.profile['id']})
        payload = next((self.root / 'biometrics').rglob('templates.enc')).read_bytes()
        self.assertNotIn(b'test-model', payload)
        self.assertNotIn(b'1.0', payload)

    def test_recognition_returns_metadata_only_and_deletion_revokes_template(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest('Optional cryptography package is not installed')
        samples = lambda *_: [
            {'embedding': [1.0, 0.0] + [0.] * 126}, {'embedding': [.99, .01] + [0.] * 126}, {'embedding': [.98, .02] + [0.] * 126}
        ]
        config = {'camera_recognition': True,
                  'camera_device': '/dev/v4l/by-id/test-video-index0'}
        with patch('aios.recognition._manifest', return_value=(MANIFEST, CALIBRATION)), \
                patch('aios.recognition.FaceEncoder'), \
                patch('aios.recognition.load_config', return_value=config), \
                patch('aios.recognition.video_device', return_value=config['camera_device']):
            enroll(self.profile['id'], '1234', self.root, capture=samples, consent=True)
            result = recognize(self.root, capture=samples)
        self.assertEqual(result['suggestion']['id'], self.profile['id'])
        self.assertEqual(set(result['suggestion']),
                         {'id', 'name', 'photo', 'confidence', 'expires_in'})
        self.assertNotIn('embedding', json.dumps(result))
        profile_dispatch({
            'action': 'delete_profile', 'owner': self.profile['id'],
            'pin': '1234', 'confirmed': True,
        }, self.root)
        self.assertEqual(FaceStore(self.root).snapshot()[1], {})

    def test_global_purge_removes_orphaned_legacy_templates_without_a_key(self):
        records = self.root / 'recognition-records'
        records.mkdir()
        orphan = records / 'face-template-orphan.enc'
        orphan.write_bytes(b'opaque')
        revoke(root=self.root)
        self.assertFalse(orphan.exists())

    def test_purge_action_deletes_templates_without_changing_configuration(self):
        with patch('aios.recognition.revoke') as revoke_templates:
            self.assertEqual(dispatch({'action': 'purge'}), {'state': 'purged'})
        revoke_templates.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
