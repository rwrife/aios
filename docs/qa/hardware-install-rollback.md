# Stage 4: offline install parity and installed-system audit checkpoints

Stage 4 (issue #101) makes an installation take every package from the booted
medium, verifies the installed result before reporting success, and records an
installed system as an auditable checkpoint.

**This stage does not deliver coordinated updates or rollback, and does not
close #101.** Restoring a previously recorded system coherently means restoring
the kernel, its modules, firmware and the Mesa/Xorg stack *together*. On an
ext4 root that needs a full-system snapshot or a signed package bundle, neither
of which exists here, so boot activation and rollback are not implemented and
no code pretends otherwise. That remains an acceptance blocker.

**Nothing in this stage is hardware evidence.** No physical disk has been
installed to, the destructive QEMU matrix has been prepared but not run, and no
device in `docs/qa/hardware-coverage.json` changes status.

## Offline installation parity

`aios-install` keeps its shape: an exact whole-disk path, an exact
`ERASE /dev/...` confirmation, and refusal of anything that is not an idle
whole disk. Those decisions live in
`distro/alpine/overlay/usr/local/lib/aios/install.sh` so they can be tested
without a disk.

### Destructive guards and stable identity

| Guard | Refuses |
| --- | --- |
| `aios_disk_path_supported` | anything that is not `/dev/sd[a-z]`, `/dev/vd[a-z]` or `/dev/nvme<N>n<N>` |
| `aios_disk_is_whole` | a non-block path, a partition, a ROM |
| `aios_disk_holds_live_media` | the running live medium, with its own message |
| `aios_disk_has_active_swap` | a disk carrying active swap |
| `aios_disk_is_mounted` | any mounted filesystem on the disk or its children |
| `aios_disk_has_mapped_devices` | device-mapper, LVM, RAID or crypt holders |

`aios_disk_guards` runs all six as one decision. Before the operator is asked
to type anything, `aios_disk_identity` records a stable identity from fixed
kernel fields:

```
diskseq=<kernel attachment sequence> devno=<MAJ:MIN> sectors=<512-byte sectors>
```

`diskseq` changes when a device is detached and reattached even under the same
name, which is exactly the hotplug case a path comparison misses; the device
number and size keep the identity meaningful on a kernel that does not expose
`diskseq`. Model and serial are shown in the prompt but are deliberately not
part of the identity, because they are not unique on cloned hardware.

Immediately after the exact confirmation and immediately before `parted`,
`aios_disk_unchanged` runs **every guard again** and re-reads the identity. A
disk that was unplugged, replaced, mounted, swapped on, claimed by
device-mapper or reassigned to another path is refused, not erased. Each of
those races has its own test.

### A genuinely offline setup-disk

Alpine v3.23's `setup-disk` builds its `apk --repository` flags from the
**live** `/etc/apk/repositories` (`local repos="$(sed -e 's/\#.*//'
"${ROOT}etc/apk/repositories")"`), unpacks the live overlay into the target
with `lbu package`, and then comments out local repository lines in the
target's copy. The installer works with that behavior rather than against it:

1. `aios.boot_repository` discovers the medium's own repository from the
   kernel's mount table: a mount point under `/media` backed by a `/dev/...`
   device, carrying Alpine's `apks/.boot_repository` marker and an
   architecture directory with `APKINDEX.tar.gz`. No path comes from the
   operator, the environment or a configuration file, and two candidate media
   are refused rather than guessed between. This runs *before* the
   confirmation, so a medium without a usable repository is refused before any
   disk is touched.
2. `aios_use_local_repository` saves the live configuration and leaves only
   that local path in it, so `apk` cannot reach a network mirror even if one
   happens to be reachable.
3. `aios_restore_repositories` puts the remote configuration back immediately
   after `setup-disk`, and again from the `EXIT INT TERM` cleanup trap, so an
   interrupted or failed run never leaves the live system pointing at a medium
   that is about to be removed. Restoring before a switch ever happened is a
   no-op, never an emptied file.
4. `aios_copy_apk_configuration` copies the restored **remote** URLs into the
   target and merges the live world into the world `setup-disk` generated.
   This preserves target-specific atoms such as `linux-lts` instead of
   deleting the installed kernel from future package maintenance. No
   `/media/...` path is written into the installed system: the ISO disappears
   when the USB is pulled, and the verifier fails the install if a local entry
   survived.

### Proof the packages came from the medium

`embedded_closure_parity` compares **every installed package and version** in
the target's apk database against the closure the booted ISO can actually
serve, read from that repository's own `APKINDEX.tar.gz` (package filenames are
a fallback only when a repository has no index). A version that is not in the
embedded closure could only have come from a network mirror, so the
installation fails. This covers the full live `/etc/apk/world`, not only
`world.hardware`, and `apk_world_installed` separately requires every
non-comment atom of the installed world to exist in the target database.
`apk_world_parity` requires the complete live world plus the installed
`linux-lts` atom and rejects other unexplained additions.

