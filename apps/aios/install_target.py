"""Installed-target preparation and fail-closed verification for aios-install.

Two fixed operations run against one explicitly named, already mounted target
root. There is no agent, MCP or `doas` path to this module: `aios-install`
invokes it with the directory it mounted itself, and the tests invoke it with a
fixture root. It takes no device paths, package names, URLs or commands.

- `recovery` adds the safe-graphics recovery menu entry to the GRUB
  configuration `setup-disk` generated, deriving its kernel and initrd from the
  generated normal entry rather than inventing a boot path.
- `verify` reads the mounted target back and decides whether the installation
  may be reported as successful. Every check fails closed: missing evidence is
  never a pass, and the result is a bounded machine-readable report.

Two references are used, both local. The running live root is the reference for
the kernel, its modules and the package world. The booted medium's own `/apks`
repository — discovered, never supplied — is the reference for what the
installation was allowed to install: every installed version must appear in
that embedded closure, which is what makes "this came from the ISO" an
assertion rather than a claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from . import boot_repository, initramfs
from .durable import write_atomic

SCHEMA_VERSION = 2
REPORT_KIND = "aios-installed-target-verification"

KERNEL_IMAGE = "boot/vmlinuz-lts"
INITRAMFS = "boot/initramfs-lts"
MODULES_ROOT = "lib/modules"
GRUB_CONFIG = "boot/grub/grub.cfg"
GRUB_CUSTOM = "boot/grub/custom.cfg"
APK_WORLD = "etc/apk/world"
APK_REPOSITORIES = "etc/apk/repositories"
APK_INSTALLED_DB = "lib/apk/db/installed"
MODE_MARKER = "etc/aios-mode"
HARDWARE_WORLD = "usr/local/share/aios/world.hardware"
INIT_SCRIPTS = "etc/init.d"
RUNLEVELS = "etc/runlevels"
# Fixed relative location of the report inside the target. Not an argument.
REPORT_PATH = "var/log/aios-install-verification.json"

MODULE_SUFFIXES = (".ko", ".ko.gz", ".ko.xz", ".ko.zst")
MODULE_INDEX_FILES = ("modules.dep", "modules.alias", "modules.builtin", "modules.symbols")
DIGEST_CHUNK = 1 << 20
# apk world atoms may carry a version constraint or a repository tag.
ATOM_SEPARATORS = ("<", ">", "=", "~")

# Artifacts each firmware mode needs to boot with the installation medium gone,
# in the layout grub-install really produces: the platform directory with its
# core image and the modules GRUB loads before it can read its configuration,
# plus the removable EFI path for UEFI.
BOOTLOADER_ARTIFACTS = {
    "uefi": ("boot/efi/EFI/BOOT/BOOTX64.EFI", "boot/grub/x86_64-efi/normal.mod",
             "boot/grub/x86_64-efi/linux.mod", "boot/grub/x86_64-efi/ext2.mod",
             "boot/grub/x86_64-efi/part_gpt.mod"),
    "bios": ("boot/grub/i386-pc/core.img", "boot/grub/i386-pc/boot.img",
             "boot/grub/i386-pc/normal.mod", "boot/grub/i386-pc/linux.mod",
             "boot/grub/i386-pc/ext2.mod", "boot/grub/i386-pc/part_gpt.mod"),
}
FIRMWARE_MODES = tuple(sorted(BOOTLOADER_ARTIFACTS))

# OpenRC services the installed desktop needs, mirroring the runlevels the
# apkovl generator creates.
REQUIRED_SERVICES = {
    "sysinit": ("devfs", "dmesg", "udev", "udev-trigger", "hwdrivers"),
    "boot": ("modules", "sysctl", "hostname", "bootmisc", "syslog", "networking"),
    "default": ("dbus", "elogind", "polkit", "networkmanager", "aios-init"),
    "shutdown": ("mount-ro", "killprocs", "savecache"),
}
# `modloop` mounts the live medium's compressed module image. setup-disk
# removes it from every runlevel on a sys install, and an installed system that
# still carried it would fail that service on every boot.
LIVE_ONLY_SERVICES = ("modloop",)
REQUIRED_INSTALLED_WORLD = ("linux-lts",)

RECOVERY_TITLE = "AIOS recovery (safe graphics)"
RECOVERY_ARGUMENTS = ("aios.recovery", "nomodeset")
CUSTOM_BEGIN = "### BEGIN AIOS recovery entry ###"
CUSTOM_END = "### END AIOS recovery entry ###"

MAX_LISTED = 8
MAX_REPORT_BYTES = 8192

EXIT_OK = 0
EXIT_FAILED = 3
EXIT_INCOMPLETE = 4

PASS, FAIL, INCOMPLETE = "pass", "fail", "incomplete"


class TargetError(RuntimeError):
    """A fixed operation could not be completed on the target."""


def _bounded(values):
    """Cap a list so one broken package cannot produce an unbounded report."""
    listed = sorted(values)
    if len(listed) <= MAX_LISTED:
        return {"count": len(listed), "sample": listed}
    return {"count": len(listed), "sample": listed[:MAX_LISTED], "truncated": True}


def _read_lines(path):
    """Non-empty, non-comment lines of a small configuration file."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def read_atoms(path):
    """Package atoms of a world file, as a set."""
    return set(_read_lines(path))


