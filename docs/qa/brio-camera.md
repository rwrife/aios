# Brio 101 control and validation

Recorded 2026-09-09. The owner has dedicated this camera to AIOS/WSL;
Windows camera access is not required. This is operational setup, not proof of
biometric authentication or of camera capture inside the AIOS VM.

## Verified device and state

- Logitech Brio 101, USB VID:PID `046d:094d`, directly connected to USB.
- Windows USB/IP bus ID at validation: `2-2`. Re-enumerate after moving ports.
- usbipd-win 5.3.0; Ubuntu WSL kernel `6.18.33.2-microsoft-standard-WSL2`.
- Linux UVC driver `uvcvideo`, USB high speed (480 Mbit/s).
- Capture node at validation: `/dev/video0`; `/dev/video1` is a second node,
  not an interchangeable capture target. Verify capabilities before selecting.
- Linux USB bus/device at validation: 001/002. These are separate from the
  Windows bus ID, and change after reattachment.
- Dedicated forced binding is configured; the camera was left attached to WSL.
- Logi Tune's updater and agent initially held the video interface open.
  They were temporarily stopped, then restored after dedicated binding.
- Two successful bounded probes: ten frames each, 640x480, approximately
  1.927 and 1.919 seconds. The second succeeded with Logi Tune running again.
  Zero frames saved. Brio capture inside the full VM is still unverified.

Do not commit device serials, raw frames, embeddings or per-machine owner logs.
Resolve the stable `/dev/v4l/by-id/*Brio_101*-video-index0` path locally and
verify its UVC capture capability. Store an exact selected path only in local
configuration, especially if multiple cameras of the same model exist.

## Attach and probe

From the repository in PowerShell:

```powershell
.\scripts\wsl-webcam.ps1 -Action Status
# Use the current Brio bus ID. Initial dedicated binding requires Administrator:
.\scripts\wsl-webcam.ps1 -Action Bind -BusId 2-2 -ForceShare
# Normal PowerShell, after binding:
.\scripts\wsl-webcam.ps1 -Action Attach -BusId 2-2
wsl -d Ubuntu -- v4l2-ctl --list-devices
.\scripts\wsl-webcam.ps1 -Action Probe -VideoDevice /dev/video0
```

Binding persists; attachment may need repeating after WSL restarts or unplugging.
No automatic startup/reconnect service was installed. Keep WSL running during
capture. After attach, wait up to five seconds for UVC enumeration before probing:
the first immediate probe in this session ran before `/dev/video0` appeared.
Probe starts the camera, discards frames, and has a 15-second process timeout.
It requests MJPEG 640x480 at 15 fps. The diagnostic now reports V4L2/OpenCV
negotiated properties separately from decoded dimensions and measured duration.
A zero/null property means unavailable; requested settings are not evidence of
negotiation. Buffer-request acceptance is reported separately from buffer size.

If Windows reports busy, follow [the ownership TSG](../wsl-webcam.md), identifying
the current interface holder. Do not repeatedly force-bind or stop unrelated
Logitech input services. A forced-bound dedicated Brio allowed Logi Tune to be
restored without losing capture; that is a tested exception to the normal
shared-camera guidance. Restore any temporarily stopped service after confirming
that the dedicated driver excludes Windows camera access.

For Linux permissions, use the `video` group for V4L2 access. Do not make all
video or USB nodes world-writable. No microphone capture is needed or authorized
by the recognition plan; the Brio also exposes USB audio interfaces.

## Full OS VM handoff

Use the single-window OS launcher, not a container chat preview. Close WSL
capture consumers and pass the camera's current Windows USB/IP bus ID. The
launcher attaches that exact device when needed, resolves its current Linux
bus/device address, and grants the WSL user a temporary ACL on only that USB
node. It restores the previous ACL when QEMU exits (including failed launch),
provided the node identity has not changed during unplug/replug. Dry-run modes
never attach a device or change its ACL:

```powershell
.\scripts\run.ps1 -CameraBusId 2-2
```

