"""Consented single-person development check. NOT a production calibration.

Run in a local terminal as the ordinary Alpine guest user. Model weights must
already be installed. Face samples stay in one disposable worker's memory;
only anonymous counts/timings can be saved. Never use this report as held-out
or unknown-person evidence. No accounts, PINs, templates or approvals are written.
"""
import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

CALIBRATION = {
    'hardware': 'brio-101', 'id': 'development-only-unvalidated-v1',
    'match_threshold': .6, 'runner_up_margin': .1, 'enrollment_consistency': .6,
    'minimum_brightness': 30, 'maximum_brightness': 225, 'minimum_sharpness': 20,
    'minimum_face_size': 80, 'enrollment_pose_delta': .05, 'maximum_pose_offset': .4,
}
GUIDANCE = {
    'look_straight': 'Look straight at the camera.',
    'turn_slightly': 'Turn slightly to one side.',
    'turn_other_way': 'Turn slightly to the other side.',
    'improve_light_or_hold_still': 'Use even light and hold still.',
    'one_person_only': 'Only one consenting person may be in view.',
    'face_camera': 'Move closer with your whole face in view.',
    'sample_accepted': 'Sample accepted; follow the next instruction.',
}
PROBE_GUIDANCE = ('Look straight at the camera lens.',) * 5
PAUSE_REASONS = {
    'no_usable_burst': 'None of the ten photos was usable. Enrollment was not created.',
    'framing_timeout': 'Positioning time expired. The camera is off.',
    'enrollment_timeout': 'This pose could not be captured. The camera is off.',
    'one_camera_required': 'The camera is missing or more than one camera is connected.',
    'inconsistent': 'The enrollment poses were not consistent enough.',
    'worker_error': 'The capture could not finish.',
}
from aios.capture_worker import ERROR_CODES, Acquisition
PAUSE_REASONS.update({code: 'Camera capture stopped (' + code + ').' for code in ERROR_CODES})
PAUSE_REASONS.update({
    'camera_read_failed': 'The camera stopped providing usable frames.',
    'camera_open_failed': 'The camera could not be opened. Check its connection.',
    'camera_decode_failed': 'The camera returned a damaged image.',
    'camera_poll_timeout': 'The camera did not respond in time.',
    'stale_frame': 'The camera did not provide a fresh frame in time.',
    'camera_driver_frame_error': 'The camera driver returned damaged frames. Check the camera connection.',
})


def pause_reason(error):
    value = str(error)
    return value if type(error) in (RuntimeError, ValueError) and value in PAUSE_REASONS else 'worker_error'


METRIC_COUNTS = ('reads', 'old_frames', 'future_frames', 'timestamp_order', 'sequence_order',
                 'driver_error_frames', 'empty_frames', 'other_read_errors', 'inference_calls',
                 'too_dark', 'too_bright', 'blurred', 'face_not_detected', 'multiple_faces',
                 'face_forward', 'turn_more', 'turn_other_side', 'turn_less', 'invalid_sample', 'inconsistent_sample',
                 'accepted_enrollment_frames', 'enrollment_captured_photos', 'enrollment_usable_photos')
METRIC_TIMES = ('maximum_frame_age', 'maximum_read_seconds', 'maximum_inference_seconds')
POSE_FEEDBACK = {
    'invalid_sample': 'This frame could not be used. Keep your face visible.',
    'inconsistent_sample': 'This frame did not agree with the reference. Hold steady with only you in view.',
    'too_dark': 'The image is too dark. Add light in front of you.',
    'too_bright': 'The image is too bright. Reduce direct light on your face.',
    'blurred': 'The image is blurred. Hold still and check the camera focus.',
    'face_not_detected': 'No usable face detected. Move closer with your whole face in view.',
    'multiple_faces': 'More than one face detected. Only you should be in view.',
    'face_forward': 'The pose check sees your head turned. Look directly at the camera lens.',
    'turn_more': 'The pose check needs a slightly larger head turn. Keep your face visible.',
    'turn_other_side': 'Turn toward the other side from the previous pose.',
    'turn_less': 'The pose check cannot use this angle. Turn back slightly toward the camera.',
}


