import subprocess
import json
import math
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import patch

from aios.camera import probe, video_device, capture_probe, guest_probe, validate_report


ROOT = Path(__file__).resolve().parents[1]


class CameraTests(unittest.TestCase):
    @staticmethod
    def report():
        return {'schema': 1, 'state': 'ready', 'reason': 'ok', 'frames': 10,
                'height': 480, 'width': 640, 'warmup_frames': 3, 'saved_frames': 0,
                'open_seconds': .1, 'warmup_seconds': .2, 'capture_seconds': .7,
                'elapsed_seconds': 1, 'close_seconds': .01,
                'negotiated': {'fourcc': 'MJPG', 'width': 640, 'height': 480,
                               'fps': 15, 'buffer_size': 1, 'buffer_request_accepted': True}}

    def test_probe_bounds_precede_device_access(self):
        for frames, timeout in [(True, 15), (0, 15), (61, 15), (10, math.nan),
                                (10, math.inf), (10, True), (10, 31)]:
            with self.subTest(frames=frames, timeout=timeout), patch('aios.camera.capture_device') as device:
                with self.assertRaises(ValueError):
                    probe('/dev/video0', frames, timeout)
                device.assert_not_called()

    def test_metadata_schema_rejects_extra_private_payload_and_nonfinite_values(self):
        validate_report(self.report(), 10)
        for key, value in [('image', 'private'), ('open_seconds', math.nan),
                           ('frames', True), ('saved_frames', 1), ('width', 0)]:
            report = self.report()
            report[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_report(report, 10)
        for value in ('serial/private', {}, True):
            report = self.report()
            report['negotiated']['fourcc'] = value
            with self.assertRaises(ValueError):
                validate_report(report, 10)

    def test_worker_report_is_validated_before_forwarding(self):
        for payload in ('not json', 'x' * 4097, json.dumps({**self.report(), 'image': 'private'})):
            with patch('aios.camera.capture_device'), patch('aios.camera.subprocess.run',
                    return_value=subprocess.CompletedProcess([], 0, stdout=payload)), self.assertRaisesRegex(RuntimeError, 'invalid report'):
                probe('/dev/video0')

    @unittest.skipUnless(sys.platform == 'linux', 'Linux guest identity')
    def test_guest_requires_exact_unprivileged_user(self):
        for uid, user in [(0, 'root'), (1000, 'other')]:
            with patch('aios.camera.os.geteuid', return_value=uid), patch('pwd.getpwuid',
                    return_value=SimpleNamespace(pw_name=user)), patch('aios.camera.probe') as capture:
                with self.assertRaisesRegex(RuntimeError, 'unprivileged'):
                    guest_probe('/dev/video0', 'aios')
                capture.assert_not_called()

    @unittest.skipUnless(sys.platform == 'linux', 'Linux guest identity')
    def test_guest_reopens_three_times_and_rejects_wrong_resolution(self):
        with patch('aios.camera.os.geteuid', return_value=1000), patch('pwd.getpwuid',
                return_value=SimpleNamespace(pw_name='aios')), patch('aios.camera.probe', return_value=self.report()) as capture:
            report = guest_probe('/dev/video0', 'aios')
            self.assertEqual(capture.call_count, 3)
            self.assertEqual(len(report['reopen_seconds']), 2)
            capture.return_value = {**self.report(), 'width': 320}
            with self.assertRaisesRegex(RuntimeError, '640x480'):
                guest_probe('/dev/video0', 'aios')

    def test_capture_drains_warmup_reports_negotiated_values_and_releases(self):
        from unittest.mock import Mock
        camera = Mock()
        camera.isOpened.return_value = True
        camera.get.side_effect = [1196444237, 640, 480, 15, 1]  # MJPG
        camera.read.return_value = (True, SimpleNamespace(size=640 * 480 * 3, shape=(480, 640, 3)))
        fake = SimpleNamespace(VideoCapture=Mock(return_value=camera), CAP_V4L2=1,
            CAP_PROP_FOURCC=2, CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
            CAP_PROP_FPS=5, CAP_PROP_BUFFERSIZE=6, VideoWriter_fourcc=lambda *args: 1196444237)
        with patch.dict(sys.modules, cv2=fake), patch('aios.camera.capture_device', return_value='/dev/video0'), patch('aios.camera.os.open', return_value=9), patch('aios.camera.os.close'):
            report = capture_probe('/dev/video0', 10)
        validate_report(report, 10)
        self.assertEqual(report['negotiated']['fourcc'], 'MJPG')
        self.assertEqual(camera.read.call_count, 13)
        camera.release.assert_called_once()
        camera.reset_mock()
        camera.get.side_effect = [1196444237, 640, 480, 15, 1]
        camera.read.return_value = (False, None)
        with patch.dict(sys.modules, cv2=fake), patch('aios.camera.capture_device'), patch('aios.camera.os.open', return_value=9), patch('aios.camera.os.close'), self.assertRaisesRegex(RuntimeError, 'deliver a frame'):
            capture_probe('/dev/video0', 10)
        camera.release.assert_called_once()

    def test_error_reason_is_allowlisted_and_driver_message_is_discarded(self):
        with patch('aios.camera.capture_device'), patch('aios.camera.subprocess.run',
                return_value=subprocess.CompletedProcess([], 1, stdout=json.dumps(
                    {'reason': 'permission_denied', 'error': 'private serial and path'}))):
            with self.assertRaisesRegex(RuntimeError, 'Camera access denied') as error:
                probe('/dev/video0')
            self.assertEqual(error.exception.reason, 'permission_denied')
            self.assertNotIn('private', str(error.exception))

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