If exactly one stable `/dev/v4l/by-id/*-video-index0` camera is already attached
to WSL, a normal `.\scripts\run.ps1` launch discovers it automatically. The
explicit `-CameraBusId` form is preferred after a WSL restart or USB reconnect.
Re-enumerate with `usbipd list` if the Windows bus ID changed.

The guest owns capture while passed through; WSL and guest must not compete for
it. Verify guest UVC enumeration, capture as the intended unprivileged service
user, and ten decoded frames before claiming VM success. Keep apps inside the
VM window. Launch with no attached stable camera and no camera selectors for a
camera-free VM.

### Stage 1 bounded guest command

Build the optional image locally with
`AIOS_IDENTITY_BUILD=1 bash scripts/build-iso-container.sh` in WSL, then launch
that exact image using `scripts/run.ps1 -Name AIOS-recognition-stage1` and the
current `-CameraBusId`. Record the ISO hash and its identity build-input flag.
Do not substitute the normal desktop image for dependency validation.

Inside the guest, close Settings camera preview and photo capture. Verify imports
with `python3 -c 'import cv2, cryptography'` and the shell startup separately.
The current local-greeting capture account is `aios` (a member of `video`), not
root or the identity broker. Resolve the selected stable index0 node locally,
then run from an administrator terminal:

```sh
su aios -s /bin/sh -c 'PYTHONPATH=/usr/local/share/aios python3 -m aios.camera --guest-probe /dev/video0 --expected-user aios'
```

Replace `/dev/video0` with the verified stable `/dev/v4l/by-id/...-video-index0`
path when available; never copy its serial-bearing name into results. The
command refuses root and any other account, runs three independent acquisitions
with a 15-second child deadline each, drains three warmup frames per acquisition,
and requires ten decoded 640x480 frames per acquisition. Every child releases
the camera on success/failure and is killed and reaped on timeout. A successful
report includes two reopen timings; warmup draining alone is not proof of
driver timestamp freshness. No media is written.

OpenCV can collapse busy, unsupported-node and driver errors into `open_failed`;
do not infer a more specific cause from that result. Ordinary device-access
failures preserve allowlisted `permission_denied`, `device_busy`, and
`device_missing` reason codes. Driver stderr and arbitrary worker fields are
never forwarded. Use `v4l2-ctl --all` locally to distinguish node capabilities;
retain only aggregate format/capability findings.

| Validation | Required evidence | Current result |
| --- | --- | --- |
| Three captures as `aios` | 10 decoded frames each, negotiated format/rate, timings | Not tested |
| Close/reopen | Second/third open timings and successful frames | Not tested |
| Permission denied | Unprivileged user outside video group fails within deadline | Not tested |
| Busy / wrong node | Controlled competing owner or non-capture node, bounded failure | Not tested |
| Blocked read | Killed child, responsive caller, subsequent device reuse | Unit coverage only |
| Unplug/replug | Operator disconnect, bounded failure, newly resolved device succeeds | Not tested |
| Explicit / automatic handoff | Same selected device, no WSL capture holder | Not tested |
| Scoped USB ACL | Only selected node changes; restore prior ACL after run | Not tested |
| Optional identity image | OpenCV/crypto imports, Qt shell startup, keyboard/PIN fallback | Headless imports/Qt/desktop passed; PIN interaction not tested |

2026-09-15 inventory: the Brio is disconnected (persisted USB/IP binding only).
No new physical capture evidence is claimed. Reconnect it before executing the
matrix. The connected Dell camera is not a substitute. Keep serials, raw driver
logs and frames out of committed results. Record aggregate JSON, image hash,
runtime versions and reason counts only.

### 2026-09-15 optional-image evidence

- Local WSL/container build succeeded with `AIOS_IDENTITY_BUILD=1`, recorded
  in the ISO build manifest; 786 packages. The unavailable FeatherPad dependency
  was replaced with Alpine v3.23 Mousepad. Its fixed sandbox command disables
  D-Bus instance forwarding; namespace and Wayland restrictions remain in force.
- Artifact: `alpine-aios-recognition-stage1-x86_64.iso`, SHA-256
  `649c73a3537fb0cd52d80a1ff2961f5589eba463d9384755e6a4b0048e16359d`.
  Diagnostic source is commit `0050c28`; packaging and launcher fixes are in
  `22b1f81` (the packaging changes were present during this development build).
