"""Stage-3 Wi-Fi regulatory country: fixed trusted operation, honest persistence.

The adapter mirrors the machine clock: validation happens before any helper
runs, the helper is a fixed no-argument doas target, and a change is reported
only after the kernel's own readback agrees.
"""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from aios import os_settings, regulatory

ROOT = Path(__file__).resolve().parents[1]


class CountryValidationTests(unittest.TestCase):
    def test_accepts_iso_3166_alpha_2_and_the_world_domain(self):
        for value in ("US", "GB", "DE", "JP", "00"):
            self.assertEqual(regulatory.parse_country(value), value)

    def test_rejects_anything_else_before_the_helper_runs(self):
        with mock.patch.object(regulatory.subprocess, "run") as run:
            for value in (None, True, 1, "", "u", "usa", "us", "U5", "US ", "US\n",
                          "US;reboot", "$(id)", "../..", "X" * 500):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    regulatory.request_domain({"action": "set", "value": value})
            for request in ({}, [], {"action": "read", "value": "US"}, {"action": "set"},
                            {"action": "reset"}, {"action": "set", "value": "US", "cmd": "id"}):
                with self.subTest(request=request), self.assertRaises(ValueError):
                    regulatory.request_domain(request)
            run.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_uses_the_fixed_helper_without_a_shell(self):
        state = {"country": "US", "persistent": False}
        with mock.patch.object(regulatory.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, json.dumps(state))) as run:
            self.assertEqual(regulatory.request_domain({"action": "read"}), state)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/doas", "-n", "/usr/local/sbin/aios-regdomain"])
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"action": "read"})
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_failures_are_explicit_never_silent(self):
        for response in (subprocess.CompletedProcess([], 1, ""),
                         subprocess.CompletedProcess([], 1, '{"error":"Wi-Fi regulatory support is unavailable."}'),
                         subprocess.CompletedProcess([], 0, "[]")):
            with mock.patch.object(regulatory.subprocess, "run", return_value=response), \
                    self.assertRaises(RuntimeError):
                regulatory.request_domain({"action": "read"})
        with mock.patch.object(regulatory.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("helper", 10)):
            with self.assertRaisesRegex(RuntimeError, "Read the country"):
                regulatory.request_domain({"action": "read"})


class ToolSurfaceTests(unittest.TestCase):
    def test_tool_routes_read_and_set_to_the_adapter(self):
        with mock.patch.object(regulatory, "request_domain", return_value={"country": "US"}) as helper:
            self.assertEqual(os_settings.act({"action": "read", "setting": "wifi_country"}), {"country": "US"})
            helper.assert_called_with({"action": "read"})
            os_settings.act({"action": "set", "setting": "wifi_country", "value": "GB"})
            helper.assert_called_with({"action": "set", "value": "GB"})

    def test_schema_advertises_bounded_country_values(self):
        properties = os_settings.TOOL["function"]["parameters"]["properties"]
        self.assertIn("wifi_country", properties["setting"]["enum"])
        country = [option for option in properties["value"]["oneOf"]
                   if option.get("maxLength") == 2]
        self.assertEqual(len(country), 1)
        self.assertEqual(country[0]["pattern"], "^([A-Z]{2}|00)$")
        self.assertIn("wifi_country", os_settings.TOOL["function"]["description"])
        self.assertFalse(os_settings.TOOL["function"]["parameters"]["additionalProperties"])

    def test_real_stdio_mcp_rejects_an_invalid_country(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "os_settings", "arguments": {"action": "set", "setting": "wifi_country", "value": "usa"}}},
        ]
        result = subprocess.run([sys.executable, "-m", "aios.os_settings"],
                                input="\n".join(map(json.dumps, messages)) + "\n",
                                capture_output=True, text=True, timeout=15, check=True)
        listed, failed = map(json.loads, result.stdout.splitlines())
        self.assertIn("wifi_country", listed["result"]["tools"][0]["inputSchema"]["properties"]["setting"]["enum"])
        self.assertTrue(failed["result"]["isError"])
        self.assertIn("ISO 3166", failed["result"]["content"][0]["text"])

    def test_image_installs_a_narrow_isolated_helper(self):
        rules = (ROOT / "distro/alpine/overlay/etc/doas.d/aios.conf").read_text()
        self.assertIn("permit nopass aios as root cmd /usr/local/sbin/aios-regdomain args\n", rules)
        helper = (ROOT / "distro/alpine/overlay/usr/local/sbin/aios-regdomain").read_text()
        self.assertTrue(helper.startswith("#!/usr/bin/python3 -I\n"))
        self.assertIn('sys.path.insert(0, "/usr/local/share/aios")', helper)

    def test_documentation_stays_aligned_with_the_tool(self):
        for path in ("docs/agentic-tools.md", "apps/skills/os-control/SKILL.md", "docs/settings.md"):
            with self.subTest(path=path):
                self.assertIn("wifi_country", (ROOT / path).read_text(encoding="utf-8"))