`linux-lts` is selected explicitly in `world.hardware`: a live image can boot
the standalone kernel and modloop without carrying the kernel APK, but
`setup-disk` requires that APK to construct an installed system offline.

### Initramfs

`setup-disk` writes `/etc/mkinitfs/mkinitfs.conf` on the *target* for that
machine's root filesystem and boot controller. `mkinitfs` otherwise reads the
live configuration, which describes the ISO's squashfs root, so the installer
passes the target file explicitly:

```
mkinitfs -b <target> -c <target>/etc/mkinitfs/mkinitfs.conf \
    -o <target>/boot/initramfs-lts <release>
```

`apps/aios/initramfs.py` then reads the produced image back. It parses the
configured features with the same assignment rule the shell applies, expands
each feature's `features.d/<feature>.modules` globs against the target's own
module tree, opens the gzip newc cpio and requires every selected module and
the `init` to really be inside it. Module names are compared in one logical
form: either merged-`/usr` root (`lib/modules/<release>/…` and
`usr/lib/modules/<release>/…`) and without the compression suffix, so an
installed `ext4.ko.gz` satisfies a configuration that selected `ext4.ko`. A
feature with no definition on the target, a missing boot module, an empty
expectation or a container this readback cannot open all fail or report missing
evidence. Timestamps and sizes are not accepted as evidence of anything.

### The installed GRUB configuration

`setup-disk` writes `/etc/default/grub` and leaves the configuration itself to
the grub package's APK trigger. That trigger runs `grub-probe` against the
*live* root, where it fails; apk reports the trigger error but still completes,
and `setup-disk` returns success. The configuration the installed system boots
from is therefore generated explicitly, by the installer, inside the target:

```
mount -o bind /dev <target>/dev
mount -t proc proc <target>/proc
mount -o bind /sys  <target>/sys
chroot <target> grub-mkconfig -o /boot/grub/grub.cfg
```

Those three filesystems are what `grub-probe` needs to resolve the target's own
root device; `/run` is not mounted because nothing in this path reads it. The
commands are fixed — no path, device or option comes from the operator. They
are unmounted again on every path out of the generation step, including a
partial mount and a failed `grub-mkconfig`, and the installer's cleanup trap
repeats the unmount before it unmounts the target itself, so an interrupted
installation cannot leave a bind mount holding the target root open.

Generation runs after `grub-install` and after the initramfs is rebuilt, and
before the recovery entry is derived from the result, so `boot_entries` reads a
configuration that names this machine's root.

### Installed-target verifier

`apps/aios/install_target.py` runs as `python3 -m aios.install_target verify
--target <mounted root> --firmware {bios,uefi}`. It is invoked by the installer
with the directory the installer mounted itself and by the tests with a fixture
root. It is deliberately **not** in `/etc/doas.d/aios.conf` and is not exposed
through any skill, tool or MCP: it takes no device path, package name, URL or
command.