- Launched through `scripts/run.ps1` with WSL QEMU/KVM, 16 GiB RAM, four vCPUs,
  a disposable disk, audio disabled, and unique name
  `AIOS-recognition-stage1-dependency-check`.
- WSLg reported COPY MODE, so this run used the supported headless path. It
  **does not establish windowed WSLg or physical camera success**.
- Guest checks passed: imports of `cv2`, `cryptography`, and `aios.camera` as
  `aios`; Mousepad executable present; no missing `aios-shell` dynamic libraries;
  ordinary-user shell process running. QEMU framebuffer inspection showed the
  Ocean desktop and Welcome wizard, not a black/blank surface. VM powered off.
- Focused tests: camera 12 passed; identity sessions 32 passed; Windows launcher
  11 passed/3 platform skips; Linux launcher 3 passed/11 platform skips.
  Broad Python run: 615 tests, two pre-existing failures and nine skips. Both
  failures (`test_skills` builder tools and `test_terminal_theme` launcher-count
  expectations) reproduce in a clean worktree of `main` at `f43c9f1`.
- No camera frames were captured or retained. Physical Brio, permission/busy/
  wrong-node/reconnect matrix, interactive PIN fallback, and protected-session
  compositor behavior remain unverified. Recognition stays disabled.

To deliberately return the dedicated camera to Windows later, detach and unbind
using the current bus ID; unbind requires Administrator. This is not the normal
end-of-test cleanup for this dedicated device.

Implementation plan: [occasional face recognition](../plans/brio-occasional-recognition.md).

## Recognition runtime prerequisites

The implementation remains disabled until `/etc/aios/face-models.json` is
installed with `yunet` and `sface` records accepted by
`aios.biometrics.verified_model`. Each record must provide an absolute model
path, SHA-256 checksum, source, license, revision, input and output contract.
The manifest must also contain a `calibration` object for hardware
`brio-101` with measured `match_threshold`, `runner_up_margin`,
`enrollment_consistency`, `minimum_brightness`, `maximum_brightness`,
`minimum_sharpness`, and optionally `minimum_face_size`.
The optional identity image includes `py3-opencv` and `py3-cryptography`.
Default installed images must add those packages explicitly; they are not added
to the diskless ISO because OpenCV exceeds its package-install filesystem budget.

Do not populate those values from examples or enable background recognition
until the full-VM capture and held-out calibration gates in the implementation
plan are complete. Development can point `AIOS_FACE_MODEL_MANIFEST` at a local
manifest. The shell exchanges only account metadata with the worker; it never
receives frames or embeddings, and every suggestion still requires the PIN.

## 2026-09-16 physical Brio guest measurements

The dedicated Brio 101 (`046d:094d`) was connected, explicitly attached with
`-CameraBusId 2-2`, and passed into the optional identity ISO recorded above.
After an approved WSLg display-service restart, readback changed from COPY MODE
to healthy (`use_gfxredir=1`). Neither WSL nor Docker was restarted.

Both a headless run and a normal windowed `scripts/run.ps1` run completed three
independent ten-frame captures as **`aios`, UID 1000**, using OpenCV **4.12.0** on
Alpine kernel **6.18.52-0-lts**, QEMU **8.2.2** (Ubuntu package
`1:8.2.2+ds-0ubuntu1.16`) and usbipd-win **5.3.0**. Every capture decoded **640x480**, negotiated
**MJPG/15 fps**, accepted a one-buffer request and reported buffer size one.
Each discarded three warmup frames before measuring ten frames; no frames,
crops, embeddings or camera serials were retained. Negotiated 15 fps is a driver
property, not a claim that the virtualized capture loop sustained 15 fps.

Windowed VM: `AIOS-recognition-stage1-Brio-windowed`, WSL QEMU/KVM, four vCPUs,
16 GiB, disposable disk, audio disabled. The guest desktop and chat rendered
normally in a framebuffer screenshot; the camera image was never displayed or
saved by these diagnostics.

