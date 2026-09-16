"""Shared CPU preflight for the bundled local inference binaries.

scripts/build-apps.sh configures llama.cpp and whisper.cpp with
-DGGML_NATIVE=OFF. In ggml that leaves the explicit instruction-set options at
their enabled default (SSE4.2, AVX, AVX2, BMI2, FMA, F16C), so llama-server,
llama-cli and whisper-cli contain unconditional AVX2-era code and die with
SIGILL on older CPUs. Every launch path calls this helper first so an
unsupported CPU gets a structured limitation and still reaches the desktop.

This is a launch preflight, not a baseline inference implementation: nothing
here makes local inference work on a CPU below the floor. Remote and
subscription providers are unaffected.
"""
from pathlib import Path

CPUINFO = Path("/proc/cpuinfo")
# /proc/cpuinfo spellings of the instruction sets the bundled build requires.
REQUIRED_FEATURES = ("sse4_2", "avx", "avx2", "bmi2", "f16c", "fma")
LIMITATION = ("This CPU does not support the instruction set the bundled local "
              "inference build requires, so local models cannot run here. The "
              "desktop and remote model providers are unaffected.")
UNDETECTED = "CPU features could not be read, so local inference was not preflighted."


def parse_flags(text):
    """Flags from /proc/cpuinfo text, or None when no flags line is present."""
    flags = None
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "flags":
            flags = set(value.split())
            break
    return flags


def read_flags(path=CPUINFO):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    return parse_flags(text)


def inference_support(path=CPUINFO):
    """Structured local-inference support state. Never raises."""
    flags = read_flags(path)
    if flags is None:
        return {"supported": True, "detected": False, "required": list(REQUIRED_FEATURES),
                "missing": [], "reason": UNDETECTED}
    missing = [name for name in REQUIRED_FEATURES if name not in flags]
    return {"supported": not missing, "detected": True, "required": list(REQUIRED_FEATURES),
            "missing": missing, "reason": "" if not missing else LIMITATION}


def limitation_message(state):
    """Actionable one-line message for an unsupported CPU."""
    return LIMITATION + " Missing CPU features: " + ", ".join(state["missing"]) + "."


def ensure_supported(path=CPUINFO):
    """Raise before launching a bundled inference binary on an unsupported CPU."""
    state = inference_support(path)
    if not state["supported"]:
        raise RuntimeError(limitation_message(state))
    return state
