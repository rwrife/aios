"""Desktop-only durable scheduling core; no worker execution or public socket."""
from contextlib import contextmanager
from datetime import timedelta
import getpass
import json
import os
from pathlib import Path
import sqlite3
import uuid

from . import core, principals
from .scheduling import (
    Clock, ConflictError, MAX_ACTIVE_RUNS, MAX_ENABLED_JOBS, MAX_RESULT_BYTES,
    MAX_STORE_BYTES, QuotaError, Schedule, SchedulingError, UnavailableError,
    integer, parse_timestamp, text, timestamp, validate_job, zone,
)


SCHEMA_VERSION = 2
ACTIVE = ('queued', 'running')
SCHEMA = (
    '''CREATE TABLE jobs (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, config TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN (0,1)),
        revision INTEGER NOT NULL CHECK(revision > 0),
        created TEXT NOT NULL, updated TEXT NOT NULL, next_due TEXT,
        last_scheduled TEXT, missed_count INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE INDEX due_jobs ON jobs(owner, enabled, next_due)''',
    '''CREATE TABLE runs (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        job_id TEXT NOT NULL REFERENCES jobs(id), owner TEXT NOT NULL,
        revision INTEGER NOT NULL, snapshot TEXT NOT NULL, scheduled_at TEXT NOT NULL,
        trigger TEXT NOT NULL CHECK(trigger IN ('schedule','manual')),
        request_id TEXT, state TEXT NOT NULL CHECK(state IN
        ('queued','running','succeeded','failed','cancelled','needs_user_action','interrupted','missed')),
        created TEXT NOT NULL, started TEXT, ended TEXT,
        result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        usage TEXT NOT NULL DEFAULT '{}', missed_count INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE UNIQUE INDEX recurring_occurrence ON runs(job_id, scheduled_at)
        WHERE trigger='schedule' ''',
    '''CREATE UNIQUE INDEX manual_request ON runs(owner, request_id)
        WHERE trigger='manual' ''',
    '''CREATE UNIQUE INDEX active_job ON runs(job_id)
        WHERE state IN ('queued','running')''',
    '''CREATE INDEX owner_runs ON runs(owner, sequence)''',
    '''CREATE TABLE manual_requests (
        owner TEXT NOT NULL, request_id TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, run_id TEXT NOT NULL,
        PRIMARY KEY(owner, request_id))''',
    '''CREATE TABLE outbox (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
        owner TEXT NOT NULL, outcome TEXT NOT NULL, created TEXT NOT NULL,
        acknowledged TEXT, notified TEXT,
        deliverable INTEGER NOT NULL DEFAULT 1 CHECK(deliverable IN (0,1)))''',
    '''CREATE INDEX unread_results ON outbox(owner, acknowledged, sequence)''',
)
MIGRATIONS = {
    1: (
        'ALTER TABLE outbox ADD COLUMN notified TEXT',
        'ALTER TABLE outbox ADD COLUMN deliverable INTEGER NOT NULL DEFAULT 1 '
        'CHECK(deliverable IN (0,1))',
        'PRAGMA user_version=2',
    ),
}


