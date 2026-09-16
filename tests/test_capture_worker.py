import ctypes
import unittest
from unittest.mock import Mock, patch

from aios.capture_worker import Acquisition


class CaptureFreshnessTests(unittest.TestCase):
    def capture(self, events):
        capture = Acquisition('/unused', clock=lambda: 100.)
        capture.camera = 1
        capture.storage = (ctypes.c_ubyte * 16)()
        capture.library = Mock()
        capture.cv = Mock()
        frame = Mock()
        frame.shape = (480, 640, 3)
        frame.dtype.name = 'uint8'
        capture.cv.imdecode.return_value = frame
        def read(_handle, _data, _size, stamp, seq):
            timestamp, sequence = next(events)
            stamp._obj.value = timestamp
            seq._obj.value = sequence
            return 1
        capture.library.aios_camera_read.side_effect = read
        return capture

    def test_drains_pre_warmup_old_duplicate_and_future_frames(self):
        capture = self.capture(iter([(98., 1), (99.6, 2), (100.1, 3), (99.8, 4), (99.9, 5)]))
        capture.cutoff = 99.7
        capture.last_capture = 99.8
        capture.driver_sequence = 4
        capture.read()
        self.assertEqual(capture.last_capture, 99.9)
        self.assertEqual(capture.driver_sequence, 5)
        self.assertEqual(capture.sequence, 1)
        self.assertEqual(capture.cv.imdecode.call_count, 1)

    def test_bad_frame_and_driver_error_fail_without_advancing_evidence(self):
        capture = self.capture(iter([(99.9, 1)]))
        capture.cv.imdecode.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'camera_decode_failed'):
            capture.read()
        self.assertEqual(capture.sequence, 0)
        capture.library.aios_camera_read.side_effect = None
        capture.library.aios_camera_read.return_value = -1
        with self.assertRaisesRegex(RuntimeError, 'camera_read_failed'):
            capture.read()

    def test_stale_stream_is_bounded(self):
        capture = self.capture(iter([(98., 1)] * 10))
        capture.clock = Mock(side_effect=[100., 100., 100., 101.])
        with self.assertRaisesRegex(RuntimeError, 'stale_frame'):
            capture.read()
        capture.cv.imdecode.assert_not_called()

    def test_driver_damaged_and_empty_frames_are_discarded_before_fresh_evidence(self):
        capture = self.capture(iter([(99.9, 4)]))
        valid_read = capture.library.aios_camera_read.side_effect
        attempts = iter([-7, -8, 1])
        def read(*args):
            code = next(attempts)
            return valid_read(*args) if code == 1 else code
        capture.library.aios_camera_read.side_effect = read
        capture.read()
        self.assertEqual(capture.sequence, 1)
        self.assertEqual(capture.driver_sequence, 4)
        self.assertEqual(capture.cv.imdecode.call_count, 1)

    def test_damaged_stream_has_an_attempt_bound_even_with_a_frozen_clock(self):
        capture = self.capture(iter([]))
        capture.library.aios_camera_read.side_effect = None
        capture.library.aios_camera_read.return_value = -7
        with self.assertRaisesRegex(RuntimeError, 'stale_frame'):
            capture.read()
        self.assertEqual(capture.library.aios_camera_read.call_count, 16)
        self.assertEqual(capture.sequence, 0)
        capture.cv.imdecode.assert_not_called()

    def test_three_embeddings_must_complete_within_two_seconds(self):
        capture = self.capture(iter([]))
        capture.clock = Mock(side_effect=[100., 100., 102.1, 102.1])
        capture.read = Mock(return_value=object())
        encoder = Mock()
        encoder.encode.return_value = [(None, [1., 0.])]
        with patch('aios.recognition._quality', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'insufficient_frames'):
                capture.embeddings('/unused', encoder, {})

    def guided(self):
        capture = self.capture(iter([]))
        elapsed = [100.]
        def clock():
            elapsed[0] += .05
            return elapsed[0]
        capture.clock = clock
        def read():
            capture.sequence += 1
            capture.last_capture = elapsed[0]
            return object()
        capture.read = read
        return capture

    def test_enrollment_uses_only_ten_forward_snapshots(self):
        import numpy as np
        capture = self.guided()
        capture.cv.imencode.return_value = (True, np.zeros(8, dtype=np.uint8))
        encoder = Mock()
        encoder.encode.return_value = [(None, [1.] + [0.] * 127)]
        events = []
        with patch('aios.recognition._quality', return_value=True), patch('aios.capture_worker.emit', events.append):
            samples = capture.enrollment_embeddings('', encoder, {})
        self.assertEqual(len(samples), 3)
        self.assertEqual(encoder.encode.call_count, 10)
        self.assertTrue(all(not call.kwargs for call in encoder.encode.call_args_list))
        progress = [event for event in events if event['kind'] == 'progress']
        self.assertEqual(progress[-1]['payload'], {'samples': 10, 'target': 10, 'reason': 'burst_capture'})
        self.assertTrue(all(set(item['payload']) == {'samples', 'target', 'reason'} for item in progress))
        self.assertNotIn('embedding', repr(events))

    def test_unusable_burst_fails_without_replacement_frames(self):
        import numpy as np
        for quality in (False, True):
            capture = self.guided()
            capture.cv.imencode.return_value = (True, np.zeros(8, dtype=np.uint8))
            encoder = Mock()
            encoder.encode.return_value = []
            with patch('aios.recognition._quality', return_value=quality), patch('aios.capture_worker.emit'), \
                    self.assertRaisesRegex(RuntimeError, 'insufficient_frames'):
                capture.enrollment_embeddings('', encoder, {})
            self.assertEqual(encoder.encode.call_count, 10 if quality else 0)
            self.assertLess(capture.sequence, 450)
