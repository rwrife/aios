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
    def test_enrollment_session_does_not_launch_five_followup_checks(self):
        result = {'status': 'enrolled', 'seconds': 21., 'matched': False}
        with patch.object(sys, 'argv', ['manual_face_session']), \
                patch.object(session.os, 'geteuid', return_value=1000), \
                patch.dict(os.environ, {}, clear=True), patch.object(sys.stdin, 'isatty', return_value=True), \
                patch.object(session, 'read_consent', return_value=True), \
                patch.object(session.subprocess, 'Popen'), patch.object(session, 'exchange', return_value=result) as exchange, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(session.main(), 0)
        self.assertEqual(exchange.call_count, 1)
        self.assertEqual(exchange.call_args.args[1], 'enroll')
    def test_worker_owns_one_stream_across_enrollment_and_all_checks(self):
        import tempfile
        from pathlib import Path
        capture = MagicMock()
        capture.__enter__.return_value = capture
        preview = MagicMock()
        commands = [{'action': 'enroll'}] + [{'action': 'probe'}] * 5
        terminal = MagicMock(buffer=io.BytesIO(b''.join(json.dumps(command).encode() + b'\n' for command in commands)))
        vector = [1.] * 128
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'models.lock.json').write_text(json.dumps({'yunet': {'path': '/unused/a'}, 'sface': {'path': '/unused/b'}}))
            library = MagicMock()
            library.prctl.return_value = 0
            with patch.dict(os.environ, {'AIOS_EVALUATION_PARENT': str(os.getppid())}), \
                    patch.object(session.ctypes, 'CDLL', return_value=library), \
                    patch('aios.biometrics.FaceEncoder'), \
                    patch('aios.capture_service.inventory', return_value={'device': ['/unused']}), \
                    patch.object(session, 'FramingPreview', return_value=preview), \
                    patch.object(session, 'PreviewAcquisition', return_value=capture) as factory, \
                    patch.object(session, 'guided_enrollment', return_value=[vector]), \
                    patch.object(session, 'collect_good_frames', return_value=[vector] * 3), \
                    patch.object(sys, 'stdin', terminal), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(session.worker(root), 0)
        factory.assert_called_once()
        capture.__enter__.assert_called_once()
        capture.__exit__.assert_called_once()
        preview.close.assert_called_once()

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

    def test_repeated_read_errors_do_not_close_or_reopen_stream(self):
        capture = session.PreviewAcquisition('/unused', MagicMock())
        frame = object()
        with patch.object(session.Acquisition, 'read', side_effect=[RuntimeError('stale_frame')] * 3 + [frame]), \
                patch.object(session.Acquisition, '__exit__') as close, \
                patch.object(session.Acquisition, '__enter__') as reopen, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(capture.read(), frame)
        close.assert_not_called()
        reopen.assert_not_called()
        self.assertEqual(capture.preview.recover.call_count, 3)
        capture.preview.advance.assert_not_called()

    def test_cancel_during_bad_frames_stops_read_loop(self):
        capture = session.PreviewAcquisition('/unused', MagicMock())
        capture.preview.recover.side_effect = RuntimeError('preview_cancelled')
        with patch.object(session.Acquisition, 'read', side_effect=RuntimeError('stale_frame')), \
                patch.object(session.Acquisition, '__enter__') as reopen, \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'preview_cancelled'):
                capture.read()
        reopen.assert_not_called()

    def test_sampling_drains_preview_between_two_second_selections(self):
        capture = MagicMock(next_sample_at=0)
        frames = [object() for _ in range(5)]
        capture.read.side_effect = frames
        clock = MagicMock(side_effect=[10., 10.2, 11., 11.9, 12.])
        self.assertIs(session.sample_frame(capture, clock), frames[0])
        self.assertIs(session.sample_frame(capture, clock), frames[4])
        self.assertEqual(capture.read.call_count, 5)
        self.assertEqual(capture.next_sample_at, 14.)

    def test_recovery_clears_stale_preview_and_pumps_cancel(self):
        preview = session.FramingPreview.__new__(session.FramingPreview)
        preview.app, preview.image, preview.feedback = MagicMock(), MagicMock(), MagicMock()
        preview.window = MagicMock()
        preview.cancelled = False
        preview.app.processEvents.side_effect = preview.cancel
        with self.assertRaisesRegex(RuntimeError, 'preview_cancelled'):
            preview.recover('Reconnecting')
        preview.image.clear.assert_called_once()

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

    def test_positioning_has_no_default_deadline(self):
        import itertools
        preview = session.FramingPreview.__new__(session.FramingPreview)
        preview.app, preview.heading, preview.feedback, preview.next_button = [MagicMock() for _ in range(4)]
        preview.hint = 'Position yourself'
        capture = MagicMock()
        calls = []
        def read():
            calls.append(True)
            if len(calls) == 4:
                preview.advance()
            return object()
        capture.read.side_effect = read
        with patch.object(session.time, 'monotonic', side_effect=itertools.count(step=10000)):
            preview.ready(capture)
        self.assertEqual(len(calls), 4)

    def test_burst_uses_ten_fixed_slots_in_twenty_seconds(self):
        capture, preview = MagicMock(), MagicMock()
        frames = [object() for _ in range(40)]
        ticks = iter([100.] + [100. + (i+1)*.5 for i in range(40)])
        counter = iter(range(40))
        def read():
            i = next(counter)
            capture.last_capture = 100. + (i+1)*.5
            return frames[i]
        capture.read_once.side_effect = read
        result = session.capture_burst(capture, preview, clock=lambda: next(ticks))
        self.assertEqual(result, frames[3::4])
        self.assertEqual(len(result), 10)
        self.assertEqual(capture.read_once.call_count, 40)

    def test_missing_burst_slots_are_not_replaced(self):
        capture, preview = MagicMock(), MagicMock()
        capture.read_once.return_value = None
        clock = MagicMock(side_effect=[0., 3., 9., 21.])
        self.assertEqual(session.capture_burst(capture, preview, clock), [None] * 10)
        self.assertEqual(capture.read_once.call_count, 3)

    def test_enrollment_uses_only_supplied_burst_and_discards_bad_shots(self):
        capture, encoder, preview = MagicMock(), MagicMock(), MagicMock()
        preview.cancelled = False
        frames = [object() for _ in range(10)]
        encoder.encode.side_effect = [[]] + [[(None, [1.] * 128)]] * 8
        with patch.object(session, 'capture_burst', return_value=frames) as burst, \
                patch.object(session, 'frame_feedback', side_effect=['too_dark'] + [None] * 9):
            samples = session.guided_enrollment(capture, encoder, preview)
        self.assertEqual(len(samples), 1)
        burst.assert_called_once()
        capture.read.assert_not_called()
        self.assertEqual(encoder.encode.call_count, 9)
        self.assertEqual(frames, [None] * 10)

    def test_mistyped_consent_can_be_retried_or_cancelled(self):
        with patch('builtins.input', side_effect=['I CONSET', '']), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(session.read_consent())

    def test_no_usable_burst_fails_without_replacements(self):
        capture, encoder, preview = MagicMock(), MagicMock(), MagicMock()
        preview.cancelled = False
        with patch.object(session, 'capture_burst', return_value=[None] * 10) as burst:
            with self.assertRaisesRegex(RuntimeError, 'no_usable_burst'):
                session.guided_enrollment(capture, encoder, preview)
        burst.assert_called_once()
        encoder.encode.assert_not_called()

    def test_reference_uses_all_ten_vectors_with_equal_weight(self):
        first, second = [1.] + [0.] * 127, [0., 20.] + [0.] * 126
        vector = session.reference_vector([first] * 5 + [second] * 5)
        self.assertAlmostEqual(vector[0], 2 ** -.5)
        self.assertAlmostEqual(vector[1], 2 ** -.5)

    def test_cancel_during_burst_does_not_process_partial_photos(self):
        capture, encoder, preview = MagicMock(), MagicMock(), MagicMock()
        capture.read_once.side_effect = RuntimeError('preview_cancelled')
        with self.assertRaisesRegex(RuntimeError, 'preview_cancelled'):
            session.guided_enrollment(capture, encoder, preview)
        encoder.encode.assert_not_called()

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

    def exchange(self, response, timeout=2):
        child = subprocess.Popen([sys.executable, '-c',
                                  'import sys; sys.stdin.readline(); print(sys.argv[1], flush=True)',
                                  json.dumps(response)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            return session.exchange(child, 'probe', timeout)
        finally:
            child.wait(timeout=3)
            child.stdin.close()
            child.stdout.close()

    def test_only_aggregate_result_accepted(self):
        value = {'kind': 'evaluation', 'status': 'measured', 'matched': True, 'seconds': .1}
        self.assertEqual(self.exchange(value), value)
        long_result = {**value, 'seconds': 100000.}
        self.assertEqual(self.exchange(long_result, timeout=None), long_result)
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
