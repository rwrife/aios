"""Validated structured scheduling tool: fixed actions over the scheduler core.

This is the ``scheduled_jobs`` service client and model-facing tool contract
from docs/plans/scheduled-agent-jobs.md. It adapts a SchedulerService (and its
ScheduledStore) into the twelve fixed actions with action-specific validation,
bounded strings and pagination, opaque identifiers, expected revisions for
mutations, and explicit invalid/unavailable/conflict/quota results. Ownership
always comes from the trusted store context, never from a request field. The
tool is not advertised to models until the per-user scheduler service host is
wired into the image; this module keeps the contract display-independent and
unit-testable.
"""
from dataclasses import dataclass
import sqlite3

from .scheduler import SchedulerService
from .scheduled_store import identifier
from .scheduling import (
    ConflictError, QuotaError, Schedule, SchedulingError, UnavailableError,
    integer, parse_timestamp,
)

ACTIONS = (
    'create', 'get', 'list', 'update', 'pause', 'resume', 'delete',
    'run_now', 'cancel_run', 'list_runs', 'read_result', 'acknowledge_result',
)
MAX_MESSAGE_CHARS = 500

TOOL = {
    'type': 'function',
    'function': {
        'name': 'scheduled_jobs',
        'description': (
            'Create and manage this user\u2019s scheduled agent jobs with fixed '
            'structured actions: create, get, list, update, pause, resume, '
            'delete, run_now, cancel_run, list_runs, read_result, '
            'acknowledge_result. Mutations require the expected_revision from '
            'the latest readback, and run_now requires a fresh UUID '
            'request_id. Jobs carry title, prompt, schedule (once timestamp or '
            'five-field cron with an explicit IANA zone), and execution '
            'binding (provider, profile, model, capabilities). Never invent '
            'identifiers or revisions; read them first. Ownership is implicit '
            'and cannot be supplied. Results are explicit: ok plus the '
            'readback, or ok false with code invalid, unavailable, conflict, '
            'or quota_exceeded.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string', 'enum': list(ACTIONS)},
                'job': {
                    'type': 'object',
                    'description': (
                        'Job configuration for create and update: title, '
                        'prompt, optional context and conversation, schedule '
                        '{kind, value, zone}, execution {provider, profile, '
                        'model, capabilities, optional budgets}, optional '
                        'notification policy.'
                    ),
                },
                'job_id': {'type': 'string', 'description': 'Opaque job UUID.'},
                'run_id': {'type': 'string', 'description': 'Opaque run UUID.'},
                'request_id': {
                    'type': 'string',
                    'description': 'Fresh UUID deduplicating a run_now request.',
                },
                'expected_revision': {
                    'type': 'integer', 'minimum': 1,
                    'description': 'Revision from the latest readback.',
                },
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
                'after': {
                    'type': 'string',
                    'description': 'list cursor: last seen job_id.',
                },
                'before': {
                    'type': 'integer', 'minimum': 1,
                    'description': 'list_runs cursor: last seen run sequence.',
                },
            },
            'required': ['action'],
            'additionalProperties': False,
        },
    },
}

# Per action: (required fields, optional fields). The action field itself is
# always required and is never listed here.
ACTION_FIELDS = {
    'create': (('job',), ()),
    'get': (('job_id',), ()),
    'list': ((), ('limit', 'after')),
    'update': (('job_id', 'expected_revision', 'job'), ()),
    'pause': (('job_id', 'expected_revision'), ()),
    'resume': (('job_id', 'expected_revision'), ()),
    'delete': (('job_id', 'expected_revision'), ()),
    'run_now': (('job_id', 'expected_revision', 'request_id'), ()),
    'cancel_run': (('run_id',), ()),
    'list_runs': (('job_id',), ('limit', 'before')),
    'read_result': (('run_id',), ()),
    'acknowledge_result': (('run_id',), ()),
}


@dataclass(frozen=True)
class ToolError(Exception):
    code: str
    message: str


def _uuid(value, name):
    if not isinstance(value, str):
        raise ToolError('invalid', f'Invalid {name}')
    try:
        identifier(value)
    except SchedulingError as exc:
        raise ToolError('invalid', str(exc)) from exc
    return value


def _message(exc):
    return str(exc).strip()[:MAX_MESSAGE_CHARS] or 'Scheduling request failed'


