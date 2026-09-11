"""Validated job snapshots and bounded, wall-clock scheduling calculations."""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, time as daytime, timedelta, timezone
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cronsim import CronSim, CronSimError


UTC = timezone.utc
SEARCH_DAYS = 366 * 8
MAX_ENABLED_JOBS = 100
MAX_ACTIVE_RUNS = 2
MAX_STORE_BYTES = 100 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024
CRON_TERM = r'(?:\*|[0-9]{1,2}(?:-[0-9]{1,2})?)(?:/[0-9]{1,2})?'
CRON_FIELD = re.compile(CRON_TERM + r'(?:,' + CRON_TERM + r')*', re.ASCII)


class SchedulingError(ValueError):
    code = 'invalid'


class ConflictError(SchedulingError):
    code = 'conflict'


class QuotaError(SchedulingError):
    code = 'quota_exceeded'


class UnavailableError(SchedulingError):
    code = 'unavailable'


class Clock:
    def now(self):
        return datetime.now(UTC)

    def monotonic(self):
        return time.monotonic()


def utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SchedulingError('Use a timestamp with an explicit UTC offset')
    result = value.astimezone(UTC)
    if not 1970 <= result.year <= 2199:
        raise SchedulingError('Timestamp must be between 1970 and 2199')
    return result


