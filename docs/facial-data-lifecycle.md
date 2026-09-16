# Facial data lifecycle

Face templates only support optional local greeting suggestions. They never
authenticate, unlock a workspace, refresh protected presence, or assert liveness.

## Namespace and key ownership

The `local-greeting` adapter uses exact account UUIDs. Names and portraits are
never identity mappings or embedding sources. Its version-2 store lives under
`chat-profiles/biometrics/local-greeting` in private owner-only directories.
Each encrypted generation has a new random AES-GCM key and an authenticated
encrypted template database. The ordinary desktop account can read this key.
This protects against other unprivileged UIDs and accidental plaintext exposure;
it does not protect against code already running as the desktop account, root,
or someone who copies both the key and ciphertext.

Protected identity enrollment is unavailable. The namespace adapter and capture
service refuse broker sessions rather than falling back to greeting storage.
Supporting that namespace later requires broker-owned encrypted storage and a
trusted input/verification path. Matching display names do not bridge namespaces.

Prototype schema-1 records are never silently migrated or accepted. Reenrollment
is required. Explicit facial-data purge also removes those legacy records/keys.

## Enrollment

Enrollment requires explicit consent and the exact target account's PIN. The
native flow verifies before capture and again under the account lock immediately
before the template commit. Account deletion holds the same lock. A storage
generation token prevents a capture started before purge, rotation or replacement
from writing afterward. Cancellation kills the acquisition worker.

The service allows at most 30 seconds. The worker guides three samples: centered,
a modest landmark offset to one side, and an offset to the other side. It rejects
poor lighting/sharpness, undersized/unclear faces, multiple faces, excessive pose,
inconsistent embeddings, missing calibrated pose bounds, and timeout. Landmark
variation is a sampling aid, not a liveness check. Raw frames/crops stay in memory.
Shell progress contains only fixed instructions and sample counts.

Each record binds namespace, account UUID, schema, consent version, detector and
embedding revisions/checksums, calibration profile ID, creation/update times and
three finite 128-dimensional samples. Model/consent/schema incompatibility removes
records durably when checked. Corrupt ciphertext or unsafe key permissions fail
closed; global purge works even when the current key or pointer is corrupt.

PINs are sensitive native input sent through the private process channel; they
are never command-line arguments, response fields, progress events, templates or
logs. That channel is unavailable to chat/model tools. No secret is returned as a
reusable authorization capability.

## Commit, deletion, rotation and recovery

Writes build a fresh encrypted generation, fsync its contents, then atomically
publish and fsync a small current-generation pointer. This pointer is the commit
point. A crash before it retains the previous committed record; a crash after it
loads only the new generation. Opening the store retires abandoned generations.
Replacement and key rotation use this same transaction. A purge commits an empty
generation marker before removing old keys and ciphertext. Account deletion
revokes facial data before committing account removal, so interruption may leave
an account without its face template, never a deleted account with a usable one.

Disabling recognition pauses capture and preserves enrollment. The separate
purge action withdraws consent for all local templates without deleting accounts,
portraits, PIN verifiers or chats. Cached suggestions and active acquisition are
cleared before the native purge/deletion flow completes.

AIOS does not create biometric backups or synchronize templates. Filesystem,
hypervisor and operator backups can retain keys/ciphertext and can restore deleted
data; they require a separate retention policy. Removal and key retirement are
logical deletion within the current store, not guaranteed erasure from SSDs,
snapshots, copied files or old backups. Key rotation cannot revoke external copies.

Real enrollment accuracy and pose thresholds remain unvalidated until the later
calibration/evaluation gate passes. Synthetic lifecycle tests do not supply that
evidence. Recognition remains experimental and disabled by default.

## Validation record (2026-09-16)

PR #135 passes eight encrypted-store tests, including actual child-process exit
immediately before/after the durable pointer commit, key rotation, key permission
failure, ciphertext tamper, legacy purge and restart recovery. Fourteen recognition
tests cover consent, exact UUID/PIN, lockout, PIN changes during capture, concurrent
purge/deletion, interrupted account removal, invalid/inconsistent samples, metadata
output, and disable-without-purge. Twenty-three service/worker tests include guided
pose progression, quality/multiple-face/pose timeout, private bounded progress,
freshness and external opt-out readback.

The full display suite passes: 96 QML checks, both installed-layout profile runs,
application-host protocol and three private display/PIN-routing checks. One first
run exposed a test race: socket publication preceded compositor readiness. The
test now waits for the same `display_ready` acknowledgement as the real launch
path, retaining its timeout and security assertions, and the rerun passed.

A broad Python run completed 657 tests with 13 skips and the two previously
confirmed `main` failures (`test_skills` application-builder allowlist expectation
and `test_terminal_theme` launcher occurrence count). No new failures appeared.
All lifecycle evidence uses synthetic data in disposable directories. Real
consented enrollment and calibrated biometric performance remain unverified;
these results must not be described as a production recognition release.
