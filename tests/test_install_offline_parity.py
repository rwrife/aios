"""Offline installation parity: installer guards, the local repository and the
fail-closed readback of an installed target.

Nothing here touches a disk. The installer's decision functions are sourced
from the real overlay library and driven with stubbed `lsblk` and a fixture
sysfs tree; the post-`setup-disk` sequence runs against fixture roots with
every external program replaced by a recorder; the verifier runs against a
synthetic target root that is mutated one check at a time.
"""
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from aios import boot_repository, initramfs, install_target
from aios.install_target import (EXIT_FAILED, EXIT_INCOMPLETE, EXIT_OK, RECOVERY_TITLE,
                                 TargetError, build_recovery_entry, prepare_recovery_entry,
                                 verify_target)

ROOT = Path(__file__).resolve().parents[1]
ALPINE = ROOT / "distro" / "alpine"
OVERLAY = ALPINE / "overlay"
LIBRARY = OVERLAY / "usr" / "local" / "lib" / "aios" / "install.sh"
INSTALLER = OVERLAY / "usr" / "local" / "sbin" / "aios-install"
DOAS_RULES = OVERLAY / "etc" / "doas.d" / "aios.conf"
GENAPKOVL = ALPINE / "apkovl" / "genapkovl-aios.sh"
APKS = ALPINE / "apks"
BOOT_TEST = ROOT / "scripts" / "test-boot.py"
APPS = ROOT / "apps"

RELEASE = "6.18.52-0-lts"
HARDWARE_ATOMS = ("linux-firmware-intel", "wireless-regdb", "iw")
WORLD = ("alpine-base", "networkmanager", *HARDWARE_ATOMS)
INSTALLED_WORLD = (*WORLD, "linux-lts")
VERSIONS = {name: "1.0-r0" for name in WORLD}
VERSIONS["linux-lts"] = "6.18.52-r0"
REPOSITORIES = ("https://dl-cdn.alpinelinux.org/alpine/v3.23/main",
                "https://dl-cdn.alpinelinux.org/alpine/v3.23/community")
MEDIA_POINT = "/media/usb"
MODULES = ("kernel/fs/ext4/ext4.ko.gz", "kernel/drivers/scsi/sd_mod.ko.gz",
           "kernel/drivers/net/iwlwifi.ko.gz")
BOOT_MODULES = ("kernel/fs/ext4/ext4.ko.gz", "kernel/drivers/scsi/sd_mod.ko.gz")
FEATURES = "base ext4"

GENERATED_GRUB = """\
set timeout=3
set default=0
menuentry 'AIOS' --class alpine {
	load_video
	insmod gzio
	insmod part_gpt
	insmod ext2
	search --no-floppy --fs-uuid --set=root 11111111-2222-3333-4444-555555555555
	linux /boot/vmlinuz-lts root=UUID=11111111-2222-3333-4444-555555555555 rw \
modules=sd-mod,usb-storage,ext4 console=tty0 console=ttyS0,115200 quiet
	initrd /boot/initramfs-lts
}
### BEGIN /etc/grub.d/41_custom ###
if [ -f  ${config_directory}/custom.cfg ]; then
  source ${config_directory}/custom.cfg
fi
### END /etc/grub.d/41_custom ###
"""

POSIX = unittest.skipUnless(os.name == "posix", "the installer helpers need a POSIX shell")


# --- shell library harness -------------------------------------------------

def run_library(snippet, mountpoints="", disk_type="disk", types="disk\npart",
                block_device=True, overrides="", arguments=()):
    """Source the real installer library and run one snippet against stubs."""
    with tempfile.TemporaryDirectory(prefix="aios-install-lib-") as directory:
        work = Path(directory)
        (work / "mountpoints").write_text(mountpoints)
        (work / "dtype").write_text(disk_type + "\n" if disk_type else "")
        (work / "types").write_text(types + "\n" if types else "")
        binaries = work / "bin"
        binaries.mkdir()
        stub = binaries / "lsblk"
        stub.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *-dnro\\ TYPE*) cat "$AIOS_TEST_DIR/dtype";;\n'
            '  *-nro\\ MOUNTPOINTS*) cat "$AIOS_TEST_DIR/mountpoints";;\n'
            '  *-nro\\ TYPE*) cat "$AIOS_TEST_DIR/types";;\n'
            "esac\n")
        stub.chmod(0o755)
        script = (f'. "{LIBRARY}"\n'
                  + ("" if block_device else "aios_is_block_device() { return 1; }\n")
                  + overrides + "\n" + snippet + "\n")
        return subprocess.run(
            ["sh", "-c", script, "sh", *[str(argument) for argument in arguments]],
            capture_output=True, text=True,
            env=dict(os.environ, PATH=f"{binaries}:{os.environ.get('PATH', '')}",
                     AIOS_TEST_DIR=str(work)))


def predicate(function, argument="/dev/sda", **keywords):
    """True when a library predicate accepts its argument."""
    return run_library(f'{function} "{argument}"', **keywords).returncode == 0


def sysfs_tree(root, name="sda", devno="8:0", sectors="500118192", diskseq="7"):
    """A fixture sysfs tree the identity helper can read."""
    directory = Path(root) / "class" / "block" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dev").write_text(devno + "\n")
    (directory / "size").write_text(sectors + "\n")
    if diskseq is not None:
        (directory / "diskseq").write_text(diskseq + "\n")
    return Path(root)


# --- fixtures ---------------------------------------------------------------

def cpio_archive(entries):
    """A newc cpio archive, gzip-compressed, exactly as mkinitfs produces."""
    raw = bytearray()

    def append(name, data):
        name_bytes = name.encode() + b"\0"
        fields = [1, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name_bytes), 0]
        raw.extend(b"070701")
        for value in fields:
            raw.extend(b"%08X" % value)
        raw.extend(name_bytes)
        raw.extend(b"\0" * (-(110 + len(name_bytes)) % 4))
        raw.extend(data)
        raw.extend(b"\0" * (-len(data) % 4))

    for name, data in entries:
        append(name, data)
    append("TRAILER!!!", b"")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as handle:
        handle.write(bytes(raw))
    return buffer.getvalue()


def apkindex(path, versions):
    """An APKINDEX.tar.gz like the one the ISO's /apks repository carries."""
    body = "".join(f"C:Q1\nP:{name}\nV:{version}\nS:1\n\n" for name, version in sorted(versions.items()))
    data = body.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(path), "w:gz") as archive:
        info = tarfile.TarInfo("APKINDEX")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))


def installed_database(versions, owned):
    """An apk installed database with file ownership for selected packages."""
    entries = []
    for name, version in sorted(versions.items()):
        lines = [f"P:{name}", f"V:{version}"]
        if name in owned:
            lines += ["F:usr/bin", f"R:{name}"]
        entries.append("\n".join(lines))
    return "\n\n".join(entries) + "\n"


