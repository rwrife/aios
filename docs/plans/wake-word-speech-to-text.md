# Wake-word activation and speech-to-text implementation plan

Status: proposed; this PR changes documentation only. Baseline: `6986a2d`.

## Intended experience and scope

After explicitly enabling wake-word listening, a user says “Hey AIOS” and
dictates a message. AIOS indicates that it heard the wake phrase, records until
the user finishes speaking, and places the transcript in the appropriate chat's
composer. The user can edit it and press Send. The existing microphone button
continues to work when wake-word listening is disabled or unavailable.

“Translation layer” here means converting speech into text for the existing
chat pipeline. The first release targets English transcription. Translation
between languages, speaker identification, voice authentication, automatic
message submission, spoken command execution, and full-duplex conversation are
separate features. The wake phrase is a proposed product default, subject to
recognition testing; arbitrary user-defined phrases are outside the first release.

## Existing foundation

| Area | Current implementation | Planned change |
| --- | --- | --- |
| Capture and playback | `apps/shell/voice.h`: per-session Qt capture, 16 kHz mono signed 16-bit PCM, temporary WAV, 60-second limit | Move microphone ownership to a shared coordinator; preserve session playback and clip cleanup |
| Chat lifecycle | `apps/shell/main.cpp`: `Backend::setVoiceActive`, worker dispatch, transcription signals and cleanup | Route manual and wake-triggered capture through one coordinator with cancellable request identities |
| Transcription | `apps/aios/voice.py`: local `whisper-cli` or configured remote transcription endpoint | Reuse providers behind a bounded, versioned result contract |
| Worker boundary | `apps/aios/worker.py`: `transcribe` action | Correlate results with the originating capture and session |
| Composer | `apps/shell/ChatWindow.qml`: append transcript without sending | Preserve draft edits and reject stale or duplicate results |
| Desktop | `apps/shell/main.qml`: launcher restores minimized chat or creates one; `ChatOrb.qml` has inactive voice hooks | Add an explicit voice target policy and bind real listening states |
| Settings | `apps/aios/core.py`, `apps/shell/ModelSettings.qml` | Add validated opt-in and readiness settings |
| Packaging | `scripts/build-apps.sh`: pinned whisper.cpp; `distro/alpine/apks/` | Pin and package the selected keyword/VAD runtime and model metadata |

See [current voice behavior](../voice-and-sessions.md). Local recognition already
uses a separately downloaded, checksum-verified tiny.en model. A wake-word
detector and continuous microphone listener do not currently exist.

## Architecture and ownership

Pipeline: microphone → shared audio coordinator → local keyword detector →
utterance capture with voice activity detection (VAD) → existing transcription
worker → session-bound transcript result → editable composer.

1. Introduce `VoiceCoordinator` in the shell as the sole microphone owner within
   a desktop audio scope. Refactor `Voice` so manual recording also acquires this
   owner; do not open a second microphone on wake detection. Normalize supported
   device formats to 16 kHz mono PCM off the UI thread. If conversion is not
   available, return a concrete unsupported-format error.
2. Introduce a fixed local keyword/VAD adapter, preferably a native helper built
   for Alpine. Run inference away from the GUI thread. Exchange bounded PCM
   frames and versioned events over inherited pipes, with a bounded queue and
   watchdog; no TCP listener, arbitrary commands, or microphone access for agents.
3. Keep at most two seconds of rolling pre-trigger audio in memory (64,000 bytes
   at the normalized format). Use detector timing to exclude the wake phrase and
   retain a short overlap for the first dictated word. Validate phrase/utterance
   boundaries; do not remove arbitrary matching words from the transcript.
   Discard inactive audio continuously and never write or upload it.
4. Freeze a target session and generation at activation. Choose the focused,
   eligible chat; otherwise restore the most recently minimized eligible chat;
   otherwise create a new anonymous chat through the existing launcher. Show
   the target while recording. Focus changes must not move an utterance.
   A busy target reports “Chat is busy”; it does not silently choose another chat.
5. The existing identity-enabled desktop currently refuses `openChat()`.
   Initially report wake listening unavailable in that mode. Before enabling it
   there, implement an explicit broker-approved audio ownership lease across
   desktop/private-shell processes and private-chat routing. Never bypass that
   guard, infer an owner from speech, or treat sign-in status as a capability.
6. Bind every capture to an opaque request ID, session ID, and cancellation
   generation. Session close/stop, privacy loss, disable, or microphone change
   cancels capture and transcription, removes temporary audio, and invalidates
   late results. Never retarget a canceled capture or reopen its chat.

## State machine and bounded behavior

