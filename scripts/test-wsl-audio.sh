#!/usr/bin/env bash
# AIOS Stage 0 audio transport diagnostics (issue #84).
#
# Runs INSIDE a WSL distribution (or the preview container) and reports each
# audio layer separately as "passed", "failed", or "not-tested" with an
# actionable cause. It never reports a generic "audio available" result.
#
# Modes:
#   check       Read-only transport survey. Never records, never plays.
#   playback    Play one short generated tone through the default sink.
#   record      Record at most --seconds (capped at 10) from a real
#               microphone source (monitor/loopback sources are rejected),
#               then run a signal-presence analysis on the captured bytes.
#   roundtrip   record, then play the recording back through the default sink.
#   --self-test Run the reporting logic against deterministic PATH stubs so
#               the stage model itself is testable on hosts with no audio.
#
# Safety contract:
#   - Check is read-only and never records.
#   - Recording modes announce capture and stop automatically within 10s.
#   - Task-specific temporary files are removed on success, failure, and
#     cancellation unless --keep-dir names a retention directory.
#   - A caller-supplied PULSE_SERVER is preserved when it answers;
#     unix:/mnt/wslg/PulseServer is used only after validating its socket.
#   - No audio daemon is installed or started; missing client tools are
#     reported as "not-tested" with the package to install.
set -u

SECONDS_LIMIT=10
REQUESTED_SECONDS=5
KEEP_DIR=""
SELF_TEST=0
MODE=""
WSLG_PULSE_PATH="${AIOS_WSLG_PULSE_SOCKET:-/mnt/wslg/PulseServer}"

die() {
  printf 'usage: %s [check|playback|roundtrip|record] [--seconds N] [--keep-dir DIR] | %s --self-test\n' \
    "$0" "$0" >&2
  printf '%s\n' "$1" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    check|playback|record|roundtrip)
      [ -z "$MODE" ] || die "only one mode may be selected"
      MODE="$1"
      ;;
    --seconds)
      [ "$#" -ge 2 ] || die "--seconds requires a value"
      REQUESTED_SECONDS="$2"
      shift
      ;;
    --keep-dir)
      [ "$#" -ge 2 ] || die "--keep-dir requires a value"
      KEEP_DIR="$2"
      shift
      ;;
    --self-test)
      SELF_TEST=1
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
  shift
done

case "$REQUESTED_SECONDS" in
  ''|*[!0-9]*) die "--seconds must be an integer between 1 and $SECONDS_LIMIT" ;;
esac
if [ "$REQUESTED_SECONDS" -lt 1 ] || [ "$REQUESTED_SECONDS" -gt "$SECONDS_LIMIT" ]; then
  die "--seconds must be between 1 and $SECONDS_LIMIT"
fi

STAGES=""
FAILURES=0
NOT_TESTED=0

stage() {
  # stage NAME passed|failed|not-tested [actionable cause]
  local name="$1" status="$2" cause="${3:-}"
  STAGES="${STAGES}${name}|${status}|${cause}
"
  case "$status" in
    failed) FAILURES=$((FAILURES + 1)) ;;
    not-tested) NOT_TESTED=$((NOT_TESTED + 1)) ;;
  esac
  if [ -n "$cause" ]; then
    printf 'STAGE %s %s: %s\n' "$name" "$status" "$cause"
  else
    printf 'STAGE %s %s\n' "$name" "$status"
  fi
}

have() {
  # AIOS_FAKE_HAVE=0 forces every tool probe to report missing so the
  # --self-test "client tools absent" scenario is deterministic on hosts
  # that actually have pactl installed.
  [ "${AIOS_FAKE_HAVE:-1}" = "1" ] || return 1
  command -v "$1" >/dev/null 2>&1
}

validate_pulse_endpoint() {
  # Print the endpoint only if it is usable; return nonzero otherwise.
  local candidate="$1"
  case "$candidate" in
    unix:*)
      local path="${candidate#unix:}"
      [ -S "$path" ] || return 1
      ;;
  esac
  PULSE_SERVER="$candidate" pactl info >/dev/null 2>&1 || return 1
  printf '%s' "$candidate"
}

