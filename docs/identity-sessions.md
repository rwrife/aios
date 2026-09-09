# Ambient identity and sessions — experimental implementation

This branch is **not a completed implementation of the ambient multi-user plan**.
It supplies an opt-in broker, deterministic simulator and initial shell surfaces.
The ordinary ISO still starts the original single-user desktop. Do not enroll
real biometrics or put real secrets into the simulator.

## Implemented behavior

- Recognition produces a candidate; it does not activate a workspace. Conflicts,
  missing liveness, multiple active people and stale evidence prevent activation.
- An explicit personal request binds a durable work session to an enrolled UUID.
  Another candidate cannot retarget the session. A new lease is issued on resume.
- Anonymous calculator/editor/terminal requests use an ephemeral context. The
  simulator records launches; it does not execute applications.
- Separate SQLite journals store titles, messages, lifecycle events and allowlisted
  restoration manifests. Reopening a journal recovers active tasks as suspended.
- Recent sessions can be browsed without creating a new work session. Bounded
  conversation pages and searchable summaries survive resume. The document panel
  atomically saves UTF-8 text/Markdown inside the encrypted workspace; directory
  traversal, symlinks and special files cannot redirect root reads or writes.
- Sandboxed processes receive a fixed UID/owner/scope descriptor. Configuration
  and data helpers use that explicit workspace and reject malformed descriptors
  instead of falling back to an inherited desktop home.
- Presence loss revokes capabilities and shields content; the longer timeout stops
  applications, closes the journal and releases storage. Cleanup failures block
  further activation rather than switching users with a live mount.
- PIN verifiers use salted scrypt. Persistent attempt counters implement backoff
  and lockout. Opaque capabilities bind owner, lease, operation, resource, expiry
  and single-use status. PIN success does not grant other capabilities.
- AES-GCM encrypts template/credential records with record-name authentication.
  Recovery secrets rotate on use. No model or template download occurs.
- A GitHub profile adapter uses a credential at a fixed HTTPS destination, refuses
  redirects and returns only the login. It is not exposed as an LLM tool.
- Optional YuNet/SFace and waveform speaker-ONNX adapters validate model digests
  before inference. Their output is not a calibrated production recognition system.

## Run the non-executing simulator

Use Linux with Python 3.10+ and `cryptography` installed. From the repository:

```sh
export PYTHONPATH="$PWD/apps"
python3 -m aios.sessiond --simulate "$HOME/.local/state/aios-identity-demo"
```

First start asks for a PIN and creates two fictional identities, `user-a` and
`user-b`. The same PIN is used for these two test identities only. No raw samples
are captured. Encrypted simulator records and its development key live together;
this is not protection against the host user or a stolen disk.

In a second terminal, launch a built shell:

```sh
AIOS_SESSION_SOCKET="$HOME/.local/state/aios-identity-demo/session.sock" aios-shell
```

The identity panel can select `unknown`, `user-a`, `user-b`, `absent` and
`conflict`. Calculator stays anonymous even after selecting a known identity.
Start creates personal work; Editor records a `Resume.txt` restoration manifest.
Recent lists only the current owner's sessions. Protected account asks for the
demo PIN and returns a mocked account name. Switching directly to the other
identity shields the old session and does not change its owner. Suspend explicitly
returns to anonymous mode. Evidence is refreshed only while the demo shell runs.

Experimental mode disables the original chat, settings and configuration-loading entry points because
their workers still use the desktop UID. It does **not** claim they have been
migrated to the broker. The shell's ordinary startup/configuration code remains
single-user when experimental mode is off. No PIN is routed through chat, command arguments or application logs.
The PIN's transient Qt/Python copies are not guaranteed to be securely zeroized.

The JSON client can also exercise the service:

```sh
printf '%s\n' '{"action":"status"}' |
  python3 -m aios.session_client "$HOME/.local/state/aios-identity-demo/session.sock"
```

## Linux adapter and deployment gates

On an Alpine target with the optional identity dependencies installed, root can
run `aios-identity-setup`. It creates dedicated identity/anonymous service accounts,
the broker group, root-private state and a random master key. Existing state is
never overwritten. It does **not** enable the service or validate a display.

