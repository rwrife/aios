"""Stage-3 hardware diagnostics: classification, redaction and output bounds.

Every test injects a fake sysfs/procfs tree and a fake command runner, so the
suite never touches real hardware, a display server or a network.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from aios import hardware_diagnostics as diagnostics

IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
MAC = re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b")

CPUINFO = "processor\t: 0\nflags\t\t: fpu sse4_2 avx avx2 bmi2 f16c fma\n"
BOOT_ID = "3f2b0b4c-0f51-4a5d-9a2f-1c4f9b0e7a11"
PREVIOUS_BOOT_ID = "8c1d5e77-2b33-4f0a-9d6e-55a7c1b3d204"


def session_state(**overrides):
    """A valid aios-session renderer record, as the session writes it."""
    record = {"version": 2, "boot_id": BOOT_ID, "status": "ready", "phase": "shell_ready",
              "backend": "glx", "renderer": "accelerated", "accelerated": True,
              "recovery": False}
    record.update(overrides)
    return json.dumps(record)



def fake_runner(general=None, devices=None, active=None, dmesg=None, dns=None, seen=None):
    def run(args):
        if seen is not None:
            seen.append(tuple(args))
        if args[0] not in diagnostics.ALLOWED_PROGRAMS:
            raise AssertionError("unexpected program " + args[0])
        if args[0] == "dmesg":
            return dmesg
        if args[0] == "getent":
            return dns
        if "general" in args:
            return general
        if "device" in args:
            return devices
        return active
    return run


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FakeSystem:
    """Minimal sysfs/procfs tree for one diagnostics run."""

    def __init__(self, directory):
        self.root = Path(directory) / "root"
        self.home = Path(directory) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        write(self.root / "proc/cmdline", "BOOT_IMAGE=/boot/vmlinuz-lts quiet\n")
        write(self.root / "proc/modules", "")
        write(self.root / "proc/cpuinfo", CPUINFO)
        write(self.root / "proc/net/route", "Iface\tDestination\tGateway\n")
        write(self.root / "etc/resolv.conf", "")
        write(self.root / "etc/aios-mode", "live\n")
        write(self.root / "proc/sys/kernel/random/boot_id", BOOT_ID + "\n")

    def interface(self, name, wireless=True, operstate="down", carrier="0"):
        base = self.root / "sys/class/net" / name
        write(base / "operstate", operstate + "\n")
        write(base / "carrier", carrier + "\n")
        if wireless:
            (base / "phy80211").mkdir(parents=True, exist_ok=True)
        return self

    def rfkill(self, index=0, kind="wlan", hard="0", soft="0"):
        base = self.root / "sys/class/rfkill" / f"rfkill{index}"
        write(base / "type", kind + "\n")
        write(base / "hard", hard + "\n")
        write(base / "soft", soft + "\n")
        return self

    def pci_network(self, address="0000:00:14.3", driver=None):
        base = self.root / "sys/bus/pci/devices" / address
        write(base / "class", "0x028000\n")
        if driver:
            (base / "driver").mkdir(parents=True, exist_ok=True)
        return self

    def modules(self, *names):
        write(self.root / "proc/modules",
              "".join(f"{name} 16384 0 - Live 0x0000000000000000\n" for name in names))
        return self

    def cmdline(self, text):
        write(self.root / "proc/cmdline", text + "\n")
        return self

    def route(self, default=True):
        rows = "Iface\tDestination\tGateway\tFlags\n"
        if default:
            rows += "wlan0\t00000000\t0101A8C0\t0003\n"
        write(self.root / "proc/net/route", rows)
        return self

    def resolv(self, text):
        write(self.root / "etc/resolv.conf", text)
        return self

    def renderer(self, text):
        write(self.home / diagnostics.SESSION_STATE_DIR / diagnostics.RENDERER_STATE, text)
        return self

    def forget_boot_id(self):
        (self.root / "proc/sys/kernel/random/boot_id").unlink()
        return self

    def drm(self, card="card0", nodes=True):
        (self.root / "sys/class/drm" / card / "device").mkdir(parents=True, exist_ok=True)
        if nodes:
            write(self.root / "dev/dri/card0", "")
            write(self.root / "dev/dri/renderD128", "")
        return self

    def probe(self, runner=None, dns_probe=False):
        return diagnostics.Probe(root=self.root, home=self.home,
                                 runner=runner or fake_runner(), dns_probe=dns_probe)


class DiagnosticsTestCase(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="aios-diagnostics-")
        self.addCleanup(workspace.cleanup)
        self.system = FakeSystem(workspace.name)


class NetworkClassificationTests(DiagnosticsTestCase):
    """Each documented failure produces a different, actionable state."""

    def evidence(self, **overrides):
        base = {
            "interfaces": [], "wireless_interfaces": 1, "wired_interfaces": 1,
            "modules": {"readable": True, "stack": ["cfg80211", "mac80211"], "wireless_drivers": ["iwlwifi"]},
            "rfkill": {"wireless_entries": 1, "wireless_hard_blocked": False, "wireless_soft_blocked": False},
            "pci": {"network_controllers": 1, "network_controllers_without_driver": 0},
            "firmware": {"checked": True, "load_failures": 0},
            "manager": {"available": True, "state": "connected", "connectivity": "full",
                        "wifi_hardware_enabled": True, "wifi_enabled": True,
                        "wifi_device_states": ["connected"], "wifi_devices": 1,
                        "active_wifi_connections": 1, "active_wired_connections": 0},
            "ip": {"default_route": True, "nameservers": 1},
            "dns_probe": {"performed": False, "resolved": None},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = {**base[key], **value}
            else:
                base[key] = value
        return base

    def test_every_documented_state_is_reachable_and_distinct(self):
        cases = {
            "connected": {},
            "rfkill_hard_block": {"rfkill": {"wireless_hard_blocked": True}},
            "rfkill_soft_block": {"rfkill": {"wireless_soft_blocked": True}},
            "no_interface": {"wireless_interfaces": 0,
                             "pci": {"network_controllers": 0, "network_controllers_without_driver": 0}},
            "missing_module": {"wireless_interfaces": 0,
                               "pci": {"network_controllers_without_driver": 1}},
            "missing_firmware": {"wireless_interfaces": 0,
                                 "firmware": {"load_failures": 2}},
            "authentication_failure": {"manager": {"wifi_device_states": ["need_authentication"]}},
            "disconnected": {"manager": {"wifi_device_states": ["disconnected"],
                                         "active_wifi_connections": 0}},
            "dhcp_failure": {"ip": {"default_route": False}},
            "dns_failure": {"ip": {"nameservers": 0}},
        }
        seen = {}
        for expected, overrides in cases.items():
            with self.subTest(state=expected):
                state, reason = diagnostics.classify_network(self.evidence(**overrides))
                self.assertEqual(state, expected)
                self.assertIn(state, diagnostics.NETWORK_STATES)
                seen.setdefault(state, set()).add(reason)
        self.assertEqual(len(seen), len(cases))

    def test_missing_firmware_needs_evidence_not_a_guess(self):
        # A firmware failure elsewhere must not accuse a working radio.
        state, _ = diagnostics.classify_network(self.evidence(firmware={"load_failures": 1}))
        self.assertEqual(state, "connected")
        state, reason = diagnostics.classify_network(self.evidence(
            firmware={"load_failures": 1}, manager={"wifi_device_states": ["unavailable"]}))
        self.assertEqual((state, reason), ("missing_firmware", "firmware_load_failed"))
        state, reason = diagnostics.classify_network(self.evidence(
            manager={"wifi_device_states": ["unavailable"]}))
        self.assertEqual((state, reason), ("disconnected", "wireless_device_unavailable"))

    def test_dns_probe_failure_is_separate_from_a_missing_resolver(self):
        state, reason = diagnostics.classify_network(self.evidence(
            dns_probe={"performed": True, "resolved": False}))
        self.assertEqual((state, reason), ("dns_failure", "resolution_failed"))
        state, reason = diagnostics.classify_network(self.evidence(ip={"nameservers": 0}))
        self.assertEqual((state, reason), ("dns_failure", "no_resolver_configured"))

    def test_unavailable_network_manager_is_reported_not_guessed(self):
        state, reason = diagnostics.classify_network(self.evidence(
            manager={"available": False}, ip={"default_route": False, "nameservers": 0}))
        self.assertEqual((state, reason), ("unknown", "network_manager_unavailable"))

    def test_loopback_and_ethernet_never_stand_in_for_wifi(self):
        # Loopback and Ethernet are active, the radio is not: the documented
        # states are Wi-Fi focused, so this is disconnected, not connected.
        for wifi_state, expected_reason in (("disconnected", "wireless_device_not_connected"),
                                            ("unmanaged", "wireless_device_not_connected")):
            with self.subTest(wifi_state=wifi_state):
                state, reason = diagnostics.classify_network(self.evidence(manager={
                    "wifi_device_states": [wifi_state], "active_wifi_connections": 0,
                    "active_wired_connections": 2}))
                self.assertEqual((state, reason), ("disconnected", expected_reason))
        state, reason = diagnostics.classify_network(self.evidence(manager={
            "wifi_device_states": ["connected"], "active_wifi_connections": 0,
            "active_wired_connections": 1}))
        self.assertEqual((state, reason), ("disconnected", "no_active_wireless_connection"))

    def test_a_radio_network_manager_does_not_manage_is_disconnected(self):
        state, reason = diagnostics.classify_network(self.evidence(manager={
            "wifi_device_states": [], "wifi_devices": 0, "active_wifi_connections": 0,
            "active_wired_connections": 1}))
        self.assertEqual((state, reason), ("disconnected", "no_managed_wireless_device"))

    def test_a_failed_active_connection_query_falls_back_to_device_state(self):
        state, reason = diagnostics.classify_network(self.evidence(manager={
            "active_wifi_connections": None, "active_wired_connections": None}))
        self.assertEqual((state, reason), ("connected", "active_connection_with_route"))
        state, reason = diagnostics.classify_network(self.evidence(manager={
            "wifi_device_states": ["disconnected"], "active_wifi_connections": None,
            "active_wired_connections": None}))
        self.assertEqual((state, reason), ("disconnected", "wireless_device_not_connected"))

    def test_active_connection_counting_is_wifi_specific_and_nameless(self):
        self.system.interface("wlan0").interface("eth0", wireless=False, operstate="up", carrier="1")
        self.system.modules("cfg80211", "mac80211", "iwlwifi").route(True)
        self.system.resolv("nameserver 10.0.0.1\n")
        seen = []
        runner = fake_runner(
            general="running:connected:full:enabled:enabled\n",
            devices="wifi:disconnected\nethernet:connected\nloopback:connected\n",
            # NetworkManager 1.42+ lists the loopback connection; a wired link
            # is active while the radio is not.
            active="loopback:activated\nethernet:activated\nwifi:deactivated\n",
            seen=seen)
        report = diagnostics.build_report(self.system.probe(runner))
        manager = report["network"]["evidence"]["manager"]
        self.assertEqual(manager["active_wifi_connections"], 0)
        self.assertEqual(manager["active_wired_connections"], 1)
        self.assertEqual(report["network"]["state"], "disconnected")
        self.assertEqual(report["network"]["reason"], "wireless_device_not_connected")
        self.assertIn(("nmcli", "-t", "-f", "TYPE,STATE", "connection", "show", "--active"), seen)
        self.assertNotIn("active_connections", manager)

    def test_a_connected_radio_alongside_ethernet_is_still_connected(self):
        self.system.interface("wlan0").interface("eth0", wireless=False, operstate="up", carrier="1")
        self.system.modules("cfg80211", "mac80211", "iwlwifi").route(True)
        self.system.resolv("nameserver 10.0.0.1\n")
        report = diagnostics.build_report(self.system.probe(fake_runner(
            general="running:connected:full:enabled:enabled\n",
            devices="wifi:connected\nethernet:connected\n",
            active="loopback:activated\nethernet:activated\nwifi:activated\n")))
        self.assertEqual(report["network"]["state"], "connected")
        self.assertEqual(report["network"]["evidence"]["manager"]["active_wifi_connections"], 1)

    def test_fixture_tree_classifies_end_to_end(self):
        self.system.interface("wlan0").rfkill(hard="1")
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["network"]["state"], "rfkill_hard_block")

        self.system.rfkill(hard="0")
        self.system.modules("cfg80211", "mac80211", "iwlwifi").route(True).resolv("nameserver 10.0.0.1\n")
        runner = fake_runner(general="running:connected:full:enabled:enabled\n",
                             devices="wifi:connected\nethernet:disconnected\n",
                             active="wifi:activated\n", dmesg="usb 1-1: new device\n")
        report = diagnostics.build_report(self.system.probe(runner))
        self.assertEqual(report["network"]["state"], "connected")
        self.assertEqual(report["network"]["evidence"]["modules"]["wireless_drivers"], ["iwlwifi"])
        self.assertEqual(report["network"]["evidence"]["wireless_interfaces"], 1)

    def test_missing_driver_on_the_bus_reads_as_a_missing_module(self):
        self.system.pci_network().modules("usbcore")
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["network"]["state"], "missing_module")


class GraphicsClassificationTests(DiagnosticsTestCase):
    """Renderer/compositor selection is observed, never re-derived."""

    def test_session_selection_drives_the_graphics_state(self):
        cases = {
            session_state(): ("accelerated", "session_renderer_probe", "glx"),
            session_state(backend="xrender", renderer="software", accelerated=False):
                ("degraded_software", "session_renderer_probe", "xrender"),
            session_state(backend="xrender", renderer="software", accelerated=False, recovery=True):
                ("degraded_software", "recovery_software_path", "xrender"),
            session_state(backend="xrender", renderer="unavailable", accelerated=False):
                ("unavailable", "session_renderer_probe_failed", "xrender"),
        }
        for recorded, (state, reason, compositor) in cases.items():
            with self.subTest(recorded=recorded):
                self.system.renderer(recorded).drm()
                report = diagnostics.build_report(self.system.probe())
                self.assertEqual(report["graphics"]["state"], state)
                self.assertEqual(report["graphics"]["reason"], reason)
                self.assertEqual(report["graphics"]["compositor"], compositor)

    def test_a_renderer_selection_alone_is_not_a_working_desktop(self):
        self.system.drm().renderer(session_state(status="starting", phase="selected"))
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "unknown")
        self.assertEqual(report["graphics"]["reason"], "renderer_selected_not_ready")
        self.assertEqual(report["graphics"]["session_phase"], "selected")
        # An unrecognised phase is equally not a success.
        self.system.renderer(session_state(status="starting", phase="almost_ready"))
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "unknown")
        self.assertEqual(report["graphics"]["evidence"]["session"]["phase"], "unknown")
        self.assertNotIn("almost_ready", json.dumps(report))

    def test_compositor_and_shell_failures_are_reported_as_startup_failures(self):
        self.system.drm()
        for phase, reason in (("compositor_failed", "compositor_did_not_start"),
                              ("shell_failed", "shell_did_not_start")):
            with self.subTest(phase=phase):
                self.system.renderer(session_state(status="failed", phase=phase))
                report = diagnostics.build_report(self.system.probe())
                self.assertEqual(report["graphics"]["state"], "startup_failed")
                self.assertEqual(report["graphics"]["reason"], reason)
                self.assertIn(report["graphics"]["state"], diagnostics.GRAPHICS_STATES)

    def test_a_ready_session_reports_its_recorded_renderer(self):
        self.system.drm()
        for phase in ("compositor_ready", "shell_ready"):
            with self.subTest(phase=phase):
                self.system.renderer(session_state(status="ready", phase=phase))
                report = diagnostics.build_report(self.system.probe())
                self.assertEqual(report["graphics"]["state"], "accelerated")
                self.assertEqual(report["graphics"]["session_phase"], phase)
                self.assertEqual(report["graphics"]["session_state_boot"], "current")

    def test_a_record_from_a_previous_boot_is_rejected(self):
        self.system.drm().renderer(session_state(boot_id=PREVIOUS_BOOT_ID))
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "unknown")
        self.assertEqual(report["graphics"]["reason"], "renderer_state_from_previous_boot")
        self.assertEqual(report["graphics"]["session_state_boot"], "previous")
        self.assertNotIn(PREVIOUS_BOOT_ID, json.dumps(report))
        self.assertNotIn(BOOT_ID, json.dumps(report))

    def test_an_unverifiable_boot_is_not_a_working_desktop(self):
        self.system.drm().forget_boot_id().renderer(session_state())
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "unknown")
        self.assertEqual(report["graphics"]["reason"], "renderer_state_boot_unverified")

    def test_an_older_record_version_is_not_trusted(self):
        self.system.drm().renderer(json.dumps(
            {"version": 1, "backend": "glx", "renderer": "accelerated",
             "accelerated": True, "recovery": False}))
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "unknown")
        self.assertEqual(report["graphics"]["reason"], "no_session_renderer_state")

    def test_missing_or_invalid_session_state_is_unknown_not_accelerated(self):
        self.system.drm()
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "unknown")
        self.assertEqual(report["graphics"]["reason"], "no_session_renderer_state")
        for invalid in ('not json', '[]', session_state(backend="wayland", renderer="turbo")):
            with self.subTest(invalid=invalid):
                self.system.renderer(invalid)
                report = diagnostics.build_report(self.system.probe())
                self.assertEqual(report["graphics"]["state"], "unknown")
                self.assertEqual(report["graphics"]["compositor"], "unknown")

    def test_no_card_node_is_reported_as_unavailable(self):
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "unavailable")
        self.assertEqual(report["graphics"]["reason"], "no_drm_card_node")

    def test_recovery_mode_is_observable(self):
        self.system.cmdline("BOOT_IMAGE=/boot/vmlinuz-lts aios.recovery nomodeset").drm()
        self.system.renderer(session_state(backend="xrender", renderer="software",
                                           accelerated=False, recovery=True))
        report = diagnostics.build_report(self.system.probe())
        self.assertTrue(report["boot"]["recovery"])
        self.assertTrue(report["boot"]["nomodeset"])
        self.assertEqual(report["graphics"]["recovery_mode"], "recovery")
        self.assertEqual(report["graphics"]["state"], "degraded_software")
        self.assertEqual(report["graphics"]["reason"], "recovery_software_path")

    def test_a_recovery_session_that_does_not_come_up_is_a_startup_failure(self):
        self.system.cmdline("BOOT_IMAGE=/boot/vmlinuz-lts aios.recovery nomodeset").drm()
        self.system.renderer(session_state(backend="xrender", renderer="software",
                                           accelerated=False, recovery=True,
                                           status="failed", phase="compositor_failed"))
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["state"], "startup_failed")
        self.assertEqual(report["graphics"]["reason"], "compositor_did_not_start")
        self.assertEqual(report["graphics"]["recovery_mode"], "recovery")

    @unittest.skipUnless(os.name == "posix", "driver binding is a sysfs symlink")
    def test_drm_driver_names_come_from_an_allowlist(self):
        self.system.drm()
        drivers = self.system.root / "sys/bus/pci/drivers"
        for name in ("i915", "supersecret_vendor_driver"):
            (drivers / name).mkdir(parents=True, exist_ok=True)
        card = self.system.root / "sys/class/drm/card0/device"
        (card / "driver").symlink_to(drivers / "i915")
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["evidence"]["drm_drivers"], ["i915"])
        (card / "driver").unlink()
        (card / "driver").symlink_to(drivers / "supersecret_vendor_driver")
        report = diagnostics.build_report(self.system.probe())
        self.assertEqual(report["graphics"]["evidence"]["drm_drivers"], ["other"])
        self.assertNotIn("supersecret", json.dumps(report))


class RedactionTests(DiagnosticsTestCase):
    """Nothing identifying may reach the report, in JSON or human form."""

    SECRETS = ("HomeNetwork5G", "a4:83:e7:11:22:33", "192.168.7.42", "laptop-of-alice",
               "SN12345678", "hunter2", "GeForce RTX 4090", BOOT_ID)

    def hostile_report(self):
        self.system.interface("wlan0-HomeNetwork5G", operstate="up", carrier="1")
        self.system.interface("eth0", wireless=False, operstate="up", carrier="1")
        self.system.rfkill(kind="wlan").modules("cfg80211", "mac80211", "iwlwifi")
        self.system.route(True).resolv("nameserver 192.168.7.42\nsearch laptop-of-alice.lan\n")
        self.system.drm()
        self.system.renderer(session_state(gpu="GeForce RTX 4090", serial="SN12345678"))
        runner = fake_runner(
            general="running:connected:full:enabled:enabled:laptop-of-alice\n",
            devices="wifi:connected:HomeNetwork5G\n",
            active="wifi:activated:HomeNetwork5G:hunter2\n",
            dmesg=("wlan0: authenticated with a4:83:e7:11:22:33 ssid HomeNetwork5G\n"
                   "iwlwifi 0000:00:14.3: Direct firmware load for iwlwifi-so-a0-gf-a0-73.ucode failed\n"))
        return diagnostics.build_report(self.system.probe(runner))

    def test_no_identifying_value_survives_into_the_report(self):
        report = self.hostile_report()
        for rendering in (json.dumps(report), diagnostics.human(report)):
            for secret in self.SECRETS:
                self.assertNotIn(secret, rendering)
            self.assertIsNone(IPV4.search(rendering), rendering)
            self.assertIsNone(MAC.search(rendering), rendering)
            self.assertNotIn("ucode", rendering)
        # The fixed redaction notice is the only place "SSID" may appear.
        payload = json.dumps({key: value for key, value in report.items() if key != "notes"})
        self.assertNotIn("ssid", payload.lower())

    def test_untrusted_values_are_mapped_to_allowlisted_enums(self):
        report = self.hostile_report()
        evidence = report["network"]["evidence"]
        self.assertEqual(evidence["manager"]["wifi_device_states"], ["unknown"])
        # Only the fixed TYPE,STATE columns are used: the trailing name and
        # secret columns are discarded rather than counted or emitted.
        self.assertEqual(evidence["manager"]["active_wifi_connections"], 1)
        self.assertEqual(evidence["manager"]["active_wired_connections"], 0)
        self.assertEqual(evidence["firmware"]["load_failures"], 1)
        for interface in evidence["interfaces"]:
            self.assertEqual(set(interface), {"index", "kind", "operstate", "carrier"})
            self.assertIn(interface["operstate"], diagnostics.OPERSTATES)
        self.assertEqual(report["graphics"]["evidence"]["session"]["backend"], "glx")
        self.assertNotIn("gpu", json.dumps(report["graphics"]))

    def test_invalid_enum_values_never_pass_through(self):
        self.system.interface("wlan0", operstate="$(rm -rf /)").rfkill(kind="cellular")
        self.system.resolv("nameserver 1.1.1.1\n")
        report = diagnostics.build_report(self.system.probe(
            fake_runner(general="running:connected%%%:hyperspace:yes:yes\n")))
        evidence = report["network"]["evidence"]
        self.assertEqual(evidence["interfaces"][0]["operstate"], "unknown")
        self.assertEqual(evidence["manager"]["connectivity"], "unknown")
        self.assertEqual(evidence["rfkill"]["wireless_entries"], 0)
        self.assertNotIn("rm -rf", json.dumps(report))


class BoundsTests(DiagnosticsTestCase):
    """Output size, list length, command surface and timeouts stay bounded."""

    def test_large_systems_stay_within_the_report_bound(self):
        for index in range(64):
            self.system.interface(f"wlan{index}", operstate="up", carrier="1")
            self.system.rfkill(index=index)
            self.system.pci_network(address=f"0000:00:{index:02d}.0")
        self.system.modules(*(f"driver{index}" for index in range(200)))
        report = diagnostics.build_report(self.system.probe(
            fake_runner(dmesg="Direct firmware load for a.ucode failed\n" * 5000,
                        general="running:connected:full:enabled:enabled\n",
                        devices="wifi:connected\n" * 500, active="wifi:activated\n" * 500)))
        serialized = json.dumps(report)
        self.assertLessEqual(len(serialized), diagnostics.MAX_REPORT_BYTES)
        self.assertLessEqual(len(report["network"]["evidence"]["interfaces"]), diagnostics.MAX_LIST_ITEMS)
        self.assertLessEqual(len(report["network"]["evidence"]["manager"]["wifi_device_states"]),
                             diagnostics.MAX_LIST_ITEMS)
        self.assertLessEqual(len(diagnostics.human(report).splitlines()), 16)

    def test_only_fixed_read_only_commands_can_run(self):
        seen = []
        diagnostics.build_report(self.system.probe(fake_runner(seen=seen)))
        self.assertTrue(seen)
        for command in seen:
            self.assertIn(command[0], diagnostics.ALLOWED_PROGRAMS)
            self.assertNotIn("-e", command[:1])
        for rejected in ([], ["sh", "-c", "id"], ["iw", "dev"], ["cat", "/etc/shadow"]):
            with self.subTest(command=rejected), self.assertRaises(ValueError):
                diagnostics.run_command(rejected)

    def test_dns_probe_is_opt_in_and_uses_a_fixed_name(self):
        seen = []
        diagnostics.build_report(self.system.probe(fake_runner(seen=seen)))
        self.assertNotIn("getent", [command[0] for command in seen])
        seen.clear()
        report = diagnostics.build_report(self.system.probe(
            fake_runner(dns="ok", seen=seen), dns_probe=True))
        self.assertIn(("getent", "hosts", diagnostics.DNS_PROBE_HOST), seen)
        self.assertTrue(report["network"]["evidence"]["dns_probe"]["performed"])

    def test_a_missing_resolver_tool_is_not_reported_as_a_dns_failure(self):
        probe = self.system.probe(fake_runner(), dns_probe=True)
        probe.available = lambda program: False
        evidence = diagnostics.build_report(probe)["network"]["evidence"]["dns_probe"]
        self.assertEqual(evidence, {"performed": False, "resolved": None,
                                    "reason": "resolver_tool_unavailable"})
        self.assertFalse(self.system.probe().available("sh"))

    def test_command_runner_bounds_its_own_timeout(self):
        self.assertLessEqual(diagnostics.COMMAND_TIMEOUT, 10)
        self.assertLessEqual(diagnostics.MAX_READ_BYTES, 65536)

    def test_cpu_section_uses_the_injected_processor(self):
        report = diagnostics.build_report(self.system.probe())
        self.assertTrue(report["cpu"]["local_inference"]["supported"])
        write(self.system.root / "proc/cpuinfo", "flags\t: fpu sse4_2 avx\n")
        report = diagnostics.build_report(self.system.probe())
        self.assertFalse(report["cpu"]["local_inference"]["supported"])
        self.assertEqual(report["cpu"]["local_inference"]["missing"], ["avx2", "bmi2", "f16c", "fma"])


class CommandLineTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "reads the host's own /proc and /sys")
    def test_report_runs_on_this_machine_in_both_modes(self):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=str(root / "apps"))
        for arguments in ([], ["--human"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, "-m", "aios.hardware_diagnostics", *arguments],
                                        capture_output=True, text=True, timeout=60, env=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertLessEqual(len(result.stdout), diagnostics.MAX_REPORT_BYTES + 512)
                if arguments:
                    self.assertIn("AIOS hardware report", result.stdout)
                else:
                    report = json.loads(result.stdout)
                    self.assertIn(report["network"]["state"], diagnostics.NETWORK_STATES)
                    self.assertIn(report["graphics"]["state"], diagnostics.GRAPHICS_STATES)

    def test_overlay_entry_point_is_a_thin_wrapper(self):
        script = (Path(__file__).resolve().parents[1]
                  / "distro/alpine/overlay/usr/local/bin/aios-hardware-report").read_text()
        self.assertIn("python3 -m aios.hardware_diagnostics", script)
        self.assertIn("/usr/local/share/aios", script)


if __name__ == "__main__":
    unittest.main()
