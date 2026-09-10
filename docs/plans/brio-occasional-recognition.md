# Occasional camera capture for account recognition and authentication

Status: Proposed implementation; hardware capture verified in WSL only.
Date: 2026-09-09
Companion: [Brio control and validation](../qa/brio-camera.md).
Parent: [ambient multi-user identity plan](ambient-multi-user-identity.md).

## Outcome and trust boundary

With explicit camera-recognition opt-in, AIOS occasionally samples the dedicated
Brio, suggests a matching enrolled account, and greets that person in the chat
account bubble. Selecting the suggestion prompts for the account PIN/password.
A face match is identity evidence, not a credential and not permission to unlock
private work, release secrets, or perform consequential actions. Keyboard-only
account creation, selection and authentication remain fully functional.

This plan deliberately does not enable face-only unlock. A normal RGB frame,
multiple similar frames, or a blink is not proof of liveness. Until an evaluated
presentation-attack defense exists, do not set `Evidence.live=true` or relax
`Fusion` to make face matches pass. Use a separate unverified UI suggestion
channel. Retain PIN verification and existing capability enforcement. Future
face-based authorization requires a separate reviewed policy change and measured
spoof resistance; this implementation does not silently introduce it.

The current local greeting accounts in `chat_profiles.py` are distinct from
broker-protected identities. Keep their IDs and storage namespaces distinct;
never infer a protected account mapping from matching display names. A local
account greeting must not imply encrypted workspace access.

## Capture scheduling

Starting policy values are configurable engineering targets, not measured Brio
performance guarantees. Measure opening latency, negotiated format and CPU usage
inside the guest before finalizing them.

| State | Capture policy | Result |
| --- | --- | --- |
| Opt-out, physical cover, unavailable camera | No background capture; no retry storm | Generic bubble and keyboard/PIN flow |
| Anonymous desktop with opt-in | One short burst every 15 seconds, only while desktop is active | Optional account suggestion |
| User opens account menu or requests recognition | Immediate burst, subject to a two-second cooldown | Refresh suggestion; never submit credentials |
| Explicit biometric enrollment | Guided finite bursts for up to 30 seconds | Quality-checked samples after consent |
| PIN verified personal session | Use the existing manual-session policy initially | Occasional photos cannot claim continuous presence |
| Future validated presence mode | Separate bounded presence loop, target one observation per second | Feed fresh, authenticated evidence under existing policy |

A recognition burst starts with a target of three fresh frames in at most two
seconds, at 640x480 using a supported negotiated format/rate. Warm up and drain
stale buffers, assign monotonic capture timestamps and sequence numbers, then
release the device. Keep only a bounded latest-frame buffer; never queue an
unbounded backlog. If opening plus warmup cannot meet the deadline, fail the
burst and retain manual access rather than recycling cached imagery. Retry
missing/busy devices with capped exponential backoff (2, 5, 15, 60 seconds),
resetting on an explicit device-add event. There is one camera owner, one burst
at a time, and no new capture while disabled or shutting down.

The existing `Fusion` has a three-second TTL and one-second dwell; sessions
shield after three seconds of missing required presence and suspend after 30.
Do not stretch evidence lifetime to bridge a 15-second sampling interval. UI
suggestions expire after five seconds and immediately on device loss, lock,
account deletion, ambiguity or opt-out. A future presence loop must demonstrate
its latency under load fits the existing timeout, or it must fail closed.
It cannot use unvalidated RGB matching to extend a verified session indefinitely.

## Components and ownership

1. Add a dedicated capture service adjacent to `apps/aios/camera.py`. Keep the
   existing bounded probe as a diagnostic. The service alone opens the selected
   UVC capture node; run it unprivileged with only required device access.
   Configuration selects a verified stable local device ID, never a network URL
   or a device path supplied by chat/model output. Device unplug/replug creates
   a new capture generation and invalidates prior observations.
2. Add a replaceable local detector, alignment and embedding worker. Reject
   blurred, poorly lit, undersized or inconsistent crops. Use absolute similarity
   and runner-up margin checks via `identity.match`, calibrated on consented
   held-out samples. No arbitrary production thresholds or assumed accuracy.
   Pin model versions and checksums; verify license and redistribution terms
   before bundling. The parent plan's YuNet/SFace proposal remains a candidate,
   not a validated or newly approved dependency choice.
3. Add an authenticated local IPC adapter. Validate peer identity, message size,
   schema, freshness, generation and sequence before accepting results. Publish
   only account UUID, confidence category, timestamps and reason codes; no raw
   image or embedding in shell IPC. An untrusted application cannot submit a
   recognition result, set liveness, switch session ownership, or mint a capability.
4. Expose unverified suggestions through `SessionControl.h` to `UserBubble.qml`.
   Label suggestions as needing PIN confirmation. In a single clear match, offer
   the account and its stored portrait; in unknown or ambiguous scenes, show the
   generic head and New account option. Do not enumerate possible matches in a
   multi-person scene or automatically enroll strangers. Each chat keeps its
   existing owner; a newly recognized person never takes over an open session.
