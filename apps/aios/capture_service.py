"""One unprivileged, parent-authenticated desktop camera service (Linux only)."""
import argparse
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

from .core import load_config
from .recognition import CaptureSchedule
from .capture_worker import MAX_EVENT

MAX_REQUEST = 4096
MODES = {'preview', 'photo', 'enroll', 'recognize', 'purge'}
IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_field')
            result[key] = value
        return result
    if len(raw) > MAX_REQUEST:
        raise ValueError('oversized')
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    if type(value) is not dict or value.get('version') != 1 or type(value.get('version')) is not int:
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
        self.explicit_lease = None
        self.buffer = bytearray()
        self.deadline = 0
        self.sequence = 0
        self.devices = scan()
        self.next_scan = 0
        self.enabled = False
        self.closed = False

    def event(self, kind, payload=None, reason='ok', request=None, sequence=0, captured_at=0):
        request = request or self.job or {}
        value = {'version': 1, 'event': kind, 'request': request.get('request', ''),
                 'consumer': request.get('consumer', ''), 'generation': self.generation,
                 'sequence': sequence, 'captured_at': captured_at, 'processed_at': self.clock(),
                 'reason': reason, 'payload': payload or {}}
        self.send(value)

    def cancel(self, reason='cancelled'):
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
        if self.job:
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
            if self.job and self.job['consumer'] == request['consumer']:
                self.cancel()
        elif action == 'capture':
            self.start(request)

    def start(self, request, background=False):
        mode = request['mode']
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
        self.deadline = self.clock() + {'preview': 35, 'photo': 5, 'recognize': 5, 'enroll': 30, 'purge': 5}[mode]
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
            if (self.config().get('camera_recognition') is True) != self.enabled:
                self.command({'action': 'configure', 'active': self.active, 'secure': self.secure})
            self.next_scan = now + .5
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
            event = json.loads(raw)
            if set(event) != {'kind', 'sequence', 'captured_at', 'payload'}:
                raise ValueError()
            kind, sequence, captured = event['kind'], event['sequence'], event['captured_at']
            if kind not in ('preview', 'photo', 'progress', 'result', 'error') or type(event['payload']) is not dict:
                raise ValueError()
            if kind == 'error':
                self.fail('unavailable')
                return False
            if type(sequence) is not int or sequence < self.sequence or (kind in ('preview', 'progress') and sequence == self.sequence):
                raise ValueError()
            if type(captured) not in (float, int) or not math.isfinite(captured) or captured < 0:
                raise ValueError()
            if captured and not 0 <= self.clock() - captured <= 3:
                raise ValueError()
            if kind in ('preview', 'photo') and kind != self.job['mode']:
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
                        type(payload['samples']) is not int or not 0 <= payload['samples'] <= 3 or
                        payload['target'] != 3 or payload['reason'] not in ('look_straight', 'turn_slightly',
                        'turn_other_way', 'improve_light_or_hold_still', 'one_person_only', 'face_camera', 'sample_accepted')):
                    raise ValueError()
            if kind == 'result':
                mode = self.job['mode']
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
                candidate = payload.get('suggestion')
                if candidate is not None:
                    if type(candidate) is not dict or set(candidate) != {'id', 'name', 'photo', 'confidence', 'expires_in'}:
                        raise ValueError()
                    if (type(candidate['id']) is not str or str(uuid.UUID(candidate['id'])) != candidate['id'] or
                            type(candidate['name']) is not str or len(candidate['name']) > 120 or
                            type(candidate['photo']) is not str or len(candidate['photo']) > 40000 or
                            candidate['confidence'] != 'candidate' or candidate['expires_in'] != 5):
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
