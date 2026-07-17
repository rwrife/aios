# aios (AI OS)

A minimal Linux distribution project focused on:
- small footprint
- simple setup
- booting straight into a lightweight window manager
- terminal-first UX with no general desktop app bundle

## Current status
Phase 1 scaffolding in progress (live ISO pipeline + terminal-only session wiring).

## Initial goals
1. Build reproducible image pipeline.
2. Boot to WM + terminal automatically.
3. Keep package surface extremely small.
4. Validate in QEMU before hardware targets.

## Build quickstart

Native Alpine host build:
- `./scripts/build-iso.sh`

Portable containerized build (recommended on Ubuntu/ARM64 hosts):
- `./scripts/build-iso-container.sh --simulate`
- `./scripts/build-iso-container.sh`

Useful overrides:
- `ARCH=aarch64 ./scripts/build-iso-container.sh`
- `ARCH=x86_64 ./scripts/build-iso-container.sh` (requires amd64 container emulation on ARM hosts)