5. Arbitrate enrollment/photo capture (`ProfilePhoto.h`) with background sampling.
   Explicit photo/enrollment capture takes priority; pause background acquisition
   and resume only after release. Prefer one acquisition service with explicit
   consumers over competing QCamera/OpenCV handles. Process termination must
   release the device. The shell must not become a root camera broker.

## Enrollment, storage and deletion

Offer recognition opt-in separately from account creation and profile photography.
Name plus PIN is sufficient to create an account. Adding face templates to an
existing account requires verification of that account's PIN and explicit consent.
Explain that samples stay local and recognition can be disabled. Capture multiple
quality-controlled samples with modest pose variation, reject multiple faces, and
ask for another attempt when quality is inadequate. A chosen avatar is not an
authentication template; do not silently derive templates from it.

Store versioned embeddings encrypted in the intended account namespace using
`secure_store.py` after reviewing key access. Bind templates to account UUID,
model version, schema and consent version. Migration between local greeting and
protected identities requires an explicit verified operation. Account deletion
and explicit facial-data purge revoke templates, cached matches and active capture
jobs; disabling recognition only pauses capture so it can be re-enabled without
enrollment. Ensure the next restart cannot reload purged templates. Define
encrypted-backup retention and key rotation before claiming complete erasure.

Raw frames and crops stay in memory and are released immediately after inference;
no screenshots, training collection, crash dumps with images, chat attachments,
telemetry images or debug frame files. Do not promise perfect memory zeroization
from Python; bound lifetime and access instead. Audit consent changes, service
health and authorization decisions without images, embeddings, PINs or stable
bystander tracking. Inference is local and offline; this plan does not enable the
Brio microphone or voice identification.

## Work packages and completion gates

### 1. Verify Brio in the full VM

Use the documented USB handoff and normal windowed OS launcher. Confirm at least
three bounded ten-frame captures by the intended unprivileged guest user, actual
negotiated format/rate, reopen latency, permission failures and unplug/replug.
Save only aggregate counts/timing and reason codes. WSL success is not this gate.
Resolve image dependencies if using the experimental protected-session image;
the normal desktop VM does not prove protected appliance startup.

### 2. Implement bounded acquisition and camera arbitration

Add opt-in scheduler, deadlines, latest-frame buffering, exclusive ownership,
state/health IPC and cancellation. Use fake clock and capture adapters to test
cadence, backoff, concurrent requests, stale frames, device generations and
opt-out. Verify process kill or blocked driver read cannot stall the shell.
Acceptance: deadline failure returns unavailable, raw-frame retention stays
bounded, and keyboard account flows still work with no camera installed.

### 3. Implement verified enrollment and encrypted templates

Connect consent and PIN verification to template writes. Test wrong PIN, canceled
consent, poor quality, two faces, model-version mismatch, corruption, deletion
while capturing, restart after deletion, and cross-account isolation. Acceptance:
no template exists without verified opt-in and no secret reaches UI logs.

### 4. Implement recognition suggestions

Connect local model inference to conservative matching and expiring UI suggestions.
Test known, unknown, ambiguous and conflicting users; closed cover; a printed
photo and replayed video; and rapid account switching. Spoofs may fool a suggestion
but must never bypass PIN or refresh protected presence. Verify name/portrait
visibility follows opt-in and that stale suggestions cannot change a chat owner.
Acceptance: a consenting enrolled user can be suggested and then sign in by PIN;
an unknown user can create an account entirely with the keyboard.

### 5. Evaluate before enabling unattended operation

Use consented participants and separately held-out enrollment/validation samples.
Record false match/reject rates, ambiguity rates, lighting/pose failures, time to
suggestion, reopen latency, CPU/memory and camera duty cycle. Set documented
acceptance thresholds before collecting the evaluation results. Do not claim
fairness, liveness or authentication accuracy from a successful capture test.
Any later validated presence mode must additionally test TTL/dwell boundaries,
worker crashes, late IPC, absence shielding and suspension under load. Keep
face-only authorization disabled until its separate policy and attack evaluation
are approved.

## Delivery and rollback

Ship disabled by default, with camera recognition settings showing enabled,
capturing, unavailable and manual-only states. Explain why the camera activates
and provide a one-action enable/disable control plus a separate facial-data purge.
A failure clears only unverified suggestions and follows existing session privacy
rules; it never silently selects another account. Preserve all existing PIN
lockout and capability rules.

Deliver the packages in reviewable PRs with their gate results and remaining
limitations. First usable milestone is a verified guest capture plus optional,
PIN-confirmed account suggestions. Rollback disables the scheduler and restores
the existing keyboard account flow; it does not delete accounts or personal data.