| State | Behavior and transitions |
| --- | --- |
| Disabled | Microphone closed; enabling requires successful model/device checks |
| Armed | Local keyword inference only; trigger reserves a target and enters Capturing |
| Capturing | VAD segments speech; Finish or silence enters Transcribing; Cancel discards |
| Transcribing | Microphone released; no additional wake capture; valid result enters Review |
| Review | Transcript inserted once; after cooldown return to Armed if still eligible |
| Suspended | Close microphone for privacy shield, native PIN prompt, suspend, or AIOS speech playback; resume only after eligibility is rechecked |
| Error | Close microphone, clear buffers, report cause; explicit retry or bounded device recovery |

Initial tunable constants: speech must start within 5 seconds of the trigger;
require at least 250 ms of speech; finish after 900 ms of trailing silence; cap
the whole captured clip, including overlap, at 60 seconds; use a 1.5-second
cooldown. These are starting values to validate, not measured guarantees.
Manual capture skips keyword detection and retains the existing Finish control.
Silence, a wake phrase alone, and VAD-rejected noise must not create draft text.

Suspend wake listening across all AIOS synthesized playback, then clear the ring
buffer and rearm after cooldown. External speakers/TV can still produce false
triggers: measure them, and retain review-before-send. Barge-in and acoustic echo
cancellation are later work. Unplug, sleep/resume, and helper crashes must release
the device and avoid restart loops; permit at most three recovery attempts with
backoff before requiring Retry.

## Engine decision and speech-to-text contract

Start with a short Alpine feasibility comparison rather than committing to a
Python wheel that may not support musl. Evaluate sherpa-onnx keyword spotting
first because it exposes customized keyword spotting, with openWakeWord as an
alternative. Test the actual “Hey AIOS” pronunciation and near matches. Choose
one engine, record its revision, dependencies, model size, licenses, CPU/RAM,
latency, and accuracy in a decision note before integration. Custom phrase
support does not itself establish acceptable recognition quality.

Use local VAD for segmentation; evaluate the chosen runtime's VAD facilities or
a separately pinned Silero model. Upstream whisper.cpp documents VAD support,
but check the repository's pinned build before depending on that option.
Do not run Whisper continuously as a wake-word detector.

Preserve `transcribe(path)` compatibility for CLI callers. Add an internal result
envelope with `version`, `request_id`, `session_id`, `generation`, `text`,
`language` (nullable), and `provider`. Bound text to 16,384 characters and the
serialized response to 1 MiB; validate types and identifiers before insertion.
Do not invent confidence values when a provider supplies none. Use stable error
codes for unavailable model/device, invalid audio, no speech, timeout, provider
failure, cancellation, and stale session, with short UI explanations.

Retain the existing local 120-second subprocess timeout, remote 90-second
request timeout, separate voice credentials, TLS verification, and refused
authenticated redirects. Enforce duration and WAV format at the worker boundary
as well as in capture, since the current file-size check alone is not a duration
check. Cancellation terminates worker/child work and cleans files even if the
provider result arrives late. Do not automatically retry remote uploads or switch
from local recognition to a remote service. Existing typed text and edits made
during transcription must survive transcript insertion.

## Settings, privacy, and UI

- Proposed settings: `wake_word_enabled` (boolean, default false),
  `wake_word_id` (allowlisted model ID, initially the validated AIOS phrase), and
  `voice_language` (`en` initially). Persist preferences through existing config
  validation; a saved enable preference is distinct from runtime readiness.
  Recheck eligibility at each startup. Failed enable attempts must not claim the
  microphone is listening. Use the existing default-input selection initially.
- Show whether listening is off, armed, capturing, transcribing, suspended, or
  unavailable. Explain on enable that the microphone stays active for local
  wake detection, and that only triggered utterances use the configured remote
  service. Download missing models explicitly through setup with progress,
  checksums, cancellation, license information, and actionable errors.
- Provide Stop listening and Cancel recording controls. Closing the settings
  dialog does not disable a saved preference; turning listening off closes the
  stream immediately. Clarify that speaker mute is separate from microphone
  listening; honor actual input mute and do not show active capture when muted.
- Use existing theme roles, quiet controls, accessible names, immediate keyboard
  focus tooltips, and reduced-motion behavior. Bind `ChatOrb` hooks only to real
  state; an error must never leave a glowing recording indicator.
- Store no raw audio in history or logs. Keep triggered WAVs owner-only in the
  existing temporary lifecycle; include startup cleanup for crash leftovers in
  an application-owned directory. Persist text only through the normal Send
  path. Log bounded error codes and aggregate timing without transcript content.
- Extend the structured `os_settings` interface with validated wake enable/read
  operations and runtime status, backed by the coordinator. Update
  `apps/skills/os-control/SKILL.md`, `apps/aios/os_settings.py`, and
  `docs/agentic-tools.md` together. Define valid values, persistence, readback,
  per-chat authorization, and unavailable/error results. No raw audio stream or
  generic helper invocation becomes an agent tool.

## Delivery sequence

