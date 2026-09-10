# Implementation evidence — 2026-09-08

This is a development candidate, not a hardware-certified release.

## Executed

- Built an x86_64 Alpine 3.23 ISO with the native Qt desktop, developer packages,
  and pinned llama.cpp CLI/server. The current voice image is approximately 1.13 GiB.
- The `20260908-r5` ISO booted with **no network device**, using both QEMU BIOS and
  OVMF UEFI. The automated check found `aios-shell` owned by `aios` and compiled
  the Qt desktop example from source using the default packages.
- An interactive QEMU session rendered the animated launcher and centered chat
  using the Qt software renderer. Closing and reopening chat preserved the view.
- Downloaded the checksum-pinned 101 MiB SmolLM2 starter model and received real
  CPU responses in the chat UI and CLI. The model is intentionally small and has
  limited reasoning ability.
- Configured the same chat UI for a separate llama.cpp server outside the VM.
  Received a real streaming response over HTTPS with API-key authentication.
  A temporary test CA was installed only in that disposable guest; the production
  image retains ordinary certificate verification. No commercial provider was tested.
- Installed a BIOS system on a disposable 32 GiB virtual disk, removed its ISO,
  and booted into the desktop. Configuration, model, conversation and a compiled
  desktop project survived. This test required manual fixes subsequently included
  in source, so it does not establish a pristine final-image installer pass.
- Eight Python backend tests pass, covering real HTTP streaming with fragmented
  UTF-8, SSE framing, error handling, credentials/config permissions and history.
  Shell syntax and Openbox XML validation also pass.
- The `20260908-r4` installer completed a fresh UEFI installation without manual
  changes, invoked by `aios` through scoped doas. Cancellation and mounted-disk
  refusal passed. The installed disk booted with the ISO removed, downloaded a
  model from the UI, answered locally, and preserved the model, conversation and
  compiled project after the UI Restart action. Local inference resumed after
  reboot. The UI Shut down action powered off the live VM.
- Terminating the desktop stopped its owned model server and opened a recovery
  terminal. The r5 build changed only the power popup presentation from r4.
- The r5 image also completed a BIOS installation with cancellation and
  mounted-disk refusal checked. Its terminal icon opened xterm as `aios`; the
  generated Qt example compiled and launched as a normal desktop window. The
  installed BIOS disk booted to the launcher with its ISO removed.

## Multi-window and voice update

- In the r7 desktop, two launcher activations created separate empty windows.
  Each generated a distinct UUID history with its own messages. Closing the
  second window left the first conversation intact and the desktop running.
- Selected a text file through the attachment picker. It appeared as a removable
  chip, and its content was included in the sent message while the UI displayed
  only the filename and prompt.
- Verified real local eSpeak synthesis and Whisper tiny.en transcription. The
  phrase "Hello world, this is a voice test" was transcribed with one word error.
- Configured a separate HTTPS speech server backed by those real speech engines.
  The UI recorded from a synthetic PulseAudio microphone, illuminated the Voice
  control, then put the returned transcript in the composer without sending it.
  No host microphone was captured. Test CA trust was added only to the disposable
  guest, never to the image.
- A spoken reply was fetched as WAV and played through Qt to the guest's
  PulseAudio output, verified by its active audio stream. This uncovered Alpine's
  separate Qt FFmpeg playback package; it is included in r9.
- r9 also contains the validated speech-model downloader correction, CLI voice
  commands and a font-independent copy icon. USB/Bluetooth microphones and real
  speaker hardware remain untested.
- r9 failed the 4 GiB offline boot gate: the live tmpfs root filled before package
  installation finished, leaving login binaries missing. A 6 GiB diagnostic boot
  measured 2,014 MiB used on `/`. r10 increases only the live-root ceiling from
  Alpine's default half of RAM to 75%; it also explicitly lists `agetty` and makes
  boot checks fail immediately on installation/login errors, saving full logs.
- **r10 passed both offline BIOS and UEFI boot checks with 4 GiB RAM**, including
  the ordinary-user desktop process and compiling the default Qt example. This
  confirms the live-root correction; complete serial logs were retained.

## Release gates still open

- Complete stream cancellation, resolution, reduced-motion and keyboard QA.
- Run two independent clean builds and compare recorded inputs. Source revisions
  and container digest are pinned, but Alpine package repositories can change;
  byte-identical reproducibility has not been demonstrated.
