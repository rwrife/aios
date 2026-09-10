import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.camera import probe, video_device


ROOT = Path(__file__).resolve().parents[1]


class CameraTests(unittest.TestCase):
    def test_only_local_video_character_devices(self):
        for value in ('https://camera/stream', '/tmp/recording.mp4', '/dev/video0/../sda', 0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                video_device(value)
        with patch('aios.camera.Path.is_char_device', return_value=False), self.assertRaises(ValueError):
            video_device('/dev/video0')

    def test_blocked_driver_is_bounded(self):
        with patch('aios.camera.video_device', return_value='/dev/video0'), patch(
            'aios.camera.subprocess.run', side_effect=subprocess.TimeoutExpired('camera', 15)
        ), self.assertRaisesRegex(RuntimeError, 'timed out'):
            probe('/dev/video0')

    def test_diagnostics_contain_no_frame_or_driver_output(self):
        with patch('aios.camera.video_device', return_value='/dev/video0'), patch(
            'aios.camera.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 1, stdout='untrusted payload', stderr='untrusted driver details')
        ), self.assertRaisesRegex(RuntimeError, '^Camera probe failed;') as error:
            probe('/dev/video0')
        self.assertNotIn('untrusted', str(error.exception))

    def test_shell_camera_surfaces_prefer_bounded_uncompressed_capture(self):
        for relative in ('apps/shell/SettingsWindow.qml', 'apps/shell/SetupWizard.qml',
                         'apps/shell/ProfilePhoto.h'):
            source = (ROOT / relative).read_text()
            with self.subTest(relative=relative):
                self.assertIn('640', source)
                self.assertIn('480', source)
                self.assertIn('Format_YUYV', source)
