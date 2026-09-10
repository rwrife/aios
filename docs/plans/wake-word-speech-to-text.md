# Wake-word activation and spoken conversation implementation plan

Status: proposed; this PR changes documentation only. Baseline: `6986a2d`.

## Intended experience and scope

After explicitly enabling wake-word listening, a user says “Hey AIOS” and
dictates a message. AIOS indicates that it heard the wake phrase, records until
the user finishes speaking, and places the transcript in the appropriate chat's
composer. The user can edit it and press Send. The existing microphone button
continues to work when wake-word listening is disabled or unavailable.

When that voice-initiated message is sent, the agent normally answers aloud as
well as in the chat. Short conversational answers can be spoken directly;
requests to write code, create documents, or launch an application receive a
brief spoken result or confirmation while the full output stays on screen.
Both input and output speech work locally, with optional downloadable quality
upgrades and explicitly selected remote providers.

“Translation layer” here means converting speech into text for the existing
chat pipeline. The first release targets English transcription. Translation
between languages, speaker identification, voice authentication, automatic
message submission, direct speech-to-tool execution, and full-duplex conversation are
separate features. The wake phrase is a proposed product default, subject to
recognition testing; arbitrary user-defined phrases are outside the first release.
Voice-origin requests may use the ordinary agent/tool workflow after Send;
spoken replies do not introduce a separate execution or approval path.

## Existing foundation

| Area | Current implementation | Planned change |
| --- | --- | --- |
| Capture and playback | `apps/shell/voice.h`: per-session Qt capture, 16 kHz mono signed 16-bit PCM, temporary WAV, 60-second limit | Move microphone ownership to a shared coordinator; preserve session playback and clip cleanup |
| Chat lifecycle | `apps/shell/main.cpp`: `Backend::setVoiceActive`, worker dispatch, transcription signals and cleanup | Route manual and wake-triggered capture through one coordinator with cancellable request identities |
| Transcription | `apps/aios/voice.py`: local `whisper-cli` or configured remote transcription endpoint | Reuse providers behind a bounded, versioned result contract |
| Spoken output | `apps/aios/voice.py`: local eSpeak NG or remote synthesis; `Backend::readReply` and Qt playback | Reuse synthesis/playback for automatic, concise replies to voice-origin turns; add optional local neural voices |
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

After Send: voice-origin turn → agent answer/tool outcomes → spoken-response
policy → bounded speech text → local or selected remote TTS → coordinated Qt
playback. The full answer/artifact remains available in the chat.

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
   cancels capture, transcription, synthesis and playback, removes temporary audio, and invalidates
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

Track output separately from capture: Idle → Preparing speech → Speaking → Idle,
with cancellation/error transitions from both active states. The shared arbiter
suspends listening before playback begins and rearms only after playback stops
and cooldown expires. Agent generation and tool work do not count as recording.

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

## Spoken-response policy and turn lifecycle

Store `input_origin` (`typed`, `voice`, or `mixed`) on the pending draft and sent
turn; append a transcript without discarding this metadata when the user edits
it. A mixed draft containing dictated text is voice-initiated. Clearing the
draft resets its origin, and later typed turns do not inherit voice mode from
an earlier turn. Keep a visible per-turn override for silent or spoken reply.
Default to automatic spoken replies for voice/mixed turns, and manual Read aloud
for typed turns. This applies equally to wake-word and microphone-button input.

| Request/result | Spoken output | Visible output |
| --- | --- | --- |
| Short question or explanation | Answer directly in natural speech | Full answer and any links |
| Long explanation, research, table, or list | Brief answer/summary and an offer to hear more | Complete detail and sources |
| Write or edit code | “I've updated the code. The checks passed.” only when both outcomes are verified | Code/diff and actual validation details |
| Launch an application | “The browser is open.” after confirmed launch; “I've requested the browser to open” if only dispatch is known | Application plus relevant status |
| Create a document or other artifact | “The document is ready to review.” after the file exists | Artifact and full answer |
| Approval or clarification needed | Ask the actual short question, including material scope | The same question and existing approval controls |
| Failure or partial success | State what failed or remains incomplete; no false completion claim | Error and recovery details |