survey_transport() {
  if ! have pactl; then
    stage pulseaudio_client not-tested "install the PulseAudio client utilities (Ubuntu/Debian: pulseaudio-utils; Alpine: pulseaudio-utils); do not start a competing audio daemon"
    return
  fi
  local resolved="" source_label=""
  if [ -n "${PULSE_SERVER:-}" ]; then
    resolved="$(validate_pulse_endpoint "$PULSE_SERVER")" && source_label="preserved inherited PULSE_SERVER"
  fi
  if [ -z "$resolved" ] && [ -S "$WSLG_PULSE_PATH" ]; then
    resolved="$(validate_pulse_endpoint "unix:$WSLG_PULSE_PATH")" && source_label="validated WSLg fallback socket"
  fi
  if [ -n "$resolved" ]; then
    export PULSE_SERVER="$resolved"
    stage transport_endpoint passed "using ${source_label} (${resolved})"
    if pactl info >/dev/null 2>&1; then
      stage pulseaudio_server passed
    else
      stage pulseaudio_server failed "endpoint existed but stopped answering the query; restart the WSL distribution session rather than shutting down every distribution"
    fi
  else
    if [ -n "${PULSE_SERVER:-}" ]; then
      stage transport_endpoint failed "supplied PULSE_SERVER=${PULSE_SERVER} did not answer and no WSLg fallback socket exists; a missing /dev/snd alone does not mean the WSLg bridge is broken"
    elif [ ! -S "$WSLG_PULSE_PATH" ]; then
      stage transport_endpoint failed "no PULSE_SERVER was supplied and unix:${WSLG_PULSE_PATH} does not exist; confirm WSLg is enabled (wsl --version shows WSLg support) and start one GUI app to initialize it"
    else
      stage transport_endpoint failed "the WSLg Pulse socket exists but rejected the query; ensure the distro is WSL2 and the user session owns ${WSLG_PULSE_PATH}"
    fi
    stage pulseaudio_server not-tested "no validated endpoint"
  fi
}

survey_devices() {
  if ! have pactl; then
    stage audio_sinks not-tested "PulseAudio client tools missing"
    stage microphone_source not-tested "PulseAudio client tools missing"
    return
  fi
  local sinks sources mics monitors names
  sinks="$(pactl list short sinks 2>/dev/null || true)"
  sources="$(pactl list short sources 2>/dev/null || true)"
  if [ -n "$sinks" ]; then
    stage audio_sinks passed
  else
    stage audio_sinks failed "no Pulse sink is registered; open Windows Sound settings, select an output device, unmute it, then re-run check"
  fi
  monitors="$(printf '%s\n' "$sources" | grep -i 'monitor' || true)"
  mics="$(printf '%s\n' "$sources" | grep -vi 'monitor' | grep -v '^[[:space:]]*$' || true)"
  if [ -n "$mics" ]; then
    names="$(printf '%s\n' "$mics" | awk '{print $2}' | paste -sd ',' -)"
    stage microphone_source passed "candidate sources: ${names}"
  elif [ -n "$monitors" ]; then
    stage microphone_source failed "only monitor/loopback sources are present; a monitor source is not microphone capture; connect an input device and allow microphone access for desktop apps (Windows Settings > Privacy > Microphone)"
  else
    stage microphone_source failed "no Pulse source is registered; check the Windows input device selection and microphone privacy permissions before WSL can see a capture source"
  fi
}

cleanup_tmp() {
  if [ -n "${TASK_WAV:-}" ] && [ -f "$TASK_WAV" ]; then
    if [ -n "$KEEP_DIR" ]; then
      mkdir -p "$KEEP_DIR" 2>/dev/null || true
      local retained="$KEEP_DIR/$(basename "$TASK_WAV")"
      mv "$TASK_WAV" "$retained" 2>/dev/null && printf 'RETAINED %s\n' "$retained"
    fi
    rm -f "$TASK_WAV" 2>/dev/null || true
  fi
}

capture_tmp() {
  # One task-specific temporary file, owner-only.
  TASK_WAV="$(mktemp "${TMPDIR:-/tmp}/aios-audio-${MODE}-$$-XXXXXX.wav")"
  chmod 600 "$TASK_WAV"
}

synthesize_tone() {
  # Writes a short 440 Hz WAV tone; used for the playback probe only.
  python3 - "$1" <<'PY'
import math, struct, sys, wave
path = sys.argv[1]
rate, seconds, freq = 8000, 0.4, 440
with wave.open(path, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(int(rate * seconds))
    )
    w.writeframes(frames)
PY
}

