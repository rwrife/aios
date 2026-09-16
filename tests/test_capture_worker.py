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

    def test_guided_enrollment_requires_center_and_both_pose_offsets(self):
        capture = self.guided()
        encoder = Mock()
        vector = [1.] + [0.] * 127
        encoder.encode.side_effect = [[(None, vector, pose)] for pose in (.8, 0., 0., .12, .15, -.12)]
        progress = []
        with patch('aios.recognition._quality', return_value=True), patch('aios.capture_worker.emit', progress.append):
            samples = capture.enrollment_embeddings('', encoder,
                {'enrollment_pose_delta': .05, 'maximum_pose_offset': .4})
        self.assertEqual(len(samples), 3)
        self.assertEqual([sample['sequence'] for sample in samples], [2, 4, 6])
        self.assertTrue(all(set(item['payload']) == {'samples', 'target', 'reason'} for item in progress))
        self.assertNotIn('embedding', repr(progress))

    def test_bad_quality_multiple_faces_and_no_pose_variation_time_out(self):
        for scenario in ('quality', 'multiple', 'pose'):
            with self.subTest(scenario=scenario):
                capture = self.guided()
                encoder = Mock()
                if scenario == 'multiple':
                    encoder.encode.side_effect = ValueError('multiple_faces')
                else:
                    encoder.encode.return_value = [(None, [1.] + [0.] * 127, 0.)]
                progress = []
                with patch('aios.recognition._quality', return_value=scenario != 'quality'), \
                        patch('aios.capture_worker.emit', progress.append), self.assertRaisesRegex(RuntimeError, 'timeout'):
                    capture.enrollment_embeddings('', encoder,
                        {'enrollment_pose_delta': .05, 'maximum_pose_offset': .4})
                self.assertLess(progress[-1]['payload']['samples'], 3)

    def test_missing_pose_calibration_never_reads_a_frame(self):
        capture = self.guided()
        with self.assertRaisesRegex(ValueError, 'calibration'):
            capture.enrollment_embeddings('', Mock(), {})
        self.assertEqual(capture.sequence, 0)