Implement a `speech_response` policy module beside the Python agent/worker code.
Wire `apps/aios/agent.py` outcome events through `apps/aios/worker.py`, then
`apps/shell/main.cpp` for turn correlation and playback. Update
`apps/shell/ChatWindow.qml` for draft origin and spoken/silent overrides, and
`apps/aios/cli.py` alongside config/settings changes so provider choices and
model setup remain available without the GUI.
Use verified tool completion events for action confirmations, with deterministic
templates for common outcomes. For conversational content, reuse a short final
answer or request a separate concise `speech_text` from the configured chat
model. A bounded optional summary pass may use that same model; it must not
require a second large model or a remote call in local mode. On malformed or
slow summary output, fall back to a truthful generic message such as “The answer
is ready in the chat,” without reading code or claiming unverified success.
Cap the optional summary pass at 2 seconds and its output at the speech limits
below; prefer templates or speech text from the original generation for latency.

Keep structured speech metadata separate from the displayed answer. A proposed
envelope includes `version`, `session_id`, `turn_id`, `generation`, `event_id`,
`kind` (`answer`, `completion`, `question`, `failure`), and plain `speech_text`.
Validate and deduplicate it before synthesis. Limit automatic speech to 80 words,
600 characters, and 30 seconds of playback; prefer one sentence for actions.
Summarize semantically rather than cutting the first characters of a long reply.
Exclude code blocks, diffs, raw tool logs, markup, long paths/URLs, and secrets.
Full explicit Read aloud remains separate and uses its own bounded limits.

Speak one terminal result per turn by default. A clarification/approval question
can be spoken when that event actually pauses work; do not narrate token streams
or every tool call. A “Done” confirmation requires a verified terminal outcome.
Distinguish an acknowledgement, a request for permission, and successful
completion. Spoken questions never grant permission: native authentication and
protected approvals retain their existing trusted UI and per-chat checks.
Until a separate voice-submission feature exists, replies to spoken questions
still use dictation/review/Send; this release is turn-based, not fully hands-free.

Add automatic speech as a follow-on job after the text answer is committed, not
through a second user message or a recursive agent call. Preserve the existing
manual `synthesize(text, destination)` and CLI behavior while routing both paths
through a shared cancellable adapter. A synthesis failure must not fail the
completed agent task. Show the answer plus a brief audio error and Retry/Read
aloud. Never rerun tools to regenerate a spoken confirmation.

Use one playback owner across chats. Play automatically only while the originating
chat is foreground and eligible; if focus moves away, keep a Read aloud action
instead of unexpectedly speaking a background result. Do not queue stale replies
for later playback. Stop speech on session stop/close, a new sent turn in that
chat, privacy loss, lock/suspend, output-device change, or explicit Stop speaking.
Manual recording preempts playback. Provide keyboard and pointer Stop controls;
voice interruption during playback remains outside this half-duplex release.
Respect output mute; do not unmute devices or replay suppressed speech afterward.

## Local-first model tiers and provider configuration

New installations default both STT and TTS to local. Existing installations keep
their explicitly saved provider settings during migration; do not reinterpret
the current `voice_mode` default as user consent to a remote provider. A local
chat model plus local STT/TTS must support the entire round trip offline once
the required models are installed. Selecting a remote chat model remains an
independent choice and should make clear that the transcript goes to that model.

| Tier | Input speech | Output speech | Delivery |
| --- | --- | --- | --- |
| Basic local | Existing Whisper tiny.en | Existing eSpeak NG | Bundled engines; explicit setup download for STT weights; TTS works without neural weights |
| Enhanced local | Validated larger/quantized Whisper model | Validated neural voice, evaluating Piper first | Optional curated downloads; show disk/RAM/CPU requirements and expected latency before installing |
| Advanced optional | Other validated local STT or explicitly chosen remote STT | Higher-quality local candidate such as Kokoro, or explicitly chosen remote TTS | Capability/hardware checks; independent provider choice for each direction |

