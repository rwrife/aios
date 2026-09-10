"""Opt-in, bounded local face suggestions. A match never verifies a PIN."""
import fcntl
import json
import math
import os
from pathlib import Path
import sys
import time

from .biometrics import FaceEncoder
from .camera import video_device
from .chat_profiles import dispatch as profile_dispatch
from .core import data_dir, load_config
from .identity import match
from .secure_store import EncryptedStore, atomic_bytes


SCHEMA = 1
CONSENT_VERSION = 1
TARGET_FRAMES = 3
BURST_SECONDS = 2.0
SUGGESTION_SECONDS = 5.0
MANIFEST = Path('/etc/aios/face-models.json')


class CaptureSchedule:
    """Pure scheduling state used by the shell policy and unit tests."""
    BACKOFF = (2, 5, 15, 60)

    def __init__(self, clock=time.monotonic, cadence=15, cooldown=2):
        self.clock = clock
        self.cadence = cadence
        self.cooldown = cooldown
        self.enabled = False
        self.active = True
        self.running = False
        self.failures = 0
        self.next_capture = float('inf')
        self.last_started = float('-inf')
        self.generation = 0

    def configure(self, enabled):
        self.enabled = bool(enabled)
        self.generation += 1
        self.running = False
        self.failures = 0
        self.next_capture = self.clock() if self.enabled and self.active else float('inf')

    def set_active(self, active):
        self.active = bool(active)
        self.generation += 1
        self.running = False
        self.next_capture = self.clock() if self.enabled and self.active else float('inf')

    def due(self, immediate=False):
        now = self.clock()
        if not self.enabled or not self.active or self.running:
            return False
        if immediate and now - self.last_started < self.cooldown:
            return False
        return immediate or now >= self.next_capture

    def start(self, immediate=False):
        if not self.due(immediate):
            return None
        self.running = True
        self.last_started = self.clock()
        return self.generation

    def finish(self, generation, success):
        if generation != self.generation:
            return
        self.running = False
        now = self.clock()
        if success:
            self.failures = 0
            self.next_capture = now + self.cadence
        else:
            delay = self.BACKOFF[min(self.failures, len(self.BACKOFF) - 1)]
            self.failures += 1
            self.next_capture = now + delay

    def device_added(self):
        self.failures = 0
        if self.enabled and self.active:
            self.next_capture = self.clock()


def _manifest(path=None):
    path = Path(path or os.environ.get('AIOS_FACE_MODEL_MANIFEST', MANIFEST))
    value = json.loads(path.read_text())
    calibration = value.get('calibration')
    if not isinstance(calibration, dict) or calibration.get('hardware') != 'brio-101':
        raise ValueError('Face model calibration is missing for the Brio 101')
    required = ('match_threshold', 'runner_up_margin', 'enrollment_consistency',
                'minimum_brightness', 'maximum_brightness', 'minimum_sharpness')
    if any(type(calibration.get(key)) not in (int, float) or not math.isfinite(calibration[key])
           for key in required):
        raise ValueError('Face model calibration is incomplete')
    if not 0 < calibration['match_threshold'] <= 1 or not 0 < calibration['runner_up_margin'] <= 1:
        raise ValueError('Face model calibration is invalid')
    return value, calibration


def _paths(root=None):
    root = Path(root or data_dir() / 'chat-profiles')
    return root, root / 'recognition-key', root / 'recognition-records'


