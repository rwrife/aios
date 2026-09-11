"""Root-only implementation for the fixed, no-argument aios-clock helper."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .machine_clock import parse_datetime, validate


def sync_daemons():
    names = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            name = (entry / "comm").read_text().strip()
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Alpine's supported time services; do not stop an administrator's daemon.
        if name in ("ntpd", "chronyd", "systemd-timesyn"):
            names.add(name)
    return sorted(names)


def read_clock():
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "local": now.astimezone().isoformat(timespec="seconds"),
        "timezone": str(now.astimezone().tzinfo),
        "sync_daemons": sync_daemons(),
        "range": "2000-01-01T00:00:00Z through 2099-12-31T23:59:59Z",
        "persistence": "Changes apply to the guest system clock. Set results report whether the UTC hardware clock was also saved; VM RTC policy can override it on reboot.",
    }


def handle(request):
    validate(request)
    if request["action"] == "read":
        return read_clock()
    instant = parse_datetime(request["value"])
    if sync_daemons():
        raise RuntimeError("Automatic time synchronization is running. An administrator must stop it before setting the clock manually.")
    try:
        time.clock_settime(time.CLOCK_REALTIME, instant.timestamp())
    except OSError:
        raise RuntimeError("Permission denied or kernel clock unavailable; the system clock was not changed.") from None
    saved = False
    try:
        saved = subprocess.run(
            ["/sbin/hwclock", "--systohc", "--utc"], capture_output=True,
            timeout=3, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass  # The system clock already changed; report the persistence failure.
    return {
        "updated": "date_time",
        "state": read_clock(),
        "hardware_clock_saved": saved,
        "notice": ("System clock updated and saved to the UTC hardware clock. VM RTC policy may override it at reboot."
                   if saved else "System clock updated, but the hardware clock could not be saved. The change may be lost at reboot."),
    }


def main():
    try:
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise RuntimeError("The clock helper requires authorized guest root access and no arguments.")
        if "ID=aios" not in Path("/etc/os-release").read_text().splitlines():
            raise RuntimeError("The clock helper is available only inside AIOS.")
        line = sys.stdin.buffer.readline(513)
        if len(line) > 512:
            raise ValueError("Clock request is too large.")
        request = json.loads(line)
        # Serialize manual updates and their readback across chats.
        with open("/run/aios-clock.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = handle(request)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    except OSError:
        print(json.dumps({"error": "Guest clock service failed. Read the clock before retrying; a change may already have applied."}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0
