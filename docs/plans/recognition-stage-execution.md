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

Later stages must use measured stage 1 formats/device behavior. Do not mark an
issue complete or claim production readiness while its hardware or cohort gates
remain unverified. Retain only aggregate camera evidence, never media or serials.