| Milestone | Deliverables | Completion gate |
| --- | --- | --- |
| 1. Feasibility and baseline | Real Alpine build spike; keyword/VAD selection; licensed model manifest; baseline STT and hardware measurements | Chosen phrase, musl build, redistribution terms, memory and accuracy meet recorded targets; otherwise document blocker and keep feature disabled |
| 2. Shared capture | Coordinator, normalization, bounded ring buffer, helper protocol, cancellation generations; migrate manual capture | Deterministic tests prove single ownership, bounded memory, cleanup, and no manual-recording regression |
| 3. Wake to draft | State machine, VAD, target routing, versioned transcription result, error handling | Wake through draft succeeds offline; two-chat isolation and late-result rejection pass |
| 4. Settings and lifecycle | Opt-in UI, orb feedback, playback/privacy suspension, structured OS controls | Status readback matches the actual microphone; settings migration, keyboard and palette checks pass |
| 5. Release validation | Pinned runtime/model packaging, local ISO, hardware tests, QA evidence and docs | Functional, privacy and performance gates below pass; publish measured limitations |

Keep milestones 2–4 as a batch for expensive image/VM validation while running
fast targeted tests during development. Initially enable wake support only in
the validated anonymous desktop scope. Identity/private-desktop support requires
the ownership lease and routing work described above plus its own isolation gate.

## Validation and release gates

Headless tests must use fake audio, a fake clock, and fake detector/provider
events. Cover all state transitions; trigger debounce; exact buffer/duration
limits; speech-start and silence timeouts; malformed/oversized helper messages;
helper hangs; cancel at every phase; duplicate/stale results; busy/closed targets;
typing during transcription; config migration; missing/corrupt models; and
provider failures. Extend `tests/test_voice_attachments.py` and add dedicated
coordinator/protocol tests. No display server or real microphone is required for
these tests. Retain existing voice credential and transport regression coverage.

Build locally in WSL, boot the real Alpine image with `scripts/run.ps1`, and give
QEMU a unique title such as `AIOS wake-word validation`. Allow several minutes
for boot. Validate synthetic PulseAudio input first, then real built-in and USB
microphones; record Bluetooth headset results separately if available. Check
48 kHz devices, unplug/replug, default-device changes, input mute, no microphone,
sleep/resume, offline recognition, remote-service errors, and missing weights.
Do not equate a synthetic-input pass with working host microphone passthrough.

Record screenshots of armed, capturing, transcribing, review, and unavailable
states with Ocean and one generated palette, including reduced motion and
keyboard focus. Check two concurrent chats, minimize/restore, target close,
cancel, and privacy transitions. Run existing browser release checks on the final
image (sandbox, loading, text input, navigation, scrolling, cleanup, and two
isolated chats) to catch packaging regressions.

Provisional product gates, to be measured on a named reference CPU/microphone:

- Wake recall at least 95% in quiet and 90% at a documented 10 dB SNR, using at
  least 200 positive utterances across at least 10 consenting speakers and
  representative accents. Keep tuning and evaluation recordings separate.
- No more than 0.5 false activations per hour over at least 10 hours of negative
  speech, music, TV and room noise. Report raw counts and test conditions.
- p95 trigger feedback under 300 ms after phrase completion; p95 local
  end-of-speech-to-draft under 5 seconds for ten-second utterances. Report remote
  latency separately with provider/network conditions.
- Armed listener averages at most 10% of one reference CPU core and adds at most
  200 MiB RSS; measure STT peak memory separately. No growth in an eight-hour soak.
- English word error rate at most 15% on the held-out quiet dictation set; report
  noisy results separately and verify no first-word loss or wake-phrase leakage.
- Zero idle audio uploads/writes, cross-chat transcript deliveries, auto-sends,
  or remaining capture/helper processes after shutdown. Verify with network/file
  instrumentation and process inspection, including cancellation races.

These are proposed acceptance targets, not current performance claims. If a
target fails, keep wake listening experimental/off by default and record the
failure; improve the engine/model or explicitly revise the product scope before
release. Rollback disables/removes the listener while retaining manual dictation
and existing local/remote transcription configuration.

## References and decisions still to close

Primary engine references reviewed September 10, 2026:

- [sherpa-onnx keyword spotting](https://k2-fsa.github.io/sherpa/onnx/kws/index.html): customized keyword detection candidate.
- [openWakeWord](https://github.com/dscripka/openWakeWord): local PCM detection candidate; inspect code and individual model licenses separately before redistribution.
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp): existing transcription engine and upstream VAD documentation.

Milestone 1 must settle the production keyword/VAD engines and exact model
licenses, the final phrase and pronunciation, target hardware, model delivery
size, and whether tiny.en can meet the transcript quality/latency gates. Add
multilingual transcription only with matching models and language controls;
cross-language translation requires a separate explicit product choice.
