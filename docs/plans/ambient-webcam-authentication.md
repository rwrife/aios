# Finish ambient webcam interaction, authentication, and chat photos

Status: Proposed implementation plan; no runtime behavior is enabled by this PR.
Date: 2026-09-10
Baseline reviewed: `origin/main` at `7789e0e`.

## 1. Intended experience

The webcam is optional. When no webcam is connected, the expected way to identify
users is the normal manual authentication flow: open the account bubble, select
an existing account and enter its PIN/passphrase, or create an account through
the native account UI. This is a supported primary experience, not an incomplete
setup or error condition. It must work without camera hardware, face enrollment,
recognition models or biometric consent. The same manual flow remains available
when camera use is disabled or unavailable.

With ambient camera use enabled, the idle desktop watches locally for a person
who stays near the computer. Passing through the picture does not trigger a
greeting. A sustained unknown visitor makes the chat orb brighten. Clicking or
keyboard-activating the orb opens an anonymous chat with an account invitation.
With that person's consent, AIOS gathers several useful face samples during
the introduction and account setup, then binds them to the newly created account.

A sustained recognized visitor gets a different orb color. On interaction,
AIOS rechecks the person, greets them by name, and opens their personal context
at the **Low** access level when the authentication prerequisites below pass.
Low includes ordinary memories, conversation history, and personal files. Native
PIN/passphrase entry raises that chat to **Full** access. Recognition never
silently transfers an existing chat to another person.

During consented interactions AIOS collects a few additional samples, especially
around successful PIN verification, to improve recognition across appearance
and lighting changes. Separately, a camera button in the chat composer lets the
user take, preview, and send a photo so a vision-capable LLM can see a reference.

This plan includes the dependencies needed to finish that experience, rather
than treating the existing greeting-profile demo as protected authentication.
Implementation is camera-only; microphone capture and speaker identification
are outside this work. A later trusted voice-activation path may invoke the same
interaction API, but arbitrary speech or model text is not identity evidence.

## 2. Existing implementation and gaps

| Area | Present implementation | Work still needed |
| --- | --- | --- |
| Capture | `apps/aios/camera.py` enumerates devices and runs bounded, discard-only probes; `ProfilePhoto.h` captures an optional 64×64 portrait | One supervised camera owner, continuous low-rate idle sensing, full photo capture, all-consumer arbitration |
| Face suggestions | `recognition.py` performs three-frame bursts, checks a pinned/calibrated manifest, and stores encrypted embeddings; `SessionControl.h` schedules suggestions roughly every 15 seconds | Persistent tracks, separate unknown/known dwell, authenticated service events, no-enrollment visitor detection, protected identity integration |
| Recognition policy | `biometrics.py` supplies face encoding/tracking; `identity.py` requires stability, interaction, liveness, freshness, and match separation | Calibrated production policy, evaluated anti-spoofing, robust track continuity, explicit interaction attribution |
| Accounts | `chat_profiles.py` supplies local name/PIN/portrait profiles; `AccountSettings.qml` supports PIN-confirmed face enrollment | Broker-owned accounts as the authority, verified migration, onboarding sample collection and atomic binding |
| Personal access | `sessions.py`, `sessiond.py`, and `authority.py` implement leases, recognized/verified levels, PIN limits, capabilities and suspension | Protected chat/model/history/file paths and per-chat authorization; legacy workers still run as desktop user |
| Display | Optional embedded per-lease Wayland compositor and isolation tests exist | Protected appliance startup and physical input/display validation; shared X11 is insufficient |
| Orb | `ChatOrb.qml` has brightness/activity rendering; `Main.qml` launches chat | Semantic invitation states, interaction handoff, expiry, suppression, accessible state labels |
| LLM photos | `ChatWindow.qml`/`main.cpp` support text attachments; `core.py` currently validates string-only message content | Typed image attachments, provider-specific image serialization, protected storage and capability checks |
| Hardware | `docs/qa/brio-camera.md` records bounded Brio 101 capture in WSL | Real Alpine guest capture, reopening/unplug tests, model packaging, sustained runtime and calibration |

