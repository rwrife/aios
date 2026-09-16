# Recognition suggestion channel

Suggestions are local greeting metadata. They cannot authenticate, activate a
broker session, change a chat owner, update Fusion presence, assert liveness, or
mint capabilities. Existing signed-in chat profiles take precedence over any
candidate. Protected identity surfaces never display greeting candidates.

## Manifest approval

The local manifest must be a bounded regular file owned by root or the desktop
UID, without group/world write permission. Both models remain SHA-256 verified
before inference. A named calibration binds its complete parameter hash, schema,
consent version, and both model revisions/checksums. Its approval object has exactly
`status`, `binding_sha256`, and `expires_at` (Unix seconds). Status must be
`approved`, the hash must match the canonical binding, and approval must be current.
Changing calibration values under the same ID invalidates approval and templates.
The release evaluation stage must supply real evidence before publishing approval;
the application never auto-approves a manifest or substitutes default thresholds.

## Reply acceptance

The private service and native client validate the version-1 envelope's exact
fields/types, bounded size, parent/child Unix PID/UID, request and consumer IDs,
generation, sequence, monotonic capture/processing times, state and fixed reason
codes. Duplicate JSON keys (including escaped spellings) and excessive nesting
are rejected. The native parser has display-independent C++ tests.

The client records explicit requests before transmission. Background replies need
a correlated start event and an eligible desktop. Completed/cancelled request IDs
are retired, duplicate/out-of-order results are refused, and a cancelled or
ineligible request cannot surface a candidate. Requests and replay history are
bounded. Frames, crops, landmarks, embedding vectors, raw scores and candidate
lists are not permitted in recognition results. Preview and photo payloads have
separate mode-specific bounds; portrait metadata accepts only tiny embedded PNGs,
never filesystem or network URLs.

Candidate metadata contains one UUID, name, stored portrait, the fixed category
`candidate`, and absolute monotonic `expires_at = captured_at + 5`. Capture must
be at most three seconds old when processed, and delayed delivery cannot restart
the five-second lifetime. Normal worker completion preserves the capture generation;
a new capture, invalidation or failure clears the previous suggestion. Secure
input, opt-out, inactivity, device loss, account/template changes and model or
calibration changes invalidate candidates. The service watches relevant local
files and the worker rechecks template/configuration bindings after matching.

Matching requires the same single winner across three samples using the calibrated
absolute threshold and runner-up margin. Unknown, conflicting, multiple-face,
ambiguous, deleted and stale results expose no candidate list. Model or data
changes during inference discard the result.

## PIN confirmation

Selecting a suggestion only opens the normal masked PIN prompt with an exact
account UUID. It does not populate or submit credentials. The greeting backend's
UUID activation path never falls back to a display-name match, including after
account deletion/recreation. The ordinary account picker has no PIN field and
does not suppress candidates; entering the actual PIN dialog does. Keyboard-only
account creation and selection remain available without camera, service or model.

The current tests establish protocol and state separation using synthetic data.
Consented real-user accuracy, printed-photo/replay behavior and calibrated full-VM
suggestions remain release-evaluation gates. No liveness or spoof-resistance claim
follows from this channel.

## Validation record (2026-09-16)

PR #136 passes the display-independent native protocol executable (fresh/replayed,
unsolicited, stale/future, wrong consumer, invalid sequence/generation/types,
duplicate/escaped keys, nesting bounds, remote portraits, private data and forged
authority fields). Twenty-six service/worker tests and seventeen recognition
tests cover completion generations, file-change invalidation, data changes during
inference, unknown/ambiguous/conflicting samples, approval expiry/mismatch and UUID
name-reuse refusal. The full native/profile/display suite passes, including 97 QML
checks and three private display/PIN-routing checks. A cooldown rejection preserves
an already-fresh candidate without restarting its expiry.

These are synthetic protocol/lifecycle checks. The final batched image and actual
consented multi-person/presentation-attack evaluations belong to stage 5; no such
biometric result is inferred from the passing tests.