def frame_feedback(frame, cv):
    gray = cv.cvtColor(frame, cv.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    if brightness < CALIBRATION['minimum_brightness']:
        return 'too_dark'
    if brightness > CALIBRATION['maximum_brightness']:
        return 'too_bright'
    if float(cv.Laplacian(gray, cv.CV_64F).var()) < CALIBRATION['minimum_sharpness']:
        return 'blurred'
    return None


def pose_feedback(pose, poses):
    delta, maximum = CALIBRATION['enrollment_pose_delta'], CALIBRATION['maximum_pose_offset']
    if not math.isfinite(pose) or abs(pose) > maximum:
        return 'turn_less'
    if not poses:
        return 'face_forward' if abs(pose) > delta else None
    if abs(pose - poses[0]) < delta:
        return 'turn_more'
    if len(poses) > 1 and (pose - poses[0]) * (poses[1] - poses[0]) >= 0:
        return 'turn_other_side'
    return None


class CaptureMetrics:
    """Fixed numeric diagnostics only; never retain a frame, vector or device path."""
    def __init__(self):
        self.values = dict.fromkeys(METRIC_COUNTS + METRIC_TIMES, 0)

    def observe(self, capture, native, *args):
        started = time.monotonic()
        count = native(*args)
        values = self.values
        values['reads'] += 1
        values['maximum_read_seconds'] = max(values['maximum_read_seconds'], time.monotonic() - started)
        if count > 0:
            stamp, sequence = args[3]._obj.value, args[4]._obj.value
            age = capture.clock() - stamp
            values['maximum_frame_age'] = max(values['maximum_frame_age'], age)
            if age < 0:
                values['future_frames'] += 1
            elif age > .5:
                values['old_frames'] += 1
            elif stamp <= max(capture.cutoff, capture.last_capture):
                values['timestamp_order'] += 1
            elif capture.driver_sequence is not None and not 0 < (sequence - capture.driver_sequence) % 2**32 < 2**31:
                values['sequence_order'] += 1
        else:
            key = {-7: 'driver_error_frames', -8: 'empty_frames'}.get(count, 'other_read_errors')
            values[key] += 1
        return count

    def emit(self):
        print(json.dumps({'kind': 'capture_metrics', 'payload': {
            key: round(value, 4) if key in METRIC_TIMES else value
            for key, value in self.values.items()}}, allow_nan=False), flush=True)


class TimedEncoder:
    def __init__(self, encoder, metrics):
        self.encoder, self.metrics = encoder, metrics

    def encode(self, *args, **kwargs):
        started = time.monotonic()
        try:
            return self.encoder.encode(*args, **kwargs)
        finally:
            values = self.metrics.values
            values['inference_calls'] += 1
            values['maximum_inference_seconds'] = max(values['maximum_inference_seconds'], time.monotonic() - started)


class PreviewAcquisition(Acquisition):
    """Keep one stream open; discard failed reads without cycling the device."""
    def __init__(self, device, preview):
        super().__init__(device)
        self.preview = preview
        self.metrics = CaptureMetrics()
        self.observed_library = None
        self.next_sample_at = 0

    def read_once(self):
        """A bounded acquisition attempt for a fixed-duration burst."""
        try:
            frame = Acquisition.read(self)
            self.preview.show(frame)
            return frame
        except RuntimeError as error:
            if str(error) not in ERROR_CODES:
                raise
            self.preview.app.processEvents()
            if self.preview.cancelled or not self.preview.window.isVisible():
                raise RuntimeError('preview_cancelled')
            return None

    def read(self):
        if self.camera and self.observed_library is not self.library:
            native = self.library.aios_camera_read
            self.library.aios_camera_read = lambda *args: self.metrics.observe(self, native, *args)
            self.observed_library = self.library
        while True:
            try:
                frame = super().read()
                self.preview.show(frame)
                return frame
            except RuntimeError as error:
                if str(error) not in ERROR_CODES:
                    raise
                self.metrics.emit()
                print(json.dumps({'kind': 'notice', 'reason': pause_reason(error)}), flush=True)
                self.preview.recover('Waiting for a usable camera frame. Keeping the stream open.')


def sample_frame(capture, clock=time.monotonic):
    """Drain/display continuously, but select at most one frame every two seconds."""
    while True:
        frame = capture.read()
        now = clock()
        if now >= capture.next_sample_at:
            capture.next_sample_at = now + 2
            return frame
        del frame


class FramingPreview:
    """Native Qt controls and local preview inside the camera-owning worker."""
    def __init__(self, cv, hint):
        from PySide6 import QtCore, QtGui, QtWidgets
        from aios.core import load_config
        from aios.terminal_theme import palette
        self.cv, self.hint = cv, hint
        self.QtCore, self.QtGui = QtCore, QtGui
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        colors = palette(load_config().get('theme_color', 'blue'))
        _, _, panel, control, muted, accent, line = colors
        self.color = tuple(int(accent.lstrip('#')[i:i + 2], 16) for i in (4, 2, 0))
        self.cancelled = self.next_requested = self.waiting = False
        self.window = QtWidgets.QWidget()
        self.window.setWindowTitle('Recognition setup')
        self.window.setFont(QtGui.QFont('DejaVu Sans', 11))
        self.window.setStyleSheet(
            f'QWidget {{ background: {panel}; color: #f1f5f6; }}'
            f'QPushButton {{ background: {control}; border: 1px solid {line}; padding: 8px 24px; }}'
            f'QPushButton:focus {{ border: 1px solid {accent}; }}'
            f'QPushButton:disabled {{ color: {muted}; }}')
        layout = QtWidgets.QVBoxLayout(self.window)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(8)
        self.heading = QtWidgets.QLabel(hint)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.image = QtWidgets.QLabel()
        self.image.setMinimumSize(1, 1)
        self.image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.image.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Ignored)
        self.image.setAccessibleName('Mirrored camera preview with framing outline')
        layout.addWidget(self.image, 1)
        self.feedback = QtWidgets.QLabel('Position yourself, then choose Next. Nothing advances automatically.')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        buttons = QtWidgets.QHBoxLayout()
        cancel = QtWidgets.QPushButton('&Cancel')
        cancel.clicked.connect(self.cancel)
        buttons.addWidget(cancel)
        buttons.addStretch()
        self.next_button = QtWidgets.QPushButton('&Next')
        self.next_button.setAccessibleName('Next step')
        self.next_button.setEnabled(False)
        self.next_button.clicked.connect(self.advance)
        buttons.addWidget(self.next_button)
        layout.addLayout(buttons)
        self.escape = QtGui.QShortcut(QtGui.QKeySequence('Esc'), self.window)
        self.escape.activated.connect(self.cancel)
        size = self.app.primaryScreen().availableGeometry()
        self.window.resize(min(680, int(size.width() * .7)), min(480, int(size.height() * .7) - 64))
        self.window.move(size.x() + 24, size.y() + 24)
        self.window.show()
        self.app.processEvents()

    def advance(self):
        if self.waiting:
            self.next_requested = True
            self.next_button.setEnabled(False)

    def cancel(self):
        self.cancelled = True

    def show(self, frame):
        display = self.cv.flip(frame, 1)
        self.cv.ellipse(display, (320, 240), (104, 160), 0, 0, 360, self.color, 1)
        rgb = self.cv.cvtColor(display, self.cv.COLOR_BGR2RGB)
        height, width, _ = rgb.shape
        picture = self.QtGui.QImage(rgb.data, width, height, rgb.strides[0],
                                    self.QtGui.QImage.Format.Format_RGB888).copy()
        self.image.setPixmap(self.QtGui.QPixmap.fromImage(picture).scaled(
            self.image.size(), self.QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            self.QtCore.Qt.TransformationMode.SmoothTransformation))
        self.app.processEvents()
        if self.cancelled or not self.window.isVisible():
            raise RuntimeError('preview_cancelled')

    def ready(self, capture, hint=None, timeout=None, notice=''):
        self.waiting = False
        self.next_button.setEnabled(False)
        self.app.processEvents()  # Drain old clicks before arming a new step.
        self.next_requested = False
        self.next_button.setText('&Next')
        if hint:
            self.hint = hint
        self.heading.setText(self.hint)
        self.feedback.setText(notice or 'Position yourself, then choose Next. Nothing advances automatically.')
        self.waiting = True
        self.next_button.setEnabled(True)
        self.next_button.setFocus()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while deadline is None or time.monotonic() < deadline:
                frame = capture.read()
                del frame
                if self.next_requested:
                    self.feedback.setText('Capturing this pose. Hold still...')
                    return
            raise RuntimeError('framing_timeout')
        finally:
            self.waiting = False
            self.next_button.setEnabled(False)

    def recover(self, message):
        """Back off between attempts while keeping cancellation responsive."""
        self.image.clear()
        self.feedback.setText(message + ' Cancel stops the session.')
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self.app.processEvents()
            if self.cancelled or not self.window.isVisible():
                raise RuntimeError('preview_cancelled')
            time.sleep(.03)

    def retry(self, reason, action):
        self.waiting = False
        self.next_button.setEnabled(False)
        self.heading.setText('Recovering capture - no step advanced')
        self.recover(PAUSE_REASONS[reason] + ' Retrying automatically.')

    def close(self):
        self.image.clear()
        self.window.close()
        self.app.processEvents()