| Check | Fails when |
| --- | --- |
| `target_root` | the target is not a directory containing `etc`, `boot` and `lib` (incomplete, never a pass) |
| `mode_marker` | `/etc/aios-mode` is not `installed` |
| `apk_world_parity` | the target's `/etc/apk/world` differs from the live one |
| `apk_repositories_parity` | the target's repositories differ from the live remote ones, or any local path survived |
| `apk_world_installed` | an atom of the target world is not in the target's apk database |
| `embedded_closure_parity` | an installed version is absent from the medium's embedded closure (incomplete when no boot repository is mounted) |
| `hardware_files_present` | a file the target's own database says a `world.hardware` package installed is not on the target |
| `kernel_image` | `/boot/vmlinuz-lts` is missing or empty |
| `kernel_modules` | the release differs from the live one, or the module inventory differs from live by logical path or by the SHA-256 of the **decompressed** module — additions, removals and same-size substitutions all fail. The live modloop ships `.ko` and `linux-lts` ships `.ko.gz`, so only the payload is comparable; a module in a container the standard library cannot open (zstd) has no payload identity and is reported as missing evidence rather than matched on its compressed bytes |
| `module_dependency_data` | `modules.dep`, `modules.alias`, `modules.builtin` or `modules.symbols` is missing, `modules.dep` is empty, or it names a module that is not on disk |
| `initramfs` | missing, empty, without `init`, missing a module its configured features select, or configured with a feature the target cannot define |
| `boot_entries` | the generated entry does not load `/boot/vmlinuz-lts` with a `root=` and `/boot/initramfs-lts`, the recovery entry is missing or lacks `aios.recovery nomodeset` and a `root=`, or the configuration never sources `custom.cfg` |
| `bootloader_artifacts` | UEFI: `EFI/BOOT/BOOTX64.EFI` or the `x86_64-efi` platform modules (`normal`, `linux`, `ext2`, `part_gpt`) missing; BIOS: `i386-pc/core.img`, `boot.img` or the same platform modules missing |
| `required_services` | a required runlevel entry is not a symlink, its init script is missing or not executable, or the live-only `modloop` service is still in a runlevel. Each entry is reported with the reason it cannot start — `not_present`, `not_a_symlink`, `wrong_target`, `script_missing` or `not_executable` — and both symlink forms (target-root absolute and OpenRC-relative) resolve |
| `aios_userspace` | `aios-install`, the installer library, the hardware world reference or `/home/aios` is missing |

The result is one bounded JSON report, stored inside the target at the fixed
relative path `/var/log/aios-install-verification.json`. Lists are capped at
eight entries and the serialized report at 8 KiB, dropping detail before
classifications. It contains relative paths, counts and classifications only:
no disk path, device name, serial number or user data. Exit codes match
`scripts/inspect-image.py`: `0` pass, `3` a check failed, `4` required evidence
was missing. Both non-zero codes withhold installation success, and the
installer says explicitly that the disk was written but is not confirmed
bootable. On failure the installer prints the whole bounded report to the
console, so a disposable-disk run says *which* check failed and with what
sample rather than only that verification failed.

AIOS's own OpenRC services — `/etc/init.d/aios-init` and
`/etc/init.d/aios-sessiond` — reach the live root from the apkovl rather than
from an apk package, so nothing but `setup-disk`'s own copy of `/etc` would
carry them onto the target. The finalization step copies those two fixed names
from the live root with their mode and re-creates `aios-init`'s `default`
runlevel symlink. No other init script is written, so Alpine-owned services
keep whatever `setup-disk` installed.

### BIOS and UEFI boot entries

The recovery entry is not invented. `apps/aios/install_target.py` reads the
first complete `menuentry` block `grub-mkconfig` generated, reuses its body
verbatim — the `search --fs-uuid`, the module loads, the `initrd` — and only
appends `aios.recovery nomodeset` to the kernel arguments. Root identification
therefore stays exactly what Alpine's own tooling decided, and the file
contains nothing else: no baseline region, no `set default`.

The entry is written to `/boot/grub/custom.cfg`, which upstream GRUB's
`41_custom` generator already sources. When the generated configuration does
not source it, the installer appends one marked, idempotent sourcing block
rather than rewriting the file, and the verifier fails the installation if the
entry still would not be read.

UEFI installs keep the existing removable path
(`grub-install --target=x86_64-efi --removable --no-nvram`); BIOS installs keep
`grub-install --target=i386-pc`. Both artifact sets are verified per firmware
mode against the layout `grub-install` really produces. These are static and
configuration checks; a real firmware boot is a remaining gate.