playback_tone() {
  if ! have aplay; then
    stage playback not-tested "install alsa-utils (provides aplay) in the distro; it is a client tool, not an audio daemon"
    return
  fi
  if ! have python3; then
    stage playback not-tested "python3 is required to synthesize the probe tone; install python3 or use the record mode instead"
    return
  fi
  capture_tmp
  if ! synthesize_tone "$TASK_WAV" 2>/dev/null; then
    stage playback failed "could not synthesize the probe tone"
    return
  fi
  local stderr_log stderr_text
  stderr_log="$(mktemp "${TMPDIR:-/tmp}/aios-audio-play-$$.XXXXXX")"
  if timeout 10 aplay -q "$TASK_WAV" > /dev/null 2>"$stderr_log"; then
    stage playback passed "played a 0.4s tone; confirm you actually heard it (human check)"
  else
    stderr_text="$(head -n 1 "$stderr_log" 2>/dev/null || true)"
    stage playback failed "aplay rejected playback: ${stderr_text:-unknown error}; verify the Windows default output device is selected and unmuted, then re-run check"
  fi
  rm -f "$stderr_log"
}

mic_source_available() {
  pactl list short sources 2>/dev/null | grep -vi 'monitor' | grep -q '[^[:space:]]'
}

analyze_capture() {
  local size data nonzero ratio
  size="$(wc -c < "$TASK_WAV" | tr -d '[:space:]')"
  if [ "${size:-0}" -le 44 ]; then
    stage capture failed "recorded an empty file; the source is likely silent or muted rather than absent"
    return
  fi
  data=$((size - 44))
  nonzero="$(tr -d '\0' < "$TASK_WAV" | wc -c | tr -d '[:space:]')"
  # Signal-presence heuristic: the nonzero byte share of the data region. A
  # muted or silent capture is almost entirely zero bytes. This proves signal
  # presence only, never transcription quality.
  ratio=$((nonzero * 100 / data))
  if [ "$ratio" -ge 1 ]; then
    stage capture passed "signal present (${ratio}% nonzero bytes over ${REQUESTED_SECONDS}s; signal-presence only, not transcription)"
  else
    stage capture failed "capture is all-silence; unmute the Windows input device and allow microphone access for desktop apps, then re-run"
  fi
}

record_capture() {
  if ! have arecord; then
    stage capture not-tested "install alsa-utils (provides arecord) in the distro"
    return
  fi
  if have pactl && ! mic_source_available; then
    stage capture failed "no microphone source passed the transport survey; refusing to record from a monitor/loopback source"
    return
  fi
  capture_tmp
  echo "[aios] RECORDING for ${REQUESTED_SECONDS}s from the default input; capturing audible sound; stopping automatically."
  local stderr_log stderr_text
  stderr_log="$(mktemp "${TMPDIR:-/tmp}/aios-audio-rec-$$.XXXXXX")"
  if timeout $((REQUESTED_SECONDS + 5)) arecord -q -r 16000 -f S16_LE -c 1 -d "$REQUESTED_SECONDS" -t wav "$TASK_WAV" 2>"$stderr_log"; then
    analyze_capture
  else
    stderr_text="$(head -n 1 "$stderr_log" 2>/dev/null || true)"
    stage capture failed "arecord exited nonzero: ${stderr_text:-unknown error}; distinguish permission denial (device busy/EPERM) from missing hardware in the message above"
  fi
  rm -f "$stderr_log"
}

run_self_test() {
  local stub_dir
  stub_dir="$(mktemp -d "${TMPDIR:-/tmp}/aios-audio-selftest.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '$stub_dir'" EXIT

  cat > "$stub_dir/pactl" <<'STUB'
#!/usr/bin/env bash
[ "${AIOS_FAKE_PACTL:-ok}" = "ok" ] || exit 1
if [ "${1:-}" = "info" ]; then
  printf 'Server String: %s\nDefault Sink: alsa_output.pulse\n' "${PULSE_SERVER:-direct}"
  exit 0
fi
if [ "${1:-}" = "list" ] && [ "${2:-}" = "short" ]; then
  case "${3:-}" in
    sinks) printf '%s\n' "${AIOS_FAKE_SINKS-}"; exit 0 ;;
    sources) printf '%s\n' "${AIOS_FAKE_SOURCES-}"; exit 0 ;;
  esac
fi
exit 0
STUB
  chmod +x "$stub_dir/pactl"
  PATH="$stub_dir:$PATH"
  export PATH

  # Scenario A: preserved inherited endpoint over a healthy server with a
  # real microphone next to a monitor source.
  rm -f "$stub_dir/pactl.missing"
  export AIOS_FAKE_PACTL=ok
  export AIOS_FAKE_SINKS="1 alsa_output.pulse-standby"
  export AIOS_FAKE_SOURCES="1 alsa_input.usb-Brio-mic.capture
