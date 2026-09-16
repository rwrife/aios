"""Bounded, sanitized hardware diagnostics for network and graphics startup.

`aios-hardware-report` classifies why Wi-Fi or the desktop renderer is not
working, using only fixed sysfs/procfs reads and a fixed, short allowlist of
read-only commands. It is deliberately not a device inventory: the report
contains fixed enums, booleans and small counts only. SSIDs, MAC addresses,
IP addresses, hostnames, serial numbers, saved credentials, raw kernel logs
and arbitrary device strings are never read into the report, and free-form
strings from a probe (renderer names, interface names, connection names) are
mapped through allowlists or dropped.

Renderer/compositor selection is not re-derived here. `aios-session` performs
the single renderer probe at session start and records its decision, and then
its startup progress, in ~/.local/state/aios/renderer.json; this module reports
that record so the report and the running desktop cannot disagree. The record
carries the boot it was written in, and a selection alone is never reported as
a working desktop: only a recorded compositor/shell readiness is.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import boot_mode
from . import cpu_features

REPORT_VERSION = 1
# Hard output/read bounds. The report is a diagnostic summary, not a log dump.
MAX_REPORT_BYTES = 8192
MAX_LIST_ITEMS = 8
MAX_READ_BYTES = 4096
MAX_COMMAND_BYTES = 256 * 1024
COMMAND_TIMEOUT = 5
# Fixed programs; no shell, no user-supplied arguments, no arbitrary paths.
ALLOWED_PROGRAMS = ("nmcli", "dmesg", "getent")
# Fixed name used only by the opt-in DNS probe. Never taken from input.
DNS_PROBE_HOST = "alpinelinux.org"

RENDERER_STATE = "renderer.json"
RENDERER_STATE_VERSION = 2
SESSION_STATE_DIR = ".local/state/aios"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

WIRELESS_STACK = ("cfg80211", "mac80211")
WIRELESS_DRIVERS = (
    "iwlwifi", "iwlmvm", "iwldvm", "ath9k", "ath10k_pci", "ath11k", "ath11k_pci",
    "ath12k", "ath12k_pci", "mt76", "mt7921e", "mt7921u", "mt7925e", "rtw88_pci",
    "rtw89_pci", "brcmfmac",
)
DRM_DRIVERS = (
    "i915", "xe", "amdgpu", "radeon", "nouveau", "virtio_gpu", "vmwgfx", "qxl",
    "bochs", "simpledrm", "vboxvideo",
)
OPERSTATES = ("up", "down", "dormant", "testing", "lowerlayerdown", "notpresent", "unknown")
RFKILL_TYPES = ("wlan", "bluetooth", "wwan", "gps", "nfc", "fm")
CONNECTIVITY_STATES = ("full", "limited", "portal", "none", "unknown")
FIRMWARE_FAILURE = re.compile(r"Direct firmware load for \S+ failed|firmware: failed to load", re.I)

NETWORK_STATES = (
    "connected", "disconnected", "no_interface", "missing_module", "missing_firmware",
    "rfkill_hard_block", "rfkill_soft_block", "authentication_failure",
    "dhcp_failure", "dns_failure", "unknown",
)
GRAPHICS_STATES = ("accelerated", "degraded_software", "unavailable", "startup_failed",
                   "unknown")
# Session startup phases recorded by aios-session, in order. A selection is not
# a working desktop: only compositor_ready/shell_ready report one.
SESSION_PHASES = ("selected", "compositor_ready", "shell_ready", "compositor_failed",
                  "shell_failed")
SESSION_STATUSES = ("starting", "ready", "failed")
SESSION_BOOTS = ("current", "previous", "unverifiable", "missing")
# nmcli connection/device types, mapped to fixed kinds. Names are never read.
CONNECTION_TYPES = {"wifi": "wireless", "802-11-wireless": "wireless",
                    "ethernet": "wired", "802-3-ethernet": "wired"}
CONNECTION_STATES = ("activated", "activating", "deactivating", "deactivated")



def run_command(args):
    """Run one allowlisted read-only program and return bounded stdout."""
    if not args or args[0] not in ALLOWED_PROGRAMS:
        raise ValueError("Only fixed diagnostic commands may be run.")
    try:
        result = subprocess.run(list(args), capture_output=True, text=True, check=False,
                                timeout=COMMAND_TIMEOUT, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout[:MAX_COMMAND_BYTES]


class Probe:
    """Filesystem/command access for one report. Injectable for tests."""

    def __init__(self, root="/", home=None, runner=None, dns_probe=False):
        self.root = Path(root)
        self.home = Path(home) if home is not None else Path(os.path.expanduser("~"))
        self.run = runner if runner is not None else run_command
        self.dns_probe = bool(dns_probe)

    def path(self, relative):
        return self.root / relative.lstrip("/")

    def read(self, relative, limit=MAX_READ_BYTES):
        try:
            with self.path(relative).open("r", encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        except (OSError, ValueError):
            return None

    def glob(self, relative, pattern):
        try:
            return sorted(self.path(relative).glob(pattern))
        except OSError:
            return []

    def exists(self, path):
        try:
            return path.exists() or path.is_symlink()
        except OSError:
            return False

    def available(self, program):
        """Whether an allowlisted diagnostic program is installed."""
        return program in ALLOWED_PROGRAMS and shutil.which(program) is not None


def _allowed(value, choices, default="unknown"):
    return value if value in choices else default


def _loaded_modules(probe):
    text = probe.read("/proc/modules", MAX_COMMAND_BYTES)
    if text is None:
        return None
    return {line.split()[0] for line in text.splitlines() if line.split()}


def boot_section(probe):
    cmdline = probe.read("/proc/cmdline", 4096) or ""
    tokens = set(cmdline.split())
    return {
        "mode": boot_mode.classify(probe.read("/etc/aios-mode", boot_mode.MAX_MODE_BYTES)),
        "recovery": "aios.recovery" in tokens,
        "install": "aios.install" in tokens,
        "nomodeset": "nomodeset" in tokens,
        "cmdline_readable": bool(cmdline),
    }


def _interfaces(probe):
    interfaces = []
    for index, entry in enumerate(probe.glob("/sys/class/net", "*")):
        if entry.name == "lo":
            continue
        wireless = probe.exists(entry / "wireless") or probe.exists(entry / "phy80211")
        operstate = (probe.read(f"/sys/class/net/{entry.name}/operstate", 32) or "").strip()
        carrier = (probe.read(f"/sys/class/net/{entry.name}/carrier", 32) or "").strip()
        interfaces.append({
            "index": index,
            "kind": "wireless" if wireless else "wired",
            "operstate": _allowed(operstate, OPERSTATES),
            "carrier": {"1": True, "0": False}.get(carrier),
        })
    return interfaces


def _rfkill(probe):
    hard = soft = False
    entries = 0
    for entry in probe.glob("/sys/class/rfkill", "rfkill*"):
        kind = _allowed((probe.read(f"/sys/class/rfkill/{entry.name}/type", 32) or "").strip(),
                        RFKILL_TYPES, "other")
        if kind != "wlan":
            continue
        entries += 1
        hard = hard or (probe.read(f"/sys/class/rfkill/{entry.name}/hard", 8) or "").strip() == "1"
        soft = soft or (probe.read(f"/sys/class/rfkill/{entry.name}/soft", 8) or "").strip() == "1"
    return {"wireless_entries": entries, "wireless_hard_blocked": hard,
            "wireless_soft_blocked": soft}


def _pci_network(probe):
    total = unbound = 0
    for entry in probe.glob("/sys/bus/pci/devices", "*"):
        device_class = (probe.read(f"/sys/bus/pci/devices/{entry.name}/class", 32) or "").strip()
        if not device_class.startswith("0x02"):
            continue
        total += 1
        if not probe.exists(entry / "driver"):
            unbound += 1
    return {"network_controllers": total, "network_controllers_without_driver": unbound}


def _firmware(probe):
    log = probe.run(["dmesg"])
    if log is None:
        return {"checked": False, "load_failures": 0}
    return {"checked": True, "load_failures": len(FIRMWARE_FAILURE.findall(log))}


def _manager(probe):
    general = probe.run(["nmcli", "-t", "-f", "RUNNING,STATE,CONNECTIVITY,WIFI-HW,WIFI", "general"])
    if general is None:
        return {"available": False, "state": "unknown", "connectivity": "unknown",
                "wifi_hardware_enabled": None, "wifi_enabled": None,
                "wifi_device_states": [], "wifi_devices": 0,
                "active_wifi_connections": None, "active_wired_connections": None}
    fields = (general.splitlines() or [""])[0].split(":")
    fields += [""] * (5 - len(fields))
    state = fields[1].strip().lower()
    state = "connected" if state.startswith("connected") else _allowed(
        state, ("connecting", "disconnected", "asleep"))
    devices = probe.run(["nmcli", "-t", "-f", "TYPE,STATE", "device", "status"]) or ""
    wifi_states = []
    for line in devices.splitlines()[:64]:
        kind, _, device_state = line.partition(":")
        if CONNECTION_TYPES.get(kind.strip().lower()) != "wireless":
            continue
        wifi_states.append(_device_state(device_state))
    active = _active_connections(probe)
    return {
        "available": True,
        "state": state,
        "connectivity": _allowed(fields[2].strip().lower(), CONNECTIVITY_STATES),
        "wifi_hardware_enabled": {"enabled": True, "disabled": False}.get(fields[3].strip().lower()),
        "wifi_enabled": {"enabled": True, "disabled": False}.get(fields[4].strip().lower()),
        "wifi_device_states": wifi_states[:MAX_LIST_ITEMS],
        "wifi_devices": len(wifi_states),
        "active_wifi_connections": active["wireless"],
        "active_wired_connections": active["wired"],
    }


def _active_connections(probe):
    """Count activated connections per fixed kind. Never reads a name or SSID.

    The field set is fixed to TYPE,STATE: `nmcli` is not asked for NAME, UUID,
    DEVICE or any other identifying column, and anything beyond the first two
    colon-separated fields of a line is discarded. Loopback and other kinds are
    counted as neither wireless nor wired, so a loopback or Ethernet connection
    can never stand in for a Wi-Fi one.
    """
    output = probe.run(["nmcli", "-t", "-f", "TYPE,STATE", "connection", "show", "--active"])
    if output is None:
        return {"wireless": None, "wired": None}
    counts = {"wireless": 0, "wired": 0}
    for line in output.splitlines()[:64]:
        raw_type, _, rest = line.partition(":")
        kind = CONNECTION_TYPES.get(raw_type.strip().lower())
        if kind is None:
            continue
        state = _allowed(rest.partition(":")[0].strip().lower(), CONNECTION_STATES)
        if state == "activated":
            counts[kind] += 1
    return counts


def _device_state(value):
    text = value.strip().lower()
    if "need authentication" in text:
        return "need_authentication"
    if "getting ip" in text:
        return "getting_ip"
    if text.startswith("connecting"):
        return "connecting"
    return _allowed(text, ("connected", "disconnected", "unavailable", "unmanaged",
                           "deactivating"))


def _ip(probe):
    route = probe.read("/proc/net/route", MAX_COMMAND_BYTES)
    default_route = None
    if route is not None:
        default_route = any(
            len(fields) > 2 and fields[1] == "00000000" and fields[0] != "Iface"
            for fields in (line.split() for line in route.splitlines()))
    resolv = probe.read("/etc/resolv.conf", MAX_READ_BYTES)
    nameservers = None if resolv is None else len([
        line for line in resolv.splitlines() if line.split()[:1] == ["nameserver"]])
    return {"default_route": default_route, "nameservers": nameservers}


def _dns(probe):
    if not probe.dns_probe:
        return {"performed": False, "resolved": None, "reason": "not_requested"}
    # A missing resolver tool is not a DNS failure; say so instead of guessing.
    if not probe.available("getent"):
        return {"performed": False, "resolved": None, "reason": "resolver_tool_unavailable"}
    # Fixed hostname, fixed command, bounded timeout. No result text is kept.
    resolved = probe.run(["getent", "hosts", DNS_PROBE_HOST]) is not None
    return {"performed": True, "resolved": resolved, "reason": "probe"}


def network_section(probe):
    modules = _loaded_modules(probe)
    interfaces = _interfaces(probe)
    evidence = {
        "interfaces": interfaces[:MAX_LIST_ITEMS],
        "wireless_interfaces": sum(entry["kind"] == "wireless" for entry in interfaces),
        "wired_interfaces": sum(entry["kind"] == "wired" for entry in interfaces),
        "modules": {
            "readable": modules is not None,
            "stack": sorted(set(WIRELESS_STACK) & modules)[:MAX_LIST_ITEMS] if modules else [],
            "wireless_drivers": sorted(set(WIRELESS_DRIVERS) & modules)[:MAX_LIST_ITEMS] if modules else [],
        },
        "rfkill": _rfkill(probe),
        "pci": _pci_network(probe),
        "firmware": _firmware(probe),
        "manager": _manager(probe),
        "ip": _ip(probe),
        "dns_probe": _dns(probe),
    }
    state, reason = classify_network(evidence)
    return {"state": state, "reason": reason, "evidence": evidence}


def classify_network(evidence):
    """Map sanitized evidence to one documented network state and reason.

    The documented states are Wi-Fi focused: they answer "why is Wi-Fi not
    working". An active loopback or Ethernet connection therefore never makes a
    disconnected radio read as connected. The single wired-capable verdict is
    the explicitly documented `route_present_without_network_manager` case,
    which applies only when NetworkManager itself cannot be queried.
    """
    rfkill = evidence["rfkill"]
    if rfkill["wireless_hard_blocked"]:
        return "rfkill_hard_block", "wireless_radio_hard_blocked"
    if rfkill["wireless_soft_blocked"]:
        return "rfkill_soft_block", "wireless_radio_soft_blocked"
    manager = evidence["manager"]
    firmware = evidence["firmware"]
    if not evidence["wireless_interfaces"]:
        if evidence["pci"]["network_controllers_without_driver"]:
            return "missing_module", "network_controller_without_driver"
        if evidence["modules"]["readable"] and not evidence["modules"]["stack"] \
                and evidence["pci"]["network_controllers"]:
            return "missing_module", "wireless_stack_not_loaded"
        if firmware["load_failures"]:
            return "missing_firmware", "firmware_load_failed"
        return "no_interface", "no_wireless_interface"
    if not manager["available"]:
        if evidence["ip"]["default_route"] and evidence["ip"]["nameservers"]:
            return "connected", "route_present_without_network_manager"
        return "unknown", "network_manager_unavailable"
    wifi_states = manager["wifi_device_states"]
    unavailable_wifi = "unavailable" in wifi_states
    if unavailable_wifi and firmware["load_failures"]:
        return "missing_firmware", "firmware_load_failed"
    if "need_authentication" in wifi_states:
        return "authentication_failure", "secrets_requested"
    if unavailable_wifi:
        return "disconnected", "wireless_device_unavailable"
    if not wifi_states:
        # The kernel exposes a radio that NetworkManager does not list at all.
        return "disconnected", "no_managed_wireless_device"
    if "connected" not in wifi_states:
        return "disconnected", "wireless_device_not_connected"
    # A None count means the active-connection query failed; fall back to the
    # device state rather than inventing either verdict.
    if manager["active_wifi_connections"] == 0:
        return "disconnected", "no_active_wireless_connection"
    if not evidence["ip"]["default_route"]:
        return "dhcp_failure", "no_default_route"
    if not evidence["ip"]["nameservers"]:
        return "dns_failure", "no_resolver_configured"
    if evidence["dns_probe"]["performed"] and not evidence["dns_probe"]["resolved"]:
        return "dns_failure", "resolution_failed"
    return "connected", "active_connection_with_route"


def _boot_freshness(probe, recorded_boot):
    """Compare the recorded boot identifier with this boot, without emitting it."""
    if not isinstance(recorded_boot, str) or not recorded_boot.strip():
        return "missing"
    current = (probe.read(BOOT_ID_PATH, 128) or "").strip()
    if not current:
        return "unverifiable"
    return "current" if recorded_boot.strip() == current else "previous"


def _unknown_renderer(boot="missing"):
    return {"available": False, "backend": "unknown", "renderer": "unknown",
            "accelerated": None, "recovery": None, "status": "unknown",
            "phase": "unknown", "boot": boot}


def _session_renderer(probe):
    """Renderer selection and startup progress recorded by aios-session.

    Strictly validated: the record must be this boot's, carry the current
    record version, and use only allowlisted enum values. The recorded boot
    identifier itself is never emitted; only its freshness is.
    """
    try:
        text = (probe.home / SESSION_STATE_DIR / RENDERER_STATE).read_text(
            encoding="utf-8", errors="replace")[:MAX_READ_BYTES]
        recorded = json.loads(text)
    except (OSError, ValueError):
        return _unknown_renderer()
    if not isinstance(recorded, dict) or recorded.get("version") != RENDERER_STATE_VERSION:
        return _unknown_renderer()
    boot = _boot_freshness(probe, recorded.get("boot_id"))
    backend = _allowed(recorded.get("backend"), ("glx", "xrender"))
    renderer = _allowed(recorded.get("renderer"), ("accelerated", "software", "unavailable"))
    accelerated = recorded.get("accelerated")
    recovery = recorded.get("recovery")
    status = _allowed(recorded.get("status"), SESSION_STATUSES)
    phase = _allowed(recorded.get("phase"), SESSION_PHASES)
    return {
        "available": boot == "current" and backend != "unknown" and renderer != "unknown"
                     and phase != "unknown",
        "backend": backend,
        "renderer": renderer,
        "accelerated": accelerated if isinstance(accelerated, bool) else None,
        "recovery": recovery if isinstance(recovery, bool) else None,
        "status": status,
        "phase": phase,
        "boot": boot,
    }


def _drm(probe):
    modules = _loaded_modules(probe)
    drivers = []
    cards = 0
    for entry in probe.glob("/sys/class/drm", "card*"):
        if "-" in entry.name:
            continue  # connector directory such as card0-eDP-1
        cards += 1
        link = entry / "device" / "driver"
        if not probe.exists(link):
            driver = "none"
        else:
            try:
                driver = _allowed(link.resolve().name, DRM_DRIVERS, "other")
            except OSError:
                driver = "other"
        if driver not in drivers:
            drivers.append(driver)
    card_nodes = len(probe.glob("/dev/dri", "card*"))
    render_nodes = len(probe.glob("/dev/dri", "renderD*"))
    readable = writable = False
    for node in probe.glob("/dev/dri", "card*") + probe.glob("/dev/dri", "renderD*"):
        readable = readable or os.access(node, os.R_OK)
        writable = writable or os.access(node, os.W_OK)
    return {
        "drm_drivers": sorted(drivers)[:MAX_LIST_ITEMS],
        "drm_modules": sorted(set(DRM_DRIVERS) & modules)[:MAX_LIST_ITEMS] if modules else [],
        "drm_cards": cards,
        "card_nodes": card_nodes,
        "render_nodes": render_nodes,
        "node_readable": readable,
        "node_writable": writable,
    }


def graphics_section(probe, boot):
    evidence = _drm(probe)
    evidence["session"] = _session_renderer(probe)
    evidence["recovery_mode"] = "recovery" if boot["recovery"] else "normal"
    state, reason = classify_graphics(evidence)
    return {"state": state, "reason": reason,
            "compositor": evidence["session"]["backend"],
            "session_phase": evidence["session"]["phase"],
            "session_state_boot": evidence["session"]["boot"],
            "recovery_mode": evidence["recovery_mode"], "evidence": evidence}


def classify_graphics(evidence):
    """Map sanitized graphics evidence to one documented state and reason.

    A recorded renderer *selection* is not a working desktop. Only a session
    that also recorded a surviving compositor (and, further on, its shell) can
    be reported as `accelerated` or `degraded_software`.
    """
    session = evidence["session"]
    if not session["available"]:
        if session["boot"] == "previous":
            return "unknown", "renderer_state_from_previous_boot"
        if session["boot"] == "unverifiable":
            return "unknown", "renderer_state_boot_unverified"
        if not evidence["card_nodes"]:
            return "unavailable", "no_drm_card_node"
        return "unknown", "no_session_renderer_state"
    if session["phase"] == "compositor_failed":
        return "startup_failed", "compositor_did_not_start"
    if session["phase"] == "shell_failed":
        return "startup_failed", "shell_did_not_start"
    if session["renderer"] == "unavailable":
        return "unavailable", "session_renderer_probe_failed"
    if session["phase"] not in ("compositor_ready", "shell_ready"):
        return "unknown", "renderer_selected_not_ready"
    if session["renderer"] == "accelerated":
        return "accelerated", "session_renderer_probe"
    reason = "recovery_software_path" if session["recovery"] else "session_renderer_probe"
    return "degraded_software", reason


def build_report(probe):
    boot = boot_section(probe)
    report = {
        "report": "aios-hardware",
        "version": REPORT_VERSION,
        "boot": boot,
        "network": network_section(probe),
        "graphics": graphics_section(probe, boot),
        "cpu": {"local_inference": cpu_features.inference_support(probe.path("/proc/cpuinfo"))},
        "notes": [
            "Sanitized: no SSIDs, MAC or IP addresses, hostnames, serial numbers, "
            "credentials, raw logs or arbitrary device strings.",
            "Point-in-time snapshot; a connection in progress can read as a failure.",
            "Evidence is not a hardware support claim.",
        ],
    }
    return _bounded(report)


def _bounded(report):
    """Keep the serialized report within MAX_REPORT_BYTES, dropping detail first."""
    if len(json.dumps(report)) <= MAX_REPORT_BYTES:
        return report
    report["network"]["evidence"]["interfaces"] = []
    report["notes"].append("Detail lists were dropped to stay within the report size bound.")
    if len(json.dumps(report)) <= MAX_REPORT_BYTES:
        return report
    return {"report": "aios-hardware", "version": REPORT_VERSION,
            "boot": report["boot"],
            "network": {"state": report["network"]["state"], "reason": report["network"]["reason"]},
            "graphics": {"state": report["graphics"]["state"], "reason": report["graphics"]["reason"],
                         "compositor": report["graphics"]["compositor"],
                         "session_phase": report["graphics"]["session_phase"],
                         "session_state_boot": report["graphics"]["session_state_boot"],
                         "recovery_mode": report["graphics"]["recovery_mode"]},
            "cpu": report["cpu"],
            "notes": ["Report truncated to its classifications to stay within the size bound."]}


def human(report):
    """Concise operator summary. Same classifications, no extra data."""
    network, graphics = report["network"], report["graphics"]
    cpu = report["cpu"]["local_inference"]
    lines = [
        "AIOS hardware report (sanitized)",
        "boot: mode={mode} recovery={recovery} nomodeset={nomodeset}".format(**report["boot"]),
        f"network: {network['state']} ({network['reason']})",
    ]
    evidence = network.get("evidence")
    if evidence:
        lines.append("  wireless interfaces: {wireless_interfaces}  wired: {wired_interfaces}".format(**evidence))
        lines.append("  rfkill: hard={wireless_hard_blocked} soft={wireless_soft_blocked}".format(**evidence["rfkill"]))
        lines.append("  managed wifi devices: {wifi_devices}  active wifi connections: {active_wifi_connections}".format(
            **evidence["manager"]))
        lines.append("  drivers: {}  firmware load failures: {} (checked: {})".format(
            ", ".join(evidence["modules"]["wireless_drivers"]) or "none",
            evidence["firmware"]["load_failures"], evidence["firmware"]["checked"]))
    lines.append(f"graphics: {graphics['state']} ({graphics['reason']})")
    lines.append("  compositor: {}  session: {}  state from: {} boot  recovery mode: {}".format(
        graphics["compositor"], graphics["session_phase"], graphics["session_state_boot"],
        graphics["recovery_mode"]))
    detail = graphics.get("evidence")
    if detail:
        lines.append("  drm drivers: {}  card nodes: {}  render nodes: {}".format(
            ", ".join(detail["drm_drivers"]) or "none", detail["card_nodes"], detail["render_nodes"]))
    if cpu["supported"] and cpu["detected"]:
        lines.append("cpu: local inference instruction-set floor met")
    elif cpu["supported"]:
        lines.append("cpu: " + cpu["reason"])
    else:
        lines.append("cpu: local inference unsupported; missing " + ", ".join(cpu["missing"]))
    lines.append("Recovery: reboot and choose the recovery boot entry; see docs/hardware-recovery.md")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aios-hardware-report",
        description="Bounded, sanitized network and graphics startup diagnostics.")
    parser.add_argument("--human", action="store_true", help="concise text summary instead of JSON")
    parser.add_argument("--dns-probe", action="store_true",
                        help=f"additionally resolve the fixed name {DNS_PROBE_HOST}")
    arguments = parser.parse_args(argv)
    try:
        report = build_report(Probe(dns_probe=arguments.dns_probe))
    except (OSError, ValueError) as exc:
        print(json.dumps({"report": "aios-hardware", "version": REPORT_VERSION,
                          "error": "Diagnostics failed: " + type(exc).__name__}), flush=True)
        return 2
    print(human(report) if arguments.human else json.dumps(report), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
