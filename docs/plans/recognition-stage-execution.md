# Recognition stage execution

All five issues are assigned to `rwrife`. Work proceeds in dependency order,
with one branch and PR per stage. Each PR is opened after its first coherent
implementation, before expensive hardware validation.

| Order | Issue | Branch | PR base | Exit evidence |
| --- | --- | --- | --- | --- |
| 1 | #91 | `codex/recognition-vm-gate` | `main` | Three unprivileged Alpine Brio captures, reopen and failure matrix |
| 2 | #93 | `codex/recognition-capture-service` | `codex/recognition-vm-gate` | Single owner, bounded protocol and migrated consumers |
| 3 | #92 | `codex/recognition-template-lifecycle` | `codex/recognition-capture-service` | Consent/PIN binding, namespaces and crash-safe deletion |
| 4 | #94 | `codex/recognition-suggestion-hardening` | `codex/recognition-template-lifecycle` | Authenticated fresh metadata, expiry and no authorization effects |
| 5 | #95 | `codex/recognition-release-evaluation` | `codex/recognition-suggestion-hardening` | Pinned artifacts, held-out calibration, VM/UX/soak evidence |

Run headless unit/protocol tests at each stage. Batch local image builds and
hardware runs where dependencies permit. Never substitute fake-camera tests
for physical capture evidence or synthetic samples for held-out participants.
Recognition stays experimental and disabled until all release gates pass.

## Current evidence and dependencies

2026-09-15: USB/IP inventory lists the Brio 101 only as a persisted binding;
the physical device is disconnected. The connected Dell camera does not satisfy
the named Brio gate. Diagnostic implementation can proceed; stage 1 cannot be
closed until the Brio is connected and the real guest measurements succeed.
Physical unplug/replug and consented cohort evaluation require operator input.

Stage 1 implementation is in draft PR #133: bounded guest diagnostics, metadata
validation, temporary USB ACL restoration, side-effect-free dry runs, and the
optional-image editor dependency repair. The identity image builds locally and
passes camera-free headless runtime/desktop checks. See `docs/qa/brio-camera.md`
for the artifact hash and test results.

2026-09-16: the Brio was connected and WSLg repaired with operator approval.
Three unprivileged ten-frame captures passed in both headless and normal
windowed Alpine QEMU. Negotiation was MJPG, 640x480, 15 fps. Permission, wrong
node, missing device, contention/release and physical unplug checks passed.
Physical reconnect, automatic discovery, actual capture deadline cleanup and
scoped ACL restoration also passed. Stage 1 implementation and validation are
complete in PR #133, ready for review. The issue closes when that PR lands.
Stage 2 implementation is on `codex/recognition-capture-service`, based on the
validated stage 1 branch. PR #134 now passes deterministic, display, physical
Brio service lifecycle, Alpine guest preemption/termination and USB/IP removal
checks. Measured evidence is in `docs/qa/brio-camera.md`. Stage 3 follows it;
successful calibrated enrollment and held-out accuracy remain later release gates.

Stage 3 is implemented in PR #135 with durable encrypted generations, immediate
UUID/PIN revalidation, guided finite sampling and protected-namespace refusal.
Crash/restart, concurrent purge/deletion, quality guidance and display/PIN tests
pass; see `docs/facial-data-lifecycle.md`. Stage 4 suggestion-channel hardening is
next. The operator delegated model/setup selection; no held-out consented cohort
was supplied, so biometric accuracy gates remain unmeasured and cannot be closed
by synthetic tests.

Stage 4 is implemented in PR #136: strict native reply validation, absolute expiry,
approval-bound model/calibration loading, invalidation and UUID-only PIN selection.
Protocol, matching-state and display/PIN tests pass. Stage 5 will pin/package the
selected OpenCV-compatible model artifacts, add reproducible evaluation gates,
and perform the final batched image/runtime checks. Human cohort accuracy remains
an explicitly unmeasured gate.

Later stages must use measured stage 1 formats/device behavior. Do not mark an
issue complete or claim production readiness while its hardware or cohort gates
remain unverified. Retain only aggregate camera evidence, never media or serials.

Stage 5 is in draft PR #137. Pinned licensed artifacts, optional packaging,
synthetic runtime validation and an offline aggregate review checker are present.
The full Python suite passes (672 tests, 13 environmental skips), as do the native
protocol/profile tests, 104 final QML checks and three private-display/PIN tests.
Real cohort calibration/accuracy, adversarial biometric trials, long soak and
human UX acceptance remain open. No runtime approval or enrollment was created.
The final local image boots and passes installed-model compatibility and Brio
service smoke checks. One initial extended-run photo handoff failed; a 20-cycle
repeat passed, but reliability is not declared proven. Final hashes, timings and
the remaining blockers are in `docs/qa/recognition-release.md`.
