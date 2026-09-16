"""One unprivileged, parent-authenticated desktop camera service (Linux only)."""
import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import sys
import time
import uuid
import stat

from .core import load_config, data_dir
from .recognition import CaptureSchedule
from .capture_worker import MAX_EVENT, ERROR_CODES

MAX_REQUEST = 4096
MODES = {'preview', 'photo', 'enroll', 'recognize', 'purge'}
IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')


def valid_portrait(value):
    if type(value) is not str:
        return False
    if not value:
        return True
    prefix = 'data:image/png;base64,'
    if not value.startswith(prefix) or len(value) > 20022:
        return False
    try:
        data = base64.b64decode(value[len(prefix):], validate=True)
        return data[:8] == b'\x89PNG\r\n\x1a\n' and data[12:24] == b'IHDR\0\0\0@\0\0\0@'
    except ValueError:
        return False


def decode(raw):
    value = strict_json(raw, MAX_REQUEST)
    return validate_request(value)


def strict_json(raw, maximum):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_field')
            result[key] = value
        return result
    if len(raw) > maximum:
        raise ValueError('oversized')
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    if type(value) is not dict:
        raise ValueError('object_required')
    return value


def validate_request(value):
    if value.get('version') != 1 or type(value.get('version')) is not int:
        raise ValueError('version')
    action = value.get('action')
    if type(action) is not str:
        raise ValueError('action')
    fields = {'version', 'action', 'request', 'consumer'}
    if action == 'configure':
        fields |= {'active', 'secure'}
        if any(type(value.get(key)) is not bool for key in ('active', 'secure')):
            raise ValueError('gate')
    elif action == 'capture':
        fields |= {'mode', 'device', 'owner', 'pin', 'consent'}
        if type(value.get('mode')) is not str or value.get('mode') not in MODES:
            raise ValueError('mode')
        if type(value.get('device')) is not str or not re.fullmatch(r'(?:[a-f0-9]{64})?', value['device']):
            raise ValueError('device')
        if type(value.get('owner')) is not str or len(value['owner']) > 64:
            raise ValueError('owner')
        if type(value.get('pin')) is not str or len(value['pin']) > 128 or type(value.get('consent')) is not bool:
            raise ValueError('consent')
        if value['mode'] != 'enroll' and (value['owner'] or value['pin'] or value['consent']):
            raise ValueError('unexpected_credentials')
    elif action not in ('release', 'refresh', 'shutdown'):
        raise ValueError('action')
    if set(value) != fields:
        raise ValueError('fields')
    if any(type(value.get(key)) is not str or not IDENTIFIER.fullmatch(value[key])
           for key in ('request', 'consumer')):
        raise ValueError('identifier')
    return value


def allowed_peer(connection, parent_pid, uid):
    pid, peer_uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return pid == parent_pid and peer_uid == uid


def inventory():
    result = {}
    for path in Path('/dev/v4l/by-id').glob('*-video-index0'):
        if not re.fullmatch(r'[A-Za-z0-9._:+-]+-video-index0', path.name) or not path.is_char_device():
            continue
        stat = path.stat()
        key = hashlib.sha256(str(path).encode()).hexdigest()
        result[key] = (str(path), stat.st_ino, stat.st_rdev)
    return result


def policy_signature(config):
    manifest = Path(os.environ.get('AIOS_FACE_MODEL_MANIFEST', '/etc/aios/face-models.json'))
    root = data_dir() / 'chat-profiles'
    paths = [manifest]
    approved = False
    try:
        if manifest.stat().st_size <= 65536:
            value = json.loads(manifest.read_text())
            if type(value) is not dict or type(value.get('approval')) is not dict:
                raise ValueError('invalid_manifest')
            approved = value.get('approval', {}).get('expires_at', 0) > time.time()
            paths.extend(Path(value[name]['path']) for name in ('yunet', 'sface'))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    def stamp(path):
        try:
            value = path.stat()
            return str(path), value.st_ino, value.st_mtime_ns, value.st_size
        except OSError:
            return str(path), None
    return (config.get('camera_device'), approved, tuple(stamp(path) for path in paths),
            tuple(stamp(path) for path in (root / 'profiles.json', root / 'biometrics/local-greeting/current')))


