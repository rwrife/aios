#!/bin/sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ISO_PATH="${1:-}"

if [ -z "$ISO_PATH" ]; then
  ISO_PATH="$(find "$ROOT_DIR/distro/alpine/out" -maxdepth 1 -type f -name '*.iso' | head -n 1 || true)"
fi

if [ -z "$ISO_PATH" ] || [ ! -f "$ISO_PATH" ]; then
  echo "usage: $0 /absolute/path/to/aios.iso"
  echo "or build first with: $ROOT_DIR/scripts/build-iso.sh"
  exit 1
fi

qemu-system-x86_64 \
  -m 2048 \
  -smp 2 \
  -enable-kvm \
  -boot d \
  -cdrom "$ISO_PATH" \
  -drive if=virtio,file="$ROOT_DIR/.tmp-aios-live.qcow2",format=qcow2 \
  -net nic,model=virtio \
  -net user