The older [occasional-recognition plan](brio-occasional-recognition.md) remains
the suggestion-only fallback. This plan proposes two explicit extensions to it:
broker-authorized Low access after interaction, and consented sample collection
plus optional encrypted crop retention. Its old no-training-storage and
PIN-for-every-suggestion assumptions do not describe the new target. The
[ambient identity plan](ambient-multi-user-identity.md),
[architecture decisions](../architecture/ambient-identity-decisions.md), and
[identity status](../identity-sessions.md) remain the basis for isolation and
capabilities. Update those documents with the corresponding implementation PRs;
do not remove their release gates merely to make the new flow appear complete.

## 3. Access policy and trust boundaries

“Low” describes an assurance tier, not public data. History, memories and files
are private and require a real authorization boundary.

| UI state | Broker state | Allowed access |
| --- | --- | --- |
| Guest | `anonymous` | Generic chat and anonymous temporary artifacts; no personal retrieval |
| Recognized invitation | No authority change | Distinct orb cue; no personal context loaded and no name announced across the room |
| Low | `recognized`, active owner-bound lease | That account's ordinary memories, history and files through scoped resource APIs |
| Full | `verified`, expiring owner/chat-bound lease | Protected resources allowed by policy after native PIN/passphrase verification |
| Confirm operation | `transaction_confirmed` capability | One exact consequential action, such as sending a message, transferring money, or deleting an account |

Full means full policy-permitted account access, not root privileges, unlimited
secret export, or blanket approval for external actions. Start with the existing
120-second verification lifetime; presence may maintain Low but cannot extend
Full. On Full expiry, revoke elevated capabilities and remove elevated context
before continuing at Low; if presence is stale, shield instead.

Required changes:

1. Keep identity observations in the trusted identity service. The broker alone
   validates evidence and issues leases. Neither shell display metadata nor an
   LLM-generated `recognized` field can authorize anything.
2. Require fresh stable evidence, an unambiguous person, a trusted explicit
   interaction, calibrated match and runner-up separation, and evaluated
   presentation-attack resistance before granting Low. Ordinary RGB matching,
   several similar frames, or a blink alone do not establish liveness. Never
   hardcode `live=true`. If the Brio-only path cannot pass the attack evaluation,
   retain PIN fallback and evaluate an additional sensor or authenticator; mark
   the requested face-to-Low milestone incomplete rather than weakening the gate.
3. Bind each interaction ticket to capture generation, track, candidate UUID,
   shell event, expiry, and destination chat. Revalidate on activation and consume
   once. A click when two people are visible does not identify which one clicked;
   require a single-person view or native PIN selection.
4. Extend the existing broker's single active personal context deliberately:
   carry owner, chat/work ID, lease and authority through every protected request.
   Model workers, browser processes, caches and tools use that context. Initially
   allow one foreground person's presence with independently scoped chats;
   other owners' chats stay suspended. A PIN in chat A does not elevate chat B.
5. Add explicit memory/history/file operations and protected-resource labels.
   Unknown/unclassified resources default to Full until classified through a
   trusted policy. The LLM cannot lower a label. Credentials and financial records
   stay Full even if embedded in a file or transcript; define labels on write and
   conservatively classify legacy imports before exposing them at Low.
6. Enforce policy on retrieval, search results, thumbnails, exports, model context,
   cached summaries and filesystem access. A Low worker must not receive a mount
   containing Full-only files; use filtered broker reads or separate subtrees.
   On downgrade, cancel pending responses and rebuild workers/context without
   Full-only information. Hiding a message in QML is insufficient.
7. Preserve existing conflict shielding and teardown. No display-validation flag
   bypass, recognition-as-PIN shortcut, or legacy greeting-profile UUID shortcut.

## 4. Idle presence and invitation state machine

These numbers are initial tunable engineering targets, not measured recognition
accuracy. Store versioned, range-checked settings; calibrate before release.

