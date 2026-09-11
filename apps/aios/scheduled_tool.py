"""Fixed model-facing scheduling schemas; the scheduler remains authoritative."""
import re

from .scheduling import SchedulingError


def _object(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': False}


def _string(maximum, *, minimum=1, pattern=None):
    value = {'type': 'string', 'minLength': minimum, 'maxLength': maximum}
    if pattern is not None:
        value['pattern'] = pattern
    return value


def _integer(minimum, maximum=2 ** 63 - 1):
    return {'type': 'integer', 'minimum': minimum, 'maximum': maximum}


ID = _string(36, pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
ZONE = _string(100, pattern=r'^[A-Za-z0-9_+./-]+$')
STAMP = _string(40, pattern=r'^[0-9T:Z+.-]+$')
SCHEDULE = {
    'oneOf': [
        _object({'kind': {'enum': ['once']}, 'value': STAMP, 'zone': ZONE}),
        _object({'kind': {'enum': ['cron']},
                 'value': _string(200, pattern=r'^[0-9*,/ \t-]+$'), 'zone': ZONE}),
    ],
}
EXECUTION = _object({
    'provider': {'enum': ['local', 'remote', 'subscription']},
    'profile': _string(72, pattern=r'^(current|agent)@[0-9a-f]{64}$'),
    'model': _string(50),
    'capabilities': {'type': 'array', 'maxItems': 32, 'uniqueItems': True,
                     'items': _string(64, pattern=r'^[a-z][a-z0-9_.-]{0,63}$')},
    'timeout_seconds': _integer(1, 900),
    'token_budget': _integer(1, 32768),
    'tool_budget': _integer(0, 32),
    'missed_run': {'enum': ['coalesce', 'skip']},
}, ['provider', 'profile', 'model', 'capabilities'])
NOTIFICATION = _object({
    'mode': {'enum': ['all', 'actionable']},
    'quiet_hours': {'oneOf': [
        {'type': 'null'},
        _object({'start': _string(5, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'),
                 'end': _string(5, pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$'),
                 'zone': ZONE}),
    ]},
    'snooze_until': {'oneOf': [{'type': 'null'}, STAMP]},
}, ['mode'])
# These character bounds also fit 24 KiB agent calls when non-BMP characters
# are JSON-escaped as surrogate pairs (12 bytes each), including policy fields.
CONFIG = _object({
    'title': _string(50),
    'prompt': _string(1024),
    'context': _string(384, minimum=0),
    'conversation': _string(50, minimum=0),
    'schedule': SCHEDULE,
    'execution': EXECUTION,
    'notification': NOTIFICATION,
}, ['title', 'prompt', 'schedule', 'execution'])

_FIELDS = {
    'config': CONFIG, 'schedule': SCHEDULE, 'prompt': _string(1024),
    'job_id': ID, 'run_id': ID, 'request_id': ID,
    'expected_revision': _integer(1), 'limit': _integer(1, 10),
    'before': _integer(1), 'after': _integer(0),
}


def parameters():
    from .scheduled_jobs import ACTIONS
    branches = []
    for action, (required, optional) in ACTIONS.items():
        properties = {'action': {'enum': [action]}}
        for name in (*required, *optional):
            properties[name] = ID if action == 'list' and name == 'after' else _FIELDS[name]
        branches.append(_object(properties, ['action', *required]))
    return {'type': 'object', 'oneOf': branches}


def definition():
    return {
        'type': 'function',
        'function': {
            'name': 'scheduled_jobs',
            'description': (
                'Save and manage durable scheduled agent tasks using fixed service actions. '
                'Activate scheduled-jobs for scheduling requests. Clear user timing/task '
                'authorizes saving; ask only if timing or scope is ambiguous. Call binding '
                'with the task prompt, copy its provider/profile/model and select only needed '
                'returned capabilities. Never invent credentials, owners or capabilities. '
                'If zone is null ask for an IANA zone; never default to UTC. Preview before '
                'create/update and report normalized saved readback. Update replaces the full '
                'configuration; mutations require the latest expected_revision. For run_now '
                'generate one UUID request_id per user request and reuse it on transport '
                'retries. Unread is durable; read_result does not acknowledge. Background '
                'jobs cannot schedule. Use the native view for larger prompts/results.'
            ),
            'parameters': parameters(),
        },
    }


def _validate(value, schema):
    if 'oneOf' in schema:
        matches = 0
        for branch in schema['oneOf']:
            try:
                _validate(value, branch)
                matches += 1
            except SchedulingError:
                pass
        if matches != 1:
            raise SchedulingError('Arguments do not match the scheduling action schema')
        return
    if 'enum' in schema and value not in schema['enum']:
        raise SchedulingError('Invalid scheduling option')
    kind = schema.get('type')
    if kind == 'object':
        if (not isinstance(value, dict) or not set(schema['required']) <= value.keys()
                or value.keys() - schema['properties'].keys()):
            raise SchedulingError('Missing or unknown scheduling fields')
        for key, item in value.items():
            _validate(item, schema['properties'][key])
    elif kind == 'string':
        if (not isinstance(value, str) or '\0' in value
                or not schema['minLength'] <= len(value) <= schema['maxLength']
                or ('pattern' in schema and re.fullmatch(schema['pattern'], value) is None)):
            raise SchedulingError('Scheduling text is invalid or exceeds the tool limit')
    elif kind == 'integer':
        if type(value) is not int or not schema['minimum'] <= value <= schema['maximum']:
            raise SchedulingError('Invalid scheduling integer')
    elif kind == 'array':
        if not isinstance(value, list) or len(value) > schema['maxItems']:
            raise SchedulingError('Invalid scheduling capabilities')
        for item in value:
            _validate(item, schema['items'])
        if len(set(value)) != len(value):
            raise SchedulingError('Duplicate scheduling capabilities')
    elif kind == 'null' and value is not None:
        raise SchedulingError('Invalid scheduling null value')


def validate(value):
    _validate(value, parameters())
    return value
