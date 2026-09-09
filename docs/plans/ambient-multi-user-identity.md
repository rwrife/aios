# AIOS Ambient Multi-User Identity and Session Implementation Plan

Status: Proposed
Target repository: `rwrife/aios`
Initial platform: Alpine Linux, OpenRC, Qt Quick shell, Python backend

## 1. Objective

Extend AIOS from a personally controlled, single-account desktop into a shared AI-first computer that:

- Is immediately usable without a login screen.
- Runs ordinary, low-risk requests in an isolated anonymous context.
- Recognizes enrolled people locally from face, voice, and interaction evidence.
- Activates a private Linux workspace only when a request requires personal context.
- Keeps each person's files, credentials, processes, conversations, and work history isolated.
- Suspends and locks personal work after the person leaves.
- Lets the same person return later and resume durable work sessions.
- Requires an explicit PIN or stronger authenticator before exposing secrets, financial information, or consequential external actions.
- Performs biometric inference and policy decisions locally by default.

The experience should feel like an intelligent shared appliance, not a conventional multi-user login system.

## 2. Product principles

1. **Anonymous by default.** Recognition alone does not mount a private workspace or attribute anonymous activity to a person.
2. **Identity just in time.** Acquire personal context only when intent, resource access, or the user explicitly requires it.
3. **Authorization is separate from recognition.** Face and voice provide identity evidence; policy determines what that evidence permits.
4. **Explicit intent for sensitive actions.** A PIN/passkey prompt must describe the capability or transaction being authorized.
5. **Applications are temporary; work is durable.** Processes may terminate on absence, while files, conversation state, and restoration manifests persist.
6. **Sessions never silently change owners.** Once personal work is bound to a user, another speaker cannot inherit it.
7. **The LLM proposes; trusted services enforce.** The model may classify intent, but it cannot mount homes, select arbitrary UIDs, verify PINs, or retrieve unrestricted secrets.
8. **Local-first privacy.** Raw camera frames and microphone buffers should normally be discarded after inference. Store encrypted templates rather than recordings.
9. **Uncertainty fails safely.** Conflicting or inadequate evidence leaves AIOS anonymous or produces a conversational verification request.

## 3. Current AIOS baseline

AIOS currently:

- Creates and autologs into one local `aios` account.
- Starts Xorg/Openbox and the Qt Quick shell from `aios-session`.
- Launches a Python worker for shell operations using JSON messages.
- Stores model configuration, API keys, conversations, and other state under the current `$HOME` using XDG paths.
- Creates independent chat windows, with UUID-based conversation files.
- Runs the local `llama-server` as the desktop user and binds it to loopback.

This is a useful ambient-shell foundation, but `$HOME` and mode-0600 files do not isolate multiple people when every operation runs under the same UID.

## 4. Terminology and identity states

### 4.1 Session types

**Presence session**
A short-lived assertion that a tracked person is actively interacting with the machine. It expires when presence or interaction evidence becomes stale.

**Work session**
A durable user task such as “build my résumé.” It owns conversation history, artifacts, working directory, application restoration state, and summaries. It can move between `active`, `suspended`, `archived`, and `deleted`.

**Application instance**
A process tree launched for a work session. Application instances are terminated on lock and reconstructed from the work-session manifest on resume.

**Anonymous session**
An ephemeral presence/work context with no personal credentials or private filesystem access. It is destroyed after its idle threshold unless a user explicitly claims an artifact.

### 4.2 Authority levels

| Level | Meaning | Examples |
| --- | --- | --- |
| `anonymous` | No personal identity activated | Calculator, generic web lookup, temporary document |
| `recognized` | Local biometric evidence identifies an enrolled person | Personal preferences, résumé workspace, ordinary session history |
| `verified` | Recognized user entered their local activation PIN or used a bound authenticator | Password vault, financial records, protected cloud credentials |
| `transaction_confirmed` | User deliberately approved one described consequential operation | Send message, purchase, transfer, credential change, destructive action |

Authority must always be scoped and time-limited. A successful PIN must not create an unlimited “everything unlocked” state.

## 5. Target architecture

### 5.1 Components

#### `aios-identityd`

A local service responsible for:

- Camera and microphone sensor orchestration.
- Face detection, tracking, alignment, and embedding generation.
- Voice activity detection and speaker embedding generation.
- Active-speaker correlation when multiple people are present.
- Enrollment and encrypted biometric-template storage.
- Confidence fusion, evidence decay, and identity-candidate reporting.
- Presentation-attack/liveness signals when supported.