Piper is a local neural TTS candidate, not an assumed Alpine dependency. Its
current engine and individual voice models have separate licensing requirements.
Evaluate Kokoro as an optional quality tier only after verifying the runtime,
model/voice licenses, musl build, CPU-only performance and resource costs. The
baseline must not require a GPU, proprietary key, or a large neural voice.

Split the currently shared `voice_mode` into validated `stt_provider` and
`tts_provider` choices (`local`/`remote`), plus allowlisted local engine/model/voice
IDs. Use separate STT/TTS URL, key and model settings so a user can keep recognition
local while choosing advanced remote speech output, or the reverse. Migrate saved
voice settings explicitly into the two namespaces, preserving existing choices
and clearing credentials on endpoint changes. Never copy chat credentials.
Retain TLS verification, redirect restrictions, bounded requests and safe key
storage. Remote STT receives only triggered audio; remote TTS receives only the
selected speech text, not the full chat, code, attachments, or raw tool results.

Create a curated model catalog with version, engine compatibility, language,
sample rate, artifact size, checksum, source, license/attribution, and measured
reference hardware requirements. Use atomic, cancellable downloads with storage
checks, verified installation and rollback. Model files are data, not executable
plugins; do not load arbitrary model-supplied code. Include voice preview,
installed-size display, selection, removal, and restoration of the basic local
voice. Pin engine builds and catalog revisions in packaging. Model upgrades are
user-selected, never a silent download during an utterance.

When an enhanced local voice is missing, incompatible, or too slow, offer/use
the bundled local fallback with a visible status. Do not silently fall back to
the network. Local STT failure can offer the installed basic model or setup;
remote failure leaves the text available and offers an explicit retry or local
choice. Cache models, not utterances; clean synthesized audio after playback,
cancellation, error and startup recovery. Enforce WAV format, decoded duration
and size limits for local and remote output before playback, with watchdogs for
synthesis as well as summary generation.

## Settings, privacy, and UI

- Proposed settings: `wake_word_enabled` (boolean, default false),
  `wake_word_id` (allowlisted model ID, initially the validated AIOS phrase), and
  `voice_language` (`en` initially). Persist preferences through existing config
  validation; a saved enable preference is distinct from runtime readiness.
  Recheck eligibility at each startup. Failed enable attempts must not claim the
  microphone is listening. Use the existing default-input selection initially.
- Add `spoken_reply_mode` (`voice_turns`, `off`, default `voice_turns`), local
  voice/model selections and independent provider settings described above.
  Show synthesis readiness separately from microphone readiness, with preview,
  Stop speaking, and an explicit manual Read aloud action. No `always` mode or
  background announcement behavior is implied by enabling voice replies.
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
  operations, spoken-reply mode, allowlisted installed voice selection, stop
  playback and runtime status, backed by the coordinator. Downloads remain an
  explicit setup action rather than arbitrary URLs in an agent tool. Update
  `apps/skills/os-control/SKILL.md`, `apps/aios/os_settings.py`, and
  `docs/agentic-tools.md` together. Define valid values, persistence, readback,
  per-chat authorization, and unavailable/error results. No raw audio stream or
  generic helper invocation becomes an agent tool.

## Delivery sequence

| Milestone | Deliverables | Completion gate |
| --- | --- | --- |
| 1. Feasibility and baseline | Real Alpine build spike; keyword/VAD and TTS candidates; licensed model catalog; baseline STT/TTS hardware measurements | Chosen phrase, voices, musl build, redistribution terms, memory and accuracy meet recorded targets; otherwise document blocker and keep feature disabled |
| 2. Shared capture | Coordinator, normalization, bounded ring buffer, helper protocol, cancellation generations; migrate manual capture | Deterministic tests prove single ownership, bounded memory, cleanup, and no manual-recording regression |
| 3. Wake to draft | State machine, VAD, target routing, versioned transcription result, error handling | Wake through draft succeeds offline; two-chat isolation and late-result rejection pass |
| 4. Voice-aware replies | Draft/turn origin metadata, spoken-response policy, outcome confirmations, local synthesis, playback arbitration | Voice turns answer aloud; code/artifact/action turns produce brief truthful confirmations; typed turns stay silent; failed speech never reruns work |
| 5. Settings and model upgrades | Opt-in UI, orb feedback, playback/privacy suspension, structured OS controls, independent STT/TTS providers, catalog downloads and migration | Basic offline round trip, optional upgraded voice, cancellation, readback, keyboard and palette checks pass |
| 6. Release validation | Pinned runtime/model packaging, local ISO, hardware tests, QA evidence and docs | Functional, privacy, speech-output and performance gates below pass; publish measured limitations |

