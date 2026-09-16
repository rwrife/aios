"""Opt-in, bounded local face suggestions. A match never verifies a PIN."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

from .biometrics import FaceEncoder
from .camera import video_device
from .chat_profiles import dispatch as profile_dispatch, verified_operation
from .core import data_dir, load_config
from .identity import match
from .face_store import FaceStore, NAMESPACE, SCHEMA, account_uuid


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


def _binding(manifest, calibration):
    import re
    models = {}
    for name in ('yunet', 'sface'):
        model = manifest[name]
        if (type(model.get('revision')) is not str or not 1 <= len(model['revision']) <= 128 or
                type(model.get('sha256')) is not str or not re.fullmatch(r'[a-f0-9]{64}', model['sha256'])):
            raise ValueError('Model binding is incomplete')
        models[name] = {key: model[key] for key in ('revision', 'sha256')}
    if type(calibration.get('id')) is not str or not 1 <= len(calibration['id']) <= 128:
        raise ValueError('A versioned calibration profile is required')
    digest = hashlib.sha256(json.dumps(calibration, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {'schema': SCHEMA, 'consent': CONSENT_VERSION, 'models': models,
            'calibration': {'id': calibration['id'], 'sha256': digest}}


def revoke(owner=None, root=None):
    root, _, _ = _paths(root)
    FaceStore(root).revoke(owner)


def _templates(profiles, binding, root=None):
    root, _, _ = _paths(root)
    records = FaceStore(root).compatible({p['id'] for p in profiles}, binding)
    result = {}
    for owner, record in records.items():
        try:
            result[owner] = _samples(record['samples'])
        except (ValueError, KeyError):
            FaceStore(root).revoke(owner)
    return result


def _samples(samples):
    if type(samples) is not list or len(samples) != TARGET_FRAMES:
        raise ValueError('Three valid face samples are required')
    for vector in samples:
        if (type(vector) is not list or len(vector) != 128 or
                any(type(x) not in (int, float) or not math.isfinite(x) for x in vector) or
                not 0 < sum(x*x for x in vector) < float('inf')):
            raise ValueError('Invalid face samples')
    return samples


def _quality(frame, cv, calibration):
    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    sharpness = float(cv.Laplacian(gray, cv.CV_64F).var())
    return (calibration['minimum_brightness'] <= brightness <= calibration['maximum_brightness'] and
            sharpness >= calibration['minimum_sharpness'])


def capture_embeddings(*_args, **_kwargs):
    raise RuntimeError('Camera acquisition requires the desktop capture service')


def _consistent(samples, threshold):
    for index, left in enumerate(samples):
        for right in samples[index + 1:]:
            if match(left, {'same': [right]}, threshold, 0) != 'same':
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


def enroll(owner, pin, root=None, manifest_path=None, capture=None, *, consent=False, namespace=NAMESPACE):
    if consent is not True:
        raise ValueError('Confirm local face recognition enrollment')
    account_uuid(owner)
    config = load_config()
    if config.get('camera_recognition') is not True:
        raise ValueError('Enable camera recognition before enrollment')
    root, _, _ = _paths(root)
    store = FaceStore(root, namespace)
    profile_dispatch({'action': 'verify_profile', 'owner': owner, 'pin': pin}, root)
    epoch, previous = store.snapshot()
    manifest, calibration = _manifest(manifest_path)
    binding = _binding(manifest, calibration)
    encoder = FaceEncoder(manifest, calibration=calibration)
    started = time.monotonic()
    with _locked(root):
        samples = _samples([item['embedding'] for item in
                   (capture or capture_embeddings)(config['camera_device'], encoder, calibration)])
    if time.monotonic() - started > 28:
        raise ValueError('Enrollment timed out')
    if not _consistent(samples, calibration['enrollment_consistency']):
        raise ValueError('Face samples were inconsistent; try enrollment again')
    def commit(profile):
        if time.monotonic() - started > 28:
            raise ValueError('Enrollment timed out')
        now = time.time()
        created = previous.get(owner, {}).get('created', now)
        store.replace(profile['id'], {'binding': binding, 'namespace': NAMESPACE, 'owner': profile['id'],
                      'created': created, 'updated': now, 'samples': samples}, epoch)
        return {'enrolled': profile['id']}
    return verified_operation(owner, pin, root, commit)


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
    templates = _templates(profiles, _binding(manifest, calibration), root)
    if not templates:
        return {'state': 'manual-only', 'reason': 'no-enrollment'}
    with _locked(root):
        samples = _samples([item['embedding'] for item in
                   (capture or capture_embeddings)(device, encoder, calibration)])
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
        return enroll(request.get('owner'), request.get('pin'), consent=True)
    if action == 'disable':
        return {'state': 'disabled'}
    if action == 'purge':
        revoke()
        return {'state': 'purged'}
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