It reports evidence; it does not mount files or launch applications.

#### `aios-sessiond`

A small privileged broker responsible for:

- Presence leases and their expiration.
- Mapping AIOS identity UUIDs to Linux UIDs/workspaces.
- Creating anonymous sandboxes.
- Activating, suspending, resuming, and archiving work sessions.
- Launching process trees under the correct user and sandbox.
- Tracking processes by session/cgroup or equivalent process scope.
- Mounting and unmounting protected per-user storage.
- Clearing clipboard, temporary files, and transient credentials on lock.
- Rejecting requests for arbitrary UIDs or paths.

Expose a narrow Unix-domain-socket API. Authenticate callers using socket permissions and peer credentials. Use structured JSON messages with strict schemas and fixed action names; never accept shell command strings.

#### `aios-policyd`

A deterministic authorization service/library responsible for:

- Mapping operations and resources to required authority.
- Evaluating current presence, identity evidence, session ownership, and capability scopes.
- Requesting step-up authentication when necessary.
- Issuing short-lived, operation-scoped capability tokens.
- Recording security-relevant decisions without recording biometric samples or PINs.

The intent model can recommend a classification, but resource brokers must perform the final policy check.

#### `aios-secretd`

A local secret broker responsible for:

- Per-user encrypted credentials.
- Releasing or using credentials only with an appropriate capability.
- Injecting credentials into a destination process or performing an authenticated request without exposing raw secrets to the LLM whenever possible.
- Revoking secret access when presence or authority expires.

#### Qt shell integration

Add shell-level support for:

- Anonymous/recognized/verified status indicators.
- System-owned identity and PIN overlays.
- Unknown-person greeting and enrollment UX.
- Recent/suspended work-session suggestions.
- Ambiguity prompts when multiple candidates or conflicting signals exist.
- Immediate privacy shielding when presence is lost.

The PIN entry surface must be isolated from chat content and ordinary applications.

### 5.2 Suggested process flow

```text
Sensors -> identityd -> identity evidence
User request -> intent classification -> policyd
policyd + evidence -> anonymous access, personal activation, or step-up request
sessiond -> isolated process/workspace launch
secretd -> narrowly scoped credential use when authorized
session journal -> durable resume state
```

## 6. Local biometric pipeline

### 6.1 Face pipeline

Use OpenCV for capture and orchestration, with locally executed ONNX models.

1. Capture reduced-resolution frames at a configurable rate.
2. Detect faces.
3. Maintain stable person tracks across frames.
4. Reject tracks that are too brief, too small, occluded, or low quality.
5. Align acceptable face crops.
6. Generate normalized embeddings.
7. Compare embeddings against enrolled templates using cosine similarity.
8. Aggregate evidence across time rather than deciding from one frame.
9. Reduce inference frequency after a track becomes stable.

YuNet plus SFace is an appropriate initial OpenCV prototype. Keep the detector and embedding interfaces replaceable, and validate model licenses before distributing weights in the ISO.

### 6.2 Voice pipeline

1. Use local voice-activity detection to locate speech segments.
2. Continue using local transcription for commands and conversation.
3. Generate a speaker embedding from adequate speech segments.
4. Match against enrolled speaker templates.
5. Correlate speech timing with visible lip movement when multiple faces are present.
6. Optionally use microphone-array direction-of-arrival evidence.

Voice is corroborating evidence, not sufficient by itself for sensitive access.

### 6.3 Confidence fusion

Do not average raw model scores from unrelated models. Calibrate each signal and combine decisions through explicit rules or calibrated likelihoods.

Initial rules:

- Strong, stable face evidence may establish a recognized candidate.
- Medium face evidence plus matching speaker evidence and active-speaker correlation may establish recognition.
- Face/voice disagreement blocks personal activation.
- Multiple faces without a reliable active speaker remain anonymous.
- A recognized bystander must not become the owner of another person's request.
- Identity evidence decays with time, occlusion, and loss of interaction.
- Apply hysteresis so a stable track does not flicker between identities.

Thresholds must be measured on the actual target cameras, rooms, lighting, microphones, and enrolled users. Store thresholds in policy configuration rather than compiling them into the models.

## 7. Enrollment flow