def build_live(root, versions=None, repositories=REPOSITORIES, media=MEDIA_POINT):
    """A fixture live root: package world, modules and a mounted boot medium."""
    live = Path(root)
    versions = dict(versions or VERSIONS)
    for relative, text in (("etc/apk/world", "\n".join(sorted(WORLD)) + "\n"),
                           ("etc/apk/repositories", "\n".join(repositories) + "\n"),
                           ("usr/local/share/aios/world.hardware",
                            "\n".join(HARDWARE_ATOMS) + "\n"),
                           ("lib/apk/db/installed",
                            installed_database(versions, set(HARDWARE_ATOMS)))):
        path = live / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    build_modules(live)
    if media:
        (live / "proc").mkdir(parents=True, exist_ok=True)
        (live / "proc" / "mounts").write_text(
            "/dev/sr0 / iso9660 ro 0 0\n"
            f"/dev/sdb1 {media} vfat ro,noatime 0 0\n"
            "tmpfs /tmp tmpfs rw 0 0\n")
        repository = live / media.lstrip("/") / "apks"
        repository.mkdir(parents=True, exist_ok=True)
        (repository / ".boot_repository").write_text("")
        apkindex(repository / "x86_64" / "APKINDEX.tar.gz", versions)
    return live


def build_modules(base, release=RELEASE, modules=MODULES):
    directory = Path(base) / "lib" / "modules" / release
    for relative in modules:
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"module:{relative}".encode())
    (directory / "modules.dep").write_text(
        "".join(f"{relative}:\n" for relative in modules))
    for index in ("modules.alias", "modules.builtin", "modules.symbols"):
        (directory / index).write_text("# generated\n")
    return directory


def build_target(root, firmware="bios", release=RELEASE):
    """A synthetic installed target that passes every verification check."""
    root = Path(root)
    live = build_live(root / "live")
    target = root / "target"
    for relative, text in (("etc/apk/world", "\n".join(sorted(INSTALLED_WORLD)) + "\n"),
                           ("etc/apk/repositories", (live / "etc/apk/repositories").read_text()),
                           ("usr/local/share/aios/world.hardware",
                            "\n".join(HARDWARE_ATOMS) + "\n"),
                           ("lib/apk/db/installed",
                            installed_database(VERSIONS, set(HARDWARE_ATOMS))),
                           ("etc/aios-mode", "installed\n"),
                           ("etc/mkinitfs/mkinitfs.conf", f'features="{FEATURES}"\n'),
                           ("etc/mkinitfs/features.d/base.modules",
                            "kernel/drivers/scsi/sd_mod.ko*\n"),
                           ("etc/mkinitfs/features.d/ext4.modules", "kernel/fs/ext4/*.ko*\n")):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    build_modules(target, release=release)
    for name in HARDWARE_ATOMS:
        path = target / "usr" / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("binary")
    (target / "boot").mkdir(parents=True, exist_ok=True)
    (target / "boot" / "vmlinuz-lts").write_bytes(b"kernel")
    (target / "boot" / "grub").mkdir(parents=True)
    (target / install_target.GRUB_CONFIG).write_text(GENERATED_GRUB)
    prepare_recovery_entry(target)
    for relative in install_target.BOOTLOADER_ARTIFACTS[firmware]:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")
    for level, services in install_target.REQUIRED_SERVICES.items():
        directory = target / "etc" / "runlevels" / level
        directory.mkdir(parents=True, exist_ok=True)
        for service in services:
            script = target / "etc" / "init.d" / service
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("#!/sbin/openrc-run\n")
            script.chmod(0o755)
            (directory / service).symlink_to(f"/etc/init.d/{service}")
    for relative in ("usr/local/sbin/aios-install", "usr/local/lib/aios/install.sh"):
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("script")
    (target / "home" / "aios").mkdir(parents=True)
    write_initramfs(target, release=release)
    return live, target


def write_initramfs(target, release=RELEASE, modules=BOOT_MODULES, init=True):
    entries = [(f"lib/modules/{release}/{relative}", b"module") for relative in modules]
    if init:
        entries.insert(0, ("init", b"#!/bin/sh\n"))
    path = Path(target) / "boot" / "initramfs-lts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cpio_archive(entries))
    return path


@POSIX
class DiskGuardTests(unittest.TestCase):
    """Every refusal the installer relies on, exercised without a disk."""

    def test_only_whole_disk_paths_are_accepted(self):
        for accepted in ("/dev/sda", "/dev/sdz", "/dev/vda", "/dev/nvme0n1", "/dev/nvme1n2"):
            self.assertTrue(predicate("aios_disk_path_supported", accepted), accepted)
        for refused in ("/dev/sda1", "/dev/nvme0n1p1", "/dev/mapper/vg-root", "/dev/loop0",
                        "/dev/sdaa", "/dev/disk/by-id/ata-x", "/dev/../dev/sda", "sda",
                        "/dev/sda ", "", "/etc/passwd", "/dev/md0", "/dev/sr0"):
            self.assertFalse(predicate("aios_disk_path_supported", refused), refused)

    def test_a_partition_or_non_block_path_is_not_a_whole_disk(self):
        self.assertTrue(predicate("aios_disk_is_whole", disk_type="disk"))
        self.assertFalse(predicate("aios_disk_is_whole", disk_type="part"))
        self.assertFalse(predicate("aios_disk_is_whole", disk_type="rom"))
        self.assertFalse(predicate("aios_disk_is_whole", disk_type="disk", block_device=False))

    def test_live_media_is_refused_explicitly(self):
        for mountpoints in ("/media/usb\n", "/media/sr0/apks\n", "/.modloop\n", "/\n",
                            "\n/media/usb\n"):
            self.assertTrue(predicate("aios_disk_holds_live_media", mountpoints=mountpoints),
                            mountpoints)
        for mountpoints in ("", "\n\n", "/mnt/data\n", "/home/backup\n", "[SWAP]\n"):
            self.assertFalse(predicate("aios_disk_holds_live_media", mountpoints=mountpoints),
                             mountpoints)

    def test_active_swap_is_refused(self):
        self.assertTrue(predicate("aios_disk_has_active_swap", mountpoints="\n[SWAP]\n"))
        self.assertFalse(predicate("aios_disk_has_active_swap", mountpoints="/mnt/data\n"))
        self.assertFalse(predicate("aios_disk_has_active_swap", mountpoints=""))

    def test_any_mounted_filesystem_is_refused(self):
        self.assertTrue(predicate("aios_disk_is_mounted", mountpoints="\n/mnt/data\n\n"))
        self.assertFalse(predicate("aios_disk_is_mounted", mountpoints="\n\n \n"))

    def test_mapped_devices_are_refused(self):
        for types in ("disk\npart\ncrypt", "disk\nlvm", "disk\npart\nraid1", "disk\ndm"):
            self.assertTrue(predicate("aios_disk_has_mapped_devices", types=types), types)
        for types in ("disk", "disk\npart\npart"):
            self.assertFalse(predicate("aios_disk_has_mapped_devices", types=types), types)

    def test_the_erase_confirmation_must_match_exactly(self):
        accepted = run_library('aios_confirmation_matches "ERASE /dev/sda" /dev/sda')
        self.assertEqual(accepted.returncode, 0)
        for typed in ("ERASE /dev/sdb", "erase /dev/sda", "ERASE  /dev/sda", "ERASE /dev/sda ",
                      " ERASE /dev/sda", "/dev/sda", "ERASE", "yes", ""):
            result = run_library(f'aios_confirmation_matches "{typed}" /dev/sda')
            self.assertNotEqual(result.returncode, 0, typed)

    def test_partition_names_follow_the_device_naming_rule(self):
        for disk, prefix in (("/dev/sda", "/dev/sda"), ("/dev/vdb", "/dev/vdb"),
                             ("/dev/nvme0n1", "/dev/nvme0n1p")):
            result = run_library(f'aios_partition_prefix "{disk}"')
            self.assertEqual(result.stdout, prefix, disk)

    def test_the_whole_guard_set_runs_as_one_decision(self):
        self.assertEqual(run_library('aios_disk_guards /dev/sda').returncode, 0)
        for keywords, message in (
                ({"disk_type": "part"}, "whole disk"),
                ({"mountpoints": "/media/usb\n"}, "running live system"),
                ({"mountpoints": "[SWAP]\n"}, "active swap"),
                ({"mountpoints": "/mnt/data\n"}, "in use"),
                ({"types": "disk\ncrypt"}, "mapped devices")):
            result = run_library('aios_disk_guards /dev/sda', **keywords)
            self.assertNotEqual(result.returncode, 0, message)
            self.assertIn(message, result.stdout)
        refused = run_library('aios_disk_guards /dev/sda1')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Unsupported disk path", refused.stdout)


