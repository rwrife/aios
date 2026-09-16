"""Single source for the live/installed marker written to /etc/aios-mode.

The live apkovl ships the file containing `live`; `aios-install` overwrites it
with `installed` on the target root. Everything that needs to know whether the
running system can persist state (regulatory persistence, diagnostics, the
chat's live badge) reads it through this helper so the semantics cannot drift.
"""

MODE_FILE = "/etc/aios-mode"
MODES = ("live", "installed")
UNKNOWN = "unknown"
MAX_MODE_BYTES = 64


def classify(text):
    """Map raw marker text to `live`, `installed` or `unknown`."""
    value = (text or "").strip()
    return value if value in MODES else UNKNOWN


def read_mode(path=MODE_FILE):
    """Read the marker. An unreadable or unexpected marker is `unknown`."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_MODE_BYTES)
    except OSError:
        return UNKNOWN
    return classify(text)


def installed(path=MODE_FILE):
    """Whether this system is an installed one, i.e. can persist /etc."""
    return read_mode(path) == "installed"
