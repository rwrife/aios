# aios (AI OS)

AIOS is a small chat-first Linux desktop: an ambient wave background, one chat
launcher, and understated terminal/power controls. It includes native desktop
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

Click Chat, choose Model, then download the small starter model, import a GGUF,
or configure an OpenAI-compatible remote endpoint. Live sessions are ephemeral.
The starter model has limited reasoning ability; larger models can be imported.
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
