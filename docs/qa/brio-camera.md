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
| Optional identity image | OpenCV/crypto imports, Qt shell startup, keyboard/PIN fallback | Not tested |

2026-09-15 inventory: the Brio is disconnected (persisted USB/IP binding only).
No new physical capture evidence is claimed. Reconnect it before executing the
matrix. The connected Dell camera is not a substitute. Keep serials, raw driver
logs and frames out of committed results. Record aggregate JSON, image hash,
runtime versions and reason counts only.

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