| Parameter | Initial target | Semantics |
| --- | --- | --- |
| Desktop idle | 30 seconds | No trusted input; desktop visible and no enrollment, secure-input overlay or explicit capture active |
| Idle sensing | 2 inference observations/second | One open low-resolution stream, newest frame only; do not reopen on every observation |
| Unknown dwell | 5 continuous seconds | Same qualifying single-person track near the interaction area |
| Known dwell | 3 continuous seconds | Same candidate above invitation threshold with adequate runner-up separation |
| Track gap | At most 0.75 seconds | Larger gap resets dwell; never accumulate disjoint visits |
| Invitation expiry | 10 seconds without fresh qualifying evidence | Clear immediately on ambiguity, device-generation change or camera opt-out |
| Ignore cooldown | 60 seconds | Suppress repeated requests for the same short-lived track |
| Decline cooldown | 10 minutes | Suppress enrollment invitations device-wide without persisting stranger identities |
| Enrollment window | Up to 45 seconds after consent | End early when sample quality/diversity target is met |
| Protected presence | At least 2 observations/second | Measure end-to-end age against existing three-second freshness/privacy limits |

States: `disabled`, `unavailable`, `idle-watch`, `qualifying-unknown`,
`qualifying-known`, `invite-unknown`, `invite-known`, `interacting`, and
`cooldown`. Keep this presentation state separate from broker authority.

- Enable watch only after device-level ambient-camera opt-in. It also runs with
  zero enrolled accounts; current `recognize()` exits with `no-enrollment`, so
  detection must be separated from gallery matching.
- A face must be large enough, adequately lit/sharp, and consistently within the
  calibrated interaction area. Passing faces, poor observations, track switches
  and multiple plausible people do not count toward dwell. Improve the current
  centroid tracker before using continuity for identity binding.
- Only one invitation is visible. Unknown-to-known transitions reset the relevant
  dwell; a fluctuating match cannot alternate orb colors every frame. Use
  hysteresis with independently calibrated invitation and authorization criteria.
- No automatic chat opening, speech, account creation, private context retrieval,
  or durable stranger gallery. The ignored invitation fades to normal.
- On interaction, capture/revalidate a current observation. If the invitation is
  stale, show a generic greeting while checking again. Known greetings use trusted
  escaped display names; generated dialogue cannot select or create the identity.
- Existing personal chats retain their owners. Opening an invitation creates a
  fresh chat or explicitly resumes the matching account's chosen work session.
- Active generic input suppresses recruiting invitations. Protected presence
  monitoring continues for an active personal lease even when the desktop is not
  idle. Do not equate Qt application focus with appliance idle/absence.
- Camera loss, service crash or contested ownership clears invitations immediately.
  Protected leases follow the existing three-second shield/30-second suspend
  policy; conflicts shield immediately. Reconnection starts a new generation and
  never resurrects a cached lease or invitation.

## 5. Camera service and protocol

Add a supervised unprivileged camera/identity service alongside `camera.py` and
`recognition.py`, with OpenRC lifecycle and access to only the selected local
capture node. Keep the diagnostic probe separate. Reuse model integrity checks,
but load models once, outside the shell event loop. Stable device selection must
survive unplug/replug without silently switching to a different camera.

The service owns the device for idle watch, enrollment, maintenance samples,
settings/setup preview, profile portraits, and chat photos. Replace independent
QCamera/OpenCV acquisitions in `SettingsWindow.qml`, `SetupWizard.qml`,
`ProfilePhoto.h`, and `SessionControl.h` with purpose-specific clients. Priority:
explicit user capture/enrollment, required protected-presence checks, idle watch.
Where explicit capture interrupts protected evidence, keep the TTL honest and
shield if necessary; do not reuse a chat photo as identity evidence.

Use a private Unix socket with peer credentials, distinct allowed caller roles,
strict fields, frame-size limits, deadlines and cancellation. Proposed contracts:

| Contract | Trusted caller / output | Bounds and authority |
| --- | --- | --- |
| `presence_status` / subscription | Shell; state, generation, sequence, observation age, invitation category and opaque ticket | Metadata only; does not return biometric vectors or grant access |
| `observe_identity` | Identity service to broker; track/candidate, quality, liveness provenance, sequence and timestamps | Only identity-service UID; stale/replayed generations rejected |
| `accept_invitation` | Shell to broker; one-use ticket and target chat | Broker checks fresh evidence and interaction; returns bounded status |
| `begin_enrollment`, `cancel_enrollment` | Native account UI; opaque enrollment ID and progress | Consent-bound transaction; broker selects account UUID |
| `commit_samples` | Identity service to broker/store | Requires enrollment or maintenance permit, matching track and model revision |
| `capture_chat_photo`, `cancel_capture` | Chat UI; chat-bound opaque attachment handle and preview | User gesture, five-second deadline, no arbitrary device/path or continuous stream |
| `camera_settings` | Trusted settings adapter | Readback for enable/pause/device; persistent opt-in, structured busy/unavailable/error results |