ENROLLMENT_FRAMES_PER_POSE = 10


def reference_vector(vectors):
    """Equal-weight mean of unit embeddings; raw images are never retained."""
    unit = [[value / math.sqrt(sum(x*x for x in vector)) for value in vector] for vector in vectors]
    mean = [sum(column) / len(unit) for column in zip(*unit)]
    norm = math.sqrt(sum(value*value for value in mean))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError('inconsistent')
    return [value / norm for value in mean]


def collect_good_frames(capture, encoder, preview, target):
    from aios.recognition import _samples
    vectors = []
    reason = None
    while len(vectors) < target:
        preview.feedback.setText(
            f'Good frames: {len(vectors)} of {target}. ' +
            (POSE_FEEDBACK[reason] if reason else 'Hold this pose. Capturing automatically...'))
        frame = sample_frame(capture)
        faces = None
        try:
            reason = frame_feedback(frame, capture.cv)
            if reason:
                continue
            try:
                faces = encoder.encode(frame)
            except ValueError:
                reason = 'multiple_faces'
                continue
            if len(faces) != 1:
                reason = 'multiple_faces' if len(faces) > 1 else 'face_not_detected'
                continue
            vector = faces[0][1]
            try:
                _samples([vector] * 3)  # Preserve the model's finite 128-D contract.
            except ValueError:
                reason = 'invalid_sample'
                continue
            vectors.append(vector)
            encoder.metrics.values['accepted_enrollment_frames'] += 1
        finally:
            if reason:
                encoder.metrics.values[reason] += 1
            faces = None
            vector = None
            del frame
    encoder.metrics.emit()
    return vectors


