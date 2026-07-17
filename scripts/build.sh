#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

# x86_64 is the default target for remote builder hosts.
ARCH="${ARCH:-x86_64}" exec "$ROOT_DIR/scripts/build-iso-container.sh" "$@"
