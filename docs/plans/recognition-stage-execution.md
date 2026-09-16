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
Physical reconnect and the remaining validation checks are in progress. Stage
1 remains open until those results are recorded; stages 2–5 have not started.

Later stages must use measured stage 1 formats/device behavior. Do not mark an
issue complete or claim production readiness while its hardware or cohort gates
remain unverified. Retain only aggregate camera evidence, never media or serials.
