import threading
from pathlib import Path
import tempfile
import unittest
import uuid

from aios.scheduled_store import ScheduledStore
from aios.scheduled_jobs import ACTIONS, TOOL, ScheduledJobsTool
from aios.scheduler import SchedulerService
from test_scheduling import FakeClock, job_config


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'scheduled'
        self.clock = FakeClock()
        self.store = ScheduledStore(self.root, self.clock)
        self.addCleanup(lambda: self.store.close())
        self.services = []
        self.release = threading.Event()
        self.hits = []

    def service(self, executor=None):
        instance = SchedulerService(self.store, executor=executor)
        self.services.append(instance)
        return instance

    def tool(self, executor=None):
        return ScheduledJobsTool(self.service(executor))

    def create(self, tool=None, expression='* * * * *', provider='remote'):
        tool = tool or self.tool()
        reply = tool.act({'action': 'create', 'job': job_config(expression, provider)})
        self.assertTrue(reply['ok'], reply)
        return reply['job']

    def succeed(self, run, control):
        self.hits.append(run['id'])
        return {'result': 'Saved answer', 'outcome': 'changed',
                'usage': {'input_tokens': 5, 'output_tokens': 7, 'tool_calls': 0}}

    def blocking(self, run, control):
        self.hits.append(run['id'])
        self.assertTrue(self.release.wait(10))
        return {'result': 'Blocked answer'}


class DefinitionTests(ToolTests):
    def test_tool_definition_matches_the_fixed_action_contract(self):
        function = TOOL['function']
        self.assertEqual(function['name'], 'scheduled_jobs')
        schema = function['parameters']
        self.assertEqual(schema['additionalProperties'], False)
        self.assertEqual(schema['required'], ['action'])
        self.assertEqual(schema['properties']['action']['enum'], list(ACTIONS))


class ValidationTests(ToolTests):
    def test_malformed_requests_are_invalid_without_touching_storage(self):
        tool = self.tool()
        bad = str(uuid.uuid4())
        cases = [
            'not-an-object',
            {},
            {'action': 'teleport'},
            {'action': 'create'},
            {'action': 'create', 'job': job_config(), 'job_id': bad},
            {'action': 'get'},
            {'action': 'get', 'job_id': 'not-a-uuid'},
            {'action': 'list', 'limit': 0},
            {'action': 'list', 'limit': 101},
            {'action': 'list', 'after': 'nope'},
            {'action': 'pause', 'job_id': bad},
            {'action': 'pause', 'job_id': bad, 'expected_revision': 0},
            {'action': 'update', 'job_id': bad, 'expected_revision': 1},
            {'action': 'update', 'job_id': bad, 'expected_revision': 1, 'job': 'nope'},
            {'action': 'run_now', 'job_id': bad, 'expected_revision': 1},
            {'action': 'run_now', 'job_id': bad, 'expected_revision': 1,
             'request_id': 'not-a-uuid'},
            {'action': 'list_runs', 'job_id': bad, 'before': 0},
            {'action': 'cancel_run'},
            {'action': 'read_result', 'run_id': bad, 'limit': 5},
            {'action': 'acknowledge_result'},
        ]
        for request in cases:
            with self.subTest(request=request):
                reply = tool.act(request)
                self.assertFalse(reply['ok'])
                self.assertEqual(reply['code'], 'invalid')
                self.assertTrue(reply['message'])
        self.assertEqual(tool.act({'action': 'list'})['jobs'], [])

    def test_owner_field_in_job_configuration_is_rejected(self):
        tool = self.tool()
        config = job_config()
        config['owner'] = 'uid:0'
        reply = tool.act({'action': 'create', 'job': config})
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'invalid')
        self.assertEqual(tool.act({'action': 'list'})['jobs'], [])


