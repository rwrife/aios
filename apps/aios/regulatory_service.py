"""Root-only implementation for the fixed, no-argument aios-regdomain helper.

Reads and sets the kernel's Wi-Fi regulatory domain through `iw`, and persists
it only where persistence is real: an installed system keeps a cfg80211 module
option, a live session's /etc is a tmpfs and is reported as non-persistent.

A set is reported in three distinct ways, following the machine-clock pattern
of separating the runtime change from its persistence: `not_applicable_live`
(live session, nothing to write), `saved` (installed, written and read back),
and `write_failed` (installed, the kernel change applied but the configuration
could not be stored). A write failure is never described as a live session.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from . import boot_mode
from .regulatory import parse_country, validate

IW = "/usr/sbin/iw"
MODPROBE_CONF = Path("/etc/modprobe.d/aios-cfg80211.conf")
MODE_FILE = Path(boot_mode.MODE_FILE)
COUNTRY_LINE = re.compile(r"^country ([A-Z0-9]{2}):", re.M)
# Fixed persistence outcomes for one set request.
PERSISTENCE_RESULTS = ("not_applicable_live", "saved", "write_failed")


def _iw(*arguments):
    try:
        result = subprocess.run([IW, *arguments], capture_output=True, text=True,
                                timeout=5, check=False, env={"LC_ALL": "C", "PATH": "/usr/sbin:/sbin:/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout[:65536]


def installed():
    """Whether this is an installed system, via the shared /etc/aios-mode marker."""
    return boot_mode.installed(str(MODE_FILE))


def persisted_country():
    try:
        text = MODPROBE_CONF.read_text()[:4096]
    except OSError:
        return None
    match = re.search(r"ieee80211_regdom=([A-Z0-9]{2})", text)
    return match.group(1) if match else None


def kernel_country():
    output = _iw("reg", "get")
    if output is None:
        return None
    match = COUNTRY_LINE.search(output)
    return match.group(1) if match else None


def read_domain():
    country = kernel_country()
    if country is None:
        raise RuntimeError("Wi-Fi regulatory support is unavailable: the cfg80211 driver is not loaded or iw is missing.")
    is_installed = installed()
    return {
        "country": country,
        "world_domain": country == "00",
        "persisted_country": persisted_country(),
        "persistent": is_installed,
        "mode": "installed" if is_installed else "live",
        "values": "Two-letter ISO 3166-1 alpha-2 code, or 00 for the world domain.",
        "persistence": ("Installed systems keep the country in /etc/modprobe.d/aios-cfg80211.conf and reapply it when cfg80211 loads at boot."
                        if is_installed else
                        "This live session stores nothing on disk: the country applies until reboot. Install AIOS to keep it."),
    }


def persist(country):
    """Write the cfg80211 module option.

    Returns one of PERSISTENCE_RESULTS: a live session has nothing to persist,
    an installed system either stores the option or fails to. A failure here is
    reported as a failure, never as a live session.
    """
    if not installed():
        return "not_applicable_live"
    try:
        MODPROBE_CONF.parent.mkdir(parents=True, exist_ok=True)
        MODPROBE_CONF.write_text(f"# Managed by AIOS: Wi-Fi regulatory country.\noptions cfg80211 ieee80211_regdom={country}\n")
        MODPROBE_CONF.chmod(0o644)
    except OSError:
        # The kernel change already applied; the stored configuration did not.
        return "write_failed"
    return "saved"


NOTICES = {
    "saved": "Wi-Fi country applied and saved for the next boot.",
    "not_applicable_live": ("Wi-Fi country applied to the running kernel only; it resets at reboot "
                            "in a live session. Install AIOS to keep it."),
    "write_failed": ("Wi-Fi country applied to the running kernel, but this installed system could "
                     "not save it to /etc/modprobe.d/aios-cfg80211.conf. The radio is using the new "
                     "country now and will fall back to the stored or default country at the next "
                     "boot. Check that the root filesystem is writable, then set it again."),
}


def handle(request):
    validate(request)
    if request["action"] == "read":
        return read_domain()
    country = parse_country(request["value"])
    if kernel_country() is None:
        raise RuntimeError("Wi-Fi regulatory support is unavailable: the cfg80211 driver is not loaded or iw is missing.")
    if _iw("reg", "set", country) is None:
        raise RuntimeError("The kernel refused this regulatory country. The Wi-Fi country was not changed.")
    state = read_domain()
    if state["country"] != country:
        # The kernel silently keeps its domain for codes the regulatory
        # database does not contain; never report an unverified change.
        raise RuntimeError("The kernel did not adopt this country code. Check that it is a valid ISO 3166-1 alpha-2 code present in the wireless regulatory database.")
    persistence = persist(country)
    state["persisted_country"] = persisted_country()
    return {
        "updated": "wifi_country",
        "state": state,
        "applied_to_kernel": True,
        "persistence_result": persistence,
        "saved_for_next_boot": persistence == "saved",
        # A failed write on an installed system is a partial result: the
        # runtime change stands, the stored configuration does not.
        "partial": persistence == "write_failed",
        # Not the transport-level "error" key: the operation partly succeeded,
        # so the adapter must deliver this result rather than raise it away.
        "persistence_error": ("The Wi-Fi country was applied to the running kernel but could not be "
                              "saved on this installed system."
                              if persistence == "write_failed" else None),
        "notice": NOTICES[persistence],
    }


def main():
    try:
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise RuntimeError("The Wi-Fi regulatory helper requires authorized guest root access and no arguments.")
        if "ID=aios" not in Path("/etc/os-release").read_text().splitlines():
            raise RuntimeError("The Wi-Fi regulatory helper is available only inside AIOS.")
        line = sys.stdin.buffer.readline(513)
        if len(line) > 512:
            raise ValueError("Wi-Fi country request is too large.")
        request = json.loads(line)
        # Serialize regulatory changes and their readback across chats.
        with open("/run/aios-regdomain.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = handle(request)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    except OSError:
        print(json.dumps({"error": "Wi-Fi regulatory service failed. Read the country before retrying; a change may already have applied."}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0
