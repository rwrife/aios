"""Owner-scoped durable task records with crash-safe side-effect journals.

Issue #167 asks that "Continue yesterday's work" reopens the correct user's
task with its inputs, outputs, generated tools and meaningful progress, and
that recovery never blindly repeats an external side effect. This module is
the durable bookkeeping half of that promise: a per-owner store of task
records, bounded checkpoints, artifact references, explicit attempts, and a
two-phase journal for effects that touch the world outside AIOS.

Deliberate boundaries (a slice, not the whole issue):

- This layer stores and validates; it never executes anything. The contract
  for an executor (a chat worker or the future scheduler) is: call
  ``begin_effect`` *before* an external mutation, ``commit_effect`` after it
  succeeds (or ``fail_effect`` after it fails). An effect left ``pending``
  when the process dies becomes ``unverified`` at recovery, which blocks
  ``resume`` until a user resolves it explicitly as having happened
  (``completed``) or definitely not having happened (``skipped``). Recovery
  therefore never replays a purchase, message, or write on its own.
- Window lifetime is separate from task lifetime: nothing here is tied to a
  chat window or process. Tasks outlive both by construction.
- Provider/model/capability bindings are stored as *references* only. No key,
  PIN, token, or sign-in capability is persisted, and the module rejects
  secret-shaped payloads fail-closed. ``resume`` revalidates the saved
  binding against the caller's current configuration; a mismatch produces
  ``needs_user_action`` rather than silently rerouting work.
- Ownership is a caller-independent owner UUID supplied to the store
  constructor by the trusted service; a model-supplied owner field is never
  consulted. Reads and searches are owner-filtered in every query, so one
  owner can neither discover nor resume another owner's task, and stale
  identity state raises instead of falling back.
- Ephemeral (guest) tasks carry a TTL, are purged by ``expire``, and an
  ephemeral store can be destroyed on close; a failed destroy quarantines
  the files instead of claiming cleanup succeeded.

Broker wiring (per-chat authenticated execution contexts), the compact task
list and conversational reopening in the shell, and the installed-VM reboot
acceptance depend on the account issues (#159/#160) and remain open.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1
STATUSES = ('active', 'needs_user_action', 'interrupted', 'completed', 'cancelled',
            'archived')
ATTEMPT_OUTCOMES = ('running', 'completed', 'cancelled', 'interrupted', 'needs_user_action')
EFFECT_STATES = ('pending', 'executed', 'failed', 'unverified', 'resolved_completed',
                 'resolved_skipped')
ARTIFACT_KINDS = ('document', 'tool', 'app', 'file')
RESOLVED_OUTCOMES = ('completed', 'skipped')

MAX_TITLE = 200
MAX_PROMPT = 32768
MAX_STEP = 200
MAX_CHECKPOINT_CONTEXT = 8192
MAX_TASKS_PER_OWNER = 1000
MAX_CHECKPOINTS_PER_TASK = 512
MAX_CAPABILITIES = 32
MAX_RECONSTRUCT_BYTES = 64 * 1024
MAX_EPOCH_EFFECTS = 200
MAX_SEARCH_RESULTS = 50
QUARANTINE_PREFIX = '.aios-quarantined-'

_TRANSITIONS = {
    'active': ('needs_user_action', 'interrupted', 'completed', 'cancelled', 'archived'),
    'needs_user_action': ('active', 'cancelled', 'archived'),
    'interrupted': ('needs_user_action', 'active', 'cancelled', 'archived'),
    'completed': ('archived',),
    'cancelled': ('archived',),
    'archived': ('active',),
}


class RecoveryError(ValueError):
    """Expected task-recovery failure with a user-safe message."""


class UnknownTask(RecoveryError):
    """The requested task does not exist for this owner."""


class Conflict(RecoveryError):
    """The requested action is not valid for the task's current state."""


def identifier(value, name='identifier'):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise RecoveryError(f'Invalid {name}') from exc
    return value


def text(value, name, maximum, empty=False):
    if not isinstance(value, str) or '\0' in value:
        raise RecoveryError(f'Invalid {name}')
    if (not empty and not value.strip()) or len(value.encode('utf-8')) > maximum:
        raise RecoveryError(f'{name} exceeds its limit or is empty')
    return value


