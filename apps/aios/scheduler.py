"""Per-user scheduler supervisor: bounded dispatch, recovery, and delivery policy.

This is the singleton service described in docs/plans/scheduled-agent-jobs.md.
It owns the transactional lifecycle around ScheduledStore: occurrence claims,
binding validation, cooperative worker supervision with monotonic deadlines,
cancellation, restart recovery, and the actionable/quiet-hours delivery
decision. Actual model execution arrives through an injected executor
callable, so this module stays independent of providers, shells, and
hardware; the real worker/tool-host adapter is wired by the service host.
"""
import fcntl
import os
from pathlib import Path
import threading
import time

from .scheduled_store import ScheduledStore
from .scheduling import (
    SchedulingError, UnavailableError, parse_timestamp, utc, zone,
)

DEFAULT_GRACE_SECONDS = 5.0
POLL_SECONDS = 0.02
SETTLE_TIMEOUT_SECONDS = 960.0


class UserActionNeeded(Exception):
    """The worker cannot proceed without explicit user action."""


class WorkerCancelled(Exception):
    """The worker observed a cancellation request and stopped."""


class WorkerControl:
    """Cooperative stop signal and execution deadline handed to each worker."""

    def __init__(self, timeout_seconds, deadline):
        self.timeout_seconds = timeout_seconds
        self.deadline = deadline
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    @property
    def cancel_requested(self):
        return self._stop.is_set()

    def wait(self, seconds):
        """Interruptible wait for well-behaved workers; True if cancelling."""
        return self._stop.wait(max(0.0, seconds))


class _Pending:
    def __init__(self, run, control):
        self.run = run
        self.thread: threading.Thread | None = None
        self.control = control
        self.result = None
        self.error = None
        self.needs_action = False
        self.cancelled = False
        self.completed = threading.Event()
        self.abandoned = False


def safe_error(message):
    text = str(message).replace('\0', '')
    text = ''.join(ch if ch >= ' ' or ch == '\n' else ' ' for ch in text)
    text = text.strip()
    if not text:
        return 'Scheduler worker failed'
    return ('Scheduler worker failed: ' + text)[:4000]


