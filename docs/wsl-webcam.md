# Webcam development in WSL

This is the reusable troubleshooting guide (TSG). Agents can load the repository
skill at [`.agents/skills/wsl-webcam/SKILL.md`](../.agents/skills/wsl-webcam/SKILL.md).
Use [`scripts/wsl-webcam.ps1`](../scripts/wsl-webcam.ps1) for the repeatable Windows
operations below. Its default `Status` action is read-only.

Use USB passthrough for a USB UVC camera. AIOS then reads ordinary Linux V4L2
devices, which is also the native Alpine capture interface. A Windows stream
bridge is unnecessary when passthrough works. Non-USB integrated cameras may
need a different development path; do not assume they are USB devices.

## Windows setup

Follow [Microsoft's USB instructions](https://learn.microsoft.com/en-us/windows/wsl/connect-usb)
and the [usbipd-win WSL guide](https://github.com/dorssel/usbipd-win/wiki/WSL-support).

```powershell
winget install --interactive --exact dorssel.usbipd-win
usbipd list
# Administrator PowerShell, using the webcam's actual bus ID from the list:
usbipd bind --busid <BUSID>
# Ordinary PowerShell, with WSL running:
usbipd attach --wsl --busid <BUSID>
```

The bridge may require a new terminal before `usbipd` is on PATH. Alternatively,
use `& 'C:\Program Files\usbipd-win\usbipd.exe' list`. Installation and binding
require administrative privileges. Do not share unrelated USB devices.

Windows cannot use the attached camera until it is detached. If Windows keeps it
busy, close applications using it. `usbipd bind --busid <BUSID> --force` can replace
its Windows driver with the sharing driver; this may require unplugging and
reconnecting the camera. A driver restart is not supported by every webcam.
Do not repeatedly retry an exported/busy camera without resolving the device hold.

## Busy-camera decision path

1. Run `./scripts/wsl-webcam.ps1 -Action Status`; select the webcam's current bus ID.
2. Check kernel/module support and keep a WSL process running while attaching.
   The helper's `Attach` action handles the short-lived keeper automatically.
3. If attachment reports `Device busy (exported)`, find the actual Windows holder.
   Closing a visible app does not necessarily close its background agent.
4. If the process reappears with a new PID, identify its parent and watchdog service.
   Stop the identified watchdog before its child, within the authorized camera
   handoff. Do not disable unrelated services or change startup configuration.
5. Retry once after that state change. If it still fails, inspect bridge diagnostics
   instead of repeating force-bind/replug cycles or assuming Windows Hello is at fault.

### Find the actual owner

Download [Microsoft Sysinternals Handle](https://learn.microsoft.com/en-us/sysinternals/downloads/handle)
outside version control. `FindOwner` checks its Microsoft signature and searches
the selected camera's current device-interface paths. Use Administrator PowerShell:

```powershell
.\scripts\wsl-webcam.ps1 -Action FindOwner -BusId <BUSID> -HandlePath C:\Tools\Handle\handle64.exe
```

The USB composite parent and its video interface have different PDO names.
Searching only the parent can miss the application holding the video interface.
The helper follows PnP parent links so identical camera models on other ports are
not mistaken for the selected camera. It only searches handles; never close raw
driver handles with Handle's `-c` option.

**Observed cause in this development session:** `LogiTuneAgent.exe` held the Dell
video interface. `LogiTuneUpdaterService` was its parent and restarted the agent
with a new PID after it was killed. Removing the Windows Hello association and
quitting the visible app did not resolve that background hold. Verify this diagnosis
on future machines before stopping any service.

When Logi Tune is confirmed as the owner, use Administrator PowerShell:

```powershell
.\scripts\wsl-webcam.ps1 -Action StopLogiTune
.\scripts\wsl-webcam.ps1 -Action Bind -BusId <BUSID>
.\scripts\wsl-webcam.ps1 -Action Attach -BusId <BUSID>
```

`StopLogiTune` records whether its updater service was running, stops that service
and the agent, and leaves its startup type unchanged. It does not stop Logitech
keyboard/mouse services. The snapshot lives under ignored `tmp/wsl-webcam/` and
is needed for `RestoreLogiTune`. A canceled administrator prompt means the action
did not run; do not report that the service stopped or work around the cancellation.

### Get a precise bridge error

```powershell
.\scripts\wsl-webcam.ps1 -Action TraceAttach -BusId <BUSID>
```

This uses the [usbipd diagnostic-server procedure](https://github.com/dorssel/usbipd-win/wiki/Troubleshooting),
with debug text logs and packet capture explicitly disabled. It refuses to restart
the bridge while another USB/IP device is attached, stops the temporary server in
`finally`, and restores the prior bridge service state. Reattach normally afterward
to keep a successful connection. Do not commit logs or downloaded diagnostic tools.

In the observed failure, the log contained
`CM_Query_And_Remove_SubTree ... CR_REMOVE_VETOED: PNP_VetoOutstandingOpen`
for the video interface. That indicates an outstanding Windows device handle;
it is not a missing Linux camera driver. `Device in error state` after forced
sharing is less specific and is not evidence that capture works.

Windows PowerShell 5.1 can treat native stderr status messages as
`NativeCommandError` even when they are informational. The helper uses native exit
codes; the diagnostic action redirects stderr separately so a normal usbipd status
line cannot prematurely stop and clean up the debug server.

## Linux setup and diagnostic

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends v4l-utils usbutils python3-opencv
modinfo uvcvideo
lsusb
v4l2-ctl --list-devices
```

The Linux kernel must include UVC support and the module must be installed.
Check `zgrep CONFIG_USB_VIDEO_CLASS /proc/config.gz` and `modinfo uvcvideo` before
considering a custom kernel. On this development machine, WSL 2.7.10 with kernel
6.18.33.2 already has the `uvcvideo` module. The Dell WB7022 is a USB camera;
the separately listed HP camera is not enumerated as a USB webcam by usbipd.

Run the diagnostic from the repository. Enumeration does not start capture:

```sh
PYTHONPATH=apps python3 -m aios.camera
# Explicitly activates the selected camera; reads and discards ten frames:
PYTHONPATH=apps python3 -m aios.camera --probe /dev/video0
```

The probe requests 640×480 MJPEG at 15 fps and reports the actual dimensions,
frame count and elapsed time. It stores no image/video or biometric template.
A separate process limits a blocked driver read to 15 seconds. Use the capture
device reported by `v4l2-ctl`; a webcam may also expose a metadata-only video node.
Grant the Linux development user membership in `video` if needed; do not make
video devices world-writable. New group membership needs a new login or `sg video`.

If `/dev/video*` is missing, attachment or driver enumeration failed. If the device
exists but returns no frames, inspect its supported formats with
`v4l2-ctl -d /dev/video0 --list-formats-ext`. Start with modest resolution/frame rate
for USB/IP. Successful capture is not proof of recognition accuracy or liveness.

## Return the camera to Windows

```powershell
usbipd detach --busid <BUSID>
# Administrator PowerShell if forced sharing was used or sharing is no longer needed:
usbipd unbind --busid <BUSID>
```

Unplug/reconnect if Windows does not reclaim it. Re-check the bus ID after moving
USB ports or reconnecting. Avoid restarting all of WSL while builds are running.

If this workflow stopped Logi Tune, restore its recorded state after detaching:

```powershell
# Administrator PowerShell:
.\scripts\wsl-webcam.ps1 -Action RestoreLogiTune
```

Do not restore its camera-monitoring agent while expecting exclusive WSL ownership.
Report the final device attachment, capture result, and any service intentionally
left stopped. A USB device listing alone is not the success criterion.

## Test environments

Keep camera experiments in WSL, and exercise session authorization with deterministic
identity events. `scripts/test-identity-linux.sh` runs real namespace, cgroup and
encrypted-workspace tests in a disposable Alpine container. Full device/compositor
release testing still requires a booted Alpine system or VM with device passthrough;
WSLg alone is not the secure multi-user display boundary.
