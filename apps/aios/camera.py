"""Explicit, bounded Linux webcam diagnostic. Never saves or emits raw frames."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time


def video_device(value):
    if not isinstance(value, str) or not re.fullmatch(r'/dev/video[0-9]+', value):
        raise ValueError('Choose a local /dev/videoN device; network streams are not accepted')
    if not Path(value).is_char_device():
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
    import cv2
    device = video_device(device)
    capture = cv2.VideoCapture(device, cv2.CAP_V4L2)
    try:
        if not capture.isOpened():
            raise RuntimeError('Cannot open camera; check permissions and other camera applications')
        # USB/IP benefits from a modest stream; no need for 4K recognition input.
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        capture.set(cv2.CAP_PROP_FPS, 15)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        count, shape = 0, None
        started = time.monotonic()
        for _ in range(frames):
            ok, frame = capture.read()
            if not ok or frame is None or not frame.size:
                raise RuntimeError('Camera did not deliver a frame')
            count += 1
            shape = list(frame.shape[:2])
            del frame
        return {'frames': count, 'height': shape[0], 'width': shape[1],
                'elapsed_seconds': round(time.monotonic() - started, 3), 'saved_frames': 0}
    finally:
        capture.release()


def probe(device, frames=10, timeout=15):
    video_device(device)
    if type(frames) is not int or not 1 <= frames <= 60 or not 1 <= timeout <= 30:
        raise ValueError('Invalid probe bounds')
    # Some V4L2 drivers block in read(). A separate bounded process guarantees
    # the caller can continue and closes the camera descriptor on timeout.
    try:
        result = subprocess.run([sys.executable, '-m', 'aios.camera', '--capture-worker', device,
                                 '--frames', str(frames)], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Camera read timed out; check USB/IP attachment and camera format') from None
    if result.returncode:
        raise RuntimeError('Camera probe failed; check device permissions, USB/IP attachment and format')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', metavar='/dev/videoN', help='Read and discard frames; activates the camera')
    parser.add_argument('--frames', type=int, default=10)
    parser.add_argument('--capture-worker', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if not 1 <= args.frames <= 60:
            raise ValueError('Choose 1–60 frames')
        if args.capture_worker:
            result = capture_probe(args.capture_worker, args.frames)
        elif args.probe:
            result = probe(args.probe, args.frames)
        else:
            result = {'devices': devices(), 'capture_started': False}
        print(json.dumps(result))
    except (ValueError, RuntimeError, ImportError) as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)


if __name__ == '__main__':
    main()
