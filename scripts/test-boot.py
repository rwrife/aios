#!/usr/bin/env python3
"""Boot an ISO without network, verify the ordinary-user shell and toolchain.

Uses only disposable VMs. No host disk, device node or existing disk image can
be attached: the one option that involves a disk, `--install-and-boot`, creates
its own qcow2 inside the harness's temporary directory, installs onto it, and
then boots that same image with no ISO and no network attached.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

INSTALL_TARGETS = {"sata": "/dev/sda", "nvme": "/dev/nvme0n1"}
DISK_IMAGE = "disposable.qcow2"
READY = "AIOS_QA_READY"
INSTALLED = "AIOS_QA_INSTALLED"
FAILED = "AIOS_QA_FAILED"
LOGIN_PROMPT = "aios login:"
SHELL_PROMPT = "aios:~#"

parser = argparse.ArgumentParser()
parser.add_argument("iso", type=Path)
parser.add_argument("--uefi", type=Path, help="OVMF_CODE firmware file")
parser.add_argument("--timeout", type=int, default=240)
parser.add_argument("--memory-mb", type=int, default=8192)
parser.add_argument("--cpus", type=int, default=4)
parser.add_argument("--log", type=Path, help="Write the complete guest serial log")
parser.add_argument("--boot-entry", choices=("default", "recovery"), default="default",
                    help="Boot the default ISO entry or direct-boot the generated recovery entry")
parser.add_argument("--install-and-boot", choices=tuple(sorted(INSTALL_TARGETS)),
                    help="Install onto a disposable disk this harness creates on the named "
                         "controller, then boot that same disk with no ISO attached. The image "
                         "belongs to the harness; no host device or existing image can be given.")
parser.add_argument("--disk-size-gb", type=int, default=24,
                    help="Size of the disposable disk image the harness creates")
args = parser.parse_args()
if args.memory_mb < 1024 or args.cpus < 1:
    parser.error("memory and CPU counts must be positive")
if args.disk_size_gb < 1:
    parser.error("the disposable disk must be at least 1 GiB")
if args.install_and_boot and args.boot_entry != "default":
    parser.error("--install-and-boot drives the default entry; --boot-entry applies to ISO runs")


class GuestFailure(RuntimeError):
    """A guest did not reach the state this harness required."""


def create_disposable_disk(directory, size_gb):
    """Create the one throwaway qcow2 that both phases use.

    The path is chosen here and never comes from an argument, so this harness
    cannot be pointed at a host disk or at an existing image.
    """
    image = Path(directory) / DISK_IMAGE
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(image), f"{size_gb}G"],
                   check=True, capture_output=True)
    return image


def disk_options(image, interface):
    options = ["-drive", f"if=none,id=aiosdisk,format=qcow2,file={image}"]
    if interface == "nvme":
        return options + ["-device", "nvme,drive=aiosdisk,serial=aios-disposable"]
    return options + ["-device", "ahci,id=aiosahci",
                      "-device", "ide-hd,drive=aiosdisk,bus=aiosahci.0"]


def verify_desktop(qmp_path, screenshot):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
        control.settimeout(10)
        control.connect(qmp_path)
        with control.makefile("rwb") as stream:
            json.loads(stream.readline())  # QMP greeting
            for command in (
                {"execute": "qmp_capabilities"},
                {"execute": "screendump", "arguments": {"filename": str(screenshot)}},
            ):
                stream.write(json.dumps(command).encode() + b"\n")
                stream.flush()
                while True:
                    response = json.loads(stream.readline())
                    if "error" in response:
                        raise RuntimeError(f"Desktop capture failed: {response['error']}")
                    if "return" in response:
                        break
    magic, dimensions, maximum, pixels = screenshot.read_bytes().split(b"\n", 3)
    width, height = map(int, dimensions.split())
    if magic != b"P6" or maximum != b"255" or len(pixels) != width * height * 3:
        raise RuntimeError("Unexpected QEMU framebuffer format")
    colors = {pixels[offset:offset + 3] for offset in range(0, len(pixels), 3)}
    visible = sum(max(pixels[offset:offset + 3]) > 32
                  for offset in range(0, len(pixels), 3))
    if len(colors) < 32 or visible < width * height // 20:
        raise RuntimeError("Desktop is black/blank despite a running aios-shell")


def extract_recovery_entry(iso, directory):
    """Extract and return the exact kernel/initramfs/append tuple from SYSLINUX."""
    config = Path(directory) / "syslinux.cfg"

    def extract(iso_path, destination):
        result = subprocess.run(
            ["xorriso", "-osirrox", "on", "-indev", str(iso.resolve()),
             "-extract", iso_path, str(destination)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not extract {iso_path} for recovery boot: "
                f"{(result.stderr or result.stdout)[-2000:]}"
            )

    extract("/boot/syslinux/syslinux.cfg", config)
    entry = {}
    active = False
    for raw_line in config.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if line.upper().startswith("LABEL "):
            active = line.split(None, 1)[1] == "recovery"
            continue
        if not active:
            continue
        keyword, _, value = line.partition(" ")
        if keyword in ("KERNEL", "INITRD", "APPEND") and value:
            entry[keyword.lower()] = value.strip()
    missing = {"kernel", "initrd", "append"} - set(entry)
    if missing:
        raise RuntimeError(f"Recovery boot entry is incomplete: missing {sorted(missing)}")
    if "aios.recovery" not in entry["append"].split() or "nomodeset" not in entry["append"].split():
        raise RuntimeError("Recovery boot entry lacks aios.recovery or nomodeset")
    kernel = Path(directory) / "recovery-kernel"
    initramfs = Path(directory) / "recovery-initramfs"
    extract(entry["kernel"], kernel)
    extract(entry["initrd"], initramfs)
    return kernel, initramfs, entry["append"]


def success_marker(suffix):
    """Print a completion marker without spelling it out in the command.

    The guest echoes every command it is sent, so a literal marker in the
    command text would be seen by this harness before the command ran.
    """
    return "printf '\\nAIOS_QA_%s\\n' " + suffix


def readiness_check(boot_entry, prefix=""):
    """The shell command that proves an ordinary-user desktop really works."""
    check = "{ " + prefix
    check += "for n in $(seq 1 60); do pgrep -u aios -x aios-shell >/dev/null && break; sleep 1; done; "
    if boot_entry == "recovery":
        check += "grep -qw aios.recovery /proc/cmdline && grep -qw nomodeset /proc/cmdline && "
        check += "for n in $(seq 1 15); do grep -q '\"status\":\"ready\"' /home/aios/.local/state/aios/renderer.json 2>/dev/null && break; sleep 1; done; "
        check += "grep -q '\"status\":\"ready\"' /home/aios/.local/state/aios/renderer.json && "
        check += "grep -q '\"phase\":\"shell_ready\"' /home/aios/.local/state/aios/renderer.json && "
        check += "grep -q '\"backend\":\"xrender\"' /home/aios/.local/state/aios/renderer.json && "
        check += "grep -q '\"renderer\":\"software\"' /home/aios/.local/state/aios/renderer.json && "
        check += "grep -q '\"recovery\":true' /home/aios/.local/state/aios/renderer.json && "
    else:
        check += "! grep -qw aios.recovery /proc/cmdline && "
    check += "pgrep -u aios -x aios-shell >/dev/null && su aios -c 'cd; aios-new-app smoke; cmake -S smoke -B smoke/build -G Ninja && cmake --build smoke/build' && "
    check += "{ for n in $(seq 1 60); do wget -qO /dev/null http://127.0.0.1:8080/health && break; sleep 1; done; "
    check += "su aios -c 'aios-llm chat \"Say hello in one sentence.\"'; } && "
    check += success_marker("READY") + "; } || "
    check += "{ printf '\\ncmdline='; cat /proc/cmdline; "
    check += "printf 'renderer='; cat /home/aios/.local/state/aios/renderer.json 2>/dev/null || true; "
    check += "printf 'session_log='; tail -n 40 /home/aios/.local/state/aios/session.log 2>/dev/null || true; "
    check += "printf 'compositor_log='; tail -n 20 /home/aios/.local/state/aios/compositor.log 2>/dev/null || true; "
    check += "printf 'processes='; ps | grep -E 'aios-session|aios-shell|pulseaudio|picom|openbox' || true; "
    check += "printf '\\nAIOS_QA_FAILED\\n'; }\n"
    return check


def installed_system_check():
    """An installed machine must also carry its own verification evidence."""
    prefix = ("grep -qx installed /etc/aios-mode && "
              "grep -q '\"status\": \"pass\"' /var/log/aios-install-verification.json && "
              "! grep -q '^/' /etc/apk/repositories && ")
    return readiness_check("default", prefix=prefix)


def installer_script(target):
    """Drive aios-install with the exact disk path and erase confirmation."""
    return ("{ printf '%s\\nERASE %s\\n' " + target + " " + target
            + " | /usr/local/sbin/aios-install; } > /tmp/install.log 2>&1; "
            + "tail -n 20 /tmp/install.log; "
            + "{ grep -q 'Installation complete' /tmp/install.log && "
            + success_marker("INSTALLED") + "; } || "
            + "{ printf '\\ninstall_log='; cat /tmp/install.log; "
            + "printf '\\nAIOS_QA_FAILED\\n'; }\n")


def run_guest(command, *, directory, timeout, steps, success, transcript,
              on_success=None, shut_down=False):
    """Boot one QEMU guest, drive its console with fixed steps, collect output.

    `steps` are `(marker, text)` pairs applied strictly in order: each is sent
    once, the first time its marker appears after the previous step was sent.
    `success` is the marker that ends the run. `on_success` runs against the
    live guest's QMP socket before it is stopped, and `shut_down` powers the
    guest down cleanly instead of terminating it, which is what an install
    phase needs before its disk is booted again.
    """
    serial_path = str(Path(directory) / "serial.sock")
    qmp_path = str(Path(directory) / "qmp.sock")
    for stale in (serial_path, qmp_path):
        Path(stale).unlink(missing_ok=True)
    command = list(command) + ["-serial", f"unix:{serial_path},server=on,wait=off",
                               "-qmp", f"unix:{qmp_path},server=on,wait=off"]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    output = ""
    try:
        while not Path(serial_path).exists():
            if process.poll() is not None:
                raise GuestFailure(process.stderr.read().decode())
            if time.monotonic() > deadline:
                raise TimeoutError("QEMU serial socket did not appear")
            time.sleep(.1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as serial:
            while True:
                try:
                    serial.connect(serial_path)
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise GuestFailure(
                            "QEMU serial listener did not become ready: " +
                            (process.stderr.read().decode() if process.poll() is not None
                             else "timeout"))
                    time.sleep(.1)
            serial.settimeout(1)
            pending = list(steps)
            while time.monotonic() < deadline:
                try:
                    data = serial.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                output += data.decode(errors="replace")
                if "No space left on device" in output or "can't run '/sbin/agetty'" in output:
                    raise GuestFailure("Guest package installation/login failed. Last output:\n"
                                       + output[-6000:])
                if pending and pending[0][0] in output:
                    serial.sendall(pending.pop(0)[1].encode())
                if f"\n{FAILED}" in output:
                    raise GuestFailure("Guest verification failed. Last output:\n" + output[-6000:])
                if f"\n{success}" in output:
                    if on_success:
                        on_success(qmp_path)
                    if shut_down:
                        serial.sendall(b"poweroff\n")
                        try:
                            process.wait(timeout=120)
                        except subprocess.TimeoutExpired:
                            raise GuestFailure("The guest did not power down after installing.")
                    return output
            raise TimeoutError(f"Guest did not reach {success}. Last output:\n" + output[-4000:])
    finally:
        transcript.append(output)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def base_command(name, extra):
    command = ["qemu-system-x86_64", "-m", str(args.memory_mb), "-smp", str(args.cpus),
               "-name", f"AIOS-{name}-{os.getpid()}",
               "-display", "none", "-nic", "none", "-no-reboot"]
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        command += ["-enable-kvm", "-cpu", "host"]
    if args.uefi:
        command += ["-drive", f"if=pflash,format=raw,readonly=on,file={args.uefi}"]
    return command + extra


with tempfile.TemporaryDirectory(prefix="aios-boot-") as directory:
    transcript = []
    screenshot = Path(directory) / "desktop.ppm"
    try:
        if args.install_and_boot:
            target = INSTALL_TARGETS[args.install_and_boot]
            image = create_disposable_disk(directory, args.disk_size_gb)
            attachment = disk_options(image, args.install_and_boot)
            run_guest(base_command(f"install-{args.install_and_boot}",
                                   ["-cdrom", str(args.iso.resolve()), "-boot", "d", *attachment]),
                      directory=directory, timeout=args.timeout,
                      steps=[(LOGIN_PROMPT, "root\n"),
                             (SHELL_PROMPT, installer_script(target))],
                      success=INSTALLED, transcript=transcript, shut_down=True)
            print(f"PASS: offline installation onto a disposable {args.install_and_boot} disk at "
                  f"{target}, gated by the installer's own fail-closed readback")
            # The same image, with no ISO and no network attached: an installed
            # machine with the installation medium removed.
            run_guest(base_command(f"installed-{args.install_and_boot}",
                                   ["-boot", "c", *attachment]),
                      directory=directory, timeout=args.timeout,
                      steps=[(LOGIN_PROMPT, "root\n"), (SHELL_PROMPT, installed_system_check())],
                      success=READY, transcript=transcript,
                      on_success=lambda qmp: verify_desktop(qmp, screenshot))
            print(f"PASS: installed {args.install_and_boot} disk boots with the ISO removed, "
                  "ordinary-user shell, desktop example compilation, bundled model reply")
        else:
            extra = ["-cdrom", str(args.iso.resolve()), "-boot", "d"]
            if args.boot_entry == "recovery":
                kernel, initramfs, append = extract_recovery_entry(args.iso, directory)
                extra += ["-kernel", str(kernel), "-initrd", str(initramfs), "-append", append]
            run_guest(base_command(f"{args.boot_entry}-boot-check", extra),
                      directory=directory, timeout=args.timeout,
                      steps=[(LOGIN_PROMPT, "root\n"),
                             (SHELL_PROMPT, readiness_check(args.boot_entry))],
                      success=READY, transcript=transcript,
                      on_success=lambda qmp: verify_desktop(qmp, screenshot))
            print(f"PASS: {args.boot_entry} offline boot, ordinary-user shell, "
                  "desktop example compilation, bundled model reply")
    finally:
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            args.log.write_text("\n".join(transcript))
