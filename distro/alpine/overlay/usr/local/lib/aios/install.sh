# Installer decision and finalization helpers, sourced by
# /usr/local/sbin/aios-install. Split out so every refusal, the stable disk
# identity, the offline repository switch, the partition naming rule and the
# post-setup-disk sequence can be exercised by
# tests/test_install_offline_parity.py without touching a disk.
#
# Nothing here partitions, formats or erases anything. The destructive steps
# stay in aios-install, behind the guards defined below.

AIOS_MODE_MARKER=/etc/aios-mode
AIOS_PYTHON_PATH=/usr/local/share/aios
AIOS_REPOSITORIES=/etc/apk/repositories

# Only whole-disk paths the installer knows how to partition. Partitions,
# device-mapper nodes, loop devices and free-form paths are refused.
aios_disk_path_supported() {
    case "$1" in
        /dev/sd[a-z]|/dev/vd[a-z]|/dev/nvme[0-9]n[0-9]) return 0;;
        *) return 1;;
    esac
}

# Kept as its own function so tests can exercise the surrounding logic without
# a real block device. Production behavior is the plain -b test.
aios_is_block_device() {
    [ -b "$1" ]
}

aios_disk_is_whole() {
    aios_is_block_device "$1" && [ "$(lsblk -dnro TYPE "$1")" = disk ]
}

# Every mountpoint of the disk and of its children. "[SWAP]" appears here for
# an active swap device, so one read covers mounted filesystems and swap.
aios_disk_mountpoints() {
    lsblk -nro MOUNTPOINTS "$1"
}

# The booted live medium mounts the ISO under /media and its modloop at
# /.modloop. Refusing those explicitly keeps the message accurate instead of
# reporting the running installer's own USB stick as an ordinary busy disk.
aios_disk_holds_live_media() {
    aios_disk_mountpoints "$1" | grep -qE '^(/|/media(/.*)?|/\.modloop)$'
}

aios_disk_has_active_swap() {
    aios_disk_mountpoints "$1" | grep -qx '\[SWAP\]'
}

aios_disk_is_mounted() {
    aios_disk_mountpoints "$1" | grep -q '[^[:space:]]'
}

# Anything that is not a plain disk or partition (device-mapper, LVM, RAID,
# crypt) means another subsystem is holding the disk open.
aios_disk_has_mapped_devices() {
    lsblk -nro TYPE "$1" | grep -Ev '^(disk|part)$' | grep -q .
}

# A stable identity for the selected disk, read from fixed sysfs fields rather
# than from the path alone. `diskseq` is the kernel's per-attachment sequence
# number: it changes when a device is detached and reattached even if it comes
# back under the same name, which is exactly the hotplug case a path comparison
# misses. The device number and size are included so the identity still changes
# on a kernel too old to expose diskseq.
#
# Model and serial are shown to the operator in the confirmation prompt but are
# not part of this identity: they are not unique across cloned hardware.
aios_disk_identity() {
    name=${1#/dev/}
    sysfs=${2:-/sys}/class/block/$name
    [ -d "$sysfs" ] || return 1
    devno=$(cat "$sysfs/dev" 2>/dev/null) || return 1
    sectors=$(cat "$sysfs/size" 2>/dev/null) || return 1
    [ -n "$devno" ] && [ -n "$sectors" ] || return 1
    diskseq=$(cat "$sysfs/diskseq" 2>/dev/null) || diskseq=none
    [ -n "$diskseq" ] || diskseq=none
    printf 'diskseq=%s devno=%s sectors=%s' "$diskseq" "$devno" "$sectors"
}

# Every refusal in one place, so the identical set runs before the confirmation
# prompt and again immediately before the disk is partitioned.
aios_disk_guards() {
    if ! aios_disk_path_supported "$1"; then
        echo 'Unsupported disk path'; return 1
    fi
    if ! aios_disk_is_whole "$1"; then
        echo 'Select a whole disk'; return 1
    fi
    if aios_disk_holds_live_media "$1"; then
        echo 'This disk carries the running live system. Install to a different disk.'; return 1
    fi
    if aios_disk_has_active_swap "$1"; then
        echo 'Disk holds active swap. Run swapoff on it first.'; return 1
    fi
    if aios_disk_is_mounted "$1"; then
        echo 'Disk is in use. Unmount its filesystems first.'; return 1
    fi
    if aios_disk_has_mapped_devices "$1"; then
        echo 'Disk contains active mapped devices.'; return 1
    fi
    return 0
}

# Run after the exact erase confirmation and immediately before parted: the
# same guards again, plus the identity captured before the prompt. A disk that
# was unplugged, replaced, mounted, swapped on or claimed by device-mapper
# while the operator was typing is refused instead of erased.
aios_disk_unchanged() {
    disk=$1
    expected=$2
    sysfs=${3:-}
    aios_disk_guards "$disk" || return 1
    current=$(aios_disk_identity "$disk" "$sysfs") || {
        echo 'The selected disk no longer reports a stable identity.'; return 1; }
    if [ "$current" != "$expected" ]; then
        echo 'The selected disk changed after the confirmation and was not erased.'
        return 1
    fi
    return 0
}

aios_confirmation_matches() {
    [ "$1" = "ERASE $2" ]
}

# nvme0n1 numbers its partitions nvme0n1p1; sda numbers them sda1.
aios_partition_prefix() {
    case "$1" in
        *[0-9]) printf '%sp' "$1";;
        *) printf '%s' "$1";;
    esac
}