## Audit checkpoints — not rollback

`/usr/local/sbin/aios-checkpoint` is a root-only helper with exactly three
operations: `status`, `verify` and `stage`. There is no `activate` and no
`rollback`, in the CLI, in the Python API or anywhere in the code.

`stage` records the running installed system as evidence:
`/var/lib/aios/checkpoints/<id>/manifest.json` plus a copy of that system's
`vmlinuz` and `initramfs` beside it. Nothing is written under `/boot`, no GRUB
file is read for writing, no default entry or bootloader pointer is moved, and
no `active`/`fallback` symlink exists. A test asserts that `/boot` and both
GRUB files are byte-identical after staging and verifying, and that user, chat
and model storage is untouched.

| Manifest field | Meaning |
| --- | --- |
| `kind`, `schema_version` | `aios-checkpoint-manifest`, `1` |
| `checkpoint_id` | SHA-256 of the canonical JSON of every field except `checkpoint_id`, `created`, `physical_revalidation_required` and `restorable` |
| `alpine_branch` | release family from `/etc/alpine-release`, e.g. `v3.23` |
| `kernel_release` | the single installed module release |
| `artifacts` | SHA-256 and size of the recorded `vmlinuz` and `initramfs` |
| `modules_inventory_digest`, `module_count` | digest over every module's relative path **and content hash**, so a same-size substitution changes it |
| `hardware_packages`, `graphics_packages` | `world.hardware` atoms and the Mesa/Xorg set with their installed versions |
| `package_closure_digest`, `package_count` | digest over every installed `name=version` |
| `created` | creation date |
| `physical_revalidation_required` | always `true` |
| `restorable` | always `false` |

`verify` re-reads the stored artifacts of each recorded checkpoint against its
own manifest. That is all it verifies.

`status` reports, in every run:

```json
"activation_enabled": false,
"rollback_enabled": false,
"package_acquisition_enabled": false,
"capability_note": "Checkpoints are an audit record only. Coherently restoring a
previously recorded system requires a full-system snapshot or a signed package
bundle that returns the kernel, its modules, firmware and the Mesa/Xorg stack
together; that does not exist here, so boot activation, rollback and package
acquisition are not implemented and this helper never changes what the machine
boots."
```

Identifiers are content-derived, so no free-form name reaches the store. The
module contains no network client and runs no subprocess; a test asserts the
absence of `subprocess`, `urllib`, `socket`, any URL, `apk add`, `grub-mkconfig`
and `set default` in its source. The helper is intentionally absent from
`/etc/doas.d/aios.conf`, so the desktop user, skills and MCP servers cannot
reach it.

### Durability, locking and reclamation

Every write goes through `apps/aios/durable.py`: a temporary name in the
destination directory, `fsync`, then a rename over the destination, with the
directory entry `fsync`ed afterwards. The manifest is written last, so an
interrupted run leaves a directory that no operation treats as a checkpoint.

Mutating work holds an exclusive, owner-only (`0600`) `flock` on
`<store>/.lock`. It is never waited on: a second run reports that one is
already in progress. Under that lock, `stage` first reclaims leftovers —
temporary names anywhere in the store including nested ones, directories that
are not content-identifiers, and checkpoint directories whose manifest is
missing, unreadable or no longer matches its own content. On any failure the
directory being written is removed, so a failed or interrupted run leaves
nothing partial behind. Concurrency and each interruption point have tests.

## QEMU harness

`scripts/test-boot.py` gained one option that owns a whole lifecycle:

```
python3 scripts/test-boot.py <iso> --install-and-boot {sata,nvme} [--uefi <OVMF_CODE>]
```

It creates one qcow2 inside its own temporary directory, boots the ISO with
that disk attached, logs in, feeds the installer exactly `/dev/sda` or
`/dev/nvme0n1` followed by `ERASE <disk>`, waits for the installer's own
`Installation complete` — which only appears after the fail-closed readback
passes — powers the guest down cleanly, and then boots **the same image** with
no ISO and no network attached. The second phase requires
`/etc/aios-mode` = `installed`, a passing
`/var/log/aios-install-verification.json`, no local repository entry in
`/etc/apk/repositories`, and then the existing desktop, toolchain and bundled
model checks plus the framebuffer capture.