- Complete ten cold boots and record repeatable resource measurements. One
  installed guest reported about 255 MiB used with the starter model loaded;
  this single observation is not a minimum-memory claim.
- Test VMware and physical machines. Secure Boot, GPU acceleration, ARM and
  non-AVX2 local inference are outside the current validated target.

## Reproduce the offline smoke checks

```sh
bash scripts/test.sh
python3 scripts/test-boot.py path/to/aios.iso
python3 scripts/test-boot.py path/to/aios.iso --uefi /usr/share/OVMF/OVMF_CODE_4M.fd
```

Current boot checks use disposable VMs with 8 GiB RAM, four CPUs and no
host-disk passthrough. Earlier checkpoints above record their historical 4 GiB
results.
The manual GitHub image workflow runs both firmware checks before uploading
the ISO, checksums, package manifest and source pins.


## Desktop settings checkpoint (2026-09-08)

Added a reusable desktop settings window alongside terminal and power. Its AI
model/voice editor is shared with chat; sections cover sound, camera, network,
display and appearance, with a simple extension point for future pages.
Sound uses pavucontrol, networking uses NetworkManager/nmtui plus wpa_supplicant,
and display configuration uses ARandR. Camera preview is explicitly activated
and stops when leaving its section or closing the window.

Executed against r11/r12: native C++ build, QML render, eight backend tests and
shell/XML checks; offline 4 GiB BIOS/UEFI boots and default Qt example compilation.
Interactive VM checks confirmed the settings launcher, automatic Ethernet DHCP
and HTTPS connectivity, ordinary-user network connection edits saved to disk,
and a sound slider change verified at 51% through PulseAudio. ARandR changed
1280x800 to 1024x768 and the desktop/panel repositioned correctly. Camera showed
an explicit no-device state and disabled preview. r13 contains the final matching
frameless panel and minor layout cleanup. The final r13 ISO also passed offline
4 GiB BIOS and UEFI boots and default Qt example compilation.

Physical webcam capture, Wi-Fi association, hardware audio, multi-monitor layouts,
and installed-system display-layout persistence still need target-hardware QA.
System sound/network/display controls open dedicated tools from the panel; these
are not yet embedded native controls. The panel itself and camera preview are
native Qt Quick. No real camera or microphone was activated for these checks.

Final r13 interactive QA also confirmed that saving a remote endpoint in desktop
Settings appears in the existing chat window's shared model editor. The final
frameless window opened successfully in UEFI; the QA harness used a USB tablet
for absolute pointer events.


## Ambient chat launcher

Replaced the central labeled chat button with an unlabeled Canvas blob at bottom
center, aligned with the smaller settings/terminal/power buttons. Its silhouette
and inner contours change on a 14-second cycle, with hover/press/focus feedback.
Keyboard activation retains normal Button behavior and an accessible name.
Reduced motion renders a static shape. Inactive activityLevel/awakened properties
provide a future integration point; no wake-word detector or microphone listener
is enabled by this change. Each click retains the existing new-session callback.

Validation: native shell compiled; actual desktop and chat rendered using the
software backend; a 70-frame animation was captured; a runtime check confirmed
phase advancement and freezing when reduced motion is enabled. This UI-only
checkpoint has not been rebuilt into an ISO; the last boot-tested ISO is r13.


## Agent browser checkpoint

Chromium/ChromeDriver are included, with no new desktop icon and a hidden desktop
entry. Desktop chat now supports bounded structured browser tool calls. Each chat
owns a private broker and an isolated temporary Chromium profile. Model prose is
never executed, page content is marked untrusted, and tool arguments cannot name
arbitrary commands or JavaScript. Local llama-server enables Jinja tool templates.

Thirteen backend tests passed, including fragmented tool calls, incomplete calls
that execute nothing, non-execution of prose, URL restrictions and loop limits.
Native shell compilation passed. In the r14 AIOS VM, Chromium with its sandbox
intact passed real open/read/type/click/back/tab/switch operations. The final
viewport snapshot code also passed scrolling to previously offscreen text and
controls. The reproducible browser smoke test is scripts/test-browser.py.

A native chat request was exercised end to end against a scripted local model
endpoint: structured tool calls opened real Chromium, filled/submitted a local
form, read the result and returned the final answer to chat. This tests the real
transport/worker/broker/UI path; it does not establish a particular LLM's browser
reasoning quality. Two chat windows created two drivers/profiles. Closing one
removed only its driver/profile; closing the other left neither behind. The
standard Chromium frame is intentionally retained for now.