2 alsa_output.pulse.monitor"
  export PULSE_SERVER="127.0.0.1:4713"
  export AIOS_WSLG_PULSE_SOCKET="$stub_dir/absent-socket"
  FAILURES=0; NOT_TESTED=0; STAGES=""
  survey_transport
  survey_devices
  [ "$FAILURES" = 0 ] || { echo "SELFTEST:FAIL scenario A reported failures"; exit 1; }
  printf '%s' "$STAGES" | grep -q '^transport_endpoint|passed' \
    || { echo "SELFTEST:FAIL scenario A endpoint"; exit 1; }
  printf '%s' "$STAGES" | grep -q '^microphone_source|passed' \
    || { echo "SELFTEST:FAIL scenario A microphone"; exit 1; }

  # Scenario B: monitor-only sources must fail exactly the microphone stage.
  export AIOS_FAKE_SOURCES="2 alsa_output.pcie.monitor"
  FAILURES=0; NOT_TESTED=0; STAGES=""
  survey_devices
  [ "$FAILURES" = 1 ] \
    || { echo "SELFTEST:FAIL scenario B must fail exactly the microphone stage"; exit 1; }
  printf '%s' "$STAGES" | grep -q '^microphone_source|failed' \
    || { echo "SELFTEST:FAIL scenario B microphone"; exit 1; }
  printf '%s' "$STAGES" | grep -q 'monitor/loopback' \
    || { echo "SELFTEST:FAIL scenario B cause wording"; exit 1; }

  # Scenario C: a dead server behind a supplied endpoint fails the transport
  # stage with an actionable cause instead of a generic "audio unavailable".
  export AIOS_FAKE_PACTL=dead
  FAILURES=0; NOT_TESTED=0; STAGES=""
  survey_transport
  printf '%s' "$STAGES" | grep -q '^transport_endpoint|failed' \
    || { echo "SELFTEST:FAIL scenario C endpoint failure"; exit 1; }
  printf '%s' "$STAGES" | grep -q '^pulseaudio_server|not-tested' \
    || { echo "SELFTEST:FAIL scenario C server not-tested"; exit 1; }

  # Scenario D: missing client tools are not-tested, never failed.
  export AIOS_FAKE_HAVE=0
  FAILURES=0; NOT_TESTED=0; STAGES=""
  survey_transport
  survey_devices
  [ "$FAILURES" = 0 ] \
    || { echo "SELFTEST:FAIL scenario D must not count missing tools as failures"; exit 1; }
  [ "$NOT_TESTED" = 3 ] \
    || { echo "SELFTEST:FAIL scenario D not-tested count"; exit 1; }
  printf '%s' "$STAGES" | grep -q '^pulseaudio_client|not-tested' \
    || { echo "SELFTEST:FAIL scenario D client stage"; exit 1; }

  echo "SELFTEST:PASS"
}

if [ "$SELF_TEST" = 1 ]; then
  run_self_test
  exit $?
fi

if [ -z "$MODE" ]; then
  MODE="check"
fi

trap 'cleanup_tmp' EXIT
trap 'cleanup_tmp; exit 130' INT TERM

printf 'AIOS WSL audio diagnostics: mode=%s host=%s user=%s\n' \
  "$MODE" "$(uname -sr)" "$(id -un)"
if [ -n "${PULSE_SERVER:-}" ]; then
  printf 'inherited PULSE_SERVER=%s (preserved when it answers)\n' "$PULSE_SERVER"
fi

survey_transport
survey_devices

if [ "$MODE" = "check" ]; then
  : # read-only: never plays, never records
elif [ "$MODE" = "playback" ]; then
  playback_tone
elif [ "$MODE" = "record" ]; then
  record_capture
elif [ "$MODE" = "roundtrip" ]; then
  record_capture
  if [ -n "${TASK_WAV:-}" ] && [ -f "$TASK_WAV" ]; then
    if have aplay; then
      if timeout 10 aplay -q "$TASK_WAV" > /dev/null 2>&1; then
        stage roundtrip_playback passed "played the recording back; confirm you heard your own voice (human check)"
      else
        stage roundtrip_playback failed "could not play the captured file back through the default sink"
      fi
    else
      stage roundtrip_playback not-tested "aplay missing (alsa-utils)"
    fi
  else
    stage roundtrip_playback not-tested "no capture was produced"
  fi
fi

printf 'SUMMARY mode=%s failures=%d not-tested=%d\n' "$MODE" "$FAILURES" "$NOT_TESTED"
if [ "$FAILURES" -gt 0 ]; then
  printf 'RESULT %s incomplete\n' "$MODE"
  exit 1
fi
printf 'RESULT %s ok\n' "$MODE"
