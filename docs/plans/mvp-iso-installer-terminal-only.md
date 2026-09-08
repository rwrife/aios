# AI OS ISO + Installer MVP Implementation Plan

> Historical plan, superseded by `docs/specs/chat-desktop.md`. The current product
> includes chat, default development tools, and visible secondary power controls.

> For Hermes: execute this plan task-by-task; implementation target is VM-first (QEMU + VMware).

Goal: Build a bootable ISO that starts with a branded loading splash, then auto-enters a single-user X11 session running only a terminal in a minimal window manager, plus an installer path to install onto local SSD.

Architecture: Use Alpine custom ISO tooling (`mkimage`) for MVP because it already supports live ISO workflows and disk installation (`setup-alpine`/custom setup wrapper). Keep desktop stack minimal: Xorg + tiny WM + xterm only, autologin to local user, no desktop menus/power UI. Keep Buildroot as a later rebase option once installer UX is stable.

Tech Stack: Alpine mkimage, OpenRC, Linux kernel, Xorg, Openbox (locked down), xterm, Plymouth bootsplash, QEMU, VMware.

---

## 1) Requirements lock (derived from your spec)

Must-have for MVP:
1. Bootable ISO.
2. Installer option to local machine SSD.
3. VM-first support (QEMU + VMware).
4. Fancy splash screen with visible loading indicator before desktop.
5. No login prompt in normal mode (single-user autologin).
6. X11 session auto-starts and only terminal is usable.
7. No shutdown UI/function in WM (no menu/button/hotkey).
8. Internet works out-of-box (DHCP in VM).
9. SSD visibility/access (detect/mount local disks).
10. Build toolchain deferred to later milestone.

Non-goals for MVP:
- Multi-user auth model.
- Full hardware compatibility across all bare metal devices.
- App store/package UX.

---

## 2) Repository layout to add

Create/maintain these paths in `aios`:

- `distro/alpine/`
- `distro/alpine/mkimage.sh` (wrapper that calls Alpine mkimage profile)
- `distro/alpine/profiles/aios.profile.sh`
- `distro/alpine/apks/world.base`
- `distro/alpine/apks/world.x11`
- `distro/alpine/apks/world.vm`
- `distro/alpine/overlay/etc/inittab`
- `distro/alpine/overlay/etc/profile`
- `distro/alpine/overlay/etc/X11/xinit/xinitrc`
- `distro/alpine/overlay/etc/xdg/openbox/rc.xml`
- `distro/alpine/overlay/etc/xdg/openbox/menu.xml`
- `distro/alpine/overlay/usr/local/bin/aios-session`
- `distro/alpine/overlay/usr/local/bin/aios-install`
- `distro/alpine/overlay/etc/plymouth/plymouthd.conf`
- `distro/alpine/overlay/usr/share/plymouth/themes/aios/*`
- `scripts/build-iso.sh`
- `scripts/run-qemu-live.sh`
- `scripts/run-qemu-install.sh`
- `scripts/test-smoke.sh`
- `docs/specs/mvp-behavior.md`
- `docs/qa/mvp-test-matrix.md`

---

## 3) Package profile decisions

Base packages:
- BusyBox/base Alpine live utilities
- `xorg-server`, `xinit`, `xauth`
- `openbox`
- `xterm`
- `dhcpcd` (or equivalent DHCP client)
- disk tools: `util-linux`, `e2fsprogs`, `parted`, `dosfstools`

VM/hardware enablement packages (MVP-safe):
- Xorg video: modesetting + VMware fallback
- input: libinput
- optional guest tooling for VMware if needed later

Splash:
- `plymouth` + theme assets

Explicit exclusions:
- no browser, file manager, editor GUI, settings panel, power manager, notification daemon.

---

## 4) Boot behavior design

Boot menu entries:
1. `AI OS Live (default)`
   - boots live system
   - autologin user `aios`
   - auto-run X11 session
2. `AI OS Install to disk`
   - enters installer workflow (text menu/wrapper script)

Live startup path:
- Kernel/initramfs starts
- Plymouth splash shows branded screen + spinner/progress
- getty autologin on tty1
- `startx` auto-runs
- openbox starts with locked config
- `xterm` starts fullscreen/maximized

Session behavior hardening:
- Disable openbox root menu and keybinds for app launch/power actions.
- Do not install GUI shutdown tools.
- Keep terminal as only visible/usable app.

