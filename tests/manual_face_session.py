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


class FramingPreview:
    """Local display in the camera-owning worker; no preview IPC or media files."""
    def __init__(self, cv, hint):
        from aios.core import load_config
        from aios.terminal_theme import palette
        self.cv, self.hint = cv, hint
        self.name = 'Recognition framing preview'
        colors = palette(load_config().get('theme_color', 'blue'))
        def bgr(value):
            return tuple(int(value.lstrip('#')[index:index + 2], 16) for index in (4, 2, 0))
        self.color, self.panel, self.ink = bgr(colors[5]), bgr(colors[2]), bgr('#f1f5f6')
        self.key = -1
        self.opened = False

    def show(self, frame):
        created = not self.opened
        if not self.opened:
            self.cv.namedWindow(self.name, self.cv.WINDOW_AUTOSIZE | self.cv.WINDOW_GUI_NORMAL)
            self.opened = True
        display = self.cv.flip(frame, 1)
        try:
            self.cv.ellipse(display, (320, 216), (104, 160), 0, 0, 360, self.color, 1)
            self.cv.rectangle(display, (0, 416), (639, 479), self.panel, -1)
            lines = self.hint.split(' | ', 1)
            for index, line in enumerate(lines):
                self.cv.addText(display, line, (24, 440 + index * 24), 'DejaVu Sans', 12, self.ink)
            self.cv.setWindowTitle(self.name, 'Recognition preview')
            self.cv.imshow(self.name, display)
            if created:
                self.cv.moveWindow(self.name, 24, 24)
            self.key = self.cv.waitKey(30) & 255
            if self.key == 27 or self.cv.getWindowProperty(self.name, self.cv.WND_PROP_VISIBLE) < 1:
                raise RuntimeError('preview_cancelled')
        finally:
            del display

    def ready(self, capture):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            frame = capture.read()
            del frame
            if self.key in (32, 10, 13):
                return
        raise RuntimeError('framing_timeout')

    def close(self):
        if self.opened:
            self.cv.destroyWindow(self.name)
            self.cv.waitKey(1)
            self.opened = False


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
    from aios import capture_worker
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
        preview = None
        original_emit = capture_worker.emit
        try:
            devices = inventory()
            if len(devices) != 1:
                raise RuntimeError('one_camera_required')
            device = next(iter(devices.values()))[0]
            preview = FramingPreview(encoder.cv, ('Center your face' if command['action'] == 'enroll'
                                     else PROBE_GUIDANCE[min(probe_index, 4)]) + ' | Space: start | Esc: cancel')
            class PreviewAcquisition(Acquisition):
                def read(self):
                    frame = super().read()
                    preview.show(frame)
                    return frame
            def progress(event):
                if event.get('kind') == 'progress':
                    payload = event['payload']
                    preview.hint = GUIDANCE[payload['reason']] + f" ({payload['samples']}/3) | Esc: cancel"
                original_emit(event)
            capture_worker.emit = progress
            with PreviewAcquisition(device) as capture:
                preview.ready(capture)
                preview.hint = 'Hold still - measuring | Esc: cancel'
                adapter = capture.enrollment_embeddings if command['action'] == 'enroll' else capture.embeddings
                vectors = _samples([item['embedding'] for item in adapter(device, encoder, CALIBRATION)])
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
        except Exception as error:
            status = 'cancelled' if isinstance(error, RuntimeError) and str(error) == 'preview_cancelled' else 'unavailable'
        finally:
            capture_worker.emit = original_emit
            if preview is not None:
                preview.close()
        if command['action'] == 'probe':
            probe_index += 1
        print(json.dumps({'kind': 'evaluation', 'status': status, 'matched': matched,
                          'seconds': round(time.monotonic() - started, 4)}, allow_nan=False), flush=True)
    gallery = None
    return 0


def exchange(process, action, timeout):
    process.stdin.write(json.dumps({'action': action}).encode() + b'\n')
    process.stdin.flush()
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    last_hint = None
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
            if value.get('kind') == 'progress':
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
                return value
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
          'Click the preview and press Space when ready; Esc closes capture.\n'
          'Press Ctrl+C at any time to cancel and erase these temporary samples.\n')
    if not read_consent():
        print('Cancelled. The camera was not opened.')
        return 1
    worker_environment = {**os.environ, 'AIOS_EVALUATION_PARENT': str(os.getpid())}
    worker_environment.pop('AIOS_CAPTURE_DIAGNOSTICS', None)
    process = subprocess.Popen([sys.executable, __file__, '--worker', '--directory', str(args.directory)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=worker_environment)
    try:
        print('Center your face in the preview, then press Space and follow its instructions.', flush=True)
        enrolled = exchange(process, 'enroll', 80)
        if enrolled['status'] != 'enrolled':
            raise RuntimeError('No usable enrollment. Samples will be discarded; the run is incomplete.')
        print('Temporary enrollment complete. No account was created.')
        results = []
        for instruction in PROBE_GUIDANCE:
            print(instruction + ' Click the preview and press Space when ready.', flush=True)
            result = exchange(process, 'probe', 55)
            if result['status'] == 'cancelled':
                raise RuntimeError('Participant cancelled the preview.')
            results.append(result)
            print('Candidate matched.' if result['matched'] else 'No candidate; this counts as a failed genuine attempt.')
        summary = {'development_only': True, 'production_approval_created': False,
                   'consent_confirmed': True, 'participants': 1, 'genuine_attempts': len(results),
                   'correct_candidates': sum(value['matched'] for value in results),
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
