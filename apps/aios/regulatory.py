"""Bounded Wi-Fi regulatory-domain adapter shared by os_settings and the CLI.

Mirrors the machine_clock pattern: the unprivileged side validates the fixed
request shape and hands it to one no-argument root helper over doas. No shell,
no free-form arguments, and no other regulatory tooling is exposed.
"""
import json
import re
import subprocess
import sys


COUNTRY_PATTERN = r"[A-Z]{2}"
HELPER = "/usr/local/sbin/aios-regdomain"


def parse_country(value):
    """ISO 3166-1 alpha-2 country code, or the world domain 00."""
    if not isinstance(value, str) or not re.fullmatch(COUNTRY_PATTERN + "|00", value):
        raise ValueError("Use a two-letter ISO 3166-1 alpha-2 country code such as US, or 00 for the world domain.")
    return value


def validate(request):
    if not isinstance(request, dict):
        raise ValueError("Invalid Wi-Fi country request.")
    if request == {"action": "read"}:
        return
    if set(request) == {"action", "value"} and request["action"] == "set":
        parse_country(request["value"])
        return
    raise ValueError("Only Wi-Fi country read or set with a two-letter code is supported.")


def request_domain(request):
    validate(request)
    try:
        process = subprocess.run(
            ["/usr/bin/doas", "-n", HELPER], input=json.dumps(request) + "\n",
            text=True, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Wi-Fi regulatory service is unavailable or timed out. Read the country before retrying.") from None
    try:
        result = json.loads(process.stdout)
    except ValueError:
        raise RuntimeError("Wi-Fi regulatory access denied or helper unavailable. Only the authorized AIOS desktop user can change the Wi-Fi country.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Invalid Wi-Fi regulatory response.")
    if "error" in result:
        raise RuntimeError(result["error"])
    if process.returncode != 0:
        raise RuntimeError("Wi-Fi regulatory operation failed. Read the country before retrying.")
    return result


def main():
    try:
        line = sys.stdin.buffer.readline(513)
        if len(line) > 512:
            raise ValueError("Wi-Fi country request is too large.")
        result = request_domain(json.loads(line))
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