def timestamp(value):
    return utc(value).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def parse_timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise SchedulingError('Invalid timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise SchedulingError('Invalid timestamp') from exc
    return utc(result)


def text(value, name, maximum, empty=False):
    if not isinstance(value, str) or '\0' in value:
        raise SchedulingError(f'Invalid {name}')
    if (not empty and not value.strip()) or len(value.encode('utf-8')) > maximum:
        raise SchedulingError(f'{name} exceeds its text limit or is empty')
    return value


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        raise SchedulingError('Missing required configuration fields')
    if value.keys() - set(required) - set(optional):
        raise SchedulingError('Unknown configuration fields')


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise SchedulingError(f'Invalid {name}')
    return value


def zone(value):
    text(value, 'IANA time zone', 100)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SchedulingError('Choose an installed IANA time zone') from exc


class Cron:
    """Keep CronSim's field parser behind our numeric-only and DST contract.

    Iterate calendar days rather than minutes, so counting years of missed
    minute jobs remains bounded. CronSim's Debian DST adjustments are not used.
    """
    def __init__(self, expression):
        text(expression, 'cron expression', 200)
        parts = expression.split()
        if len(parts) != 5 or any(not CRON_FIELD.fullmatch(part) for part in parts):
            raise SchedulingError('Use five numeric cron fields, without commands or macros')
        self.expression = ' '.join(parts)
        try:
            # Parse DOM separately: CronSim rejects February 31 before applying
            # DOM/DOW OR, although a restricted weekday can make that valid.
            parsed = CronSim(' '.join(parts[:2] + ['*'] + parts[3:]), datetime(2024, 1, 1))
            days = CronSim(f'0 0 {parts[2]} * *', datetime(2024, 1, 1))
        except CronSimError as exc:
            raise SchedulingError('Invalid cron field range or step') from exc
        self.days = days.days
        self.months = parsed.months
        self.weekdays = {day % 7 for day in parsed.weekdays}
        self.dom_restricted = parts[2] != '*'
        self.dow_restricted = parts[4] != '*'
        self.minutes = sorted(hour * 60 + minute for hour in parsed.hours for minute in parsed.minutes)

    def matches_day(self, day):
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.weekday() + 1) % 7 in self.weekdays
        if self.dom_restricted and self.dow_restricted:
            return dom or dow
        return dom and dow

    @staticmethod
    def instant(day, minute, tz):
        local = datetime.combine(day, daytime(minute // 60, minute % 60), tz).replace(fold=0)
        instant = local.astimezone(UTC)
        # A UTC round trip rejects gaps. fold=0 chooses only the first copy.
        if instant.astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
            return None
        return instant

    def next_after(self, after, tz):
        after = utc(after)
        local = after.astimezone(tz)
        day = local.date()
        for offset in range(SEARCH_DAYS + 1):
            if day.year > 2199:
                break
            if self.matches_day(day):
                start = bisect_right(self.minutes, local.hour * 60 + local.minute) if offset == 0 else 0
                for minute in self.minutes[start:]:
                    candidate = self.instant(day, minute, tz)
                    if candidate is not None and candidate > after:
                        return candidate
            day += timedelta(days=1)
        raise SchedulingError('No occurrence within the eight-year search horizon')

    def window(self, start, end, tz):
        """Return (count, latest UTC instant) for an inclusive UTC window."""
        start, end = utc(start), utc(end)
        if end < start:
            return 0, None
        # Local dates can move backwards across midnight during a fold. UTC
        # bounds plus one day cover all zoneinfo offsets without assuming order.
        day = start.date() - timedelta(days=1)
        final = end.date() + timedelta(days=1)
        count, latest = 0, None
        while day <= final:
            if self.matches_day(day):
                midnight = datetime.combine(day, daytime(), tz)
                tomorrow = midnight + timedelta(days=1)
                # On complete ordinary days every wall minute has one instant.
                # Boundary and offset-transition days use explicit round trips.
                if (midnight.astimezone(UTC) >= start and tomorrow.astimezone(UTC) <= end
                        and midnight.utcoffset() == tomorrow.utcoffset()
                        and midnight.utcoffset() == (midnight + timedelta(hours=12)).utcoffset()):
                    count += len(self.minutes)
                    candidate = self.instant(day, self.minutes[-1], tz)
                    if candidate is not None and (latest is None or candidate > latest):
                        latest = candidate
                else:
                    for minute in self.minutes:
                        candidate = self.instant(day, minute, tz)
                        if candidate is not None and start <= candidate <= end:
                            count += 1
                            if latest is None or candidate > latest:
                                latest = candidate
            day += timedelta(days=1)
        return count, latest


@dataclass(frozen=True)
class Schedule:
    kind: str
    value: str
    zone: str

    @classmethod
    def parse(cls, value):
        fields(value, ('kind', 'value', 'zone'))
        zone(value['zone'])
        if value['kind'] == 'once':
            instant = parse_timestamp(value['value'])
            if instant.second or instant.microsecond:
                raise SchedulingError('Schedules have minute resolution')
            normalized = timestamp(instant)
        elif value['kind'] == 'cron':
            normalized = Cron(value['value']).expression
        else:
            raise SchedulingError('Schedule kind must be once or cron')
        return cls(value['kind'], normalized, value['zone'])

    def as_dict(self):
        return {'kind': self.kind, 'value': self.value, 'zone': self.zone}

    def next_after(self, after):
        after = utc(after)
        if self.kind == 'once':
            instant = parse_timestamp(self.value)
            return instant if instant > after else None
        return Cron(self.value).next_after(after, zone(self.zone))

    def preview(self, after):
        result = []
        for _ in range(3):
            after = self.next_after(after)
            if after is None:
                break
            result.append({'utc': timestamp(after), 'local': after.astimezone(zone(self.zone)).isoformat()})
        return result

    def window(self, start, end):
        if self.kind == 'once':
            instant = parse_timestamp(self.value)
            return (1, instant) if utc(start) <= instant <= utc(end) else (0, None)
        return Cron(self.value).window(start, end, zone(self.zone))


def validate_job(value, now):
    """Return a fresh JSON-safe configuration, never credentials or ownership."""
    fields(value, ('title', 'prompt', 'schedule', 'execution'),
           ('context', 'conversation', 'notification'))
    schedule = Schedule.parse(value['schedule'])
    if not schedule.preview(now):
        raise SchedulingError('Choose a future one-shot timestamp')
    execution = value['execution']
    fields(execution, ('provider', 'profile', 'model', 'capabilities'),
           ('timeout_seconds', 'token_budget', 'tool_budget', 'missed_run'))
    if execution['provider'] not in ('local', 'remote', 'subscription'):
        raise SchedulingError('Invalid provider binding')
    capabilities = execution['capabilities']
    if (not isinstance(capabilities, list) or len(capabilities) > 32
            or any(not isinstance(item, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', item)
                   for item in capabilities) or len(set(capabilities)) != len(capabilities)):
        raise SchedulingError('Invalid saved capabilities')
    if execution.get('missed_run', 'coalesce') not in ('coalesce', 'skip'):
        raise SchedulingError('Invalid missed-run policy')
    notification = value.get('notification', {'mode': 'all'})
    fields(notification, ('mode',), ('quiet_hours', 'snooze_until'))
    if notification['mode'] not in ('all', 'actionable'):
        raise SchedulingError('Invalid notification mode')
    notification = dict(notification)
    if notification.get('quiet_hours') is not None:
        quiet = notification['quiet_hours']
        fields(quiet, ('start', 'end', 'zone'))
        for name in ('start', 'end'):
            if not isinstance(quiet[name], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', quiet[name]):
                raise SchedulingError('Quiet hours require HH:MM times')
        if quiet['start'] == quiet['end']:
            raise SchedulingError('Quiet hours must have distinct start and end times')
        zone(quiet['zone'])
        notification['quiet_hours'] = dict(quiet)
    if notification.get('snooze_until') is not None:
        notification['snooze_until'] = timestamp(parse_timestamp(notification['snooze_until']))
    return {
        'title': text(value['title'], 'title', 200).strip(),
        'prompt': text(value['prompt'], 'prompt', 32 * 1024),
        'context': text(value.get('context', ''), 'context', 64 * 1024, empty=True),
        'conversation': text(value.get('conversation', ''), 'conversation reference', 200, empty=True),
        'schedule': schedule.as_dict(),
        'execution': {
            'provider': execution['provider'],
            'profile': text(execution['profile'], 'provider profile reference', 200),
            'model': text(execution['model'], 'model', 200),
            'capabilities': list(capabilities),
            'timeout_seconds': integer(execution.get('timeout_seconds', 900), 'timeout', 1, 900),
            'token_budget': integer(execution.get('token_budget', 8192), 'token budget', 1, 32768),
            'tool_budget': integer(execution.get('tool_budget', 16), 'tool budget', 0, 32),
            'missed_run': execution.get('missed_run', 'coalesce'),
        },
        'notification': notification,
    }