@POSIX
class DiskIdentityTests(unittest.TestCase):
    """The identity a confirmation applies to, and what invalidates it."""

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="aios-sysfs-")
        self.addCleanup(workspace.cleanup)
        self.sysfs = sysfs_tree(workspace.name)

    def identity(self, disk="/dev/sda"):
        return run_library(f'aios_disk_identity "{disk}" "$1"', arguments=(self.sysfs,))

    def test_the_identity_is_built_from_fixed_kernel_fields(self):
        result = self.identity()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "diskseq=7 devno=8:0 sectors=500118192")

    def test_an_unknown_device_has_no_identity(self):
        self.assertNotEqual(self.identity("/dev/sdz").returncode, 0)

    def test_a_kernel_without_diskseq_still_yields_a_device_identity(self):
        sysfs_tree(self.sysfs, name="sdb", devno="8:16", sectors="1024", diskseq=None)
        result = run_library('aios_disk_identity /dev/sdb "$1"', arguments=(self.sysfs,))
        self.assertEqual(result.stdout, "diskseq=none devno=8:16 sectors=1024")

    def recheck(self, expected, **keywords):
        return run_library(f'aios_disk_unchanged /dev/sda "{expected}" "$1"',
                           arguments=(self.sysfs,), **keywords)

    def test_an_unchanged_disk_passes_the_second_look(self):
        result = self.recheck("diskseq=7 devno=8:0 sectors=500118192")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_hotplugged_or_reassigned_disk_is_refused_after_confirmation(self):
        # Same path, different attachment: unplugged and replugged, or a new
        # device that took the name of the one the operator confirmed.
        for devno, sectors, diskseq in (("8:0", "500118192", "9"),
                                        ("8:32", "500118192", "7"),
                                        ("8:0", "120034123", "7")):
            sysfs_tree(self.sysfs, name="sda", devno=devno, sectors=sectors, diskseq=diskseq)
            result = self.recheck("diskseq=7 devno=8:0 sectors=500118192")
            self.assertNotEqual(result.returncode, 0, (devno, sectors, diskseq))
            self.assertIn("changed after the confirmation", result.stdout)

    def test_a_disk_that_vanished_after_confirmation_is_refused(self):
        result = run_library('aios_disk_unchanged /dev/sdb "diskseq=1 devno=8:16 sectors=1" "$1"',
                             arguments=(self.sysfs,))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stable identity", result.stdout)

    def test_every_guard_runs_again_after_the_confirmation(self):
        expected = "diskseq=7 devno=8:0 sectors=500118192"
        for keywords, message in (({"mountpoints": "/mnt/data\n"}, "in use"),
                                  ({"mountpoints": "[SWAP]\n"}, "active swap"),
                                  ({"mountpoints": "/media/usb\n"}, "running live system"),
                                  ({"types": "disk\ncrypt"}, "mapped devices"),
                                  ({"disk_type": "part"}, "whole disk"),
                                  ({"block_device": False}, "whole disk")):
            result = self.recheck(expected, **keywords)
            self.assertNotEqual(result.returncode, 0, message)
            self.assertIn(message, result.stdout)


@POSIX
class LocalRepositoryTests(unittest.TestCase):
    """setup-disk is pointed at the medium's repository and never left there."""

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="aios-repos-")
        self.addCleanup(workspace.cleanup)
        self.live = build_live(Path(workspace.name) / "live")
        self.saved = Path(workspace.name) / "saved"
        self.saved.write_text("")

    def repositories(self):
        return (self.live / "etc/apk/repositories").read_text()

    def switch(self, snippet):
        return run_library(snippet, arguments=(self.saved, self.live))

    def test_the_live_configuration_is_saved_and_replaced_by_the_local_one(self):
        result = self.switch('aios_use_local_repository /media/usb/apks "$1" "$2"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.repositories(), "/media/usb/apks\n")
        self.assertEqual(self.saved.read_text(), "\n".join(REPOSITORIES) + "\n")

    def test_restoring_puts_the_remote_configuration_back_exactly_once(self):
        self.switch('aios_use_local_repository /media/usb/apks "$1" "$2"')
        self.assertEqual(self.switch('aios_restore_repositories "$1" "$2"').returncode, 0)
        self.assertEqual(self.repositories(), "\n".join(REPOSITORIES) + "\n")
        self.assertFalse(self.saved.exists())
        # The cleanup trap calls this again on the way out; it must be a no-op.
        self.assertEqual(self.switch('aios_restore_repositories "$1" "$2"').returncode, 0)
        self.assertEqual(self.repositories(), "\n".join(REPOSITORIES) + "\n")

    def test_restoring_before_the_switch_never_empties_the_configuration(self):
        self.assertEqual(self.switch('aios_restore_repositories "$1" "$2"').returncode, 0)
        self.assertEqual(self.repositories(), "\n".join(REPOSITORIES) + "\n")


class BootRepositoryDiscoveryTests(unittest.TestCase):
    """The local repository is discovered from trusted state, never supplied."""

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="aios-bootrepo-")
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.live = build_live(self.root / "live")

    def test_the_mounted_medium_with_the_marker_is_found(self):
        system_path, filesystem_path = boot_repository.discover(self.live)
        self.assertEqual(system_path, "/media/usb/apks")
        self.assertTrue((filesystem_path / ".boot_repository").is_file())

    def test_the_embedded_closure_comes_from_the_index(self):
        _, closure = boot_repository.discovered_closure(self.live)
        self.assertEqual(sorted(closure), sorted(VERSIONS))
        self.assertEqual(closure["iw"], {"1.0-r0"})

    def test_a_medium_without_the_marker_is_not_a_repository(self):
        (self.live / "media/usb/apks/.boot_repository").unlink()
        with self.assertRaises(boot_repository.RepositoryError):
            boot_repository.discover(self.live)

    def test_a_directory_that_is_not_mounted_is_ignored(self):
        second = self.live / "mnt" / "elsewhere" / "apks" / "x86_64"
        second.mkdir(parents=True)
        (second.parent / ".boot_repository").write_text("")
        apkindex(second / "APKINDEX.tar.gz", VERSIONS)
        self.assertEqual(boot_repository.discover(self.live)[0], "/media/usb/apks")

    def test_two_mounted_repositories_are_refused_rather_than_guessed(self):
        (self.live / "proc" / "mounts").write_text(
            "/dev/sdb1 /media/usb vfat ro 0 0\n/dev/sr0 /media/cdrom iso9660 ro 0 0\n")
        repository = self.live / "media" / "cdrom" / "apks"
        repository.mkdir(parents=True)
        (repository / ".boot_repository").write_text("")
        apkindex(repository / "x86_64" / "APKINDEX.tar.gz", VERSIONS)
        with self.assertRaises(boot_repository.RepositoryError) as raised:
            boot_repository.discover(self.live)
        self.assertIn("unmount all but", str(raised.exception))

    def test_escaped_mount_points_are_read_correctly(self):
        (self.live / "proc" / "mounts").write_text("/dev/sdb1 /media/live\\040usb vfat ro 0 0\n")
        media = self.live / "media" / "live usb" / "apks"
        media.mkdir(parents=True)
        (media / ".boot_repository").write_text("")
        apkindex(media / "x86_64" / "APKINDEX.tar.gz", VERSIONS)
        self.assertEqual(boot_repository.discover(self.live)[0], "/media/live usb/apks")

    def test_package_filenames_are_a_fallback_identity_source(self):
        self.assertEqual(boot_repository.filename_identity("alpine-base-3.23.5-r0.apk"),
                         ("alpine-base", "3.23.5-r0"))
        self.assertEqual(boot_repository.filename_identity("mesa-dri-gallium-25.2.7-r0.apk"),
                         ("mesa-dri-gallium", "25.2.7-r0"))
        self.assertIsNone(boot_repository.filename_identity("not-a-package.txt"))

    def test_the_module_reports_one_path_and_nothing_else(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = boot_repository.main(["path", "--live-root", str(self.live)])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().strip(), "/media/usb/apks")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(boot_repository.main(["path", "--live-root", str(self.root / "none")]), 1)


