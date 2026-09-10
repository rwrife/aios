# Agent Instructions

## Running AIOS

- On Windows, run AIOS by launching the built image in QEMU. Use
  `.\scripts\run.ps1` for the preferred WSL2/WSLg flow, or add `-Native` for the
  slower native Windows fallback.
- A QEMU boot can take several minutes, especially when hardware acceleration is
  unavailable. Wait for the guest UI to finish booting before treating the
  launch as failed or restarting it.
- Give every QEMU instance a unique, descriptive window title so the user can
  tell which VM they are viewing. Pass a task- or session-specific value to
  QEMU's `-name` option instead of leaving multiple windows titled `AIOS`.
