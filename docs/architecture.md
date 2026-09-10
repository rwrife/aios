# Implementation notes

Keep Alpine/OpenRC/Xorg/Openbox to build on the existing distribution. The
desktop is a Qt Quick application, with a separate chat window managed by
Openbox. Qt's software renderer is the VM default; waves use Canvas rather than
GPU-only shader effects. The wave animation pauses while chat is open.

The C++ shell launches a Python worker for each operation. Worker requests and
responses are JSON, never shell commands. Each chat also owns a generic Python
tool host on a private AF_UNIX socket. The host registers AIOS built-ins
(`browser` and `application`) and approved stdio MCP tools, returns validated
function schemas, and accepts structured calls only. Model prose and
JSON-looking text are never executed.

`agent.py` owns provider-neutral skill activation, tool filtering, progress,
bounded call parsing, and dispatch. Ordinary chat uses the primary provider.
Only a `remote-preferred` active skill consults the explicit Agent tasks route:
current provider, ChatGPT subscription, or a separately configured remote
OpenAI-compatible service. There is no silent paid fallback. The ChatGPT adapter
supplies the same validated registry as dynamic tools while keeping Codex host
tools disabled.

The application tool stores single-file offline apps in the user's XDG data
directory. Publication records a digest and searchable manifest. Launch
revalidates both, serves a sandboxed wrapper and app over loopback, and starts
Chromium app mode with a temporary profile and restrictive CSP. The application
builder searches this cache before creating anything.

The CLI uses the same Python backend for configuration and streaming. The local
llama-server runs as the desktop user, is bound to loopback, and is owned by the
shell process. This intentionally replaces the proposed boot-time system
service: a model does not need to consume memory before a user selects one.
`aios-llm serve` provides independent CLI use. See
[agentic tools](agentic-tools.md) for skill, MCP, routing, and trust details.

Alpine build tools, target repositories, and aports use one release family.
Container digest and aports/llama.cpp commits are recorded in build.env. Alpine
package repositories can still publish newer revisions: this is a pinned-source,
repeatable workflow, not a byte-reproducible release claim. A release manifest
must record exact package versions. Docker uses a named Linux cache volume to
avoid Windows bind-mount metadata overhead.

The live root uses a tmpfs ceiling of 75% of RAM. The development packages,
voice stack, and bundled Qwen starter exceed Alpine's default half-RAM limit.
The automated boot gate uses 8 GiB, while development launchers default to
16 GiB so every curated model can be downloaded in the RAM-backed live system.
The ceiling does not reserve memory up front and does not apply to installed
ext4 systems. Larger models should use installed or mounted storage.

Models: the registry records source, quantization source, license, immutable
revision URL, length, and checksum. Interrupted downloads restart from zero;
the `.part` file never becomes a selectable model until verification succeeds.

The graphical account can invoke exact reboot/poweroff commands and the guided
installer through scoped doas rules. Model replies are plain text, with selection/copy but no HTML or command
execution. Side effects require advertised, validated structured tool calls.
Remote endpoints require TLS; local loopback servers may use HTTP. HTTP
redirects are rejected for authenticated API requests.
