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
PROBE_GUIDANCE = ('Look straight ahead.', 'Turn slightly left.', 'Turn slightly right.',
                  'Move a little farther away.', 'Return to your usual position.')
PAUSE_REASONS = {
    'framing_timeout': 'Positioning time expired. The camera is off.',
    'enrollment_timeout': 'This pose could not be captured. The camera is off.',
    'one_camera_required': 'The camera is missing or more than one camera is connected.',
    'inconsistent': 'The enrollment poses were not consistent enough.',
    'worker_error': 'The capture could not finish.',
}
from aios.capture_worker import ERROR_CODES
PAUSE_REASONS.update({code: 'Camera capture stopped (' + code + ').' for code in ERROR_CODES})
PAUSE_REASONS.update({
    'camera_read_failed': 'The camera stopped providing usable frames.',
    'camera_open_failed': 'The camera could not be opened. Check its connection.',
    'camera_decode_failed': 'The camera returned a damaged image.',
    'camera_poll_timeout': 'The camera did not respond in time.',
    'stale_frame': 'The camera did not provide a fresh frame in time.',
})


def pause_reason(error):
    value = str(error)
    return value if type(error) in (RuntimeError, ValueError) and value in PAUSE_REASONS else 'worker_error'


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

    def ready(self, capture, hint=None, timeout=120, notice=''):
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
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                frame = capture.read()
                del frame
                if self.next_requested:
                    self.feedback.setText('Capturing this pose. Hold still...')
                    return
            raise RuntimeError('framing_timeout')
        finally:
            self.waiting = False
            self.next_button.setEnabled(False)

    def retry(self, reason, action):
        """Camera context has exited before this UI-only wait; never advance."""
        self.waiting = False
        self.next_button.setEnabled(False)
        self.image.clear()
        self.heading.setText('Capture paused - no step advanced')
        self.feedback.setText(PAUSE_REASONS[reason] + (' Retry restarts enrollment.' if action == 'enroll'
                                                    else ' Retry repeats this check.'))
        self.app.processEvents()
        self.next_requested = False
        self.next_button.setText('&Retry enrollment' if action == 'enroll' else '&Retry check')
        self.waiting = True
        self.next_button.setEnabled(True)
        self.next_button.setFocus()
        try:
            while not self.next_requested:
                self.app.processEvents()
                if self.cancelled or not self.window.isVisible():
                    raise RuntimeError('preview_cancelled')
                time.sleep(.03)
        finally:
            self.waiting = False
            self.next_button.setEnabled(False)

    def close(self):
        self.image.clear()
        self.window.close()
        self.app.processEvents()


def guided_enrollment(capture, encoder, preview):
    from aios.recognition import _quality
    samples, poses = [], []
    delta, maximum = CALIBRATION['enrollment_pose_delta'], CALIBRATION['maximum_pose_offset']
    for index, instruction in enumerate(PROBE_GUIDANCE[:3]):
        deadline = time.monotonic() + 120
        accepted = False
        notice = ''
        while time.monotonic() < deadline:
            preview.ready(capture, f'Enrollment {index + 1} of 3: {instruction}',
                          timeout=max(.1, deadline - time.monotonic()), notice=notice)
            attempt_end = min(deadline, time.monotonic() + 3)
            while time.monotonic() < attempt_end:
                frame = capture.read()
                try:
                    if not _quality(frame, capture.cv, CALIBRATION):
                        continue
                    try:
                        faces = encoder.encode(frame, include_pose=True)
                    except ValueError:
                        continue
                    if len(faces) != 1:
                        continue
                    pose = faces[0][2]
                    valid = math.isfinite(pose) and abs(pose) <= maximum
                    if not poses:
                        valid = valid and abs(pose) <= delta
                    elif len(poses) == 1:
                        valid = valid and abs(pose - poses[0]) >= delta
                    else:
                        valid = valid and (pose - poses[0]) * (poses[1] - poses[0]) < 0 and abs(pose - poses[0]) >= delta
                    if valid:
                        samples.append(faces[0][1])
                        poses.append(pose)
                        accepted = True
                        break
                finally:
                    del frame
            if accepted:
                break
            # Retrying the same pose also requires another explicit Next click.
            notice = 'Pose not captured. Adjust lighting/position, then choose Next to retry.'
        if not accepted:
            raise RuntimeError('enrollment_timeout')
    return samples


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
    from aios.capture_worker import Acquisition
    from aios.recognition import _consistent, _samples
    from aios.identity import match
    parent = int(os.environ['AIOS_EVALUATION_PARENT'])
    if os.getppid() != parent or ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent:
        return 1
    lock = json.loads((directory / 'models.lock.json').read_text())
    for name in ('yunet', 'sface'):
        lock[name]['path'] = str(directory / Path(lock[name]['path']).name)
    encoder = FaceEncoder(lock, calibration=CALIBRATION)
    gallery = None
    probe_index = 0
    for raw in sys.stdin.buffer:
        if len(raw) > 1024:
            return 1
        command = json.loads(raw)
        if command not in ({'action': 'enroll'}, {'action': 'probe'}):
            return 1
        started = time.monotonic()
        status, matched = 'unavailable', False
        preview = FramingPreview(encoder.cv, 'Position your face')
        try:
            while True:
                try:
                    devices = inventory()
                    if len(devices) != 1:
                        raise RuntimeError('one_camera_required')
                    device = next(iter(devices.values()))[0]
                    class PreviewAcquisition(Acquisition):
                        def read(self):
                            frame = super().read()
                            preview.show(frame)
                            return frame
                    with PreviewAcquisition(device) as capture:
                        if command['action'] == 'enroll':
                            vectors = _samples(guided_enrollment(capture, encoder, preview))
                        else:
                            preview.ready(capture, f'Check {probe_index + 1} of 5: {PROBE_GUIDANCE[min(probe_index, 4)]}')
                            vectors = _samples([item['embedding'] for item in capture.embeddings(device, encoder, CALIBRATION)])
                    if command['action'] == 'enroll':
                        if not _consistent(vectors, CALIBRATION['enrollment_consistency']):
                            raise ValueError('inconsistent')
                        gallery = {'temporary-person': vectors}
                        status = 'enrolled'
                    elif gallery:
                        matches = [match(vector, gallery, CALIBRATION['match_threshold'], CALIBRATION['runner_up_margin'])
                                   for vector in vectors]
                        matched = all(value == 'temporary-person' for value in matches)
                        status = 'measured'
                    else:
                        raise ValueError('enrollment_required')
                    break
                except Exception as error:
                    if isinstance(error, RuntimeError) and str(error) == 'preview_cancelled':
                        raise
                    reason = pause_reason(error)
                    print(json.dumps({'kind': 'notice', 'reason': reason}), flush=True)
                    preview.retry(reason, command['action'])
        except RuntimeError as error:
            status = 'cancelled' if str(error) == 'preview_cancelled' else 'unavailable'
        finally:
            preview.close()
        if command['action'] == 'probe':
            probe_index += 1
        print(json.dumps({'kind': 'evaluation', 'status': status, 'matched': matched,
                          'seconds': round(time.monotonic() - started, 4)}, allow_nan=False), flush=True)
    gallery = None
    return 0