### 7.1 Trigger

Begin enrollment only when:

- An unknown track remains present beyond a configurable threshold, and
- The person intentionally interacts with AIOS.

Do not greet every passerby. Presence duration alone may prepare the greeting, but speech, keyboard/mouse use, or deliberate gaze should trigger it.

### 7.2 Conversation

Example:

1. “Hi, I don't think we've met. What should I call you?”
2. Collect a display name.
3. If that name resembles an existing identity, do not merge or grant access; require account recovery or verification.
4. Explain that face/voice templates will be stored locally and request consent.
5. Collect multiple face samples under modest pose variation.
6. Ask the person to repeat a randomized phrase and collect speaker samples.
7. Create an internal identity UUID, Linux workspace, PIN, and recovery method.
8. Confirm successful enrollment and explain when AIOS will request the PIN.

A spoken name is self-asserted metadata, not proof of ownership of an existing identity.

### 7.3 Template safety

- Encrypt templates at rest.
- Avoid retaining raw enrollment video/audio by default.
- Never place biometrics in chat logs.
- Do not continually retrain a user's template from ordinary low-confidence matches.
- Add new samples only after high-confidence or explicitly verified sessions.
- Support inspect, re-enroll, export where appropriate, and permanent deletion.

## 8. Anonymous execution

Anonymous mode must remain usable even if recognition is unavailable, declined, or ambiguous.

An anonymous sandbox should have:

- A unique ephemeral UID/namespace or container.
- A temporary home backed by `tmpfs` or a disposable directory.
- No mount of any personal home or session database.
- No saved user credentials or inherited user-specific environment variables.
- A separate clipboard and temporary/download area where feasible.
- Restricted device, filesystem, IPC, and privileged-command access.
- A process scope that can be terminated as a unit.
- A short idle timeout followed by secure cleanup.

Recognition may maintain an identity candidate in the background, but anonymous actions must stay anonymous unless the request requires personal context or the user explicitly asks to save/claim the work.

When claiming anonymous work, copy only explicitly selected artifacts into the verified user's workspace. Do not retroactively attach the anonymous conversation or browsing history without explicit consent.

## 9. Personal workspace and application isolation

### 9.1 Initial storage model

Create a real Linux UID for each enrolled user and a private persistent home or workspace volume. Maintain the mapping from AIOS identity UUID to UID in privileged state. Never derive usernames or filesystem paths directly from spoken display names.

Use filesystem ownership plus per-user encryption. Where hardware permits, protect volume-unlock material with a TPM-bound key. A PIN may activate a protected key but should not be used directly as the volume-encryption key.

### 9.2 Launch model

All personal applications must be launched through `aios-sessiond` with:

- A valid presence/work-session lease.
- A fixed mapped UID.
- A controlled environment.
- A bounded filesystem view.
- A session-specific runtime and IPC directory.
- A trackable process scope.

The LLM may request `launch_app(type, session_id, arguments)` but cannot submit an executable path, arbitrary UID, or unvalidated environment.

### 9.3 Display-server boundary

The existing Xorg/Openbox architecture is not a strong multi-user application boundary because X11 clients sharing a display can inspect input or interact with other clients.

Development strategy:

- Use sequential user activation and complete process teardown for an early functional prototype.
- Treat that prototype as insufficient for strong isolation.
- Before claiming secure multi-user support, migrate the user-facing shell/application boundary to Wayland or run each user's applications in a separately isolated compositor/session.
- Verify clipboard, screen capture, input injection, drag/drop, and notification isolation as explicit release gates.

## 10. Durable work-session journal

Store session metadata inside the owning user's encrypted workspace. SQLite is sufficient initially.

### 10.1 Core tables

```text
identities
work_sessions
presence_sessions
session_events
artifacts
application_manifests
session_summaries
capability_audit
```

### 10.2 Work-session fields

```json
{
  "id": "session-uuid",
  "owner_id": "identity-uuid",
  "title": "Build my resume",
  "status": "suspended",
  "created_at": "timestamp",
  "last_active_at": "timestamp",
  "summary": "Current task summary",
  "conversation_id": "conversation-uuid",
  "working_directory": "Documents/Resume",
  "artifacts": [],
  "applications": []
}
```

### 10.3 Event journal

Record:

