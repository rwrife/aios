# Contributing to AIOS

AIOS builds an x86_64 Alpine Linux ISO inside a Linux container. Docker provides
the consistent build environment on Windows, Linux, and macOS.

## Get the source

```sh
git clone https://github.com/rwrife/aios.git
cd aios
```

Build outputs are written to `distro/alpine/out`. The reusable Docker volume
`aios-build-cache` keeps downloaded packages and compiled dependencies between
builds.

## Windows

Install:

- WSL2 with an Ubuntu distribution
- Docker Desktop with WSL integration enabled for that distribution
- PowerShell 5.1 or newer

Confirm Docker is available inside WSL:

```powershell
wsl -d Ubuntu -- docker version
```

From the repository root, build the ISO:

```powershell
.\scripts\build.ps1
```

Use `-Distro` if Docker is enabled in a different WSL distribution:

```powershell
.\scripts\build.ps1 -Distro Debian
```

## Linux

Install Git and Docker Engine, then ensure your user can run Docker commands.
From the repository root:

```sh
docker version
bash scripts/build.sh
```

## macOS

Install Git and Docker Desktop, start Docker Desktop, and confirm the Docker
daemon is available:

```sh
docker version
```

From the repository root:

```sh
bash scripts/build.sh
```

The build targets `linux/amd64`. Docker Desktop performs the required emulation
on Apple silicon, so builds there may take longer than on an x86_64 host.

The included `scripts/run.sh` launcher is designed for Linux and WSL. On macOS,
open the generated x86_64 ISO with a compatible virtual machine application.

## Build options

The build scripts recognize these environment variables:

- `RELEASE_TAG` sets the version embedded in the output filename.
- `AIOS_IDENTITY_BUILD=1` includes the optional identity display support.
- `AIOS_BUILDER_IMAGE` overrides the pinned Alpine builder image.
- `RUNTIME` selects another Docker-compatible container command.

Set variables before running the platform build command. For example:

```sh
RELEASE_TAG=development bash scripts/build.sh
```

```powershell
$env:RELEASE_TAG = 'development'
.\scripts\build.ps1
```

Only the x86_64 build profile is currently validated.

## Validate changes

Run the source and backend checks:

```sh
python3 -m pip install --only-binary=:all: --require-hashes -r apps/requirements.txt
bash scripts/test.sh
```

Use a Python virtual environment if your distribution manages the system
interpreter. Scheduling uses the system IANA time-zone database (`tzdata` on
Linux); the Alpine image includes it.

Windows contributors can run the complete source and display suites from
PowerShell after installing the Python requirements in the configured WSL
distribution:

```powershell
.\scripts\test.ps1
```

Use `-Distro` if the test environment is in another distribution:

```powershell
.\scripts\test.ps1 -Distro Debian
```

Before opening a pull request, confirm the intended files are included:

```sh
git status
git diff --check
```
