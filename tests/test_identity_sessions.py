import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from aios.authority import Capabilities, pin_record, verify_pin
from aios.identity import Fusion, match
from aios.isolation import SimulatorIsolation, application
from aios.sessiond import Service, decode
from aios.sessions import Sessions


class Clock:
    def __init__(self):
        self.now = 1000.0
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += seconds


class MemoryStore:
    def __init__(self):
        self.records = {}
    def get(self, key, default=None):
        return json.loads(json.dumps(self.records.get(key, default)))
    def put(self, key, value):
        self.records[key] = json.loads(json.dumps(value))


def evidence(owner, voice=None, **extra):
    return dict(track='one', face=owner, voice=voice or owner, face_strength='strong',
                stable=True, interacting=True, active_speaker=True, live=True, **extra)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = MemoryStore()
        self.isolation = SimulatorIsolation(self.tmp.name)
        self.s = Sessions(self.isolation, self.store, self.clock, self.clock)
        samples = {'face': [[1.] * 16] * 3, 'voice': [[1.] * 16] * 3}
        self.a = self.s.enroll('Alice', '123456', True, samples)['identity']
        self.b = self.s.enroll('Bob', '987654', True, samples)['identity']
        self.addCleanup(lambda: self.s.suspend() if not self.s.fault else None)

    def recognize(self, owner):
        self.s.evidence([evidence(owner)])
        self.clock.advance(1.1)
        self.s.evidence([evidence(owner)])

    def test_interrupted_enrollment_reconciles_committed_workspace(self):
        original = self.store.put
        def fail_record(key, value):
            if key.startswith('identity-'):
                raise OSError('simulated power loss')
            original(key, value)
        with patch.object(self.store, 'put', side_effect=fail_record):
            with self.assertRaises(OSError):
                self.s.enroll('Carol', '135790', True, None)
        pending = self.store.get('pending-enrollment')
        owner = pending['identity']
        self.assertNotIn('135790', json.dumps(pending))
        self.assertNotIn(owner, self.store.get('identities'))
        restarted = Sessions(self.isolation, self.store, self.clock, self.clock)
        self.assertEqual(self.store.get('identities')[owner], 'Carol')
        self.assertIsNone(self.store.get('pending-enrollment'))
        restarted.activate_verified('Carol', '135790')
        restarted.suspend()

    def test_incomplete_allocation_is_not_adopted_or_reformatted(self):
        with patch.object(self.isolation, 'provision', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.s.enroll('Carol', '135790', True, None)
        pending = self.store.get('pending-enrollment')
        restarted = Sessions(self.isolation, self.store, self.clock, self.clock)
        self.assertTrue(restarted.enrollment_blocked)
        with patch.object(self.isolation, 'provision') as provision:
            with self.assertRaises(PermissionError):
                restarted.enroll('Dave', '135790', True, None)
            provision.assert_not_called()
        self.assertEqual(self.store.get('pending-enrollment'), pending)
        restarted.activate_verified('Alice', '123456')
        restarted.suspend()

    def test_recovery_rotates_secret_and_revokes_active_session(self):
        profile = self.s.enroll('Carol', '135790', True, None)
        self.s.activate_verified('Carol', '135790', 'Private notes')
        work = self.s.work
        challenge = self.s.request_capability('secrets.github.profile', 'github')
        token = self.s.verify(challenge['id'], '135790', False)
        replacement = self.s.recover('Carol', profile['recovery'], '246802')
        self.assertIsNone(self.s.owner)
        self.assertNotIn(token, self.s.capabilities.tokens)
        with self.assertRaises(PermissionError):
            self.s.recover('Carol', profile['recovery'], '999999')
        self.s.activate_verified('Carol', '246802', session=work)
        self.assertEqual(self.s.work, work)
        self.s.suspend()
        self.assertNotEqual(replacement, profile['recovery'])
        with self.assertRaises(PermissionError):
            Service(self.s, 1000, 1001).dispatch(
                {'action': 'recover', 'owner': 'Carol', 'recovery': replacement, 'pin': '123456'}, 1000)
        self.s.recover('Carol', replacement, '123456')

    def activate(self, owner=None):
        self.recognize(owner or self.a)
        return self.s.activate('Résumé')

    def token(self, operation='secrets.github.profile', resource='github', confirmed=False):
        challenge = self.s.request_capability(operation, resource)
        return self.s.verify(challenge['id'], '123456', confirmed)

    def test_recognition_does_not_attribute_anonymous_work(self):
        self.recognize(self.a)
        self.s.launch('calculator', [])
        self.assertIsNone(self.s.owner)
        self.assertIsNone(self.s.journal)
        self.assertEqual(self.s.status()['authority'], 'anonymous')

    def test_durable_resume_and_owner_isolation(self):
        session = self.activate()
        root_a = self.s.root
        (root_a / 'artifacts' / 'Resume.txt').write_text('Alice private draft')
        self.s.launch('editor', ['Resume.txt'])
        self.s.message('user', 'Draft a résumé')
        self.s.suspend()
        self.activate(self.b)
        self.assertNotEqual(self.s.root, root_a)
        self.assertFalse((self.s.root / 'artifacts' / 'Resume.txt').exists())
        with self.assertRaises(PermissionError):
            self.s.journal.get(session)
        self.s.suspend()
        self.recognize(self.a)
        self.s.activate(session=session)
        self.assertEqual((self.s.root / 'artifacts' / 'Resume.txt').read_text(), 'Alice private draft')
        self.assertEqual(self.isolation.launches[-1][3], 'editor')

    def test_privacy_then_suspension(self):
        work = self.activate()
        token = self.token()
        self.clock.advance(4)
        self.s.tick()
        self.assertTrue(self.s.shield)
        self.assertNotIn(work, self.isolation.stopped)
        self.assertFalse(self.s.capabilities.tokens)
        with self.assertRaises(PermissionError):
            self.s.use(token, 'secrets.github.profile', 'github')
        self.clock.advance(30)
        self.s.tick()
        self.assertIn(work, self.isolation.stopped)
        self.assertIsNone(self.s.owner)

    def test_history_survives_resume_and_denies_absent_owner(self):
        session = self.activate()
        for index in range(25):
            self.s.message('user', str(index))
        self.s.summarize('Résumé drafting history')
        self.s.suspend()
        self.recognize(self.a)
        self.s.activate(session=session)
        page = self.s.history()
        self.assertEqual([m['content'] for m in page['messages']], [str(i) for i in range(5, 25)])
        older = self.s.history(page['before'])
        self.assertEqual([m['content'] for m in older['messages']], [str(i) for i in range(5)])
        self.assertIsNone(older['before'])
        self.assertEqual(self.s.list_work('drafting')[0]['id'], session)
        self.clock.advance(4)
        with self.assertRaises(PermissionError):
            self.s.history()

    def test_recent_sessions_need_no_throwaway_work_session(self):
        session = self.activate()
        self.s.suspend()
        self.recognize(self.a)
        self.assertEqual([item['id'] for item in self.s.list_work('')], [session])
        self.assertIsNone(self.s.work)
        self.assertEqual(self.s.owner, self.a)
        with self.assertRaises(PermissionError):
            self.s.launch('calculator', [])
        self.s.suspend()
        self.recognize(self.b)
        self.assertEqual(self.s.list_work(''), [])

    def test_document_save_resume_and_cross_owner_denial(self):
        session = self.activate()
        saved = self.s.document('Resume.txt', 'Private résumé')
        self.s.suspend()
        self.activate(self.b)
        with self.assertRaises(FileNotFoundError):
            self.s.document('Resume.txt')
        self.s.suspend()
        self.recognize(self.a)
        self.s.activate(session=session)
        self.assertEqual(self.s.document('Resume.txt')['sha256'], saved['sha256'])
        self.clock.advance(4)
        with self.assertRaises(PermissionError):
            self.s.document('Resume.txt', 'overwrite')

    def test_manual_enrollment_and_pin_fallback_without_sensors(self):
        enrolled = self.s.enroll('Manual user', '456789', True, None)
        owner = enrolled['identity']
        self.assertFalse(self.store.get('identity-' + owner)['biometric_consent'])
        with self.assertRaises(PermissionError):
            self.s.activate_verified(owner, 'wrong', title='Private document')
        self.clock.advance(3)
        session = self.s.activate_verified(owner, '456789', title='Private document')
        self.assertEqual(self.s.status()['authority'], 'verified')
        self.s.document('Resume.txt', 'Saved without camera')
        self.clock.advance(60)
        self.s.activate(session=session)
        self.assertEqual(self.s.document('Resume.txt')['content'], 'Saved without camera')
        self.clock.advance(61)
        with self.assertRaises(PermissionError):
            self.s.document('Resume.txt')
        self.assertTrue(self.s.shield or self.s.owner is None)

    def test_sensor_conflict_revokes_manual_pin_session(self):
        self.s.activate_verified(self.a, '123456', title='Manual')
        self.s.evidence([evidence(self.a, self.b)])
        self.assertTrue(self.s.shield)
        self.assertIsNone(self.s.manual_owner)
        with self.assertRaises(PermissionError):
            self.s.activate_verified(self.a, '123456', title='Conflict cannot be bypassed')

    def test_pin_unlock_opens_catalog_by_name_without_new_session(self):
        session = self.activate()
        self.s.suspend()
        self.s.fusion.feed([])
        self.assertIsNone(self.s.activate_verified('Alice', '123456'))
        self.assertEqual([item['id'] for item in self.s.list_work('')], [session])
        self.assertIsNone(self.s.work)

    def test_lost_trusted_shell_locks_even_with_valid_pin_lease(self):
        self.s.activate_verified(self.a, '123456', title='Private')
        with patch('aios.sessiond.time.monotonic', return_value=100):
            service = Service(self.s, 1000, 1001, personal_enabled=True)
        with patch('aios.sessiond.time.monotonic', return_value=104):
            service.tick()
        self.assertIsNone(self.s.owner)

    def test_embedded_personal_gate_requires_direct_display_and_registered_process(self):
        self.isolation.requires_display = True
        service = Service(self.s, 1000, 1001, personal_enabled=True)
        service.dispatch({'action': 'display_attest', 'platform': 'xcb', 'embedded': True}, 1000, 10)
        self.assertFalse(service.dispatch({'action': 'status'}, 1000, 10)['personal_available'])
        with self.assertRaises(PermissionError):
            service.dispatch({'action': 'enroll_manual', 'name': 'Other', 'pin': '123456', 'consent': True}, 1000, 10)
        service.dispatch({'action': 'display_attest', 'platform': 'eglfs', 'embedded': True}, 1000, 10)
        self.assertTrue(service.dispatch({'action': 'status'}, 1000, 10)['personal_available'])
        self.s.activate_verified(self.a, '123456', title='Protected')
        with self.assertRaises(PermissionError):
            service.dispatch({'action': 'history', 'before': None}, 1000, 11)
        self.assertIsNone(service.dispatch({'action': 'status'}, 1000, 11)['session'])
        service.dispatch({'action': 'display_attest', 'platform': 'offscreen', 'embedded': True}, 1000, 11)
        self.assertIsNone(self.s.owner)

    def test_history_unicode_page_fits_response(self):
        self.activate()
        for _ in range(3):
            self.s.message('assistant', '\U0001f642' * 32768)
        page = self.s.history()
        self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 262000)
        self.assertEqual(len(page['messages']), 1)
        self.assertIsNotNone(page['before'])
        with self.assertRaises(ValueError):
            self.s.history(True)

    def test_conflict_immediately_revokes_and_never_retargets(self):
        self.activate()
        self.token()
        self.s.evidence([evidence(self.a, self.b)])
        self.assertTrue(self.s.shield)
        self.assertEqual(self.s.owner, self.a)
        self.assertFalse(self.s.capabilities.tokens)
        self.recognize(self.b)
        with self.assertRaises(PermissionError):
            self.s.activate('Bob task')

    def test_capability_scope_single_use_and_expiry(self):
        self.activate()
        token = self.token()
        with self.assertRaises(PermissionError):
            self.s.use(token, 'financial.read', 'github')
        self.s.use(token, 'secrets.github.profile', 'github')
        with self.assertRaises(PermissionError):
            self.s.use(token, 'secrets.github.profile', 'github')
        token = self.token('financial.read', 'account-1')
        self.clock.advance(121)
        self.recognize(self.a)
        with self.assertRaises(PermissionError):
            self.s.use(token, 'financial.read', 'account-1')

    def test_transaction_requires_exact_confirmation(self):
        self.activate()
        with self.assertRaises(PermissionError):
            self.token('message.send', 'draft-1')
        token = self.token('message.send', 'draft-1', True)
        with self.assertRaises(PermissionError):
            self.s.use(token, 'message.send', 'draft-2')
        self.s.use(token, 'message.send', 'draft-1')

    def test_anonymous_idle_cleanup(self):
        self.s.anonymous()
        root, lease = self.s.root, self.s.lease
        self.clock.advance(121)
        self.s.tick()
        self.assertFalse(root.exists())
        self.assertIn(lease, self.isolation.stopped)

    def test_enrollment_name_cannot_take_over(self):
        with self.assertRaises(PermissionError):
            self.s.enroll('ALICE', '555555', True, {})

    def test_wrong_pin_counters_survive_new_session_object(self):
        self.activate()
        challenge = self.s.request_capability('financial.read', 'account')
        with self.assertRaises(PermissionError):
            self.s.verify(challenge['id'], '000000', False)
        self.assertEqual(self.store.get('identity-' + self.a)['pin']['failures'], 1)
        with self.assertRaises(PermissionError):
            self.token('financial.read', 'account')
        self.clock.advance(2)
        self.recognize(self.a)
        self.assertTrue(self.token('financial.read', 'account'))

    def test_socket_roles_and_production_simulator_denial(self):
        service = Service(self.s, 1001, 1002)
        with self.assertRaises(PermissionError):
            service.dispatch({'action': 'evidence', 'tracks': []}, 1001)
        with self.assertRaises(PermissionError):
            service.dispatch({'action': 'simulate', 'state': 'user-a'}, 1001)
        with self.assertRaises(PermissionError):
            service.dispatch({'action': 'status'}, 1003)
        with self.assertRaises(PermissionError):
            service.dispatch({'action': 'activate', 'title': 'test', 'session': None}, 1001)

    def test_failed_cleanup_blocks_new_principal(self):
        self.activate()
        def fail(*_):
            raise RuntimeError('busy mount')
        self.isolation.release = fail
        with self.assertRaises(RuntimeError):
            self.s.suspend()
        self.assertTrue(self.s.fault)
        self.assertTrue(self.s.shield)
        self.recognize(self.b)
        with self.assertRaises(PermissionError):
            self.s.activate('Bob')

    def test_restart_recovers_active_session_as_suspended(self):
        from aios.journal import Journal
        session = self.activate()
        self.s.journal.close()
        self.s.journal = Journal(self.s.root, self.a)
        self.assertEqual(self.s.journal.get(session)['status'], 'suspended')