Transfer images only on the dedicated preview/attachment channel, with bounded
dimensions and payloads, not in ordinary status events. Use sealed memory/file
descriptors or opaque broker-managed storage handles; an agent cannot supply an
arbitrary path. A capture subprocess deadline must terminate blocked driver reads
and release the descriptor. Bound queues to the latest frame and cap retries at
2, 5, 15, then 60 seconds. Discard late completions after cancellation or revocation.

If agent camera settings are exposed, update `apps/skills/os-control/SKILL.md`,
the `os_settings` schema and `docs/agentic-tools.md` together. Model tools may
request the native flow and observe completion; they cannot enroll a face,
collect a PIN, force a capture, or submit synthetic identity evidence.

## 6. Unknown-person onboarding and account binding

1. Idle detection uses transient frames only. It does not retain a pre-consent
   photo collection. On orb interaction, show a native greeting and “Would you
   like to create an account?” with Create account and Continue as guest.
2. Explain local face recognition and offer separate consent for enrollment,
   ongoing improvement, and optional retained face crops. Beginning consented
   setup starts capture while the person continues the introduction and enters
   their name/PIN. Do not require PIN entry before collecting a new account's
   temporary samples, and never put that PIN into chat/model messages.
3. Collect 8–12 accepted, aligned samples spanning at least three modest pose or
   lighting groups over the bounded window. Enforce spacing (initially 500 ms),
   duplicate rejection, face-crop quality and within-person consistency. Quality
   and diversity, not elapsed time or a raw frame count, determine completion.
4. Keep the temporary batch in service memory, bound to a random enrollment ID,
   consent revision, camera generation and continuous track. Pause/reset on a
   second face, track discontinuity, cover or sensor failure. Give simple progress
   guidance without showing raw confidence scores.
5. Native account creation asks the broker to allocate a UUID and PIN verifier.
   Extend its existing durable enrollment intent to commit account, consent and
   template association consistently. Never join records by display name. The
   broker validates the one-use sample permit, not an owner supplied by the LLM.
6. If setup succeeds before enough samples exist, complete the account as
   PIN-only, explain that face setup needs finishing, and offer a bounded retry.
   Do not block account access on camera quality or invent successful enrollment.
7. On cancel, consent withdrawal, timeout, track change or allocation failure,
   discard temporary samples. Crash recovery can finish only committed, valid
   intents; incomplete samples never become another account's templates.
8. A face resembling an existing account does not merge accounts or recover one.
   Offer native PIN sign-in. Linking an existing legacy greeting profile to a
   broker identity requires explicit verification of the relevant accounts and
   an atomic UUID mapping; conflicting mappings require resolution, not guessing.

Keyboard-only account creation and recovery remain available throughout.

## 7. Recognition learning, storage and deletion

“Training” initially means improving a bounded per-account embedding gallery.
Do not fine-tune the detector/encoder online on self-labeled webcam observations.

- On a consented recognized interaction, collect at most three diverse candidate
  samples per encounter. Samples based only on recognition remain quarantined
  in memory for at most two minutes and cannot modify the trusted gallery or
  reinforce their own match. If no verification follows, discard them.
- Successful native PIN verification creates a one-use maintenance permit tied
  to that account, chat, capture generation and track. Capture or validate a few
  fresh samples within ten seconds; require a single continuous face and quality
  checks. PIN verifies the account credential, not every face visible nearby.
  Capture can resume immediately after the secure overlay closes; no PIN-screen
  pixels or keystrokes enter the camera/learning channel.
- PIN-associated samples receive verified provenance, but admission still checks
  consistency and outliers. A major mismatch requests explicit re-enrollment;
  it never silently overwrites the enrolled identity. Support glasses, hairstyle
  and gradual appearance changes through diverse samples and intentional updates.