After the display prerequisite is satisfied, **Create profile** provisions a new
LUKS2 image and unused UID in the reserved 30000–39999 range. Keep that range
reserved for AIOS workspaces. Only exclusively created UUID directories and new
regular image files are eligible for formatting; existing devices/images are
never accepted. Failed allocations remain root-private for administrator review.
Initial image creation temporarily occupies the serialized broker while no
personal workspace is active; the shell shows progress and pauses status polling.
Enrollment interruption/reconciliation still needs production hardening.

**Unlock with PIN** accepts the profile name and rate-limited PIN/passphrase,
opens Recent without a throwaway session, and grants a two-minute local fallback
lease. It requires no camera and does not collect biometric templates. It locks
at expiry, does not extend on Start/Resume, and conflicts revoke it immediately.
This is explicit PIN verification, not a claim of continued sensor presence.
Protected resources still require their own operation-scoped challenge. Save the
one-time recovery code when creating the profile. These controls are also usable
with fictional profiles in the simulator, without kernel-isolation claims.

`aios.isolation.LinuxIsolation` is a root-only adapter for broker-provisioned
LUKS volumes, fixed UID mappings, cgroup v2 scopes and bubblewrap launches. Apps
receive only the artifact directory, system binaries and a private Wayland socket;
the journal, broker socket, user homes, host network, X11 socket and credentials
are not in the sandbox. Application types and arguments are allowlisted.

The adapter passes headless tests in a disposable Alpine container on the WSL
Linux kernel: real UID permission denial, namespace filesystem/environment/network
isolation, cgroup limits and forked-process teardown, LUKS locking/reopening, and
stale process/mount recovery after broker restart. This is **not an Alpine VM boot
or display-isolation test**. The adapter is not enabled at boot.
`world.identity` is an optional dependency list, not part of the default
ISO; package availability and size still require validation. The OpenRC script
requires an administrator-created `aios-broker` group and configuration. There
is intentionally no sample with a real device, UID or disk-formatting command.

Production configuration is root-owned JSON at `/etc/aios/sessiond.json`:

| Field | Meaning |
| --- | --- |
| `state`, `master_key` | Root-private encrypted record directory and 32-byte key file |
| `runtime` | Root-owned workspace mount directory; traversal-only to reach UID-owned artifacts |
| `socket`, `socket_gid` | Broker socket in a root-owned, group-traversable directory |
| `shell_uid`, `identity_uid`, `anonymous_uid` | Distinct unprivileged accounts, distinct from all personal UIDs |
| `principals` | UUID to `{uid, mount, device, key_file}` mapping; new allocations are persisted atomically |
| `volume_store`, `workspace_size_mib` | Root-private directory for new image allocations; 512 MiB default |
| `wayland_sockets` | UID to separately isolated compositor socket mapping |
| `display_isolation_validated` | Defaults false; blocks personal APIs and GUI launches |

Setting the validation flag is **not** display isolation. A separate compositor,
trusted shell/input channel and completed isolation tests are prerequisites. Never
point it at a shared desktop or nested compositor controlled by another user.
The existing X11 desktop is insufficient, including for secure PIN input.

### Embedded private display

Build the shell with `-DAIOS_EMBEDDED_DISPLAY=ON` and Qt Wayland Compositor
development dependencies to exercise the new path. This optional Qt module is
available under GPLv3 or a commercial license; the default shell build remains
unchanged. Setup initializes `embedded_display: true`. The broker creates a new
UID-private Wayland listener for each lease and passes its listening descriptor
only to the registered shell process. Applications receive that socket inside
their mount namespace. Application surfaces are clipped to the shell's application
area, and PIN UI uses an in-window overlay. Keyboard focus is explicitly removed
from application surfaces while trusted input is active. Screen-copy and virtual
input extensions are not instantiated, and the shell clipboard is not bridged.

