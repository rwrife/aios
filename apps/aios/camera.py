"""Explicit, bounded Linux webcam diagnostic. Never saves or emits raw frames."""
import argparse
import errno
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class ProbeError(RuntimeError):
    """A bounded reason and fixed, non-driver diagnostic message."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


FAILURES = {
    'permission_denied': 'Camera access denied; check the capture account video group and device permissions',
    'device_busy': 'Camera is busy; release preview and other capture consumers',
    'device_missing': 'Camera is not attached; reconnect it and resolve the stable capture node',
    'open_failed': 'Cannot open camera; check permissions, capture-node capability and other camera applications',
    'no_frame': 'Camera did not deliver a frame; check the cover, USB/IP attachment and negotiated format',
    'runtime_missing': 'Camera runtime unavailable; install the optional identity image dependencies',
    'capture_failed': 'Camera capture failed; check the UVC device and negotiated format',
}


def capture_device(value):
    return video_device(value, stable=isinstance(value, str) and value.startswith('/dev/v4l/by-id/'))


def bounds(frames, timeout):
    if (type(frames) is not int or not 1 <= frames <= 60
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 1 <= timeout <= 30):
        raise ValueError('Invalid probe bounds')


def video_device(value, stable=False):
    patterns = [r'/dev/video[0-9]+']
    if stable:
        patterns = [r'/dev/v4l/by-id/[A-Za-z0-9._:+-]+-video-index[0-9]+']
    if not isinstance(value, str) or not any(re.fullmatch(pattern, value) for pattern in patterns):
        expected = 'a stable /dev/v4l/by-id/*-video-indexN device' if stable else 'a local /dev/videoN device'
        raise ValueError(f'Choose {expected}; network streams are not accepted')
    path = Path(value)
    if not path.is_char_device():
        raise ValueError('Video device is not attached')
    return value


def devices():
    """Metadata only; enumerating devices does not open the camera."""
    result = []
    for path in sorted(Path('/sys/class/video4linux').glob('video*')):
        name = path / 'name'
        result.append({'device': '/dev/' + path.name,
                       'name': name.read_text().strip() if name.exists() else 'Video device'})
    return result


def capture_probe(device, frames):
    bounds(frames, 15)
    import cv2
    device = capture_device(device)
    # OpenCV hides errno. Check ordinary device access first, without streaming.
    try:
        descriptor = os.open(device, os.O_RDWR | os.O_NONBLOCK)
        os.close(descriptor)
    except OSError as exc:
        reason = {errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied',
                  errno.EBUSY: 'device_busy', errno.ENOENT: 'device_missing',
                  errno.ENODEV: 'device_missing'}.get(exc.errno, 'open_failed')
        raise ProbeError(reason, FAILURES[reason]) from None
    started = time.monotonic()
    capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
    opened = time.monotonic()
    try:
        if not capture.isOpened():
            raise ProbeError('open_failed', FAILURES['open_failed'])
        # USB/IP benefits from a modest stream; no need for 4K recognition input.
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        capture.set(cv2.CAP_PROP_FPS, 15)
        buffer_set = capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        def negotiated(prop):
            value = float(capture.get(prop))
            return value if math.isfinite(value) and 0 <= value <= 2 ** 32 else None
        fourcc = negotiated(cv2.CAP_PROP_FOURCC)
        fourcc = ''.join(chr((int(fourcc) >> (8 * n)) & 255) for n in range(4)) if fourcc else None
        if fourcc and not re.fullmatch(r'[A-Za-z0-9 ]{4}', fourcc):
            fourcc = None
        settings = {'fourcc': fourcc, 'width': negotiated(cv2.CAP_PROP_FRAME_WIDTH),
                    'height': negotiated(cv2.CAP_PROP_FRAME_HEIGHT),
                    'fps': negotiated(cv2.CAP_PROP_FPS),
                    'buffer_size': negotiated(cv2.CAP_PROP_BUFFERSIZE),
                    'buffer_request_accepted': bool(buffer_set)}
        # Drain three decoded frames. This is measured warmup, not proof of
        # driver timestamp freshness (the production service must supply that).
        for _ in range(3):
            ok, frame = capture.read()
            if not ok or frame is None or not frame.size:
                raise ProbeError('no_frame', FAILURES['no_frame'])
            del frame
        warmed = time.monotonic()
        count, shape = 0, None
        for _ in range(frames):
            ok, frame = capture.read()
            if not ok or frame is None or not frame.size:
                raise ProbeError('no_frame', FAILURES['no_frame'])
            count += 1
            shape = list(frame.shape[:2])
            del frame
        finished = time.monotonic()
        result = {'schema': 1, 'state': 'ready', 'reason': 'ok',
                  'frames': count, 'height': shape[0], 'width': shape[1],
                  'negotiated': settings, 'warmup_frames': 3,
                  'open_seconds': round(opened - started, 6),
                  'warmup_seconds': round(warmed - opened, 6),
                  'capture_seconds': round(finished - warmed, 6),
                  'elapsed_seconds': round(finished - started, 6), 'saved_frames': 0}
    finally:
        closing = time.monotonic()
        capture.release()
    result['close_seconds'] = round(time.monotonic() - closing, 6)
    return result


def probe(device, frames=10, timeout=15):
    bounds(frames, timeout)
    capture_device(device)
    # Some V4L2 drivers block in read(). A separate bounded process guarantees
    # the caller can continue and closes the camera descriptor on timeout.
    try:
        result = subprocess.run([sys.executable, '-m', 'aios.camera', '--capture-worker', device,
                                 '--frames', str(frames)], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProbeError('timeout', 'Camera read timed out; check USB/IP attachment and camera format') from None
    if result.returncode:
        try:
            failure = json.loads(result.stdout) if len(result.stdout) <= 4096 else None
        except (ValueError, TypeError):
            failure = None
        if type(failure) is dict and type(failure.get('reason')) is str and failure['reason'] in FAILURES:
            reason = failure['reason']
            raise ProbeError(reason, FAILURES[reason])
        raise RuntimeError('Camera probe failed; check device permissions, USB/IP attachment and format')
    try:
        if len(result.stdout) > 4096:
            raise ValueError()
        report = json.loads(result.stdout)
        validate_report(report, frames)
        return report
    except (ValueError, TypeError, KeyError):
        raise ProbeError('invalid_report', 'Camera worker returned an invalid report') from None


def validate_report(report, frames):
    """Reject unexpected payload fields rather than forwarding worker output."""
    timing = {'open_seconds', 'warmup_seconds', 'capture_seconds', 'elapsed_seconds', 'close_seconds'}
    fields = timing | {'schema', 'state', 'reason', 'frames', 'height', 'width',
                       'negotiated', 'warmup_frames', 'saved_frames'}
    if type(report) is not dict or set(report) != fields:
        raise ValueError('Invalid report fields')
    for key, expected in {'schema': 1, 'frames': frames, 'warmup_frames': 3, 'saved_frames': 0}.items():
        if type(report[key]) is not int or report[key] != expected:
            raise ValueError('Invalid report count')
    if report['state'] != 'ready' or report['reason'] != 'ok':
        raise ValueError('Invalid report state')
    for key in timing:
        if type(report[key]) not in (int, float) or not math.isfinite(report[key]) or not 0 <= report[key] <= 30:
            raise ValueError('Invalid timing')
    for key in ('width', 'height'):
        if type(report[key]) is not int or not 1 <= report[key] <= 8192:
            raise ValueError('Invalid dimensions')
    settings = report['negotiated']
    if type(settings) is not dict or set(settings) != {'fourcc', 'width', 'height', 'fps', 'buffer_size', 'buffer_request_accepted'}:
        raise ValueError('Invalid negotiated settings')
    if settings['fourcc'] is not None and (type(settings['fourcc']) is not str or not re.fullmatch(r'[A-Za-z0-9 ]{4}', settings['fourcc'])):
        raise ValueError('Invalid format')
    if type(settings['buffer_request_accepted']) is not bool:
        raise ValueError('Invalid buffer status')
    for key in ('width', 'height', 'fps', 'buffer_size'):
        value = settings[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 2 ** 32):
            raise ValueError('Invalid negotiated value')


def guest_probe(device, expected_user):
    """Three independent acquisitions under the named ordinary guest account."""
    import pwd
    if os.geteuid() == 0 or pwd.getpwuid(os.geteuid()).pw_name != expected_user:
        raise ProbeError('wrong_user', 'Run the guest probe as the intended unprivileged capture account')
    reports = [probe(device, 10, 15) for _ in range(3)]
    if any((r['width'], r['height']) != (640, 480) for r in reports):
        raise ProbeError('wrong_format', 'Camera did not decode the required 640x480 frames')
    return {'schema': 1, 'state': 'ready', 'reason': 'ok', 'unprivileged': True,
            'captures': reports, 'reopen_seconds': [r['open_seconds'] for r in reports[1:]],
            'saved_frames': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', metavar='/dev/videoN', help='Read and discard frames; activates the camera')
    parser.add_argument('--frames', type=int, default=10)
    parser.add_argument('--guest-probe', metavar='DEVICE', help='Three independent ten-frame captures; activates the camera')
    parser.add_argument('--expected-user', default='aios', help='Required unprivileged guest capture account')
    parser.add_argument('--capture-worker', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if not 1 <= args.frames <= 60:
            raise ValueError('Choose 1–60 frames')
        if args.capture_worker:
            result = capture_probe(args.capture_worker, args.frames)
        elif args.guest_probe:
            result = guest_probe(args.guest_probe, args.expected_user)
        elif args.probe:
            result = probe(args.probe, args.frames)
        else:
            result = {'devices': devices(), 'capture_started': False}
        print(json.dumps(result))
    except Exception as exc:
        if args.capture_worker:
            reason = exc.reason if isinstance(exc, ProbeError) else ('runtime_missing' if isinstance(exc, ImportError) else 'capture_failed')
            print(json.dumps({'reason': reason}))
        else:
            message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else FAILURES['capture_failed']
            print(json.dumps({'error': message, 'reason': getattr(exc, 'reason', 'unavailable')}))
        sys.exit(1)


if __name__ == '__main__':
    main()
