"""Single per-OS-user scheduler, independent of shell and originating chats."""
import fcntl
import ctypes
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import time

from . import scheduled_jobs as protocol
from .scheduled_execution import bind, binding, permitted_capabilities, NeedsUserAction
from .scheduled_store import ScheduledStore
from .scheduling import Schedule, SchedulingError, UnavailableError, zone

RECHECK_SECONDS = 1
MAX_CLIENTS = 16


def locked_file(path, *, blocking=False):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise UnavailableError('Scheduler lease must be a private user-owned file')
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BaseException:
        os.close(fd)
        raise
    return fd


def configured_zone():
    value = os.environ.get('TZ', '').lstrip(':')
    if not value:
        try:
            value = Path('/etc/timezone').read_text().strip()
        except FileNotFoundError:
            target = str(Path('/etc/localtime').resolve())
            marker = '/zoneinfo/'
            value = target.split(marker, 1)[1] if marker in target else ''
    try:
        zone(value)
        return value
    except SchedulingError:
        return None


class Scheduler:
    def __init__(self, *, protected=False):
        if os.getuid() == 0:
            raise UnavailableError('Run the scheduler as the unprivileged desktop user')
        self.protected = protected
        if protected:
            from .principals import current
            principal = current()
            if principal is None or not principal.owner:
                raise UnavailableError('Protected scheduling requires an owner sandbox')
            self.root = Path('/run/aios-scheduler')
            self.root.mkdir(mode=0o700, exist_ok=True)
            self.root.chmod(0o700)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise UnavailableError('Protected runtime is not private')
        else:
            self.root = protocol.runtime_dir()
        if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
            raise UnavailableError('Scheduler requires Linux child supervision')
        self.store = ScheduledStore(protected=protected)
        try:
            self.lease = locked_file(self.store.root / 'service.lock')
        except BlockingIOError:
            self.store.close()
            raise UnavailableError('A scheduler already owns this OS user') from None
        self.leases = self.store.root / 'leases'
        self.running = {}
        self.stopping = False
        self.health_error = None
        try:
            self.leases.mkdir(mode=0o700, exist_ok=True)
            info = self.leases.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise UnavailableError('Scheduler process leases must be private')
            # An old run retains its lease until its subreaper has stopped all
            # descendants. Never recover SQLite first and allow duplicate work.
            self.recover_processes()
            if protected:
                # Broker loss always revokes this scope, even if the cgroup
                # was killed before its pipe supervisor could persist cleanup.
                for run in self.store.active_runs():
                    self.store.cancel(run['id'])
            else:
                self.store.recover()
        except BaseException:
            self.store.close()
            os.close(self.lease)
            raise

    def recover_processes(self):
        for path in self.leases.iterdir():
            deadline = time.monotonic() + 10
            while True:
                try:
                    lease = locked_file(path)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise UnavailableError('Previous scheduled processes have not stopped; recovery is blocked')
                    time.sleep(0.05)
            os.close(lease)
            path.unlink()
        for directory in self.root.glob('run-*'):
            if directory.is_symlink() or not directory.is_dir():
                raise UnavailableError('Invalid scheduler run directory')
            shutil.rmtree(directory)

    def readback(self, job):
        job['next_occurrences'] = Schedule.parse(job['schedule']).preview(self.store.clock.now())
        return job

    def configuration(self, value):
        from .scheduling import validate_job
        config = validate_job(value, self.store.clock.now())
        config['execution'], _ = bind(config['execution'])
        allowed = set(permitted_capabilities()['capabilities'])
        if set(config['execution']['capabilities']) - allowed:
            raise SchedulingError('Choose only currently permitted background capabilities')
        return config

    def background_peer(self, pid):
        workers = {entry[0].pid for entry in self.running.values()}
        for _ in range(128):
            if pid in workers:
                return True
            if pid <= 1 or pid == os.getpid():
                return False
            try:
                data = Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()
                pid = int(data[1])
            except (FileNotFoundError, IndexError, ValueError):
                return True
        return True

    def dispatch(self, value):
        protocol.validate_request(value)
        action = value['action']
        if self.health_error and action in ('create', 'update', 'resume', 'run_now'):
            raise UnavailableError(self.health_error)
        args = {key: item for key, item in value.items() if key != 'action'}
        if action == 'health':
            return {'available': self.health_error is None, 'error': self.health_error,
                    'active_runs': len(self.running), 'zone': configured_zone()}
        if action == 'binding':
            return {**binding(args['prompt']), **permitted_capabilities(), 'zone': configured_zone()}
        if action == 'preview':
            schedule = Schedule.parse(args['schedule'])
            return schedule.preview(self.store.clock.now())
        if action == 'create':
            return self.readback(self.store.create(self.configuration(args['config'])))
        if action == 'update':
            args['config'] = self.configuration(args['config'])
            return self.readback(self.store.update(**args))
        if action == 'get':
            return self.readback(self.store.get(**args))
        if action == 'list':
            return [{key: item for key, item in self.readback(job).items()
                     if key not in ('prompt', 'context')}
                    for job in self.store.list_jobs(**args)]
        if action in ('pause', 'resume'):
            if action == 'resume':
                bind(self.store.get(args['job_id'])['execution'], require_credentials=True)
            return self.readback(getattr(self.store, action)(**args))
        if action == 'delete':
            self.store._job(args['job_id'], args['expected_revision'])
            for run in self.store.active_runs():
                if run['job_id'] == args['job_id']:
                    self.stop_run(run['id'])
            self.store.delete(**args)
            return {'deleted': True}
        if action == 'run_now':
            return self.store.run_now(**args)
        if action == 'cancel_run':
            run = self.store.get_run(args['run_id'])
            if run['state'] not in ('queued', 'running'):
                return run
            self.stop_run(run['id'])
            return self.store.cancel(run['id'])
        if action == 'list_runs':
            return self.store.list_runs(**args)
        if action == 'read_result':
            return self.store.get_run(**args)
        if action == 'acknowledge_result':
            self.store.acknowledge(**args)
            return {'acknowledged': True}
        if action == 'mark_notified':
            self.store.mark_notified(**args)
            return {'notified': True}
        if action == 'unread':
            return self.store.unread(**args)
        raise SchedulingError('Unknown scheduling action')

    def launch(self, run):
        try:
            if '@' not in run['snapshot']['execution']['profile']:
                raise NeedsUserAction('Save this job again to bind its provider and model explicitly')
            bind(run['snapshot']['execution'], require_credentials=True)
        except NeedsUserAction as error:
            self.store.block(run['id'], str(error))
            return
        directory = self.root / ('run-' + run['id'])
        directory.mkdir(mode=0o700)
        lease = locked_file(self.leases / run['id'])
        process = None
        try:
            payload = protocol.encode(run, protocol.RUN_REQUEST_LIMIT)
            process = subprocess.Popen(
                [sys.executable, '-m', 'aios.scheduled_runner', str(lease), str(directory)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(lease,), start_new_session=True)
            os.set_blocking(process.stdout.fileno(), False)
            self.running[run['id']] = (process, directory, bytearray())
            self.store.start(run['id'])
            process.stdin.write(payload)
            process.stdin.flush()
        except (OSError, SchedulingError):
            if process is not None:
                self.stop_run(run['id'])
            else:
                shutil.rmtree(directory)
                (self.leases / run['id']).unlink(missing_ok=True)
            if self.store.get_run(run['id'])['state'] == 'queued':
                self.store.start(run['id'])
            self.store.finish(run['id'], 'failed', error='Scheduled worker could not start')
        finally:
            os.close(lease)

    def stop_run(self, run_id):
        entry = self.running.get(run_id)
        if entry is None:
            return
        process, directory, _ = entry
        if not process.stdin.closed:
            process.stdin.close()
        deadline = time.monotonic() + 10
        while process.poll() is None:
            try:
                os.read(process.stdout.fileno(), 65536)
            except BlockingIOError:
                pass
            if time.monotonic() >= deadline:
                raise UnavailableError('Scheduled process cleanup is still pending')
            time.sleep(0.02)
        if process.returncode != 0:
            self.crashed_pool()
            raise UnavailableError(self.health_error)
        self.release_run(run_id)

    def release_run(self, run_id):
        process, directory, _ = self.running.pop(run_id)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()
        if directory.exists():
            shutil.rmtree(directory)
        (self.leases / run_id).unlink(missing_ok=True)

    def crashed_pool(self):
        # A dead run subreaper's children belong to this service now. Recovery
        # must also cover a cancel/delete arriving before the next timer tick.
        from .scheduled_runner import clean_children
        for entry in self.running.values():
            if not entry[0].stdin.closed:
                entry[0].stdin.close()
        clean_children()
        for run_id in list(self.running):
            self.running[run_id][0].wait()
            self.release_run(run_id)
        self.store.recover()
        self.health_error = 'Scheduled supervisor crashed; restart the service before dispatch'

    def tick(self):
        for run_id, (process, directory, frame) in list(self.running.items()):
            while True:
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except BlockingIOError:
                    break
                if not chunk:
                    break
                frame.extend(chunk)
                if len(frame) > 512 * 1024:
                    self.stop_run(run_id)
                    self.store.finish(run_id, 'failed', error='Scheduled supervisor response was too large')
                    break
            if run_id not in self.running:
                continue
            if process.poll() is None:
                continue
            while chunk := os.read(process.stdout.fileno(), 65536):
                frame.extend(chunk)
                if len(frame) > 512 * 1024:
                    frame.clear()
                    break
            if process.returncode != 0:
                self.crashed_pool()
                return
            try:
                outcome = json.loads(frame)
                if (not isinstance(outcome, dict) or outcome.get('state') not in (
                        'succeeded', 'failed', 'needs_user_action', 'cancelled')):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                outcome = {'state': 'failed', 'error': 'Scheduled supervisor stopped unexpectedly'}
            self.stop_run(run_id)
            state = outcome.pop('state')
            if state == 'cancelled':
                self.store.cancel(run_id)
            elif state == 'needs_user_action':
                self.store.block(run_id, outcome['error'])
            else:
                self.store.finish(run_id, state, **outcome)
        if self.stopping or self.health_error:
            return
        self.store.claim_due()
        for run in self.store.active_runs():
            if run['state'] == 'queued':
                self.launch(run)
        self.store.prune()

    def close(self):
        self.stopping = True
        try:
            for run_id in list(self.running):
                self.stop_run(run_id)
            if self.protected:
                for run in self.store.active_runs():
                    self.store.cancel(run['id'])
            else:
                self.store.recover()
        finally:
            self.store.close()
            os.close(self.lease)

    def serve(self):
        path = self.root / 'service.sock'
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise UnavailableError('Scheduler socket path is occupied')
            path.unlink()
        with socket.socket(socket.AF_UNIX) as server, selectors.DefaultSelector() as poll:
            server.bind(str(path))
            path.chmod(0o600)
            server.listen(MAX_CLIENTS)
            server.setblocking(False)
            poll.register(server, selectors.EVENT_READ)
            clients = {}
            next_tick = 0

            def recheck():
                nonlocal next_tick
                if time.monotonic() < next_tick:
                    return
                try:
                    self.tick()
                except (sqlite3.Error, OSError, SchedulingError):
                    self.health_error = 'Scheduler storage or process supervision failed; dispatch is stopped'
                next_tick = time.monotonic() + RECHECK_SECONDS

            try:
                while not self.stopping:
                    recheck()
                    for key, _ in poll.select(0.1):
                        recheck()
                        if key.fileobj is server:
                            client, _ = server.accept()
                            try:
                                pid = protocol.same_user(client)
                                if self.background_peer(pid):
                                    raise UnavailableError('Background runs cannot access scheduling operations')
                                if len(clients) >= MAX_CLIENTS:
                                    client.close()
                                    continue
                                client.setblocking(False)
                                clients[client] = [bytearray(), time.monotonic() + 2]
                                poll.register(client, selectors.EVENT_READ)
                            except (OSError, SchedulingError):
                                client.close()
                            continue
                        client = key.fileobj
                        try:
                            chunk = client.recv(8192)
                            frame, deadline = clients[client]
                            frame.extend(chunk)
                            if len(frame) > protocol.REQUEST_LIMIT:
                                raise SchedulingError('Scheduling request is too large')
                            if b'\n' not in frame and chunk:
                                continue
                            value = json.loads(frame)
                            result = self.dispatch(value)
                            response = {'status': 'ok', 'result': result}
                        except SchedulingError as error:
                            response = {'status': error.code, 'error': str(error)}
                        except (ValueError, TypeError, RecursionError):
                            response = {'status': 'invalid', 'error': 'Invalid scheduling JSON'}
                        except (OSError, sqlite3.Error):
                            self.health_error = 'Scheduler storage or process supervision failed; dispatch is stopped'
                            response = {'status': 'unavailable', 'error': self.health_error}
                        try:
                            try:
                                raw = protocol.encode(response, protocol.RESPONSE_LIMIT)
                            except SchedulingError as error:
                                raw = protocol.encode({'status': error.code, 'error': str(error)}, protocol.RESPONSE_LIMIT)
                            client.settimeout(0.1)
                            client.sendall(raw)
                        except OSError:
                            pass
                        poll.unregister(client)
                        client.close()
                        clients.pop(client)
                    for client, (_, deadline) in list(clients.items()):
                        if time.monotonic() >= deadline:
                            poll.unregister(client)
                            client.close()
                            clients.pop(client)
            finally:
                for client in clients:
                    client.close()
                path.unlink(missing_ok=True)


def main():
    scheduler = Scheduler()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda *_: setattr(scheduler, 'stopping', True))
    try:
        scheduler.serve()
    finally:
        scheduler.close()


if __name__ == '__main__':
    main()