Keep milestones 2–5 as a batch for expensive image/VM validation while running
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

Add headless spoken-response tests for typed/voice/mixed origin and draft reset;
short answers versus code/artifact summaries; success/partial/failure/approval
events; summary timeouts and malformed metadata; late/duplicate synthesis;
foreground-only playback; stop/new-turn races; muted output; model download
verification and rollback; and provider migration. Confirm no network fallback,
no full code/secret payload sent to remote TTS, and no tool re-execution on Retry.
Test missing speakers and playback failure independently of microphone failure.

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

Extend screenshots to preparing-speech/speaking, model selection/download, and
audio failure states. On the real image, exercise voice question → reviewed Send
→ spoken answer, code request → concise confirmation, successful/failed app
launch → truthful status, and approval question → existing approval UI. Test
local chat/STT/TTS with network access disabled after setup, downloaded neural
voices, explicitly configured mixed local/remote directions, and speaker
unplug/mute. Capture loopback output to prove playback does not wake the listener.

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
- p95 completed-answer-to-first-audio under 2 seconds for basic local speech and
  under 4 seconds for the selected enhanced local tier, including summary time;
  measure on the reference CPU under concurrent chat load. Measure cold and warm
  starts separately and publish additional peak memory/disk cost for each tier.
- At least 95% listener transcription accuracy on a held-out 50-phrase spoken
  output set reviewed by at least five listeners; zero incorrect success claims
  in the deterministic action/failure suite. All automatic output respects the
  80-word/600-character/30-second bounds and excludes full code/diffs.
- Stop speaking silences output within 250 ms; no overlapping chat playback,
  self-trigger from synthesized replies, late playback after privacy loss, or
  automatic speech for typed-only turns. Test both basic and enhanced voices.
- Zero idle audio uploads/writes, cross-chat transcript deliveries, auto-sends,
  or remaining capture/helper processes after shutdown. Verify with network/file
  instrumentation and process inspection, including cancellation races.

These are proposed acceptance targets, not current performance claims. If a
target fails, keep wake listening experimental/off by default and record the
failure; improve the engine/model or explicitly revise the product scope before
release. Rollback disables/removes the listener while retaining manual dictation
and existing local/remote transcription configuration. Disable automatic spoken
replies independently while retaining manual Read aloud; model rollback restores
the basic local voice without changing provider choices or deleting chat history.

## References and decisions still to close

Primary engine references reviewed September 10, 2026:

- [sherpa-onnx keyword spotting](https://k2-fsa.github.io/sherpa/onnx/kws/index.html): customized keyword detection candidate.
- [openWakeWord](https://github.com/dscripka/openWakeWord): local PCM detection candidate; inspect code and individual model licenses separately before redistribution.
- [whisper.cpp](https://github.com/ggml-org/whisper.cpp): existing transcription engine and upstream VAD documentation.
- [Piper engine](https://github.com/OHF-Voice/piper1-gpl) and [voice model documentation](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md): optional local neural synthesis and per-voice licensing review.
- [Kokoro model card](https://huggingface.co/hexgrad/Kokoro-82M): candidate higher-quality local speech model; validate the selected deployment runtime separately.

Milestone 1 must settle the production keyword/VAD/TTS engines and exact model
licenses, the final phrase and pronunciation, target hardware, model delivery
size, selected downloadable voices, and whether tiny.en and the selected TTS tiers
can meet the quality/latency gates. Add
multilingual transcription only with matching models and language controls;
cross-language translation requires a separate explicit product choice.
