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
It currently requests MJPEG 640x480 at 15 fps; reported probe duration does not
establish the negotiated frame rate. Inspect actual settings with `v4l2-ctl`.

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
capture consumers, inspect `lsusb`, and grant the development user read/write
access to the selected USB node only (a temporary ACL is sufficient). For the
observed bus/device numbers, subject to re-verification:

```powershell
wsl -d Ubuntu -- lsusb
# Replace user and node with current verified values; root is only for the ACL.
wsl -d Ubuntu -u root -- setfacl -m u:ryrife:rw /dev/bus/usb/001/002
$env:AIOS_VM_CAMERA_BUS = '1'
$env:AIOS_VM_CAMERA_ADDR = '2'
.\scripts\run.ps1
```

The guest owns capture while passed through; WSL and guest must not compete for
it. Verify guest UVC enumeration, capture as the intended unprivileged service
user, and ten decoded frames before claiming VM success. Keep apps inside the
VM window. Remove the two environment variables for a camera-free launch.

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

Do not populate those values from examples or enable background recognition
until the full-VM capture and held-out calibration gates in the implementation
plan are complete. Development can point `AIOS_FACE_MODEL_MANIFEST` at a local
manifest. The shell exchanges only account metadata with the worker; it never
receives frames or embeddings, and every suggestion still requires the PIN.