class Service:
    def __init__(self, send, clock=time.monotonic, spawn=subprocess.Popen, scan=inventory, config=load_config):
        self.send = send
        self.clock = clock
        self.spawn = spawn
        self.scan = scan
        self.config = config
        self.schedule = CaptureSchedule(clock)
        self.active = False
        self.secure = False
        self.generation = 1
        self.worker = None
        self.job = None
        self.pending = None
        self.pending_deadline = 0
        self.pending_job_deadline = 0
        self.explicit_lease = None
        self.buffer = bytearray()
        self.deadline = 0
        self.sequence = 0
        self.devices = scan()
        self.next_scan = 0
        self.enabled = False
        self.closed = False
        self.policy = policy_signature(config())

    def event(self, kind, payload=None, reason='ok', request=None, sequence=0, captured_at=0):
        request = request or self.job or {}
        value = {'version': 1, 'event': kind, 'request': request.get('request', ''),
                 'consumer': request.get('consumer', ''), 'generation': self.generation,
                 'sequence': sequence, 'captured_at': captured_at, 'processed_at': self.clock(),
                 'reason': reason, 'payload': payload or {}}
        self.send(value)

    def cancel(self, reason='cancelled'):
        if self.pending is not None:
            self.event('cancelled', reason=reason, request=self.pending)
            self.pending = None
        if reason != 'completed':
            self.generation += 1
        if self.worker:
            self.worker.kill()
            try:
                self.worker.wait(timeout=.1)
            except subprocess.TimeoutExpired:
                # Quarantine the handle until the kernel actually reaps it.
                # Never start a competing worker after an uninterruptible read.
                pass
            if self.worker.poll() is not None:
                self.worker.stdout.close()
                self.worker = None
        if self.job and reason != 'completed':
            self.event('cancelled', reason=reason)
        self.job = None
        self.buffer.clear()
        self.schedule.running = False
        self.schedule.next_capture = max(self.schedule.next_capture, self.clock() + 2)
        self.event('state', {'state': 'disabled' if not self.enabled else 'ready'}, reason)

    def refresh(self):
        devices = self.scan()
        if devices != self.devices:
            self.cancel('device_changed')
            self.devices = devices
            self.schedule.device_added()

    def command(self, request):
        action = request['action']
        if action == 'shutdown':
            self.closed = True
            self.cancel('shutdown')
        elif action == 'configure':
            enabled = self.config().get('camera_recognition') is True
            changed = (enabled, request['active'], request['secure']) != (self.enabled, self.active, self.secure)
            self.enabled, self.active, self.secure = enabled, request['active'], request['secure']
            if changed:
                self.cancel('gate_changed')
                self.schedule.configure(enabled)
                self.schedule.set_active(self.active and not self.secure)
        elif action == 'refresh':
            self.refresh()
        elif action == 'release':
            if self.explicit_lease == request['consumer']:
                self.explicit_lease = None
            if ((self.job and self.job['consumer'] == request['consumer']) or
                    (self.pending and self.pending['consumer'] == request['consumer'])):
                self.cancel()
        elif action == 'capture':
            self.start(request)

    def start(self, request, background=False, deadline=None):
        mode = request['mode']
        if self.pending is not None:
            if mode == 'purge':
                self.cancel('preempted')
            else:
                self.event('error', reason='busy', request=request)
                return
        if (not self.active and mode != 'purge') or (mode == 'recognize' and (self.secure or not self.enabled)):
            self.event('error', reason='inactive', request=request)
            return
        if mode == 'enroll' and (not self.enabled or request.get('consent') is not True):
            self.event('error', reason='consent_required', request=request)
            return
        if self.worker:
            if self.job and (mode == 'purge' or (self.job['mode'] in ('preview', 'recognize') and mode in ('photo', 'enroll'))):
                self.cancel('preempted')
            if self.worker:
                if self.job is None:
                    # A cancelled/completed worker can take longer than the
                    # immediate reap budget. Queue one handoff, never overlap
                    # device owners or turn delayed teardown into a retry storm.
                    self.pending = request.copy()
                    self.pending_deadline = self.clock() + 1
                    self.pending_job_deadline = deadline or self.clock() + {
                        'preview': 35, 'photo': 5, 'recognize': 5, 'enroll': 30, 'purge': 5}[mode]
                    if mode != 'recognize':
                        self.explicit_lease = request['consumer']
                    return
                self.event('error', reason='busy', request=request)
                return
        if mode == 'recognize' and not self.schedule.due(immediate=not background):
            self.event('error', reason='cooldown', request=request)
            return
        config = self.config()
        chosen = request['device']
        if not chosen:
            preferred = config.get('camera_device', '')
            chosen = hashlib.sha256(preferred.encode()).hexdigest() if isinstance(preferred, str) and preferred else ''
            if not chosen and len(self.devices) == 1:
                chosen = next(iter(self.devices))
        if mode != 'purge' and chosen not in self.devices:
            self.event('error', reason='unavailable', request=request)
            if mode == 'recognize':
                self.schedule.finish(self.schedule.generation, False)
            return
        if mode == 'recognize':
            self.schedule.start(immediate=not background)
        self.generation += 1
        self.job = request.copy()
        if mode != 'recognize':
            self.explicit_lease = request['consumer']
        self.sequence = 0
        self.buffer.clear()
        self.deadline = deadline or self.clock() + {'preview': 35, 'photo': 5, 'recognize': 5, 'enroll': 30, 'purge': 5}[mode]
        payload = {'mode': mode, 'device': self.devices[chosen][0] if mode != 'purge' else '',
                   'owner': request['owner'], 'pin': request['pin'], 'consent': request['consent']}
        self.worker = self.spawn([sys.executable, '-m', 'aios.capture_worker'], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                 env={**os.environ, 'AIOS_CAPTURE_PARENT_PID': str(os.getpid())})
        self.worker.stdin.write(json.dumps(payload).encode() + b'\n')
        self.worker.stdin.close()
        self.job['pin'] = ''
        os.set_blocking(self.worker.stdout.fileno(), False)
        self.event('state', {'state': 'enrolling' if mode == 'enroll' else 'capturing'})

    def tick(self):
        now = self.clock()
        if now >= self.next_scan:
            self.refresh()
            policy = policy_signature(self.config())
            if policy != self.policy:
                # Enrollment and purge legitimately publish account/template
                # updates themselves. Their transactional locks handle races.
                if policy[:3] != self.policy[:3] or not self.job or self.job['mode'] == 'recognize':
                    self.cancel('configuration_changed')
                self.policy = policy
            if (self.config().get('camera_recognition') is True) != self.enabled:
                self.command({'action': 'configure', 'active': self.active, 'secure': self.secure})
            self.next_scan = now + .5
        if self.pending is not None:
            if self.worker and self.worker.poll() is not None:
                self.worker.stdout.close()
                self.worker = None
            if now >= self.pending_deadline:
                self.event('error', reason='busy', request=self.pending)
                self.pending = None
            elif self.worker is None:
                request, deadline = self.pending, self.pending_job_deadline
                self.pending = None
                self.start(request, deadline=deadline)
            return
        if self.worker:
            if not self.job:
                if self.worker.poll() is not None:
                    self.worker.stdout.close()
                    self.worker = None
                return
            if now >= self.deadline:
                self.fail('timeout')
                return
            data = self.worker.stdout.read(MAX_EVENT + 1)
            if data:
                self.buffer.extend(data)
                while b'\n' in self.buffer:
                    line, _, rest = self.buffer.partition(b'\n')
                    self.buffer[:] = rest
                    if len(line) > MAX_EVENT:
                        self.fail('oversized')
                        return
                    if not self.result(line):
                        return
                if len(self.buffer) > MAX_EVENT:
                    self.fail('oversized')
                    return
            # An exited writer may still have unread bytes in the pipe.
            if self.worker and not data and self.worker.poll() is not None:
                self.fail('worker_exit')
        elif not self.explicit_lease and self.enabled and self.active and not self.secure and self.schedule.due():
            self.start({'request': uuid.uuid4().hex, 'consumer': '0' * 32, 'mode': 'recognize',
                        'device': '', 'owner': '', 'pin': '', 'consent': False}, background=True)

    def result(self, raw):
        try:
            event = strict_json(raw, MAX_EVENT)
            if set(event) != {'kind', 'sequence', 'captured_at', 'payload'}:
                raise ValueError()
            kind, sequence, captured = event['kind'], event['sequence'], event['captured_at']
            if kind not in ('preview', 'photo', 'progress', 'result', 'error', 'diagnostic') or type(event['payload']) is not dict:
                raise ValueError()
            if kind == 'diagnostic':
                payload = event['payload']
                if (set(payload) != {'driver_error_frames', 'empty_frames'} or
                        any(type(value) is not int or not 0 <= value <= 16 for value in payload.values()) or
                        not 1 <= sum(payload.values()) <= 16 or type(sequence) is not int or sequence != 0 or
                        type(captured) not in (int, float) or captured != 0):
                    raise ValueError()
                if os.environ.get('AIOS_CAPTURE_DIAGNOSTICS') == '1':
                    print(json.dumps(payload), file=sys.stderr, flush=True)
                return True  # Private aggregate diagnostics never enter the native UI protocol.
            if kind == 'error':
                payload = event['payload']
                if ((payload and (set(payload) != {'code'} or type(payload['code']) is not str or payload['code'] not in ERROR_CODES)) or
                        type(sequence) is not int or sequence != 0 or type(captured) not in (float, int) or captured != 0):
                    raise ValueError()
                if os.environ.get('AIOS_CAPTURE_DIAGNOSTICS') == '1':
                    print(json.dumps({'camera_failure_code': payload.get('code', 'worker_error')}), file=sys.stderr, flush=True)
                self.fail('unavailable')
                return False
            if type(sequence) is not int or sequence < self.sequence or (kind in ('preview', 'progress') and sequence == self.sequence):
                raise ValueError()
            if type(captured) not in (float, int) or not math.isfinite(captured) or captured < 0:
                raise ValueError()
            if captured and not 0 <= self.clock() - captured <= 3:
                raise ValueError()
            if kind in ('preview', 'photo') and kind != self.job['mode'] and not (kind == 'preview' and self.job['mode'] == 'enroll'):
                raise ValueError()
            if kind == 'preview' and set(event['payload']) != {'image'}:
                raise ValueError()
            if kind == 'photo' and set(event['payload']) != {'image', 'rgb'}:
                raise ValueError()
            if kind == 'result' and set(event['payload']) - {'state', 'suggestion', 'reason', 'enrolled'}:
                raise ValueError()
            payload = event['payload']
            if kind == 'progress':
                if (self.job['mode'] != 'enroll' or set(payload) != {'samples', 'target', 'reason'} or
                        type(payload['samples']) is not int or not 0 <= payload['samples'] <= 10 or
                        payload['target'] != 10 or payload['reason'] != 'burst_capture'):
                    raise ValueError()
            if kind == 'result':
                mode = self.job['mode']
                if mode == 'photo':
                    raise ValueError()
                if mode == 'enroll' and set(payload) != {'enrolled'}:
                    raise ValueError()
                if mode == 'recognize' and 'state' not in payload:
                    raise ValueError()
                if mode in ('purge', 'preview') and payload != {'state': 'purged' if mode == 'purge' else 'manual-only'}:
                    raise ValueError()
                if 'enrolled' in payload and (set(payload) != {'enrolled'} or
                        type(payload['enrolled']) is not str or str(uuid.UUID(payload['enrolled'])) != payload['enrolled']):
                    raise ValueError()
                if 'state' in payload and payload['state'] not in ('disabled', 'ready', 'manual-only', 'unavailable', 'purged'):
                    raise ValueError()
                if 'reason' in payload and (type(payload['reason']) is not str or len(payload['reason']) > 64):
                    raise ValueError()
                if 'reason' in payload and payload['reason'] not in ('no-enrollment', 'unknown-or-ambiguous', 'deleted', 'configuration-changed'):
                    raise ValueError()
                candidate = payload.get('suggestion')
                if candidate is not None:
                    if type(candidate) is not dict or set(candidate) != {'id', 'name', 'photo', 'confidence', 'expires_at'}:
                        raise ValueError()
                    if (type(candidate['id']) is not str or str(uuid.UUID(candidate['id'])) != candidate['id'] or
                            type(candidate['name']) is not str or not 1 <= len(candidate['name']) <= 80 or re.search(r'[\x00-\x1f]', candidate['name']) or
                            not valid_portrait(candidate['photo']) or
                            candidate['confidence'] != 'candidate' or
                            type(candidate['expires_at']) not in (float, int) or
                            not math.isfinite(candidate['expires_at']) or captured <= 0 or sequence < 3 or
                            candidate['expires_at'] != captured + 5 or candidate['expires_at'] <= self.clock() or
                            mode != 'recognize' or payload.get('state') != 'ready'):
                        raise ValueError()
            if kind in ('photo', 'preview'):
                image = payload['image']
                prefix = 'data:image/png;base64,' if kind == 'photo' else 'data:image/jpeg;base64,'
                if type(image) is not str or not image.startswith(prefix) or len(image) > 240000:
                    raise ValueError()
                if kind == 'photo' and (type(payload['rgb']) is not str or len(payload['rgb']) != 16384):
                    raise ValueError()
            self.sequence = sequence
            self.event(kind, event['payload'], sequence=sequence, captured_at=captured)
            if kind not in ('preview', 'progress'):
                mode = self.job['mode']
                self.cancel('completed')
                if mode == 'recognize':
                    self.schedule.finish(self.schedule.generation, True)
                return False
            return True
        except (ValueError, TypeError, KeyError):
            self.fail('invalid_result')
            return False

    def fail(self, reason):
        mode = self.job['mode'] if self.job else None
        self.event('error', reason=reason)
        self.cancel(reason)
        if mode == 'recognize':
            self.schedule.finish(self.schedule.generation, False)