- Initial caps: 24 trusted embeddings per account, at most three additions per
  verified encounter and six per day. Preserve initial enrollment anchors; replace
  redundant non-anchor samples using diversity/quality, not merely oldest-first.
  Version galleries and allow rollback after a bad update.
- Default persistence is encrypted embeddings plus minimal provenance: account
  UUID, model digest/revision, schema, consent revision, capture time, quality,
  source (`enrollment` or `pin_verified`) and gallery version. Embeddings remain
  sensitive biometric data. The identity service alone reads the gallery.
- Optional “Keep face samples for recognition improvement” saves only accepted,
  aligned face crops encrypted under broker-controlled account keys, maximum 24
  crops/account with a 30-day rolling retention. Full frames, bystanders, discarded
  candidates and debug media are never persisted. Default off; the requested
  multi-photo collection still supports embedding enrollment without keeping JPEGs.
- Retained crops permit deliberate future re-encoding after a model upgrade.
  Without crops, require re-enrollment for incompatible embeddings. Store new
  revisions separately and evaluate before switching; never compare incompatible
  model spaces or silently download new weights.
- Device pause stops capture; disabling enrollment removes consent for further
  matching/learning. Provide distinct pause, revoke recognition, delete face data
  and account deletion controls with accurate scope. Purge removes templates,
  crops, quarantine, versions and cached tickets, cancels in-flight writes, and
  remains effective after restart and backup restore. Define backup expiry and
  key destruction; do not promise physical overwriting of SSD blocks.
- Audit consent, admission/rejection counts, version changes and failures without
  images, embeddings, PINs or enduring unknown-person identifiers. Disable image
  crash dumps/debug logging. No automatic network upload of biometric samples.

## 8. Orb and native chat UX

Extend `ChatOrb.qml` with semantic state properties supplied by a controller,
not recognition logic inside the renderer. Use `Theme.wave` for unknown invitation
and `Theme.accent` for known invitation, with a restrained outline/brightness
change. Test both Ocean and a generated palette; if roles are insufficiently
distinct, add a shared semantic role in `Theme.qml` rather than local hex colors.
Also distinguish states through an accessible description and small shape/outline
variation, so color is not the only signal. Reduced motion uses a static cue.

Do not announce the person's name before interaction. After broker acceptance,
use a deterministic greeting such as “Welcome back, [name]” and a visible Low
access indicator. The native account bubble offers “Unlock full access” and
shows expiry/downgrade status. If matching or liveness is insufficient, offer
native PIN sign-in without claiming Low was unlocked.

Unknown greeting, enrollment progress, camera status and PIN entry are trusted
UI surfaces. The LLM may help with ordinary conversation, but cannot skip consent,
claim an account was created, or change capture/authentication state through text.
Keep camera activity visible with an immediate pause control. Respect keyboard
focus, accessible names/tooltips, existing quiet controls, window sizing and
`WindowBorder.qml` requirements. Arbitrate invitation cues with chat activity and
scheduled-job notifications: authentication/privacy and active interaction take
precedence; an invitation never overwrites another chat's activity indicator.

## 9. Camera button: giving the LLM a reference photo

1. Add an accessible Camera button beside Attach in `ChatWindow.qml`. It requests
   one photo for this chat. Show unavailable/busy status and allow cancel without
   blocking the composer. This explicit action works even when ambient recognition
   is disabled, provided the camera has not been globally prohibited.
2. Display a bounded native preview with Take photo, Retake, Use photo and Cancel.
   An attached thumbnail remains removable. Capture/Use photo never submits the
   message automatically; Send is the boundary for model delivery.
3. Start with a maximum 1600-pixel long edge, 2 MiB encoded photo and four total
   attachments/message, adapting downward to a provider's declared limits. Decode,
   validate and re-encode to supported JPEG/PNG; strip metadata. Preserve object
   detail for the LLM rather than reusing the 64×64 profile portrait.
