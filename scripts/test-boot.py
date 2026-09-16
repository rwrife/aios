#!/usr/bin/env python3
"""Boot an ISO without network, verify the ordinary-user shell and toolchain.

Uses only a disposable VM, no host disk passthrough. Requires QEMU on Linux.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("iso", type=Path)
parser.add_argument("--uefi", type=Path, help="OVMF_CODE firmware file")
parser.add_argument("--timeout", type=int, default=240)
parser.add_argument("--memory-mb", type=int, default=8192)
parser.add_argument("--cpus", type=int, default=4)
parser.add_argument("--log", type=Path, help="Write the complete guest serial log")
parser.add_argument("--boot-entry", choices=("default", "recovery"), default="default",
                    help="Boot the default ISO entry or direct-boot the generated recovery entry")
args = parser.parse_args()
if args.memory_mb < 1024 or args.cpus < 1:
    parser.error("memory and CPU counts must be positive")


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


with tempfile.TemporaryDirectory(prefix="aios-boot-") as directory:
    serial_path = str(Path(directory) / "serial.sock")
    qmp_path = str(Path(directory) / "qmp.sock")
    command = ["qemu-system-x86_64", "-m", str(args.memory_mb), "-smp", str(args.cpus),
               "-name", f"AIOS-{args.boot_entry}-boot-check-{os.getpid()}",
               "-cdrom", str(args.iso.resolve()),
               "-boot", "d", "-display", "none", "-nic", "none", "-no-reboot",
               "-serial", f"unix:{serial_path},server=on,wait=off",
               "-qmp", f"unix:{qmp_path},server=on,wait=off"]
    if args.boot_entry == "recovery":
        kernel, initramfs, append = extract_recovery_entry(args.iso, directory)
        command += ["-kernel", str(kernel), "-initrd", str(initramfs), "-append", append]
    if os.access("/dev/kvm", os.R_OK | os.W_OK):
        command += ["-enable-kvm", "-cpu", "host"]
    if args.uefi:
        command += ["-drive", f"if=pflash,format=raw,readonly=on,file={args.uefi}"]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + args.timeout
    output = ""
    try:
        while not Path(serial_path).exists():
            if process.poll() is not None:
                raise RuntimeError(process.stderr.read().decode())
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
                        raise RuntimeError("QEMU serial listener did not become ready: " +
                                           (process.stderr.read().decode() if process.poll() is not None else "timeout"))
                    time.sleep(.1)
            serial.settimeout(1)
            output = ""
            logged_in = sent_test = False
            while time.monotonic() < deadline:
                try:
                    data = serial.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                output += data.decode(errors="replace")
                if "No space left on device" in output or "can't run '/sbin/agetty'" in output:
                    raise RuntimeError("Guest package installation/login failed. Last output:\n" + output[-6000:])
                if "aios login:" in output and not logged_in:
                    serial.sendall(b"root\n")
                    logged_in = True
                if "aios:~#" in output and not sent_test:
                    # The success marker does not occur literally in the echoed command.
                    check = "{ for n in $(seq 1 60); do pgrep -u aios -x aios-shell >/dev/null && break; sleep 1; done; "
                    if args.boot_entry == "recovery":
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
                    check += "su aios -c 'aios-llm chat \"Say hello in one sentence.\"'; } && printf '\\nAIOS_QA_%s\\n' READY; } || "
                    check += "{ printf '\\ncmdline='; cat /proc/cmdline; "
                    check += "printf 'renderer='; cat /home/aios/.local/state/aios/renderer.json 2>/dev/null || true; "
                    check += "printf 'session_log='; tail -n 40 /home/aios/.local/state/aios/session.log 2>/dev/null || true; "
                    check += "printf 'compositor_log='; tail -n 20 /home/aios/.local/state/aios/compositor.log 2>/dev/null || true; "
                    check += "printf 'processes='; ps | grep -E 'aios-session|aios-shell|pulseaudio|picom|openbox' || true; "
                    check += "printf '\\nAIOS_QA_FAILED\\n'; }\n"
                    serial.sendall(check.encode())
                    sent_test = True
                if "\nAIOS_QA_FAILED" in output:
                    raise RuntimeError("Guest readiness verification failed. Last output:\n" + output[-4000:])
                if "\nAIOS_QA_READY" in output:
                    verify_desktop(qmp_path, Path(directory) / "desktop.ppm")
                    print(f"PASS: {args.boot_entry} offline boot, ordinary-user shell, "
                          "desktop example compilation, bundled model reply")
                    break
            else:
                raise TimeoutError("Boot/toolchain test timed out. Last output:\n" + output[-4000:])
            if "\nAIOS_QA_READY" not in output:
                raise RuntimeError("Guest exited before verification. Last output:\n" + output[-4000:])
    finally:
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            args.log.write_text(output)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