def identifier(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise SchedulingError('Invalid opaque identifier') from exc
    return value


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class ScheduledStore:
    def __init__(self, root=None, clock=None, *, protected=False):
        principal = principals.current()
        if principal is not None and not protected:
            raise UnavailableError('Scheduling is unavailable in protected workspaces')
        if protected and (principal is None or not principal.owner or root is not None):
            raise UnavailableError('Protected storage requires the broker owner workspace')
        self.owner = f'uid:{os.getuid()}' if hasattr(os, 'getuid') else f'user:{getpass.getuser()}'
        if protected:
            self.owner = principal.owner
        self.clock = clock or Clock()
        self.root = Path(root) if root is not None else core.data_dir() / 'scheduled'
        if self.root.is_symlink():
            raise UnavailableError('Scheduler storage must be a private directory')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'jobs.sqlite3'
        if self.path.is_symlink():
            raise UnavailableError('Scheduler database must be a regular private file')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        try:
            # DDL and the version marker commit together, including under first-open races.
            with self._transaction():
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                if version < 0 or version > SCHEMA_VERSION:
                    raise UnavailableError('Unsupported scheduler database version')
                if version == 0:
                    for statement in SCHEMA:
                        self.db.execute(statement)
                    self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                else:
                    while version < SCHEMA_VERSION:
                        migration = MIGRATIONS.get(version)
                        if migration is None:
                            raise UnavailableError('Unsupported scheduler database version')
                        for statement in migration:
                            self.db.execute(statement)
                        version = self.db.execute('PRAGMA user_version').fetchone()[0]
            page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
            self.db.execute(f'PRAGMA max_page_count={MAX_STORE_BYTES // page_size}')
        except (sqlite3.Error, SchedulingError):
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def _transaction(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            yield

    def _now(self):
        return timestamp(self.clock.now())

    def _space(self):
        pages = self.db.execute('PRAGMA page_count').fetchone()[0]
        free = self.db.execute('PRAGMA freelist_count').fetchone()[0]
        size = self.db.execute('PRAGMA page_size').fetchone()[0]
        # Leave room for the two active workers to commit their bounded results.
        if (pages - free) * size >= MAX_STORE_BYTES - 4 * 1024 * 1024:
            raise QuotaError('Scheduler storage is full; acknowledge or delete retained results')

    def _job(self, job_id, revision=None):
        identifier(job_id)
        row = self.db.execute('SELECT * FROM jobs WHERE id=? AND owner=? AND deleted=0',
                              (job_id, self.owner)).fetchone()
        if row is None:
            raise UnavailableError('Job unavailable')
        if revision is not None:
            integer(revision, 'expected revision', 1, 2 ** 63 - 1)
            if revision != row['revision']:
                raise ConflictError('Job changed; read the current revision')
        return row

    @staticmethod
    def _readback(row):
        result = dict(row)
        config = json.loads(result.pop('config'))
        result.update(config)
        result['enabled'] = bool(result['enabled'])
        result.pop('deleted')
        result['next_local'] = None
        if result['next_due']:
            result['next_local'] = parse_timestamp(result['next_due']).astimezone(
                zone(config['schedule']['zone'])).isoformat()
        return result

    def get(self, job_id):
        return self._readback(self._job(job_id))

    def list_jobs(self, limit=50, after=None):
        integer(limit, 'page limit', 1, 100)
        if after is not None:
            identifier(after)
        return [self._readback(row) for row in self.db.execute(
            '''SELECT * FROM jobs WHERE owner=? AND deleted=0 AND (? IS NULL OR id>?)
               ORDER BY id LIMIT ?''', (self.owner, after, after, limit))]

    def _enabled_quota(self):
        count = self.db.execute('SELECT count(*) FROM jobs WHERE owner=? AND enabled=1',
                                (self.owner,)).fetchone()[0]
        if count >= MAX_ENABLED_JOBS:
            raise QuotaError('At most 100 enabled jobs are allowed')

    def create(self, config):
        now = self.clock.now()
        config = validate_job(config, now)
        due = Schedule.parse(config['schedule']).next_after(now)
        job_id = str(uuid.uuid4())
        with self._transaction():
            self._space()
            self._enabled_quota()
            self.db.execute('''INSERT INTO jobs
                (id,owner,config,enabled,revision,created,updated,next_due)
                VALUES (?,?,?,1,1,?,?,?)''',
                (job_id, self.owner, encode(config), timestamp(now), timestamp(now), timestamp(due)))
        return self.get(job_id)

    def update(self, job_id, expected_revision, config):
        integer(expected_revision, 'expected revision', 1, 2 ** 63 - 1)
        now = self.clock.now()
        config = validate_job(config, now)
        with self._transaction():
            self._space()
            row = self._job(job_id, expected_revision)
            floor = max(now, parse_timestamp(row['last_scheduled'])) if row['last_scheduled'] else now
            due = Schedule.parse(config['schedule']).next_after(floor)
            if due is None:
                raise ConflictError('One-shot occurrence precedes the job occurrence watermark')
            self.db.execute('''UPDATE jobs SET config=?,revision=revision+1,updated=?,next_due=?
                WHERE id=?''', (encode(config), timestamp(now), timestamp(due) if row['enabled'] else None, job_id))
        return self.get(job_id)

    def pause(self, job_id, expected_revision):
        integer(expected_revision, 'expected revision', 1, 2 ** 63 - 1)
        with self._transaction():
            self._job(job_id, expected_revision)
            self.db.execute('''UPDATE jobs SET enabled=0,next_due=NULL,revision=revision+1,updated=?
                WHERE id=?''', (self._now(), job_id))
        return self.get(job_id)

    def resume(self, job_id, expected_revision):
        integer(expected_revision, 'expected revision', 1, 2 ** 63 - 1)
        with self._transaction():
            self._space()
            row = self._job(job_id, expected_revision)
            if row['enabled']:
                return self._readback(row)
            self._enabled_quota()
            now = self.clock.now()
            floor = max(now, parse_timestamp(row['last_scheduled'])) if row['last_scheduled'] else now
            due = Schedule.parse(json.loads(row['config'])['schedule']).next_after(floor)
            if due is None:
                raise SchedulingError('Expired one-shot must be edited before resuming')
            self.db.execute('''UPDATE jobs SET enabled=1,next_due=?,revision=revision+1,updated=?
                WHERE id=?''', (timestamp(due), timestamp(now), job_id))
        return self.get(job_id)

    def _capacity(self, job):
        rows = self.db.execute('''SELECT job_id,snapshot FROM runs
            WHERE owner=? AND state IN ('queued','running')''', (self.owner,)).fetchall()
        if len(rows) >= MAX_ACTIVE_RUNS or any(row['job_id'] == job['id'] for row in rows):
            return False
        local = json.loads(job['config'])['execution']['provider'] == 'local'
        return not local or not any(
            json.loads(row['snapshot'])['execution']['provider'] == 'local' for row in rows)

    def _insert_run(self, job, instant, trigger, request_id=None, missed=0):
        self._space()
        run_id = str(uuid.uuid4())
        snapshot = self._readback(job)
        self.db.execute('''INSERT INTO runs
            (id,job_id,owner,revision,snapshot,scheduled_at,trigger,request_id,state,created,missed_count)
            VALUES (?,?,?,?,?,?,?,?,'queued',?,?)''',
            (run_id, job['id'], self.owner, job['revision'], encode(snapshot), timestamp(instant),
             trigger, request_id, self._now(), missed))
        return run_id

    def claim_due(self):
        """Claim due jobs atomically; overlap stays coalesced in next_due."""
        claimed = []
        now = self.clock.now()
        with self._transaction():
            self._space()
            jobs = self.db.execute('''SELECT * FROM jobs
                WHERE owner=? AND enabled=1 AND deleted=0 AND next_due<=?
                ORDER BY next_due,id LIMIT ?''', (self.owner, timestamp(now), MAX_ENABLED_JOBS)).fetchall()
            for job in jobs:
                # Do not consume an occurrence while its preceding run is active.
                if not self._capacity(job):
                    continue
                config = json.loads(job['config'])
                schedule = Schedule.parse(config['schedule'])
                due = parse_timestamp(job['next_due'])
                count, latest = schedule.window(due, now)
                if latest is None:
                    raise SchedulingError('Stored due time is inconsistent with the schedule')
                late = now - latest >= timedelta(minutes=1)
                expired_once = schedule.kind == 'once' and late
                skip = (config['execution']['missed_run'] == 'skip' and late
                        or now - latest > timedelta(hours=24))
                missed = count if skip or expired_once else count - 1
                if expired_once:
                    run_id = self._insert_run(job, latest, 'schedule', missed=missed)
                    self._finish(run_id, 'missed', '', 'One-shot occurrence expired before dispatch')
                elif not skip:
                    run_id = self._insert_run(job, latest, 'schedule', missed=missed)
                    claimed.append(run_id)
                following = schedule.next_after(max(now, latest))
                self.db.execute('''UPDATE jobs SET next_due=?,last_scheduled=?,
                    missed_count=missed_count+?,updated=?,enabled=? WHERE id=?''',
                    (timestamp(following) if following else None, timestamp(latest), missed,
                     timestamp(now), int(following is not None), job['id']))
        return [self.get_run(run_id) for run_id in claimed]

    def run_now(self, job_id, expected_revision, request_id):
        integer(expected_revision, 'expected revision', 1, 2 ** 63 - 1)
        identifier(request_id)
        with self._transaction():
            job = self._job(job_id)
            old = self.db.execute('SELECT * FROM manual_requests WHERE owner=? AND request_id=?',
                                  (self.owner, request_id)).fetchone()
            if old:
                if old['job_id'] != job_id or old['revision'] != expected_revision:
                    raise ConflictError('Request ID belongs to another job revision')
                # If retention removed the result, report unavailable, never execute again.
                return self.get_run(old['run_id'])
            self._job(job_id, expected_revision)
            self._space()
            if not self._capacity(job):
                raise QuotaError('Job or worker capacity is already occupied')
            run_id = self._insert_run(job, self.clock.now(), 'manual', request_id)
            self.db.execute('INSERT INTO manual_requests VALUES (?,?,?,?,?)',
                            (self.owner, request_id, job_id, expected_revision, run_id))
        return self.get_run(run_id)

    def get_run(self, run_id):
        identifier(run_id)
        row = self.db.execute('SELECT * FROM runs WHERE id=? AND owner=?',
                              (run_id, self.owner)).fetchone()
        if row is None:
            raise UnavailableError('Run unavailable')
        result = dict(row)
        result['snapshot'] = json.loads(result['snapshot'])
        result['usage'] = json.loads(result['usage'])
        return result

    def list_runs(self, job_id, limit=20, before=None):
        self._job(job_id)
        integer(limit, 'page limit', 1, 100)
        if before is not None:
            integer(before, 'run cursor', 1, 2 ** 63 - 1)
        # Bodies and snapshots are fetched individually, not multiplied by page size.
        return [dict(row) for row in self.db.execute('''SELECT sequence,id,job_id,revision,
            scheduled_at,trigger,state,started,ended,missed_count FROM runs
            WHERE owner=? AND job_id=? AND (? IS NULL OR sequence<?)
            ORDER BY sequence DESC LIMIT ?''', (self.owner, job_id, before, before, limit))]

    def start(self, run_id):
        with self._transaction():
            run = self.get_run(run_id)
            if run['state'] != 'queued':
                raise ConflictError('Only a queued run can start')
            self.db.execute("UPDATE runs SET state='running',started=? WHERE id=?", (self._now(), run_id))
        return self.get_run(run_id)

    def active_runs(self):
        return [self.get_run(row['id']) for row in self.db.execute(
            "SELECT id FROM runs WHERE owner=? AND state IN ('queued','running')",
            (self.owner,))]

    def block(self, run_id, error):
        """Commit one action-needed result and pause the still-matching binding."""
        text(error, 'safe error', 4096)
        with self._transaction():
            run = self.get_run(run_id)
            self._finish(run_id, 'needs_user_action', '', error)
            row = self._job(run['job_id'])
            if json.loads(row['config'])['execution'] == run['snapshot']['execution']:
                self.db.execute('''UPDATE jobs SET enabled=0,next_due=NULL,
                    revision=revision+1,updated=? WHERE id=?''', (self._now(), row['id']))
        return self.get_run(run_id)

    def _finish(self, run_id, state, result, error, usage=None, outcome=None):
        run = self.get_run(run_id)
        if run['state'] not in ACTIVE:
            raise ConflictError('Run is already terminal')
        now = self._now()
        self.db.execute('''UPDATE runs SET state=?,ended=?,result=?,error=?,usage=? WHERE id=?''',
                        (state, now, result, error, encode(usage or {}), run_id))
        # Persist before any future notification bridge can observe the result.
        saved_outcome = outcome or state
        notification = run['snapshot'].get('notification', {'mode': 'all'})
        deliverable = not (
            state == 'succeeded'
            and notification.get('mode') == 'actionable'
            and saved_outcome == 'unchanged'
        )
        self.db.execute('''INSERT INTO outbox
            (run_id,owner,outcome,created,deliverable) VALUES (?,?,?,?,?)''',
            (run_id, self.owner, saved_outcome, now, int(deliverable)))

    def finish(self, run_id, state, result='', error='', usage=None, outcome=None):
        if state not in ('succeeded', 'failed', 'needs_user_action'):
            raise SchedulingError('Invalid worker terminal state')
        text(result, 'result', MAX_RESULT_BYTES, empty=True)
        text(error, 'safe error', 4096, empty=True)
        if outcome not in (None, 'changed', 'unchanged', 'needs_user_action'):
            outcome = None
        if usage is not None:
            if not isinstance(usage, dict) or usage.keys() - {'input_tokens', 'output_tokens', 'tool_calls'}:
                raise SchedulingError('Invalid usage record')
            for key, value in usage.items():
                integer(value, key, 0, 2 ** 31 - 1)
        with self._transaction():
            if self.get_run(run_id)['state'] != 'running':
                raise ConflictError('Only a running worker can complete')
            self._finish(run_id, state, result, error, usage, outcome if state == 'succeeded' else state)
        return self.get_run(run_id)

    def cancel(self, run_id):
        """Record cancellation after the supervisor has stopped the worker."""
        with self._transaction():
            self._finish(run_id, 'cancelled', '', '')
        return self.get_run(run_id)

    def recover(self):
        """Called by the future singleton supervisor after cleaning up old workers."""
        with self._transaction():
            runs = self.db.execute('''SELECT id FROM runs
                WHERE owner=? AND state IN ('queued','running')''', (self.owner,)).fetchall()
            for run in runs:
                self._finish(run['id'], 'interrupted', '', 'Scheduler stopped before the run completed')
        return len(runs)

    def delete(self, job_id, expected_revision):
        """Tombstone configuration after the supervisor has stopped its workers."""
        integer(expected_revision, 'expected revision', 1, 2 ** 63 - 1)
        with self._transaction():
            self._job(job_id, expected_revision)
            runs = self.db.execute('''SELECT id FROM runs WHERE job_id=?
                AND state IN ('queued','running')''', (job_id,)).fetchall()
            for run in runs:
                self._finish(run['id'], 'cancelled', '', 'Job deleted')
            self.db.execute('''UPDATE jobs SET deleted=1,enabled=0,config='{}',next_due=NULL,
                revision=revision+1,updated=? WHERE id=?''', (self._now(), job_id))

    def unread(self, limit=50, after=0):
        integer(limit, 'page limit', 1, 100)
        integer(after, 'outbox cursor', 0, 2 ** 63 - 1)
        rows = self.db.execute('''SELECT o.*,r.job_id,r.state,r.scheduled_at,r.started,r.ended,
            r.snapshot FROM outbox o JOIN runs r ON r.id=o.run_id
            WHERE o.owner=? AND o.acknowledged IS NULL AND o.deliverable=1
                AND o.sequence>? ORDER BY o.sequence LIMIT ?''',
            (self.owner, after, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            snapshot = json.loads(item.pop('snapshot'))
            item['title'] = snapshot.get('title', 'Scheduled result')
            item['notification'] = snapshot.get('notification', {'mode': 'all'})
            item['suppressed_until'] = self._suppressed_until(item['notification'])
            result.append(item)
        return result

    def _suppressed_until(self, notification):
        now = self.clock.now()
        release = None
        snooze = notification.get('snooze_until')
        if snooze:
            instant = parse_timestamp(snooze)
            if instant > now:
                release = instant
        quiet = notification.get('quiet_hours')
        if quiet:
            local = now.astimezone(zone(quiet['zone']))
            start_hour, start_minute = (int(part) for part in quiet['start'].split(':'))
            end_hour, end_minute = (int(part) for part in quiet['end'].split(':'))
            start = local.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
            end = local.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
            if start < end:
                active = start <= local < end
            else:
                active = local >= start or local < end
                if local >= start:
                    end += timedelta(days=1)
            if active and local < start:
                start -= timedelta(days=1)
            if active:
                quiet_release = end.astimezone(now.tzinfo)
                release = max(release, quiet_release) if release else quiet_release
        return timestamp(release) if release else None

    def mark_notified(self, run_id):
        with self._transaction():
            self.get_run(run_id)
            changed = self.db.execute('''UPDATE outbox SET notified=coalesce(notified,?)
                WHERE run_id=? AND owner=? AND acknowledged IS NULL AND deliverable=1''',
                (self._now(), run_id, self.owner))
            if changed.rowcount != 1:
                raise ConflictError('Only unread feedback can be marked notified')

    def acknowledge(self, run_id):
        with self._transaction():
            self.get_run(run_id)
            changed = self.db.execute('''UPDATE outbox SET acknowledged=coalesce(acknowledged,?)
                WHERE run_id=? AND owner=?''', (self._now(), run_id, self.owner))
            if changed.rowcount != 1:
                raise ConflictError('Only a terminal result can be acknowledged')

    def prune(self):
        """Keep unread results, plus at most 1,000 terminal runs younger than 30 days."""
        cutoff = timestamp(self.clock.now() - timedelta(days=30))
        with self._transaction():
            rows = self.db.execute('''SELECT r.id,r.ended,o.acknowledged FROM runs r
                JOIN outbox o ON o.run_id=r.id WHERE r.owner=? ORDER BY r.sequence DESC''',
                (self.owner,)).fetchall()
            removed = 0
            for index, row in enumerate(rows):
                if row['acknowledged'] is not None and (index >= 1000 or row['ended'] < cutoff):
                    self.db.execute('DELETE FROM runs WHERE id=?', (row['id'],))
                    removed += 1
            self.db.execute('''DELETE FROM jobs WHERE owner=? AND deleted=1
                AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.job_id=jobs.id)''', (self.owner,))
        return removed
