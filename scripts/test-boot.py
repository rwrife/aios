#!/usr/bin/env python3
"""Boot an ISO without network, verify the ordinary-user shell and toolchain.

Uses only a disposable VM, no host disk passthrough. Requires QEMU on Linux.
"""
import argparse
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
args = parser.parse_args()
if args.memory_mb < 1024 or args.cpus < 1:
    parser.error("memory and CPU counts must be positive")
with tempfile.TemporaryDirectory(prefix="aios-boot-") as directory:
    serial_path = str(Path(directory) / "serial.sock")
    command = ["qemu-system-x86_64", "-m", str(args.memory_mb), "-smp", str(args.cpus),
               "-cdrom", str(args.iso.resolve()),
               "-boot", "d", "-display", "none", "-nic", "none", "-no-reboot",
               "-serial", f"unix:{serial_path},server=on,wait=off"]
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
                    check = "for n in 1 2 3 4 5 6 7 8 9 10; do pgrep -u aios -x aios-shell >/dev/null && break; sleep 1; done; "
                    check += "pgrep -u aios -x aios-shell >/dev/null && su aios -c 'cd; aios-new-app smoke; cmake -S smoke -B smoke/build -G Ninja && cmake --build smoke/build' && "
                    check += "{ for n in $(seq 1 60); do wget -qO /dev/null http://127.0.0.1:8080/health && break; sleep 1; done; "
                    check += "su aios -c 'aios-llm chat \"Say hello in one sentence.\"'; } && printf '\\nAIOS_QA_%s\\n' READY\n"
                    serial.sendall(check.encode())
                    sent_test = True
                if "\nAIOS_QA_READY" in output:
                    print("PASS: offline boot, ordinary-user shell, desktop example compilation, bundled model reply")
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