aios_firmware_mode() {
    if [ -d /sys/firmware/efi ]; then printf 'uefi'; else printf 'bios'; fi
}

# --- offline package source -------------------------------------------------
#
# setup-disk builds its `apk --repository` flags from the *live*
# /etc/apk/repositories, so directing the installation at the ISO's own
# repository means temporarily replacing that file. The path is discovered by
# aios.boot_repository from the kernel's mount table and Alpine's
# `.boot_repository` marker; no path comes from the operator or the
# environment. With only a local repository configured, apk cannot reach a
# network mirror even if one happens to be reachable.

aios_boot_repository() {
    live=${1:-}
    PYTHONPATH=$live$AIOS_PYTHON_PATH python3 -m aios.boot_repository path \
        --live-root "${live:-/}"
}

# Save the live repository configuration and leave only the local one in place.
aios_use_local_repository() {
    repository=$1
    saved=$2
    live=${3:-}
    [ -s "$live$AIOS_REPOSITORIES" ] || return 1
    cat "$live$AIOS_REPOSITORIES" > "$saved" || return 1
    printf '%s\n' "$repository" > "$live$AIOS_REPOSITORIES" || return 1
}

# Put the live repository configuration back. Safe to call repeatedly and safe
# to call before anything was saved: an empty saved copy means the switch never
# happened, and the live file is left alone. The saved copy is removed once
# restored, so the cleanup trap is a no-op after a successful restore.
aios_restore_repositories() {
    saved=$1
    live=${2:-}
    [ -s "$saved" ] || return 0
    cat "$saved" > "$live$AIOS_REPOSITORIES" || return 1
    rm -f "$saved"
}

# --- post-setup-disk sequence ----------------------------------------------
#
# Every function below takes the live root as an explicit trailing argument,
# empty for the real "/" root, so the whole sequence can be exercised against a
# fixture root without touching the running system.

# The installed system must keep setup-disk's target-specific atoms (notably
# linux-lts) while requesting every atom from the live image. Merge the live
# world into the installed world instead of replacing it. Repository URLs are
# copied from the restored live configuration so no /media path survives.
aios_copy_apk_configuration() {
    mountdir=$1
    live=${2:-}
    mkdir -p "$mountdir/etc/apk" || return 1
    [ -s "$mountdir/etc/apk/world" ] || {
        echo 'setup-disk produced no installed package world.'
        return 1
    }
    world_tmp="$mountdir/etc/apk/.world.aios.tmp"
    {
        sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$mountdir/etc/apk/world"
        sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$live/etc/apk/world"
    } | sort -u > "$world_tmp" || {
        rm -f "$world_tmp"
        return 1
    }
    mv "$world_tmp" "$mountdir/etc/apk/world" || return 1
    cat "$live$AIOS_REPOSITORIES" > "$mountdir$AIOS_REPOSITORIES" || return 1
    chmod 644 "$mountdir/etc/apk/world" "$mountdir$AIOS_REPOSITORIES" || return 1
}

