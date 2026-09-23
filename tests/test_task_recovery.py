"""Owner-scoped task recovery contract tests (issue #167)."""
from unittest import mock
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from aios import task_recovery
from aios.task_recovery import Conflict, RecoveryError, TaskStore, UnknownTask


class Clock:
    def __init__(self):
        self.value = 1000000.0

    def __call__(self):
        return self.value


class TaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'tasks'
        self.open_stores = []

    def store(self, owner=None, clock=None):
        store = TaskStore(self.root, owner or str(uuid.uuid4()), clock=clock or Clock())
        self.open_stores.append(store)
        return store

    def task(self, store, title='Summarize the release notes', **kwargs):
        return store.create(title, prompt='visit the URL and summarize',
                            provider='local', model='tester-1',
                            capabilities_=('browser.read',), **kwargs)

    def crash(self, store):
        """Simulate process death: close the connection without close()."""
        store.db.close()
        self.open_stores.remove(store)

    def test_create_starts_first_attempt_and_survives_reopen(self):
        store = self.store()
        made = self.task(store)
        self.assertEqual(made['status'], 'active')
        self.assertEqual(made['attempt'], 1)
        store.complete(made['id'], 'summary delivered')
        store.close()
        reopened = self.store(store.owner)
        revived = reopened.get(made['id'])
        self.assertEqual(revived['status'], 'completed')
        self.assertEqual([a['outcome'] for a in reopened.attempts(made['id'])],
                         ['completed'])

    def test_invalid_input_fails_closed(self):
        store = self.store()
        with self.assertRaises(RecoveryError):
            store.create('')
        with self.assertRaises(RecoveryError):
            store.create('a' * 300)
        with self.assertRaises(RecoveryError):
            store.create('ok', capabilities_=['browser read'])
        with self.assertRaises(RecoveryError):
            store.create('ok', prompt={'not': 'text'})

    def test_status_transitions_are_gated(self):
        store = self.store()
        made = self.task(store)
        store.complete(made['id'])
        with self.assertRaises(Conflict):
            store.complete(made['id'])
        store.archive(made['id'])
        with self.assertRaises(Conflict):
            store.complete(made['id'])
        # An archived task resumes only with a valid revalidated binding.
        with self.assertRaises(RecoveryError):
            store.resume(made['id'], 'local', 'tester-1', ['browser read'])

    def test_search_and_open_lists_are_owner_filtered(self):
        alice, bob = self.store(), self.store()
        secret = self.task(alice, title='Reconcile the household budget')
        self.task(bob, title='Track the harvest calendar')
        hits = alice.search('budget')
        self.assertEqual([item['id'] for item in hits], [secret['id']])
        self.assertEqual(alice.search('harvest'), [])
        self.assertEqual(bob.search('budget'), [])
        with self.assertRaises(UnknownTask):
            bob.get(secret['id'])
        self.assertEqual([t['id'] for t in alice.list_open()], [secret['id']])

    # ------------------------------------------------------- crash recovery

    def test_crash_interrupts_active_work_but_not_finished_work(self):
        store = self.store()
        crashed = self.task(store, title='Draft the migration note')
        finished = self.task(store, title='Already delivered')
        paused = self.task(store, title='Paused for user')
        store.checkpoint(crashed['id'], 'gathered inputs', 'page contents staged')
        store.complete(finished['id'])
        store.needs_user_action(paused['id'], 'confirm the address')
        self.crash(store)
        revived = self.store(store.owner)
        self.assertEqual(revived.get(crashed['id'])['status'], 'interrupted')
        self.assertEqual(revived.get(finished['id'])['status'], 'completed')
        self.assertEqual(revived.get(paused['id'])['status'], 'needs_user_action')

    def test_pending_effect_becomes_unverified_and_blocks_resume(self):
        store = self.store()
        made = self.task(store)
        effect = store.begin_effect(made['id'], 'purchase', 'domain:example.com',
                                    {'sku': 'domain-renewal'})
        store.commit_effect(made['id'], effect['id'])
        risky = store.begin_effect(made['id'], 'message', 'chat:#ops', {'text': 'go'})
        with self.assertRaises(Conflict):
            store.complete(made['id'])  # pending effect prevents completion
        self.crash(store)
        revived = self.store(store.owner)
        self.assertEqual(revived.get(made['id'])['status'], 'interrupted')
        self.assertEqual(revived.get(made['id'])['unverified_effects'], 1)
        view = revived.resume(made['id'], 'local', 'tester-1', ['browser.read'])
        self.assertEqual(view['status'], 'needs_user_action')
        self.assertEqual([e['id'] for e in view['unverified']], [risky['id']])
        self.assertEqual(revived.get(made['id'])['status'], 'needs_user_action')
        # The already-executed effect stays recorded as executed; it is never
        # queued again for the resumed attempt.
        executed = revived.effects(made['id'], ('executed',))
        self.assertEqual([e['id'] for e in executed], [effect['id']])

    def test_resolution_skipped_permits_retry_but_completed_does_not(self):
        store = self.store()
        made = self.task(store)
        first = store.begin_effect(made['id'], 'email', 'to:partner', {'body': 'quote'})
        self.crash(store)
        revived = self.store(store.owner)
        # User confirms the email did NOT go out -> retry allowed.
        revived.resolve_effect(made['id'], first['id'], 'skipped', 'mail log empty')
        view = revived.resume(made['id'], 'local', 'tester-1', ['browser.read'])
        self.assertEqual(view['status'], 'resumed')
        retry = revived.begin_effect(made['id'], 'email', 'to:partner', {'body': 'quote'})
        self.assertEqual(retry['id'], first['id'])  # same journal row re-armed
        revived.commit_effect(made['id'], retry['id'])
        with self.assertRaises(Conflict):
            revived.begin_effect(made['id'], 'email', 'to:partner', {'body': 'quote'})

    def test_resolution_completed_blocks_duplicate_forever(self):
        store = self.store()
        made = self.task(store)
        effect = store.begin_effect(made['id'], 'purchase', 'domain:example.com',
                                    {'sku': 'renewal'})
        self.crash(store)
        revived = self.store(store.owner)
        revived.resolve_effect(made['id'], effect['id'], 'completed', 'receipt found')
        view = revived.resume(made['id'], 'local', 'tester-1', ['browser.read'])
        self.assertEqual(view['status'], 'resumed')
        self.assertEqual([e['id'] for e in view['executed_effects']], [effect['id']])
        with self.assertRaises(Conflict):
            revived.begin_effect(made['id'], 'purchase', 'domain:example.com',
                                 {'sku': 'renewal'})

    def test_failed_effect_can_be_retried_immediately(self):
        store = self.store()
        made = self.task(store)
        effect = store.begin_effect(made['id'], 'email', 'to:partner', {'body': 'x'})
        store.fail_effect(made['id'], effect['id'], 'smtp refused')
        retry = store.begin_effect(made['id'], 'email', 'to:partner', {'body': 'x'})
        self.assertEqual(retry['id'], effect['id'])
        store.commit_effect(made['id'], retry['id'])
        with self.assertRaises(Conflict):
            store.resolve_effect(made['id'], effect['id'], 'skipped')

    def test_recovery_report_is_explicit(self):
        store = self.store()
        made = self.task(store)
        store.begin_effect(made['id'], 'message', 'chat:#ops', {'text': 'ship'})
        self.crash(store)
        revived = self.store(store.owner)
        report = revived.recovery
        self.assertEqual(len(report['interrupted_attempts']), 1)
        self.assertEqual(report['interrupted_tasks'], [made['id']])
        self.assertEqual(len(report['unverified_effects']), 1)

    def test_restart_after_recovery_does_not_re_report(self):
        store = self.store()
        self.task(store)
        store.begin_effect(self.task(store)['id'], 'message', 'chat:#ops', {'text': 's'})
        self.crash(store)
        revived = self.store(store.owner)
        self.assertEqual(len(revived.recovery['unverified_effects']), 1)
        self.crash(revived)
        again = self.store(store.owner)
        self.assertEqual(again.recovery, {'interrupted_attempts': [],
                                          'interrupted_tasks': [], 'unverified_effects': []})

    # -------------------------------------------------------------- resume

    def test_resume_reconstructs_without_regeneration(self):
        store = self.store()
        made = self.task(store)
        for step in range(25):
            store.checkpoint(made['id'], f'step {step}', 'context ' + str(step))
        store.record_artifact(made['id'], 'document', 'artifact:doc-1', 'a' * 64)
        store.record_artifact(made['id'], 'tool', 'tool:csv-join', None)
        effect = store.begin_effect(made['id'], 'fetch', 'url:release-notes', {})
        store.commit_effect(made['id'], effect['id'])
        self.crash(store)
        revived = self.store(store.owner)
        view = revived.resume(made['id'], 'local', 'tester-1', ['browser.read'])
        self.assertEqual(view['status'], 'resumed')
        self.assertEqual(view['attempt'], 2)
        self.assertEqual([a['outcome'] for a in revived.attempts(made['id'])],
                         ['interrupted', 'running'])
        self.assertEqual([e['id'] for e in view['executed_effects']], [effect['id']])
        self.assertEqual({a['ref'] for a in view['artifacts']},
                         {'artifact:doc-1', 'tool:csv-join'})
        self.assertLessEqual(len(view['checkpoints']), 20)
        self.assertEqual(view['checkpoints'][-1]['step'], 'step 24')

    def test_resume_requires_revalidation_then_rejects_changed_binding(self):
        store = self.store()
        made = self.task(store)
        self.crash(store)
        revived = self.store(store.owner)
        view = revived.resume(made['id'])
        self.assertEqual(view['status'], 'needs_user_action')
        self.assertIn('Revalidate', view['reason'])
        stale = revived.resume(made['id'], 'local', 'tester-2', ['browser.read'])
        self.assertEqual(stale['status'], 'needs_user_action')
        self.assertEqual(stale['required'], ['model'])
        missing_caps = revived.resume(made['id'], 'local', 'tester-1', ['files.read'])
        self.assertIn('capabilities', missing_caps['required'])

    def test_resume_of_interrupted_with_changed_model_lands_needs_action(self):
        store = self.store()
        made = self.task(store)
        self.crash(store)
        revived = self.store(store.owner)
        view = revived.resume(made['id'], 'remote', 'other-model', ['browser.read'])
        self.assertEqual(view['status'], 'needs_user_action')
        self.assertEqual(sorted(view['required']), ['model', 'provider'])
        self.assertEqual(revived.get(made['id'])['status'], 'needs_user_action')

    def test_resume_never_replays_executed_effects(self):
        store = self.store()
        made = self.task(store)
        done = store.begin_effect(made['id'], 'fetch', 'url:a', {})
        store.commit_effect(made['id'], done['id'])
        self.crash(store)
        revived = self.store(store.owner)
        view = revived.resume(made['id'], 'local', 'tester-1', ['browser.read'])
        self.assertEqual([e['id'] for e in view['executed_effects']], [done['id']])
        with self.assertRaises(Conflict):
            revived.begin_effect(made['id'], 'fetch', 'url:a', {})

    def test_resume_after_needs_user_action_flow(self):
        store = self.store()
        made = self.task(store)
        store.needs_user_action(made['id'], 'payment method expired')
        store.close()
        reopened = self.store(store.owner)
        view = reopened.resume(made['id'], 'local', 'tester-1', ['browser.read'])
        self.assertEqual(view['status'], 'resumed')
        self.assertEqual(reopened.get(made['id'])['status'], 'active')
        self.assertEqual(reopened.get(made['id'])['attempt'], 2)

    # ---------------------------------------------------------- artifacts

    def test_checkpoint_mutation_requires_active_attempt(self):
        store = self.store()
        made = self.task(store)
        store.complete(made['id'])
        with self.assertRaises(Conflict):
            store.checkpoint(made['id'], 'late note')
        fresh = self.task(store)
        with self.assertRaises(RecoveryError):
            store.record_artifact(fresh['id'], 'archive', 'artifact:1')
        with self.assertRaises(RecoveryError):
            store.record_artifact(fresh['id'], 'document', 'artifact:1', 'zz' * 32)

    def test_artifact_reference_rejects_secret_shaped_values(self):
        store = self.store()
        made = self.task(store)
        for bad in ('api_key=abc123', 'line\nbreak', 'x' * 500, '', '   '):
            with self.assertRaises(RecoveryError, msg=repr(bad)):
                store.record_artifact(made['id'], 'document', bad)

    def test_history_page_is_bounded(self):
        store = self.store()
        made = self.task(store)
        for step in range(30):
            store.checkpoint(made['id'], f'step {step}')
        history = store.checkpoint_history(made['id'], limit=5)
        self.assertEqual([item['seq'] for item in history], [26, 27, 28, 29, 30])
        with self.assertRaises(RecoveryError):
            store.checkpoint_history(made['id'], limit=99)

    def test_checkpoint_pruning_bounds_storage(self):
        store = self.store()
        made = self.task(store)
        original = task_recovery.MAX_CHECKPOINTS_PER_TASK
        task_recovery.MAX_CHECKPOINTS_PER_TASK = 10
        try:
            for step in range(40):
                store.checkpoint(made['id'], f'step {step}')
        finally:
            task_recovery.MAX_CHECKPOINTS_PER_TASK = original
        kept = store.checkpoint_history(made['id'], limit=50)
        self.assertEqual(len(kept), 10)
        self.assertEqual(kept[-1]['step'], 'step 39')

    # ------------------------------------------------------------ effects

    def test_effect_argument_changes_do_not_dedupe(self):
        store = self.store()
        made = self.task(store)
        first = store.begin_effect(made['id'], 'email', 'to:partner', {'body': 'v1'})
        with self.assertRaises(Conflict):
            store.begin_effect(made['id'], 'email', 'to:partner', {'body': 'v1'})
        second = store.begin_effect(made['id'], 'email', 'to:partner', {'body': 'v2'})
        self.assertNotEqual(first['id'], second['id'])

    def test_unresolved_effect_cap_blocks_runaway_journaling(self):
        store = self.store()
        made = self.task(store)
        for index in range(task_recovery.MAX_EPOCH_EFFECTS):
            store.begin_effect(made['id'], 'write', f'row:{index}', {})
        with self.assertRaises(RecoveryError):
            store.begin_effect(made['id'], 'write', 'row:overflow', {})

    def test_effect_state_machine_is_gated(self):
        store = self.store()
        made = self.task(store)
        effect = store.begin_effect(made['id'], 'fetch', 'url:a', {})
        with self.assertRaises(Conflict):
            store.resolve_effect(made['id'], effect['id'], 'skipped')  # not unverified
        store.commit_effect(made['id'], effect['id'])
        with self.assertRaises(Conflict):
            store.commit_effect(made['id'], effect['id'])  # already executed
        with self.assertRaises(RecoveryError):
            store.resolve_effect(made['id'], effect['id'], 'maybe')

    def test_cancel_marks_pending_effects_unverified(self):
        store = self.store()
        made = self.task(store)
        store.begin_effect(made['id'], 'message', 'chat:#ops', {'text': 'go'})
        store.cancel(made['id'], 'user aborted')
        self.assertEqual({e['state'] for e in store.effects(made['id'])}, {'unverified'})
        with self.assertRaises(Conflict):
            store.resume(made['id'], 'local', 'tester-1', ['browser.read'])

    # ------------------------------------------------------------ ephemeral

    def test_ephemeral_tasks_require_ttl_and_expire(self):
        clock = Clock()
        store = TaskStore(self.root, str(uuid.uuid4()), clock=clock)

        def die():
            store.db.close()
        self.addCleanup(die)
        with self.assertRaises(RecoveryError):
            store.create('guest task', ephemeral=True)
        for bad_ttl in ('3600', 0, 31 * 86400, True, None):
            with self.assertRaises(RecoveryError, msg=repr(bad_ttl)):
                store.create('guest task', ephemeral=True, ttl=bad_ttl)
        guest = store.create('guest task', ephemeral=True, ttl=3600)
        clock.value += 3601
        self.assertEqual(store.expire(), 1)
        with self.assertRaises(UnknownTask):
            store.get(guest['id'])

    def test_ephemeral_store_destroy_removes_file(self):
        store = self.store()
        store.create('guest scratch', ephemeral=True, ttl=60)
        path = store.path
        store.close(destroy=True)
        self.assertFalse(path.exists())

    def test_destroy_failure_quarantines_instead_of_claiming_success(self):
        store = self.store()
        store.create('guest scratch', ephemeral=True, ttl=60)
        with mock.patch('aios.task_recovery.os.unlink', side_effect=OSError):
            result = store.close(destroy=True)
        self.assertFalse(result['destroyed'])
        self.assertIsNotNone(result['quarantined'])
        self.assertTrue((self.root / result['quarantined']).exists())
        self.open_stores.remove(store)

    # ------------------------------------------------------------ schema

    def test_newer_store_version_refuses(self):
        store = self.store()
        path = store.path
        store.close()
        db = sqlite3.connect(path)
        db.execute('PRAGMA user_version=99')
        db.commit()
        db.close()
        with self.assertRaises(RecoveryError):
            TaskStore(self.root, store.owner)

    def test_store_rejects_non_uuid_owner(self):
        with self.assertRaises(RecoveryError):
            TaskStore(self.root, 'someone@example.com')

    def test_invalid_identifiers_rejected(self):
        store = self.store()
        for bad in ('', 'not-a-uuid', 'x' * 40):
            with self.assertRaises(RecoveryError):
                store.get(bad)

    def test_task_quota_is_bounded(self):
        store = self.store()
        original = task_recovery.MAX_TASKS_PER_OWNER
        task_recovery.MAX_TASKS_PER_OWNER = 3
        try:
            for index in range(3):
                store.create(f'task {index}')
            with self.assertRaises(RecoveryError):
                store.create('fourth')
        finally:
            task_recovery.MAX_TASKS_PER_OWNER = original

    def tearDown(self):
        for store in self.open_stores:
            try:
                store.db.close()
            except sqlite3.ProgrammingError:
                pass
        self.open_stores = []


if __name__ == '__main__':
    unittest.main()
