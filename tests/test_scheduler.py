import time
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from aios.scheduled_store import ScheduledStore
from aios.scheduler import (
    SchedulerService, UnavailableError, UserActionNeeded, WorkerCancelled,
    WorkerControl, safe_error,
)
from aios.scheduling import parse_timestamp
from test_scheduling import FakeClock, job_config


def attention_config(expression='* * * * *', notification=None, provider='remote'):
    config = job_config(expression, provider)
    if notification is not None:
        config['notification'] = notification
    return config


class SchedulerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'scheduled'
        self.clock = FakeClock()
        self.store = ScheduledStore(self.root, self.clock)
        self.addCleanup(lambda: self.store.close())
        self.services = []
        self.calls = []

    def service(self, executor=None, validate_binding=None, grace_seconds=0.1):
        instance = SchedulerService(self.store, executor=executor,
                                    validate_binding=validate_binding,
                                    grace_seconds=grace_seconds)
        self.services.append(instance)
        return instance

    def succeed(self, run, control):
        self.calls.append(run['id'])
        return {'result': 'Saved answer'}

    def create(self, config=None):
        return self.store.create(config or attention_config())

    def wait_for(self, predicate, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def settle(self, service, timeout=30):
        service.settle(timeout=timeout)
        self.assertFalse(service._pending)

    def run_to_completion(self, service, job):
        self.clock.advance(60)
        self.assertEqual(service.run_once()['dispatched'], 1)
        self.settle(service)
        runs = self.store.list_runs(job['id'])
        return self.store.get_run(runs[0]['id'])

    def test_success_commits_result_outbox_and_attention_cycle(self):
        service = self.service(executor=self.succeed)
        run = self.run_to_completion(service, self.create())
        self.assertEqual(run['state'], 'succeeded')
        self.assertEqual(run['result'], 'Saved answer')
        self.assertEqual(len(self.calls), 1)
        attention = service.attention()
        self.assertEqual(attention['state'], 'unread')
        entry = attention['results'][0]
        self.assertEqual(entry['reason'], 'deliver')
        self.assertFalse(entry['actionable'])
        self.assertFalse(entry['acknowledged'])
        # Attention is durable: reading it never consumes the outbox row.
        self.assertEqual(service.attention()['state'], 'unread')
        self.store.acknowledge(run['id'])
        self.assertEqual(service.attention()['state'], 'idle')

    def test_singleton_lock_prevents_two_dispatchers_on_one_store(self):
        self.service()
        with self.assertRaises(UnavailableError):
            SchedulerService(self.store)
        # Release enables a replacement service (restart path).
        self.services.pop().close()
        self.assertIsNotNone(self.service())

    def test_startup_recovers_abandoned_runs_without_retry(self):
        job = self.create()
        self.clock.advance(60)
        run = self.store.claim_due()[0]
        self.store.start(run['id'])
        service = self.service(executor=self.succeed)
        self.assertEqual(service.startup(), {'recovered': 1})
        self.assertEqual(self.store.get_run(run['id'])['state'], 'interrupted')
        attention = service.attention()
        self.assertEqual(attention['state'], 'action_needed')
        self.assertTrue(attention['results'][0]['actionable'])
        # Recovery never requeues the abandoned run itself.
        self.calls.clear()
        self.clock.advance(60)
        self.assertEqual(service.run_once()['dispatched'], 1)
        self.settle(service)
        self.assertEqual(len(self.calls), 1)  # only the new occurrence executed

    def test_worker_failure_records_bounded_safe_error(self):
        def explode(run, control):
            raise RuntimeError('provider exploded ' + 'x' * 5000)
        service = self.service(executor=explode)
        run = self.run_to_completion(service, self.create())
        self.assertEqual(run['state'], 'failed')
        self.assertIn('provider exploded', run['error'])
        self.assertLessEqual(len(run['error']), 4000)
        self.assertEqual(service.attention()['state'], 'action_needed')
        self.assertEqual(safe_error('   '), 'Scheduler worker failed')
        self.assertEqual(safe_error('a' + chr(0) + 'b' + chr(27) + '[31mc'), 'Scheduler worker failed: ab [31mc')

    def test_invalid_executor_outcome_is_a_failure_not_a_claim(self):
        service = self.service(executor=lambda run, control: 'just a string')
        run = self.run_to_completion(service, self.create())
        self.assertEqual(run['state'], 'failed')
        self.assertIn('invalid result', run['error'])

    def test_user_action_request_becomes_visible_action_needed(self):
        def blocked(run, control):
            raise UserActionNeeded('Sign in to the provider to continue')
        service = self.service(executor=blocked)
        run = self.run_to_completion(service, self.create())
        self.assertEqual(run['state'], 'needs_user_action')
        self.assertIn('Sign in', run['error'])
        attention = service.attention()
        self.assertEqual(attention['state'], 'action_needed')
        self.assertTrue(attention['results'][0]['actionable'])

    def test_stale_binding_pauses_dispatch_with_one_action_needed_notice(self):
        job = self.create()
        service = self.service(executor=self.succeed,
                               validate_binding=lambda snapshot: False)
        self.clock.advance(60)
        service.run_once()
        self.settle(service)
        run = self.store.list_runs(job['id'])[0]
        self.assertEqual(self.store.get_run(run['id'])['state'], 'needs_user_action')
        self.assertFalse(self.store.get(job['id'])['enabled'])
        # A paused job must not queue more stale-binding notices.
        self.clock.advance(600)
        self.assertEqual(service.run_once()['dispatched'], 0)

    def test_cooperative_cancellation_records_cancelled_not_failed(self):
        def worker(run, control):
            while not control.cancel_requested:
                time.sleep(0.01)
            raise WorkerCancelled
        service = self.service(executor=worker)
        job = self.create()
        self.clock.advance(60)
        service.run_once()
        self.assertTrue(self.wait_for(
            lambda: service._pending and next(iter(service._pending.values())).thread.is_alive()))
        run_id = next(iter(service._pending))
        service.cancel_run(run_id)
        self.settle(service)
        self.assertEqual(self.store.get_run(run_id)['state'], 'cancelled')

    def test_unresponsive_worker_is_abandoned_after_grace(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def stubborn(run, control):
            release.wait(30)  # Ignores cancellation entirely.
            return {'result': 'too late'}

        config = attention_config()
        config['execution']['timeout_seconds'] = 1
        service = self.service(executor=stubborn, grace_seconds=0.1)
        job = self.store.create(config)
        self.clock.advance(60)
        service.run_once()
        self.settle(service, timeout=30)
        run = self.store.list_runs(job['id'])[0]
        self.assertEqual(self.store.get_run(run['id'])['state'], 'failed')
        self.assertIn('timeout', self.store.get_run(run['id'])['error'])

    def test_shutdown_stops_dispatch_and_successor_recovers_stragglers(self):
        # A manual run that never got an executor remains queued active work.
        service = self.service(executor=None)
        job = self.create()
        run = service.run_now(job['id'], job['revision'], str(uuid.uuid4()))
        self.assertEqual(run['state'], 'queued')
        summary = service.shutdown()
        self.assertEqual(summary['recovered'], 1)
        self.assertEqual(self.store.get_run(run['id'])['state'], 'interrupted')

    def test_stopped_service_claims_no_new_work(self):
        service = self.service(executor=self.succeed)
        self.create()
        service.close()
        self.clock.advance(60)
        self.assertEqual(service.run_once()['dispatched'], 0)
        self.assertEqual(len(self.store.claim_due()), 1)

    def test_run_now_is_idempotent_through_the_service(self):
        service = self.service(executor=self.succeed)
        job = self.create()
        request = str(uuid.uuid4())
        run = service.run_now(job['id'], job['revision'], request)
        self.settle(service)
        repeat = service.run_now(job['id'], job['revision'], request)
        self.assertEqual(run['id'], repeat['id'])
        self.assertEqual(len(self.store.list_runs(job['id'])), 1)
        self.assertEqual(self.store.get_run(run['id'])['state'], 'succeeded')

    def test_queued_run_without_executor_cancels_via_recovery(self):
        service = self.service(executor=None)
        job = self.create()
        run = service.run_now(job['id'], job['revision'], str(uuid.uuid4()))
        self.assertEqual(run['state'], 'queued')
        cancelled = service.cancel_run(run['id'])
        self.assertEqual(cancelled['state'], 'interrupted')

    def test_delivery_suppresses_unchanged_only_in_actionable_mode(self):
        def unchanged(run, control):
            return {'result': 'checked, nothing new', 'outcome': 'unchanged'}
        service = self.service(executor=unchanged)
        job = self.create(attention_config(notification={'mode': 'actionable'}))
        self.run_to_completion(service, job)
        attention = service.attention()
        entry = attention['results'][0]
        self.assertFalse(entry['visible'])
        self.assertEqual(entry['reason'], 'unchanged')
        self.assertEqual(attention['state'], 'idle')
        # The suppressed result still needs an explicit acknowledgement.
        self.assertEqual(len(self.store.unread()), 1)

    def test_failures_stay_visible_in_actionable_mode(self):
        def explode(run, control):
            raise RuntimeError('nope')
        service = self.service(executor=explode)
        job = self.create(attention_config(notification={'mode': 'actionable'}))
        self.run_to_completion(service, job)
        attention = service.attention()
        self.assertEqual(attention['state'], 'action_needed')
        self.assertTrue(attention['results'][0]['visible'])

    def test_unchanged_results_remain_visible_in_default_mode(self):
        def unchanged(run, control):
            return {'result': 'checked', 'outcome': 'unchanged'}
        service = self.service(executor=unchanged)
        job = self.create()  # notification defaults to mode 'all'
        self.run_to_completion(service, job)
        attention = service.attention()
        self.assertEqual(attention['state'], 'unread')
        self.assertTrue(attention['results'][0]['visible'])
        self.assertEqual(attention['results'][0]['reason'], 'deliver')

    def test_quiet_hours_and_snooze_suppress_visibility_without_consuming(self):
        config = attention_config(notification={
            'mode': 'all',
            'quiet_hours': {'start': '23:00', 'end': '01:00', 'zone': 'UTC'}})
        service = self.service(executor=self.succeed)
        quiet_job = self.create(config)
        self.run_to_completion(service, quiet_job)
        inside = service.attention(now=parse_timestamp('2026-01-01T00:30:00Z'))
        self.assertEqual([item['reason'] for item in inside['results']], ['quiet_hours'])
        self.assertFalse(inside['results'][0]['visible'])
        self.assertEqual(inside['state'], 'idle')
        outside = service.attention(now=parse_timestamp('2026-01-01T02:00:00Z'))
        self.assertEqual([item['reason'] for item in outside['results']], ['deliver'])
        self.assertEqual(outside['state'], 'unread')
        # Pause the recurring job so only the snoozed job fires next.
        refreshed = self.store.get(quiet_job['id'])
        self.store.pause(quiet_job['id'], refreshed['revision'])
        snoozed = attention_config(notification={'mode': 'all',
                                                 'snooze_until': '2026-06-01T00:00:00Z'})
        self.create(snoozed)
        self.clock.advance(60)
        self.assertEqual(service.run_once()['dispatched'], 1)
        self.settle(service)
        attention = service.attention(now=parse_timestamp('2026-01-01T02:00:00Z'))
        reasons = sorted(item['reason'] for item in attention['results'])
        self.assertEqual(reasons, ['deliver', 'snoozed'])
        # Suppression is not acknowledgement: both rows are still unread.
        self.assertEqual(len(self.store.unread()), 2)

    def test_attention_precedence_action_needed_then_unread_then_idle(self):
        service = self.service(executor=lambda run, control: {'result': 'ok'})
        self.run_to_completion(service, self.create())
        attention = service.attention()
        self.assertEqual(attention['state'], 'unread')

        def blocked(run, control):
            raise UserActionNeeded('sign in')
        service.executor = blocked
        self.create()
        self.clock.advance(60)
        service.run_once()
        self.settle(service)
        attention = service.attention()
        self.assertEqual(attention['state'], 'action_needed')
        for item in attention['results']:
            self.store.acknowledge(item['run_id'])
        self.assertEqual(service.attention()['state'], 'idle')

    def test_attention_reports_running_while_a_worker_is_active(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow(run, control):
            release.wait(30)
            return {'result': 'done'}

        service = self.service(executor=slow, grace_seconds=60)
        self.create()
        self.clock.advance(60)
        service.run_once()
        self.assertTrue(self.wait_for(lambda: bool(service._pending)))
        self.assertEqual(service.attention()['state'], 'running')
        self.assertEqual(service.attention()['active_runs'], 1)

    def test_worker_control_wait_is_interruptible(self):
        control = WorkerControl(5, 1_000_000.0)
        timer = threading.Timer(0.02, control.cancel)
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertTrue(control.wait(30))
        idle = WorkerControl(1, 0.0)
        self.assertFalse(idle.wait(0.0))
        self.assertFalse(idle.cancel_requested)

    def test_double_run_once_does_not_claim_the_same_occurrence_twice(self):
        service = self.service(executor=self.succeed)
        job = self.create()
        self.clock.advance(60)
        first = service.run_once()
        second = service.run_once()
        self.assertEqual(first['dispatched'], 1)
        self.assertEqual(second['dispatched'], 0)
        self.settle(service)
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