def atom_name(atom):
    """The package name an apk world atom selects."""
    name = atom.split("@", 1)[0]
    for separator in ATOM_SEPARATORS:
        name = name.split(separator, 1)[0]
    return name.lstrip("!").strip()


def _digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(DIGEST_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_installed_packages(root, wanted=None):
    """Parse an apk installed database.

    Returns `(names, files)`: every installed package name, and the files owned
    by the packages in `wanted` (all of them when `wanted` is None).
    """
    names, files = set(), {}
    current, directory = None, ""
    with open(Path(root) / APK_INSTALLED_DB, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                current, directory = None, ""
                continue
            key, _, value = line.partition(":")
            if key == "P":
                current = value
                names.add(current)
                if wanted is None or current in wanted:
                    files[current] = []
            elif key == "F":
                directory = value
            elif key == "R" and current in files:
                files[current].append(f"{directory}/{value}" if directory else value)
    return names, files


def read_package_versions(root):
    """`{package: version}` from an apk installed database."""
    versions, current = {}, None
    with open(Path(root) / APK_INSTALLED_DB, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                current = None
                continue
            key, _, value = line.partition(":")
            if key == "P":
                current = value
            elif key == "V" and current is not None:
                versions[current] = value
    return versions


def module_releases(root):
    """Kernel module release directories present under a root."""
    modules = Path(root) / MODULES_ROOT
    if not modules.is_dir():
        return []
    return sorted(entry.name for entry in modules.iterdir() if entry.is_dir())


def module_inventory(directory):
    """`{relative path: sha256}` for every kernel module under a release."""
    directory = Path(directory)
    inventory = {}
    for parent, _, filenames in os.walk(str(directory)):
        for name in sorted(filenames):
            if not name.endswith(MODULE_SUFFIXES):
                continue
            path = Path(parent) / name
            inventory[path.relative_to(directory).as_posix()] = _digest_file(path)
    return inventory


# --- recovery boot entry ---------------------------------------------------

def menuentry_blocks(text):
    """`(title, body-lines)` for every complete `menuentry { ... }` block."""
    blocks, lines = [], text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("menuentry "):
            continue
        depth = line.count("{") - line.count("}")
        if depth <= 0:
            continue
        title = stripped.split('"')[1] if stripped.count('"') >= 2 else ""
        body = []
        for following in lines[index + 1:]:
            depth += following.count("{") - following.count("}")
            if depth <= 0:
                blocks.append((title, body))
                break
            body.append(following)
    return blocks


def first_menuentry_body(text):
    """Lines inside the first `menuentry { ... }` block of a GRUB config."""
    blocks = menuentry_blocks(text)
    return blocks[0][1] if blocks else None


def build_recovery_entry(grub_config_text):
    """Build the recovery menu entry from the generated normal entry.

    The whole body of the generated entry is reused, so the root search,
    filesystem module loads and initrd stay exactly what `grub-mkconfig`
    decided. Only the kernel arguments gain `aios.recovery nomodeset`.
    """
    body = first_menuentry_body(grub_config_text)
    if body is None:
        raise TargetError("The generated GRUB configuration has no complete menu entry to base recovery on.")
    rewritten, has_linux, has_initrd = [], False, False
    for line in body:
        stripped = line.strip()
        if not stripped:
            continue
        keyword = stripped.split(None, 1)[0]
        if keyword in ("linux", "linux16"):
            arguments = [word for word in stripped.split()[1:] if word not in RECOVERY_ARGUMENTS]
            if len(arguments) < 1:
                raise TargetError("The generated GRUB entry names no kernel image.")
            stripped = " ".join([keyword, *arguments, *RECOVERY_ARGUMENTS])
            has_linux = True
        elif keyword in ("initrd", "initrd16"):
            has_initrd = True
        rewritten.append("\t" + stripped)
    if not has_linux or not has_initrd:
        raise TargetError("The generated GRUB entry is missing its kernel or initrd line.")
    return "".join([
        "# Managed by AIOS. Regenerated by aios-install; edits are not preserved.\n",
        f'menuentry "{RECOVERY_TITLE}" {{\n',
        "\n".join(rewritten),
        "\n}\n",
    ])


def _sourcing_block():
    return "\n".join([
        "",
        CUSTOM_BEGIN,
        "if [ -f ${config_directory}/custom.cfg ]; then",
        "  source ${config_directory}/custom.cfg",
        'elif [ -z "${config_directory}" -a -e /boot/grub/custom.cfg ]; then',
        "  source /boot/grub/custom.cfg",
        "fi",
        CUSTOM_END,
        "",
    ])


def prepare_recovery_entry(target):
    """Write the recovery entry and make sure the GRUB configuration reads it."""
    target = Path(target)
    configuration = target / GRUB_CONFIG
    if not configuration.is_file():
        raise TargetError("setup-disk produced no GRUB configuration on the target.")
    text = configuration.read_text(encoding="utf-8", errors="replace")
    write_atomic(target / GRUB_CUSTOM, build_recovery_entry(text))
    # grub-mkconfig's own 41_custom already sources custom.cfg. When it does
    # not, add one marked, idempotent block rather than rewriting the file.
    if "custom.cfg" not in text:
        write_atomic(configuration, text.rstrip("\n") + "\n" + _sourcing_block())
    return {"recovery_entry": RECOVERY_TITLE, "sourced": True}


# --- verification ----------------------------------------------------------

def _check_target_root(target, results):
    missing = [name for name in ("etc", "boot", "lib") if not (target / name).is_dir()]
    if missing:
        results["target_root"] = {"status": INCOMPLETE, "missing_directories": _bounded(missing)}
        return False
    results["target_root"] = {"status": PASS}
    return True


def _check_mode_marker(target, results):
    path = target / MODE_MARKER
    value = path.read_text(encoding="utf-8", errors="replace").strip() if path.is_file() else None
    results["mode_marker"] = {
        "status": PASS if value == "installed" else FAIL,
        "value": value if value in ("live", "installed") else "unknown",
    }


def _check_apk_world_parity(target, live, results):
    live_path, target_path = live / APK_WORLD, target / APK_WORLD
    if not live_path.is_file():
        results["apk_world_parity"] = {"status": INCOMPLETE, "reason": "live_reference_missing"}
        return set()
    if not target_path.is_file():
        results["apk_world_parity"] = {"status": FAIL, "reason": "target_file_missing"}
        return set()
    expected, actual = read_atoms(live_path), read_atoms(target_path)
    required = set(REQUIRED_INSTALLED_WORLD)
    missing = expected - actual
    missing_required = required - actual
    unexpected = actual - expected - required
    results["apk_world_parity"] = {
        "status": PASS if not (missing or missing_required or unexpected) else FAIL,
        "expected_entries": len(expected),
        "target_entries": len(actual),
        "required_installed_entries": _bounded(required),
        "missing": _bounded(missing),
        "missing_required_installed": _bounded(missing_required),
        "unexpected": _bounded(unexpected),
    }
    return actual


def _check_apk_repositories(target, live, results):
    """The installed system must ask the live remote URLs, not the medium.

    The ISO's own `/apks` directory disappears when the medium is removed, so a
    local repository path left in the target would break every later `apk`
    invocation. The installed configuration therefore has to match the live
    *configured* repositories and contain no local path.
    """
    live_path, target_path = live / APK_REPOSITORIES, target / APK_REPOSITORIES
    if not live_path.is_file():
        results["apk_repositories_parity"] = {"status": INCOMPLETE,
                                              "reason": "live_reference_missing"}
        return
    if not target_path.is_file():
        results["apk_repositories_parity"] = {"status": FAIL, "reason": "target_file_missing"}
        return
    expected, actual = set(_read_lines(live_path)), set(_read_lines(target_path))
    local = sorted(entry for entry in actual if entry.startswith("/"))
    results["apk_repositories_parity"] = {
        "status": PASS if expected == actual and not local else FAIL,
        "expected_entries": len(expected),
        "target_entries": len(actual),
        "missing": _bounded(expected - actual),
        "unexpected": _bounded(actual - expected),
        "local_repository_entries": _bounded(local),
    }


def _check_world_installed(target, world, results):
    """Every atom the installed world selects is really in the target's db."""
    if not (target / APK_INSTALLED_DB).is_file():
        results["apk_world_installed"] = {"status": INCOMPLETE,
                                          "reason": "target_package_database_missing"}
        return {}
    versions = read_package_versions(target)
    if not world:
        results["apk_world_installed"] = {"status": INCOMPLETE, "reason": "target_world_missing"}
        return versions
    required = {atom_name(atom) for atom in world if not atom.startswith("!")}
    absent = sorted(name for name in required if name not in versions)
    results["apk_world_installed"] = {
        "status": PASS if not absent else FAIL,
        "required": len(required),
        "installed": len(versions),
        "missing": _bounded(absent),
    }
    return versions


def _check_embedded_closure(live, versions, results):
    """Proof the installation came from the medium's own package closure.

    Every installed `name=version` must be one the booted ISO can serve. A
    version that is not in the embedded closure could only have come from a
    network mirror, which an offline installation must never have reached.
    """
    if not versions:
        results["embedded_closure_parity"] = {"status": INCOMPLETE,
                                              "reason": "target_package_database_missing"}
        return
    try:
        _, closure = boot_repository.discovered_closure(live)
    except (boot_repository.RepositoryError, OSError) as error:
        message = error.strerror if isinstance(error, OSError) else str(error)
        results["embedded_closure_parity"] = {"status": INCOMPLETE,
                                              "reason": "boot_repository_unavailable",
                                              "detail": message}
        return
    outside = sorted(f"{name}={version}" for name, version in versions.items()
                     if version not in closure.get(name, ()))
    results["embedded_closure_parity"] = {
        "status": PASS if not outside else FAIL,
        "installed_packages": len(versions),
        "closure_packages": len(closure),
        "outside_closure": _bounded(outside),
    }


def _check_hardware_files(target, live, versions, results):
    """Firmware, regulatory data and diagnostics really landed on the target."""
    reference = live / HARDWARE_WORLD
    if not reference.is_file():
        results["hardware_files_present"] = {"status": INCOMPLETE,
                                             "reason": "hardware_world_reference_missing"}
        return
    if not versions:
        results["hardware_files_present"] = {"status": INCOMPLETE,
                                             "reason": "target_package_database_missing"}
        return
    atoms = {atom_name(atom) for atom in read_atoms(reference)}
    _, owned = read_installed_packages(target, wanted=atoms)
    missing_files, checked = [], 0
    for package in sorted(owned):
        for relative in owned[package]:
            checked += 1
            if not (target / relative).exists():
                missing_files.append(relative)
    results["hardware_files_present"] = {
        "status": FAIL if missing_files else (PASS if checked else INCOMPLETE),
        "packages": len(atoms),
        "files_checked": checked,
        "missing": _bounded(missing_files),
    }
    if not checked:
        results["hardware_files_present"]["reason"] = "no_owned_files_recorded"


def _check_kernel_and_modules(target, live, results):
    kernel = target / KERNEL_IMAGE
    results["kernel_image"] = {
        "status": PASS if kernel.is_file() and kernel.stat().st_size > 0 else FAIL,
        "path": KERNEL_IMAGE,
    }
    target_releases, live_releases = module_releases(target), module_releases(live)
    if not target_releases:
        results["kernel_modules"] = {"status": FAIL, "reason": "no_module_release_installed"}
        results["module_dependency_data"] = {"status": FAIL,
                                             "reason": "no_module_release_installed"}
        return None
    release = target_releases[0]
    if release not in live_releases:
        results["kernel_modules"] = {
            "status": FAIL if live_releases else INCOMPLETE,
            "reason": "release_differs_from_live" if live_releases else "live_reference_missing",
            "release_matches_live": False,
        }
    else:
        # Exact content comparison: same relative paths, same bytes. A count
        # would accept a truncated or substituted module tree.
        target_inventory = module_inventory(target / MODULES_ROOT / release)
        live_inventory = module_inventory(live / MODULES_ROOT / release)
        missing = sorted(set(live_inventory) - set(target_inventory))
        unexpected = sorted(set(target_inventory) - set(live_inventory))
        altered = sorted(name for name in set(live_inventory) & set(target_inventory)
                         if live_inventory[name] != target_inventory[name])
        results["kernel_modules"] = {
            "status": PASS if target_inventory and not (missing or unexpected or altered) else FAIL,
            "release_matches_live": True,
            "target_modules": len(target_inventory),
            "live_modules": len(live_inventory),
            "missing": _bounded(missing),
            "unexpected": _bounded(unexpected),
            "altered": _bounded(altered),
        }
    _check_module_dependency_data(target, release, results)
    return release


def _check_module_dependency_data(target, release, results):
    directory = target / MODULES_ROOT / release
    missing = [name for name in MODULE_INDEX_FILES if not (directory / name).is_file()]
    if missing:
        results["module_dependency_data"] = {"status": FAIL,
                                             "missing_index_files": _bounded(missing)}
        return
    dependency_file = directory / "modules.dep"
    entries, dangling = 0, []
    with open(dependency_file, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            name, separator, _ = line.partition(":")
            if not separator:
                continue
            entries += 1
            if not (directory / name.strip()).exists():
                dangling.append(name.strip())
    results["module_dependency_data"] = {
        "status": FAIL if (dangling or not entries) else PASS,
        "entries": entries,
        "dangling": _bounded(dangling),
    }


def _check_initramfs(target, release, results):
    """The image must carry what the target's own mkinitfs configuration asks.

    `setup-disk` writes `/etc/mkinitfs/mkinitfs.conf` for the root filesystem
    and the controller this machine boots from, and `aios-install` builds the
    initramfs with that file. Reading the produced image back and looking for
    those modules is the only check that distinguishes a usable initramfs from
    a freshly timestamped one.
    """
    image = target / INITRAMFS
    if not image.is_file() or image.stat().st_size == 0:
        results["initramfs"] = {"status": FAIL, "reason": "missing_or_empty"}
        return
    if release is None:
        results["initramfs"] = {"status": INCOMPLETE, "reason": "no_module_release_installed"}
        return
    try:
        inspection = initramfs.inspect(target, release, image)
    except initramfs.InitramfsError as error:
        results["initramfs"] = {"status": INCOMPLETE, "reason": "initramfs_unreadable",
                                "detail": str(error)}
        return
    usable = (inspection["has_init"] and inspection["expected_modules"] > 0
              and not inspection["missing_modules"]
              and not inspection["features_without_definition"])
    results["initramfs"] = {
        "status": PASS if usable else FAIL,
        "features": inspection["features"],
        "features_without_definition": _bounded(inspection["features_without_definition"]),
        "expected_modules": inspection["expected_modules"],
        "modules_in_initramfs": inspection["modules_in_initramfs"],
        "missing_modules": _bounded(inspection["missing_modules"]),
        "has_init": inspection["has_init"],
        "size": image.stat().st_size,
    }


def _check_boot_entries(target, results):
    configuration, custom = target / GRUB_CONFIG, target / GRUB_CUSTOM
    if not configuration.is_file():
        results["boot_entries"] = {"status": FAIL, "reason": "grub_configuration_missing"}
        return
    generated = configuration.read_text(encoding="utf-8", errors="replace")
    normal = first_menuentry_body(generated) or []
    kernel_words = next((line.split() for line in (entry.strip() for entry in normal)
                         if line.startswith(("linux ", "linux16 "))), [])
    initrd_words = next((line.split() for line in (entry.strip() for entry in normal)
                         if line.startswith(("initrd ", "initrd16 "))), [])
    # What Alpine's own grub-mkconfig writes for a sys install: the kernel
    # image under /boot, a root= identification, and the initramfs beside it.
    has_normal = (len(kernel_words) > 1 and kernel_words[1].endswith("/vmlinuz-lts")
                  and any(word.startswith("root=") for word in kernel_words)
                  and len(initrd_words) > 1 and initrd_words[1].endswith("/initramfs-lts"))
    custom_text = custom.read_text(encoding="utf-8", errors="replace") if custom.is_file() else ""
    recovery_body = next((body for title, body in menuentry_blocks(custom_text)
                          if title == RECOVERY_TITLE), None)
    recovery_arguments = " ".join(recovery_body).split() if recovery_body else []
    has_recovery = (all(argument in recovery_arguments for argument in RECOVERY_ARGUMENTS)
                    and any(word.startswith("root=") for word in recovery_arguments))
    sourced = "custom.cfg" in generated
    results["boot_entries"] = {
        "status": PASS if (has_normal and has_recovery and sourced) else FAIL,
        "normal_entry": has_normal,
        "recovery_entry": has_recovery,
        "recovery_entry_sourced": sourced,
    }


def _check_bootloader_artifacts(target, firmware, results):
    required = BOOTLOADER_ARTIFACTS[firmware]
    missing = [relative for relative in required
               if not (target / relative).is_file() or (target / relative).stat().st_size == 0]
    results["bootloader_artifacts"] = {
        "status": PASS if not missing else FAIL,
        "firmware": firmware,
        "required": len(required),
        "missing": _bounded(missing),
    }


def _check_services(target, results):
    """Runlevel entries must be symlinks onto executable init scripts.

    A plain file in a runlevel directory, or a symlink whose init script the
    installation never copied, means the service will not start on the target.
    `modloop` is the mirror image: it belongs to the live medium and must be
    gone, which is what setup-disk's sys install does.
    """
    broken, missing = [], []
    for level, services in sorted(REQUIRED_SERVICES.items()):
        for service in services:
            entry = target / RUNLEVELS / level / service
            if not entry.is_symlink():
                missing.append(f"{level}/{service}")
                continue
            script = target / INIT_SCRIPTS / service
            link = os.readlink(str(entry))
            if os.path.isabs(link):
                resolved = target / link.lstrip("/")
            else:
                resolved = Path(os.path.normpath(str(entry.parent / link)))
            if resolved != script or not script.is_file() or not os.access(str(script), os.X_OK):
                broken.append(f"{level}/{service}")
    live_only = []
    runlevels = target / RUNLEVELS
    if runlevels.is_dir():
        for level in sorted(entry for entry in runlevels.iterdir() if entry.is_dir()):
            for service in LIVE_ONLY_SERVICES:
                if (level / service).is_symlink() or (level / service).exists():
                    live_only.append(f"{level.name}/{service}")
    results["required_services"] = {
        "status": PASS if not (broken or missing or live_only) else FAIL,
        "required": sum(len(names) for names in REQUIRED_SERVICES.values()),
        "missing": _bounded(missing),
        "unresolved": _bounded(broken),
        "live_only_services": _bounded(live_only),
    }


def _check_userspace(target, results):
    required = ("usr/local/sbin/aios-install", "usr/local/lib/aios/install.sh",
                HARDWARE_WORLD, "home/aios")
    missing = [relative for relative in required if not (target / relative).exists()]
    results["aios_userspace"] = {
        "status": PASS if not missing else FAIL,
        "missing": _bounded(missing),
    }


def verify_target(target, firmware, live_root="/"):
    """Read the mounted target back. Missing evidence never reads as a pass."""
    if firmware not in BOOTLOADER_ARTIFACTS:
        raise TargetError(f"Unknown firmware mode; expected one of {', '.join(FIRMWARE_MODES)}.")
    target, live = Path(target), Path(live_root)
    results = {}
    report = {
        "kind": REPORT_KIND,
        "schema_version": SCHEMA_VERSION,
        "firmware": firmware,
        "checks": results,
    }
    if not target.is_dir():
        results["target_root"] = {"status": INCOMPLETE, "reason": "target_is_not_a_directory"}
    elif _check_target_root(target, results):
        _check_mode_marker(target, results)
        world = _check_apk_world_parity(target, live, results)
        _check_apk_repositories(target, live, results)
        versions = _check_world_installed(target, world, results)
        _check_embedded_closure(live, versions, results)
        _check_hardware_files(target, live, versions, results)
        release = _check_kernel_and_modules(target, live, results)
        _check_initramfs(target, release, results)
        _check_boot_entries(target, results)
        _check_bootloader_artifacts(target, firmware, results)
        _check_services(target, results)
        _check_userspace(target, results)
    failed = sorted(name for name, check in results.items() if check["status"] == FAIL)
    incomplete = sorted(name for name, check in results.items() if check["status"] == INCOMPLETE)
    report["failed"] = failed
    report["incomplete"] = incomplete
    report["status"] = FAIL if failed else (INCOMPLETE if incomplete else PASS)
    return report


def serialize(report):
    """Serialize within a fixed budget, dropping detail before classifications."""
    text = json.dumps(report, indent=2, sort_keys=True)
    if len(text.encode("utf-8")) <= MAX_REPORT_BYTES:
        return text
    reduced = dict(report)
    reduced["checks"] = {name: {"status": check["status"]}
                         for name, check in report["checks"].items()}
    reduced["detail_omitted"] = True
    return json.dumps(reduced, indent=2, sort_keys=True)


def exit_code(report):
    if report["status"] == FAIL:
        return EXIT_FAILED
    if report["status"] == INCOMPLETE:
        return EXIT_INCOMPLETE
    return EXIT_OK


def _store_report(target, text):
    """Keep the report inside the target at its fixed relative location."""
    destination = Path(target) / REPORT_PATH
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(destination, text)
    except OSError:
        return None
    return REPORT_PATH


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="aios.install_target",
        description="Prepare and verify an already mounted AIOS installation target.")
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("recovery", "verify"):
        operation = operations.add_parser(name)
        operation.add_argument("--target", required=True, type=Path,
                               help="mounted target root, supplied by the installer")
        operation.add_argument("--live-root", default="/", type=Path,
                               help=argparse.SUPPRESS)
        if name == "verify":
            operation.add_argument("--firmware", required=True, choices=FIRMWARE_MODES)
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "recovery":
            result = prepare_recovery_entry(arguments.target)
            print(json.dumps(result, sort_keys=True))
            return EXIT_OK
        report = verify_target(arguments.target, arguments.firmware, arguments.live_root)
    except TargetError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return EXIT_INCOMPLETE
    except OSError as error:
        print(json.dumps({"error": f"Target verification could not read the target: "
                                   f"{error.strerror}."}), file=sys.stderr)
        return EXIT_INCOMPLETE
    text = serialize(report)
    stored = _store_report(arguments.target, text)
    print(text)
    if report["status"] != PASS:
        print(f"Installed-target verification {report['status']}: "
              f"failed={','.join(report['failed']) or 'none'} "
              f"incomplete={','.join(report['incomplete']) or 'none'}", file=sys.stderr)
    elif stored:
        print(f"Installed-target verification passed; report stored at /{stored}.", file=sys.stderr)
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