@POSIX
class InstallerScriptTests(unittest.TestCase):
    """The installer keeps every guard, in order, and gates its success."""

    @classmethod
    def setUpClass(cls):
        cls.text = INSTALLER.read_text(encoding="utf-8")
        cls.lines = cls.text.splitlines()

    def index(self, fragment):
        for number, line in enumerate(self.lines):
            if fragment in line:
                return number
        self.fail(f"aios-install no longer contains {fragment!r}")

    def test_the_script_is_valid_and_root_only(self):
        subprocess.run(["sh", "-n", str(INSTALLER)], check=True, capture_output=True)
        subprocess.run(["sh", "-n", str(LIBRARY)], check=True, capture_output=True)
        self.assertIn('[ "$(id -u)" = 0 ]', self.text)
        self.assertIn("export PATH=/usr/sbin:/usr/bin:/sbin:/bin", self.text)
        self.assertIn(". /usr/local/lib/aios/install.sh", self.text)

    def test_every_guard_runs_before_any_destructive_command(self):
        confirmation = self.index("aios_confirmation_matches")
        for guard in ("aios_disk_guards", "aios_disk_identity", "aios_boot_repository"):
            self.assertLess(self.index(guard), confirmation, guard)
        for destructive in ("parted -s", "mkfs.ext4", "BOOTLOADER=grub setup-disk",
                            "grub-install"):
            self.assertGreater(self.index(destructive), confirmation, destructive)

    def test_the_identity_is_captured_before_the_operator_is_asked_to_confirm(self):
        self.assertIn('identity=$(aios_disk_identity "$disk")', self.text)
        self.assertLess(self.index('identity=$(aios_disk_identity'),
                        self.index("Type ERASE %s"))

    def test_every_guard_and_the_identity_are_rechecked_before_partitioning(self):
        recheck = self.index('aios_disk_unchanged "$disk" "$identity"')
        self.assertGreater(recheck, self.index("aios_confirmation_matches"))
        self.assertLess(recheck, self.index("parted -s"))
        self.assertIn('aios_disk_unchanged "$disk" "$identity" || exit 1', self.text)

    def test_the_exact_erase_prompt_is_unchanged(self):
        self.assertIn("printf 'Type ERASE %s to permanently erase this disk: ' \"$disk\"", self.text)

    def test_cleanup_unmounts_and_restores_the_repositories_on_every_exit_path(self):
        self.assertIn("trap cleanup EXIT INT TERM", self.text)
        self.assertLess(self.index("trap cleanup"), self.index("parted -s"))
        self.assertIn('umount "$mountdir/boot/efi"', self.text)
        self.assertIn('umount "$mountdir"', self.text)
        self.assertLess(self.index('aios_restore_repositories "$saved_repositories" || true'),
                        self.index('umount "$mountdir/boot/efi"'))

    def test_setup_disk_runs_against_the_local_repository_only(self):
        switch = self.index('aios_use_local_repository "$repository" "$saved_repositories"')
        setup = self.index("BOOTLOADER=grub setup-disk")
        restore = next(number for number, line in enumerate(self.lines)
                       if number > setup and "aios_restore_repositories" in line)
        self.assertLess(switch, setup)
        self.assertLess(restore, self.index("aios_finalize_target"))
        # The cleanup trap restores it too, so an interrupted setup-disk cannot
        # leave the live system pointing at a medium that is about to go away.
        self.assertLess(self.index('aios_restore_repositories "$saved_repositories" || true'),
                        switch)

    def test_success_is_reported_only_after_verification(self):
        finalize = self.index("aios_finalize_target")
        self.assertIn('aios_finalize_target "$mountdir" "$firmware" || exit 1', self.text)
        self.assertLess(finalize, self.index("Installation complete"))

    def test_no_activation_or_rollback_surface_is_offered(self):
        for absent in ("aios-baseline", "activate", "rollback"):
            self.assertNotIn(absent, self.text, absent)

    def test_the_checkpoint_helper_is_not_reachable_from_the_desktop(self):
        rules = DOAS_RULES.read_text(encoding="utf-8")
        self.assertIn("cmd /usr/local/sbin/aios-install args\n", rules)
        self.assertNotIn("aios-checkpoint", rules)
        self.assertNotIn("aios-baseline", rules)


