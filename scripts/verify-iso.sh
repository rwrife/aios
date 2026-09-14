#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 path/to/aios.iso" >&2
  exit 2
fi

iso=$1
out_dir=$(CDPATH= cd -- "$(dirname -- "$iso")" && pwd)
iso_name=$(basename -- "$iso")
sums="$out_dir/SHA256SUMS"

[ -s "$iso" ] || { echo "ISO not found or empty: $iso" >&2; exit 1; }
[ -s "$sums" ] || { echo "Checksum manifest not found: $sums" >&2; exit 1; }
[ -s "$iso.packages.txt" ] || { echo "Package manifest not found: $iso.packages.txt" >&2; exit 1; }
[ -s "$iso.build-manifest.json" ] || { echo "Build/package-closure manifest not found: $iso.build-manifest.json" >&2; exit 1; }
[ -s "$out_dir/build-inputs.env" ] || { echo "Build input manifest not found: $out_dir/build-inputs.env" >&2; exit 1; }

expected=$(awk -v file="$iso_name" '{
  listed=$2
  sub(/^\*/, "", listed)
  sub(/^\.\//, "", listed)
  if (listed == file) print $1
}' "$sums")
[ -n "$expected" ] || { echo "Checksum missing for $iso_name" >&2; exit 1; }
actual=$(sha256sum "$iso" | awk '{ print $1 }')
[ "$actual" = "$expected" ] || { echo "Checksum mismatch for $iso_name" >&2; exit 1; }

boot_args=$(xorriso -indev "$iso" -report_el_torito as_mkisofs 2>&1)
printf '%s\n' "$boot_args" | grep -F -- "-b '/boot/syslinux/isolinux.bin'" >/dev/null
printf '%s\n' "$boot_args" | grep -F -- "-e '/boot/grub/efi.img'" >/dev/null
printf '%s\n' "$boot_args" | grep -F -- "-isohybrid-mbr" >/dev/null
printf '%s\n' "$boot_args" | grep -F -- "-isohybrid-gpt-basdat" >/dev/null

echo "PASS: checksum, manifests, BIOS boot, UEFI boot and hybrid USB metadata"