# The installed kernel release, read from the target rather than from uname so
# a future kernel change cannot silently regenerate the wrong module data.
aios_target_kernel_release() {
    ls -1 "$1/lib/modules" 2>/dev/null | head -n 1
}

# Module dependency data and initramfs are regenerated against the target root.
# depmod -b and mkinitfs -b operate on a base directory, so no chroot, bind
# mount or network access is involved. mkinitfs would otherwise read the *live*
# /etc/mkinitfs/mkinitfs.conf, which describes the ISO's squashfs root, so the
# configuration setup-disk generated for this machine's root filesystem and
# boot controller is passed explicitly with -c.
aios_regenerate_boot_artifacts() {
    mountdir=$1
    release=$(aios_target_kernel_release "$mountdir")
    [ -n "$release" ] || { echo 'No kernel modules were installed on the target.'; return 1; }
    config=$mountdir/etc/mkinitfs/mkinitfs.conf
    [ -f "$config" ] \
        || { echo 'setup-disk generated no initramfs configuration on the target.'; return 1; }
    depmod -b "$mountdir" "$release" \
        || { echo 'Could not regenerate module dependency data on the target.'; return 1; }
    mkinitfs -b "$mountdir" -c "$config" -o "$mountdir/boot/initramfs-lts" "$release" \
        || { echo 'Could not regenerate the installed initramfs.'; return 1; }
}

# Adds the safe-graphics recovery entry to the GRUB configuration setup-disk
# generated, deriving its kernel arguments from the generated normal entry.
aios_prepare_boot_entries() {
    mountdir=$1
    live=${2:-}
    PYTHONPATH=$live$AIOS_PYTHON_PATH python3 -m aios.install_target recovery \
            --target "$mountdir" --live-root "${live:-/}" \
        || { echo 'Could not add the recovery boot entry to the installed GRUB configuration.'; return 1; }
}

# grub-mkconfig runs grub-probe, which reads the running kernel's device and
# mount tables. Without these three filesystems present inside the target it
# probes the live tmpfs root instead of the installed one; that is exactly what
# makes setup-disk's own grub APK trigger fail during the installation. The set
# is fixed: /run is not needed because os-prober is not installed and nothing
# in the generation path reads it.
aios_mount_chroot_filesystems() {
    mountdir=$1
    mkdir -p "$mountdir/dev" "$mountdir/proc" "$mountdir/sys" || return 1
    mount -o bind /dev "$mountdir/dev" || return 1
    mount -t proc proc "$mountdir/proc" || return 1
    mount -o bind /sys "$mountdir/sys" || return 1
}

# Unmounts whatever is mounted, in reverse order. Safe to call when nothing was
# mounted and after a partial mount, which is why both the generation helper
# and the installer's cleanup trap call it unconditionally. A bind mount left
# behind would keep the target root busy and defeat the trap's own umount.
aios_unmount_chroot_filesystems() {
    mountdir=$1
    for relative in sys proc dev; do
        if mountpoint -q "$mountdir/$relative"; then
            umount "$mountdir/$relative" || umount -l "$mountdir/$relative" || true
        fi
    done
}

# The authoritative GRUB configuration for the installed system. setup-disk
# writes /etc/default/grub and leaves the configuration to the grub APK
# trigger, which runs against the live root and fails there; setup-disk still
# returns success, so this explicit generation is what produces a grub.cfg that
# names the target's own root. It runs after grub-install and after the
# initramfs is regenerated, and before the recovery entry is derived from it.
aios_generate_grub_configuration() {
    mountdir=$1
    aios_mount_chroot_filesystems "$mountdir" || {
        aios_unmount_chroot_filesystems "$mountdir"
        echo 'Could not prepare the installed root for GRUB configuration.'
        return 1
    }
    if chroot "$mountdir" grub-mkconfig -o /boot/grub/grub.cfg; then
        generated=0
    else
        generated=1
        echo 'Could not generate the installed GRUB configuration.'
    fi
    aios_unmount_chroot_filesystems "$mountdir"
    return $generated
}