---

## 5) Installer design (MVP)

Installer mode behavior:
- Guided single-disk install for VM targets first (`/dev/vda`, `/dev/sda`, `/dev/nvme0n1`).
- Partition layout (MVP):
  - EFI: 512MiB FAT32 (if UEFI)
  - root: remainder ext4
- Install bootloader + copy configured system.
- First boot of installed system follows same autologin -> X11 terminal-only flow.

Safety guardrails:
- Show target disk and require explicit confirmation token before wipe.
- Refuse install if more than one non-removable disk unless user explicitly selects one.

Note: for MVP, wrapping Alpine’s install flow is acceptable; full custom graphical installer is deferred.

---

## 6) Driver/compatibility baseline for stated VM targets

Kernel/driver baseline to include:
- Storage: `virtio_blk`, `virtio_scsi`, `ahci`, `nvme`
- Network: `virtio_net`, `e1000`, `vmxnet3`
- Video: `virtio_gpu`, `vmwgfx`, DRM/KMS stack
- Filesystems: `ext4` (required), plus `vfat` for EFI

Acceptance:
- QEMU virtio profile gets network + display + disk access.
- VMware profile gets network + display + disk access.

---

## 7) Execution phases

### Phase A: Live ISO first boot (no installer yet)
Objective: prove splash -> autologin -> X11 terminal-only path.

Steps:
1. Create Alpine profile + package lists.
2. Add overlays for autologin and xinit flow.
3. Add locked openbox configs.
4. Build ISO.
5. Boot in QEMU and verify behavior.

Exit criteria:
- boots with splash
- reaches xterm-only desktop without login prompt
- no WM shutdown/menu functionality exposed

### Phase B: Installer mode
Objective: add reliable install-to-SSD path.

Steps:
1. Add `aios-install` script/wrapper flow.
2. Add boot-menu installer entry.
3. Install from ISO to blank virtual disk.
4. Reboot installed system and verify same UX.

Exit criteria:
- successful install in QEMU and VMware
- installed system auto-boots to xterm-only session

### Phase C: Validation and hardening
Objective: make behavior deterministic and safe.

Steps:
1. Smoke tests for network, disk visibility, and no-extra-apps.
2. Repeat boot/install cycles.
3. Document known limitations and recovery path.

Exit criteria:
- test matrix passes in both hypervisors
- deterministic startup path over repeated runs

---

## 8) Test plan (must pass)

Functional tests:
- ISO boots in QEMU and VMware.
- Splash appears before desktop.
- Desktop auto-enters terminal-only environment.
- No visible UI shutdown action in WM.

Installer tests:
- Install to blank virtual SSD succeeds.
- Installed system reboots into same terminal-only UX.

Network tests:
- DHCP lease acquired.
- DNS + outbound connectivity works.

Disk tests:
- SSD devices visible via `lsblk`.
- Root filesystem mounted and writable.

Negative tests:
- `firefox`, `thunar`, `gnome-terminal` unavailable.
- openbox menu invocation does nothing.

---

## 9) Risks and mitigation

Risk: Plymouth integration complexity on minimal stack.
- Mitigation: if blocker, use lightweight fallback splash in initramfs for MVP and keep plymouth as Phase C item.

Risk: VMware/QEMU driver gaps from aggressive minimization.
- Mitigation: keep broad VM driver baseline first, optimize size after tests pass.

Risk: no-auth autologin reduces local security.
- Mitigation: keep scope explicitly appliance-like for MVP; add optional auth profile later.

Risk: installer data-loss mistakes.
- Mitigation: strong confirmation prompts and single-disk safeguards.

---

## 10) Deferred backlog (explicitly later)

- Build tools/toolchain in system image
- Package build sandbox
- Bare-metal hardware profile expansion
- Optional authenticated mode
- Buildroot rebase path after installer behavior is stable

---

## 11) Definition of done for this MVP

MVP is complete when all are true:
1. ISO boots in QEMU and VMware.
2. Splash with loading indicator displays during startup.
3. System auto-enters X11 with no login prompt.
4. WM session exposes terminal-only workflow.
5. No shutdown option exists in WM UI/keybinds.
6. Installer mode installs to SSD and reboot works.
7. Internet and disk access validated by smoke tests.