class BoundaryTests(unittest.TestCase):
    def test_pin_minimum_length(self):
        for value in ('123', 'abcdef', 'abcdefghi', '12345\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pin_record(value)
        self.assertTrue(pin_record('longer passphrase'))
        self.assertTrue(verify_pin(pin_record('1234'), '1234', 0))

    def test_schema_rejects_extra_duplicate_and_nonfinite_fields(self):
        for value in ('{"action":"status","uid":0}', '{"action":"status","action":"status"}',
                      '{"action":"simulate","state":NaN}', '[]'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode(value)

    def test_launch_rejects_commands_and_traversal(self):
        for app, args in [('sh', []), ('terminal', ['-e']), ('editor', ['../private']),
                          ('editor', ['/etc/shadow']), ('editor', ['x\\y']), ('editor', ['-x'])]:
            with self.subTest(app=app, args=args), self.assertRaises(ValueError):
                application(app, args)

    def test_pin_lockout(self):
        record = pin_record('123456')
        for i in range(10):
            self.assertFalse(verify_pin(record, '000000', i * 1000))
        self.assertFalse(verify_pin(record, '123456', 999999))

    def test_multiple_faces_and_stale_evidence(self):
        clock = Clock()
        fusion = Fusion(clock)
        a = '00000000-0000-4000-8000-000000000001'
        b = '00000000-0000-4000-8000-000000000002'
        one, two = evidence(a), evidence(b)
        two['track'] = 'two'
        fusion.feed([one, two])
        clock.advance(2)
        self.assertIsNone(fusion.current())
        fusion.feed([one])
        clock.advance(1.1)
        self.assertEqual(fusion.current(), a)
        clock.advance(4)
        fusion.feed([one])
        self.assertIsNone(fusion.current())

    def test_liveness_and_interaction_are_required(self):
        clock = Clock()
        fusion = Fusion(clock, dwell=0)
        one = evidence('00000000-0000-4000-8000-000000000001')
        for field in ('live', 'interacting', 'stable'):
            record = {**one, field: False}
            self.assertIsNone(fusion.feed([record]))

    def test_ambiguous_embedding_match(self):
        self.assertIsNone(match([1., 0.], {'a': [[1., 0.]], 'b': [[1., .01]]}, .8, .1))


if __name__ == '__main__':
    unittest.main()