def capabilities(values):
    if not isinstance(values, (list, tuple)) or len(values) > MAX_CAPABILITIES:
        raise RecoveryError('Capabilities must be a bounded list')
    result = []
    for item in values:
        if not isinstance(item, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,99}', item):
            raise RecoveryError('Invalid capability name')
        if item not in result:
            result.append(item)
    return list(result)


def reference(value, name='reference'):
    """Opaque artifact/tool reference: printable, bounded, never a live secret."""
    text(value, name, 400)
    if not value.strip() or any(ch in value for ch in '\r\n') or '=' in value:
        # Reject credential-shaped assignments (``token=abc``) and control text.
        raise RecoveryError(f'Invalid {name}')
    return value.strip()


def digest(value):
    if isinstance(value, str):
        payload = value.encode('utf-8')
    else:
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                                 sort_keys=True, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError) as exc:
            raise RecoveryError('Effect arguments must be JSON-safe') from exc
    if len(payload) > MAX_CHECKPOINT_CONTEXT:
        raise RecoveryError('Effect arguments exceed the digest limit')
    return hashlib.sha256(payload).hexdigest()


SCHEMA = (
    '''CREATE TABLE tasks (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL,
        title TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        capabilities TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL CHECK(status IN
        ('active','needs_user_action','interrupted','completed','cancelled','archived')),
        ephemeral INTEGER NOT NULL DEFAULT 0 CHECK(ephemeral IN (0,1)),
        note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL CHECK(revision > 0),
        created REAL NOT NULL, updated REAL NOT NULL, expires REAL)''',
    '''CREATE INDEX owner_tasks ON tasks(owner, status, updated)''',
    '''CREATE TABLE attempts (
        id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        number INTEGER NOT NULL, started REAL NOT NULL, ended REAL,
        outcome TEXT NOT NULL DEFAULT 'running' CHECK(outcome IN
        ('running','completed','cancelled','interrupted','needs_user_action')),
        UNIQUE(task, number))''',
    '''CREATE INDEX running_attempts ON attempts(outcome)''',
    '''CREATE TABLE checkpoints (
        task TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, step TEXT NOT NULL,
        context TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        PRIMARY KEY(task, seq))''',
    '''CREATE TABLE artifacts (
        task TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK(kind IN ('document','tool','app','file')),
        ref TEXT NOT NULL, sha256 TEXT, created REAL NOT NULL,
        PRIMARY KEY(task, kind, ref))''',
    '''CREATE TABLE effects (
        id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        attempt TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
        operation TEXT NOT NULL, target TEXT NOT NULL, digest TEXT NOT NULL,
        nonce TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
        ('pending','executed','failed','unverified','resolved_completed','resolved_skipped')),
        note TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(task, operation, target, digest))''',
    '''CREATE INDEX effect_states ON effects(task, state)''',
)


