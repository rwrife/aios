# aios (AI OS)

AIOS is a small chat-first Linux desktop: an ambient wave background, one chat
launcher, and understated settings/terminal/power controls. It includes native desktop
development tools and local/remote LLM command-line tools by default.

The chat desktop is under active development. See
[`docs/specs/chat-desktop.md`](docs/specs/chat-desktop.md) for the current target
and [`docs/architecture.md`](docs/architecture.md) for implementation decisions.
See `docs/qa/implementation-status.md` for executed checks and remaining release gates.
See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup on Windows,
Linux, and macOS.

## Try the development image

Build on Linux with Docker:

```sh
bash scripts/build.sh
```

On Windows, use PowerShell with WSL2 and a working Linux Docker daemon:

```powershell
.\scripts\build.ps1
# Choose a different WSL distribution:
.\scripts\build.ps1 -Distro Debian
```

Build caches use the Docker volume `aios-build-cache`; images are written to
`distro/alpine/out`.

## Download and flash a bootable ISO

The [Build bootable ISO workflow](https://github.com/rwrife/aios/actions/workflows/build-iso.yml)
builds the pinned Alpine image, verifies its checksum and hybrid USB metadata,
then boots it offline with both BIOS and UEFI firmware before publishing it.
Manual workflow runs provide a 30-day Actions artifact. Pushing a `v*` tag also
creates or updates a GitHub Release with the ISO, `SHA256SUMS`, package manifest
and pinned build inputs.

The x86_64 ISO is a hybrid image that can be written directly to a thumb drive.
Verify its checksum first, then use a raw-image writer such as Rufus or
balenaEtcher on Windows, or `dd` on Linux:

```sh
sha256sum -c SHA256SUMS
sudo dd if=alpine-aios-*-x86_64.iso of=/dev/sdX bs=4M status=progress conv=fsync
```

Replace `/dev/sdX` with the whole USB device, not a partition. This erases that
device. Boot the result on a 64-bit x86 machine in BIOS or UEFI mode. Secure Boot
must currently be disabled; physical hardware compatibility is not yet certified.

Launch with QEMU installed on the host:

```sh
# Linux (Debian/Ubuntu: sudo apt install qemu-system-x86 qemu-utils qemu-system-gui)
bash scripts/run.sh
# Or choose an ISO:
bash scripts/run.sh /path/to/aios-x86_64.iso
```

```powershell
# Windows PowerShell: WSL2 Ubuntu + WSLg, with the Linux QEMU packages above
powershell -ExecutionPolicy Bypass -File .\scripts\run.ps1
# Or choose an ISO:
.\scripts\run.ps1 C:\images\aios-x86_64.iso
# Choose another WSL distribution, or use the slower native Windows fallback:
.\scripts\run.ps1 -Distro Ubuntu
.\scripts\run.ps1 -Native
```

Both launchers select the newest ISO in `distro/alpine/out` when no path is given.
The whole OS runs inside one resizable QEMU window, with fullscreen disabled at
startup. Chat, settings, and other guest apps stay inside that VM. Use this flow
for interactive OS previews; `preview-chat.sh` is only an isolated UI development
tool. Camera passthrough setup is documented in [the webcam guide](docs/wsl-webcam.md#camera-inside-the-windowed-vm).
They provide NAT networking (outbound internet through the host, with guest DHCP),
Intel HD Audio speakers and microphone, 4 GiB RAM, two CPUs, and a persistent
32 GiB sparse disk at `.tmp-aios-live.qcow2`. Existing disks are reused unchanged.
Networking needs no bridge or administrator privileges; the guest sees wired
Ethernet even when the host uses Wi-Fi. Inbound connections are not forwarded.
Linux uses PulseAudio (including PipeWire's PulseAudio compatibility service);
Windows uses WSLg's PulseAudio connection. The optional `-Native` launcher uses
DirectSound and requires Windows QEMU on PATH or in `C:\Program Files\qemu`.
Allow host microphone access for voice input.
These devices use [QEMU's audio and network options](https://www.qemu.org/docs/master/system/invocation.html).

Set `AIOS_VM_MEM_MB`, `AIOS_VM_CPUS`, `AIOS_VM_DISK`, or `AIOS_VM_DISK_SIZE` to
override defaults (disk size applies only to new disks). `AIOS_QEMU_AUDIO` selects
another QEMU audio backend, such as `alsa` on Linux or `none` for a silent VM.
Linux and WSL use KVM when `/dev/kvm` is accessible. In WSL, add your Linux user
to the `kvm` group (`sudo usermod -aG kvm "$USER"`) and open a new WSL session.
Without KVM, software emulation is substantially slower. The optional native
Windows fallback uses TCG; `AIOS_QEMU_ACCEL` overrides its accelerator.
Preview without
launching or creating a disk with
`DRY_RUN=1 bash scripts/run.sh /path/to/image.iso` or
`.\scripts\run.ps1 C:\images\image.iso -DryRun`.

For boot diagnostics, `AIOS_QEMU_SERIAL` accepts a QEMU serial destination
(for WSL, use a Linux path, such as `file:/tmp/aios-boot.log`). If WSL audio
devices appear but playback hangs, check `timeout 5 pactl list short sinks`
inside WSL. A timeout there indicates a WSLg audio-service problem, outside the
guest; see [the upstream report](https://github.com/microsoft/wslg/issues/1482).

The Settings button beside Terminal and Power opens AI models, Sound, Camera,
Network & Wi-Fi, Display, and Appearance. See [desktop settings](docs/settings.md).
On a desktop user's first launch, an optional setup wizard offers network setup,
local profile and ChatGPT account sign-in, a private camera check, and local model downloads. Every
step can be skipped, the wizard can be closed at any time, and **Run setup wizard**
in Settings opens it again.
AI models also supports [ChatGPT subscription sign-in](docs/chatgpt-subscription.md),
including device-code login from outside a VM, model selection, and usage status.

The softly animated blob at the bottom center restores the most recently
minimized chat, or opens a new chat window and conversation when none are
minimized. Qwen3 0.6B Q4_K_M is bundled and starts automatically in
non-thinking mode for offline chat on first boot. Use the ellipsis menu
to switch back to the starter model, import a GGUF, or configure a compatible
remote endpoint. The composer keeps attachments and Voice understated. Voice
lights up while recording, and transcription fills the draft before sending.
Remote speech and on-device speech are supported; see
[`voice and sessions`](docs/voice-and-sessions.md). Live sessions are ephemeral.
Chromium is available through chat with a tool-capable model, without a desktop
browser icon. See [browser tools](docs/browser.md). The starter supports simple
tool bootstrap but is not a reliable browser agent; critical setup controls call
deterministic backend actions instead of relying on the model. Settings and setup offer
[three stronger Qwen3 models](docs/local-models.md) with tool capabilities,
RAM guidance, and disk-space checks; custom GGUF models can also be imported.
The current CPU inference build targets x86_64 with AVX2. Start with 4 GiB RAM
and a 32 GiB disposable disk; larger models need more memory and storage.
Secure Boot and physical hardware have not yet been validated.

CLI examples:

```sh
aios-llm setup-local
aios-llm serve
# In another terminal:
aios-llm chat 'Hello'
aios-llm configure --mode remote --url https://your-provider.example/v1 --model MODEL --ask-key
aios-new-app hello
cmake -S hello -B hello/build -G Ninja
cmake --build hello/build
./hello/build/hello
```

Installer: `doas /usr/local/sbin/aios-install`. It requires selecting an unused
whole disk and typing its exact erase confirmation. Use a disposable VM disk
until the release QA matrix has been completed.

Run backend and source validation with `bash scripts/test.sh`. Image CI is
available through the Validate workflow's manual dispatch.
