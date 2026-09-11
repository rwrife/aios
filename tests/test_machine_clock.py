import datetime
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from aios import machine_clock, os_settings


class MachineClockTests(unittest.TestCase):
    def test_dates_and_offsets(self):
        self.assertEqual(machine_clock.parse_datetime("2026-09-11T07:30:00-07:00"),
                         datetime.datetime(2026, 9, 11, 14, 30, tzinfo=datetime.timezone.utc))
        for value in ("2000-01-01T00:00:00Z", "2099-12-31T23:59:59Z",
                      "2024-02-29T12:00:00+14:00"):
            machine_clock.parse_datetime(value)
        for value in (None, True, 0, {}, "2026-09-11", "2026-09-11T12:00:00",
                      "2026-02-29T12:00:00Z", "2026-09-11T24:00:00Z",
                      "2026-09-11T12:00:60Z", "2026-09-11T12:00:00.1Z",
                      "2026-09-11T12:00:00+14:01", "2026-09-11T12:00:00+12:60",
                      "2026-09-11T12:00:00-00:00", "1999-12-31T23:59:59Z",
                      "2000-01-01T00:00:00+00:01", "2099-12-31T23:59:59-00:01",
                      "2026-09-11T12:00:00Z\n", "$(date)", "x" * 10000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                machine_clock.parse_datetime(value)

    def test_invalid_requests_never_launch_helper(self):
        with mock.patch.object(machine_clock.subprocess, "run") as run:
            for request in ({}, [], {"action": "read", "value": "x"},
                            {"action": "set"}, {"action": "set", "value": True},
                            {"action": "set", "value": "2026-09-11T12:00:00Z", "command": "id"}):
                with self.assertRaises(ValueError):
                    machine_clock.request_clock(request)
            run.assert_not_called()

    def test_adapter_fixed_helper_and_readback(self):
        state = {"utc": "2026-09-11T14:30:00Z"}
        with mock.patch.object(machine_clock.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(state))) as run:
            self.assertEqual(machine_clock.request_clock({"action": "read"}), state)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/doas", "-n", "/usr/local/sbin/aios-clock"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"action": "read"})
            self.assertNotIn("shell", run.call_args.kwargs)
        for response in (subprocess.CompletedProcess([], 1, ""),
                         subprocess.CompletedProcess([], 1, '{"error":"Automatic synchronization is running."}'),
                         subprocess.CompletedProcess([], 0, "[]")):
            with mock.patch.object(machine_clock.subprocess, "run", return_value=response), self.assertRaises(RuntimeError):
                machine_clock.request_clock({"action": "read"})
        with mock.patch.object(machine_clock.subprocess, "run", side_effect=subprocess.TimeoutExpired("helper", 8)):
            with self.assertRaisesRegex(RuntimeError, "Read the clock"):
                machine_clock.request_clock({"action": "read"})

    def test_os_tool_routes_and_schema(self):
        with mock.patch.object(machine_clock, "request_clock", return_value={"utc": "now"}) as clock:
            self.assertEqual(os_settings.act({"action": "read", "setting": "date_time"}), {"utc": "now"})
            os_settings.act({"action": "set", "setting": "date_time", "value": "2026-09-11T14:30:00Z"})
            clock.assert_called_with({"action": "set", "value": "2026-09-11T14:30:00Z"})
        with mock.patch.object(os_settings, "_desktop") as desktop:
            os_settings.act({"action": "open", "section": "date_time"})
            desktop.assert_called_once_with("open", section="date_time")
        properties = os_settings.TOOL["function"]["parameters"]["properties"]
        self.assertIn("date_time", properties["setting"]["enum"])
        self.assertIn("date_time", properties["section"]["enum"])

    def test_real_stdio_mcp_lists_and_rejects_invalid_date(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "os_settings", "arguments": {"action": "set", "setting": "date_time", "value": "invalid"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "aios.os_settings"],
                                input="\n".join(map(json.dumps, messages)) + "\n",
                                capture_output=True, text=True, timeout=5, check=True)
        listed, failed = map(json.loads, result.stdout.splitlines())
        self.assertIn("date_time", listed["result"]["tools"][0]["inputSchema"]["properties"]["setting"]["enum"])
        self.assertTrue(failed["result"]["isError"])
        self.assertIn("YYYY-MM-DD", failed["result"]["content"][0]["text"])

    def test_image_installs_narrow_isolated_helper(self):
        root = Path(__file__).resolve().parents[1]
        rules = (root / "distro/alpine/overlay/etc/doas.d/aios.conf").read_text()
        self.assertIn("permit nopass aios as root cmd /usr/local/sbin/aios-clock args\n", rules)
        helper = (root / "distro/alpine/overlay/usr/local/sbin/aios-clock").read_text()
        self.assertTrue(helper.startswith("#!/usr/bin/python3 -I\n"))
        self.assertIn('sys.path.insert(0, "/usr/local/share/aios")', helper)


@unittest.skipUnless(sys.platform == "linux", "Linux guest clock service")
class ClockServiceTests(unittest.TestCase):
    def setUp(self):
        from aios import clock_service
        self.service = clock_service

    def test_read_is_non_mutating(self):
        with mock.patch.object(self.service, "sync_daemons", return_value=[]), \
                mock.patch.object(self.service.time, "clock_settime") as clock:
            result = self.service.handle({"action": "read"})
            self.assertTrue(result["utc"].endswith("Z"))
            self.assertIn("timezone", result)
            clock.assert_not_called()

    def test_set_and_hardware_failure_return_actual_state(self):
        for code in (0, 1):
            with mock.patch.object(self.service, "sync_daemons", return_value=[]), \
                    mock.patch.object(self.service.time, "clock_settime") as clock, \
                    mock.patch.object(self.service, "read_clock", return_value={"utc": "readback"}), \
                    mock.patch.object(self.service.subprocess, "run", return_value=subprocess.CompletedProcess([], code)) as rtc:
                result = self.service.handle({"action": "set", "value": "2026-09-11T07:30:00-07:00"})
                clock.assert_called_once_with(self.service.time.CLOCK_REALTIME, 1789137000.0)
                self.assertEqual(result["state"]["utc"], "readback")
                self.assertEqual(result["hardware_clock_saved"], code == 0)
                rtc.assert_called_once_with(["/sbin/hwclock", "--systohc", "--utc"],
                                            capture_output=True, timeout=3, check=False)

    def test_sync_and_invalid_inputs_never_set_clock(self):
        with mock.patch.object(self.service, "sync_daemons", return_value=["ntpd"]), \
                mock.patch.object(self.service.time, "clock_settime") as clock:
            with self.assertRaisesRegex(RuntimeError, "synchronization"):
                self.service.handle({"action": "set", "value": "2026-09-11T14:30:00Z"})
            with self.assertRaises(ValueError):
                self.service.handle({"action": "set", "value": "bad"})
            clock.assert_not_called()

    def test_kernel_failure_does_not_save_rtc(self):
        with mock.patch.object(self.service, "sync_daemons", return_value=[]), \
                mock.patch.object(self.service.time, "clock_settime", side_effect=PermissionError), \
                mock.patch.object(self.service.subprocess, "run") as rtc:
            with self.assertRaisesRegex(RuntimeError, "not changed"):
                self.service.handle({"action": "set", "value": "2026-09-11T14:30:00Z"})
            rtc.assert_not_called()
