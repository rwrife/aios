"""Checks the human-consent boundary and aggregate-only development protocol."""
import contextlib
import io
import json
import subprocess
import os
import select
import sys
import unittest
from unittest.mock import patch, MagicMock

import manual_face_session as session


@unittest.skipUnless(sys.platform == 'linux', 'local Linux evaluation terminal')
class EvaluationSessionTests(unittest.TestCase):
    def test_pose_feedback_distinguishes_direction_and_amount(self):
        self.assertIsNone(session.pose_feedback(0., []))
        self.assertEqual(session.pose_feedback(.1, []), 'face_forward')
        self.assertEqual(session.pose_feedback(.01, [0.]), 'turn_more')
        self.assertEqual(session.pose_feedback(.1, [0., .1]), 'turn_other_side')
        self.assertEqual(session.pose_feedback(.5, [0.]), 'turn_less')
        self.assertIsNone(session.pose_feedback(-.1, [0., .1]))

    def test_lighting_feedback_only_follows_measured_quality_failure(self):
        cv = MagicMock()
        cv.Laplacian.return_value.var.return_value = 100.
        for brightness, expected in ((10., 'too_dark'), (250., 'too_bright'), (120., None)):
            cv.cvtColor.return_value.mean.return_value = brightness
            self.assertEqual(session.frame_feedback(object(), cv), expected)
        cv.Laplacian.return_value.var.return_value = 1.
        self.assertEqual(session.frame_feedback(object(), cv), 'blurred')

    def test_metrics_distinguish_rejections_without_changing_native_result(self):
        import ctypes
        metrics = session.CaptureMetrics()
        capture = MagicMock(cutoff=9., last_capture=9., driver_sequence=10)
        capture.clock.return_value = 10.
        def read(stamp, sequence, count=100):
            return metrics.observe(capture, lambda *args: count, None, None, 1000,
                                   ctypes.byref(ctypes.c_double(stamp)), ctypes.byref(ctypes.c_uint32(sequence)))
        self.assertEqual(read(9.4, 11), 100)
        read(10.1, 11)
        capture.last_capture = 9.9
        read(9.8, 11)
        read(9.95, 10)
        read(9.95, 11)
        self.assertEqual(read(0., 0, -7), -7)
        read(0., 0, -8)
        read(0., 0, -2)
        for key in ('old_frames', 'future_frames', 'timestamp_order', 'sequence_order',
                    'driver_error_frames', 'empty_frames', 'other_read_errors'):
            self.assertEqual(metrics.values[key], 1, key)
        self.assertEqual(metrics.values['reads'], 8)
        self.assertAlmostEqual(metrics.values['maximum_frame_age'], .6)

    def test_inference_timing_survives_failure_without_logging_exception(self):
        metrics = session.CaptureMetrics()
        encoder = MagicMock()
        encoder.encode.side_effect = ValueError('private data')
        with patch.object(session.time, 'monotonic', side_effect=[10., 12.]):
            with self.assertRaises(ValueError):
                session.TimedEncoder(encoder, metrics).encode(object())
        self.assertEqual(metrics.values['inference_calls'], 1)
        self.assertEqual(metrics.values['maximum_inference_seconds'], 2.)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            metrics.emit()
        self.assertNotIn('private', output.getvalue())

    def test_metrics_protocol_rejects_extra_data_and_invalid_numbers(self):
        values = session.CaptureMetrics().values
        for change in ({'image': 'private'}, {'reads': True}, {'old_frames': -1},
                       {'maximum_frame_age': float('nan')}, {'maximum_read_seconds': 'private'}):
            with self.assertRaises(RuntimeError):
                self.exchange({'kind': 'capture_metrics', 'payload': {**values, **change}})

    def test_stale_stream_is_closed_and_reopened_once_without_advancing(self):
        preview = MagicMock()
        capture = session.PreviewAcquisition('/unused', preview)
        capture.cutoff, capture.last_capture, capture.driver_sequence = 90., 99., 100
        frame = object()
        order = []
        with patch.object(session.Acquisition, 'read', side_effect=[RuntimeError('stale_frame'), frame]), \
                patch.object(session.Acquisition, '__exit__', side_effect=lambda: order.append('closed')), \
                patch.object(session.Acquisition, '__enter__', side_effect=lambda: order.append('opened')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(capture.read(), frame)
        self.assertEqual(order, ['closed', 'opened'])
        self.assertEqual(capture.stream_restarts, 1)
        self.assertIsNone(capture.driver_sequence)
        preview.advance.assert_not_called()
        preview.show.assert_called_once_with(frame)

    def test_failed_stream_recovery_is_bounded(self):
        capture = session.PreviewAcquisition('/unused', MagicMock())
        with patch.object(session.Acquisition, 'read', side_effect=RuntimeError('stale_frame')), \
                patch.object(session.Acquisition, '__exit__') as close, \
                patch.object(session.Acquisition, '__enter__') as reopen, \
                contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, 'stale_frame'):
                    capture.read()
            self.assertEqual(close.call_count, 1)
            self.assertEqual(reopen.call_count, 1)
        capture.preview.show.assert_not_called()

    def test_pause_clears_preview_and_waits_for_explicit_retry(self):
        preview = session.FramingPreview.__new__(session.FramingPreview)
        preview.app, preview.image, preview.heading = MagicMock(), MagicMock(), MagicMock()
        preview.feedback, preview.next_button, preview.window = MagicMock(), MagicMock(), MagicMock()
        preview.cancelled = False
        iterations = []
        def event_loop():
            iterations.append(True)
            if len(iterations) == 4:
                preview.advance()
        preview.app.processEvents.side_effect = event_loop
        with patch.object(session.time, 'sleep'):
            preview.retry('camera_read_failed', 'enroll')
        self.assertEqual(len(iterations), 4)
        preview.image.clear.assert_called_once()
        preview.heading.setText.assert_called_once_with('Capture paused - no step advanced')
        self.assertFalse(preview.waiting)

    def test_arbitrary_failure_details_are_not_exposed(self):
        self.assertEqual(session.pause_reason(ValueError('private embedding data')), 'worker_error')
        with self.assertRaises(RuntimeError):
            self.exchange({'kind': 'notice', 'reason': 'private embedding data'})

    def test_retry_notice_is_recorded_and_counted(self):
        metrics = session.CaptureMetrics().values
        replies = [{'kind': 'capture_metrics', 'payload': metrics},
                   {'kind': 'notice', 'reason': 'camera_read_failed'},
                   {'kind': 'evaluation', 'status': 'measured', 'matched': True, 'seconds': .1}]
        child = subprocess.Popen([sys.executable, '-c',
            'import sys; sys.stdin.readline(); print(sys.argv[1], flush=True)',
            '\n'.join(json.dumps(reply) for reply in replies)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        notices = []
        diagnostics = []
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = session.exchange(child, 'probe', 2, notices.append, diagnostics.append)
            self.assertEqual(notices, ['camera_read_failed'])
            self.assertEqual(diagnostics, [metrics])
            self.assertEqual(result['retry_count'], 1)
        finally:
            child.wait(timeout=3)
            child.stdin.close()
            child.stdout.close()

    def test_next_is_only_accepted_while_waiting(self):
        preview = session.FramingPreview.__new__(session.FramingPreview)
        preview.next_button = MagicMock()
        preview.next_requested = False
        preview.waiting = False
        preview.advance()
        self.assertFalse(preview.next_requested)
        preview.waiting = True
        preview.advance()
        self.assertTrue(preview.next_requested)
        preview.next_button.setEnabled.assert_called_once_with(False)

    def test_each_enrollment_pose_requires_a_separate_next(self):
        events = []
        capture, encoder, preview = MagicMock(), MagicMock(), MagicMock()
        preview.ready.side_effect = lambda *args, **kwargs: events.append('next')
        poses = iter((0., .1, -.1))
        def encode(*args, **kwargs):
            events.append('sample')
            return [(None, [1.] * 128, next(poses))]
        encoder.encode.side_effect = encode
        with patch.object(session, 'frame_feedback', return_value=None):
            samples = session.guided_enrollment(capture, encoder, preview)
        self.assertEqual(len(samples), 3)
        self.assertEqual(events, ['next', 'sample'] * 3)
        self.assertEqual([call.args[1] for call in preview.ready.call_args_list],
                         ['Enrollment 1 of 3: Look straight ahead.',
                          'Enrollment 2 of 3: Turn slightly left.',
                          'Enrollment 3 of 3: Turn slightly right.'])

    def test_mistyped_consent_can_be_retried_or_cancelled(self):
        with patch('builtins.input', side_effect=['I CONSET', '']), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(session.read_consent())

    def test_failed_pose_requires_next_again_without_skipping_step(self):
        import itertools
        capture, encoder, preview = MagicMock(), MagicMock(), MagicMock()
        poses = iter((0., 0., 0., .1, -.1))
        encoder.encode.side_effect = lambda *args, **kwargs: [(None, [1.] * 128, next(poses))]
        with patch.object(session, 'frame_feedback', return_value=None), \
                patch.object(session.time, 'monotonic', side_effect=itertools.count()):
            self.assertEqual(len(session.guided_enrollment(capture, encoder, preview)), 3)
        steps = [call.args[1] for call in preview.ready.call_args_list]
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[1], steps[2])
        self.assertIn('not captured', preview.ready.call_args_list[2].kwargs['notice'])

    def test_both_backspace_encodings_edit_the_local_prompt(self):
        import pty
        import time
        for backspace in (b'\x08', b'\x7f'):
            master, slave = pty.openpty()
            child = subprocess.Popen([sys.executable, '-c',
                'import sys; sys.path.insert(0,"tests"); from manual_face_session import read_consent; '
                'print("EDIT_RESULT", read_consent())'], stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            output = b''
            try:
                deadline = time.monotonic() + 3
                while b'cancels): ' not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        output += os.read(master, 4096)
                self.assertIn(b'cancels): ', output)
                # Isolated prompt only: no camera, worker, or participant consent.
                os.write(master, b'I CONSENX' + backspace + b'T\n')
                while b'EDIT_RESULT True' not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            output += os.read(master, 4096)
                        except OSError:
                            break
                self.assertIn(b'EDIT_RESULT True', output)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
                os.close(master)

    def test_declining_consent_never_starts_worker(self):
        with patch.object(sys, 'argv', ['manual_face_session']), \
                patch.object(session.os, 'geteuid', return_value=1000), \
                patch.dict(session.os.environ, {}, clear=True), \
                patch.object(sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value='no'), \
                patch.object(session.subprocess, 'Popen') as start, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.main(), 1)
            start.assert_not_called()

    def exchange(self, response):
        child = subprocess.Popen([sys.executable, '-c',
                                  'import sys; sys.stdin.readline(); print(sys.argv[1], flush=True)',
                                  json.dumps(response)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            return session.exchange(child, 'probe', 2)
        finally:
            child.wait(timeout=3)
            child.stdin.close()
            child.stdout.close()

    def test_only_aggregate_result_accepted(self):
        value = {'kind': 'evaluation', 'status': 'measured', 'matched': True, 'seconds': .1}
        self.assertEqual(self.exchange(value), value)
        for addition in ({'embedding': [1.]}, {'image': 'data:image/png;base64,AAAA'}):
            with self.assertRaises(RuntimeError):
                self.exchange({**value, **addition})

    def test_nonfinite_or_nonboolean_results_rejected(self):
        value = {'kind': 'evaluation', 'status': 'measured', 'matched': True, 'seconds': .1}
        for change in ({'seconds': float('nan')}, {'seconds': -1}, {'matched': 1}):
            with self.assertRaises(RuntimeError):
                self.exchange({**value, **change})


if __name__ == '__main__':
    unittest.main()
