from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from aios.scheduled_store import ScheduledStore
from aios.scheduling import ConflictError, QuotaError, SchedulingError, UnavailableError
from test_scheduling import FakeClock, job_config


class ScheduledStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'scheduled'
        self.clock = FakeClock()
        self.store = ScheduledStore(self.root, self.clock)
        self.addCleanup(lambda: self.store.close())

    def create(self, expression='* * * * *', provider='remote'):
        return self.store.create(job_config(expression, provider))

    def once(self):
        config = job_config()
        config['schedule'] = {'kind': 'once', 'value': '2026-01-01T00:01Z', 'zone': 'UTC'}
        return self.store.create(config)

    def complete(self, run):
        self.store.start(run['id'])
        return self.store.finish(run['id'], 'succeeded', result='Saved answer')

    def reopen(self):
        self.store.close()
        self.store = ScheduledStore(self.root, self.clock)

    def test_once_and_cron_survive_reopen_with_normalized_readback(self):
        once, recurring = self.once(), self.create()
        self.reopen()
        for job in (once, recurring):
            self.assertEqual(self.store.get(job['id']), job)
        self.clock.advance(60)
        runs = self.store.claim_due()
        self.assertEqual(len(runs), 2)
        self.assertFalse(self.store.get(once['id'])['enabled'])
        self.assertTrue(self.store.get(recurring['id'])['enabled'])
        self.assertEqual(self.store.claim_due(), [])

    def test_snapshots_are_immutable_across_edits_and_caller_mutation(self):
        config = job_config()
        job = self.store.create(config)
        self.clock.advance(60)
        run = self.store.claim_due()[0]
        config['prompt'] = 'Replacement task'
        config['execution']['model'] = 'replacement-model'
        updated = self.store.update(job['id'], job['revision'], config)
        config['prompt'] = 'Caller changed again'
        stored_run = self.store.get_run(run['id'])
        self.assertEqual(stored_run['snapshot']['prompt'], job['prompt'])
        self.assertEqual(stored_run['snapshot']['execution']['model'], 'test-model')
        self.assertEqual(stored_run['revision'], 1)
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(self.store.get(job['id'])['prompt'], 'Replacement task')

    def test_mutations_require_expected_revisions(self):
        job = self.create()
        paused = self.store.pause(job['id'], 1)
        self.assertEqual(paused['revision'], 2)
        for revision in (1, 0, None, True, '2'):
            for action in (
                lambda: self.store.pause(job['id'], revision),
                lambda: self.store.resume(job['id'], revision),
                lambda: self.store.update(job['id'], revision, job_config()),
                lambda: self.store.delete(job['id'], revision),
                lambda: self.store.run_now(job['id'], revision, str(uuid.uuid4())),
            ):
                with self.subTest(revision=revision), self.assertRaises(SchedulingError):
                    action()

    def test_pause_suppresses_catchup_but_does_not_cancel_active_run(self):
        job = self.create()
        self.clock.advance(60)
        run = self.store.claim_due()[0]
        paused = self.store.pause(job['id'], 1)
        self.assertIsNone(paused['next_due'])
        self.assertEqual(self.store.get_run(run['id'])['state'], 'queued')
        self.complete(run)
        self.clock.advance(2 * 86400)
        self.assertEqual(self.store.claim_due(), [])
        resumed = self.store.resume(job['id'], paused['revision'])
        self.assertEqual(resumed['next_due'], '2026-01-03T00:02:00.000000Z')
        self.assertEqual(resumed['missed_count'], 0)

    def test_expired_paused_once_requires_edit(self):
        job = self.once()
        self.store.pause(job['id'], 1)
        self.clock.advance(3600)
        with self.assertRaises(SchedulingError):
            self.store.resume(job['id'], 2)
        config = job_config()
        config['schedule'] = {'kind': 'once', 'value': '2026-01-02T00:00Z', 'zone': 'UTC'}
        self.store.update(job['id'], 2, config)
        self.assertTrue(self.store.resume(job['id'], 3)['enabled'])

    def test_missed_runs_coalesce_to_latest_without_backlog(self):
        job = self.create()
        self.clock.advance(3 * 86400 + 30)
        run = self.store.claim_due()[0]
        self.assertEqual(run['scheduled_at'], '2026-01-04T00:00:00.000000Z')
        self.assertEqual(run['missed_count'], 4319)
        self.assertEqual(self.store.get(job['id'])['missed_count'], 4319)
        self.clock.advance(10 * 60)
        self.assertEqual(self.store.claim_due(), [])
        self.complete(run)
        next_run = self.store.claim_due()[0]
        self.assertEqual(next_run['missed_count'], 9)
        self.assertEqual(len(self.store.list_runs(job['id'])), 2)

    def test_coalesce_window_and_skip_policy(self):
        job = self.create('0 1 * * *')
        self.clock.advance(2 * 86400 + 1800)
        run = self.store.claim_due()[0]
        self.assertEqual(run['scheduled_at'], '2026-01-02T01:00:00.000000Z')
        self.assertEqual(run['missed_count'], 1)
        self.complete(run)
        config = job_config('0 1 * * *')
        config['execution']['missed_run'] = 'skip'
        self.store.update(job['id'], 1, config)
        self.clock.advance(7200)
        self.assertEqual(self.store.claim_due(), [])
        self.assertEqual(self.store.get(job['id'])['missed_count'], 2)

    def test_old_weekly_occurrences_are_skipped(self):
        job = self.create('0 1 * * 4')
        self.clock.advance(3 * 86400)
        self.assertEqual(self.store.claim_due(), [])
        self.assertEqual(self.store.get(job['id'])['missed_count'], 1)
        self.assertEqual(self.store.get(job['id'])['next_due'], '2026-01-08T01:00:00.000000Z')

    def test_expired_one_shot_becomes_missed_with_durable_feedback(self):
        job = self.once()
        self.clock.advance(3600)
        self.assertEqual(self.store.claim_due(), [])
        run = self.store.list_runs(job['id'])[0]
        self.assertEqual(run['state'], 'missed')
        self.assertEqual(run['missed_count'], 1)
        self.reopen()
        self.assertEqual(self.store.unread()[0]['run_id'], run['id'])
        self.assertFalse(self.store.get(job['id'])['enabled'])

    def test_rollback_edits_and_retention_do_not_reclaim_occurrences(self):
        job = self.create()
        self.clock.advance(60)
        run = self.store.claim_due()[0]
        self.complete(run)
        self.store.acknowledge(run['id'])
        self.clock.advance(31 * 86400)
        self.assertEqual(self.store.prune(), 1)
        self.clock.advance(-31 * 86400 - 60)
        self.store.update(job['id'], 1, job_config())
        self.assertEqual(self.store.get(job['id'])['next_due'], '2026-01-01T00:02:00.000000Z')
        self.clock.advance(60)
        self.assertEqual(self.store.claim_due(), [])
        self.clock.advance(60)
        self.assertEqual(len(self.store.claim_due()), 1)

    def test_manual_runs_are_idempotent_and_separate_from_recurring_claims(self):
        job = self.create()
        request = str(uuid.uuid4())
        run = self.store.run_now(job['id'], 1, request)
        self.assertEqual(self.store.run_now(job['id'], 1, request)['id'], run['id'])
        self.complete(run)
        self.reopen()
        self.assertEqual(self.store.run_now(job['id'], 1, request)['id'], run['id'])
        other = self.create()
        with self.assertRaises(ConflictError):
            self.store.run_now(other['id'], 1, request)
        self.clock.advance(60)
        self.assertEqual(len(self.store.claim_due()), 2)

    def test_active_job_and_user_capacity_limits(self):
        a, b, c = self.create(), self.create(), self.create()
        run = self.store.run_now(a['id'], 1, str(uuid.uuid4()))
        with self.assertRaises(QuotaError):
            self.store.run_now(a['id'], 1, str(uuid.uuid4()))
        self.store.run_now(b['id'], 1, str(uuid.uuid4()))
        with self.assertRaises(QuotaError):
            self.store.run_now(c['id'], 1, str(uuid.uuid4()))
        self.complete(run)
        self.store.run_now(c['id'], 1, str(uuid.uuid4()))

    def test_manual_dedup_survives_edits_and_result_retention(self):
        job = self.create()
        request = str(uuid.uuid4())
        run = self.store.run_now(job['id'], 1, request)
        self.store.update(job['id'], 1, job_config('0 * * * *'))
        self.assertEqual(self.store.run_now(job['id'], 1, request)['id'], run['id'])
        self.complete(run)
        self.store.acknowledge(run['id'])
        self.clock.advance(31 * 86400)
        self.store.prune()
        self.reopen()
        with self.assertRaises(UnavailableError):
            self.store.run_now(job['id'], 1, request)
        self.assertEqual(self.store.list_runs(job['id']), [])

    def test_only_one_local_run_with_remote_capacity_remaining(self):
        self.create(provider='local')
        self.create(provider='local')
        self.create(provider='remote')
        self.clock.advance(60)
        runs = self.store.claim_due()
        self.assertEqual(len(runs), 2)
        self.assertEqual(sorted(run['snapshot']['execution']['provider'] for run in runs), ['local', 'remote'])

    def test_enabled_quota_counts_paused_jobs_on_resume(self):
        for _ in range(100):
            self.create()
        with self.assertRaises(QuotaError):
            self.create()
        job = self.store.list_jobs(limit=1)[0]
        self.store.pause(job['id'], 1)
        self.create()
        with self.assertRaises(QuotaError):
            self.store.resume(job['id'], 2)

    def test_result_bounds_and_terminal_transitions(self):
        job = self.create()
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        with self.assertRaises(ConflictError):
            self.store.finish(run['id'], 'succeeded', result='Not started')
        self.store.start(run['id'])
        with self.assertRaises(ConflictError):
            self.store.start(run['id'])
        with self.assertRaises(SchedulingError):
            self.store.finish(run['id'], 'succeeded', result='x' * (65536 + 1))
        self.assertEqual(self.store.unread(), [])
        self.store.finish(run['id'], 'failed', error='Provider unavailable', usage={'tool_calls': 0})
        with self.assertRaises(ConflictError):
            self.store.finish(run['id'], 'succeeded')
        self.assertEqual(len(self.store.unread()), 1)

    def test_finish_and_outbox_are_one_transaction(self):
        job = self.create()
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.store.start(run['id'])
        self.store.db.execute('''CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox
            BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.finish(run['id'], 'succeeded', result='Must not be lost')
        self.assertEqual(self.store.get_run(run['id'])['state'], 'running')
        self.assertEqual(self.store.unread(), [])
        self.store.db.execute('DROP TRIGGER fail_outbox')
        self.store.finish(run['id'], 'succeeded', result='Persisted')
        self.reopen()
        self.assertEqual(self.store.get_run(run['id'])['result'], 'Persisted')
        self.assertEqual(len(self.store.unread()), 1)

    def test_failed_occurrence_leaves_future_schedule_enabled(self):
        job = self.create()
        self.clock.advance(60)
        run = self.store.claim_due()[0]
        self.store.start(run['id'])
        self.store.finish(run['id'], 'failed', error='Offline')
        self.clock.advance(60)
        self.assertEqual(len(self.store.claim_due()), 1)
        self.assertTrue(self.store.get(job['id'])['enabled'])

    def test_reopen_does_not_recover_until_supervisor_explicitly_requests_it(self):
        first, second = self.create(), self.create()
        run = self.store.run_now(first['id'], 1, str(uuid.uuid4()))
        self.store.start(run['id'])
        self.store.run_now(second['id'], 1, str(uuid.uuid4()))
        self.reopen()
        self.assertEqual(self.store.get_run(run['id'])['state'], 'running')
        self.assertEqual(self.store.recover(), 2)
        self.assertEqual(self.store.recover(), 0)
        self.assertEqual(self.store.get_run(run['id'])['state'], 'interrupted')
        self.assertEqual(len(self.store.unread()), 2)

    def test_acknowledgement_is_idempotent_and_unread_survives_retention(self):
        job = self.create()
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.complete(run)
        self.clock.advance(31 * 86400)
        self.assertEqual(self.store.prune(), 0)
        self.assertEqual(len(self.store.unread()), 1)
        self.store.acknowledge(run['id'])
        self.store.acknowledge(run['id'])
        self.assertEqual(self.store.unread(), [])
        self.assertEqual(self.store.prune(), 1)
        with self.assertRaises(UnavailableError):
            self.store.get_run(run['id'])

    def test_non_deliverable_results_are_retention_eligible(self):
        config = job_config()
        config['notification'] = {'mode': 'actionable'}
        job = self.store.create(config)
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.store.start(run['id'])
        self.store.finish(run['id'], 'succeeded', result='No changes', outcome='unchanged')
        self.assertEqual(self.store.unread(), [])
        self.clock.advance(31 * 86400)
        self.assertEqual(self.store.prune(), 1)
        with self.assertRaises(UnavailableError):
            self.store.get_run(run['id'])

    def test_notification_policy_and_persisted_pulse_receipt(self):
        all_job = self.create()
        all_run = self.store.run_now(all_job['id'], 1, str(uuid.uuid4()))
        self.store.start(all_run['id'])
        self.store.finish(all_run['id'], 'succeeded', result='Changed', outcome='changed')

        actionable = job_config()
        actionable['notification'] = {'mode': 'actionable'}
        actionable_job = self.store.create(actionable)
        quiet_run = self.store.run_now(actionable_job['id'], 1, str(uuid.uuid4()))
        self.store.start(quiet_run['id'])
        self.store.finish(quiet_run['id'], 'succeeded', result='No change', outcome='unchanged')
        error_run = self.store.run_now(actionable_job['id'], 1, str(uuid.uuid4()))
        self.store.start(error_run['id'])
        self.store.finish(error_run['id'], 'failed', error='Provider unavailable')

        unread = self.store.unread()
        self.assertEqual([item['run_id'] for item in unread], [error_run['id'], all_run['id']])
        self.assertEqual([item['run_id'] for item in self.store.unread(after=unread[1]['sequence'])],
                         [error_run['id']])
        self.assertEqual(unread[1]['title'], all_job['title'])
        self.assertIsNone(unread[1]['notified'])
        self.store.mark_notified(all_run['id'])
        self.store.mark_notified(all_run['id'])
        self.reopen()
        persisted = self.store.unread()
        self.assertIsNotNone(persisted[1]['notified'])
        with self.assertRaises(ConflictError):
            self.store.mark_notified(quiet_run['id'])
        self.assertEqual(self.store.get_run(all_run['id'])['outcome'], 'changed')
        self.assertEqual(self.store.list_runs(all_job['id'])[0]['outcome'], 'changed')

    def test_quiet_hours_and_snooze_report_release_without_hiding_feedback(self):
        config = job_config()
        config['notification'] = {
            'mode': 'all',
            'quiet_hours': {'start': '23:00', 'end': '01:00', 'zone': 'UTC'},
            'snooze_until': '2026-01-01T00:30:00Z',
        }
        job = self.store.create(config)
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.complete(run)
        self.assertEqual(self.store.unread()[0]['suppressed_until'],
                         '2026-01-01T01:00:00.000000Z')
        self.clock.advance(3601)
        self.assertIsNone(self.store.unread()[0]['suppressed_until'])

    def test_future_snooze_that_lands_in_quiet_hours_releases_after_quiet(self):
        config = job_config()
        config['notification'] = {
            'mode': 'all',
            'quiet_hours': {'start': '23:00', 'end': '07:00', 'zone': 'UTC'},
            'snooze_until': '2026-01-02T01:00:00Z',
        }
        job = self.store.create(config)
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.complete(run)
        self.assertEqual(self.store.unread()[0]['suppressed_until'],
                         '2026-01-02T07:00:00.000000Z')

    def test_version_one_outbox_migrates_atomically(self):
        config = job_config()
        config['notification'] = {'mode': 'actionable'}
        job = self.store.create(config)
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.store.start(run['id'])
        self.store.finish(run['id'], 'succeeded', result='No change', outcome='unchanged')
        self.store.close()
        with sqlite3.connect(self.root / 'jobs.sqlite3') as db:
            db.execute('ALTER TABLE outbox DROP COLUMN deliverable')
            db.execute('ALTER TABLE outbox DROP COLUMN notified')
            db.execute('PRAGMA user_version=1')
        self.store = ScheduledStore(self.root, self.clock)
        columns = {row['name'] for row in self.store.db.execute('PRAGMA table_info(outbox)')}
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], 2)
        self.assertIn('deliverable', columns)
        self.assertIn('notified', columns)
        self.assertEqual(self.store.unread(), [])

    def test_thousand_run_retention_preserves_unread_overflow(self):
        job = self.create()
        unread = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.complete(unread)
        # Use one transaction to populate the retention fixture without 2,000 fsyncs.
        with self.store._transaction():
            for _ in range(1001):
                run_id = self.store._insert_run(self.store._job(job['id']), self.clock.now(), 'manual')
                self.store._finish(run_id, 'succeeded', 'Saved answer', '')
                self.store.db.execute('UPDATE outbox SET acknowledged=created WHERE run_id=?', (run_id,))
        self.assertEqual(self.store.prune(), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM runs').fetchone()[0], 1001)
        self.assertEqual(self.store.unread()[0]['run_id'], unread['id'])

    def test_saved_zone_ignores_desktop_zone_changes(self):
        config = job_config('0 9 * * *')
        config['schedule']['zone'] = 'America/New_York'
        job = self.store.create(config)
        with patch.dict(os.environ, {'TZ': 'Asia/Tokyo'}):
            self.reopen()
            self.assertEqual(self.store.get(job['id'])['next_local'], '2026-01-01T09:00:00-05:00')
            self.assertEqual(self.store.get(job['id'])['next_due'], job['next_due'])

    def test_claim_during_date_crossing_fold(self):
        self.clock.wall = FakeClock('2010-03-04T14:29Z').now()
        config = job_config()
        config['schedule']['zone'] = 'Antarctica/Casey'
        job = self.store.create(config)
        self.clock.advance(61 * 60)
        run = self.store.claim_due()[0]
        self.assertEqual(run['scheduled_at'], '2010-03-04T14:59:00.000000Z')
        self.assertEqual(run['missed_count'], 29)
        self.assertEqual(self.store.get(job['id'])['next_due'], '2010-03-04T18:00:00.000000Z')

    def test_delete_tombstones_configuration_and_preserves_unread_result(self):
        job = self.create()
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.store.delete(job['id'], 1)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store.get_run(run['id'])['state'], 'cancelled')
        self.assertEqual(len(self.store.unread()), 1)
        with self.assertRaises(UnavailableError):
            self.store.get(job['id'])
        self.assertEqual(self.store.db.execute('SELECT config FROM jobs').fetchone()[0], '{}')

    def test_storage_budget_stops_new_work_without_dropping_feedback(self):
        job = self.create()
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.store.start(run['id'])
        with patch('aios.scheduled_store.MAX_STORE_BYTES', 1):
            with self.assertRaises(QuotaError):
                self.create()
            with self.assertRaises(QuotaError):
                self.store.claim_due()
            self.store.finish(run['id'], 'succeeded', result='Already running can finish')
        self.assertEqual(len(self.store.unread()), 1)

    def test_unknown_schema_is_rejected_without_downgrading(self):
        self.store.db.execute('PRAGMA user_version=99')
        with self.assertRaises(UnavailableError):
            ScheduledStore(self.root, self.clock)
        self.assertEqual(self.store.db.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_initial_migration_is_transactional(self):
        root = Path(self.temp.name) / 'migration'
        with patch('aios.scheduled_store.SCHEMA', ('CREATE TABLE partial (id TEXT)', 'NOT SQL')):
            with self.assertRaises(sqlite3.OperationalError):
                ScheduledStore(root, self.clock)
        with sqlite3.connect(root / 'jobs.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchall(), [])
        reopened = ScheduledStore(root, self.clock)
        reopened.close()

    def test_protected_context_never_opens_desktop_storage(self):
        protected_root = Path(self.temp.name) / 'protected'
        with patch('aios.principals.current', return_value=object()):
            with self.assertRaises(UnavailableError):
                ScheduledStore(protected_root, self.clock)
        self.assertFalse(protected_root.exists())

    def test_owner_filtering_and_bounded_pagination(self):
        job = self.create()
        run = self.store.run_now(job['id'], 1, str(uuid.uuid4()))
        self.complete(run)
        self.store.db.execute("UPDATE jobs SET owner='not-this-user'")
        self.store.db.execute("UPDATE runs SET owner='not-this-user'")
        self.store.db.execute("UPDATE outbox SET owner='not-this-user'")
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(self.store.unread(), [])
        for action in (lambda: self.store.get(job['id']), lambda: self.store.get_run(run['id']),
                       lambda: self.store.acknowledge(run['id'])):
            with self.assertRaises(UnavailableError):
                action()
        for limit in (0, 101, True):
            with self.assertRaises(SchedulingError):
                self.store.list_jobs(limit=limit)

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership modes')
    def test_storage_permissions(self):
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_claim_race_across_independent_connections(self):
        job = self.create()
        self.clock.advance(60)
        barrier = threading.Barrier(2)

        def claim():
            store = ScheduledStore(self.root, self.clock)
            try:
                barrier.wait(timeout=10)
                return store.claim_due()
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(len(result) for result in results), 1)
        self.assertEqual(len(self.store.list_runs(job['id'])), 1)

    def test_manual_request_race_is_idempotent(self):
        job = self.create()
        request = str(uuid.uuid4())
        barrier = threading.Barrier(2)

        def run():
            store = ScheduledStore(self.root, self.clock)
            try:
                barrier.wait(timeout=10)
                return store.run_now(job['id'], 1, request)['id']
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(results[0], results[1])
