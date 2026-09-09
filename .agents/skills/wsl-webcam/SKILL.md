---
name: wsl-webcam
description: Set up or troubleshoot USB webcam access from Windows into WSL, including busy-device ownership, restarting background camera agents, UVC checks, and bounded capture validation. Use for WSL camera attachment failures; not biometric enrollment or general Windows account changes.
---

Use the repository's [WSL webcam TSG](../../../docs/wsl-webcam.md) and
[PowerShell helper](../../../scripts/wsl-webcam.ps1). Resolve those paths from
this skill directory; do not depend on a particular drive letter or device ID.

Start with `-Action Status`. Determine the actual webcam bus ID and Linux UVC
module availability. A device listed by Windows is not necessarily USB; a listed
USB device is not proof of a working V4L2 capture stream.

For `Device busy (exported)`, identify the holder before escalating or repeating
attach. Sysinternals Handle must search the camera interface's current PDO path,
not only the composite USB parent. `-Action FindOwner` derives this from the
selected device and validates the Microsoft signature of the supplied Handle tool.
If an agent returns with a new PID, correlate its parent with a Windows service.
In the observed Dell WB7022 case, LogiTuneUpdaterService restarted LogiTuneAgent,
which held the camera after the visible app was closed. Treat this as a diagnosis
to verify, not a reason to stop Logi Tune on every machine.

Within an authorized camera handoff, use the specific holder's supported stop
mechanism. The helper's explicit `StopLogiTune` action records service state and
stops that watchdog/agent without changing startup configuration. Restore it
after detaching with `RestoreLogiTune`. Do not stop unrelated Logitech input
services, disable Windows Hello, close raw driver handles, or reboot WSL/Windows
to work around an unidentified hold.

Keep the selected WSL distro alive across the attach command. The helper uses a
bounded keeper process. Windows native stderr can contain successful status
messages; use process exit codes, not PowerShell NativeCommandError, to judge
success. Avoid unbounded retries: after one retry following a real state change,
use ownership or bridge diagnostics. Forced sharing may require a physical
unplug/replug while leaving the device bound; unbinding undoes that preparation.

Use `TraceAttach` only when ordinary status/ownership checks do not explain the
failure. It restores the previous USB bridge service state and disables packet
capture. Diagnostic success still requires a fresh normal attach after its
temporary server exits. Do not commit logs, device serials, downloaded tools, or
capture buffers; the default output lives under ignored `tmp/`.

Verify success with `/dev/video*` enumeration and the explicit bounded `Probe`.
The probe activates the camera, discards frames and reports only counts, size and
timing. Report attached-but-unproven separately from successful capture. Camera
access does not establish face recognition accuracy, liveness, or display security.
