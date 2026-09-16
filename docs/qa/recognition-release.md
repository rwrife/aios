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

The recovery guest completed three previews/photos/preemptions, inactive
rejection and worker termination, but initially reproduced the sampler failure.
A camera-independent reproduction showed that Linux removes a process's address
space before it always reports zombie/exited state; `/proc/PID/fd` becomes
inaccessible during that interval. The sampler now explicitly verifies the
missing address space and records `exiting_process_samples`, while permission
failures for live address spaces still fail the run. Two regression tests and
a 300-process exit stress check passed (zero sampling errors). First/last idle
RSS now exclude interpreter startup before the first observed camera open.
These are still sampled estimates, not proof of every instantaneous descriptor
state. The updated sampler is an external test tool; the image is unchanged.

The local consent terminal is open. A bounded supervisor waits up to two hours
for the participant session to exit with five aggregate probe results, then
runs a new three-cycle guest smoke check. Only if that passes will it start the
24-hour acquisition soak with 15-second idle intervals. At this checkpoint its
state is **waiting for participant**; no completed session or soak is claimed.

### First participant session and framing preview

The participant completed the first consented development session: five genuine
attempts, four correct candidates and one unavailable attempt. Attempt times
were 1.3713, 1.1828, 1.2689, 1.3855 and 1.3438 seconds. This is one development
participant with provisional thresholds, not held-out accuracy evidence.

The subsequent ordinary-user guest smoke test passed three preview/photo
preemptions, inactive rejection and worker termination in 8.972 seconds. Maximum
frame age was 0.114 seconds; first-frame/photo-handoff p95 was 1.345/1.251 seconds.
Sampling reported zero errors, one peak camera owner, 153580 KiB peak combined
RSS and 11 peak descriptors. Idle service RSS was 20924 KiB before/after. This
continuous short capture test's duty/CPU measurements are not idle/active soak
results. One driver-error frame was discarded. The soak started, then was
deliberately stopped for the requested framing-preview improvement after at
least nine cycles (last progress: 143.687 seconds). It is not a completed soak.

The development tool now displays a mirrored local preview in the same worker
that owns acquisition and temporary vectors. A framing outline and instructions
use the shared theme palette and DejaVu Sans. Space begins measurement after
positioning; Escape cancels. Preview positioning is bounded to 45 seconds per
step and does not create another camera owner. No preview images enter parent
IPC or files. New aggregate reports explicitly mark that timings include human
framing time and must not be compared with inference/automatic latency gates.
Six consent/protocol/preview tests pass; camera-off synthetic layouts were
visually checked in Ocean and Sage on the guest. The consent prompt has been
reopened for this version; a fresh soak is gated on its completion and another
passing guest smoke test. Previous aggregate results remain preserved.

### Participant-controlled pose wizard

Participant feedback identified that the original guided enrollment still
advanced between straight/left/right poses automatically after the initial
confirmation. The development harness now uses native Qt instructions and
Next/Cancel buttons alongside the local preview. Every enrollment pose and
every repeat check waits for a fresh Next action; the button is disabled during
measurement. A failed enrollment pose stays on that step and requires Next to
retry. Escape/window close cancels. Each enrollment step has a two-minute
positioning/retry budget; it never advances merely because that time elapsed.

This manual QA tool requires Alpine `py3-pyside6`, installed only in the disposable
validation guest; it is not added to the production image. Raw frames and
temporary vectors remain in the same worker. Eight focused tests cover consent,
editing, strict aggregate replies, separate pose confirmations and retry gating.
The previous preview-session attempt did not complete; its terminal reported
incomplete enrollment, and no completed aggregate or replacement soak was
produced. The original four-of-five development result remains the only completed
participant session so far.

Native Qt synthetic tests in the guest passed three distinct Next-button
confirmations and Escape cancellation in both Ocean and Sage; screenshots of
the camera-off layouts were inspected. The revised wizard is reopened for
participant validation. Synthetic UI actions are not participant consent or
biometric evidence.

### Visible failures instead of disappearing preview

The participant reported the native wizard closing before Next. That run had no
completed aggregate; the previous generic exception handling did not preserve
the exact cause. A fresh 30-second acquisition-only check read 221 frames without
a capture exception; a real Qt preview check displayed 139 frames over 20 seconds
without failing. The latter emitted two decoder warnings, so these checks do not
establish the original cause or prove camera reliability.

Capture exceptions and positioning/pose timeouts now leave a visible paused
window after the camera context exits. The stale image is cleared; a fixed,
allowlisted reason appears with Retry/Cancel. Retrying restarts incomplete
enrollment or repeats the current probe; errors cannot silently skip a probe.
The two-minute camera/positioning budget is unchanged, but waiting for Retry is
UI-only. The parent bounds each interactive command to two hours. Fixed pause
codes are retained in a private `.events.jsonl` companion to the aggregate, and
successful reports include enrollment/probe retry counts instead of hiding
failed attempts. No images, vectors or arbitrary exception text enter this log.

