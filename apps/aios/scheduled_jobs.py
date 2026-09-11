"""Bounded, same-user Unix client shared by native UI and scheduling tools."""
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time

from .scheduled_execution import desktop_only
from .scheduling import SchedulingError, UnavailableError, fields, integer, text
from .scheduled_store import identifier

REQUEST_LIMIT = 128 * 1024
RUN_REQUEST_LIMIT = 256 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
STATUSES = {'ok', 'invalid', 'unavailable', 'conflict', 'quota_exceeded', 'needs_user_action'}
ACTIONS = {
    'health': ((), ()),
    'binding': (('prompt',), ()),
    'preview': (('schedule',), ()),
    'create': (('config',), ()),
    'get': (('job_id',), ()),
    'list': ((), ('limit', 'after')),
    'update': (('job_id', 'expected_revision', 'config'), ()),
    'pause': (('job_id', 'expected_revision'), ()),
    'resume': (('job_id', 'expected_revision'), ()),
    'delete': (('job_id', 'expected_revision'), ()),
    'run_now': (('job_id', 'expected_revision', 'request_id'), ()),
    'cancel_run': (('run_id',), ()),
    'list_runs': (('job_id',), ('limit', 'before')),
    'read_result': (('run_id',), ()),
    'acknowledge_result': (('run_id',), ()),
    'unread': ((), ('limit', 'after')),
}
NATIVE_ACTIONS = {
    'mark_notified': (('run_id',), ()),
}


def runtime_dir():
    desktop_only()
    base = os.environ.get('XDG_RUNTIME_DIR')
    if not base:
        raise UnavailableError('The private desktop runtime directory is unavailable')
    parent = Path(base)
    info = parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise UnavailableError('Scheduler requires a private user-owned runtime directory')
    path = parent / 'aios-scheduler'
    if len(os.fsencode(path / ('run-' + '0' * 36) / 'tools.sock')) >= 108:
        raise UnavailableError('Scheduler runtime path exceeds the Unix socket path limit')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise UnavailableError('Scheduler runtime directory is not private')
    return path


def socket_path():
    return runtime_dir() / 'service.sock'


def same_user(connection):
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, _ = struct.unpack('3i', credentials)
    if uid != os.getuid():
        raise UnavailableError('Scheduler peer identity does not match the OS user')
    return pid


def validate_request(value):
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise SchedulingError('Invalid scheduling request')
    action = value['action']
    actions = {**ACTIONS, **NATIVE_ACTIONS}
    if action not in actions:
        raise SchedulingError('Unknown scheduling action')
    required, optional = actions[action]
    fields(value, ('action', *required), optional)
    for name in ('job_id', 'run_id', 'request_id'):
        if name in value:
            identifier(value[name])
    if 'expected_revision' in value:
        integer(value['expected_revision'], 'expected revision', 1, 2 ** 63 - 1)
    if 'limit' in value:
        integer(value['limit'], 'page limit', 1, 100)
    if 'before' in value:
        integer(value['before'], 'run cursor', 1, 2 ** 63 - 1)
    if 'after' in value:
        if action == 'list':
            identifier(value['after'])
        else:
            integer(value['after'], 'outbox cursor', 0, 2 ** 63 - 1)
    if 'prompt' in value:
        text(value['prompt'], 'prompt', 32768)
    return value


def encode(value, limit):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
    except (ValueError, TypeError, RecursionError):
        raise SchedulingError('Invalid scheduling JSON') from None
    if len(raw) > limit:
        raise SchedulingError('Scheduling message is too large; request a smaller page')
    return raw


def request(value, timeout=10):
    """Return an explicit status envelope. No credentials or ownership arguments."""
    try:
        from .toolhost import _read_socket_line

        relay = os.environ.get('AIOS_PROTECTED_CHAT_SOCKET')
        if relay:
            validate_request(value)
            raw = encode({'action': 'scheduled_jobs', 'request': value}, REQUEST_LIMIT)
            with socket.socket(socket.AF_UNIX) as client:
                deadline = time.monotonic() + timeout
                client.settimeout(timeout)
                client.connect(relay)
                same_user(client)
                client.sendall(raw)
                reply = json.loads(_read_socket_line(
                    client, RESPONSE_LIMIT, deadline,
                    incomplete_error='Invalid protected scheduling response'))
            if not isinstance(reply, dict) or reply.get('status') not in STATUSES:
                raise UnavailableError('Invalid protected scheduling response')
            expected = {'status', 'result'} if reply['status'] == 'ok' else {'status', 'error'}
            if set(reply) != expected:
                raise UnavailableError('Invalid protected scheduling response')
            return reply
        desktop_only()
        if os.environ.get('AIOS_SESSION_SOCKET') or os.environ.get('AIOS_SESSION_ID'):
            raise UnavailableError('Protected scheduling requires a broker-authorized per-chat route')
        validate_request(value)
        raw = encode(value, REQUEST_LIMIT)
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise SchedulingError('Invalid scheduling timeout')
        with socket.socket(socket.AF_UNIX) as client:
            deadline = time.monotonic() + timeout
            client.settimeout(timeout)
            client.connect(str(socket_path()))
            same_user(client)
            client.sendall(raw)
            reply = json.loads(_read_socket_line(
                client, RESPONSE_LIMIT, deadline, incomplete_error='Invalid scheduler response'))
        if not isinstance(reply, dict) or reply.get('status') not in STATUSES:
            raise UnavailableError('Invalid scheduler response')
        expected = {'status', 'result'} if reply['status'] == 'ok' else {'status', 'error'}
        if set(reply) != expected or ('error' in reply and not isinstance(reply['error'], str)):
            raise UnavailableError('Invalid scheduler response')
        return reply
    except SchedulingError as error:
        return {'status': error.code, 'error': str(error)}
    except (OSError, RuntimeError, ValueError):
        return {'status': 'unavailable', 'error': 'Scheduled jobs service is unavailable'}


def act(value):
    """Bound model calls without weakening the shared service's validation."""
    from .scheduled_tool import validate

    try:
        validate(value)
        # Keep headroom for the tool-call envelope in both provider transports.
        encode(value, 23 * 1024)
        value = dict(value)
        if value['action'] in ('list', 'list_runs', 'unread'):
            value.setdefault('limit', 10)
        result = request(value)
        if len(encode(result, RESPONSE_LIMIT)) > 60 * 1024:
            return {'status': 'unavailable',
                    'error': 'Readback exceeds the model tool limit; request a smaller page '
                             'or open the native Scheduled jobs view'}
        return result
    except SchedulingError as error:
        return {'status': error.code, 'error': str(error)}
    except (ValueError, TypeError, RecursionError):
        return {'status': 'invalid', 'error': 'Invalid scheduling JSON'}