There is no `--install-disk`, no `--boot-from`, no image or device argument and
no passthrough option: the disk path is chosen by the harness and cannot be
supplied. BIOS and UEFI come from the existing `--uefi` flag. Unit tests assert
the absence of any host disk input, the single image lifecycle shared by both
phases, and the exact installer input.

**This matrix has not been run.** The harness change is preparation.

## Secure Boot

Secure Boot remains **unsupported and must be disabled**, on the live medium
and on an installed system. `AIOS_MODLOOP_SIGN` signs Alpine's modloop for the
live image's own integrity check; that is not Secure Boot and is never
described as such. Signed bootloaders and kernels, key lifecycle, firmware
enrollment, revocation and a recovery path for a bricked key state are a
separate project that has not started.

## Tests

`tests/test_install_offline_parity.py` and `tests/test_checkpoint_audit.py`,
both non-destructive and display-independent:

- every disk refusal, driven through the real overlay library with a stubbed
  `lsblk`, individually and through `aios_disk_guards`;
- the stable identity from a fixture sysfs tree, including a kernel without
  `diskseq`, and refusal after the confirmation for a changed `diskseq`, a
  changed device number, a changed size, a vanished disk and every guard that
  newly trips;
- static structure of `aios-install`: identity captured before the prompt,
  repository discovered before the prompt, `aios_disk_unchanged` between the
  confirmation and `parted`, the repository switch before `setup-disk` and the
  restore after it and in the cleanup trap, and `Installation complete` only
  after `aios_finalize_target` succeeds;
- the boot-repository discovery: marker required, unmounted directories
  ignored, two mounted repositories refused, escaped mount points, index-based
  closure, filename fallback, and the `path` operation printing one line;
- the local repository switch and its restore, including a restore before any
  switch and a repeated restore from the trap;
- the whole post-`setup-disk` sequence against fixture roots with every
  external program recorded: exact `depmod` and `mkinitfs -b … -c …` command
  construction, world/repository parity, the firmware mode reaching the
  verifier, regeneration ordered before the readback, and an explicit failure
  when any single step fails;
- the initramfs readback: feature parsing, module expectation, a missing boot
  module inside a present image, a missing `init`, an undefined feature and an
  unreadable container;
- the verifier against a complete synthetic target, then one mutation per
  check, including a same-size module substitution, an extra module, a local
  repository left behind, a version outside the embedded closure, a non-symlink
  runlevel entry, a non-executable init script and a surviving `modloop`;
- the generated apkovl carrying `world.hardware`, the installer library and an
  executable `aios-checkpoint`, with no `aios-baseline` and no `doas` rule;
- checkpoints: manifest coverage and content-derived ids, refusal of free-form
  ids, staging idempotence, free-space refusal, live-session refusal,
  interruption at the artifact copy, at the manifest write and at the readback,
  orphan and nested-temporary reclamation, the exclusive lock under
  concurrency, `/boot` and user state left byte-identical, and the absence of
  any activation or rollback operation.

Run them with `bash scripts/test.sh`.

## Remaining gates

Acceptance blockers, none of which this stage clears:

- **coherent update and rollback is not enabled.** A full-system snapshot or a
  signed package bundle that restores kernel, modules, firmware and Mesa/Xorg
  atomically has to be designed first; #101 stays open.
- a real `setup-disk` run: every command construction is tested, but no actual
  Alpine installation has been performed, so the verifier has never run against
  a genuinely installed root;
- the disposable-disk QEMU matrix: BIOS and UEFI × NVMe and SATA. Prepared,
  not run;
- sacrificial physical NVMe and SATA installs on selected machines, at least
  three cold boots each, with the USB removed and the network disconnected;
- eMMC, which stays untested and unguarded;
- interrupted installs caused by real power loss rather than a failed write;
- Secure Boot in any form.

Device status in `docs/qa/hardware-coverage.json` stays `untested`.