Eleven focused tests pass. Synthetic fault injection in the guest, in Ocean and
Sage, verified that a read failure before Next leaves the window visible and
clears its image, performs no additional reads while paused, and resumes only
after an explicit Retry click. The revised consent session is open; completed
biometric validation and the replacement soak remain pending.

### Fresh-stream recovery after a confirmed interruption

The participant's next run recorded `stale_frame` in its fixed-code event log.
This identifies a freshness/drain failure, not a rejected identity or pose; it
does not distinguish every possible timestamp, sequence or damaged-frame cause.
Metadata-only reproduction tested ten reads each with 0, 0.5, 1 and 2 second
consumer pauses. All completed; the 1/2-second cases each discarded ten old
frames and recovered fresh frames. Synthetic model inference was
0.0833/0.0690/0.0743/0.0796 seconds with four OpenCV threads. Ten real-preview
Next transitions plus synthetic inference also completed without rejection.
The participant's intermittent failure has therefore not been reproduced
deterministically or attributed to inference latency.

The manual preview adapter now permits one stream restart per capture operation
after `stale_frame`: close the old handle first, open and warm up a new stream,
then apply the same age, timestamp and sequence checks. It never retimestamps
old evidence, weakens the 0.5-second age limit, or advances the wizard. A second
failure still pauses for explicit Retry/Cancel. Interruptions, including an
automatic restart, remain counted in the event log and aggregate retry counts.
Thirteen focused tests pass, including close-before-open ordering and bounded
recovery when freshness cannot be restored.

A real-Brio technical test injected one stale-frame exception after Next, then
completed five transitions with exactly one stream restart. The wizard heading
stayed on the same step and maximum observed returned-frame age was 0.115 seconds.
This used no biometric enrollment and is recovery evidence only. The updated
participant session is reopened; its completion and the full soak are pending.

### Persistent participant failure: diagnostic run required

The participant reported another failure after several Next clicks. That run
recorded two `stale_frame` interruptions and produced no completed aggregate.
The one-restart mitigation therefore has not resolved the participant failure.
Do not treat the injected-error recovery check as evidence that it has.

Two 15-second real-camera detector checks (without/with Qt preview) completed
114/115 reads without rejection, but detected zero single-face frames. Separate
15-second SFace load checks using an all-zero synthetic crop completed 114 reads
each without rejection. These exercise model load, not participant recognition,
and have not reproduced the reported failure.

The manual harness now retains fixed numeric counters for old/future timestamps,
timestamp/sequence ordering, damaged/empty frames, other native read errors, and
maximum read/inference times. Metrics are emitted before capture errors and after
successful operations, and validated by the parent before being written to the
private event log. This changes diagnostics only; camera freshness limits and
manual pose confirmation remain unchanged. Sixteen focused tests pass, including
rejection classification, inference timing on failure, valid protocol delivery,
and rejection of extra fields or invalid numeric values. No frames, embeddings,
device identifiers, or arbitrary exception messages enter the diagnostic log.
Root cause and participant completion remain pending a diagnostic attempt.

The next participant attempt recorded 20 inference calls (maximum 0.2171 s),
11 driver-error frames across the original stream and its restart, and zero
old/future timestamp or ordering rejections. Maximum observed frame age was
0.1916 s. Thus its eventual `stale_frame` exhaustion was associated with damaged
driver frames, not stale timestamps; the underlying USB/driver fault is still
unresolved. No completed participant aggregate was produced.

The participant also reported a misleading lighting/pose retry message. The
manual wizard now distinguishes measured darkness, excess brightness, blur,
missing/multiple faces, and head angle/direction. Each failed attempt emits fixed
reason counts and keeps the same pose behind an explicit Next click. Existing
quality and pose thresholds are preserved. The previous log cannot establish
which pose check failed. Eighteen focused tests pass, including distinct pose and
quality feedback. Driver-only failures receive a camera-driver message instead
of generic freshness wording. Participant validation remains pending.

### Ten accepted frames per enrollment pose

At the participant's request, the manual wizard now collects ten usable frames
after each pose's Next click, rather than accepting one frame or asking for a
retry after three seconds. A visible count advances only for frames passing the
existing image quality, single-face, pose, finite-vector, and consistency checks.
Rejected frames are discarded and capture continues on the same pose. Each pose
has a separate two-minute capture budget, Cancel remains active, and the next
pose still requires an explicit click. A partial batch cannot produce a reference.

The ten accepted embeddings are individually normalized and averaged, then the
mean is normalized into one reference per pose. The temporary gallery therefore
still contains three references, now derived from thirty accepted frames; no raw
images are retained. Reports identify the ten-frame count and aggregation method.
This is a development harness change, not an approved production calibration.
Twenty-one focused tests pass, including rejection without extra clicks, exact batch
size, incomplete-batch failure/cancellation, and equal weighting in the reference. The known
driver-error interruption remains unresolved and is not hidden by this change.
Camera-off native Qt checks in Ocean and Sage each accepted thirty synthetic
samples, rejected one unusable sample, required three Next clicks, and passed
Escape cancellation. Both progress layouts were inspected in screenshots.

### Participant-controlled stopping