def _validate(arguments):
    """Action-specific validation; returns the request as a fresh dict."""
    if not isinstance(arguments, dict) or not arguments:
        raise ToolError('invalid', 'Expected a structured request object')
    if any(not isinstance(key, str) for key in arguments):
        raise ToolError('invalid', 'Unknown request fields')
    action = arguments.get('action')
    if action not in ACTIONS:
        raise ToolError('invalid', 'Unknown scheduling action')
    required, optional = ACTION_FIELDS[action]
    provided = set(arguments) - {'action'}
    if provided - set(required) - set(optional):
        raise ToolError('invalid', 'Unknown request fields for this action')
    missing = set(required) - provided
    if missing:
        raise ToolError('invalid', 'Missing required fields: ' + ', '.join(sorted(missing)))
    request: dict = {'action': action}
    for field in provided:
        value = arguments[field]
        if field == 'job_id':
            request[field] = _uuid(value, 'job id')
        elif field == 'run_id':
            request[field] = _uuid(value, 'run id')
        elif field == 'request_id':
            request[field] = _uuid(value, 'request id')
        elif field == 'expected_revision':
            try:
                request[field] = integer(value, 'expected revision', 1, 2 ** 63 - 1)
            except SchedulingError as exc:
                raise ToolError('invalid', str(exc)) from exc
        elif field == 'limit':
            try:
                request[field] = integer(value, 'page limit', 1, 100)
            except SchedulingError as exc:
                raise ToolError('invalid', str(exc)) from exc
        elif field == 'after':
            request[field] = _uuid(value, 'page cursor')
        elif field == 'before':
            try:
                request[field] = integer(value, 'run cursor', 1, 2 ** 63 - 1)
            except SchedulingError as exc:
                raise ToolError('invalid', str(exc)) from exc
        elif field == 'job':
            if not isinstance(value, dict):
                raise ToolError('invalid', 'Job configuration must be an object')
            request[field] = value
        else:  # pragma: no cover - ACTION_FIELDS is the closed set
            raise ToolError('invalid', 'Unknown request fields for this action')
    return request


class ScheduledJobsTool:
    """Fixed-action validated facade; every reply is ok plus readback or an explicit code."""

    def __init__(self, scheduler):
        if not isinstance(scheduler, SchedulerService):
            raise SchedulingError('ScheduledJobsTool requires a SchedulerService')
        self.scheduler = scheduler
        self.store = scheduler.store

    def act(self, arguments):
        try:
            request = _validate(arguments)
            return {'ok': True, 'action': request['action'],
                    **self._dispatch(request)}
        except ToolError as exc:
            return {'ok': False, 'code': exc.code, 'message': exc.message}
        except (UnavailableError, QuotaError, ConflictError, SchedulingError) as exc:
            return {'ok': False, 'code': exc.code, 'message': _message(exc)}
        except sqlite3.Error:
            return {'ok': False, 'code': 'unavailable',
                    'message': 'Scheduler storage is unavailable'}

    # ---------------------------------------------------------------- core

    def _job_reply(self, job):
        """Add the bounded three-occurrence preview the plan requires in readback."""
        now = self.store.clock.now()
        floor = now
        if job['enabled'] and job['last_scheduled']:
            floor = max(now, parse_timestamp(job['last_scheduled']))
        reply = dict(job)
        reply['preview'] = Schedule.parse(job['schedule']).preview(floor)
        return reply

    def _run_reply(self, run, body=False):
        reply = {key: value for key, value in run.items() if key != 'snapshot'}
        if body:
            return reply
        for key in ('result', 'error', 'usage'):
            reply.pop(key, None)
        return reply

    def _dispatch(self, request):
        action = request['action']
        if action == 'create':
            return {'job': self._job_reply(self.store.create(request['job']))}
        if action == 'get':
            return {'job': self._job_reply(self.store.get(request['job_id']))}
        if action == 'list':
            jobs = self.store.list_jobs(
                limit=request.get('limit', 50), after=request.get('after'))
            return {'jobs': [self._job_reply(job) for job in jobs]}
        if action == 'update':
            job = self.store.update(request['job_id'], request['expected_revision'],
                                    request['job'])
            return {'job': self._job_reply(job)}
        if action == 'pause':
            return {'job': self._job_reply(
                self.store.pause(request['job_id'], request['expected_revision']))}
        if action == 'resume':
            return {'job': self._job_reply(
                self.store.resume(request['job_id'], request['expected_revision']))}
        if action == 'delete':
            self.store.delete(request['job_id'], request['expected_revision'])
            return {'job_id': request['job_id'], 'deleted': True}
        if action == 'run_now':
            run = self.scheduler.run_now(request['job_id'],
                                         request['expected_revision'],
                                         request['request_id'])
            return {'run': self._run_reply(run)}
        if action == 'cancel_run':
            return {'run': self._run_reply(
                self.scheduler.cancel_run(request['run_id']))}
        if action == 'list_runs':
            runs = self.store.list_runs(
                request['job_id'], limit=request.get('limit', 20),
                before=request.get('before'))
            return {'runs': [self._run_reply(run) for run in runs]}
        if action == 'read_result':
            run = self.store.get_run(request['run_id'])
            if run['state'] in ('queued', 'running'):
                return {'run': {'id': run['id'], 'job_id': run['job_id'],
                                'state': run['state'], 'result': '',
                                'error': '', 'usage': run['usage']}}
            return {'run': self._run_reply(run, body=True)}
        # acknowledge_result
        self.store.acknowledge(request['run_id'])
        run = self.store.get_run(request['run_id'])
        return {'run_id': run['id'], 'acknowledged': True}


def main(argv=None):  # pragma: no cover - documented CLI entry point
    """Human debugging entry point; the image wires no public socket yet."""
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Inspect the scheduling tool contract.')
    parser.add_argument('request', nargs='?', help='JSON action object, or - for stdin')
    args = parser.parse_args(argv)
    print(json.dumps(TOOL, indent=2))
    if args.request:
        import sys
        raw = sys.stdin.read() if args.request == '-' else args.request
        try:
            action = json.loads(raw).get('action')
        except (ValueError, AttributeError):
            action = None
        print(json.dumps({
            'ok': False, 'code': 'unavailable',
            'message': 'No scheduler service host is wired in this build',
            'request': action,
        }, indent=2))
    return 0


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
