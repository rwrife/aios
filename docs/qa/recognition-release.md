# Recognition release evidence

Status: **experimental, disabled by default; accuracy gates unmeasured**.
Hardware capture and protocol tests do not establish recognition accuracy.

## Reproducible artifacts and packaging

`apps/aios/face-models.lock.json` pins FP32 YuNet 2023mar and SFace 2021dec from
OpenCV Zoo commit `47534e27c9851bb1128ccc0102f1145e27f23f98`. Source URLs, byte
counts, SHA-256, API contracts and attribution are in that lock. YuNet's MIT and
SFace's Apache-2.0 licenses accompany the weights. These choices target the
[OpenCV 4.x APIs](https://docs.opencv.org/4.12.0/d0/dd4/tutorial_dnn_face.html).
The [YuNet upstream README](https://github.com/opencv/opencv_zoo/blob/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet/README.md)
distinguishes the 2023 OpenCV 4.x export from the 2026 OpenCV 5.x export.

`AIOS_IDENTITY_BUILD=1` stages checksum-verified weights and licenses under
`/usr/local/share/aios/face-models`, with ordinary read permissions. Default
builds omit them and remove stale optional artifacts from a reused staging
directory. OpenCV is in the default image for photo decoding; cryptography is
in the optional image for encrypted templates. Neither build creates
`/etc/aios/face-models.json`, calibrated thresholds, enrollment or approval.
Runtime never downloads models. A reviewed administrator manifest must bind
these exact hashes to a versioned Brio calibration and expiring approval.

Run `PYTHONPATH=apps python3 scripts/check-face-runtime.py --directory DIR`
locally, or against the installed directory in Alpine. It verifies model hashes,
blank-image rejection and finite 128-value output from a synthetic crop, reporting
load/cold/warm times and peak process RSS only. It cannot measure real accuracy,
alignment on real faces, end-to-end latency, or liveness.

## Preregistered evaluation procedure (not yet executed)

Before collecting held-out results, freeze model/calibration/image hashes and an
acceptance plan. The reviewer must approve its limits, minimum counts, conditions
and expiry. Keep the signed review reference and canonical plan SHA-256 with the
aggregate report. Any changed threshold or quality filter requires a new held-out
run. Do not copy literature thresholds into a production approval.

Suggested starting review targets, **not approved or measured**: false match
rate <=0.001, false reject <=0.05, ambiguity <=0.05, quality rejection <=0.10
within the declared lighting/pose/distance envelope; cold/warm suggestion p95
<=5/3 seconds, open/reopen p95 <=2 seconds, recovery p95 <=15 seconds after
device availability; idle/active average CPU <=10% of one core, peak service+
worker RSS <=256 MiB, growth <=1 MiB/hour, camera duty <=0.15, and a 24-hour
idle/active soak. These targets may prove unattainable on the reference CPU.
Report failures rather than moving limits after observing held-out results.

Use separate calibration and validation participant cohorts. Within each cohort,
enrollment and probe samples must be disjoint (validation subjects need their
own separate enrollment samples). Include unknown people absent from templates.
Document counts and uncertainty; report exact binomial confidence intervals and
participant/session clustering, since repeated frames are not independent trials.
Approve sample sizes based on those intervals, not just a zero observed error.

Obtain explicit informed consent before capture; document local encrypted storage,
who can access it, withdrawal and deletion dates. Keep participant mappings and
media outside the repository. Do not send them to an agent, telemetry or GitHub.
Prefer in-memory evaluation; delete temporary recordings, crops and embeddings
at the declared deadline. Publish only reviewed aggregate counts and timings.
No evaluation dataset or consent was supplied in this implementation session.

Stratify quality/error counts by lighting (lux), pose, distance and session.
Record reference CPU/RAM, firmware without serial, image/hash, QEMU, WSL, kernel,
OpenCV, negotiated MJPG 640x480 15 fps, cohort sizes and denominators. Distinguish
wrong suggestions among impostor attempts, genuine attempts without a correct
suggestion, ambiguous attempts and quality rejection. Measure camera opening,
warmup, inference, matching, IPC and UI separately; cold and warm end-to-end
latencies include failures/timeouts in the report rather than silently dropping
them. Sample service plus worker CPU/RSS and actual camera-open durations.

Run printed-photo, screen/video replay, consented lookalike, multiple-person,
covered/partial-face, darkness and blur cases. Record a fooled suggestion as such;
`Evidence.live=false`, mandatory PIN, workspace ownership and capabilities must
remain unchanged even if every spoof obtains a suggestion. Include busy device,
physical unplug/replug, crash, late/replayed IPC, rapid account/chat switching,
preview/photo preemption, enrollment/deletion/purge, secure input, lock/suspend,
shutdown and restart. A QEMU USB host-address change currently needs a VM restart.

## Aggregate review checker

`PYTHONPATH=apps python3 -m aios.recognition_release PLAN.json REPORT.json`
returns exit 2 for missing, mismatched, unapproved or failed evidence. Passing
means **review-ready**, never runtime approval or automatic enablement. This is
a consistency checker of reviewer attestations, not proof that an experiment
occurred. It does not replace a consented corpus runner or statistical review.

Plan fields: `status=approved`, `review_reference`, Unix `frozen_at`,
`model_lock_sha256`, `calibration_sha256`, `image_sha256`, `maximums` for every
metric in `recognition_release.METRICS`, and positive `minimums` for participants,
genuine_attempts, impostor_attempts, soak_hours. Report fields: canonical
`plan_sha256` (sorted compact JSON SHA-256), the same artifact hashes,
`evaluation_started_at`, `metrics`, `counts`, and every named `GATES` entry as
`{"status":"pass","evidence":"reviewed aggregate artifact reference"}`.
Boolean/nonfinite/negative metrics, missing gates and undersized runs cannot pass.

## Rollback and current limits

Disable Camera recognition in Settings: the persisted setting cancels scheduled
acquisition and clears suggestions. Accounts, PINs, portraits, chats and templates
remain. Purge is a separate explicit operation; account deletion revokes its
template. Missing/expired approval, unavailable camera or invalid artifacts fail
to manual PIN access. Never approve a manifest to make a test look successful.

Stages 1–4 evidence is in `brio-camera.md`, `facial-data-lifecycle.md` and
`recognition-suggestions.md`. Stage 5 still requires calibrated real enrollment,
held-out accuracy/adversarial measurements, a full long soak and human UX review.
Unit, synthetic-runtime and capture checks are labeled separately below.

### Automated and container checks, 2026-09-16

- Python: 672 tests, 13 environmental skips, no failures. Two stale baseline
  expectations were updated to the current application-builder contract and
  two terminal launch paths; no production behavior was changed for those tests.
- Native strict camera protocol, both installed-layout profile/PIN checks,
  application-host protocol, 97 QML checks (Ocean/Sage) and all three private
  display/input/teardown tests pass.
- Model downloads match locked sizes and SHA-256. Default-build cleanup,
  corrupted cache refusal and incomplete/changed/failed aggregate evidence
  are covered by six release/artifact tests.
- Alpine 3.23 container, OpenCV 4.12.0, actual pinned weights: synthetic runtime
  check passed. Model loading 2.8204 s; cold detect+feature 0.3118 s; three warm
  runs 0.1394/0.1220/0.1186 s; peak process RSS 244708 KiB. Concurrent compilation
  was active, so this is compatibility evidence, not a controlled benchmark.
- Ubuntu host OpenCV 4.6.0 failed YuNet inference (`getLayerData`); it is not
  the validated runtime. The target remains Alpine's recorded 4.12 package.