| Capture | Open (s) | Warmup (s) | Ten frames (s) | Total before close (s) | Close (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.203377 | 1.071249 | 1.332283 | 2.606909 | 0.031060 |
| 2 (reopen) | 0.235056 | 1.142548 | 1.283369 | 2.660974 | 0.021489 |
| 3 (reopen) | 0.196605 | 1.076897 | 1.342822 | 2.616324 | 0.014065 |

Failure and recovery evidence so far:

- Guest device permissions were `0660 root:video`. Capture as `nobody` returned
  `permission_denied` without forwarding driver details.
- The non-capture `/dev/video1` returned bounded `open_failed`; nonexistent
  `/dev/video99` returned `Video device is not attached`.
- A controlled unprivileged OpenCV process held the capture device. A second
  probe returned `open_failed` in **0.410 s**. After terminating/reaping the
  holder, another ten-frame capture succeeded (open **0.191708 s**, warmup
  **1.139483 s**, capture **1.342337 s**, close **0.013822 s**).
- The operator physically unplugged the Brio. A probe during removal returned
  `open_failed`, both guest video nodes disappeared, and the ordinary-user shell
  remained alive. Reconnect validation is recorded below.

The initial long-command contention harness exceeded the serial console's
reliable interactive input handling. Retrying with short, paced heredoc lines
completed the test. That harness timeout is not counted as a driver timeout.
Recognition remains disabled; these measurements prove camera acquisition,
not face matching, liveness, authorization, or production readiness.

The complete `scripts/test-identity-display.sh` run also passed in its disposable
Alpine container: both installed-layout profile-control runs, **94 QML tests**,
the compiled application-host protocol test, and the display-isolation tests.
This includes native PIN-overlay key routing and account creation/selection
without a camera. These automated UI/security results are recorded separately
from the physical camera measurements; they are not biometric evidence.

### Reconnect, deadline and ACL completion

The operator reconnected the Brio. USB/IP reattachment changed its Linux address
from `001/002` to `001/003`. The running QEMU process remained bound to the old
address, correctly leaving its guest without a camera. Only that disposable
test VM was restarted; WSL, Docker and other VMs were not restarted. A normal
`scripts/run.ps1` launch **without `-CameraBusId`** automatically discovered the
single stable index0 camera and passed through `001/003`.

Three fresh ten-frame 640x480 MJPG/15-fps captures then passed as `aios`, with
open times **0.202490, 0.209747, 0.224050 s** and total pre-close times
**2.591456, 2.796007, 2.649451 s**. The stable by-id path also passed three
independent captures; no serial-bearing path was retained in evidence.

An actual capture interrupted with a one-second child deadline returned
`timeout` after **1.552 s** including process startup/cleanup. The worker count
after return was **zero**. Three subsequent stable-path ten-frame captures
passed (pre-close times **2.557276, 2.622820, 2.561642 s**). This validates forced
termination and release during live acquisition; no persistent hardware-driver
hang was induced.

Host ACL hashes were recorded before launch, during QEMU and after its confirmed
exit. The only changed node during the run was the selected
`/dev/bus/usb/001/003`; **all USB-node ACL hashes matched the original snapshot
after exit**. The camera remains attached to WSL and no test capture is active.

Stage 1 acquisition validation is complete. USB address changes currently
require relaunching the selected QEMU VM; transparent QEMU hotplug recovery is
not claimed. The next service layer must preserve this measured format,
unprivileged boundary, deadlines and cleanup behavior. Recognition remains off.

## Stage 2 service validation (2026-09-16)

PR #134 centralizes the four camera consumers in one private service and a
disposable native V4L2 worker. The locally built identity ISO is
`alpine-aios-recognition-stage2-x86_64.iso`, SHA-256
`3e134454ec8a20ff533f7251a6e18d05ff143d49e42bbf7624273862eae4e585`.
Its manifest records 786 packages and a 2147483648-byte image. The image build
completed; the first manifest step exposed read-only extracted-directory cleanup.
That recorder bug was fixed, covered by a regression test, and the manifest was
regenerated from the completed ISO. No package closure was inferred from sources.

Under normal windowed QEMU, the installed service ran as `aios` (UID 1000):

- Three preview-to-photo handoffs passed, each preempting the previous worker.
- All three requested portraits were returned in memory and immediately discarded.
- Inactive-desktop capture was rejected.
- Killing the service killed its active worker; no second owner was opened.
- The complete lifecycle run took **10.061 seconds**, with maximum delivered
  frame age **0.127 seconds**, measured from the kernel monotonic timestamp.
- The physical Brio also passed the same service check in WSL: **12.228 seconds**,
  maximum frame age **0.116 seconds**.
- USB/IP detach while a guest preview was active produced `device_changed` and
  the next photo request was rejected as unavailable. This is a bridge-disconnect
  test; the actual physical unplug/replug is recorded separately under stage 1.
- Reattachment changed the host address from `001/002` to `001/003`. Relaunching
  only this test VM automatically discovered the new address and preview worked.
  Transparent QEMU hotplug remains unsupported.

The opt-in `tests/manual_camera_service.py` runner retains only counts/times and
performs these checks without creating accounts, templates or portrait files.
The first removal attempt timed out in the operator harness before detach; its
operator deadline was extended to 60 seconds and the complete check passed.
Production capture deadlines were not changed.

Automated validation passed: **18 service/freshness tests**, **12 camera tests**,
**6 recognition tests**, **20 manifest tests**, and the full display suite
(95 QML checks including Ocean/Sage, both installed-layout profile runs,
application-host protocol and three private display/PIN-routing checks).
The final dialog-cancellation change was compiled and exercised by that display
suite after the ISO build; it awaits inclusion in the next batched image.

Successful enrollment/inference transitions still require the calibrated model
and consented evaluation inputs in later stages. These capture results do not
establish biometric accuracy, liveness, or protected-session authorization.

## Stage 5 installed artifact and extended lifecycle check (2026-09-16)

The local identity image `alpine-aios-recognition-stage5-x86_64.iso` was built
from production source `d4d3731`, with 786 resolved packages and 2,182,791,168
bytes. SHA-256:
`b8eb6dbc8458236f32110809afcaef6553bde9f98ce863a716aec712b02bad93`.
The generated build manifest and package list accompany it in `distro/alpine/out`.
No camera code was substituted into the guest for these checks.

Reference environment: AMD Ryzen AI Max+ Pro 395 host, 51,298,828,288 bytes host
RAM; WSL 2.7.10.0, WSLg 1.0.73.2, QEMU 8.2.2 with KVM/host CPU, four vCPUs and
16 GiB guest RAM. Guest kernel 6.18.52-0-lts, OpenCV 4.12.0-r3, cryptography
46.0.7-r0. Brio USB device revision reports `9915`; a separately verified firmware
version was not obtained. No serial is retained in this report.

As ordinary `aios` UID 1000, the installed checksum-pinned models passed the
camera-free synthetic runtime check: load 0.1465 s, cold detect+feature 0.0972 s,
warm 0.0504/0.0506/0.0553 s, peak process RSS 240304 KiB. This does not measure
real-face alignment, recognition accuracy or service-plus-worker resource use.
The production calibration/approval manifest was absent, as intended.

The first extended preview/photo lifecycle run stopped with `photo unavailable`.
Its original runner did not report the cycle or service reason, so the cause is
unresolved. The runner now reports those metadata on failure. A fresh diagnostic
repeat passed **20 previews, 20 photos and 20 preemptions**, inactive capture
rejection and service-kill/worker cleanup in **54.348 s**, with maximum frame age
**0.122 s**. Media stayed in memory and was discarded. This successful repeat
does not erase the initial failure or establish a long-duration reliability gate.

The installed Settings UI was visually checked in Ocean and Sage, including
keyboard focus, off-state/purge text and reduced motion. Camera preview remained
off during screenshots. `xdotool` was installed only into the disposable guest
for UI input; it is not an added image dependency. The subsequent status-text
refinement distinguishes active capture and paused suggestions; its final image
and test evidence are recorded in `recognition-release.md`.

No people were enrolled, no cohort measurements were made, and no recognition
approval was created. Accuracy/adversarial, complete lifecycle/UX and long-soak
gates remain open in #95; production recognition remains off.