The build-container browser test could not start Chromium in its sandbox; the
real browser checks above were run successfully as the ordinary user in AIOS.
A real tool-capable local/remote model matrix, broader websites, frames/canvas
applications and hardware performance remain release validation work.

The real SmolLM2 starter model also returned a normal greeting through the new
agent transport with browser tool schemas and Jinja enabled. This verifies plain
chat compatibility, not reliable browser planning by that small model.

Final r17 ISO: offline BIOS and UEFI boots both passed with 4 GiB RAM, including
ordinary-user desktop startup and compilation of the included Qt example. This
image includes Chromium, the current settings panel and the muted chat blob.

## First-boot chat and window controls (r18)

SmolLM2 135M Q4_K_M is now bundled with its Apache-2.0 license and provenance.
The build verifies its pinned SHA-256, including cached weights. A fresh local
configuration uses the bundled model automatically; saved custom model paths
and remote configurations remain unchanged. "Use starter model" reuses the
bundled weights without downloading another copy.

Reproduced an inert terminal close button: Openbox's custom configuration had
removed the title-bar mouse bindings. Restored close, minimize, maximize,
focus/raise, title-bar dragging, and border resizing. Verified that the same
terminal closes after reconfiguration; the native chat and Settings close
controls also work.

Fourteen backend tests pass. The r18 ISO passes offline BIOS and UEFI boots at
4 GiB, including ordinary-user shell startup, desktop example compilation,
and an actual reply from the bundled model without setup or network access.

## Streaming chat scrolling (r19)

Chat now updates a stable QML message model instead of resetting ListView on
every backend notification/token. Following the latest message waits for row
layout and tracks growing replies and viewport changes. Wheel scrolling or
dragging the scrollbar pauses following; returning to the bottom resumes it.
Sending a new message cancels any remaining flick and returns to the latest row.

Three Qt Quick regression cases pass with the shipped Qt runtime: a streamed
reply grows without recreating its row and stays visible, wheel scrolling keeps
the history position across new tokens and can resume following, and sending
while scrolled up returns to the latest message. Reply completion and window
resizing are also covered. Image builds now run this UI suite before compiling.

## Minimized chat restoration

The chat blob no longer displays a visual tooltip. Activating it restores
minimized chat windows from most recently minimized to oldest before creating a
new independent session. Closing a minimized chat removes it from the restore
order, while visible chat windows continue to allow additional sessions.

Source-level Qt Quick regression tests cover accessibility metadata, tooltip
removal, minimize tracking, restore order, close cleanup and session creation.
This checkpoint does not claim a new ISO or interactive VM validation.

## Agentic tools checkpoint (2026-09-09)

Agent Skills, the shared per-chat tool host, allowlisted stdio MCP tools, Agent
tasks provider routing, and the cached native/web application builder are now
covered by backend integration tests and user/architecture documentation.

The new deterministic end-to-end test starts a real worker subprocess, agent
loop, AF_UNIX ToolHost and ApplicationStore, plus a loopback OpenAI-compatible
streaming server. It copies the shipped application-builder skill into a
temporary user skill directory. The store has an executable native-host
capability stub, a recording launcher, and an exact legacy web calculator. The
first turn verifies the advertised native schema, searches, deliberately creates
the trusted native calculator instead of reusing the web entry, publishes a
manifest-only cache entry, and launches it without `read` or `write`. The second
turn finds the exact native entry first and launches it without changing the
manifest. Chromium, the real Qt host, and paid providers are intentionally
replaced by deterministic fixtures.

Validation results:

- Under WSL, the native application-builder end-to-end test passed three
  requested runs:
  1 test in 3.536 seconds, 3.642 seconds, and 3.383 seconds.
- Under WSL, the skill, browser-agent, application-builder agent,
  application-store, and ToolHost test files passed 154 tests in 13.205
  seconds, with one existing platform/fixture skip.
- Under WSL, final full Python discovery passed 337 tests in 47.774 seconds,
  with two existing platform/fixture skips.
- Windows Git `diff --check` passed.

The previously validated compiled host was not rebuilt, as requested. No new
interactive VM, real Qt-window, full ISO, paid OpenAI/ChatGPT, or configured
third-party MCP run is claimed by this checkpoint.
