# Desktop camera service

`CameraClient` launches one unprivileged `aios.capture_service` per desktop UID.
Settings and Setup preview, profile photos, enrollment and background recognition
all request this service. Qt enumerates cameras but never opens a capture stream.
The diagnostic CLI remains a separate, explicitly invoked hardware test.

The service selects only enumerated stable V4L2 index-0 devices. Requests carry an
opaque device identifier, never a path or command. A disposable acquisition worker
loads `/usr/local/lib/libaios-camera.so`, negotiates MJPG 640x480 at 15 fps, and
decodes frames in memory with OpenCV. `AIOS_CAPTURE_LIBRARY` is a trusted local
development override, not an IPC field or model tool.

## Ownership and timing

One authoritative `CaptureSchedule` implements a 15-second background cadence,
two-second immediate cooldown, and 2/5/15/60-second failure backoff. Background
capture requires opt-in, an active desktop and no secure input. Explicit photo
or enrollment preempts preview/background work. Explicit consumers retain a
lease until they release it; completion alone does not resume background capture.
Preview never resumes implicitly and ends after 30 seconds.

The worker discards three warmup frames, then rejects kernel timestamps from
before warmup, more than 500 ms old, in the future, or out of sequence. Recognition
must collect three quality-approved single-face samples within two seconds.
V4L2 buffers are limited to four at 1 MiB each, JPEG decode to 640x480, and IPC
retains only bounded events. No frames or embeddings are saved to disk.

Account enrollment captures ten fixed snapshot slots over twenty seconds after
Next, without replacement photos. It emits a live preview plus bounded progress
(`samples` 0..10, `target` 10, `reason` `burst_capture`), then builds three stored
references from disjoint groups of the usable shots. At least three usable shots
are required; an inadequate burst fails rather than extending capture. No head
turns or follow-up recognition checks are part of enrollment.

The service enforces deadlines (photo/recognition five seconds, enrollment 30,
preview 35). Cancellation kills the worker; an unreaped process is quarantined
and prevents another acquisition. Linux parent-death signaling also kills the
worker if the service is killed. Device inventory changes invalidate generations.

## Private protocol

The socket is in a mode-0700 directory. Both endpoints verify exact parent/child
PID and UID using `SO_PEERCRED`; unrelated same-UID peers are refused. This is a
native desktop interface, not an authentication boundary against arbitrary code
already executing inside the trusted shell. Protected broker sessions are
unsupported and fail closed. Root service execution is refused.

Version 1 uses newline-delimited JSON. Requests have exact fields, UUID request
and consumer IDs, and a 4096-byte limit. Fixed operations are configure, capture,
release, refresh and shutdown. Events are limited to 262144 bytes and include
request/consumer IDs, generation, sequence, monotonic capture/processing times,
reason and payload. Only explicitly requested preview/photo events, including consented enrollment
previews, contain bounded image data. Recognition events contain bounded candidate metadata, never embeddings.
Slow readers disconnect after a bounded write. The shell limits its output queue
and retries a failed service at most three times.

Suggestions never authenticate, unlock, prove liveness, or grant capabilities.
PIN access remains available when the service or camera fails. Enrollment and
template lifecycle hardening is tracked separately in recognition stage 3;
candidate protocol hardening is stage 4. Production calibration remains stage 5.
The [suggestion channel](recognition-suggestions.md) specifies strict native
validation, absolute expiry, manifest approval and UUID-only PIN preselection.

## Validation

`tests/test_capture_service.py` covers peer credentials, exact request shapes,
cadence/backoff, preemption, explicit release, cancellation, device changes,
quarantined workers and bounded invalid results. `test_capture_worker.py` checks
driver timestamp freshness, warmup rejection, sequence handling and sample
deadlines. The display suite covers migrated preview lifecycle in Ocean and Sage,
installed-layout profile behavior, and private PIN input routing.

Initial Brio WSL adapter check: three post-warmup frames in 1.703 seconds total;
last frame age 0.067 seconds. This does not replace the Alpine guest validation.