The participant requested no automatic stop before cancellation. The manual
wizard now has no positioning, enrollment collection, or parent interactive
command deadline, and no retry-count cutoff. Individual camera reads retain
their existing bounded waits and strict freshness checks. Failed streams close
before reopening, with a one-second event-pumping delay between attempts; the
current pose and its accepted frames survive read recovery. Other recoverable
operation failures retry automatically rather than requiring a Retry click.
Cancel, Escape, and window close remain cancellation actions. Completed batches
still wait for Next to begin a different pose, and a completed session finishes
normally. No invalid frame is accepted merely to keep the wizard moving.

Twenty-three focused tests pass, including unlimited positioning, results beyond
the former parent deadline, repeated stream recovery, and cancellation during
reconnection and partial collection. Camera-off native Qt fault injection in
Ocean and Sage each performed three automatic retries, then stopped on the
actual Cancel button. This does not resolve the underlying driver fault or
establish participant accuracy.

### Forward-only development reference

The participant requested a simpler ten-frame, straight-ahead reference. The
manual wizard now requires one enrollment Next click and collects ten usable
single-face frames. It no longer gates collection on the landmark pose offset
or pairwise enrollment similarity, and no left/right views are requested.
Image quality, model face detection/size, finite embedding validation, and camera
freshness checks remain. The ten unit embeddings form one normalized mean
reference; raw photos are not saved. Five subsequent development checks also
request a forward-facing view. Matching threshold and margin remain unchanged.

Reports identify `forward_only`, ten enrollment frames, and the averaging method.
This changes the development protocol and must not be mixed with previous
three-pose results or treated as production approval. Twenty-three focused tests
pass, including one-click collection of exactly ten accepted frames, rejected
frames excluded, and matching against a one-reference gallery. Continuous retry
and participant cancellation remain in effect.

### Continuous camera ownership and two-second sampling

The participant reported the camera light cycling and requested a continuously
open stream with a frame selected every couple of seconds. The manual worker now
owns one preview and camera context across enrollment and all five checks. Read
errors discard frames and retry on the same handle; they no longer close/reopen
the camera. Preview frames are drained continuously, but face processing selects
at most one fresh frame every two seconds. Probe collection uses the same pacing
without the previous two-second three-frame deadline. Cancellation or completed
session cleanup releases the camera. An opened stream that cannot recover stays
visible and cancellable rather than being automatically power-cycled.

Twenty-four focused tests pass, including exactly one camera enter/exit across
enrollment and five checks, no reopen after repeated read failures, cancellation
while waiting for usable frames, and discarding intermediate preview frames
between two-second sample selections. This changes the manual development
harness, not the production capture adapter or approval state.

A real-Brio preview check selected ten frames on the same handle, 2.004–2.152
seconds apart, while draining 143 frames. It recorded zero frame rejections,
maximum observed frame age 0.1005 seconds, and confirmed closure only at the end.
This was a stream/pacing test without face inference or biometric enrollment.

### Fixed twenty-second enrollment burst

The participant replaced quality-gated collection with a fixed budget: preview,
one Next click, ten snapshot slots at 2, 4, ..., 20 seconds. The preview drains
the same stream continuously. Each slot can select only a fresh frame captured
within its preceding half second; a missing/damaged slot remains missing and is
never replaced. The capture window does not expand for quality or inference.
A bounded native read may finish shortly after the window, but frames captured
after its end are ineligible. Inference runs after the burst using only the
selected snapshots, held temporarily in worker memory and then discarded.

The usable subset forms the temporary reference. Zero usable images produces an
incomplete enrollment, without automatically taking another burst. Reports
distinguish scheduled shots, captured photos, usable photos, twenty-second
duration and zero replacements. This remains a development reference, not model
training or desktop enrollment. Twenty-five focused tests pass, including exact
slot selection, missing slots without replacement, unusable shots excluded from
the fixed batch, no reference from an empty batch, and participant cancellation.

The real-Brio burst check captured all ten scheduled snapshots in 20.064 seconds
on the same camera handle and verified cleanup at the end. No face inference or
participant reference was created by this technical check.

### Account-bound single-capture enrollment

Enrollment now offers one forward-facing capture: ten fixed snapshot slots over
20 seconds, with a green progress line around the preview and no pose or
follow-up recognition steps. The development participant harness reports zero
recognition attempts for this enrollment-only flow and discards its reference.
The updated harness was uploaded and reopened in the running Brio guest.

Native setup is optional after account creation and available later in Accounts.
Both entry points require fresh native PIN entry and explicit consent for the
selected account. Cancellation clears secrets and releases preview/capture.
Native enrollment processes only the fixed batch, creating three references from
disjoint groups of usable frames. Its manifest binding now includes
`forward-burst-10-v1`; previous protocol approvals cannot silently carry over.
The production manifest, held-out cohort and 24-hour validation remain pending.

Validation: 81 focused Python tests passed after the final protocol changes;
107 QML tests passed, plus the final 20 identity tests after UI refinements.
The native shell build and camera protocol test passed. Ocean and generated Sage
screenshots used synthetic camera fixtures, including the optional offer and
green capture progress. No participant images were retained in screenshots.
