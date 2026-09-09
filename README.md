# aios (AI OS)

AIOS is a small chat-first Linux desktop: an ambient wave background, one chat
launcher, and understated settings/terminal/power controls. It includes native desktop
development tools and local/remote LLM command-line tools by default.

The chat desktop is under active development. See
[`docs/specs/chat-desktop.md`](docs/specs/chat-desktop.md) for the current target
and [`docs/architecture.md`](docs/architecture.md) for implementation decisions.
See `docs/qa/implementation-status.md` for executed checks and remaining release gates.

## Try the development image

On Linux with Docker: `bash scripts/build.sh`, then `bash scripts/run.sh`.
On Windows, run the commands inside WSL with a working Linux Docker daemon.
Build caches use the Docker volume `aios-build-cache`; images are written to
`distro/alpine/out`. The initial VM setting is 4 GiB RAM.

The Settings button beside Terminal and Power opens AI models, Sound, Camera,
Network & Wi-Fi, Display, and Appearance. See [desktop settings](docs/settings.md).
AI models also supports [ChatGPT subscription sign-in](docs/chatgpt-subscription.md),
including device-code login from outside a VM, model selection, and usage status.

The softly animated blob at the bottom center restores the most recently
minimized chat, or opens a new chat window and conversation when none are
minimized. SmolLM2 135M is bundled and starts automatically for offline chat on
first boot. Use the ellipsis menu
to switch back to the starter model, import a GGUF, or configure a compatible
remote endpoint. The composer keeps attachments and Voice understated. Voice
lights up while recording, and transcription fills the draft before sending.
Remote speech and on-device speech are supported; see
[`voice and sessions`](docs/voice-and-sessions.md). Live sessions are ephemeral.
Chromium is available through chat with a tool-capable model, without a desktop
browser icon. See [browser tools](docs/browser.md). The starter model has limited
reasoning ability and is not a reliable browser agent; larger models can be imported.
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