# Init scripts AIOS owns. They reach the live root from the apkovl rather than
# from an apk package, so setup-disk's own copy of /etc is the only thing that
# would carry them, and nothing verifies that it did. Copying them explicitly,
# with their mode, and re-creating their runlevel symlinks, is what makes the
# installed system's service set deterministic. Only these fixed names are
# written: Alpine-owned init scripts are never touched.
AIOS_OWNED_SERVICES='aios-init aios-sessiond'
AIOS_OWNED_DEFAULT_SERVICES='aios-init'

aios_copy_owned_services() {
    mountdir=$1
    live=${2:-}
    mkdir -p "$mountdir/etc/init.d" || return 1
    for service in $AIOS_OWNED_SERVICES; do
        [ -f "$live/etc/init.d/$service" ] || continue
        cp -a "$live/etc/init.d/$service" "$mountdir/etc/init.d/$service" || return 1
        chmod 755 "$mountdir/etc/init.d/$service" || return 1
    done
    for service in $AIOS_OWNED_DEFAULT_SERVICES; do
        [ -f "$mountdir/etc/init.d/$service" ] || {
            echo "The installed root carries no /etc/init.d/$service."
            return 1
        }
        mkdir -p "$mountdir/etc/runlevels/default" || return 1
        ln -sfn "/etc/init.d/$service" "$mountdir/etc/runlevels/default/$service" || return 1
    done
}

# Fail-closed readback of the mounted target. Its result is what decides
# whether the installation is reported as successful. The verifier stores its
# own JSON report inside the target; only a failure is echoed to the console.
aios_verify_target() {
    mountdir=$1
    firmware=$2
    live=${3:-}
    log="$mountdir/var/log/aios-install-verification.log"
    mkdir -p "$mountdir/var/log" || return 1
    mountpoint -q "$mountdir" || { echo 'The installation target is no longer mounted.'; return 1; }
    if PYTHONPATH=$live$AIOS_PYTHON_PATH python3 -m aios.install_target verify \
            --target "$mountdir" --firmware "$firmware" --live-root "${live:-/}" > "$log" 2>&1; then
        return 0
    fi
    echo "Verification report: /var/log/aios-install-verification.json on the target."
    cat "$mountdir/var/log/aios-install-verification.json" 2>/dev/null \
        || tail -n 80 "$log" 2>/dev/null \
        || true
    return 1
}

# Everything that runs after setup-disk and grub-install, in order. Returns
# non-zero with an explicit message on the first failure; the caller must not
# report success when it does.
aios_finalize_target() {
    mountdir=$1
    firmware=$2
    live=${3:-}
    aios_copy_apk_configuration "$mountdir" "$live" \
        || { echo 'Could not copy the live package world and repositories to the target.'; return 1; }
    mkdir -p "$mountdir/usr/local" "$mountdir/home" "$mountdir/var/log" || return 1
    cp -a "$live/usr/local/." "$mountdir/usr/local/" || return 1
    cp -a "$live/home/aios" "$mountdir/home/" || return 1
    aios_copy_owned_services "$mountdir" "$live" \
        || { echo 'Could not install the AIOS-owned services on the target.'; return 1; }
    printf 'installed\n' > "$mountdir$AIOS_MODE_MARKER" || return 1
    aios_regenerate_boot_artifacts "$mountdir" || return 1
    aios_generate_grub_configuration "$mountdir" || return 1
    aios_prepare_boot_entries "$mountdir" "$live" || return 1
    sync
    aios_verify_target "$mountdir" "$firmware" "$live" || {
        echo 'Installed-system verification failed. The disk was written but is not confirmed bootable.'
        return 1
    }
}
