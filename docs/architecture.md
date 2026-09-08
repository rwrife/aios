# Implementation notes

Keep Alpine/OpenRC/Xorg/Openbox to build on the existing distribution. The
desktop is a Qt Quick application, with a separate chat window managed by
Openbox. Qt's software renderer is the VM default; waves use Canvas rather than
GPU-only shader effects. The wave animation pauses while chat is open.

The C++ shell launches a Python worker for each operation. Worker requests and
responses are JSON, never shell commands. The CLI uses the same Python backend
for configuration and streaming. The local llama-server runs as the desktop
user, is bound to loopback, and is owned by the shell process. This intentionally
replaces the proposed boot-time system service: a model does not need to consume
memory before a user selects one. `aios-llm serve` provides independent CLI use.

Alpine build tools, target repositories, and aports use one release family.
Container digest and aports/llama.cpp commits are recorded in build.env. Alpine
package repositories can still publish newer revisions: this is a pinned-source,
repeatable workflow, not a byte-reproducible release claim. A release manifest
must record exact package versions. Docker uses a named Linux cache volume to
avoid Windows bind-mount metadata overhead.

The live root uses a tmpfs ceiling of 75% of RAM. The default development and
voice packages occupy roughly 2 GiB, exceeding Alpine's default half-RAM limit
on a 4 GiB guest. The ceiling does not reserve memory up front and does not apply
to installed ext4 systems. Large models should use installed or mounted storage.

Models: the registry records source, quantization source, license, immutable
revision URL, length, and checksum. Interrupted downloads restart from zero;
the `.part` file never becomes a selectable model until verification succeeds.

The graphical account can invoke exact reboot/poweroff commands and the guided
installer through scoped doas rules. Model replies are plain text, with selection/copy but no HTML or command
execution. Remote endpoints require TLS; local loopback servers may use HTTP.
HTTP redirects are rejected for authenticated API requests.
