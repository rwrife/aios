"""Bounded guest clock adapter shared by Settings and os_settings."""
import datetime
import json
import re
import subprocess
import sys


DATETIME_PATTERN = r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})"
HELPER = "/usr/local/sbin/aios-clock"


def parse_datetime(value):
    if not isinstance(value, str) or not re.fullmatch(DATETIME_PATTERN, value):
        raise ValueError("Use YYYY-MM-DDTHH:MM:SSZ or YYYY-MM-DDTHH:MM:SS+HH:MM (2000-2099).")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = value[-6:]
        if value[-1] != "Z" and (int(offset[1:3]) > 14 or int(offset[4:]) > 59
                                 or (int(offset[1:3]) == 14 and int(offset[4:]) != 0)
                                 or offset == "-00:00"):
            raise ValueError
        utc = parsed.astimezone(datetime.timezone.utc)
        if not 2000 <= utc.year <= 2099:
            raise ValueError
    except ValueError:
        raise ValueError("Invalid calendar date, time or UTC offset; use 2000-2099 and an offset within +/-14:00.") from None
    return utc


def validate(request):
    if not isinstance(request, dict):
        raise ValueError("Invalid clock request.")
    if request == {"action": "read"}:
        return
    if set(request) == {"action", "value"} and request["action"] == "set":
        parse_datetime(request["value"])
        return
    raise ValueError("Only clock read or set with an explicit date-time is supported.")


def request_clock(request):
    validate(request)
    try:
        process = subprocess.run(
            ["/usr/bin/doas", "-n", HELPER], input=json.dumps(request) + "\n",
            text=True, capture_output=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Guest clock service is unavailable or timed out. Read the clock before retrying.") from None
    try:
        result = json.loads(process.stdout)
    except ValueError:
        raise RuntimeError("Guest clock access denied or helper unavailable. Only the authorized AIOS desktop user can change the clock.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Invalid guest clock response.")
    if "error" in result:
        raise RuntimeError(result["error"])
    if process.returncode != 0:
        raise RuntimeError("Guest clock operation failed. Read the clock before retrying.")
    return result


def main():
    try:
        line = sys.stdin.buffer.readline(513)
        if len(line) > 512:
            raise ValueError("Clock request is too large.")
        result = request_clock(json.loads(line))
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
