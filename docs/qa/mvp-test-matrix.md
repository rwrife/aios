# AI OS MVP test matrix (initial)

## Hypervisors
- QEMU (x86_64, virtio)
- VMware Workstation/Fusion

## Checks
1. Boot reaches login/session automatically
2. X11 starts automatically from tty1
3. Openbox session launches xterm
4. No right-click root menu app launcher
5. Network interface acquires DHCP address
6. Disk devices visible (vda/sda/nvme0n1)

## Status
- Branch implements startup/session scaffolding
- Full runtime validation pending first ISO build
