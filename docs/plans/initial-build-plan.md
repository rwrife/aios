# Minimal Terminal-Only Linux Distro Implementation Plan

> Historical plan, superseded by `docs/specs/chat-desktop.md`. Do not implement
> terminal-only restrictions or rebase to Buildroot from this document.

> For Hermes: this is planning only; no implementation in this step.

Goal: Build a small, reproducible Linux distro image that boots directly into a lightweight X11 window manager and exposes only a terminal app (no browser, file manager, office apps, etc.).

Architecture: Use Buildroot as the build system to generate a custom root filesystem and bootable image. Keep userland minimal (BusyBox + init + Xorg + tiny WM + xterm). Configure autologin and auto-start X so the system lands in a WM session running terminal only.

Tech Stack: Buildroot, Linux kernel, BusyBox, Xorg, tiny WM (dwm/openbox/fluxbox), xterm, QEMU (for validation).

---

## Why this approach

Recommendation: Buildroot-first.
- Small output size and deterministic builds.
- Easier to strip packages than general-purpose distros.
- Good for "kiosk-like minimal desktop" behavior.

Alternative (faster prototype): Alpine custom ISO (mkimage).
- Faster path to first boot.
- Less control than Buildroot over image composition.

Given your goals (small + easy + minimal), do:
1) Prototype quickly in Alpine if you want rapid visual results.
2) Ship on Buildroot for a cleaner, tighter distro.

---

## Scope definition (before building)

Decide these first:
1. Target architecture: x86_64 only (initially).
2. Boot mode: UEFI only (or UEFI + BIOS).
3. Display stack: X11 (simple and proven).
4. WM choice: openbox (easiest) or dwm (smallest, but custom compile workflow).
5. Terminal choice: xterm.
6. Security model: autologin local user for MVP; harden later.

Acceptance criteria:
- System boots to graphical session automatically.
- Only terminal is available from UI.
- No package manager tools exposed to normal user session.
- No network-facing services enabled by default (except SSH if explicitly requested).
- Reproducible build command produces same artifact structure.

---

## Step-by-step implementation plan

### Phase 1: Build environment and baseline image

1) Create workspace structure
- `distro/`
- `distro/buildroot/`
- `distro/board/miniterm/`
- `distro/board/miniterm/rootfs-overlay/`
- `distro/configs/`

2) Fetch Buildroot and build a baseline console image
- Start from an x86_64 defconfig.
- Build once with no X11 to verify toolchain/kernel/rootfs pipeline.

3) Record the baseline config
- Save defconfig to: `distro/configs/miniterm_defconfig`

Validation checkpoint:
- Image boots in QEMU.
- Console login works.

---

### Phase 2: Add minimal graphical stack

4) Enable only required graphics packages in Buildroot config
- X server components.
- Input/video drivers needed for your VM/hardware.
- `xinit`.
- One WM (openbox or dwm).
- `xterm`.

5) Keep package set minimal
- Disable audio stacks, printing, bluetooth, desktop portals, browser/toolkit-heavy apps unless required.

6) Rebuild and boot test
- Confirm X starts manually first (`startx` from shell).

Validation checkpoint:
- WM starts.
- `xterm` launches.
- No extra GUI applications installed.

---

### Phase 3: Auto-boot into WM + terminal-only UX

7) Add rootfs overlay files

Files to create/modify:
- `distro/board/miniterm/rootfs-overlay/etc/inittab`
  - Configure tty autologin for a dedicated local user (MVP).
- `distro/board/miniterm/rootfs-overlay/etc/profile`
  - Auto-run `startx` on tty1 login.
- `distro/board/miniterm/rootfs-overlay/etc/X11/xinit/xinitrc`
  - Start WM and auto-launch xterm.

8) Restrict session surface area
- Remove WM menus or keybindings that launch non-terminal apps.
- Hide/disable desktop helpers.
- Ensure PATH does not include package-management tools for normal user shell.

9) Add clean shutdown/restart keybinds only (optional)
- Keep a minimal WM config for usability.

Validation checkpoint:
- Boot -> autologin -> X -> WM -> terminal visible, without manual steps.
- No other GUI launchers present.

---

### Phase 4: Hardening and polish

10) Security baseline
- Disable root remote login.
- Remove unnecessary daemons.
- If networking required, keep SSH optional and key-only.

11) Reliability
- Ensure filesystem mount options and logs are sane.
- Add watchdog/restart behavior only if required for your environment.

12) Branding/versioning
- Add `/etc/os-release` metadata.
- Add build version string and artifact naming convention.

Validation checkpoint:
- Cold boot test x10 in VM.
- Verify deterministic startup behavior.

---

### Phase 5: Build artifacts and release process

13) Define output artifacts
- VM image (qcow2/raw).
- Bootable image (ISO or disk image depending on platform target).

14) Add repeatable build entrypoint
- `distro/scripts/build.sh`
- `distro/scripts/test-qemu.sh`

15) Add CI (optional but recommended)
- Build image on each tag/release branch.
- Save artifacts and checksums.

Validation checkpoint:
- One command builds image from clean checkout.
- Test script boots image and verifies terminal-on-WM startup.

---

## Test and verification checklist

Functional:
- Boots to WM automatically within target time budget.
- Terminal available and interactive.
- No browser/editor/file manager GUI apps present.

Negative tests:
- Attempt common app launch commands (`firefox`, `thunar`, etc.) -> not installed.
- WM app menu entries for non-terminal apps -> absent.

Operational:
- Reboot and shutdown from terminal works.
- Network up/down does not break boot path.

Reproducibility:
- Fresh build on another machine with same Buildroot version creates equivalent artifact set.

---

## Risks and tradeoffs

1) "Easy setup" vs "very minimal"
- Extreme minimalism increases manual config burden.
- Mitigation: openbox for MVP, then tighten further.

2) Driver support in tiny images
- Too aggressive package trimming can break video/input.
- Mitigation: validate with target hardware early, not only QEMU.

3) Autologin security
- Convenient for appliance use, weaker for shared physical access.
- Mitigation: keep this in MVP only, add optional login mode profile.

4) Wayland vs X11
- Wayland can be cleaner long-term, but X11 is easier for first minimal terminal-only desktop.

---

## Suggested first milestone (1-2 days)

Deliverable:
- Bootable VM image that auto-starts into openbox + xterm only.
- Build instructions + defconfig committed.

Milestone done when:
- `startx` is no longer manual.
- User lands in terminal window after boot every time.

---

## Open decisions to finalize before execution

1) Buildroot-only or Alpine prototype first?
2) WM choice: openbox (simpler) vs dwm (smaller).
3) Need SSH in base image or fully local-only?
4) UEFI-only acceptable?
5) Target hardware beyond VM (GPU/input constraints)?