def serve(path):
    if os.geteuid() == 0 or os.environ.get('AIOS_SESSION_SOCKET'):
        raise RuntimeError('unprivileged account required')
    os.umask(0o077)
    parent, uid = os.getppid(), os.getuid()
    endpoint = Path(path)
    directory = endpoint.parent
    if directory.stat().st_uid != uid or directory.stat().st_mode & 0o077:
        raise RuntimeError('private endpoint directory required')
    # One service per desktop UID, even when a second shell is launched.
    lock_path = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / ('aios-camera-' + str(uid) + '.lock')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    lock_stat = os.fstat(descriptor)
    if lock_stat.st_uid != uid or not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
        os.close(descriptor)
        raise RuntimeError('unsafe lock')
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection = None
    service = None
    try:
        server.bind(str(endpoint))
        server.listen(2)
        server.setblocking(False)
        buffer = bytearray()
        def send(value):
            data = json.dumps(value, allow_nan=False, separators=(',', ':')).encode() + b'\n'
            if len(data) > MAX_EVENT:
                raise ValueError('oversized')
            connection.sendall(data)  # 50ms maximum, no unbounded output queue.
        service = Service(send)
        while os.getppid() == parent and not service.closed:
            readable, _, _ = select.select([server] + ([connection] if connection else []), [], [], .05)
            if server in readable:
                peer, _ = server.accept()
                if connection or not allowed_peer(peer, parent, uid):
                    peer.close()
                else:
                    connection = peer
                    connection.settimeout(.05)
                    service.event('state', {'state': 'disabled'})
            if connection and connection in readable:
                data = connection.recv(MAX_REQUEST + 1)
                if not data:
                    break
                buffer.extend(data)
                while b'\n' in buffer:
                    line, _, rest = buffer.partition(b'\n')
                    buffer[:] = rest
                    service.command(decode(line))
                if len(buffer) > MAX_REQUEST:
                    break
            if connection:
                service.tick()
    finally:
        if service:
            # Socket may already be gone; cleanup must not depend on delivery.
            service.send = lambda _: None
            service.cancel('shutdown')
        if connection:
            connection.close()
        server.close()
        endpoint.unlink(missing_ok=True)
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket', required=True)
    args = parser.parse_args()
    try:
        serve(args.socket)
    except (OSError, ValueError, RuntimeError):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
