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

The boot checks use disposable VMs with 4 GiB RAM and no host-disk passthrough.
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