Only a shell reporting the direct `eglfs` platform can enable personal APIs in
embedded mode, and the administrator's display-validation gate must also pass.
X11, WSLg/nested Wayland and offscreen runs remain anonymous even if the validation
flag was accidentally set. The declaration comes from the dedicated trusted shell
UID; application UIDs cannot register or request listener descriptors. A changed
shell process or a lost shell heartbeat suspends existing work. This requires a
clean appliance startup that does not run untrusted programs under the shell UID;
it is not a security upgrade for an already-running shared desktop.

`bash scripts/test-identity-display.sh` builds an isolated test container and checks
real Wayland clients, cross-UID connection denial, listener replacement, rendered
pixels from a sandboxed synthetic app, and PIN keystroke/clipboard separation.
The synthetic app replaces the calculator only inside that disposable container.
No test harness is installed by the shell build. These offscreen checks do not
validate DRM/input hardware, VT transitions, recovery consoles or device failure.

## Remaining work by plan phase

| Phase | Status and remaining implementation |
| --- | --- |
| 0 | Initial ADR/threat model and simulator implemented. Schemas and security review need expansion. |
| 1 | Broker, Linux adapter and explicit process storage context implemented; headless checks pass. Routing chat, browser, model workers and settings through broker-owned scopes remains. |
| 2 | New encrypted-volume provisioning, PIN-only profile UI, durable history paging, summary search and safe text-document save/resume implemented. Artifact claim, broader application adapters and interrupted-enrollment reconciliation remain. |
| 3 | Face/model/tracker adapters implemented. Continuous identity daemon, consent/enrollment UI, calibrated quality thresholds and liveness hardware integration remain. |
| 4 | Speaker encoder interface and conservative fusion implemented. Microphone capture/VAD, lip synchronization, direction-of-arrival and adversarial attribution testing remain. |
| 5 | Scoped capability/PIN/recovery logic and restricted GitHub adapter implemented. Separate secrets process, provisioning UI, transaction UI, protected configuration and TPM integration remain. |
| 6 | Embedded per-lease Wayland prototype, clipped application surfaces, in-window PIN overlay and protocol/rendering/input checks implemented. Protected appliance startup, physical display/input validation, deletion workflow and accessibility remain. |

No claim is made that the seven-phase definition of done has been achieved.

## Verification

Run `bash scripts/test.sh` on Linux. Tests include durable session restoration,
ownership denial, anonymous idle cleanup, conflict/stale evidence, liveness gating,
PIN backoff/lockout, scope mismatch, single use, timeout/revocation, malformed
protocol, actual Unix socket framing/permissions/deadlines, model integrity and
ciphertext tamper/name-substitution detection. The encryption test skips if the
optional `cryptography` package is missing. Simulator tests do not demonstrate
kernel, storage or display isolation.

Run `bash scripts/test-identity-linux.sh` for the separate real-kernel test suite.
It starts a privileged disposable Alpine container with a private cgroup namespace
and a read-only repository mount. Only newly created temporary regular files are
formatted as encrypted volumes; no supplied path or pre-existing disk is formatted.
The tests verify anonymous namespace isolation, separate-UID filesystem denial,
cgroup limits/teardown, LUKS lock/resume and recovery of stale scopes and mounts.
The `identity-isolation` CI job runs these checks independently of the simulator.

See [WSL webcam development](wsl-webcam.md) for USB passthrough and the bounded
capture diagnostic. Webcam enumeration starts no capture; the explicit probe
reads and discards frames and saves no media.

Required release tests remain: two-UID filesystem/IPC attacks; fork/daemon escape;
broker crash/reboot and busy-volume recovery; full compositor input/capture and
clipboard isolation; PIN focus integrity; camera/microphone unplug; printed face,
video, recorded/synthesized speech; realistic lighting and multiple speakers;
disk-full/power-loss enrollment; recovery abuse; secure deletion and accessible
fallbacks. Record hardware, calibration, latency and false acceptance/rejection
measurements locally; do not upload telemetry automatically.

References for the optional adapters: [OpenCV face recognition](https://docs.opencv.org/4.13.0/d0/dd4/tutorial_dnn_face.html),
[bubblewrap](https://github.com/containers/bubblewrap),
[cryptsetup open](https://man7.org/linux/man-pages/man8/cryptsetup-open.8.html).