@unittest.skipUnless(sys.platform == "linux", "Linux guest regulatory service")
class RegulatoryServiceTests(unittest.TestCase):
    def setUp(self):
        from aios import regulatory_service
        self.service = regulatory_service

    def test_read_reports_live_persistence_honestly(self):
        with mock.patch.object(self.service, "kernel_country", return_value="US"), \
                mock.patch.object(self.service, "installed", return_value=False), \
                mock.patch.object(self.service, "persisted_country", return_value=None):
            state = self.service.handle({"action": "read"})
        self.assertEqual(state["country"], "US")
        self.assertFalse(state["persistent"])
        self.assertEqual(state["mode"], "live")
        self.assertIn("until reboot", state["persistence"])

    def test_installed_systems_report_a_stored_country(self):
        with mock.patch.object(self.service, "kernel_country", return_value="DE"), \
                mock.patch.object(self.service, "installed", return_value=True), \
                mock.patch.object(self.service, "persisted_country", return_value="DE"):
            state = self.service.handle({"action": "read"})
        self.assertTrue(state["persistent"])
        self.assertEqual(state["persisted_country"], "DE")
        self.assertEqual(state["mode"], "installed")

    def test_unavailable_wireless_stack_is_an_error_not_a_default(self):
        with mock.patch.object(self.service, "_iw", return_value=None):
            for request in ({"action": "read"}, {"action": "set", "value": "US"}):
                with self.subTest(request=request), self.assertRaisesRegex(RuntimeError, "unavailable"):
                    self.service.handle(request)

    def test_set_requires_kernel_readback_agreement(self):
        with mock.patch.object(self.service, "kernel_country", side_effect=["00", "00"]), \
                mock.patch.object(self.service, "_iw", return_value="") as iw, \
                mock.patch.object(self.service, "persist") as persist:
            with self.assertRaisesRegex(RuntimeError, "did not adopt"):
                self.service.handle({"action": "set", "value": "ZZ"})
            iw.assert_called_once_with("reg", "set", "ZZ")
            persist.assert_not_called()

    def test_set_applies_persists_and_reads_back(self):
        import tempfile
        for is_installed in (True, False):
            with self.subTest(installed=is_installed), \
                    tempfile.TemporaryDirectory(prefix="aios-regdomain-") as directory:
                conf = Path(directory) / "modprobe.d" / "aios-cfg80211.conf"
                with mock.patch.object(self.service, "kernel_country", return_value="GB"), \
                        mock.patch.object(self.service, "_iw", return_value="") as iw, \
                        mock.patch.object(self.service, "installed", return_value=is_installed), \
                        mock.patch.object(self.service, "MODPROBE_CONF", conf):
                    result = self.service.handle({"action": "set", "value": "GB"})
                self.assertEqual(iw.call_args.args, ("reg", "set", "GB"))
                self.assertEqual(result["state"]["country"], "GB")
                self.assertEqual(result["saved_for_next_boot"], is_installed)
                self.assertEqual(conf.exists(), is_installed)
                if is_installed:
                    self.assertIn("options cfg80211 ieee80211_regdom=GB", conf.read_text())
                    self.assertEqual(result["state"]["persisted_country"], "GB")
                else:
                    self.assertIsNone(result["state"]["persisted_country"])
                    self.assertIn("resets at reboot", result["notice"])

    def test_helper_refuses_non_guest_hosts_and_arguments(self):
        for argv, uid, release in ((["aios-regdomain", "set"], 0, "ID=aios\n"),
                                   (["aios-regdomain"], 1000, "ID=aios\n"),
                                   (["aios-regdomain"], 0, "ID=ubuntu\n")):
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(self.service.os, "geteuid", return_value=uid), \
                    mock.patch.object(Path, "read_text", return_value=release), \
                    mock.patch.object(self.service, "handle") as handle, \
                    mock.patch.object(sys, "stdout", new_callable=io.StringIO) as output:
                self.assertEqual(self.service.main(), 1)
                self.assertIn("error", json.loads(output.getvalue()))
                handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
