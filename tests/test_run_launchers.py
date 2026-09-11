import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RunLauncherTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Linux launcher requires bash")
    def test_linux_defaults_show_window_and_boot_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            iso = Path(directory) / "test-x86_64.iso"
            iso.touch()
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AIOS_")}
            env["DRY_RUN"] = "1"
            args = shlex.split(subprocess.check_output(
                ["bash", str(ROOT / "scripts/run.sh"), str(iso)],
                env=env, text=True,
            ))
            self.assertEqual(args[args.index("-display") + 1],
                             "gtk,full-screen=off,zoom-to-fit=on")
            self.assertEqual(args[args.index("-serial") + 1], "mon:stdio")
            self.assertEqual(args.count("-serial"), 1)

    @unittest.skipUnless(os.name == "nt", "Native launcher requires Windows PowerShell")
    def test_native_defaults_show_window_and_boot_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            iso = Path(directory) / "test-x86_64.iso"
            iso.touch()
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("AIOS_")}
            command = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(ROOT / "scripts/run.ps1"), str(iso),
                 "-Native", "-DryRun"], env=env, text=True,
            )
            self.assertIn("'-display' 'sdl,full-screen=off'", command)
            self.assertIn("'-serial' 'mon:stdio'", command)
            self.assertEqual(command.count("'-serial'"), 1)

    @unittest.skipUnless(os.name == "posix", "Linux launcher requires bash")
    def test_linux_name_and_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            iso = Path(directory) / "test image-x86_64.iso"
            iso.touch()
            env = dict(os.environ, DRY_RUN="1", AIOS_VM_NAME="Boot check, Ocean",
                       AIOS_QEMU_SERIAL="file:/tmp/boot.log",
                       AIOS_VM_CAMERA_BUS="", AIOS_VM_CAMERA_ADDR="", AIOS_QEMU_UEFI="0")
            for headless in ("0", "1"):
                with self.subTest(headless=headless):
                    env["AIOS_QEMU_HEADLESS"] = headless
                    args = shlex.split(subprocess.check_output(
                        ["bash", str(ROOT / "scripts/run.sh"), str(iso)],
                        env=env, text=True,
                    ))
                    self.assertEqual(args[args.index("-name") + 1], "Boot check,, Ocean")
                    self.assertEqual(args[args.index("-serial") + 1], "file:/tmp/boot.log")
                    self.assertEqual(args.count("-serial"), 1)
                    self.assertEqual(args[args.index("-cdrom") + 1], str(iso))
            env.pop("AIOS_VM_NAME")
            args = shlex.split(subprocess.check_output(
                ["bash", str(ROOT / "scripts/run.sh"), str(iso)], env=env, text=True))
            self.assertRegex(args[args.index("-name") + 1], r"^AIOS-test image-x86_64-\d+$")

    @unittest.skipUnless(os.name == "nt", "Native launcher requires Windows PowerShell")
    def test_native_name_and_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            iso = Path(directory) / "test image-x86_64.iso"
            iso.touch()
            env = dict(os.environ, AIOS_VM_NAME="Environment name",
                       AIOS_QEMU_SERIAL=r"file:C:\Temp\boot.log", AIOS_QEMU_UEFI="0",
                       AIOS_VM_CAMERA_BUS="", AIOS_VM_CAMERA_ADDR="")
            for headless in ("0", "1"):
                with self.subTest(headless=headless):
                    env["AIOS_QEMU_HEADLESS"] = headless
                    command = subprocess.check_output(
                        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(ROOT / "scripts/run.ps1"), str(iso),
                         "-Native", "-DryRun", "-Name", "Boot check, Ocean"],
                        env=env, text=True,
                    )
                    self.assertIn("'-name' 'Boot check,, Ocean'", command)
                    self.assertIn(r"'-serial' 'file:C:\Temp\boot.log'", command)
                    self.assertEqual(command.count("'-serial'"), 1)
                    self.assertIn(str(iso), command)


if __name__ == "__main__":
    unittest.main()