class TaskStore:
    """Durable task journal for one trusted service context.

    ``owner`` is the authenticated account UUID established by the service
    (never by a model field). ``root`` holds the SQLite file; for guests the
    root lives inside session-private storage and TTLs are enforced.
    """

    def __init__(self, root, owner, clock=time.time):
        self.owner = identifier(owner, 'owner')
        self.clock = clock
        directory = Path(root)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / 'tasks.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.path.chmod(0o600)
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            self.db.executescript(';\n'.join(SCHEMA)
                                  + f';\nPRAGMA user_version={SCHEMA_VERSION};')
        elif version != SCHEMA_VERSION:
            self.db.close()
            raise RecoveryError('Unsupported task store version')
        self.recovery = self.recover()

    # ------------------------------------------------------------- recovery

    def recover(self):
        """Adopt abandoned work fail-closed; returns the recovery report.

        Runs that were still executing when the previous process died become
        ``interrupted``; their world-affecting effects still marked
        ``pending`` become ``unverified`` and will block resume until a user
        resolves them. Nothing here is retried automatically.
        """
        now = self.clock()
        report = {'interrupted_attempts': [], 'interrupted_tasks': [],
                  'unverified_effects': []}
        running = self.db.execute(
            "SELECT id, task FROM attempts WHERE outcome='running'").fetchall()
        with self.db:
            for row in running:
                self.db.execute(
                    "UPDATE attempts SET outcome='interrupted', ended=? WHERE id=?",
                    (now, row['id']))
                report['interrupted_attempts'].append(row['id'])
                if self.db.execute("SELECT status FROM tasks WHERE id=? AND status='active'",
                                   (row['task'],)).fetchone():
                    self.db.execute(
                        "UPDATE tasks SET status='interrupted', updated=? WHERE id=?",
                        (now, row['task']))
                    report['interrupted_tasks'].append(row['task'])
            pending = self.db.execute(
                "SELECT id, task FROM effects WHERE state='pending'").fetchall()
            for row in pending:
                self.db.execute(
                    "UPDATE effects SET state='unverified', note='state unknown after "
                    "recovery', updated=? WHERE id=?", (now, row['id']))
                report['unverified_effects'].append(row['id'])
        return report

    def expire(self):
        """Delete expired ephemeral (guest) tasks. Returns the deleted count."""
        now = self.clock()
        rows = self.db.execute(
            'SELECT id FROM tasks WHERE ephemeral=1 AND expires IS NOT NULL AND expires<=?',
            (now,)).fetchall()
        with self.db:
            for row in rows:
                self.db.execute('DELETE FROM tasks WHERE id=?', (row['id'],))
        return len(rows)

    def close(self, destroy=False):
        """Close the store; optionally destroy an ephemeral store's files.

        A failed destroy quarantines the files under a restricted name and
        reports it; cleanup is never falsely claimed.
        """
        self.db.close()
        if not destroy:
            return {'destroyed': True}
        try:
            os.unlink(self.path)
            return {'destroyed': True}
        except OSError:
            pass
        try:
            quarantine = self.path.with_name(
                f'{QUARANTINE_PREFIX}{uuid.uuid4()}.sqlite3')
            os.replace(self.path, quarantine)
            try:
                os.chmod(quarantine, 0o600)
            except OSError:
                pass
            return {'destroyed': False, 'quarantined': quarantine.name}
        except OSError:
            return {'destroyed': False, 'quarantined': None}

    # -------------------------------------------------------------- queries

    def get(self, task_id):
        task_id = identifier(task_id, 'task id')
        row = self.db.execute(
            'SELECT * FROM tasks WHERE id=? AND owner=?', (task_id, self.owner)).fetchone()
        if row is None:
            raise UnknownTask('Task unavailable')
        record = dict(row)
        record['capabilities'] = json.loads(record['capabilities'])
        attempt = self.db.execute(
            "SELECT number FROM attempts WHERE task=? ORDER BY number DESC LIMIT 1",
            (task_id,)).fetchone()
        record['attempt'] = attempt['number'] if attempt else 0
        record['unverified_effects'] = self.db.execute(
            "SELECT COUNT(*) AS n FROM effects WHERE task=? AND state='unverified'",
            (task_id,)).fetchone()['n']
        record['checkpoints'] = self.db.execute(
            'SELECT COUNT(*) AS n FROM checkpoints WHERE task=?', (task_id,)).fetchone()['n']
        return record

    def search(self, query=''):
        if not isinstance(query, str) or len(query) > MAX_TITLE:
            raise RecoveryError('Invalid search')
        rows = self.db.execute(
            '''SELECT id, title, status, updated,
                      substr(prompt, 1, 200) AS preview FROM tasks
               WHERE owner=? AND status!='archived' AND (instr(lower(title), lower(?)) > 0
               OR instr(lower(prompt), lower(?)) > 0)
               ORDER BY updated DESC LIMIT ?''',
            (self.owner, query, query, MAX_SEARCH_RESULTS)).fetchall()
        return [dict(row) for row in rows]

    def list_open(self):
        rows = self.db.execute(
            '''SELECT id, title, status, updated FROM tasks
               WHERE owner=? AND status IN ('active','needs_user_action','interrupted')
               ORDER BY updated DESC LIMIT ?''',
            (self.owner, MAX_SEARCH_RESULTS)).fetchall()
        return [dict(row) for row in rows]

    def attempts(self, task_id):
        self.get(task_id)
        return [dict(row) for row in self.db.execute(
            'SELECT id, number, started, ended, outcome FROM attempts '
            'WHERE task=? ORDER BY number', (task_id,)).fetchall()]

    def checkpoint_history(self, task_id, limit=10):
        self.get(task_id)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RecoveryError('Invalid checkpoint limit')
        rows = self.db.execute(
            '''SELECT seq, step, context, created FROM checkpoints
               WHERE task=? ORDER BY seq DESC LIMIT ?''', (task_id, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def artifacts(self, task_id):
        self.get(task_id)
        return [dict(row) for row in self.db.execute(
            'SELECT kind, ref, sha256 FROM artifacts WHERE task=? '
            'ORDER BY created', (task_id,)).fetchall()]

    def effects(self, task_id, states=EFFECT_STATES):
        self.get(task_id)
        if not isinstance(states, tuple) or not set(states) <= set(EFFECT_STATES):
            raise RecoveryError('Invalid effect state filter')
        marks = ','.join('?' * len(states))
        return [dict(row) for row in self.db.execute(
            f'''SELECT id, operation, target, digest, state, note FROM effects
                WHERE task=? AND state IN ({marks}) ORDER BY created''',
            (task_id, *states)).fetchall()]

    # ------------------------------------------------------------- mutations

    def create(self, title, prompt='', provider='', model='', capabilities_=(),
               ephemeral=False, ttl=None):
        """Create a task record and start its first attempt."""
        title = text(title, 'title', MAX_TITLE)
        prompt = text(prompt, 'prompt', MAX_PROMPT, empty=True)
        provider = text(provider, 'provider', 100, empty=True).strip()
        model = text(model, 'model', 100, empty=True).strip()
        caps = capabilities(capabilities_)
        if not isinstance(ephemeral, bool):
            raise RecoveryError('Invalid ephemeral flag')
        if ephemeral:
            if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) \
                    or not 1 <= ttl <= 30 * 86400:
                raise RecoveryError('Ephemeral tasks require a TTL between 1s and 30 days')
        else:
            ttl = None
        open_tasks = self.db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE owner=?", (self.owner,)).fetchone()['n']
        if open_tasks >= MAX_TASKS_PER_OWNER:
            raise RecoveryError('Task quota reached')
        now = self.clock()
        task_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        with self.db:
            self.db.execute(
                'INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (task_id, self.owner, title.strip(), prompt, provider, model,
                 json.dumps(caps), 'active', 1 if ephemeral else 0, '', 1, now, now,
                 now + ttl if ttl is not None else None))
            self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                            (attempt_id, task_id, 1, now, None, 'running'))
        return self.get(task_id)

    def _live(self, task_id):
        task = self.get(task_id)
        if task['status'] != 'active':
            raise Conflict(f'Task is {task["status"]}; that action needs an active task')
        row = self.db.execute(
            "SELECT id FROM attempts WHERE task=? AND outcome='running'",
            (task_id,)).fetchone()
        if row is None:
            raise Conflict('Task has no running attempt')
        return task, row['id']

    def checkpoint(self, task_id, step, context=''):
        """Record durable progress; the newest checkpoints are the resume view."""
        task, _ = self._live(task_id)
        step = text(step, 'step', MAX_STEP)
        context = text(context, 'checkpoint context', MAX_CHECKPOINT_CONTEXT, empty=True)
        now = self.clock()
        with self.db:
            seq = self.db.execute(
                'SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM checkpoints WHERE task=?',
                (task_id,)).fetchone()['n']
            self.db.execute('INSERT INTO checkpoints VALUES (?,?,?,?,?)',
                            (task_id, seq, step, context, now))
            self.db.execute(
                '''DELETE FROM checkpoints WHERE task=? AND seq <=
                   (SELECT MAX(seq) - ? FROM checkpoints WHERE task=?)''',
                (task_id, MAX_CHECKPOINTS_PER_TASK, task_id))
            self.db.execute('UPDATE tasks SET updated=?, revision=revision+1 WHERE id=?',
                            (now, task_id))
        return {'seq': seq, 'step': step}

    def record_artifact(self, task_id, kind, ref, sha256=None):
        task, _ = self._live(task_id)
        if kind not in ARTIFACT_KINDS:
            raise RecoveryError('Invalid artifact kind')
        ref = reference(ref)
        if sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise RecoveryError('Invalid artifact digest')
        now = self.clock()
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?)',
                (task_id, kind, ref, sha256, now))
            self.db.execute('UPDATE tasks SET updated=?, revision=revision+1 WHERE id=?',
                            (now, task_id))
        return {'kind': kind, 'ref': ref}

    def begin_effect(self, task_id, operation, target, arguments=None):
        """Record the intent of an outside-world mutation *before* performing it.

        The executor must then commit or fail the effect. A journal entry
        left pending through a crash is treated as possibly-executed and
        blocked from automatic replay.
        """
        task, attempt = self._live(task_id)
        operation = text(operation, 'operation', 100).strip()
        target = text(target, 'target', 200).strip()
        digest_value = digest(arguments if arguments is not None else '')
        pending = self.db.execute(
            "SELECT COUNT(*) AS n FROM effects WHERE task=? AND state IN "
            "('pending','unverified')", (task_id,)).fetchone()['n']
        if pending >= MAX_EPOCH_EFFECTS:
            raise RecoveryError('Too many unresolved effects on this task')
        now = self.clock()
        effect_id = str(uuid.uuid4())
        try:
            with self.db:
                self.db.execute(
                    'INSERT INTO effects VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (effect_id, task_id, attempt, operation, target, digest_value,
                     str(uuid.uuid4()), 'pending', '', now, now))
        except sqlite3.IntegrityError:
            prior = self.db.execute(
                'SELECT id, state FROM effects WHERE task=? AND operation=? AND '
                'target=? AND digest=?', (task_id, operation, target, digest_value)).fetchone()
            # Retry is legitimate only after the journal proved the effect did
            # not happen (failed) or a user confirmed it did not (skipped).
            # Anything else (pending/executed/unverified) is a duplicate.
            if prior is None or prior['state'] not in ('failed', 'resolved_skipped'):
                raise Conflict('An identical effect was already journaled for this task')
            with self.db:
                self.db.execute(
                    "UPDATE effects SET state=?, attempt=?, nonce=?, note='', updated=? "
                    'WHERE id=?', ('pending', attempt, str(uuid.uuid4()), now, prior['id']))
            effect_id = prior['id']
        return {'id': effect_id, 'operation': operation, 'target': target,
                'state': 'pending'}

    def _set_effect(self, task_id, effect_id, state, note=''):
        task = self.get(task_id)
        effect_id = identifier(effect_id, 'effect id')
        row = self.db.execute(
            'SELECT * FROM effects WHERE id=? AND task=?', (effect_id, task_id)).fetchone()
        if row is None:
            raise RecoveryError('Unknown effect')
        allowed = {
            'executed': ('pending',),
            'failed': ('pending',),
            'resolved_completed': ('unverified',),
            'resolved_skipped': ('unverified',),
        }[state]
        if row['state'] not in allowed:
            raise Conflict(f'Effect is {row["state"]}; cannot mark it {state}')
        with self.db:
            self.db.execute('UPDATE effects SET state=?, note=?, updated=? WHERE id=?',
                            (state, text(note, 'note', 500, empty=True), self.clock(),
                             effect_id))
            self.db.execute('UPDATE tasks SET updated=?, revision=revision+1 WHERE id=?',
                            (self.clock(), task_id))
        return {'id': effect_id, 'state': state}

    def commit_effect(self, task_id, effect_id):
        return self._set_effect(task_id, effect_id, 'executed')

    def fail_effect(self, task_id, effect_id, note=''):
        return self._set_effect(task_id, effect_id, 'failed', note)

    def resolve_effect(self, task_id, effect_id, outcome, note=''):
        """User decision on a recovery-``unverified`` effect.

        ``completed`` acknowledges the effect did happen (never re-run it);
        ``skipped`` confirms it did not and frees it to be retried.
        """
        if outcome not in RESOLVED_OUTCOMES:
            raise RecoveryError('Effect resolution must be completed or skipped')
        return self._set_effect(task_id, effect_id, f'resolved_{outcome}', note)

    def _transition(self, task_id, status, note='', outcome=None):
        task = self.get(task_id)
        if status not in _TRANSITIONS[task['status']]:
            raise Conflict(f'Task is {task["status"]}; cannot become {status}')
        now = self.clock()
        with self.db:
            self.db.execute(
                'UPDATE tasks SET status=?, note=?, updated=?, revision=revision+1 '
                'WHERE id=?', (status, text(note, 'note', 500, empty=True), now, task_id))
            if outcome is not None:
                self.db.execute(
                    """UPDATE attempts SET outcome=?, ended=? WHERE task=? AND
                       outcome='running'""", (outcome, now, task_id))
        return self.get(task_id)

    def complete(self, task_id, note=''):
        task, _ = self._live(task_id)
        pending = self.db.execute(
            "SELECT COUNT(*) AS n FROM effects WHERE task=? AND state IN "
            "('pending','unverified')", (task_id,)).fetchone()['n']
        if pending:
            raise Conflict('Unresolved effects prevent completing the task')
        return self._transition(task_id, 'completed', note, outcome='completed')

    def cancel(self, task_id, note=''):
        task = self.get(task_id)
        if task['status'] not in ('active', 'needs_user_action', 'interrupted'):
            raise Conflict(f'Task is {task["status"]}; cancellation does not apply')
        now = self.clock()
        with self.db:
            self.db.execute(
                """UPDATE effects SET state='unverified', note='unknown at cancellation',
                   updated=? WHERE task=? AND state='pending'""", (now, task_id))
        return self._transition(task_id, 'cancelled', note, outcome='cancelled')

    def archive(self, task_id):
        return self._transition(task_id, 'archived')

    def needs_user_action(self, task_id, note):
        task = self.get(task_id)
        if task['status'] != 'active':
            raise Conflict(f'Task is {task["status"]}')
        text(note, 'note', 500)
        return self._transition(task_id, 'needs_user_action', note,
                                outcome='needs_user_action')

    def resume(self, task_id, provider=None, model=None, capabilities_=None):
        """Explicitly continue work with a fresh attempt, or say what is needed.

        ``provider``/``model``/``capabilities_`` describe the caller's
        *current* configuration; the saved binding must still hold. Pending
        unverified effects, a changed binding, or missing revalidation each
        return a ``needs_user_action`` view instead of starting an attempt.
        """
        task = self.get(task_id)
        if task['status'] not in ('interrupted', 'needs_user_action', 'archived'):
            raise Conflict(f'Task is {task["status"]}; resume needs unfinished work')
        if provider is None or model is None or capabilities_ is None:
            return {'status': 'needs_user_action', 'task': task,
                    'reason': 'Revalidate the provider, model and capabilities first',
                    'required': [], 'unverified': []}
        caps = capabilities(capabilities_)
        changed = []
        if task['provider'] and provider.strip() != task['provider']:
            changed.append('provider')
        if task['model'] and model.strip() != task['model']:
            changed.append('model')
        missing = [item for item in task['capabilities'] if item not in caps]
        if missing:
            changed.append('capabilities')
        unverified = self.effects(task_id, ('unverified',))
        if unverified or changed:
            if task['status'] == 'interrupted':
                self._transition(task_id, 'needs_user_action',
                                 'resume blocked; user action required',
                                 outcome='needs_user_action')
            return {'status': 'needs_user_action', 'task': self.get(task_id),
                    'reason': ('Effects need user resolution' if unverified
                               else 'Saved configuration no longer matches'),
                    'required': changed, 'unverified': unverified}
        now = self.clock()
        attempt_id = str(uuid.uuid4())
        with self.db:
            number = self.db.execute(
                'SELECT COALESCE(MAX(number), 0) + 1 AS n FROM attempts WHERE task=?',
                (task_id,)).fetchone()['n']
            self.db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?)',
                            (attempt_id, task_id, number, now, None, 'running'))
            if task['status'] != 'active':
                self.db.execute(
                    """UPDATE tasks SET status='active', note='', updated=?,
                       revision=revision+1 WHERE id=?""", (now, task_id))
        # Bounded reconstruction: the newest checkpoints, prior artifacts and
        # the already-executed effects, so the executor reuses instead of
        # regenerating and does not repeat committed world changes.
        history = self.checkpoint_history(task_id, limit=20)
        budget, kept = MAX_RECONSTRUCT_BYTES, []
        for item in reversed(history):
            cost = len(item['context'].encode('utf-8'))
            if kept and cost > budget:
                break
            kept.append(item)
            budget -= cost
        return {'status': 'resumed', 'task': self.get(task_id),
                'attempt': number, 'checkpoints': list(reversed(kept)),
                'artifacts': self.artifacts(task_id),
                'executed_effects': self.effects(task_id, ('executed',
                                                           'resolved_completed'))}