class JobLifecycleTests(ToolTests):
    def test_create_readback_normalizes_schedule_and_previews_occurrences(self):
        job = self.create()
        self.assertEqual(job['enabled'], True)
        self.assertEqual(job['revision'], 1)
        self.assertEqual(job['schedule'], {'kind': 'cron', 'value': '* * * * *', 'zone': 'UTC'})
        self.assertIsNotNone(job['next_due'])
        self.assertEqual(len(job['preview']), 3)
        self.assertTrue(all(item['utc'] > '2026-01-01T00:00:00.000000Z'
                            for item in job['preview']))

    def test_get_list_update_pause_resume_and_delete(self):
        tool = self.tool()
        first = self.create(tool)
        second = self.create(tool)
        self.assertEqual({job['id'] for job in tool.act({'action': 'list'})['jobs']},
                         {first['id'], second['id']})
        updated = tool.act({'action': 'update', 'job_id': first['id'],
                            'expected_revision': first['revision'],
                            'job': job_config('0 9 * * *')})
        self.assertTrue(updated['ok'], updated)
        self.assertEqual(updated['job']['revision'], 2)
        self.assertEqual(updated['job']['schedule']['value'], '0 9 * * *')
        self.assertTrue(updated['job']['next_due'].endswith('T09:00:00.000000Z'))
        stale = tool.act({'action': 'pause', 'job_id': first['id'],
                          'expected_revision': 1})
        self.assertFalse(stale['ok'])
        self.assertEqual(stale['code'], 'conflict')
        paused = tool.act({'action': 'pause', 'job_id': first['id'], 'expected_revision': 2})
        self.assertEqual(paused['job']['enabled'], False)
        self.assertIsNone(paused['job']['next_due'])
        resumed = tool.act({'action': 'resume', 'job_id': first['id'], 'expected_revision': 3})
        self.assertEqual(resumed['job']['enabled'], True)
        self.assertIsNotNone(resumed['job']['next_due'])
        deleted = tool.act({'action': 'delete', 'job_id': second['id'],
                            'expected_revision': second['revision']})
        self.assertTrue(deleted['ok'])
        self.assertEqual(deleted['deleted'], True)
        gone = tool.act({'action': 'get', 'job_id': second['id']})
        self.assertFalse(gone['ok'])
        self.assertEqual(gone['code'], 'unavailable')
        remaining = tool.act({'action': 'list'})['jobs']
        self.assertEqual([job['id'] for job in remaining], [first['id']])

    def test_list_pages_with_the_after_cursor(self):
        tool = self.tool()
        created = sorted(self.create(tool)['id'] for _ in range(3))
        page = tool.act({'action': 'list', 'limit': 2})
        self.assertEqual([job['id'] for job in page['jobs']], created[:2])
        tail = tool.act({'action': 'list', 'limit': 2, 'after': page['jobs'][-1]['id']})
        self.assertEqual([job['id'] for job in tail['jobs']], created[2:])

    def test_store_validation_failures_report_invalid(self):
        tool = self.tool()
        broken = job_config('not a cron')
        self.assertEqual(tool.act({'action': 'create', 'job': broken})['code'], 'invalid')
        noisy = job_config()
        noisy['notification'] = {'mode': 'loud'}
        self.assertEqual(tool.act({'action': 'create', 'job': noisy})['code'], 'invalid')
        self.assertEqual(tool.act({'action': 'list'})['jobs'], [])


