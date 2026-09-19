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
DISK_SIZE="${AIOS_VM_DISK_SIZE:-64G}"
MEM_MB="${AIOS_VM_MEM_MB:-16384}"
CPU_COUNT="${AIOS_VM_CPUS:-4}"
VM_NAME="${AIOS_VM_NAME:-AIOS-$(basename "$ISO_PATH" .iso)-$$}"
AUDIO_BACKEND="${AIOS_QEMU_AUDIO:-pa}"

# Stage-0 audio preflight (issue #84): name the selected backend and resolve
# its Pulse endpoint before QEMU starts, so silent audio failures are
# attributable here instead of inside the guest. A caller-supplied
# PULSE_SERVER is preserved; unix:/mnt/wslg/PulseServer is only used after
# its socket exists. This warning never blocks headless or non-audio flows.
if [ "$AUDIO_BACKEND" = "pa" ]; then
  if [ -n "${PULSE_SERVER:-}" ]; then
    printf '[aios] QEMU audio backend pa uses the preserved PULSE_SERVER=%s\n' "$PULSE_SERVER" >&2
  elif [ -S /mnt/wslg/PulseServer ]; then
    export PULSE_SERVER="unix:/mnt/wslg/PulseServer"
    printf '[aios] QEMU audio backend pa uses the validated WSLg socket %s\n' "$PULSE_SERVER" >&2
  elif [ "$DRY_RUN" != "1" ]; then
    printf '[aios] warning: QEMU audio backend is pa but no Pulse endpoint exists (no PULSE_SERVER, no /mnt/wslg/PulseServer); guest audio will be silent. See docs/qa/wsl-audio.md or set AIOS_QEMU_AUDIO=none to silence this warning.\n' >&2
  fi
fi
SERIAL="${AIOS_QEMU_SERIAL:-}"
if [ -z "$SERIAL" ]; then
  if [ "${AIOS_QEMU_HEADLESS:-0}" = "1" ]; then
    SERIAL=mon:stdio
  else
    SERIAL="file:$ROOT_DIR/.tmp-aios-boot.log"
  fi
fi
CAMERA_BUS="${AIOS_VM_CAMERA_BUS:-}"
CAMERA_ADDR="${AIOS_VM_CAMERA_ADDR:-}"
if [ -n "$CAMERA_BUS$CAMERA_ADDR" ]; then
  if [[ ! "$CAMERA_BUS" =~ ^[1-9][0-9]*$ || ! "$CAMERA_ADDR" =~ ^[1-9][0-9]*$ ]]; then
    echo '[aios] Set both AIOS_VM_CAMERA_BUS and AIOS_VM_CAMERA_ADDR to the selected Linux USB bus/device numbers (without leading zeros).' >&2
    exit 1
  fi
  printf -v CAMERA_DEVICE '/dev/bus/usb/%03d/%03d' "$CAMERA_BUS" "$CAMERA_ADDR"
  if [ "$DRY_RUN" != 1 ] && { [ ! -r "$CAMERA_DEVICE" ] || [ ! -w "$CAMERA_DEVICE" ]; }; then
    echo "[aios] Selected camera $CAMERA_DEVICE is missing or not accessible. Attach it and grant this user access to that device first." >&2
    exit 1
  fi
fi

if [ "$DRY_RUN" != "1" ] && [ ! -f "$DISK_PATH" ]; then
  if ! command -v qemu-img >/dev/null 2>&1; then
    echo "[aios] qemu-img not found; cannot create $DISK_PATH" >&2
    exit 1
  fi
  qemu-img create -f qcow2 "$DISK_PATH" "$DISK_SIZE" >/dev/null
fi

QEMU_ARGS=(
  qemu-system-x86_64
  -name "${VM_NAME//,/,,}"
  -m "$MEM_MB"
  -smp "$CPU_COUNT"
  -boot d
  -cdrom "$ISO_PATH"
  -serial "$SERIAL"
  -drive "if=virtio,file=${DISK_PATH//,/,,},format=qcow2"
  -nic user,model=virtio-net-pci
  -audiodev "${AUDIO_BACKEND},id=audio0"
  -device intel-hda
  -device hda-duplex,audiodev=audio0
)
if [ -n "$CAMERA_BUS" ]; then
  QEMU_ARGS+=( -device qemu-xhci,id=camera-usb -device "usb-host,bus=camera-usb.0,hostbus=$CAMERA_BUS,hostaddr=$CAMERA_ADDR" )
fi
if [ "${AIOS_QEMU_UEFI:-0}" = 1 ]; then
  : "${AIOS_OVMF_CODE:?Set AIOS_OVMF_CODE to your OVMF_CODE firmware file}"
  QEMU_ARGS+=( -drive "if=pflash,format=raw,readonly=on,file=${AIOS_OVMF_CODE//,/,,}" )
fi

if [ "${AIOS_QEMU_HEADLESS:-0}" = "1" ]; then
  QEMU_ARGS+=( -display none )
else
  QEMU_ARGS+=( -display gtk,full-screen=off,zoom-to-fit=off )
fi

if [ "$(uname -m)" = "x86_64" ] && [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
  QEMU_ARGS+=( -enable-kvm -cpu host )
else
  if [ "$DRY_RUN" != "1" ]; then
    echo "[aios] KVM is unavailable; software emulation will be slow. Check /dev/kvm permissions." >&2
  fi
  QEMU_ARGS+=( -cpu max )
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  printf '%q ' "${QEMU_ARGS[@]}"
  echo
  exit 0
fi

printf '[aios] Booting %s as %s\n' "$ISO_PATH" "$VM_NAME"
if [ "${AIOS_QEMU_HEADLESS:-0}" != "1" ]; then
  echo '[aios] The full desktop opens in a separate QEMU window. No terminal login is needed.'
  echo '[aios] Live desktop startup can take several minutes.'
fi
printf '[aios] Serial diagnostics: %s\n' "$SERIAL"
exec "${QEMU_ARGS[@]}"
