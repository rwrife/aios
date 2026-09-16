# Stage 3 QA record: predictable Wi-Fi, graphics startup and recovery

Scope: the software-addressable part of phase 3 in
[docs/plans/physical-hardware-enablement.md](../plans/physical-hardware-enablement.md)
(issue #100). Every change below is implemented and covered by offline,
display-independent tests. **No physical machine has been tested and no
hardware status is promoted by this stage.** The operator-facing behavior lives
in [docs/hardware-recovery.md](../hardware-recovery.md).

## What changed

### Network ownership

`distro/alpine/apkovl/genapkovl-aios.sh` no longer adds `wpa_supplicant` to the
`default` runlevel. Evidence for the duplicate-ownership finding, all from the
shipped artifacts rather than assumption:

1. `/etc/NetworkManager/NetworkManager.conf` sets `wifi.backend=wpa_supplicant`,
   so NetworkManager owns the supplicant's lifecycle.
2. Alpine's `wpa_supplicant` package installs
   `/usr/share/dbus-1/system-services/fi.w1.wpa_supplicant1.service`, so the
   system bus activates the supplicant on NetworkManager's request. No runlevel
   entry is needed for Wi-Fi to work.
3. Alpine's `wpa_supplicant` OpenRC script claims interfaces independently: its
   `start_pre` appends `-i<first wireless interface>` found in
   `/sys/class/net`, and it runs before NetworkManager (`before net`;
   NetworkManager `provide net`).
4. The same `start_pre` calls `eerror "Could not find a wireless interface"` on
   a machine with no radio, so the stock image logged a service failure at every
   boot of a wired-only or virtual machine.

Only the runlevel entry was removed. `wpa_supplicant` and
`wpa_supplicant-openrc` stay in `world.base`, so the NetworkManager backend and
a manual recovery-console supplicant are still available, and Wi-Fi support is
unchanged. `tests/test_hardware_startup.py` runs the real generator and asserts
the resulting runlevel map.

### Bounded hardware diagnostics

`apps/aios/hardware_diagnostics.py` with the
`distro/alpine/overlay/usr/local/bin/aios-hardware-report` entry point
classifies the nine required network states plus `unknown`, and classifies
graphics as `accelerated`, `degraded_software`, `unavailable` or `unknown`. It
uses fixed sysfs/procfs reads and a three-program allowlist with per-command
timeouts, emits fixed enums, booleans and counts only, and bounds its own
output. The field contract and redaction rules are documented in
[docs/hardware-recovery.md](../hardware-recovery.md).

Renderer and compositor selection is read from the session's own recorded
decision (`~/.local/state/aios/renderer.json`), not re-derived, so the report
cannot disagree with the running desktop.

### Recovery boot and session

`distro/alpine/profiles/mkimg.aios.sh` emits three entries from one shared
list for both bootloaders: `live` (default), `install`, and `recovery`
(`aios.recovery nomodeset`). Serial and VGA console arguments are unchanged for
every entry, and GRUB terminal redirection was deliberately **not** added:
the kernel console arguments already cover serial access, and a `serial`
command that fails on firmware without a UART risks a blank menu on hardware
this stage cannot test.

`aios-session` reads `aios.recovery`, skips the renderer probe, forces
`LIBGL_ALWAYS_SOFTWARE=1` and the XRender compositor, and records the result.
The normal path keeps its existing probe, GLX/XRender selection and
damage-only repaint mitigation unchanged. `/etc/profile.d/aios.sh` and the
session's failure branch print the console/diagnostics instructions. No vendor
Xorg configuration was added.

### CPU preflight

`apps/aios/cpu_features.py` is the single helper. `aios-llm serve`,
`voice.transcribe`, `voice.setup_local_voice`, `local_models.install`/
`list_models` and the desktop shell's `startLocal()` all consult it, the last
through the `local_inference` field already carried by the local model
inventory in the `loaded` payload, so no new subprocess or duplicated CPU
detection was introduced. Remote and subscription providers are untouched.

### Wi-Fi regulatory country

`os_settings` gained the fixed `wifi_country` read/set operation backed by
`/usr/local/sbin/aios-regdomain` (`apps/aios/regulatory.py` and
`regulatory_service.py`), following the existing `aios-clock` pattern: one
exact `doas` rule, no arguments, isolated Python, validation before the helper
runs, kernel readback before reporting success, and explicit
unavailable/error results. `apps/skills/os-control/SKILL.md`,
`docs/agentic-tools.md` and `docs/settings.md` were updated with it.

## Tests

```sh
bash scripts/test.sh          # whole offline suite, including the files below
PYTHONPATH=apps python3 -m unittest discover -s tests -v \
  -p 'test_hardware_startup.py'        # runlevels, boot entries, recovery session
PYTHONPATH=apps python3 -m unittest discover -s tests -v \
  -p 'test_hardware_diagnostics.py'    # classifiers, redaction, bounds
PYTHONPATH=apps python3 -m unittest discover -s tests -v \
  -p 'test_cpu_features.py'            # preflight and launch gating
PYTHONPATH=apps python3 -m unittest discover -s tests -v \
  -p 'test_wifi_regulatory.py'         # country validation, helper, persistence
```

`test_hardware_startup.py` executes the real apkovl generator, the real image
profile's bootloader generators, and the real `aios-session` script with every
external program replaced by a stub, so it needs no display server, GPU or
network. The diagnostics tests inject a fake sysfs tree and a fake command
runner. The CPU tests inject `/proc/cpuinfo` fixtures.

## Not done in this stage

- No ISO was built and no VM or physical boot was run for this change.
- `scripts/test-boot.py` was not extended with a recovery-entry QEMU case;
  that needs an ISO build and belongs with the VM regression gate.
- No baseline/dispatch inference build for pre-AVX2 CPUs.
- No Settings UI page for the Wi-Fi country; the trusted operation is exposed
  through `os_settings` and the CLI only.

## Physical evidence still required

All of the following are untested. They are the phase-3 exit criteria that
software alone cannot satisfy.

| Gate | Required evidence |
| --- | --- |
| Wi-Fi per adapter family | Scan, WPA2/WPA3 association, DHCP, DNS, 30-minute transfer, rfkill on/off, reconnect, five suspend/resume cycles, on at least one Intel, one Qualcomm/Atheros and one MediaTek adapter |
| Credential persistence | Saved connection reused after reboot on an installed disk; live session confirmed ephemeral |
| Regulatory country | `wifi_country` set and readback on a real adapter, and the installed-system module option honoured after reboot |
| Failure classification | A real missing-firmware device, a real rfkill block, a wrong passphrase, a DHCP-less network and a resolver-less network each producing the documented distinct state |
| Graphics | Intel iGPU, AMD APU/discrete, selected NVIDIA generations and one hybrid laptop: active module, renderer, native resolution, GLX compositor, Qt windows and browser, hotplug, 30-minute use, five suspend/resume cycles |
| Recovery boot | The recovery entry selected from the boot prompt/menu on BIOS and UEFI firmware, reaching either a software desktop or the documented console, including a machine whose displays are wired to a discrete GPU |
| CPU floor | A pre-AVX2 machine reaching the desktop and receiving the preflight message instead of SIGILL |
| Visual | Ocean and one generated palette on both accelerated and software-rendered paths |

Until that evidence exists, every entry in
[docs/qa/hardware-coverage.json](hardware-coverage.json) stays `untested`.
