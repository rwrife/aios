"""Broker-controlled scheduling in an unlocked, encrypted owner workspace.

The broker is the only request ingress. The sandbox exposes no scheduling
socket: an inherited pipe carries fixed requests and short authorization
heartbeats. Neither the process descriptor nor a sign-in status grants access.
"""
import json
import os
import selectors
import signal
import sqlite3
import sys
import time
import uuid

from .scheduling import SchedulingError, UnavailableError


AUTHORIZATION_SECONDS = 2


class Starting(UnavailableError):
    pass


class ProtectedScheduler:
    """Owned by Sessions; all methods run on the serialized broker thread."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.owner, self.lease, self.work = sessions.owner, sessions.lease, sessions.work
        self.scope = str(uuid.uuid4())
        self.process = None
        self.started = time.monotonic()
        self.ready = False
        sessions._present()
        if not self.work or not hasattr(sessions.isolation, 'scheduled'):
            raise UnavailableError('Protected scheduling requires an isolated owner workspace')
        self.process = sessions.isolation.scheduled(self.scope, sessions.root, sessions.uid)
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        except (OSError, ValueError):
            self.close()
            raise UnavailableError('Protected scheduling control descriptors are unavailable') from None

    def is_ready(self):
        if self.ready:
            return True
        if self.process is None or self.process.poll() is not None:
            raise UnavailableError('Protected scheduling service is unavailable')
        with selectors.DefaultSelector() as poll:
            poll.register(self.process.stdout, selectors.EVENT_READ)
            if poll.select(0):
                if os.read(self.process.stdout.fileno(), 1024) != b'{"ready":true}\n':
                    raise UnavailableError('Protected scheduling service could not initialize')
                self.ready = True
                return True
        if time.monotonic() - self.started > 10:
            raise UnavailableError('Protected scheduling initialization timed out')
        return False

    def authorize(self):
        self.sessions._present()
        if (self.owner, self.lease, self.work) != (
                self.sessions.owner, self.sessions.lease, self.sessions.work):
            raise PermissionError('Protected scheduling scope expired')

    def exchange(self, value):
        from .scheduled_jobs import encode, REQUEST_LIMIT, RESPONSE_LIMIT, STATUSES
        self.authorize()
        if not self.is_ready():
            raise Starting('Protected scheduling is starting; retry shortly')
        process = self.process
        if process is None or process.poll() is not None:
            raise UnavailableError('Protected scheduling service is unavailable')
        raw = encode(value, REQUEST_LIMIT + 1024)
        frame = bytearray()
        deadline = time.monotonic() + AUTHORIZATION_SECONDS
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdin, selectors.EVENT_WRITE)
            poll.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                for key, _ in poll.select(max(0, deadline - time.monotonic())):
                    if key.fileobj is process.stdin:
                        sent = os.write(process.stdin.fileno(), raw)
                        raw = raw[sent:]
                        if not raw:
                            poll.unregister(process.stdin)
                    else:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise UnavailableError('Protected scheduling service stopped')
                        frame.extend(chunk)
                        if len(frame) > RESPONSE_LIMIT:
                            raise UnavailableError('Protected scheduling response was too large')
                        if b'\n' in frame:
                            if not frame.endswith(b'\n') or frame.count(b'\n') != 1:
                                raise UnavailableError('Invalid protected scheduling response')
                            result = json.loads(frame)
                            if not isinstance(result, dict) or result.get('status') not in STATUSES:
                                raise UnavailableError('Invalid protected scheduling response')
                            expected = {'status', 'result'} if result['status'] == 'ok' else {'status', 'error'}
                            if set(result) != expected or ('error' in result and not isinstance(result['error'], str)):
                                raise UnavailableError('Invalid protected scheduling response')
                            # Presence may expire during IO. Never release even a
                            # previously authorized result to a now-locked display.
                            self.authorize()
                            return result
        raise UnavailableError('Protected scheduling authorization expired')

    def request(self, value):
        return self.exchange({'request': value})

    def tick(self):
        if self.is_ready():
            self.exchange({'tick': True})

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if not process.stdin.closed:
                process.stdin.close()
            # cgroup teardown is the authority boundary, not cooperative exit.
            # It also kills detached browser, provider and MCP descendants.
            self.sessions.isolation.stop(self.scope)
        except Exception:
            self.sessions.fault = True
            raise
        finally:
            process.stdout.close()


def serve(scheduler, source, destination):
    """No ticks without a fresh broker heartbeat; EOF/expiry revokes all runs."""
    from .scheduled_jobs import encode, REQUEST_LIMIT, RESPONSE_LIMIT
    frame = bytearray()
    deadline = time.monotonic() + AUTHORIZATION_SECONDS
    with selectors.DefaultSelector() as poll:
        poll.register(source, selectors.EVENT_READ)
        while not scheduler.stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if not poll.select(min(remaining, .1)):
                continue
            chunk = os.read(source.fileno(), 65536)
            if not chunk:
                return
            frame.extend(chunk)
            if len(frame) > REQUEST_LIMIT + 1024:
                return
            if b'\n' not in frame:
                continue
            if not frame.endswith(b'\n') or frame.count(b'\n') != 1:
                return
            try:
                value = json.loads(frame)
                frame.clear()
                if isinstance(value, dict) and set(value) == {'tick'} and value['tick'] is True:
                    scheduler.tick()
                    result = {'status': 'ok', 'result': {}}
                elif isinstance(value, dict) and set(value) == {'request'}:
                    result = {'status': 'ok', 'result': scheduler.dispatch(value['request'])}
                else:
                    return
            except SchedulingError as error:
                result = {'status': error.code, 'error': str(error)}
            except (OSError, sqlite3.Error):
                scheduler.health_error = 'Protected scheduling storage or process supervision failed'
                result = {'status': 'unavailable', 'error': scheduler.health_error}
            except (ValueError, TypeError, RecursionError):
                result = {'status': 'invalid', 'error': 'Invalid scheduling request'}
            # A slow request cannot extend a lease that already expired.
            if time.monotonic() >= deadline:
                return
            deadline = time.monotonic() + AUTHORIZATION_SECONDS
            try:
                raw = encode(result, RESPONSE_LIMIT)
            except SchedulingError:
                raw = encode({'status': 'invalid', 'error': 'Request a smaller scheduling page'},
                             RESPONSE_LIMIT)
            destination.write(raw)
            destination.flush()


def main():
    from .scheduler import Scheduler
    os.umask(0o077)
    scheduler = Scheduler(protected=True)
    def stop(*_):
        scheduler.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        sys.stdout.buffer.write(b'{"ready":true}\n')
        sys.stdout.buffer.flush()
        serve(scheduler, sys.stdin.buffer, sys.stdout.buffer)
    except (BrokenPipeError, InterruptedError):
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        scheduler.close()


if __name__ == '__main__':
    main()
