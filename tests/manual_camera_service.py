"""Opt-in physical service check. Activates the camera; retains only counts/times.

Run as the ordinary guest account with PYTHONPATH pointing to installed AIOS.
No model, enrollment, portrait file, or biometric record is created.
"""
import json
import argparse
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
from collections import Counter
from aios.capture_worker import ERROR_CODES
from camera_resources import Resources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--removal', action='store_true')
    parser.add_argument('--cycles', type=int, choices=range(1, 101), default=3)
    parser.add_argument('--duration-seconds', type=int, default=0,
                        help='Run for this wall time instead of a cycle count (maximum 86400).')
    parser.add_argument('--interval', type=float, default=0,
                        help='Seconds idle between cycles (maximum 60).')
    args = parser.parse_args()
    if not 0 <= args.duration_seconds <= 86400 or not 0 <= args.interval <= 60:
        parser.error('Duration or interval is outside its bounded range')
    if os.geteuid() == 0:
        raise RuntimeError('run as the ordinary guest account')
    with tempfile.TemporaryDirectory(prefix='aios-camera-check-') as directory:
        os.chmod(directory, 0o700)
        env = {**os.environ, 'XDG_RUNTIME_DIR': directory, 'XDG_CONFIG_HOME': directory,
               'XDG_DATA_HOME': directory, 'AIOS_CAPTURE_DIAGNOSTICS': '1'}
        path = str(Path(directory) / 'service.sock')
        server = subprocess.Popen([sys.executable, '-m', 'aios.capture_service', '--socket', path],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        resources = Resources(server.pid)
        resources.start()
        os.set_blocking(server.stderr.fileno(), False)
        diagnostic_buffer = bytearray()
        diagnostics = Counter()
        def drain_diagnostics():
            data = server.stderr.read(4096)
            if not data:
                return
            diagnostic_buffer.extend(data)
            if len(diagnostic_buffer) > 8192:
                raise RuntimeError('oversized diagnostic response')
            while b'\n' in diagnostic_buffer:
                line, _, rest = diagnostic_buffer.partition(b'\n')
                diagnostic_buffer[:] = rest
                try:
                    value = json.loads(line)
                    if type(value) is not dict:
                        continue
                    if (set(value) == {'camera_failure_code'} and type(value['camera_failure_code']) is str and
                            value['camera_failure_code'] in ERROR_CODES):
                        diagnostics[value['camera_failure_code']] += 1
                    elif (set(value) == {'driver_error_frames', 'empty_frames'} and
                          all(type(count) is int and 0 <= count <= 16 for count in value.values())):
                        diagnostics.update(value)
                except (ValueError, TypeError):
                    pass
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(7)
        buffer = bytearray()
        consumer = uuid.uuid4().hex
        counts = {'previews': 0, 'photos': 0, 'preemptions': 0, 'inactive_rejections': 0}
        ages = []
        preview_latencies, photo_latencies = [], []
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
                drain_diagnostics()
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
            if args.removal:
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
            cycles = 0
            next_progress = started + 60
            while (time.monotonic() - started < args.duration_seconds if args.duration_seconds else cycles < args.cycles):
                operation_started = time.monotonic()
                preview = send('capture', 'preview')
                event = receive({'preview', 'error'}, preview)
                if event['event'] != 'preview':
                    raise RuntimeError(f"preview unavailable after {counts['previews']} completions: {event.get('reason')}")
                counts['previews'] += 1
                preview_latencies.append(time.monotonic() - operation_started)
                ages.append(event['processed_at'] - event['captured_at'])
                operation_started = time.monotonic()
                photo = send('capture', 'photo')
                event = receive({'photo', 'error'}, photo)
                if event['event'] != 'photo':
                    raise RuntimeError(f"photo unavailable after {counts['photos']} completions: {event.get('reason')}")
                counts['photos'] += 1
                photo_latencies.append(time.monotonic() - operation_started)
                ages.append(event['processed_at'] - event['captured_at'])
                # Deliberately discard image payloads before the next operation.
                event.clear()
                send('release')
                cycles += 1
                if time.monotonic() >= next_progress:
                    print(json.dumps({'progress': True, 'cycles': cycles,
                                      'elapsed': round(time.monotonic() - started, 3)}), flush=True)
                    next_progress = time.monotonic() + 60
                if args.interval:
                    time.sleep(args.interval)
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
            def p95(values):
                return round(sorted(values)[min(len(values) - 1, int(len(values) * .95))], 3)
            sampled = resources.finish()
            drain_diagnostics()
            if sampled['peak_camera_owners'] > 1 or sampled['sampling_errors']:
                raise RuntimeError('resource sampling or exclusive ownership check failed')
            print(json.dumps({'ok': True, 'uid': os.getuid(), **counts,
                              'worker_terminated': True, 'elapsed': round(time.monotonic() - started, 3),
                              'maximum_frame_age': round(max(ages), 3),
                              'preview_first_frame_p95_seconds': p95(preview_latencies),
                              'photo_handoff_p95_seconds': p95(photo_latencies),
                              'diagnostics': dict(diagnostics),
                              'resources': sampled}))
        finally:
            resources.finish()
            client.close()
            if server.poll() is None:
                server.kill()
                server.wait(timeout=3)
            drain_diagnostics()
            if diagnostics:
                print(json.dumps({'camera_diagnostics': dict(diagnostics)}), flush=True)
            server.stderr.close()


if __name__ == '__main__':
    main()