@POSIX
class FinalizeTargetTests(unittest.TestCase):
    """The post-setup-disk sequence: parity, regeneration and the gate."""

    def finalize(self, failing=(), firmware="bios"):
        """Run aios_finalize_target with every external program recorded."""
        workspace = tempfile.TemporaryDirectory(prefix="aios-finalize-")
        self.addCleanup(workspace.cleanup)
        work = Path(workspace.name)
        live, target = work / "live", work / "target"
        for relative, text in (("etc/apk/world", "\n".join(WORLD) + "\n"),
                               ("etc/apk/repositories", "\n".join(REPOSITORIES) + "\n"),
                               ("usr/local/share/aios/world.hardware", "iw\n"),
                               ("home/aios/.profile", "")):
            path = live / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (target / "lib" / "modules" / RELEASE).mkdir(parents=True)
        (target / "boot").mkdir(parents=True)
        (target / "etc" / "apk").mkdir(parents=True)
        (target / "etc" / "apk" / "world").write_text("linux-lts\n")
        (target / "etc" / "mkinitfs").mkdir(parents=True)
        (target / "etc" / "mkinitfs" / "mkinitfs.conf").write_text(f'features="{FEATURES}"\n')
        binaries = work / "bin"
        binaries.mkdir()
        recorder = work / "commands"
        for name in ("depmod", "mkinitfs", "python3", "mountpoint", "sync", "tail"):
            stub = binaries / name
            stub.write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "{name} $*" >> "$AIOS_RECORD"\n'
                f'case " $AIOS_FAILING " in *" {name} "*) exit 1;; esac\nexit 0\n')
            stub.chmod(0o755)
        result = subprocess.run(
            ["sh", "-c",
             f'. "{LIBRARY}"\naios_finalize_target "$1" "$2" "$3"', "sh",
             str(target), firmware, str(live)],
            capture_output=True, text=True,
            env=dict(os.environ, PATH=f"{binaries}:{os.environ.get('PATH', '')}",
                     AIOS_RECORD=str(recorder), AIOS_FAILING=" ".join(failing)))
        recorded = recorder.read_text().splitlines() if recorder.exists() else []
        return result, recorded, live, target

    def test_a_complete_run_copies_parity_files_and_builds_every_artifact(self):
        result, recorded, live, target = self.finalize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (target / "etc/apk/world").read_text().splitlines(),
            sorted(INSTALLED_WORLD),
        )
        self.assertEqual((target / "etc/apk/repositories").read_text(),
                         (live / "etc/apk/repositories").read_text())
        self.assertEqual((target / "etc/aios-mode").read_text(), "installed\n")
        self.assertIn(f"depmod -b {target} {RELEASE}", recorded)
        self.assertIn(f"python3 -m aios.install_target recovery --target {target} "
                      f"--live-root {live}", recorded)
        self.assertIn(f"python3 -m aios.install_target verify --target {target} "
                      f"--firmware bios --live-root {live}", recorded)
        # Regeneration must happen before the readback that gates success.
        self.assertLess(recorded.index(f"depmod -b {target} {RELEASE}"),
                        next(index for index, line in enumerate(recorded) if "verify" in line))

    def test_the_initramfs_is_built_against_the_targets_own_configuration(self):
        _, recorded, _, target = self.finalize()
        self.assertIn(f"mkinitfs -b {target} -c {target}/etc/mkinitfs/mkinitfs.conf "
                      f"-o {target}/boot/initramfs-lts {RELEASE}", recorded)

    def test_a_target_without_an_initramfs_configuration_fails(self):
        result, recorded, _, target = self.finalize()
        (target / "etc" / "mkinitfs" / "mkinitfs.conf").unlink()
        repeat = subprocess.run(
            ["sh", "-c", f'. "{LIBRARY}"\naios_regenerate_boot_artifacts "$1"', "sh", str(target)],
            capture_output=True, text=True)
        self.assertNotEqual(repeat.returncode, 0)
        self.assertIn("no initramfs configuration", repeat.stdout)

    def test_the_uefi_firmware_mode_reaches_the_verifier(self):
        _, recorded, _, target = self.finalize(firmware="uefi")
        self.assertTrue(any("--firmware uefi" in line for line in recorded))

    def test_any_failed_step_fails_the_installation_and_never_reports_success(self):
        for failing, expected in (("depmod", "module dependency data"),
                                  ("mkinitfs", "initramfs"),
                                  ("mountpoint", "no longer mounted")):
            result, _, _, _ = self.finalize(failing=(failing,))
            self.assertNotEqual(result.returncode, 0, failing)
            self.assertIn(expected, result.stdout, failing)
            self.assertNotIn("Installation complete", result.stdout)

    def test_a_failed_verification_withholds_success(self):
        result, recorded, _, _ = self.finalize(failing=("python3",))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recovery boot entry", result.stdout)
        self.assertNotIn("Installation complete", result.stdout)

    def test_verification_failure_alone_is_enough_to_fail(self):
        # The recovery step succeeds, the readback does not: still no success.
        workspace = tempfile.TemporaryDirectory(prefix="aios-verify-gate-")
        self.addCleanup(workspace.cleanup)
        work = Path(workspace.name)
        binaries = work / "bin"
        binaries.mkdir()
        for name in ("mountpoint", "tail"):
            stub = binaries / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        python = binaries / "python3"
        python.write_text('#!/bin/sh\ncase "$*" in *verify*) exit 3;; esac\nexit 0\n')
        python.chmod(0o755)
        target = work / "target"
        (target / "var" / "log").mkdir(parents=True)
        result = subprocess.run(
            ["sh", "-c", f'. "{LIBRARY}"\naios_verify_target "$1" bios ""', "sh", str(target)],
            capture_output=True, text=True,
            env=dict(os.environ, PATH=f"{binaries}:{os.environ.get('PATH', '')}"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Verification report", result.stdout)


class RecoveryEntryTests(unittest.TestCase):
    """The recovery entry is derived from the generated normal entry."""

    def test_the_generated_entry_is_reused_with_safe_graphics_arguments(self):
        entry = build_recovery_entry(GENERATED_GRUB)
        self.assertIn(f'menuentry "{RECOVERY_TITLE}"', entry)
        body = install_target.first_menuentry_body(entry)
        arguments = " ".join(body).split()
        self.assertIn("aios.recovery", arguments)
        self.assertIn("nomodeset", arguments)
        self.assertIn("root=UUID=11111111-2222-3333-4444-555555555555", arguments)
        self.assertIn("console=ttyS0,115200", arguments)
        self.assertIn("/boot/initramfs-lts", arguments)
        self.assertIn("search", arguments)

    def test_no_baseline_region_is_written_into_the_installed_configuration(self):
        entry = build_recovery_entry(GENERATED_GRUB)
        for absent in ("BEGIN AIOS baselines", "aios-baseline", "set default="):
            self.assertNotIn(absent, entry, absent)

    def test_safe_graphics_arguments_are_never_duplicated(self):
        entry = build_recovery_entry(GENERATED_GRUB)
        twice = build_recovery_entry(GENERATED_GRUB.replace("quiet", "quiet aios.recovery nomodeset"))
        self.assertEqual(" ".join(entry.split()), " ".join(twice.split()))

    def test_an_unusable_generated_configuration_is_an_explicit_error(self):
        for text in ("", "set default=0\n", 'menuentry "x" {\n linux /boot/vmlinuz-lts\n}\n'):
            with self.assertRaises(TargetError):
                build_recovery_entry(text)

    def test_a_configuration_that_does_not_read_custom_cfg_gains_one_sourcing_block(self):
        with tempfile.TemporaryDirectory(prefix="aios-grub-") as directory:
            target = Path(directory)
            (target / "boot" / "grub").mkdir(parents=True)
            without = GENERATED_GRUB.split("### BEGIN /etc/grub.d/41_custom ###")[0]
            (target / install_target.GRUB_CONFIG).write_text(without)
            prepare_recovery_entry(target)
            prepare_recovery_entry(target)
            text = (target / install_target.GRUB_CONFIG).read_text()
            self.assertEqual(text.count(install_target.CUSTOM_BEGIN), 1)
            self.assertIn("source /boot/grub/custom.cfg", text)


class InitramfsReadbackTests(unittest.TestCase):
    """An initramfs is judged by what it contains, never by its timestamp."""

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="aios-initramfs-")
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.live, self.target = build_target(self.root)

    def test_configured_features_select_the_modules_that_must_be_present(self):
        inspection = initramfs.inspect(self.target, RELEASE, self.target / "boot/initramfs-lts")
        self.assertEqual(inspection["features"], ["base", "ext4"])
        self.assertEqual(inspection["expected_modules"], len(BOOT_MODULES))
        self.assertEqual(inspection["missing_modules"], [])
        self.assertTrue(inspection["has_init"])

    def test_a_missing_boot_module_is_detected_inside_the_image(self):
        write_initramfs(self.target, modules=("kernel/drivers/scsi/sd_mod.ko.gz",))
        inspection = initramfs.inspect(self.target, RELEASE, self.target / "boot/initramfs-lts")
        self.assertEqual(inspection["missing_modules"], ["kernel/fs/ext4/ext4.ko.gz"])

    def test_features_are_parsed_with_the_shell_assignment_rule(self):
        configuration = self.target / "etc/mkinitfs/mkinitfs.conf"
        configuration.write_text('# comment\nfeatures="ata base"\nfeatures="base ext4 nvme"\n')
        self.assertEqual(initramfs.read_features(configuration), ["base", "ext4", "nvme"])
        configuration.write_text("kernel_flavor=lts\n")
        with self.assertRaises(initramfs.InitramfsError):
            initramfs.read_features(configuration)

    def test_an_unreadable_container_is_missing_evidence_not_a_pass(self):
        image = self.target / "boot/initramfs-lts"
        image.write_bytes(b"\x28\xb5\x2f\xfd" + b"0" * 64)
        self.assertEqual(initramfs.compression(image), "zstd")
        with self.assertRaises(initramfs.InitramfsError):
            initramfs.entry_names(image)

    def test_a_truncated_archive_is_refused(self):
        image = self.target / "boot/initramfs-lts"
        image.write_bytes(image.read_bytes()[:64])
        with self.assertRaises((initramfs.InitramfsError, OSError, EOFError)):
            initramfs.entry_names(image)


@POSIX
class TargetVerificationTests(unittest.TestCase):
    """The verifier passes a complete target and fails closed on every gap."""

    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="aios-target-")
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.live, self.target = build_target(self.root)

    def verify(self, firmware="bios"):
        return verify_target(self.target, firmware, self.live)

    def assertCheckFails(self, name, report=None):
        report = report or self.verify()
        self.assertEqual(report["checks"][name]["status"], "fail", json.dumps(report, indent=2))
        self.assertNotEqual(report["status"], "pass")
        self.assertIn(name, report["failed"])

    def test_a_complete_target_passes(self):
        report = self.verify()
        self.assertEqual(report["status"], "pass", json.dumps(report, indent=2))
        self.assertEqual(report["failed"], [])
        self.assertEqual(report["incomplete"], [])
        self.assertEqual(install_target.exit_code(report), EXIT_OK)

    def test_a_missing_or_live_mode_marker_fails(self):
        (self.target / "etc" / "aios-mode").write_text("live\n")
        self.assertCheckFails("mode_marker")
        (self.target / "etc" / "aios-mode").unlink()
        self.assertCheckFails("mode_marker")

    def test_a_world_difference_fails(self):
        (self.target / "etc/apk/world").write_text("alpine-base\n")
        report = self.verify()
        self.assertCheckFails("apk_world_parity", report)

    def test_the_installed_kernel_must_remain_in_the_target_world(self):
        world = self.target / "etc/apk/world"
        world.write_text("\n".join(sorted(WORLD)) + "\n")
        report = self.verify()
        self.assertCheckFails("apk_world_parity", report)
        self.assertEqual(
            report["checks"]["apk_world_parity"]["missing_required_installed"]["sample"],
            ["linux-lts"],
        )

    def test_unexplained_target_world_atoms_fail(self):
        world = self.target / "etc/apk/world"
        world.write_text(world.read_text() + "unexpected-package\n")
        report = self.verify()
        self.assertCheckFails("apk_world_parity", report)
        self.assertEqual(
            report["checks"]["apk_world_parity"]["unexpected"]["sample"],
            ["unexpected-package"],
        )

    def test_every_world_atom_must_exist_in_the_target_package_database(self):
        database = self.target / "lib/apk/db/installed"
        database.write_text(database.read_text().replace("P:wireless-regdb", "P:something-else"))
        report = self.verify()
        self.assertCheckFails("apk_world_installed", report)
        self.assertIn("wireless-regdb", report["checks"]["apk_world_installed"]["missing"]["sample"])

    def test_an_installed_version_outside_the_embedded_closure_fails(self):
        database = self.target / "lib/apk/db/installed"
        database.write_text(database.read_text().replace("P:iw\nV:1.0-r0", "P:iw\nV:9.9-r9"))
        report = self.verify()
        self.assertCheckFails("embedded_closure_parity", report)
        self.assertIn("iw=9.9-r9",
                      report["checks"]["embedded_closure_parity"]["outside_closure"]["sample"])

    def test_a_world_wider_than_world_hardware_is_proved_too(self):
        # networkmanager is not in world.hardware; a wrong version of it must
        # still be caught by the closure proof.
        database = self.target / "lib/apk/db/installed"
        database.write_text(database.read_text().replace("P:networkmanager\nV:1.0-r0",
                                                         "P:networkmanager\nV:2.0-r0"))
        self.assertCheckFails("embedded_closure_parity")

    def test_an_unavailable_boot_repository_is_missing_evidence(self):
        (self.live / "proc" / "mounts").write_text("tmpfs /tmp tmpfs rw 0 0\n")
        report = self.verify()
        self.assertEqual(report["checks"]["embedded_closure_parity"]["status"], "incomplete")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(install_target.exit_code(report), EXIT_INCOMPLETE)

    def test_a_local_repository_left_in_the_target_fails(self):
        (self.target / "etc/apk/repositories").write_text(
            "/media/usb/apks\n" + "\n".join(REPOSITORIES) + "\n")
        report = self.verify()
        self.assertCheckFails("apk_repositories_parity", report)
        self.assertEqual(
            report["checks"]["apk_repositories_parity"]["local_repository_entries"]["sample"],
            ["/media/usb/apks"])

    def test_a_target_asking_different_repositories_fails(self):
        (self.target / "etc/apk/repositories").write_text("https://example.invalid/main\n")
        self.assertCheckFails("apk_repositories_parity")

    def test_a_missing_firmware_or_regulatory_file_fails(self):
        (self.target / "usr" / "bin" / "wireless-regdb").unlink()
        report = self.verify()
        self.assertCheckFails("hardware_files_present", report)
        self.assertEqual(report["checks"]["hardware_files_present"]["missing"]["count"], 1)

    def test_a_missing_kernel_or_module_release_fails(self):
        (self.target / "boot" / "vmlinuz-lts").unlink()
        self.assertCheckFails("kernel_image")
        self.setUp()
        (self.target / "lib/modules" / RELEASE / MODULES[2]).unlink()
        report = self.verify()
        self.assertCheckFails("kernel_modules", report)
        self.assertEqual(report["checks"]["kernel_modules"]["missing"]["sample"], [MODULES[2]])

    def test_a_module_with_different_content_fails_even_at_the_same_count(self):
        path = self.target / "lib/modules" / RELEASE / MODULES[0]
        path.write_bytes(b"tampered-but-same-length!!")
        report = self.verify()
        self.assertCheckFails("kernel_modules", report)
        self.assertEqual(report["checks"]["kernel_modules"]["target_modules"],
                         report["checks"]["kernel_modules"]["live_modules"])
        self.assertEqual(report["checks"]["kernel_modules"]["altered"]["sample"], [MODULES[0]])

    def test_an_extra_module_the_live_image_never_had_fails(self):
        extra = self.target / "lib/modules" / RELEASE / "kernel/fs/xfs/xfs.ko.gz"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"module")
        self.assertCheckFails("kernel_modules")

    def test_a_release_the_live_image_does_not_have_fails(self):
        current = self.target / "lib/modules" / RELEASE
        current.rename(self.target / "lib/modules" / "6.99.0-0-lts")
        report = self.verify()
        self.assertCheckFails("kernel_modules", report)
        self.assertFalse(report["checks"]["kernel_modules"]["release_matches_live"])

    def test_missing_module_dependency_data_fails(self):
        (self.target / "lib/modules" / RELEASE / "modules.dep").unlink()
        self.assertCheckFails("module_dependency_data")

    def test_an_initramfs_without_the_configured_modules_fails(self):
        write_initramfs(self.target, modules=("kernel/drivers/scsi/sd_mod.ko.gz",))
        report = self.verify()
        self.assertCheckFails("initramfs", report)
        self.assertEqual(report["checks"]["initramfs"]["missing_modules"]["sample"],
                         ["kernel/fs/ext4/ext4.ko.gz"])

    def test_a_fresh_but_empty_initramfs_fails(self):
        write_initramfs(self.target, modules=(), init=True)
        self.assertCheckFails("initramfs")
        (self.target / "boot" / "initramfs-lts").unlink()
        self.assertCheckFails("initramfs")

    def test_an_initramfs_without_an_init_fails(self):
        write_initramfs(self.target, init=False)
        report = self.verify()
        self.assertCheckFails("initramfs", report)
        self.assertFalse(report["checks"]["initramfs"]["has_init"])

    def test_a_feature_with_no_definition_on_the_target_fails(self):
        (self.target / "etc/mkinitfs/mkinitfs.conf").write_text('features="base ext4 nvme"\n')
        report = self.verify()
        self.assertCheckFails("initramfs", report)
        self.assertEqual(report["checks"]["initramfs"]["features_without_definition"]["sample"],
                         ["nvme"])

    def test_a_missing_recovery_entry_fails(self):
        (self.target / install_target.GRUB_CUSTOM).unlink()
        self.assertCheckFails("boot_entries")

    def test_a_generated_entry_without_a_root_identification_fails(self):
        configuration = self.target / install_target.GRUB_CONFIG
        configuration.write_text(GENERATED_GRUB.replace(
            "root=UUID=11111111-2222-3333-4444-555555555555 rw", "rw"))
        report = self.verify()
        self.assertCheckFails("boot_entries", report)
        self.assertFalse(report["checks"]["boot_entries"]["normal_entry"])

    def test_a_generated_entry_without_the_installed_initramfs_fails(self):
        configuration = self.target / install_target.GRUB_CONFIG
        configuration.write_text(GENERATED_GRUB.replace("initrd /boot/initramfs-lts",
                                                        "echo no-initrd"))
        self.assertCheckFails("boot_entries")

    def test_a_recovery_entry_the_configuration_never_reads_fails(self):
        configuration = self.target / install_target.GRUB_CONFIG
        configuration.write_text(GENERATED_GRUB.split("### BEGIN /etc/grub.d/41_custom ###")[0])
        report = self.verify()
        self.assertCheckFails("boot_entries", report)
        self.assertFalse(report["checks"]["boot_entries"]["recovery_entry_sourced"])

    def test_each_firmware_mode_requires_its_own_bootloader_artifacts(self):
        report = self.verify(firmware="uefi")
        self.assertCheckFails("bootloader_artifacts", report)
        self.setUp()
        self.live, self.target = build_target(self.root / "uefi", firmware="uefi")
        self.assertEqual(self.verify(firmware="uefi")["status"], "pass")
        self.assertCheckFails("bootloader_artifacts", self.verify(firmware="bios"))

    def test_a_partial_grub_platform_directory_fails(self):
        (self.target / "boot/grub/i386-pc/linux.mod").unlink()
        report = self.verify()
        self.assertCheckFails("bootloader_artifacts", report)
        self.assertEqual(report["checks"]["bootloader_artifacts"]["missing"]["sample"],
                         ["boot/grub/i386-pc/linux.mod"])

    def test_a_missing_required_service_fails(self):
        (self.target / "etc/runlevels/default/networkmanager").unlink()
        report = self.verify()
        self.assertCheckFails("required_services", report)
        self.assertIn("default/networkmanager", report["checks"]["required_services"]["missing"]["sample"])

    def test_a_runlevel_entry_that_is_not_a_symlink_fails(self):
        entry = self.target / "etc/runlevels/default/polkit"
        entry.unlink()
        entry.write_text("not a symlink")
        report = self.verify()
        self.assertCheckFails("required_services", report)
        self.assertIn("default/polkit", report["checks"]["required_services"]["missing"]["sample"])

    def test_a_runlevel_symlink_to_the_wrong_executable_fails(self):
        entry = self.target / "etc/runlevels/default/networkmanager"
        entry.unlink()
        entry.symlink_to("/bin/false")
        report = self.verify()
        self.assertCheckFails("required_services", report)
        self.assertIn(
            "default/networkmanager",
            report["checks"]["required_services"]["unresolved"]["sample"],
        )

    def test_a_service_whose_init_script_is_absent_or_not_executable_fails(self):
        (self.target / "etc/init.d/dbus").chmod(0o644)
        report = self.verify()
        self.assertCheckFails("required_services", report)
        self.assertIn("default/dbus", report["checks"]["required_services"]["unresolved"]["sample"])
        self.setUp()
        (self.target / "etc/init.d/elogind").unlink()
        self.assertCheckFails("required_services")

    def test_a_live_only_service_left_in_a_runlevel_fails(self):
        script = self.target / "etc/init.d/modloop"
        script.write_text("#!/sbin/openrc-run\n")
        script.chmod(0o755)
        (self.target / "etc/runlevels/sysinit/modloop").symlink_to("/etc/init.d/modloop")
        report = self.verify()
        self.assertCheckFails("required_services", report)
        self.assertEqual(report["checks"]["required_services"]["live_only_services"]["sample"],
                         ["sysinit/modloop"])

    def test_a_missing_aios_userspace_file_fails(self):
        (self.target / "usr/local/lib/aios/install.sh").unlink()
        self.assertCheckFails("aios_userspace")

    def test_missing_evidence_is_never_a_pass(self):
        empty = self.root / "empty"
        empty.mkdir()
        report = verify_target(empty, "bios", self.live)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(install_target.exit_code(report), EXIT_INCOMPLETE)
        missing = verify_target(self.root / "absent", "bios", self.live)
        self.assertEqual(missing["status"], "incomplete")

    def test_a_failed_check_is_reported_as_failed_not_incomplete(self):
        (self.target / "etc" / "aios-mode").write_text("live\n")
        self.assertEqual(install_target.exit_code(self.verify()), EXIT_FAILED)

    def test_an_unknown_firmware_mode_is_refused(self):
        with self.assertRaises(TargetError):
            verify_target(self.target, "coreboot", self.live)

    def test_the_report_is_bounded_and_carries_no_device_or_user_data(self):
        database = self.target / "lib/apk/db/installed"
        extra = "".join(f"\nP:iw-extra-{index}\nV:1\nF:usr/bin\nR:missing-{index}\n"
                        for index in range(200))
        database.write_text(database.read_text() + extra)
        report = self.verify()
        text = install_target.serialize(report)
        self.assertLessEqual(len(text.encode("utf-8")), install_target.MAX_REPORT_BYTES)
        self.assertNotIn(str(self.target), text)
        self.assertNotIn("/dev/", text)
        self.assertNotIn("SERIAL", text.upper())

    def test_the_report_is_written_into_the_target_at_a_fixed_path(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = install_target.main(["verify", "--target", str(self.target),
                                        "--firmware", "bios", "--live-root", str(self.live)])
        self.assertEqual(code, EXIT_OK)
        stored = json.loads((self.target / install_target.REPORT_PATH).read_text())
        self.assertEqual(stored["status"], "pass")
        self.assertEqual(stored["kind"], install_target.REPORT_KIND)


@POSIX
class ImageContractTests(unittest.TestCase):
    """The image carries what the installer and verifier need."""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="aios-apkovl-") as directory:
            work = Path(directory)
            (work / "stage").mkdir()
            environment = dict(
                os.environ, AIOS_STAGE_DIR=str(work / "stage"), AIOS_OVERLAY_DIR=str(OVERLAY),
                AIOS_REPOSITORIES="https://example.invalid/main", AIOS_WORLD_IDENTITY="",
                AIOS_WORLD_HARDWARE=str(APKS / "world.hardware"),
                AIOS_APKOVL_SCRIPT="genapkovl-aios.sh")
            for name in ("BASE", "X11", "VM", "DEVEL", "AI"):
                environment["AIOS_WORLD_" + name] = str(APKS / ("world." + name.lower()))
            subprocess.run(["sh", str(GENAPKOVL), "aios"], cwd=work, check=True,
                           env=environment, capture_output=True)
            cls.members = {}
            with tarfile.open(work / "aios.apkovl.tar.gz") as archive:
                for member in archive.getmembers():
                    name = member.name.lstrip("./")
                    if member.isfile():
                        cls.members[name] = (archive.extractfile(member).read().decode(), member.mode)

    def test_the_hardware_world_reference_ships_for_the_verifier(self):
        text, mode = self.members["usr/local/share/aios/world.hardware"]
        expected = [line.strip() for line in (APKS / "world.hardware").read_text().splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        self.assertEqual(text.splitlines(), expected)
        self.assertEqual(mode & 0o777, 0o644)
        world, _ = self.members["etc/apk/world"]
        self.assertTrue(set(expected) <= set(world.split()))

    def test_the_installer_library_and_checkpoint_helper_ship(self):
        self.assertIn("usr/local/lib/aios/install.sh", self.members)
        self.assertIn("aios_disk_holds_live_media", self.members["usr/local/lib/aios/install.sh"][0])
        self.assertIn("aios_disk_unchanged", self.members["usr/local/lib/aios/install.sh"][0])
        _, mode = self.members["usr/local/sbin/aios-checkpoint"]
        self.assertTrue(mode & 0o111, "aios-checkpoint must be executable")
        _, installer_mode = self.members["usr/local/sbin/aios-install"]
        self.assertTrue(installer_mode & 0o111)
        self.assertNotIn("usr/local/sbin/aios-baseline", self.members)


class DisposableDiskHarnessTests(unittest.TestCase):
    """The QEMU harness models a real install without any host disk input."""

    @classmethod
    def setUpClass(cls):
        cls.text = BOOT_TEST.read_text(encoding="utf-8")

    def test_one_option_owns_the_whole_install_and_boot_lifecycle(self):
        self.assertIn('parser.add_argument("--install-and-boot"', self.text)
        self.assertIn('INSTALL_TARGETS = {"sata": "/dev/sda", "nvme": "/dev/nvme0n1"}', self.text)
        for removed in ("--install-disk", "--boot-from"):
            self.assertNotIn(removed, self.text, removed)

    def test_the_harness_creates_and_reuses_one_disposable_image(self):
        self.assertIn('image = Path(directory) / DISK_IMAGE', self.text)
        self.assertIn('"qemu-img", "create", "-f", "qcow2"', self.text)
        self.assertEqual(self.text.count("create_disposable_disk(directory, args.disk_size_gb)"), 1)
        self.assertEqual(self.text.count("attachment = disk_options(image"), 1)
        self.assertEqual(self.text.count("*attachment"), 2)

    def test_no_host_device_or_image_path_can_be_supplied(self):
        for forbidden in ("--disk-path", "--disk-image", "file=/dev", "if=pflash,format=raw,file=",
                          "-hda", "-hdb", "raw,file=/dev"):
            self.assertNotIn(forbidden, self.text, forbidden)
        self.assertNotIn("/dev/sd", self.text.replace('"sata": "/dev/sda"', ""))

    def test_the_installer_input_is_exactly_the_disk_and_its_confirmation(self):
        self.assertIn("printf '%s\\\\nERASE %s\\\\n'", self.text)
        self.assertIn("/usr/local/sbin/aios-install", self.text)
        self.assertIn("grep -q 'Installation complete'", self.text)

    def test_the_second_phase_boots_the_same_disk_without_the_iso(self):
        self.assertIn('["-boot", "c", *attachment]', self.text)
        self.assertIn("shut_down=True", self.text)
        self.assertIn("grep -qx installed /etc/aios-mode", self.text)
        self.assertIn("aios-install-verification.json", self.text)
        # Every guest runs without a network device attached.
        self.assertIn('"-nic", "none"', self.text)
        self.assertNotIn("-netdev", self.text)


class PythonSourceTests(unittest.TestCase):
    """No unsafe activation surface survives in the installation modules."""

    MODULES = ("install_target.py", "checkpoint.py", "boot_repository.py", "initramfs.py",
               "durable.py")

    def test_no_installation_module_offers_activation_or_rollback(self):
        for name in self.MODULES:
            text = (APPS / "aios" / name).read_text(encoding="utf-8")
            self.assertNotIn("def activate", text, name)
            self.assertNotIn("def rollback", text, name)
            self.assertNotIn("BASELINE_BEGIN", text, name)
            self.assertNotIn("replace_symlink", text, name)

    def test_the_baseline_module_and_helper_are_gone(self):
        self.assertFalse((APPS / "aios" / "baseline.py").exists())
        self.assertFalse((OVERLAY / "usr/local/sbin/aios-baseline").exists())
        self.assertFalse((ROOT / "tests" / "test_baseline_rollback.py").exists())


if __name__ == "__main__":
    unittest.main()