- User and assistant messages.
- Applications launched and terminated.
- Files created or modified, including hashes where useful.
- AIOS-executed commands and tool calls.
- Permission grants and denials.
- Suspension, resumption, archive, and recovery events.
- Unfinished operations and recoverable errors.

Keep security logs separate from conversational memory. Never log PINs, raw credentials, biometric samples, or unnecessary sensitive response bodies.

### 10.4 Search and recall

Support queries such as:

- “Continue my résumé.”
- “What was I working on yesterday?”
- “Open the project where we fixed authentication.”
- “Show my unfinished sessions.”

Begin with metadata and full-text search over titles, summaries, paths, and messages. Add locally generated semantic embeddings later if they materially improve recall. Only search databases authorized for the active principal.

## 11. Presence loss, lock, and resume

Use two timeouts:

1. **Privacy timeout:** Quickly cover/hide personal content after loss of reliable presence.
2. **Suspension timeout:** After a longer absence, persist state and terminate the session process tree.

Both must be configurable and empirically tuned. Looking away briefly should not destroy a session, but walking away must hide private content promptly.

### 11.1 Lock sequence

1. Revoke sensitive capabilities immediately.
2. Display a trusted privacy shield.
3. Stop new personal tool/resource requests.
4. Ask managed applications to save through supported adapters.
5. Persist conversation, artifacts, summary, and restoration manifest.
6. Gracefully terminate application processes.
7. Force-terminate remaining processes after a deadline.
8. Unmount/lock private storage.
9. Clear clipboard, temporary audio, thumbnails, temporary attachments, runtime directories, and transient credentials.
10. Return the shell to a clean anonymous state.

### 11.2 Resume sequence

1. Establish a new presence lease for the returning person.
2. Mount/unlock their workspace at the appropriate authority.
3. Load recent suspended work-session metadata.
4. Offer the most likely task conversationally without revealing its details to an unverified bystander.
5. Reconstruct applications from allowlisted restoration adapters.
6. Reopen documents, working directories, and relevant conversation context.
7. Mark the work session active and append a resume event.

Do not depend on transparent process checkpoint/restore for the initial implementation. GPU state, network sockets, GUI state, and external services make arbitrary process restoration fragile. Reconstruct applications from durable state instead.

## 12. Multi-person interaction policy

- Maintain one identity candidate per tracked person.
- Associate each utterance with the most likely active track using voice identity, lip synchronization, recency, and optional microphone direction.
- Personal pronouns such as “my” refer to the active speaker only when attribution confidence is sufficient.
- A named request such as “open Ryan's résumé” does not grant the speaker access to Ryan's workspace.
- “Our” or explicitly shared tasks use a separately authorized shared workspace.
- If attribution is ambiguous, keep the action anonymous or ask who is speaking.
- Never retarget an existing personal work session because another person begins speaking.
- Hide private session details when additional untrusted people are present if the owner's privacy policy requires it.

## 13. PIN and sensitive-action security

### 13.1 Secure prompt

Implement a trusted PIN overlay owned by the shell/security broker, not QML supplied by conversational content and not an ordinary application window.

- Capture PIN input outside the LLM and session transcript.
- Clearly state the identity and requested scope.
- Return only success/failure and a scoped capability.
- Cancel on presence loss, identity conflict, timeout, or focus-integrity failure.

### 13.2 PIN storage and verification

- Use a user-specific PIN of at least six digits; allow a longer alphanumeric secret.
- Rate-limit attempts and introduce progressive delays.
- Lock sensitive access after repeated failures without destroying the user's data.
- Store a salted memory-hard verifier, with an additional hardware-protected secret when a TPM/TEE is available.
- Prefer using the PIN to unlock a hardware-protected key rather than treating it as the encryption key.
- Never log the PIN or retain it after verification.
- Provide an explicit recovery and re-enrollment process.

### 13.3 Capability examples

| Capability | Typical lifetime | Trigger |
| --- | --- | --- |
| `workspace.personal` | While recognized and present | Face/voice policy |
| `secrets.read:github` | A few minutes | PIN/passkey |
| `financial.read` | A few minutes | PIN/passkey |
| `message.send:<draft-id>` | Single use | Explicit confirmation |
| `financial.transfer:<transaction-id>` | Single use | PIN/passkey plus transaction confirmation |
| `system.install:<package>` | Single operation | Verified administrator authority |

## 14. Implementation phases

### Phase 0 — Architecture decisions and threat model

Deliverables:

- Architecture decision records for UID/container isolation, encrypted storage, display isolation, and capability tokens.
- Threat model covering spoofed biometrics, bystanders, poisoned enrollment, malicious prompts/documents, compromised apps, credential theft, session confusion, and unattended devices.
- JSON schemas for identity evidence, presence leases, work sessions, launch requests, and capabilities.
- Simulator that feeds deterministic identity/presence events without requiring a camera.

Exit criteria:

- Security boundaries and trusted processes are documented.
- Every sensitive resource has an enforcing broker.
- Tests can simulate one user, multiple users, disappearance, ambiguity, and spoof/conflict conditions.

### Phase 1 — Principal abstraction and anonymous sandbox

Deliverables:

- Replace direct `$HOME` assumptions with an explicit execution principal and workspace locator.
- Implement `aios-sessiond` skeleton and Unix-socket protocol.
- Create anonymous ephemeral workspaces and process scopes.
- Route shell-launched applications through the broker.
- Add authority/status indicator to the shell.

Exit criteria:

- AIOS remains fully usable without an enrolled person.
- Anonymous apps cannot read the current `aios` home, credentials, or personal-session metadata.
- Idle cleanup terminates the anonymous process scope and removes its data.

### Phase 2 — Personal workspaces and durable sessions

Deliverables:

- Identity UUID to Linux UID/workspace mapping.
- Per-user persistent storage and SQLite work-session journal.
- Work-session lifecycle APIs.
- Application restoration adapters for the terminal and first document editor.
- Manual developer-only identity selection to exercise the system before biometrics.

Exit criteria:

- Two manually selected users receive different files, histories, processes, and session lists.
- Leaving/suspending one user prevents access from the other.
- A résumé-editing session can be suspended and reconstructed after reboot.

### Phase 3 — Presence and face recognition

Deliverables:

- `aios-identityd` camera pipeline.
- Face tracking, quality filters, embeddings, encrypted template store, and enrollment flow.
- Privacy and suspension timers.
- Confidence hysteresis and developer diagnostics that do not expose raw biometrics.

Exit criteria:

- Passersby do not trigger enrollment.
- Known users are recognized across tested lighting and seating conditions.
- Recognition never independently authorizes sensitive resources.
- Presence loss reliably shields and suspends personal work.

### Phase 4 — Voice identity and multi-person attribution

Deliverables:

- Voice activity and speaker-embedding pipeline.
- Active-speaker correlation.
- Multi-track evidence fusion.
- Conflict/ambiguity conversational UX.

Exit criteria:

- With two enrolled people visible, ordinary “my” requests are attributed to the active speaker in the defined test matrix.
- Face/voice conflicts block personal activation.
- Recordings and synthesized voices are included in adversarial testing.

### Phase 5 — PIN, capability policy, and secret broker

Deliverables:

- Trusted PIN enrollment and entry surfaces.
- Rate-limited local verifier with optional TPM support.
- `aios-policyd`, scoped capabilities, and audit records.
- `aios-secretd` with at least one external-account integration.
- Transaction-specific confirmation flow.

Exit criteria:

- The LLM and user applications never receive the PIN.
- Recognized-only sessions cannot access protected secrets.
- Capabilities expire on timeout, use, presence loss, and identity conflict.
- Sensitive operations cannot bypass the resource broker by prompt manipulation.

### Phase 6 — Display isolation and production hardening

Deliverables:

- Wayland or isolated-compositor architecture.
- Clipboard/input/screen-capture/notification isolation.
- Anti-spoofing/liveness integration where supported.
- Secure update and model-integrity strategy.
- Biometric deletion/re-enrollment and account recovery.
- Performance, privacy, accessibility, and failure-mode testing.

Exit criteria:

- No shared-X11 security claim remains.
- User A cannot observe or inject input into User B's processes.
- Camera/microphone/model failure gracefully falls back to anonymous mode and explicit verification.
- Release documentation accurately states the assurance level and known hardware limitations.

## 15. Initial repository work breakdown

Suggested structure:

```text
apps/
  identity/
    service.py
    face.py
    voice_identity.py
    tracking.py
    enrollment.py
    evidence.py
  session/
    service.py
    principals.py
    workspaces.py
    lifecycle.py
    journal.py
    launch_policy.py
  policy/
    service.py
    rules.py
    capabilities.py
  secrets/
    service.py
    vault.py
  shell/
    IdentityStatus.qml
    PrivacyShield.qml
    EnrollmentFlow.qml
    SecurePinPrompt.qml
```