def capture_burst(capture, preview, clock=time.monotonic):
    """Ten fixed slots at 2..20 seconds; missing slots never get replacements."""
    from collections import deque
    started = clock()
    recent, snapshots = deque(), []
    while len(snapshots) < 10:
        frame = capture.read_once()
        now = clock()
        if frame is not None:
            recent.append((capture.last_capture, frame))
        while len(snapshots) < 10 and now >= started + 2 * (len(snapshots) + 1):
            due = started + 2 * (len(snapshots) + 1)
            candidates = [image for stamp, image in recent if due - .5 <= stamp <= due]
            snapshots.append(candidates[-1] if candidates else None)
        # Keep only recent frames for the next fixed slot, plus selected shots.
        while recent and recent[0][0] < now - 2:
            recent.popleft()
        preview.feedback.setText(f'Photos: {len(snapshots)} of 10. {max(0, 20 - int(now - started))} seconds remaining.')
    recent.clear()
    return snapshots


def guided_enrollment(capture, encoder, preview):
    from aios.recognition import _samples
    preview.ready(capture, 'Look forward. Next starts 10 photos over 20 seconds.')
    snapshots = capture_burst(capture, preview)
    encoder.metrics.values['enrollment_captured_photos'] = sum(frame is not None for frame in snapshots)
    vectors = []
    for index in range(10):
        preview.feedback.setText(f'Capture complete. Processing photo {index + 1} of 10...')
        preview.app.processEvents()
        if preview.cancelled or not preview.window.isVisible():
            raise RuntimeError('preview_cancelled')
        frame = snapshots[index]
        try:
            if frame is None or frame_feedback(frame, capture.cv):
                continue
            faces = encoder.encode(frame)
            if len(faces) != 1:
                continue
            vector = faces[0][1]
            _samples([vector] * 3)
            vectors.append(vector)
        except ValueError:
            continue
        finally:
            snapshots[index] = None
            frame = None
    encoder.metrics.values['enrollment_usable_photos'] = len(vectors)
    preview.feedback.setText(f'Finished: {len(vectors)} usable photos out of 10. No replacements taken.')
    encoder.metrics.emit()
    if not vectors:
        raise RuntimeError('no_usable_burst')
    reference = reference_vector(vectors)
    vectors.clear()
    return [reference]