class RunTests(ToolTests):
    def settle(self, tool):
        tool.scheduler.settle(timeout=30)
        self.assertFalse(tool.scheduler._pending)

    def test_run_now_executes_once_and_replays_the_request_id(self):
        tool = self.tool(self.succeed)
        job = self.create(tool)
        request_id = str(uuid.uuid4())
        first = tool.act({'action': 'run_now', 'job_id': job['id'],
                          'expected_revision': job['revision'], 'request_id': request_id})
        self.assertTrue(first['ok'], first)
        self.settle(tool)
        after = tool.act({'action': 'read_result', 'run_id': first['run']['id']})
        self.assertEqual(after['run']['state'], 'succeeded')
        self.assertEqual(after['run']['result'], 'Saved answer')
        self.assertEqual(after['run']['usage']['tool_calls'], 0)
        replay = tool.act({'action': 'run_now', 'job_id': job['id'],
                           'expected_revision': job['revision'], 'request_id': request_id})
        self.assertTrue(replay['ok'])
        self.assertEqual(replay['run']['id'], first['run']['id'])
        self.assertEqual(len(self.hits), 1)

    def test_run_now_without_a_host_stays_queued_and_reads_as_no_body(self):
        tool = self.tool()  # SchedulerService with executor=None
        job = self.create(tool)
        reply = tool.act({'action': 'run_now', 'job_id': job['id'],
                          'expected_revision': job['revision'],
                          'request_id': str(uuid.uuid4())})
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['run']['state'], 'queued')
        self.assertNotIn('result', reply['run'])
        read = tool.act({'action': 'read_result', 'run_id': reply['run']['id']})
        self.assertEqual(read['run']['state'], 'queued')
        self.assertEqual(read['run']['result'], '')

    def test_capacity_exhaustion_reports_quota_exceeded(self):
        tool = self.tool(self.blocking)
        jobs = [self.create(tool) for _ in range(3)]
        for job in jobs[:2]:
            reply = tool.act({'action': 'run_now', 'job_id': job['id'],
                              'expected_revision': job['revision'],
                              'request_id': str(uuid.uuid4())})
            self.assertTrue(reply['ok'], reply)
        overflow = tool.act({'action': 'run_now', 'job_id': jobs[2]['id'],
                             'expected_revision': jobs[2]['revision'],
                             'request_id': str(uuid.uuid4())})
        self.assertFalse(overflow['ok'])
        self.assertEqual(overflow['code'], 'quota_exceeded')
        self.release.set()
        self.settle(tool)

    def test_run_now_requires_the_current_revision(self):
        tool = self.tool(self.succeed)
        job = self.create(tool)
        reply = tool.act({'action': 'run_now', 'job_id': job['id'],
                          'expected_revision': 99, 'request_id': str(uuid.uuid4())})
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'conflict')
        self.assertEqual(self.hits, [])

    def test_list_runs_and_cancel_run_readbacks(self):
        tool = self.tool(self.succeed)
        job = self.create(tool)
        run = tool.act({'action': 'run_now', 'job_id': job['id'],
                        'expected_revision': job['revision'],
                        'request_id': str(uuid.uuid4())})['run']
        self.settle(tool)
        listing = tool.act({'action': 'list_runs', 'job_id': job['id']})
        self.assertTrue(listing['ok'])
        self.assertEqual([item['id'] for item in listing['runs']], [run['id']])
        for body in ('snapshot', 'result', 'error', 'usage'):
            self.assertNotIn(body, listing['runs'][0])
        # Cancelling a terminal run is a no-op readback, not an error.
        late = tool.act({'action': 'cancel_run', 'run_id': run['id']})
        self.assertTrue(late['ok'])
        self.assertEqual(late['run']['state'], 'succeeded')
        missing = tool.act({'action': 'cancel_run', 'run_id': str(uuid.uuid4())})
        self.assertFalse(missing['ok'])
        self.assertEqual(missing['code'], 'unavailable')

    def test_read_and_acknowledge_result_flow(self):
        tool = self.tool(self.succeed)
        job = self.create(tool)
        run_id = tool.act({'action': 'run_now', 'job_id': job['id'],
                           'expected_revision': job['revision'],
                           'request_id': str(uuid.uuid4())})['run']['id']
        self.settle(tool)
        unread = tool.act({'action': 'read_result', 'run_id': run_id})
        self.assertEqual(unread['run']['result'], 'Saved answer')
        self.assertEqual(unread['run']['error'], '')
        acked = tool.act({'action': 'acknowledge_result', 'run_id': run_id})
        self.assertTrue(acked['ok'])
        self.assertEqual(acked['acknowledged'], True)
        # Acknowledgement is idempotent: the store keeps the first timestamp.
        again = tool.act({'action': 'acknowledge_result', 'run_id': run_id})
        self.assertTrue(again['ok'])
        self.assertEqual(again['acknowledged'], True)
        self.assertEqual(tool.act({'action': 'list'})['jobs'][0]['id'], job['id'])
        missing = tool.act({'action': 'read_result', 'run_id': str(uuid.uuid4())})
        self.assertFalse(missing['ok'])
        self.assertEqual(missing['code'], 'unavailable')

    def test_readback_stays_consistent_after_a_scheduled_dispatch(self):
        tool = self.tool(self.succeed)
        job = self.create(tool)
        self.clock.advance(60)
        self.assertEqual(tool.scheduler.run_once()['dispatched'], 1)
        self.settle(tool)
        runs = tool.act({'action': 'list_runs', 'job_id': job['id']})['runs']
        self.assertEqual(runs[0]['state'], 'succeeded')
        self.assertEqual(runs[0]['trigger'], 'schedule')
        read = tool.act({'action': 'read_result', 'run_id': runs[0]['id']})
        self.assertEqual(read['run']['result'], 'Saved answer')
        current = tool.act({'action': 'get', 'job_id': job['id']})['job']
        self.assertEqual(current['last_scheduled'], runs[0]['scheduled_at'])


class GuardTests(ToolTests):
    def test_tool_requires_a_scheduler_service(self):
        with self.assertRaises(Exception):
            ScheduledJobsTool(self.store)

    def test_action_field_cannot_be_supplemented_by_extras(self):
        tool = self.tool()
        job = self.create(tool)
        reply = tool.act({'action': 'get', 'job_id': job['id'], 'limit': 5})
        self.assertFalse(reply['ok'])
        self.assertEqual(reply['code'], 'invalid')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
