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
        with self.assertRaisesRegex(RuntimeError, 'unusable_frame'):
            capture.read()
        self.assertEqual(capture.sequence, 0)
        capture.library.aios_camera_read.side_effect = None
        capture.library.aios_camera_read.return_value = -1
        with self.assertRaisesRegex(RuntimeError, 'unusable_frame'):
            capture.read()

    def test_stale_stream_is_bounded(self):
        capture = self.capture(iter([(98., 1)] * 10))
        capture.clock = Mock(side_effect=[100., 100., 100., 101.])
        with self.assertRaisesRegex(RuntimeError, 'stale_frame'):
            capture.read()
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