def read_consent():
    import readline
    readline.parse_and_bind('"\\C-h": backward-delete-char')
    readline.parse_and_bind('"\\C-?": backward-delete-char')
    readline.set_auto_history(False)
    while True:
        response = input('To consent, type I CONSENT (Enter alone cancels): ').strip()
        if response == 'I CONSENT':
            return True
        if not response or response.casefold() in ('no', 'cancel'):
            return False
        print('That did not match. Nothing was captured. Try again or press Enter to cancel.')


def worker(directory):
    from aios.biometrics import FaceEncoder
    from aios.capture_service import inventory
    from aios.recognition import _consistent, _samples
    from aios.identity import match
    parent = int(os.environ['AIOS_EVALUATION_PARENT'])
    if os.getppid() != parent or ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
        return 1
    lock = json.loads((directory / 'models.lock.json').read_text())
    for name in ('yunet', 'sface'):
        lock[name]['path'] = str(directory / Path(lock[name]['path']).name)
    encoder = FaceEncoder(lock, calibration=CALIBRATION)
    from contextlib import ExitStack
    gallery = None
    probe_index = 0
    preview = FramingPreview(encoder.cv, 'Position your face')
    with ExitStack() as resources:
        resources.callback(preview.close)
        capture = None
        for raw in sys.stdin.buffer:
            if len(raw) > 1024:
                return 1
            command = json.loads(raw)
            if command not in ({'action': 'enroll'}, {'action': 'probe'}):
                return 1
            started = time.monotonic()
            status, matched = 'unavailable', False
            try:
                while True:
                    try:
                        if capture is None:
                            devices = inventory()
                            if len(devices) != 1:
                                raise RuntimeError('one_camera_required')
                            device = next(iter(devices.values()))[0]
                            capture = resources.enter_context(PreviewAcquisition(device, preview))
                        timed_encoder = TimedEncoder(encoder, capture.metrics)
                        if command['action'] == 'enroll':
                            vectors = guided_enrollment(capture, timed_encoder, preview)
                            gallery = {'temporary-person': vectors}
                            status = 'enrolled'
                        else:
                            preview.ready(capture, f'Check {probe_index + 1} of 5: {PROBE_GUIDANCE[min(probe_index, 4)]}')
                            vectors = _samples(collect_good_frames(capture, timed_encoder, preview, 3))
                            if gallery is None:
                                raise ValueError('enrollment_required')
                            matched = all(match(vector, gallery, CALIBRATION['match_threshold'],
                                                CALIBRATION['runner_up_margin']) == 'temporary-person' for vector in vectors)
                            status = 'measured'
                        capture.metrics.emit()
                        break
                    except Exception as error:
                        if isinstance(error, RuntimeError) and str(error) == 'preview_cancelled':
                            raise
                        if command['action'] == 'enroll' and capture is not None:
                            print(json.dumps({'kind': 'notice', 'reason': pause_reason(error)}), flush=True)
                            status = 'unavailable'
                            break  # Never take another enrollment burst automatically.
                        reason = pause_reason(error)
                        print(json.dumps({'kind': 'notice', 'reason': reason}), flush=True)
                        preview.retry(reason, command['action'])
            except RuntimeError as error:
                status = 'cancelled' if str(error) == 'preview_cancelled' else 'unavailable'
            if command['action'] == 'probe':
                probe_index += 1
            print(json.dumps({'kind': 'evaluation', 'status': status, 'matched': matched,
                              'seconds': round(time.monotonic() - started, 4)}, allow_nan=False), flush=True)
            if status == 'cancelled':
                break
    gallery = None
    return 0