def exchange(process, action, timeout, on_notice=None):
    process.stdin.write(json.dumps({'action': action}).encode() + b'\n')
    process.stdin.flush()
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    last_hint = None
    retries = 0
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], max(0, min(.2, deadline - time.monotonic())))
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
            if value.get('kind') == 'notice':
                if set(value) != {'kind', 'reason'} or type(value['reason']) is not str or value['reason'] not in PAUSE_REASONS:
                    raise RuntimeError('Invalid evaluation response.')
                retries += 1
                if retries > 128:
                    raise RuntimeError('Too many failed capture attempts.')
                if on_notice:
                    on_notice(value['reason'])
                print('Capture paused: ' + PAUSE_REASONS[value['reason']], flush=True)
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
                  type(value['seconds']) in (int, float) and math.isfinite(value['seconds']) and 0 <= value['seconds'] <= timeout):
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
          'Each pose waits for the Next button. Cancel or Esc stops the test.\n'
          'Press Ctrl+C at any time to cancel and erase these temporary samples.\n')
    if not read_consent():
        print('Cancelled. The camera was not opened.')
        return 1
    worker_environment = {**os.environ, 'AIOS_EVALUATION_PARENT': str(os.getpid())}
    worker_environment.pop('AIOS_CAPTURE_DIAGNOSTICS', None)
    events = args.output.with_suffix('.events.jsonl').open('x', encoding='utf-8') if args.output else None
    def record_notice(reason):
        if events:
            events.write(json.dumps({'event': 'capture_paused', 'reason': reason}) + '\n')
            events.flush()
    process = subprocess.Popen([sys.executable, __file__, '--worker', '--directory', str(args.directory)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=worker_environment)
    try:
        print('Follow the preview instructions and choose Next for each pose.', flush=True)
        enrolled = exchange(process, 'enroll', 7200, record_notice)
        if enrolled['status'] != 'enrolled':
            raise RuntimeError('No usable enrollment. Samples will be discarded; the run is incomplete.')
        print('Temporary enrollment complete. No account was created.')
        results = []
        for instruction in PROBE_GUIDANCE:
            print(instruction + ' Choose Next in the preview when ready.', flush=True)
            result = exchange(process, 'probe', 7200, record_notice)
            if result['status'] == 'cancelled':
                raise RuntimeError('Participant cancelled the preview.')
            results.append(result)
            print('Candidate matched.' if result['matched'] else 'No candidate; this counts as a failed genuine attempt.')
        summary = {'development_only': True, 'production_approval_created': False,
                   'consent_confirmed': True, 'participants': 1, 'genuine_attempts': len(results),
                   'correct_candidates': sum(value['matched'] for value in results),
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
