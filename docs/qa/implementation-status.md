# Implementation evidence — 2026-09-08

This is a development candidate, not a hardware-certified release.

## Executed

- Built an x86_64 Alpine 3.23 ISO with the native Qt desktop, developer packages,
  and pinned llama.cpp CLI/server. The image is approximately 1.08 GiB.
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
- Five Python backend tests pass, covering real HTTP streaming with fragmented
  UTF-8, SSE framing, error handling, credentials/config permissions and history.
  Shell syntax and Openbox XML validation also pass.
- The `20260908-r4` installer completed a fresh UEFI installation without manual
  changes, invoked by `aios` through scoped doas. Cancellation and mounted-disk
  refusal passed. The installed disk booted with the ISO removed, downloaded a
  model from the UI, answered locally, and preserved the model, conversation and
  compiled project after the UI Restart action. Local inference resumed after
  reboot. The UI Shut down action powered off the live VM.
- Terminating the desktop stopped its owned model server and opened a recovery
  terminal. The final build changes only the power popup presentation from r4.
- The final image also completed a BIOS installation with cancellation and
  mounted-disk refusal checked. Its terminal icon opened xterm as `aios`; the
  generated Qt example compiled and launched as a normal desktop window.

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
