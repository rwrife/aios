#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ISO_PATH="${1:-}"

if [ -z "$ISO_PATH" ]; then
  ISO_PATH="$(ls -1t "$ROOT_DIR"/distro/alpine/out/*-x86_64.iso 2>/dev/null | head -n 1 || true)"
fi

if [ -z "$ISO_PATH" ] || [ ! -f "$ISO_PATH" ]; then
  echo "usage: $0 /absolute/path/to/aios-x86_64.iso" >&2
  echo "or build first with: $ROOT_DIR/scripts/build.sh" >&2
  exit 1
fi

DRY_RUN="${DRY_RUN:-0}"

if [ "$DRY_RUN" != "1" ] && ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
  echo "[aios] qemu-system-x86_64 not found on host" >&2
  echo "[aios] install qemu-system-x86 or run this script on your x86 builder host" >&2
  exit 1
fi

DISK_PATH="${AIOS_VM_DISK:-$ROOT_DIR/.tmp-aios-live.qcow2}"
DISK_SIZE="${AIOS_VM_DISK_SIZE:-16G}"
MEM_MB="${AIOS_VM_MEM_MB:-4096}"
CPU_COUNT="${AIOS_VM_CPUS:-2}"

if [ "$DRY_RUN" != "1" ] && [ ! -f "$DISK_PATH" ]; then
  if ! command -v qemu-img >/dev/null 2>&1; then
    echo "[aios] qemu-img not found; cannot create $DISK_PATH" >&2
    exit 1
  fi
  qemu-img create -f qcow2 "$DISK_PATH" "$DISK_SIZE" >/dev/null
fi

QEMU_ARGS=(
  qemu-system-x86_64
  -m "$MEM_MB"
  -smp "$CPU_COUNT"
  -boot d
  -cdrom "$ISO_PATH"
  -drive "if=virtio,file=$DISK_PATH,format=qcow2"
  -nic user,model=virtio-net-pci
)
if [ "${AIOS_QEMU_UEFI:-0}" = 1 ]; then
  : "${AIOS_OVMF_CODE:?Set AIOS_OVMF_CODE to your OVMF_CODE firmware file}"
  QEMU_ARGS+=( -drive "if=pflash,format=raw,readonly=on,file=$AIOS_OVMF_CODE" )
fi

if [ "${AIOS_QEMU_HEADLESS:-0}" = "1" ]; then
  QEMU_ARGS+=( -display none -serial mon:stdio )
fi

if [ "$(uname -m)" = "x86_64" ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
  QEMU_ARGS+=( -enable-kvm -cpu host )
else
  QEMU_ARGS+=( -cpu max )
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  printf '%q ' "${QEMU_ARGS[@]}"
  echo
  exit 0
fi

exec "${QEMU_ARGS[@]}"
