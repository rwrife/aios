# AIOS 0.1 behavior

This specification supersedes the terminal-only requirements in the older plans.

AIOS boots into Alpine/OpenRC, Xorg, Openbox, and an ordinary local `aios` user.
The desktop has an original slow wave animation, one prominent Chat launcher,
and understated terminal and power icons. Chat opens one centered window;
closing it returns to the desktop. Other development applications remain usable.

Chat supports a local GGUF model through llama.cpp or a remote OpenAI-compatible
HTTPS endpoint. Replies stream as plain text, can be stopped, and never execute
commands. Model configuration and the current conversation live under the user's
XDG config/data directories. API keys are stored in a mode-0600 config file and
are not returned to the UI. Autologin assumes a personally controlled machine.

The default ISO includes a C/C++ compiler, CMake, Ninja, pkg-config, Git, GDB,
Python, Qt development libraries, a desktop example, and llama-cli/llama-server.
Model weights are downloaded or imported separately. The optional small starter
model is for trying local chat; it is not a capable coding assistant.

First target: x86_64 QEMU. BIOS and UEFI are separate validation gates. VMware
and bare metal are not supported claims until tested. Start with 4 GiB RAM and
32 GiB disk for development; larger models require additional RAM/storage.
These are initial test settings, not measured minimum requirements.

Release gates: clean image build, offline boot, ordinary-user graphical session,
local and remote real responses, recoverable failures, example app built offline,
installation to an explicitly selected blank virtual disk, persisted state after
reboot, working power controls, release checksums and source/package manifests.

Live mode is ephemeral. Installed mode must preserve user data. Never imply that
a live download or chat history survives reboot unless persistence is enabled.
