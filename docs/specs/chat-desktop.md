# AIOS 0.1 behavior

This specification supersedes the terminal-only requirements in the older plans.

AIOS boots into Alpine/OpenRC, Xorg, Openbox, and an ordinary local `aios` user.
The desktop has an original slow wave animation, one prominent Chat launcher,
and understated terminal and power icons. The launcher restores chat windows
that are out of sight (minimized by their button or hidden by the window
manager), from most recently out of sight to oldest. When none are out of
sight, it creates a new centered chat window and independent conversation. Existing
windows retain their own drafts, attachments and generation state. Closing one
stops only that session. Other development applications remain usable.

Chat supports a local GGUF model through llama.cpp or a remote OpenAI-compatible
HTTPS endpoint. Tool-capable chat can discover Agent Skills, use built-in
browser/application tools, and call allowlisted stdio MCP tools. Requests such
as "I need a calculator" activate the application builder, search the persistent
app cache first, then prefer the advertised trusted, compiled Qt calculator
template when `aios-app-host` is available. Native cache entries are
manifest-only, and the model never generates or compiles C++ or QML. If the
request has no advertised native template, the builder creates one sandboxed
offline `index.html` instead. Native launch is verified after its first frame;
web launch is verified after the first `/app` request. A failed handshake returns
`launched: false`, which must not be reported as success. Skills and MCP servers
are installed/configured in the user's XDG directories and remain ephemeral in
an unpersisted live session.

Replies stream as plain text and can be stopped. Only validated structured calls
to tools advertised for that turn execute; prose, code blocks, JSON-looking text,
and arbitrary command strings never execute. Agent tasks can stay on the primary
model or cross an explicit user-configured boundary to ChatGPT or a separate
remote endpoint, with no silent paid fallback. Model configuration and the
current conversation live under the user's XDG config/data directories.
Conversations use unique files under
`~/.local/share/aios/conversations/`; a new window never loads an old session.
API keys are stored in a mode-0600 config file and
are not returned to the UI. Autologin assumes a personally controlled machine.

The composer has discreet attachment and waveform/Voice controls. Model and voice
configuration lives behind an ellipsis menu. Text/source files and text-bearing
PDFs can be attached; image understanding and scanned PDFs are not implemented.
The Voice control illuminates while recording, driven by the actual recording
state. Clicking it and the programmatic `setVoiceActive(bool)` entry point share
the same behavior. Recordings are limited to 60 seconds and can be discarded.
Transcriptions populate the composer for review; they are not automatically sent.
Spoken replies are opt-in per reply and are identified as synthesized speech.

Remote voice is the initial settings default and requires a separately configured
compatible speech endpoint and credential. On-device recognition uses a downloaded
Whisper tiny.en model and local synthesis uses eSpeak NG. Neither route is enabled
by pretending a microphone or voice service is available.

The default ISO includes a C/C++ compiler, CMake, Ninja, pkg-config, Git, GDB,
Python, Qt development libraries, a desktop example, and llama-cli/llama-server.
Model weights are downloaded or imported separately. The optional small starter
model is for trying local chat; it is not a capable coding assistant.

First target: x86_64 QEMU. BIOS and UEFI are separate validation gates. VMware
and bare metal are not supported claims until tested. Development launchers use
16 GiB RAM, four CPUs, and a 64 GiB disk so the RAM-backed live environment can
run and download every curated model. These are test settings, not measured
minimum requirements.

Release gates: clean image build, offline boot, ordinary-user graphical session,
local and remote real responses, recoverable failures, example app built offline,
installation to an explicitly selected blank virtual disk, persisted state after
reboot, working power controls, release checksums and source/package manifests.

Live mode is ephemeral. Installed mode must preserve user data. Never imply that
a live download, generated application, user skill, MCP configuration, or chat
history survives reboot unless persistence is enabled.

See [agentic tools](../agentic-tools.md) for the supported skill and MCP subsets,
application sandbox, provider routing, and trust boundaries.