4. Introduce typed text/image content and versioned conversation persistence in
   `main.cpp`, `worker.py`, `core.py`, `subscription.py`, and the journal/storage
   path. Keep old text-only histories readable. Add provider capability metadata;
   serialize image content for each supported backend and fail visibly for a
   text-only model. Do not silently drop photos or pretend the model saw them.
5. Keep attachment handles chat/owner/lease-bound. Recheck at Send and on history
   load. Store private sent images in the correct encrypted workspace; pending
   captures are temporary and disappear on cancel, chat close, lease loss or
   service crash. Guest attachments remain guest-scoped and are removed with the
   guest session unless explicitly claimed through the verified artifact flow.
6. Indicate before Send whether the selected model processes the photo locally or
   sends it to a named remote provider. Existing external-data policy applies;
   do not use identity-service upload paths. A provider failure preserves a
   removable draft without duplicating submissions during retries.
7. Never use chat reference photos for enrollment or learning. Never expose the
   ambient camera stream or biometric gallery to the LLM. Image content, including
   photographed instructions, is untrusted reference material and cannot grant
   tool capabilities. Camera access is native, not a browser permission exception.

## 10. Delivery sequence and acceptance gates

Each package is a separate reviewable implementation PR or small PR group.
Dependencies below prevent UI completion from being mistaken for secure sign-in.

| Package | Work and principal files | Dependencies | Acceptance |
| --- | --- | --- | --- |
| A. Policy/contracts | Identity ADR, schemas, `authority.py`, resource classification, consent/retention settings | None | Reviewed state/access matrix, initial test parameters and measurable release criteria recorded |
| B. Hardware/runtime | Camera service, OpenRC, image dependencies/model manifests, launcher diagnostics | A | Intended guest UID captures fresh frames in real Alpine; unplug, cover, blocked read and reopen are bounded |
| C. Idle invitations | Track/dwell controller, `recognition.py`, `SessionControl.h`, `ChatOrb.qml`, `Main.qml` | B | Unknown visitors detected with empty gallery; passers-by ignored; cues expire, differ accessibly and never unlock data |
| D. Onboarding/gallery | Enrollment UI, account settings, secure store and broker enrollment intents | A, B | Consented diverse samples bind atomically to correct UUID; PIN-only fallback, cancellation and deletion work |
| E. Protected chat access | Broker-owned workers/storage, per-chat leases, resource labels and protected display startup | A; hardware/display validation | Low/Full enforced below UI across retrieval, files, model context and tools; cross-account and cross-chat attacks fail |
| F. Recognized-to-Low flow | Calibrated matcher/liveness, interaction ticket, lease activation, native Full step-up | C, D, E | Known interaction opens correct private context without PIN only after attack/display gates pass; PIN elevates only target chat |
| G. Ongoing improvement | Quarantine, PIN-bound permits, gallery admission/rollback and crop retention | D, E, F | Recognition cannot self-train; PIN plus continuous single-person track admits bounded samples; poisoning tests pass |
| H. Chat camera reference | Camera preview, typed attachments, backend adapters and history rendering | B, A; E for personal storage | Preview/retake/send works; supported LLM receives image; unsupported models fail clearly; lease loss prevents delivery |
| I. Release validation | Local Alpine build, real camera, physical protected display, calibration report and UX screenshots | All | Entire scenario matrix passes with documented limits and rollback |

After contracts, the invitation, protected-chat and photo-attachment packages can
progress independently. Integrate changes before expensive local builds to batch
VM validation. Do not use GitHub to build ISO images. Launch QEMU through the
documented Windows/WSL flow with a unique task-specific `-name`, allow boot to
finish, and use the real guest rather than treating a container preview as proof.

## 11. Verification plan

Backend tests stay display-independent. Extend `test_camera.py`,
`test_recognition.py`, `test_identity_protocol.py`, `test_identity_sessions.py`,
`test_chat_profiles.py`, and add focused presence/gallery/attachment tests with
fake clocks and capture adapters. QML tests cover controls and renderer states;
existing real Linux and display-isolation suites remain separate gates.

