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
        with patch('aios.recognition._quality', return_value=True):
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
        with patch('aios.recognition._quality', return_value=True), \
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
