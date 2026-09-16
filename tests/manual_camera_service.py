"""Opt-in physical service check. Activates the camera; retains only counts/times.

Run as the ordinary guest account with PYTHONPATH pointing to installed AIOS.
No model, enrollment, portrait file, or biometric record is created.
"""
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid


def main():
    if os.geteuid() == 0:
        raise RuntimeError('run as the ordinary guest account')
    with tempfile.TemporaryDirectory(prefix='aios-camera-check-') as directory:
        os.chmod(directory, 0o700)
        env = {**os.environ, 'XDG_RUNTIME_DIR': directory, 'XDG_CONFIG_HOME': directory,
               'XDG_DATA_HOME': directory}
        path = str(Path(directory) / 'service.sock')
        server = subprocess.Popen([sys.executable, '-m', 'aios.capture_service', '--socket', path],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(7)
        buffer = bytearray()
        consumer = uuid.uuid4().hex
        counts = {'previews': 0, 'photos': 0, 'preemptions': 0, 'inactive_rejections': 0}
        ages = []
        started = time.monotonic()
        def send(action, mode=None, **extra):
            request = {'version': 1, 'action': action, 'consumer': consumer, 'request': uuid.uuid4().hex}
            if mode:
                request.update(mode=mode, device='', owner='', pin='', consent=False)
            request.update(extra)
            client.sendall(json.dumps(request).encode() + b'\n')
            return request['request']
        def receive(wanted, identifier=None, timeout=7):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if b'\n' not in buffer:
                    client.settimeout(max(.1, deadline - time.monotonic()))
                    data = client.recv(262145)
                    if not data:
                        raise RuntimeError('service disconnected')
                    buffer.extend(data)
                    if len(buffer) > 524288:
                        raise RuntimeError('oversized event')
                    continue
                raw, _, rest = buffer.partition(b'\n')
                buffer[:] = rest
                event = json.loads(raw)
                if event['event'] == 'cancelled' and event['reason'] == 'preempted':
                    counts['preemptions'] += 1
                if (identifier is None or event['request'] == identifier) and event['event'] in wanted:
                    return event
            raise RuntimeError('event deadline')
        try:
            deadline = time.monotonic() + 5
            while not Path(path).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            client.connect(path)
            receive({'state'})
            send('configure', active=True, secure=False)
            if '--removal' in sys.argv:
                preview = send('capture', 'preview')
                receive({'preview'}, preview).clear()
                print(json.dumps({'ready_for_disconnect': True}), flush=True)
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    event = receive({'state', 'cancelled', 'error'}, timeout=60)
                    if event['reason'] == 'device_changed':
                        break
                else:
                    raise RuntimeError('device change was not observed')
                photo = send('capture', 'photo')
                event = receive({'error'}, photo)
                if event['reason'] != 'unavailable':
                    raise RuntimeError('missing device was not rejected')
                print(json.dumps({'ok': True, 'device_change_observed': True,
                                  'missing_device_rejected': True}), flush=True)
                return
            for _ in range(3):
                preview = send('capture', 'preview')
                event = receive({'preview', 'error'}, preview)
                if event['event'] != 'preview':
                    raise RuntimeError('preview unavailable')
                counts['previews'] += 1
                ages.append(event['processed_at'] - event['captured_at'])
                photo = send('capture', 'photo')
                event = receive({'photo', 'error'}, photo)
                if event['event'] != 'photo':
                    raise RuntimeError('photo unavailable')
                counts['photos'] += 1
                ages.append(event['processed_at'] - event['captured_at'])
                # Deliberately discard image payloads before the next operation.
                event.clear()
                send('release')
            send('configure', active=False, secure=True)
            rejected = send('capture', 'preview')
            event = receive({'error'}, rejected)
            if event['reason'] != 'inactive':
                raise RuntimeError('inactive gate failed')
            counts['inactive_rejections'] += 1
            send('configure', active=True, secure=False)
            preview = send('capture', 'preview')
            receive({'preview'}, preview).clear()
            children = Path('/proc') / str(server.pid) / 'task' / str(server.pid) / 'children'
            workers = children.read_text().split()
            if len(workers) != 1:
                raise RuntimeError('expected one worker')
            server.kill()
            server.wait(timeout=3)
            deadline = time.monotonic() + 3
            alive = True
            while time.monotonic() < deadline:
                status = Path('/proc') / workers[0] / 'stat'
                alive = status.exists() and status.read_text().split()[2] != 'Z'
                if not alive:
                    break
                time.sleep(.02)
            if alive:
                raise RuntimeError('worker survived service termination')
            print(json.dumps({'ok': True, 'uid': os.getuid(), **counts,
                              'worker_terminated': True, 'elapsed': round(time.monotonic() - started, 3),
                              'maximum_frame_age': round(max(ages), 3)}))
        finally:
            client.close()
            if server.poll() is None:
                server.kill()
                server.wait(timeout=3)


if __name__ == '__main__':
    main()
