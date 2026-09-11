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

    def test_shell_camera_surfaces_prefer_bounded_capture(self):
        for relative in ('apps/shell/SettingsWindow.qml', 'apps/shell/SetupWizard.qml'):
            source = (ROOT / relative).read_text()
            with self.subTest(relative=relative):
                self.assertIn('640', source)
                self.assertIn('360', source)
                self.assertIn('cameraLoader.active = false', source)
                self.assertIn('sourceComponent: Camera', source)
                self.assertNotIn('VideoFrameFormat', source)
        photo = (ROOT / 'apps/shell/ProfilePhoto.h').read_text()
        self.assertIn('"video4linux2"', photo)
        self.assertIn('"mjpeg"', photo)
        self.assertIn('640 * 360 * 3', photo)
        self.assertIn('CameraDevice::capturePath()', photo)
        self.assertIn('QProcess::nullDevice()', photo)
        self.assertIn('retryTimer.start(250)', photo)
        self.assertIn('ffmpeg', (ROOT / 'distro/alpine/apks/world.ai').read_text().splitlines())

    def test_windows_launcher_discovers_attached_camera(self):
        launcher = (ROOT / 'scripts/run.ps1').read_text()
        self.assertIn("[string]$CameraBusId", launcher)
        self.assertIn('usbipd.exe', launcher)
        self.assertIn('/dev/v4l/by-id/*-video-index0', launcher)
        self.assertIn('setfacl -m "u:${linuxUser}:rw"', launcher)
        self.assertIn('"AIOS_VM_CAMERA_BUS=$cameraBus"', launcher)
        self.assertIn('"AIOS_VM_CAMERA_ADDR=$cameraAddr"', launcher)