| Scenario | Required result |
| --- | --- |
| Unknown person passes for 1–4 seconds | No invitation, account or retained samples |
| Unknown person remains past threshold, ignores orb | Unknown cue only; expires/cools down without repeat nagging |
| Unknown person interacts and declines | Guest chat, no durable face data, decline cooldown |
| Unknown person consents and completes account setup | Diverse gallery linked to new UUID, or explicit PIN-only completion if samples insufficient |
| Known person remains, then clicks | Different cue; fresh ticket yields correct Low lease only with valid liveness/isolation |
| Known cue clicked after person leaves or changes | No stale greeting/private retrieval/owner assignment |
| Two people, track crossing, similar-looking candidates | Ambiguity blocks identity activation and sample admission |
| Printed photo, phone replay, recorded video | No Low authorization; absence of evaluated defense is a release blocker |
| Correct PIN, wrong PIN, brute-force restart | Only correct PIN elevates; existing persistent limits survive restart |
| PIN in chat A while chat B is open | Only A elevates; B's data/tool permissions unchanged |
| Full expires with protected content in context | Pending output canceled; worker rebuilt at Low, no protected content leak |
| Person leaves, camera fails or service stalls | Timely shield/revocation and suspension; no TTL extension from cached frames |
| Successful PIN with another person entering frame | No automatic learning from the other face |
| Repeated recognition without PIN | No trusted-gallery growth or feedback loop |
| Account deletion/purge races with capture or commit | No resurrected gallery after completion, restart or restore |
| Disk full, power loss, failed profile allocation | Existing accounts usable; incomplete association never committed to wrong owner |
| Reference photo retaken/canceled/sent | Only selected photo sent after Send; no gallery reuse; correct history/owner binding |
| Model lacks vision or remote request fails | Clear recoverable status; no false “image seen” claim or duplicate send |
| No camera, opt-out, closed cover, inaccessible device | Normal manual account selection, PIN/passphrase authentication, account creation and guest use work without biometric prerequisites; no camera-required setup or repeated missing-camera prompts |

Before evaluation, freeze numerical release criteria and the test protocol in a
local calibration report: false accept/reject bounds, confidence intervals,
impostor attempts and participant count, attack success criteria, invitation
false-positive rate, acceptable p95 interaction latency, CPU/RAM and duty cycle.
Do not choose thresholds after looking at the final test set. Use consented,
held-out participants/sessions across lighting, pose, glasses and appearance;
measure account-level gallery matching rather than only pairwise similarity.
No universal similarity score or small successful demo establishes certainty.

Measure freshness and the three-second privacy deadline under CPU pressure and
concurrent photo capture. Record three successful bounded guest capture probes
plus sustained idle/interaction runs and repeated reconnects. Keep aggregate
results, model digests and redacted UX screenshots; never commit raw samples,
embeddings, PINs or machine-specific device serials. Test Ocean and one generated
palette, keyboard-only flows and reduced motion. Browser changes, if required by
worker migration, also need sandbox-enabled Alpine page/input/navigation/scroll,
cleanup and two isolated concurrent-chat validation from `AGENTS.md`.

## 12. Rollout and definition of done

Ship separate disabled-by-default flags for ambient invitations, face-to-Low
activation, adaptive enrollment and optional crop retention. Explicit chat photos
are independent. Show prerequisites and failure reasons accurately in Settings.
Suggestion-only operation may ship before face authorization, but is a partial
milestone. Do not enable Low access on ordinary X11 or call the feature finished
while protected workers, physical display validation or anti-spoofing are missing.

Rollback disables invitations/learning, revokes biometric leases and returns to
native PIN access. Preserve accounts/history; do not drop personal data or weaken
permissions to keep an older build running. Gallery/schema changes require
version-aware readers and an explicit migration/rollback path.

Complete means the entire intended experience works on the real supported device:
quiet idle detection; distinct unknown/known invitations; consented account-bound
sampling; interaction-triggered Low access to the correct private context;
per-chat PIN Full access; reliable departure locking; bounded verified learning;
and a working preview-and-send camera attachment to a vision-capable model.
It also means a camera-free installation supports normal manual identification
and authentication end to end, including account creation, selection, PIN-based
personal access and recovery. Camera presence or recognition enrollment must
never be a prerequisite for those flows.
All privacy, isolation, replay, retention and hardware gates above must have
recorded results. This documentation PR implements none of those runtime changes.
