# Recovery, diagnostics and startup limitations

This page is the operator runbook for the stage-3 hardware work: what to do
when Wi-Fi or the desktop does not come up, what the sanitized hardware report
contains, and which limitations are real today. Nothing here claims that any
physical machine has been tested; see
[hardware coverage](qa/hardware-coverage.md) for tested status.

## Boot entries

The ISO offers three entries on both bootloaders. `live` is the default on
each; `recovery` is selectable without editing kernel arguments and never
replaces the normal path.

| Entry | Syslinux label | GRUB title | Extra kernel arguments |
| --- | --- | --- | --- |
| Normal desktop | `live` (default) | `AIOS live` | none |
| Guided install | `install` | `AIOS install` | `aios.install` |
| Recovery | `recovery` | `AIOS recovery (safe graphics)` | `aios.recovery nomodeset` |

An installed system keeps the same recovery route. `aios-install` adds an
`AIOS recovery (safe graphics)` entry to `/boot/grub/custom.cfg`, derived from
the normal entry `grub-mkconfig` generated, so its root identification and
initrd are whatever Alpine's own tooling chose and only `aios.recovery
nomodeset` is added. The installation fails rather than completing if that
entry is missing or the generated configuration would not read it. See
[offline install parity and audit checkpoints](qa/hardware-install-rollback.md).

- SYSLINUX shows a boot prompt (`PROMPT 1`, three-second timeout) and prints
  the entry list from the config's `SAY` lines; type `recovery` and press
  Enter. GRUB shows the three menu entries and defaults to the first.
- Every entry keeps `console=tty0 console=ttyS0,115200`, and SYSLINUX keeps
  `SERIAL 0 115200`, so a serial console works in all three.
- `nomodeset` is a diagnostic fallback only. It disables kernel mode setting,
  so the recovery desktop is software rendered and slow; use it to diagnose or
  to reach a shell, not as a normal configuration.

## Recovery desktop

With `aios.recovery` on the command line, `aios-session` skips the renderer
probe entirely, exports `LIBGL_ALWAYS_SOFTWARE=1`, and starts Picom on the
XRender backend. That is the same fallback the normal path selects when its
probe reports a software renderer, so the desktop reaches a usable state
instead of a black screen. No vendor-specific `xorg.conf` is installed in any
mode; Xorg keeps its automatic driver detection.

## Console path

If the desktop still cannot start:

- The login profile prints what happened and leaves a shell on tty1. `tty2`
  and `tty3` also offer logins, as does the serial console on `ttyS0`.
- Run `aios-hardware-report --human` for a sanitized summary, or
  `aios-hardware-report` for JSON.
- `~/.local/state/aios/session.log`, `graphics.log`, `compositor.log` and
  `renderer.json` hold the session's own records. `graphics.log` contains the
  raw `glxinfo` output and is never copied into a report.
- Run `startx` to retry the desktop after a change.

## `aios-hardware-report` field contract

`/usr/local/bin/aios-hardware-report` prints one JSON object (default) or a
short text summary (`--human`). `--dns-probe` additionally resolves one fixed
host name; when the resolver tool is missing the probe is reported as not
performed rather than as a failure. It uses fixed sysfs/procfs reads plus a
fixed program allowlist
(`nmcli`, `dmesg`, `getent`) with a five-second timeout each, never a shell,
and accepts no file paths, commands, host names or device names as input.

| Field | Values |
| --- | --- |
| `boot.mode` | `live`, `installed`, `unknown` |
| `boot.recovery`, `boot.install`, `boot.nomodeset` | booleans from fixed `/proc/cmdline` tokens |
| `network.state` | `connected`, `disconnected`, `no_interface`, `missing_module`, `missing_firmware`, `rfkill_hard_block`, `rfkill_soft_block`, `authentication_failure`, `dhcp_failure`, `dns_failure`, `unknown` |
| `network.reason` | fixed reason code for the state |
| `network.evidence` | counts and booleans only: interface kinds/operstates, rfkill blocks, allowlisted module names, PCI network-controller counts, firmware-load failure count, NetworkManager state enums, default-route and nameserver presence |
| `graphics.state` | `accelerated`, `degraded_software`, `unavailable`, `unknown` |
| `graphics.compositor` | `glx`, `xrender`, `unknown` |
| `graphics.recovery_mode` | `normal`, `recovery` |
| `graphics.evidence` | allowlisted DRM driver/module names, DRM card count, `/dev/dri` card and render node counts, node read/write access, the session's recorded renderer selection |
| `cpu.local_inference` | `supported`, `detected`, `required`, `missing`, `reason` |

How the network state is decided, in order: a hard then soft rfkill block; then
with no wireless interface, an unbound PCI network controller (`missing_module`),
an unloaded `cfg80211`/`mac80211` stack (`missing_module`), a firmware load
failure (`missing_firmware`), otherwise `no_interface`; then an unavailable
NetworkManager (`unknown`, or `connected` when a route and resolver exist);
then a Wi-Fi device NetworkManager reports as unavailable together with a
firmware load failure (`missing_firmware`); then a secrets request
(`authentication_failure`); then no active connection (`disconnected`); then a
missing default route (`dhcp_failure`); then a missing resolver or a failed
opt-in probe (`dns_failure`); otherwise `connected`.

The graphics state is *not* re-derived. `aios-session` runs the single renderer
probe at session start and records its decision in
`~/.local/state/aios/renderer.json`; the report reads that file so the report
and the running compositor cannot disagree. Without that file the state is
`unknown` (or `unavailable` when no DRM card node exists), never "accelerated".

### Redaction rules

The report never contains SSIDs, MAC addresses, IP addresses, host names,
serial numbers, saved credentials, raw kernel logs, `nmcli` connection names,
interface names or arbitrary device strings. Values that come from outside
(NetworkManager fields, driver names, operstates, recorded session state) are
mapped through fixed allowlists and become `unknown`/`other` when they do not
match. Firmware failures are counted, never quoted. Output is bounded: lists
are capped at eight entries and the serialized report at 8 KiB, dropping detail
and finally everything but the classifications rather than growing.

A report is a point-in-time snapshot: a connection that is still being set up
can read as `dhcp_failure`. Re-run it before acting.

## Wi-Fi ownership and credential persistence

NetworkManager is the only intended owner of physical Wi-Fi interfaces.
`/etc/NetworkManager/NetworkManager.conf` selects the `wpa_supplicant`
backend, and the `wpa_supplicant` package ships
`/usr/share/dbus-1/system-services/fi.w1.wpa_supplicant1.service`, so
NetworkManager starts and stops the supplicant itself over the system bus.

Alpine's standalone `wpa_supplicant` OpenRC service is therefore **not** in a
runlevel. It is a second owner: its `start_pre` binds `-i<first wireless
interface>` before NetworkManager starts, and it fails with "Could not find a
wireless interface" on machines without a radio. The packages stay installed so
the NetworkManager backend and a manual recovery-console supplicant remain
available.

Credential persistence differs by medium, and the live session does not promise
durability:

| System | Where a connection is stored | Survives reboot |
| --- | --- | --- |
| Live ISO/USB session | `/etc/NetworkManager/system-connections` on the tmpfs root | No |
| Installed system | the same path on the ext4 root | Yes |

Passwords are held by NetworkManager's keyfile plugin with owner-only
permissions. Re-entering Wi-Fi credentials after every live boot is expected
behavior, not a fault.

## Wi-Fi regulatory country

`os_settings` exposes a fixed `wifi_country` operation (read and set) backed by
the no-argument `/usr/local/sbin/aios-regdomain` helper, authorized by one
exact `doas` rule for the desktop user. Values are ISO 3166-1 alpha-2 codes, or
`00` for the world domain. A set applies `iw reg set` and is reported as
successful only after the kernel's own `iw reg get` readback agrees; a code the
wireless regulatory database does not contain is an explicit error.

Persistence follows the same live/installed split: an installed system stores
`options cfg80211 ieee80211_regdom=<CC>` in
`/etc/modprobe.d/aios-cfg80211.conf`, which the kernel applies when `cfg80211`
loads at the next boot. A live session writes nothing and says so. If
`cfg80211` is not loaded or `iw` is missing, the operation reports that it is
unavailable rather than guessing.

## CPU limitation for local inference

The bundled `llama-server`, `llama-cli` and `whisper-cli` are built with
`-DGGML_NATIVE=OFF`, which leaves ggml's explicit instruction-set options
enabled, so the binaries require **SSE4.2, AVX, AVX2, BMI2, F16C and FMA**.
On a CPU without them, those binaries abort with SIGILL.

AIOS now preflights this in one shared helper (`aios.cpu_features`) before any
of them starts:

- the desktop still boots normally; only local inference is refused;
- the chat status, `aios-llm serve`, local speech setup/transcription and the
  local model catalog all report the missing features instead of crashing;
- remote and subscription providers keep working;
- `aios-hardware-report` reports `cpu.local_inference`.

This is a preflight, not a compatible build. A baseline/dispatch inference
build for older CPUs remains unimplemented and is not claimed anywhere.

## Installed-system audit checkpoints

An installed system can record what it currently runs. The root-only
`/usr/local/sbin/aios-checkpoint` helper has three fixed operations: `status`,
`verify` and `stage`.

- `stage` writes `/var/lib/aios/checkpoints/<id>/manifest.json` — kernel
  release and Alpine release family, digests of the kernel image and initramfs,
  a module inventory digest over paths *and* content, and every installed
  package version — plus a copy of that kernel and initramfs beside it.
- Identifiers are derived from the recorded content, so no free-form name can
  be supplied.
- `verify` re-reads those stored copies against their own manifest.
- User, chat, model and profile storage is never read or written, and nothing
  under `/boot` or in any GRUB file is written at all.
- `status` is sanitized: digests, versions and counts only, no device names or
  serial numbers.

**This is an audit record, not a way back.** There is no `activate` and no
`rollback`: restoring a recorded system coherently means restoring the kernel,
its modules, firmware and the Mesa/Xorg stack together, which on an ext4 root
needs a full-system snapshot or a signed package bundle that does not exist
here. Every `status` run says so, reporting `activation_enabled: false`,
`rollback_enabled: false` and `package_acquisition_enabled: false`. The helper
is deliberately absent from `/etc/doas.d/aios.conf`, so no desktop user, skill
or MCP server can reach it. The full contract is in
[offline install parity and audit checkpoints](qa/hardware-install-rollback.md).

## Secure Boot

Secure Boot is **unsupported and must be disabled** to boot AIOS, on the live
medium and on an installed system. The optional modloop signature
(`AIOS_MODLOOP_SIGN`) is the live image's own integrity check and is not Secure
Boot. Signed bootloaders and kernels, key lifecycle, firmware enrollment,
revocation and a recovery path are a separate, unstarted project.

## Physical gates that remain

Everything above is implemented and covered by offline, display-independent
tests. None of it is hardware evidence. The following still require physical
machines and are untested:
- Wi-Fi association on real adapters: WPA2 and WPA3, band coverage, DHCP/DNS,
  a 30-minute transfer, rfkill, reconnect, and five suspend/resume cycles per
  adapter family.
- Real regulatory-domain behavior per adapter, including whether a driver
  honours a country change without a reload.
- Intel, AMD, NVIDIA and hybrid graphics: native resolution, active module and
  renderer, external display hotplug, brightness, lid close, and five
  suspend/resume cycles.
- The recovery entry booted on real firmware (BIOS and UEFI), including
  `nomodeset` output on machines whose ports are wired to a discrete GPU.
- A machine that actually lacks AVX2, to confirm the preflight message rather
  than a SIGILL.
- Ocean and generated-palette screenshots on accelerated and software paths.
- A real installation: no `setup-disk` run, disposable-disk QEMU matrix,
  sacrificial NVMe/SATA install or cold boot with the USB removed has been
  performed. eMMC stays untested and unguarded.
- Coherent update and rollback: not implemented at all. A full-system snapshot
  or signed package bundle that restores kernel, modules, firmware and
  Mesa/Xorg together has to be designed before any of it can be tested.
