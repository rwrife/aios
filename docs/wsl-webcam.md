# Webcam development in WSL

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

## Test environments

Keep camera experiments in WSL, and exercise session authorization with deterministic
identity events. `scripts/test-identity-linux.sh` runs real namespace, cgroup and
encrypted-workspace tests in a disposable Alpine container. Full device/compositor
release testing still requires a booted Alpine system or VM with device passthrough;
WSLg alone is not the secure multi-user display boundary.
