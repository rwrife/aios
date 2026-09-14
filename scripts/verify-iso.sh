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

# Offline hardware bundle: the build must have recorded the hardware and vm
# worlds, every hardware package must really be in the embedded closure, and
# every one of them must carry a license and repository, or a first boot would
# need the network and the image would ship unattributed firmware. The full
# driver/firmware closure is validated below with an extracted modloop.
python3 - "$iso.build-manifest.json" "$iso.packages.txt" <<'PY'
import json
import re
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
worlds = {
    world.get("role"): world
    for world in (manifest.get("package_worlds") or {}).get("worlds", [])
}
unrecorded = [
    role for role in ("hardware", "vm")
    if worlds.get(role, {}).get("status") != "recorded"
]
if unrecorded:
    sys.exit(f"build manifest did not record world(s): {', '.join(unrecorded)}")

shipped = set()
with open(sys.argv[2], encoding="utf-8") as listing:
    for line in listing:
        match = re.match(r"^(?P<name>.+)-[^-]+-r\d+\.apk$", line.strip())
        if match:
            shipped.add(match.group("name"))
missing = sorted(set(worlds["hardware"]["packages"]) - shipped)
if missing:
    sys.exit("hardware packages missing from the embedded closure: " + ", ".join(missing))
recorded_packages = manifest.get("hardware_packages") or {}
if recorded_packages.get("status") != "recorded":
    sys.exit("build manifest recorded no hardware package license/provenance")
records = recorded_packages.get("packages") or {}
incomplete = []
for name in sorted(worlds["hardware"]["packages"]):
    record = records.get(name) or {}
    absent = [field for field in ("license", "repository") if not record.get(field)]
    if absent:
        incomplete.append(f"{name} (missing {', '.join(absent)})")
if incomplete:
    sys.exit("hardware packages without a recorded license and repository: "
             + ", ".join(incomplete))
PY

# Full offline driver/firmware closure validation. This is the release gate, so
# it runs the same inspection a developer runs by hand: extract the parts of the
# ISO that carry evidence, unpack the modloop, and let
# scripts/inspect-image.py --validate-hardware decide. Exit 3 (a check failed)
# and exit 4 (required evidence was missing) both fail the build.
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
for tool in xorriso unsquashfs python3; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Required tool not found: $tool" >&2
    exit 1
  }
done

work=$(mktemp -d)
cleanup() { rm -rf "$work"; }
trap cleanup EXIT
trap 'cleanup; exit 1' INT TERM HUP

apkovl_name=$(xorriso -indev "$iso" -find / -maxdepth 1 -name '*.apkovl.tar.gz' 2>/dev/null \
  | tr -d "'" | sed -n 's|^/||p' | head -n 1)
[ -n "$apkovl_name" ] || { echo "No *.apkovl.tar.gz found in $iso_name" >&2; exit 1; }

xorriso -osirrox on -indev "$iso" \
  -extract /apks "$work/root/apks" \
  -extract /boot "$work/root/boot" \
  -extract "/$apkovl_name" "$work/root/$apkovl_name" >/dev/null 2>&1

modloop=$(find "$work/root/boot" -maxdepth 1 -name 'modloop-*' -type f -print 2>/dev/null \
  | sort | head -n 1)
[ -n "$modloop" ] || { echo "No boot/modloop-* in $iso_name" >&2; exit 1; }
unsquashfs -no-progress -d "$work/modloop" "$modloop" >/dev/null

status=0
python3 "$repo_root/scripts/inspect-image.py" \
  --root "$work/root" \
  --apkovl "$work/root/$apkovl_name" \
  --apks-dir "$work/root/apks" \
  --modloop-root "$work/modloop" \
  --build-manifest "$iso.build-manifest.json" \
  --coverage-manifest "$repo_root/docs/qa/hardware-coverage.json" \
  --hardware-package-manifest "$repo_root/docs/qa/hardware-packages.json" \
  --hardware-world "$repo_root/distro/alpine/apks/world.hardware" \
  --validate-hardware \
  -o "$out_dir/$iso_name.hardware-validation.json" || status=$?
if [ "$status" -ne 0 ]; then
  echo "Hardware bundle validation failed for $iso_name (inspect-image.py exit $status)" >&2
  exit "$status"
fi

echo "PASS: checksum, manifests, offline hardware closure, hardware bundle validation, BIOS boot, UEFI boot and hybrid USB metadata"
