"""Private acquisition adapter used only by the desktop capture service.

One worker owns one V4L2 handle. It is disposable because drivers can block in
open/read/release. Raw frames never leave this process, except explicit bounded
preview/profile-photo encodings. No media is written to disk.
"""
import base64
import json
import sys
import time
import os
import signal
import ctypes

from .camera import video_device

MAX_EVENT = 262144
ERROR_CODES = frozenset({'camera_open_failed', 'camera_read_failed', 'camera_decode_failed',
                        'stale_frame', 'timeout', 'ambiguous', 'insufficient_frames',
                        'multiple_faces', 'parent_unavailable', 'worker_error'})
READ_ERRORS = {-2: 'camera_poll_timeout', -3: 'camera_poll_failed', -4: 'camera_dequeue_failed',
               -5: 'camera_metadata_invalid', -6: 'camera_requeue_failed',
               -7: 'camera_driver_frame_error', -8: 'camera_empty_frame'}
ERROR_CODES = ERROR_CODES | frozenset(READ_ERRORS.values())


def emit(value):
    data = json.dumps(value, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(data) > MAX_EVENT:
        raise ValueError('oversized')
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


class Acquisition:
    def __init__(self, device, clock=time.monotonic):
        self.device = device
        self.clock = clock
        self.camera = None
        self.sequence = 0
        self.last_capture = 0
        self.cutoff = 0
        self.driver_sequence = None

    def __enter__(self):
        import cv2
        self.cv = cv2
        video_device(self.device, stable=True)
        self.library = ctypes.CDLL(os.environ.get('AIOS_CAPTURE_LIBRARY', '/usr/local/lib/libaios-camera.so'))
        self.library.aios_camera_open.argtypes = [ctypes.c_char_p]
        self.library.aios_camera_open.restype = ctypes.c_void_p
        self.library.aios_camera_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                                ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_uint32)]
        self.library.aios_camera_read.restype = ctypes.c_int
        self.library.aios_camera_close.argtypes = [ctypes.c_void_p]
        self.library.aios_camera_close.restype = None
        self.storage = (ctypes.c_ubyte * 1048576)()
        self.camera = self.library.aios_camera_open(self.device.encode())
        try:
            if not self.camera:
                raise RuntimeError('camera_open_failed')
            for _ in range(3):
                frame = self.read()
                del frame
            self.cutoff = self.clock()
            self.sequence = 0
            return self
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_):
        self.library.aios_camera_close(self.camera)
        self.camera = None

    def read(self):
        import numpy as np
        # Drain old queued buffers without accepting them as new evidence.
        deadline = self.clock() + .75
        discarded = {'driver_error_frames': 0, 'empty_frames': 0}
        attempts = 0
        while self.clock() < deadline and attempts < 16:
            attempts += 1
            timestamp, sequence = ctypes.c_double(), ctypes.c_uint32()
            count = self.library.aios_camera_read(self.camera, self.storage, len(self.storage),
                                                  ctypes.byref(timestamp), ctypes.byref(sequence))
            if count in (-7, -8):
                discarded['driver_error_frames' if count == -7 else 'empty_frames'] += 1
                continue
            if count <= 0 or count > len(self.storage):
                raise RuntimeError(READ_ERRORS.get(count, 'camera_read_failed'))
            now = self.clock()
            if not 0 <= now - timestamp.value <= .5:
                continue
            if timestamp.value <= self.cutoff or timestamp.value <= self.last_capture:
                continue
            if self.driver_sequence is not None and not 0 < (sequence.value - self.driver_sequence) % 2**32 < 2**31:
                continue
            break
        else:
            self.diagnostic(discarded)
            raise RuntimeError('stale_frame')
        self.diagnostic(discarded)
        frame = self.cv.imdecode(np.frombuffer(self.storage, dtype=np.uint8, count=count), self.cv.IMREAD_COLOR)
        if frame is None or frame.shape != (480, 640, 3) or frame.dtype.name != 'uint8':
            raise RuntimeError('camera_decode_failed')
        self.sequence += 1
        self.last_capture = timestamp.value
        self.driver_sequence = sequence.value
        return frame

    @staticmethod
    def diagnostic(discarded):
        if os.environ.get('AIOS_CAPTURE_DIAGNOSTICS') == '1' and any(discarded.values()):
            emit({'kind': 'diagnostic', 'sequence': 0, 'captured_at': 0, 'payload': discarded})

    def embeddings(self, _device, encoder, calibration):
        from .recognition import _quality
        samples = []
        deadline = self.clock() + 2
        while len(samples) < 3 and self.clock() < deadline:
            frame = self.read()
            try:
                if not _quality(frame, self.cv, calibration):
                    continue
                faces = encoder.encode(frame)
                if len(faces) != 1:
                    raise ValueError('ambiguous')
                if self.clock() >= deadline:
                    break
                samples.append({'sequence': self.sequence, 'captured_at': self.last_capture,
                                'embedding': faces[0][1]})
            finally:
                del frame
        if len(samples) != 3:
            raise RuntimeError('insufficient_frames')
        return samples

    def image(self, photo=False):
        frame = self.read()
        try:
            if photo:
                # Match the existing centered 64px portrait, independent of templates.
                frame = self.cv.resize(frame[:, 80:560], (64, 64))
                rgb = self.cv.cvtColor(frame, self.cv.COLOR_BGR2RGB).tobytes()
                ok, encoded = self.cv.imencode('.png', frame)
                kind = 'png'
            else:
                ok, encoded = self.cv.imencode('.jpg', frame, [self.cv.IMWRITE_JPEG_QUALITY, 70])
                kind = 'jpeg'
            if not ok or encoded.nbytes > 180000:
                raise ValueError('oversized')
            result = {'image': 'data:image/' + kind + ';base64,' + base64.b64encode(encoded).decode()}
            if photo:
                result['rgb'] = base64.b64encode(rgb).decode()
            return result
        finally:
            del frame

    def enrollment_embeddings(self, _device, encoder, calibration):
        """Fixed forward-facing burst; never replace unusable scheduled photos."""
        from collections import deque
        from .recognition import _quality, _samples
        import math
        started = self.clock()
        recent, shots = deque(), []
        while len(shots) < 10:
            try:
                frame = self.read()
            except RuntimeError as error:
                if str(error) not in ERROR_CODES:
                    raise
                frame = None
            now = self.clock()
            if frame is not None:
                recent.append((self.last_capture, self.sequence, frame))
            progressed = False
            while len(shots) < 10 and now >= started + 2 * (len(shots) + 1):
                due = started + 2 * (len(shots) + 1)
                eligible = [item for item in recent if due - .5 <= item[0] <= due]
                shots.append(eligible[-1] if eligible else None)
                progressed = True
            while recent and recent[0][0] < now - 2:
                recent.popleft()
            if frame is not None:
                if progressed:
                    emit({'kind': 'progress', 'sequence': self.sequence, 'captured_at': self.last_capture,
                          'payload': {'samples': len(shots), 'target': 10, 'reason': 'burst_capture'}})
                else:
                    ok, encoded = self.cv.imencode('.jpg', frame, [self.cv.IMWRITE_JPEG_QUALITY, 70])
                    if ok and encoded.nbytes <= 180000:
                        emit({'kind': 'preview', 'sequence': self.sequence, 'captured_at': self.last_capture,
                              'payload': {'image': 'data:image/jpeg;base64,' + base64.b64encode(encoded).decode()}})
            frame = None
        recent.clear()
        accepted = []
        for index, item in enumerate(shots):
            try:
                if item is None or not _quality(item[2], self.cv, calibration):
                    continue
                faces = encoder.encode(item[2])
                if len(faces) != 1:
                    continue
                vector = _samples([faces[0][1]] * 3)[0]
                norm = math.sqrt(sum(x*x for x in vector))
                accepted.append({'sequence': item[1], 'captured_at': item[0],
                                 'embedding': [x / norm for x in vector]})
            except ValueError:
                continue
            finally:
                shots[index] = None
        if len(accepted) < 3:
            raise RuntimeError('insufficient_frames')
        # Preserve the three-reference storage contract, using disjoint groups
        # from this burst only. No sample is duplicated to fill a missing group.
        references = []
        for offset in range(3):
            group = accepted[offset::3]
            mean = [sum(item['embedding'][i] for item in group) / len(group) for i in range(128)]
            norm = math.sqrt(sum(x*x for x in mean))
            if not math.isfinite(norm) or norm <= 0:
                raise ValueError('inconsistent')
            references.append({**group[-1], 'embedding': [x / norm for x in mean]})
        return references