def exchange(process, action, timeout, on_notice=None, on_metrics=None):
    process.stdin.write(json.dumps({'action': action}).encode() + b'\n')
    process.stdin.flush()
    deadline = None if timeout is None else time.monotonic() + timeout
    buffer = bytearray()
    last_hint = None
    retries = 0
    while deadline is None or time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], .2 if deadline is None else max(0, min(.2, deadline - time.monotonic())))
        if not ready:
            continue
        data = os.read(process.stdout.fileno(), 4096)
        if not data:
            raise RuntimeError('Evaluation worker exited. No samples were saved.')
        buffer.extend(data)
        if len(buffer) > 8192:
            raise RuntimeError('Invalid evaluation response.')
        while b'\n' in buffer:
            raw, _, rest = buffer.partition(b'\n')
            buffer[:] = rest
            value = json.loads(raw)
            if type(value) is not dict:
                raise RuntimeError('Invalid evaluation response.')
            if value.get('kind') == 'capture_metrics':
                payload = value.get('payload')
                if (set(value) != {'kind', 'payload'} or type(payload) is not dict or
                        set(payload) != set(METRIC_COUNTS + METRIC_TIMES) or
                        any(type(payload[key]) is not int or not 0 <= payload[key] <= 10**9 for key in METRIC_COUNTS) or
                        any(type(payload[key]) not in (int, float) or not math.isfinite(payload[key]) or
                            not 0 <= payload[key] <= 10**9 for key in METRIC_TIMES)):
                    raise RuntimeError('Invalid evaluation response.')
                if on_metrics:
                    on_metrics(payload)
            elif value.get('kind') == 'notice':
                if set(value) != {'kind', 'reason'} or type(value['reason']) is not str or value['reason'] not in PAUSE_REASONS:
                    raise RuntimeError('Invalid evaluation response.')
                retries += 1
                if on_notice:
                    on_notice(value['reason'])
                print('Capture interrupted: ' + PAUSE_REASONS[value['reason']], flush=True)
            elif value.get('kind') == 'progress':
                payload = value.get('payload', {})
                if (set(value) != {'kind', 'sequence', 'captured_at', 'payload'} or
                        type(payload) is not dict or set(payload) != {'reason', 'samples', 'target'} or
                        type(payload['reason']) is not str or payload['reason'] not in GUIDANCE or
                        type(payload['samples']) is not int or not 0 <= payload['samples'] <= 3 or payload['target'] != 3):
                    raise RuntimeError('Invalid evaluation response.')
                hint = GUIDANCE.get(payload.get('reason'))
                if hint and hint != last_hint:
                    print(hint, flush=True)
                    last_hint = hint
            elif (set(value) == {'kind', 'status', 'matched', 'seconds'} and value['kind'] == 'evaluation' and
                  value['status'] in ('enrolled', 'measured', 'unavailable', 'cancelled') and type(value['matched']) is bool and
                  type(value['seconds']) in (int, float) and math.isfinite(value['seconds']) and 0 <= value['seconds'] and (timeout is None or value['seconds'] <= timeout)):
                return {**value, **({'retry_count': retries} if retries else {})}
            else:
                raise RuntimeError('Invalid evaluation response.')
    raise RuntimeError('Capture deadline reached. No samples were saved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('/usr/local/share/aios/face-models'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.geteuid() == 0 or os.environ.get('AIOS_SESSION_SOCKET'):
        raise RuntimeError('Use the ordinary, unprotected guest account.')
    if args.worker:
        return worker(args.directory.resolve())
    if not sys.stdin.isatty():
        raise RuntimeError('Consent must be entered by the participant in a local terminal.')
    print('Single-person development check — NOT a production accuracy study.\n'
          'Only you should be in view. Close other camera previews.\n'
          'The camera will measure your face for temporary enrollment,\n'
          'then five repeat checks. Images and face vectors stay in worker memory\n'
          'and are discarded when it exits. Only anonymous counts/times are saved.\n'
          'No accounts, PINs or approvals change.\n'
          'A mirrored local preview helps you frame your face inside the outline.\n'
          'Look forward and choose Next once: 10 photos over 20 seconds, no replacements.\n'
          'Rejected frames are discarded. Cancel or Esc stops the test.\n'
          'Press Ctrl+C at any time to cancel and erase these temporary samples.\n')
    if not read_consent():
        print('Cancelled. The camera was not opened.')
        return 1
    worker_environment = {**os.environ, 'AIOS_EVALUATION_PARENT': str(os.getpid())}
    worker_environment.pop('AIOS_CAPTURE_DIAGNOSTICS', None)
    events = args.output.with_suffix('.events.jsonl').open('x', encoding='utf-8') if args.output else None
    def record_notice(reason):
        if events:
            events.write(json.dumps({'event': 'capture_interrupted', 'reason': reason}) + '\n')
            events.flush()
    latest_metrics = {}
    def record_metrics(payload):
        latest_metrics.update(payload)
        if events:
            events.write(json.dumps({'event': 'capture_metrics', 'payload': payload}) + '\n')
            events.flush()
    process = subprocess.Popen([sys.executable, __file__, '--worker', '--directory', str(args.directory)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=worker_environment)
    try:
        print('Follow the preview instructions and choose Next for each pose.', flush=True)
        enrolled = exchange(process, 'enroll', None, record_notice, record_metrics)
        if enrolled['status'] != 'enrolled':
            raise RuntimeError('No usable enrollment. Samples will be discarded; the run is incomplete.')
        enrollment_counts = {key: latest_metrics.get(key, 0) for key in ('enrollment_captured_photos', 'enrollment_usable_photos')}
        print(f"Enrollment complete: {enrollment_counts['enrollment_usable_photos']} usable photos from the 10 scheduled shots. No account was created.")
        results = []
        for instruction in PROBE_GUIDANCE:
            print(instruction + ' Choose Next in the preview when ready.', flush=True)
            result = exchange(process, 'probe', None, record_notice, record_metrics)
            if result['status'] == 'cancelled':
                raise RuntimeError('Participant cancelled the preview.')
            results.append(result)
            print('Candidate matched.' if result['matched'] else 'No candidate; this counts as a failed genuine attempt.')
        summary = {**enrollment_counts, 'development_only': True, 'production_approval_created': False,
                   'consent_confirmed': True, 'participants': 1, 'genuine_attempts': len(results),
                   'correct_candidates': sum(value['matched'] for value in results),
                   'enrollment_frames': ENROLLMENT_FRAMES_PER_POSE,
                   'enrollment_views': 'forward_only',
                   'sampling_interval_seconds': 2,
                   'enrollment_capture_seconds': 20,
                   'enrollment_replacements': 0,
                   'enrollment_reference_method': 'mean_of_unit_embeddings',
                   'enrollment_retry_count': enrolled.get('retry_count', 0),
                   'probe_retry_counts': [value.get('retry_count', 0) for value in results],
                   'unavailable_attempts': sum(value['status'] == 'unavailable' for value in results),
                   'attempt_seconds': [value['seconds'] for value in results],
                   'framing_preview': True, 'timings_include_participant_framing': True,
                   'calibration': CALIBRATION, 'unknown_person_tested': False, 'held_out_cohort_tested': False}
        if args.output:
            with args.output.open('x', encoding='utf-8') as output:
                json.dump(summary, output, indent=2, allow_nan=False)
                output.write('\n')
        print(json.dumps(summary, indent=2, allow_nan=False))
        print('Finished. Temporary face samples discarded when this worker closes.\n'
              'This cannot establish production accuracy, spoof resistance or liveness.')
    finally:
        process.kill()
        process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
        if events:
            events.close()
    return 0


if __name__ == '__main__':
    os.umask(0o077)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nCancelled. Temporary face samples discarded.')
        raise SystemExit(1)
    except (OSError, ValueError, RuntimeError):
        print('Evaluation incomplete. Temporary samples discarded; use manual PIN access.')
        raise SystemExit(1)