class SchedulerService:
    """One supervisor per OS user store; never two dispatchers on one store."""

    def __init__(self, store, executor=None, validate_binding=None,
                 grace_seconds=DEFAULT_GRACE_SECONDS):
        if not isinstance(store, ScheduledStore):
            raise SchedulingError('SchedulerService requires a ScheduledStore')
        if executor is not None and not callable(executor):
            raise SchedulingError('executor must be callable')
        if validate_binding is not None and not callable(validate_binding):
            raise SchedulingError('validate_binding must be callable')
        self.store = store
        self.executor = executor
        self.validate_binding = validate_binding
        self.grace_seconds = max(0.05, float(grace_seconds))
        self.stopping = False
        self._pending = {}
        self._lock = threading.Lock()
        # Singleton ownership: the flock is held for the service lifetime.
        self._lock_path = Path(store.root) / 'service.lock'
        if self._lock_path.is_symlink():
            raise UnavailableError('Scheduler lock must be a regular private file')
        fd = os.open(self._lock_path,
                     os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise UnavailableError('Another scheduler service already owns this store')
        os.chmod(self._lock_path, 0o600)
        self._lock_fd = fd

    def close(self):
        self.shutdown()
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
        except OSError:
            pass
        self._lock_fd = -1

    # ---------------------------------------------------------------- startup

    def startup(self):
        """Recover abandoned work before dispatch; never blindly retry it."""
        recovered = self.store.recover()
        return {'recovered': recovered}

    # -------------------------------------------------------------- dispatch

    def run_once(self):
        """One non-blocking cycle: claim due work, supervise, settle settled."""
        dispatched = 0
        runs = []
        if not self.stopping and self.executor is not None:
            for run in self.store.claim_due():
                self._dispatch(run)
                runs.append(run['id'])
            dispatched = len(runs)
        self._drain()
        return {'dispatched': dispatched, 'runs': runs}

    def run_now(self, job_id, expected_revision, request_id):
        """Transactional manual run; idempotent on the caller's request ID."""
        run = self.store.run_now(job_id, expected_revision, request_id)
        if run['state'] == 'queued' and not self.stopping and self.executor is not None:
            self._dispatch(run)
        self._drain()
        return self.store.get_run(run['id'])

    def cancel_run(self, run_id):
        """Request cooperative stop; the drain records the terminal state."""
        with self._lock:
            pending = self._pending.get(run_id)
        if pending is not None:
            pending.control.cancel()
            self._drain(wait=True, timeout=self.grace_seconds + 5.0)
            return self.store.get_run(run_id)
        run = self.store.get_run(run_id)
        if run['state'] in ('queued', 'running'):
            with self._lock:
                tracking = bool(self._pending)
            if tracking:
                return run  # Another active run owns the dispatcher; wait for it.
            # A run left active by a dead supervisor is stopped without a
            # worker to signal; recovery records it honestly as interrupted.
            self.store.recover()
            return self.store.get_run(run_id)
        return run

    def settle(self, timeout=SETTLE_TIMEOUT_SECONDS):
        """Block until active workers reach a terminal state or time out."""
        self._drain(wait=True, timeout=timeout)

    def shutdown(self):
        """Stop dispatch, signal active workers, then recover the stragglers."""
        self.stopping = True
        with self._lock:
            controls = [pending.control for pending in self._pending.values()]
        for control in controls:
            control.cancel()
        self._drain(wait=True, timeout=self.grace_seconds + 5.0)
        return {'recovered': self.store.recover()}

    # ------------------------------------------------------------- internals

    def _dispatch(self, run):
        if run['state'] != 'queued':
            return
        snapshot = run['snapshot']
        if self.validate_binding is not None and not self.validate_binding(snapshot):
            self.store.start(run['id'])
            self.store.finish(
                run['id'], 'needs_user_action',
                error='Saved provider binding no longer matches the live configuration')
            try:
                job = self.store.get(run['job_id'])
                self.store.pause(run['job_id'], job['revision'])
            except UnavailableError:
                pass  # The job was deleted while its occurrence was claimed.
            return
        self.store.start(run['id'])
        timeout = snapshot['execution']['timeout_seconds']
        control = WorkerControl(timeout, time.monotonic() + timeout)
        container = _Pending(run, control)
        executor = self.executor
        thread = threading.Thread(target=self._supervise_worker,
                                  args=(executor, container), daemon=True)
        container.thread = thread
        with self._lock:
            self._pending[run['id']] = container
        thread.start()

    def _supervise_worker(self, executor, container):
        try:
            outcome = executor(container.run, container.control)
            if not isinstance(outcome, dict) or outcome.keys() - {'result', 'outcome', 'usage'}:
                raise SchedulingError('Worker returned an invalid result record')
            container.result = outcome
        except UserActionNeeded as exc:
            container.needs_action = True
            container.error = str(exc)
        except WorkerCancelled:
            container.cancelled = True
        except Exception as exc:
            container.error = exc
        finally:
            container.completed.set()

    def _drain(self, wait=False, timeout=None):
        started = time.monotonic()
        while True:
            with self._lock:
                pending = list(self._pending.values())
            if not pending:
                return
            now = time.monotonic()
            progressed = False
            for container in pending:
                if container.completed.is_set():
                    self._finalize(container)
                    progressed = True
                elif container.thread is not None and not container.thread.is_alive() \
                        and not container.completed.is_set():
                    container.error = RuntimeError('worker exited without a result')
                    self._finalize(container)
                    progressed = True
                elif now > container.control.deadline:
                    container.control.cancel()
                    if now > container.control.deadline + self.grace_seconds:
                        container.abandoned = True
                        container.error = RuntimeError(
                            'run exceeded its timeout and the worker did not stop')
                        self._finalize(container)
                        progressed = True
            if not wait:
                return
            if not progressed and time.monotonic() - started > (timeout or 0.0):
                return
            time.sleep(POLL_SECONDS)

    def _finalize(self, container):
        run_id = container.run['id']
        with self._lock:
            current = self._pending.get(run_id)
            if current is None:
                return
            del self._pending[run_id]
        control = container.control
        try:
            if container.abandoned:
                self.store.finish(run_id, 'failed',
                                  error=safe_error(container.error or 'timeout'))
            elif container.cancelled:
                self.store.cancel(run_id)
            elif control.cancel_requested:
                if container.needs_action:
                    self.store.finish(run_id, 'needs_user_action',
                                      error=str(container.error or 'User action required')[:4000])
                elif isinstance(container.error, Exception):
                    self.store.finish(run_id, 'failed', error=safe_error(container.error))
                else:
                    self.store.cancel(run_id)
            elif container.needs_action:
                self.store.finish(run_id, 'needs_user_action',
                                  error=str(container.error or 'User action required')[:4000])
            elif isinstance(container.error, Exception):
                self.store.finish(run_id, 'failed', error=safe_error(container.error))
            else:
                result = container.result or {}
                self.store.finish(run_id, 'succeeded',
                                  result=result.get('result', ''),
                                  outcome=result.get('outcome'),
                                  usage=result.get('usage'))
        except (SchedulingError, UnavailableError):
            # A concurrent terminal record wins; late results are dropped.
            pass

    # ------------------------------------------------------------- attention

    def attention(self, now=None):
        """Durable inbox view with delivery decisions; never mutates state."""
        now = utc(now if now is not None else self.store.clock.now())
        entries = []
        for row in self.store.unread(limit=100):
            run = self.store.get_run(row['run_id'])
            entries.append(self._delivery(run, row, now))
        visible_entries = [entry for entry in entries if entry['visible']]
        visible_any = bool(visible_entries)
        actionable = any(entry['actionable'] for entry in visible_entries)
        with self._lock:
            active = len(self._pending)
        if actionable:
            state = 'action_needed'
        elif visible_any:
            state = 'unread'
        elif active:
            state = 'running'
        else:
            state = 'idle'
        return {'state': state, 'results': entries, 'active_runs': active}

    def _delivery(self, run, outbox_row, now):
        snapshot = run['snapshot']
        notification = snapshot.get('notification') or {'mode': 'all'}
        outcome = outbox_row['outcome']
        if outcome not in ('changed', 'unchanged', 'succeeded', 'failed',
                           'needs_user_action', 'cancelled', 'interrupted'):
            outcome = 'succeeded'
        actionable = outcome in ('failed', 'needs_user_action', 'interrupted')
        visible, reason = True, 'deliver'
        if notification.get('mode') == 'actionable' and outcome == 'unchanged':
            visible, reason = False, 'unchanged'
        if visible and notification.get('snooze_until') is not None:
            if parse_timestamp(notification['snooze_until']) > now:
                visible, reason = False, 'snoozed'
        quiet = notification.get('quiet_hours')
        if visible and isinstance(quiet, dict):
            local = now.astimezone(zone(quiet['zone'])).strftime('%H:%M')
            start, end = quiet['start'], quiet['end']
            inside = start <= local < end if start < end else local >= start or local < end
            if inside:
                visible, reason = False, 'quiet_hours'
        return {
            'run_id': run['id'],
            'job_id': run['job_id'],
            'job_title': snapshot['title'],
            'state': run['state'],
            'outcome': outcome,
            'actionable': actionable,
            'visible': visible,
            'reason': reason,
            'sequence': outbox_row['sequence'],
            'acknowledged': outbox_row['acknowledged'] is not None,
            'created': outbox_row['created'],
        }
