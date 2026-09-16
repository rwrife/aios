import json
import hashlib
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aios.chat_profiles import dispatch as profile_dispatch
from aios.face_store import FaceStore
from aios.authority import pin_record
from aios.secure_store import atomic_bytes
from aios.recognition import CaptureSchedule, dispatch, enroll, recognize, revoke
from aios.recognition import _manifest, _binding


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
    'minimum_face_size': 80,
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


class ManifestApprovalTests(unittest.TestCase):
    def test_missing_expired_changed_and_unsafe_manifest_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'manifest.json'
            manifest = json.loads(json.dumps(MANIFEST))
            digest = hashlib.sha256(json.dumps(_binding(manifest, CALIBRATION), sort_keys=True,
                                              separators=(',', ':')).encode()).hexdigest()
            manifest['approval'] = {'status': 'approved', 'binding_sha256': digest, 'expires_at': 200.}
            path.write_text(json.dumps(manifest))
            with patch('aios.recognition.time.time', return_value=100.):
                self.assertEqual(_manifest(path)[0], manifest)
                for change in ({'status': 'pending'}, {'expires_at': 99.}, {'binding_sha256': '0' * 64}):
                    modified = {**manifest, 'approval': {**manifest['approval'], **change}}
                    path.write_text(json.dumps(modified))
                    with self.assertRaisesRegex(ValueError, 'approval'):
                        _manifest(path)
                path.write_text(json.dumps({**manifest, 'calibration': {**CALIBRATION, 'match_threshold': .9}}))
                with self.assertRaisesRegex(ValueError, 'approval'):
                    _manifest(path)
                path.write_text(json.dumps(MANIFEST))
                with self.assertRaisesRegex(ValueError, 'approval'):
                    _manifest(path)
                path.write_text(json.dumps(manifest)); path.chmod(0o666)
                with self.assertRaisesRegex(ValueError, 'permissions'):
                    _manifest(path)
class RecognitionStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = profile_dispatch({
            'action': 'enroll_manual', 'name': 'Alice', 'pin': '1234', 'consent': True
        }, self.root)['profile']

    def configured(self):
        stack = ExitStack()
        stack.enter_context(patch('aios.recognition._manifest', return_value=(MANIFEST, CALIBRATION)))
        stack.enter_context(patch('aios.recognition.FaceEncoder'))
        stack.enter_context(patch('aios.recognition.load_config', return_value={
            'camera_recognition': True, 'camera_device': '/dev/v4l/by-id/test-video-index0'}))
        return stack

    def samples(self, *_):
        return [{'embedding': [1.] + [0.] * 127} for _ in range(3)]

    def test_missing_consent_or_non_uuid_never_captures(self):
        with self.configured():
            for owner, consent in ((self.profile['id'], False), ('Alice', True)):
                with self.assertRaises(ValueError):
                    enroll(owner, '1234', self.root, capture=self.samples, consent=consent)
        self.assertEqual(FaceStore(self.root).snapshot()[1], {})

    def test_locked_account_does_not_capture(self):
        path = self.root / 'profiles.json'
        records = json.loads(path.read_text())
        records[self.profile['id']]['pin']['failures'] = 10
        atomic_bytes(path, json.dumps(records).encode())
        with self.configured(), self.assertRaisesRegex(ValueError, 'locked'):
            enroll(self.profile['id'], '1234', self.root, capture=self.samples, consent=True)
        self.assertFalse((self.root / 'biometrics').exists())

    def test_rechecks_pin_immediately_before_commit(self):
        def change_pin(*_):
            path = self.root / 'profiles.json'
            records = json.loads(path.read_text())
            records[self.profile['id']]['pin'] = pin_record('5678')
            atomic_bytes(path, json.dumps(records).encode())
            return self.samples()
        with self.configured(), self.assertRaisesRegex(ValueError, 'PIN'):
            enroll(self.profile['id'], '1234', self.root, capture=change_pin, consent=True)
        self.assertEqual(FaceStore(self.root).snapshot()[1], {})

    def test_purge_during_capture_cannot_be_undone_by_late_enrollment(self):
        def purge(*_):
            revoke(root=self.root)
            return self.samples()
        with self.configured(), self.assertRaisesRegex(ValueError, 'changed'):
            enroll(self.profile['id'], '1234', self.root, capture=purge, consent=True)
        self.assertEqual(FaceStore(self.root).snapshot()[1], {})

    def test_account_deleted_during_capture_cannot_receive_a_template(self):
        def delete(*_):
            profile_dispatch({'action': 'delete_profile', 'owner': self.profile['id'],
                              'pin': '1234', 'confirmed': True}, self.root)
            return self.samples()
        with self.configured(), self.assertRaisesRegex(ValueError, 'PIN'):
            enroll(self.profile['id'], '1234', self.root, capture=delete, consent=True)
        self.assertEqual(FaceStore(self.root).snapshot()[1], {})

    def test_interrupted_account_delete_leaves_profile_without_template(self):
        with self.configured():
            enroll(self.profile['id'], '1234', self.root, capture=self.samples, consent=True)
        writes = []
        def interrupt(path, payload):
            writes.append(path)
            if len(writes) == 2:
                raise RuntimeError('simulated interruption before account commit')
            atomic_bytes(path, payload)
        with patch('aios.chat_profiles.atomic_bytes', side_effect=interrupt), self.assertRaises(RuntimeError):
            profile_dispatch({'action': 'delete_profile', 'owner': self.profile['id'],
                              'pin': '1234', 'confirmed': True}, self.root)
        self.assertEqual(FaceStore(self.root).snapshot()[1], {})
        profiles = profile_dispatch({'action': 'profiles'}, self.root)['profiles']
        self.assertEqual(profiles[0]['id'], self.profile['id'])

    def test_rejected_samples_preserve_previous_committed_enrollment(self):
        with self.configured():
            enroll(self.profile['id'], '1234', self.root, capture=self.samples, consent=True)
            epoch, records = FaceStore(self.root).snapshot()
            cases = [[], self.samples()[:2], [{'embedding': [float('nan')] * 128}] * 3,
                     [{'embedding': [0.] * 128}] * 3,
                     [{'embedding': [1.] + [0.] * 127}, {'embedding': [0., 1.] + [0.] * 126},
                      {'embedding': [0., 0., 1.] + [0.] * 125}]]
            for samples in cases:
                with self.subTest(samples=len(samples)), self.assertRaises(ValueError):
                    enroll(self.profile['id'], '1234', self.root, capture=lambda *_: samples, consent=True)
                self.assertEqual(FaceStore(self.root).snapshot(), (epoch, records))

    def test_disabling_preserves_templates(self):
        with patch('aios.recognition.revoke') as purge, \
                patch('aios.recognition.load_config', return_value={'camera_recognition': True}), \
                patch('aios.recognition.save_config') as save:
            self.assertEqual(dispatch({'action': 'disable'}), {'state': 'disabled'})
            purge.assert_not_called()
            save.assert_called_once_with({'camera_recognition': False})

    def test_unknown_ambiguous_conflicting_and_deleted_results_have_no_candidate(self):
        second = profile_dispatch({'action': 'enroll_manual', 'name': 'Bob', 'pin': '5678', 'consent': True}, self.root)['profile']
        other_samples = lambda *_: [{'embedding': [0., 1.] + [0.] * 126} for _ in range(3)]
        with self.configured(), patch('aios.recognition.video_device'):
            enroll(self.profile['id'], '1234', self.root, capture=self.samples, consent=True)
            enroll(second['id'], '5678', self.root, capture=other_samples, consent=True)
            unknown = lambda *_: [{'embedding': [0., 0., 1.] + [0.] * 125} for _ in range(3)]
            conflict = lambda *_: [self.samples()[0], other_samples()[0], self.samples()[0]]
            for samples in (unknown, conflict):
                result = recognize(self.root, capture=samples)
                self.assertIsNone(result['suggestion'])
                self.assertEqual(set(result), {'state', 'suggestion', 'reason'})
            enroll(second['id'], '5678', self.root, capture=self.samples, consent=True)
            self.assertIsNone(recognize(self.root, capture=self.samples)['suggestion'])
            revoke(second['id'], self.root)
            def deleted(*_):
                revoke(self.profile['id'], self.root)
                return self.samples()
            result = recognize(self.root, capture=deleted)
            self.assertIsNone(result['suggestion'])
            self.assertEqual(result['reason'], 'configuration-changed')

    def test_uuid_pin_selection_cannot_fall_back_to_a_reused_display_name(self):
        profile_dispatch({'action': 'delete_profile', 'owner': self.profile['id'], 'pin': '1234', 'confirmed': True}, self.root)
        profile_dispatch({'action': 'enroll_manual', 'name': self.profile['id'], 'pin': '1234', 'consent': True}, self.root)
        with self.assertRaisesRegex(ValueError, 'PIN'):
            profile_dispatch({'action': 'activate_profile', 'owner': self.profile['id'], 'pin': '1234'}, self.root)

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
