# WSL / preview / QEMU audio validation (Stage 0)

Issue #84 foundation for all wake-word and spoken-response work. The goal is
to identify the **exact layer** where audio fails, never a generic
"audio available". These tools report each layer separately as `passed`,
`failed`, or `not-tested` with an actionable cause.

Status legend used by every stage line:

- `passed` - the layer was exercised and behaved.
- `failed` - the layer was exercised and misbehaved; the printed cause names
  the fix.
- `not-tested` - a dependency for that probe is missing; the cause names the
  package or prerequisite to install. A missing probe is never silently
  treated as a pass.

## Tooling contract

| Tool | Runs on | Purpose |
| --- | --- | --- |
| `scripts/wsl-audio.ps1` | Windows PowerShell | Selects the distro, checks the Windows device/privacy layers, then delegates the transport probe into WSL. |
| `scripts/test-wsl-audio.sh` | Inside a WSL distro or the preview container | Pulse endpoint validation, sink/source survey, bounded tone playback, bounded microphone recording with signal-presence analysis. |
| `scripts/preview-chat.sh [--desktop]` | WSLg + the Alpine preview container | Runs the built desktop shell (orb included) without rebuilding an ISO. |
| `scripts/run.ps1 -Name ...` | Windows | Boots the real ISO with a task-specific QEMU window title. |

Guarantees the wrappers make:

- `Check` is read-only: it never records and never plays.
- `Record` and `RoundTrip` announce capture on the console and stop
  automatically after at most 10 seconds (`--seconds`/`-Seconds` caps at 10).
- Recordings go to one task-specific owner-only temporary file removed on
  success, failure, and cancellation. Only an explicit `-KeepRecording`
  retains the file, in a per-run directory, for the operator to delete.
- A valid caller-supplied `PULSE_SERVER` is preserved verbatim;
  `unix:/mnt/wslg/PulseServer` is used only after its socket is validated.
- Monitor/loopback sources are detected and never counted as microphone
  capture.
- Nothing installs or starts a competing audio daemon, exposes unauthenticated
  TCP audio, restarts all WSL distributions, or terminates other development
  sessions.

## Layer order

1. **Windows devices and privacy** (`windows_audio_devices`,
   `windows_microphone_privacy`): a working sound device exists and the
   machine-level microphone consent allows desktop apps. Distinguish
   permission denial from missing hardware. Select the intended Windows
   default output/input manually in Sound settings and unmute it.
2. **WSL / WSLg** (`wsl_distro`, `wslg_bridge`): the distro is reachable and
   `/mnt/wslg/PulseServer` exists (WSL2 + WSLg enabled; start one GUI app once
   to initialize WSLg). A missing `/dev/snd` alone does **not** mean the WSLg
   bridge is broken.
3. **Transport endpoint** (`pulseaudio_client`, `transport_endpoint`,
   `pulseaudio_server`): install client tools only —
   `sudo apt install pulseaudio-utils alsa-utils` (Ubuntu/Debian) or
   `apk add pulseaudio-utils alsa-utils` (Alpine). Do not install or start a
   competing PulseAudio daemon inside the distro; WSLg provides the server.
4. **Devices inside the endpoint** (`audio_sinks`, `microphone_source`):
   at least one sink is registered and at least one non-monitor source exists.
   A monitor-only survey means Windows sees no live input device (or privacy
   blocks it), not that capture will "work anyway".
5. **The requested probe** (`playback`, `capture`, `roundtrip_playback`):
   see the modes below. Signal analysis proves signal presence only — it is
   not transcription quality.

## Runbook

From the repository root in PowerShell:

```powershell
# Read-only survey of every layer. Safe any time.
.\scripts\wsl-audio.ps1 -Mode Check

# Play a short 440 Hz tone through WSL into the Windows default output.
.\scripts\wsl-audio.ps1 -Mode Playback      # human check: did you hear it?

# Bounded microphone capture + signal-presence analysis (auto-stops <= 10s).
.\scripts\wsl-audio.ps1 -Mode Record -Seconds 5

# Record, then play the recording back through the default output.
.\scripts\wsl-audio.ps1 -Mode RoundTrip     # human check: your own voice?

# Non-default distro, or an explicit endpoint (preserved verbatim if valid):
.\scripts\wsl-audio.ps1 -Mode Check -Distro Debian
.\scripts\wsl-audio.ps1 -Mode Check -PulseServer unix:/mnt/wslg/PulseServer
```

The same distro probe can be run directly inside WSL:

```bash
bash scripts/test-wsl-audio.sh check
bash scripts/test-wsl-audio.sh playback
bash scripts/test-wsl-audio.sh record --seconds 5
bash scripts/test-wsl-audio.sh roundtrip --seconds 3
bash scripts/test-wsl-audio.sh --self-test   # deterministic stubs, no audio needed
```

### Preview container (desktop orb without rebuilding an ISO)

```bash
bash scripts/preview-chat.sh           # chat window preview
bash scripts/preview-chat.sh --desktop # full desktop (orb) preview
```

The container mounts `/mnt/wslg` (including the Pulse socket) and sets
`PULSE_SERVER=unix:/mnt/wslg/PulseServer`. Dependency detection verifies every
audio-relevant package (Qt Multimedia incl. the GStreamer plugin backend and
`gst-plugins-good`, PulseAudio client tools, STT/TTS binaries) — an existing
`cmake` binary can no longer hide a missing audio dependency.

### Real Alpine guest

```powershell
.\scripts\run.ps1 -Name "Audio validation <task>" path\to\aios-x86_64.iso
```

`AIOS_QEMU_AUDIO` selects the QEMU audiodev backend (default `pa` in the Linux
launcher, `dsound` in `run-native.ps1`) and `AIOS_VM_NAME`/`-Name` gives the
VM a unique descriptive window title. A caller-supplied `PULSE_SERVER` in the
Windows environment is preserved through PowerShell -> WSL -> the Linux
launcher. Inside the guest, the image's manual voice path (chat microphone
button, Read aloud) exercises the duplex HDA device; the guest keeps capture
and playback enabled on it.

## Recovery notes

- WSLg COPY MODE (invisible GUI windows) also recycles PulseAudio. Use the
  existing `run.ps1` display repair prompt rather than manual kills, and wait
  for it to report restored before audio checks.
- Prefer restarting the single distribution (`wsl --terminate <distro>`) over
  `wsl --shutdown`, which closes every distro and Docker. Never automate it.
- If Windows privacy blocks the microphone, allow it for desktop apps in
  Settings > Privacy > Microphone; distinguish this denial from unplugged
  hardware in the reported stage.

## Results template

Record one row per probe. Paste exact `STAGE ...` lines under the table.
Do not commit raw recordings or per-machine logs.

| Date | Host/Windows build | Distro | Mode | Command | Failures | Not-tested | Human listening check | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |  |
|  |  |  |  |  |  |  |  |  |

## Current validation status

Headless-verified on Linux: the distro probe's reporting model
(`bash scripts/test-wsl-audio.sh --self-test`), launcher wiring, preview
dependency gating, and the full repository suite.

**Not yet verified (device/human gates remain):** live runs of every mode on a
real Windows/WSLg host with physical microphone and speakers, the preview
container audio path, the in-guest manual voice path in the real Alpine image,
and the ten-second auto-stop/cancellation behavior observed with real capture.
These require the operator's hardware per issue #84 and cannot be claimed on a
headless runner.