def run(request):
    from . import recognition
    mode = request['mode']
    if mode == 'purge':
        recognition.revoke()
        emit({'kind': 'result', 'sequence': 0, 'captured_at': 0, 'payload': {'state': 'purged'}})
        return
    if mode in ('recognize', 'enroll'):
        metadata = {'sequence': 0, 'captured_at': 0}
        def embeddings(device, encoder, calibration):
            with Acquisition(request['device']) as capture:
                adapter = capture.enrollment_embeddings if mode == 'enroll' else capture.embeddings
                result = adapter(device, encoder, calibration)
                metadata.update(sequence=capture.sequence, captured_at=capture.last_capture)
                return result
        if mode == 'recognize':
            result = recognition.recognize(capture=embeddings)
        else:
            if request.get('consent') is not True:
                raise ValueError('consent_required')
            result = recognition.enroll(request['owner'], request['pin'], capture=embeddings, consent=True)
        if mode == 'recognize' and result.get('suggestion'):
            result['suggestion'].pop('expires_in', None)
            result['suggestion']['expires_at'] = metadata['captured_at'] + 5
        emit({'kind': 'result', **metadata, 'payload': result})
        return
    with Acquisition(request['device']) as capture:
        if mode == 'preview':
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                payload = capture.image()
                emit({'kind': 'preview', 'sequence': capture.sequence,
                      'captured_at': capture.last_capture, 'payload': payload})
                time.sleep(.2)
            emit({'kind': 'result', 'sequence': capture.sequence + 1,
                  'captured_at': capture.last_capture, 'payload': {'state': 'manual-only'}})
        elif mode == 'photo':
            payload = capture.image(photo=True)
            emit({'kind': 'photo', 'sequence': capture.sequence,
                  'captured_at': capture.last_capture, 'payload': payload})


def main():
    try:
        # The kernel kills acquisition even when the service itself is SIGKILLed.
        parent = int(os.environ['AIOS_CAPTURE_PARENT_PID'])
        if os.getppid() != parent or ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            raise RuntimeError('parent_unavailable')
        raw = sys.stdin.buffer.readline(4097)
        if len(raw) > 4096:
            raise ValueError('oversized')
        run(json.loads(raw))
    except Exception as error:
        # Never expose driver/model exceptions, paths, PINs or biometric values.
        code = str(error) if type(error) in (RuntimeError, ValueError) else 'worker_error'
        if code not in ERROR_CODES:
            code = 'worker_error'
        emit({'kind': 'error', 'sequence': 0, 'captured_at': 0, 'payload': {'code': code}})
        raise SystemExit(1)


if __name__ == '__main__':
    main()