Additional system work:

- Add OpenRC service definitions for privileged brokers.
- Add dedicated service accounts and groups with minimal permissions.
- Add runtime directories beneath `/run/aios` with strict ownership.
- Add database migrations and versioned schemas.
- Add model manifests containing source, license, revision, checksum, and expected input/output formats.
- Add camera/microphone dependencies only after validating Alpine availability and ISO-size impact.
- Extend build/test scripts with service protocol, isolation, enrollment, and lifecycle tests.

Avoid putting all responsibilities into the existing Qt `Backend` class. Keep UI orchestration in the shell and enforce identity/session/secrets decisions in separately testable services.

## 16. Test strategy

### Functional scenarios

- Unknown person requests calculator and remains anonymous.
- Recognized person requests calculator and still remains anonymous.
- Recognized person requests “my résumé” and receives their workspace.
- User leaves briefly and receives privacy shielding without teardown.
- User remains absent and the work session is suspended.
- A second user arrives and cannot see or query the first user's work.
- First user returns and resumes the correct session.
- Anonymous artifact is explicitly claimed by an identified user.
- Multiple people are present and the active speaker is correctly attributed.
- Face and voice identities disagree and no personal session opens.
- Sensitive request triggers a secure PIN prompt.
- PIN grants only the described capability.
- High-risk action requires transaction-specific confirmation.

### Adversarial scenarios

- Printed photograph and replayed video.
- Recorded and synthesized voice.
- Person speaking from outside the camera frame.
- Bystander saying another user's name during enrollment.
- Model/prompt attempts to call a privileged service directly.
- Malicious document instructing the model to retrieve secrets.
- Application attempting to inspect another user's clipboard, display, processes, or files.
- Rapid user changes during application launch.
- Power loss during session journal writes.
- Camera/microphone unplug or permission failure.
- Brute-force PIN attempts and recovery abuse.

### Privacy verification

- Confirm raw frames/audio are not retained unexpectedly.
- Confirm logs contain no PINs, API keys, biometric templates, or sensitive response bodies.
- Confirm deletion removes templates, session metadata, credentials, and workspace data according to policy.

## 17. Metrics

Track locally and expose diagnostics without identifying data:

- Time from approach to stable presence track.
- Time from request to personal workspace activation.
- False recognition and false rejection observations from the test cohort.
- Ambiguity rate with multiple visible people.
- Active-speaker attribution accuracy.
- Privacy-shield and full-suspension latency.
- Session restoration success rate.
- Anonymous sandbox cleanup success rate.
- PIN failure/lockout behavior.
- CPU, memory, camera, and power usage at idle and during recognition.

Do not silently upload telemetry. Diagnostic export should be deliberate and scrubbed.

## 18. Recommended first executable milestone

Build a biometric-free vertical slice before integrating OpenCV:

1. Add `anonymous` and two test principals.
2. Create separate persistent workspaces for the test principals.
3. Route calculator, terminal, and editor launches through `aios-sessiond`.
4. Add a developer identity simulator to select `unknown`, `user-a`, `user-b`, `absent`, and `conflict`.
5. Implement work-session journaling, suspension, cleanup, and résumé restoration.
6. Add the trusted PIN prompt and one mocked protected resource.
7. Demonstrate that anonymous use never requires identity and cannot access either personal workspace.

Once this lifecycle and security model works deterministically, connect face and voice evidence to it. This prevents biometric uncertainty from obscuring bugs in session ownership, isolation, and authorization.

## 19. Definition of done

The feature is complete when a fresh AIOS machine can demonstrate this sequence:

1. An unknown person sits down and immediately launches a calculator anonymously.
2. They ask to create a personal résumé, enroll locally, and receive an isolated workspace.
3. They leave; private content is hidden, processes terminate, storage locks, and work remains resumable.
4. A second person uses AIOS and receives an independent anonymous or personal context without exposure to the first person's data.
5. The first person returns, is recognized, and resumes the résumé from durable session state.
6. Accessing credentials or financial data requires the user's PIN.
7. A consequential external action requires explicit, transaction-scoped approval.
8. All recognition and session-policy processing operates locally, and the system remains useful in anonymous mode when sensors or models are unavailable.
