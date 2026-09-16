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

### Final source and installed image

After the specific capturing/paused status-text change, the native/display suite
passed again: **104 QML checks**, both profile/PIN runs, strict camera protocol,
application host and three private-display tests. Production source is
`1d5cab5`; later changes are evidence documentation only.

Final local identity image: `alpine-aios-recognition-stage5-final-x86_64.iso`,
2,182,791,168 bytes, 786 resolved packages, SHA-256
`32fc6b0b126f555d71b25adf92ff4702a918da5448590c5d042ae9f3bb20e9bc`.
Its build manifest and package list are beside the ISO in `distro/alpine/out`.
Automatically triggered GitHub ISO jobs were canceled; validation used local
WSL/Docker builds and ordinary windowed QEMU.

Final guest, `aios` UID 1000: pinned-model synthetic load 0.2533 s, cold
detect+feature 0.1120 s, warm 0.0639/0.0548/0.0523 s, peak process RSS
238852 KiB. Three preview/photo preemptions, inactive rejection and service-kill
worker cleanup passed in 10.602 s; maximum frame age 0.106 s. No calibration
approval manifest was installed. The same camera backend's earlier extended
run and unresolved initial handoff failure are reported in `brio-camera.md`.

**Open release blockers:** consented calibration and held-out cohort; real
enrollment-to-suggestion accuracy and confidence intervals; biometric adverse
conditions/spoof trials; full CPU/RSS/duty/latency decomposition; long-duration
soak and the unresolved initial photo handoff failure; complete human
accessibility/fallback/rapid-switching acceptance. Stage 5 stays draft and #95
stays open. No metric, consent, model approval or successful biometric enrollment
is inferred from the passing synthetic and acquisition tests.

## Resumed Stage 5: handoff reliability and resource sampling

A repeated physical Brio run reproduced `busy` after two preview completions.
The service was still reaping the previous disposable worker after its 100 ms
immediate cleanup budget. It previously rejected the next request even though
the old job had completed/cancelled. One pending handoff now waits at most one
second for actual worker exit, within the original request's capture deadline.
It never starts a second owner. Release, secure-input/inactive transitions,
device/model changes and shutdown cancel the pending request. Purge supersedes
it. A worker that does not exit still produces a bounded busy failure.

`tests/manual_camera_service.py` now imports `tests/camera_resources.py`. Copy
both files together when running the installed guest. For a metadata-only
idle/active acquisition soak, as the ordinary guest account:

```sh
PYTHONPATH=/usr/local/share/aios python3 /tmp/manual_camera_service.py \
  --duration-seconds 86400 --interval 15
```

This repeatedly previews, preempts with a photo, discards the encodings, releases
the lease and idles between cycles. It finally checks inactive rejection and
service/worker termination. It stops on any failed capture instead of hiding
errors in successful averages. Progress reports contain counts/times only.
The sampler reads service/worker process metadata every 50 ms: CPU (including
reaped children), summed RSS, descriptor counts and camera-open ownership.
Duty fraction and peaks are sampled estimates; brief events between samples
can be missed. Preview first-frame and photo-handoff latency include startup,
camera warmup and IPC, not recognition inference or UI paint. This acquisition
soak does not replace a calibrated recognition soak or the held-out evaluation.

The resumed handoff test then exposed a separate failure after 89 successful
photos. Bounded private diagnostics identified V4L2 driver-error frames. The
adapter still rejects these frames, but now requeues them and waits for a valid
frame within the existing deadline and a 16-read cap. Empty frames receive the
same treatment. Invalid metadata, failed dequeues/requeues, decode errors and
timeouts remain failures. Diagnostics contain only fixed error codes and
discard counts; the native UI protocol receives neither these private events
nor arbitrary exception text.

The requested target is full production validation. Only one willing adult is
currently available. `tests/manual_face_session.py` supports an explicitly
consented, single-person development session in a local ordinary-user terminal
on the Alpine guest. It uses the pinned models and provisional thresholds,
keeps temporary enrollment vectors in a disposable worker, and saves only
aggregate counts/times. Consent must be typed by the participant. This tool
does not install a manifest, create an account, persist templates or approve
calibration. Its five repeat probes cannot establish a false-match rate or
replace separate calibration and held-out participants.

Recovery source `22c2833`: 678 automated Python tests passed (13 environmental
skips), plus three consent/protocol tests for the development session. Native
validation passed strict camera protocol, both profile/PIN runs, 104 QML checks,
the application host test and three private-display tests. A host stress run
completed 100 preview/photo cycles and discarded 23 driver-error frames, but its
final resource check failed. Follow-up runs observed transient `/proc/PID/fd`
permission errors during worker transitions; ten further cycles completed with
one observed camera owner, but sampling remained incomplete even with bounded
retries. These host runs are not accepted resource-gate evidence.

The locally rebuilt recovery image contains 786 packages and is 2,182,791,168
bytes: `alpine-aios-recognition-stage5-recovery-x86_64.iso`, SHA-256
`7490a9682c2d86032a21b14d91b6fd8a17590b2862c7d6677fcf6f7f424fbe90`.
Guest validation and the consented development session are pending. No runtime
approval or production calibration has been created.