def _store(root=None):
    root, key_path, records = _paths(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not key_path.exists():
        atomic_bytes(key_path, os.urandom(32))
        key_path.chmod(0o600)
    key = key_path.read_bytes()
    if len(key) != 32:
        raise ValueError('Recognition key is invalid')
    return EncryptedStore(records, key)


def _record_name(owner):
    return 'face-template-' + owner


def revoke(owner=None, root=None):
    root, key_path, records = _paths(root)
    if owner is not None:
        if not key_path.exists():
            return
        store = EncryptedStore(records, key_path.read_bytes())
        store.delete(_record_name(owner))
        return
    if records.exists():
        for path in records.glob('face-template-*.enc'):
            if path.is_file():
                path.unlink()


def _templates(profiles, model_revision, root=None):
    store = _store(root)
    result = {}
    for profile in profiles:
        record = store.get(_record_name(profile['id']))
        if not record:
            continue
        if (record.get('schema') != SCHEMA or record.get('consent') != CONSENT_VERSION or
                record.get('owner') != profile['id'] or record.get('model') != model_revision):
            continue
        samples = record.get('samples')
        if isinstance(samples, list) and samples:
            result[profile['id']] = samples
    return result


def _quality(frame, cv, calibration):
    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    sharpness = float(cv.Laplacian(gray, cv.CV_64F).var())
    return (calibration['minimum_brightness'] <= brightness <= calibration['maximum_brightness'] and
            sharpness >= calibration['minimum_sharpness'])


def capture_embeddings(device, encoder, calibration, frames=TARGET_FRAMES,
                       deadline=BURST_SECONDS, clock=time.monotonic, capture_factory=None):
    import cv2
    video_device(device, stable=True)
    capture_factory = capture_factory or (lambda path: cv2.VideoCapture(path, cv2.CAP_V4L2))
    capture = capture_factory(device)
    started = clock()
    sequence = 0
    embeddings = []
    try:
        if not capture.isOpened():
            raise RuntimeError('Camera is unavailable')
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        capture.set(cv2.CAP_PROP_FPS, 15)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for _ in range(2):
            if clock() - started >= deadline:
                raise RuntimeError('Camera warmup exceeded the capture deadline')
            capture.grab()
        while len(embeddings) < frames and clock() - started < deadline:
            ok, frame = capture.read()
            sequence += 1
            captured_at = clock()
            if not ok or frame is None or not frame.size:
                raise RuntimeError('Camera did not deliver a fresh frame')
            if not _quality(frame, cv2, calibration):
                del frame
                continue
            faces = encoder.encode(frame)
            del frame
            if len(faces) != 1:
                raise ValueError('Show exactly one well-lit face to the camera')
            embeddings.append({'sequence': sequence, 'captured_at': captured_at,
                               'embedding': faces[0][1]})
        if len(embeddings) != frames:
            raise RuntimeError('Camera burst did not produce enough quality frames')
        return embeddings
    finally:
        capture.release()


def _consistent(samples, threshold):
    for index, left in enumerate(samples):
        if index + 1 < len(samples) and match(
                left, {'same': samples[index + 1:]}, threshold, 0) != 'same':
            return False
    return True


def _locked(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / 'recognition-camera.lock').open('a')
    os.chmod(lock.name, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Camera is busy') from None
    return lock


def enroll(owner, pin, root=None, manifest_path=None, capture=None):
    config = load_config()
    if config.get('camera_recognition') is not True:
        raise ValueError('Enable camera recognition before enrollment')
    root, _, _ = _paths(root)
    profile = profile_dispatch({'action': 'verify_profile', 'owner': owner, 'pin': pin}, root)['profile']
    manifest, calibration = _manifest(manifest_path)
    encoder = FaceEncoder(manifest, calibration=calibration)
    with _locked(root):
        samples = [item['embedding'] for item in
                   (capture or capture_embeddings)(config['camera_device'], encoder, calibration)]
    if not _consistent(samples, calibration['enrollment_consistency']):
        raise ValueError('Face samples were inconsistent; try enrollment again')
    _store(root).put(_record_name(profile['id']), {
        'schema': SCHEMA, 'consent': CONSENT_VERSION, 'owner': profile['id'],
        'model': manifest['sface']['revision'], 'samples': samples,
    })
    return {'enrolled': profile['id']}


def recognize(root=None, manifest_path=None, capture=None):
    config = load_config()
    if config.get('camera_recognition') is not True:
        return {'state': 'disabled'}
    device = config.get('camera_device')
    video_device(device, stable=True)
    manifest, calibration = _manifest(manifest_path)
    encoder = FaceEncoder(manifest, calibration=calibration)
    root, _, _ = _paths(root)
    profiles = profile_dispatch({'action': 'profiles'}, root)['profiles']
    templates = _templates(profiles, manifest['sface']['revision'], root)
    if not templates:
        return {'state': 'manual-only', 'reason': 'no-enrollment'}
    with _locked(root):
        samples = [item['embedding'] for item in
                   (capture or capture_embeddings)(device, encoder, calibration)]
    owners = [match(sample, templates, calibration['match_threshold'],
                    calibration['runner_up_margin']) for sample in samples]
    if not owners or any(owner is None or owner != owners[0] for owner in owners):
        return {'state': 'ready', 'suggestion': None, 'reason': 'unknown-or-ambiguous'}
    profile = next((item for item in profiles if item['id'] == owners[0]), None)
    if not profile:
        return {'state': 'ready', 'suggestion': None, 'reason': 'deleted'}
    return {'state': 'ready', 'suggestion': {
        'id': profile['id'], 'name': profile['name'], 'photo': profile.get('photo', ''),
        'confidence': 'candidate', 'expires_in': SUGGESTION_SECONDS,
    }}


def dispatch(request):
    if not isinstance(request, dict) or len(json.dumps(request)) > 65536:
        raise ValueError('Invalid recognition request')
    action = request.get('action')
    if action == 'recognize':
        return recognize()
    if action == 'enroll':
        if request.get('consent') is not True:
            raise ValueError('Confirm local face recognition enrollment')
        return enroll(request.get('owner'), request.get('pin'))
    if action == 'disable':
        revoke()
        return {'state': 'disabled'}
    raise ValueError('Unsupported recognition action')


def main():
    os.umask(0o077)
    try:
        payload = sys.stdin.buffer.readline(65537)
        if len(payload) > 65536:
            raise ValueError('Recognition request is too large')
        result = dispatch(json.loads(payload))
        response = {'ok': True, 'result': result}
    except (ValueError, RuntimeError) as error:
        response = {'ok': False, 'error': str(error)}
    except (OSError, ImportError, json.JSONDecodeError):
        response = {'ok': False, 'error': 'Face recognition is unavailable; use your account PIN.'}
    print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
