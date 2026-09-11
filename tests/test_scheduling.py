from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from aios.scheduling import (
    Clock, Schedule, SchedulingError, parse_timestamp, timestamp, validate_job,
)


class FakeClock(Clock):
    def __init__(self, value='2026-01-01T00:00:00Z'):
        self.wall = parse_timestamp(value)
        self.elapsed = 0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.elapsed += max(seconds, 0)


def job_config(expression='* * * * *', provider='remote'):
    return {
        'title': 'Release summary', 'prompt': 'Summarize the approved release notes.',
        'schedule': {'kind': 'cron', 'value': expression, 'zone': 'UTC'},
        'execution': {'provider': provider, 'profile': 'default', 'model': 'test-model',
                      'capabilities': []},
    }


class SchedulingTests(unittest.TestCase):
    def schedule(self, expression, timezone='UTC'):
        return Schedule.parse({'kind': 'cron', 'value': expression, 'zone': timezone})

    def next_at(self, expression, after, timezone='UTC'):
        return timestamp(self.schedule(expression, timezone).next_after(parse_timestamp(after)))

    def test_once_requires_offset_minute_resolution_and_explicit_zone(self):
        for value in ('2026-01-02T00:00:00', '2026-01-02T00:00:01Z',
                      '2026-01-02T00:00:00.001Z', 'bad', 7):
            with self.subTest(value=value), self.assertRaises(SchedulingError):
                Schedule.parse({'kind': 'once', 'value': value, 'zone': 'UTC'})
        config = job_config()
        config['schedule'] = {'kind': 'once', 'value': '2026-01-02T10:30:00+05:30', 'zone': 'Asia/Kolkata'}
        validated = validate_job(config, FakeClock().now())
        schedule = Schedule.parse(validated['schedule'])
        self.assertEqual(schedule.value, '2026-01-02T05:00:00.000000Z')
        self.assertEqual(len(schedule.preview(FakeClock().now())), 1)
        self.assertIsNone(schedule.next_after(parse_timestamp(schedule.value)))

    def test_invalid_or_expired_configuration(self):
        config = job_config()
        invalid = [
            {**config, 'owner': 'someone'},
            {**config, 'title': ''},
            {**config, 'prompt': 'x' * (32768 + 1)},
            {**config, 'context': 'x' * (65536 + 1)},
            {**config, 'schedule': {'kind': 'once', 'value': '2025-01-01T00:00Z', 'zone': 'UTC'}},
            {**config, 'schedule': {'kind': 'cron', 'value': '* * * * *'}},
            {**config, 'schedule': {'kind': 'cron', 'value': '* * * * *', 'zone': 'No/SuchZone'}},
        ]
        for value in invalid:
            with self.subTest(value=str(value)[:100]), self.assertRaises(SchedulingError):
                validate_job(value, FakeClock().now())

    def test_execution_budget_and_secret_fields_rejected(self):
        for key, value in [('timeout_seconds', 901), ('timeout_seconds', True),
                           ('token_budget', 0), ('tool_budget', 33), ('missed_run', 'all'),
                           ('api_key', 'secret'), ('provider', 'fallback'),
                           ('capabilities', ['browser', 'browser']), ('capabilities', ['shell;exec'])]:
            config = job_config()
            config['execution'][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(SchedulingError):
                validate_job(config, FakeClock().now())

    def test_normalization_readback_and_input_not_mutated(self):
        config = job_config('  0  9  * * 1-5 ')
        config['notification'] = {'mode': 'actionable', 'quiet_hours': {
            'start': '22:00', 'end': '07:00', 'zone': 'Europe/London'},
            'snooze_until': '2026-01-03T10:00:00+01:00'}
        original = deepcopy(config)
        validated = validate_job(config, FakeClock().now())
        self.assertEqual(config, original)
        config['execution']['capabilities'].append('browser')
        self.assertEqual(validated['execution']['capabilities'], [])
        self.assertEqual(validated['execution']['timeout_seconds'], 900)
        self.assertEqual(validated['schedule']['value'], '0 9 * * 1-5')
        self.assertEqual(validated['notification']['snooze_until'], '2026-01-03T09:00:00.000000Z')

    def test_invalid_notification_policy(self):
        for policy in ({'mode': 'hidden'}, {'mode': 'all', 'quiet_hours': {
                'start': '25:00', 'end': '07:00', 'zone': 'UTC'}},
                {'mode': 'all', 'snooze_until': 'tomorrow'}, {'mode': 'all', 'extra': 1}):
            config = job_config()
            config['notification'] = policy
            with self.subTest(policy=policy), self.assertRaises(SchedulingError):
                validate_job(config, FakeClock().now())

    def test_rejects_extended_cron_and_command_syntax(self):
        for expression in ('@daily', '0 0 * * * echo hello', '0 0 * * MON',
                           '0 0 L * *', '0 0 * * 1#2', '0 0 * * ?', 'TZ=UTC 0 0 * * *',
                           '0 0 * * *;id', '*/0 * * * *', '60 * * * *', '* 24 * * *',
                           '* * 0 * *', '* * * 13 *', '* * * * 8', '4-2 * * * *',
                           '* * * * 1,,2', '0' * 201, '*/-1 * * * *'):
            with self.subTest(expression=expression), self.assertRaises(SchedulingError):
                self.schedule(expression)

    def test_lists_ranges_steps_and_sunday_alias(self):
        self.assertEqual(self.next_at('5,20-30/5 8-10 * * *', '2026-01-01T08:21:30Z'),
                         '2026-01-01T08:25:00.000000Z')
        self.assertEqual(self.next_at('0 9 * * 0', '2026-01-01T00:00Z'),
                         self.next_at('0 9 * * 7', '2026-01-01T00:00Z'))
        self.assertEqual(self.next_at('0 9 * * 0,7', '2026-01-01T00:00Z'),
                         '2026-01-04T09:00:00.000000Z')

    def test_dom_dow_or_including_impossible_dom_and_restricted_star(self):
        self.assertEqual(self.next_at('0 9 31 2 1', '2026-02-01T00:00Z'),
                         '2026-02-02T09:00:00.000000Z')
        self.assertEqual(self.next_at('0 9 15 * 1', '2026-01-01T00:00Z'),
                         '2026-01-05T09:00:00.000000Z')
        self.assertEqual(self.next_at('0 9 */2 * 1', '2026-01-01T10:00Z'),
                         '2026-01-03T09:00:00.000000Z')
        self.assertEqual(self.next_at('0 9 * * 1', '2026-01-01T00:00Z'),
                         '2026-01-05T09:00:00.000000Z')

    def test_impossible_dates_are_bounded(self):
        with self.assertRaisesRegex(SchedulingError, 'eight-year'):
            self.schedule('0 0 31 2 *').preview(FakeClock().now())

    def test_leap_days_century_and_month_end(self):
        self.assertEqual(self.next_at('0 0 29 2 *', '2096-03-01T00:00Z'),
                         '2104-02-29T00:00:00.000000Z')
        self.assertEqual(self.next_at('0 0 31 * *', '2026-04-01T00:00Z'),
                         '2026-05-31T00:00:00.000000Z')

    def test_spring_gap_is_skipped_and_autumn_uses_first_fold_only(self):
        timezone = 'America/New_York'
        self.assertEqual(self.next_at('30 2 * * *', '2026-03-08T05:00Z', timezone),
                         '2026-03-09T06:30:00.000000Z')
        schedule = self.schedule('30 1 * * *', timezone)
        preview = schedule.preview(parse_timestamp('2026-11-01T00:00Z'))
        self.assertEqual(preview[0]['utc'], '2026-11-01T05:30:00.000000Z')
        self.assertEqual(preview[1]['utc'], '2026-11-02T06:30:00.000000Z')
        self.assertEqual(self.next_at('30 1 * * *', '2026-11-01T06:00Z', timezone),
                         '2026-11-02T06:30:00.000000Z')

    def test_every_minute_never_repeats_during_fold(self):
        self.assertEqual(self.next_at('* * * * *', '2026-11-01T05:59Z', 'America/New_York'),
                         '2026-11-01T07:00:00.000000Z')
        count, _ = self.schedule('* * * * *', 'America/New_York').window(
            parse_timestamp('2026-11-01T04:00Z'), parse_timestamp('2026-11-02T04:59Z'))
        self.assertEqual(count, 1440)

    def test_non_hour_dst_and_skipped_calendar_day(self):
        self.assertEqual(self.next_at('15 2 * * *', '2026-10-03T00:00Z', 'Australia/Lord_Howe'),
                         '2026-10-04T15:15:00.000000Z')
        self.assertEqual(self.next_at('0 12 * * *', '2011-12-30T00:00Z', 'Pacific/Apia'),
                         '2011-12-30T22:00:00.000000Z')

    def test_window_counts_boundary_and_gap_minutes(self):
        schedule = self.schedule('* * * * *')
        count, latest = schedule.window(parse_timestamp('2026-01-01T00:00:01Z'),
                                        parse_timestamp('2026-01-02T00:00Z'))
        self.assertEqual(count, 1440)
        self.assertEqual(timestamp(latest), '2026-01-02T00:00:00.000000Z')
        count, _ = self.schedule('* * * * *', 'America/New_York').window(
            parse_timestamp('2026-03-08T05:00Z'), parse_timestamp('2026-03-09T03:59Z'))
        self.assertEqual(count, 1380)

    def test_large_clock_jump_counts_by_day_not_each_minute(self):
        schedule = self.schedule('* * * * *')
        start, end = parse_timestamp('2020-01-01T00:00Z'), parse_timestamp('2026-01-01T00:00Z')
        count, latest = schedule.window(start, end)
        self.assertEqual(count, int((end - start).total_seconds() // 60) + 1)
        self.assertEqual(latest, end)

    def test_fold_across_midnight_does_not_reverse_window_bounds(self):
        schedule = self.schedule('* * * * *', 'Antarctica/Casey')
        count, latest = schedule.window(parse_timestamp('2010-03-04T14:30Z'),
                                        parse_timestamp('2010-03-04T15:30Z'))
        self.assertEqual(count, 30)
        self.assertEqual(timestamp(latest), '2010-03-04T14:59:00.000000Z')

    def test_invalid_timestamp_bounds_and_naive_clock(self):
        for value in ('1969-01-01T00:00Z', '2200-01-01T00:00Z'):
            with self.assertRaises(SchedulingError):
                parse_timestamp(value)
        with self.assertRaises(SchedulingError):
            self.schedule('* * * * *').next_after(datetime(2026, 1, 1))

    def test_wall_clock_rollback_does_not_change_monotonic_deadline(self):
        clock = FakeClock()
        clock.advance(60)
        clock.advance(-120)
        self.assertEqual(clock.monotonic(), 60)
        self.assertEqual(clock.now(), parse_timestamp('2025-12-31T23:59Z'))
