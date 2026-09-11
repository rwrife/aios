import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "WSL wrapper requires Windows PowerShell")
class WslDisplayTests(unittest.TestCase):
    def run_launcher(self, scenario, answer="yes", options=()):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("AIOS_") and key != "DRY_RUN"}
        env.update(AIOS_TEST_LAUNCHER=str(ROOT / "scripts/run.ps1"),
                   AIOS_TEST_SCENARIO=scenario, AIOS_TEST_ANSWER=answer)
        if scenario == "headless":
            env["AIOS_QEMU_HEADLESS"] = "1"
        if scenario == "env-dry-run":
            env["DRY_RUN"] = "1"
        script = r"""
$script:probes = 0
$script:restarted = $false
function global:Start-Sleep {}
function global:Read-Host { Write-Host 'CONFIRM'; return $env:AIOS_TEST_ANSWER }
function global:wsl.exe {
    $global:LASTEXITCODE = 0
    if ($args -contains 'wslpath') { return '/mnt/test/scripts/run.sh' }
    if ($args -contains '--system') {
        Write-Host 'RESTART'
        if ($args -notcontains $env:AIOS_TEST_DISTRO -and $env:AIOS_TEST_DISTRO) {
            throw 'Restart targeted the wrong distro'
        }
        if (($args -join ' ') -notmatch 'kill -TERM \$1') {
            throw 'Restart did not target the discovered Weston PID'
        }
        $script:restarted = $true
        if ($env:AIOS_TEST_SCENARIO -eq 'restart-failure') { $global:LASTEXITCODE = 1 }
        return
    }
    if (($args -join ' ') -match 'use_gfxredir') {
        Write-Host 'PROBE'
        $script:probes++
        switch ($env:AIOS_TEST_SCENARIO) {
            'ready' { return 'ready' }
            'starting' { if ($script:probes -eq 1) { return 'starting' }; return 'ready' }
            'unavailable' { return 'unavailable' }
            'probe-failure' { $global:LASTEXITCODE = 1; return }
            'invalid-state' { return 'unexpected' }
            'repair-timeout' { return 'broken' }
            default {
                if ($script:restarted -and $script:probes -gt 2) { return 'ready' }
                return 'broken'
            }
        }
    }
    if ($args -contains '-lc') { return }
    if ($args -contains 'bash') { Write-Host ('LAUNCH ' + ($args -join ' ')); return }
    throw "Unexpected WSL call: $args"
}
& $env:AIOS_TEST_LAUNCHER
"""
        if "-Distro" in options:
            env["AIOS_TEST_DISTRO"] = options[options.index("-Distro") + 1]
        script = script.rstrip() + " " + " ".join(options)
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script], env=env, input="", capture_output=True, text=True,
            timeout=20,
        )

    def test_healthy_default_launch_needs_no_repair(self):
        for scenario in ("ready", "starting"):
            with self.subTest(scenario=scenario):
                result = self.run_launcher(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("LAUNCH -d Ubuntu --exec env bash /mnt/test/scripts/run.sh",
                              result.stdout)
                self.assertNotIn("RESTART", result.stdout)
                self.assertNotIn("CONFIRM", result.stdout)

    def test_broken_display_is_repaired_with_consent_and_readback(self):
        result = self.run_launcher("broken", options=("-Distro", "Debian"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CONFIRM", result.stdout)
        self.assertEqual(result.stdout.count("RESTART"), 1)
        self.assertEqual(result.stdout.count("PROBE"), 3)
        self.assertIn("WSLg display restored.", result.stdout)
        self.assertLess(result.stdout.index("RESTART"), result.stdout.index("LAUNCH"))
        self.assertIn("LAUNCH -d Debian --exec env", result.stdout)
        self.assertNotIn("--shutdown", result.stdout)

    def test_declining_repair_does_not_restart_or_launch(self):
        for answer in ("no", ""):
            with self.subTest(answer=answer):
                result = self.run_launcher("broken", answer=answer)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("repair was declined", result.stderr)
                self.assertNotIn("RESTART", result.stdout)
                self.assertNotIn("LAUNCH", result.stdout)

    def test_failed_or_unavailable_display_does_not_launch(self):
        for scenario in ("restart-failure", "repair-timeout", "unavailable",
                         "probe-failure", "invalid-state"):
            with self.subTest(scenario=scenario):
                result = self.run_launcher(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("QEMU was not started", " ".join(result.stderr.split()))
                self.assertNotIn("LAUNCH", result.stdout)

    def test_headless_and_dry_run_do_not_inspect_or_restart_wslg(self):
        for scenario, options in (("headless", ()), ("env-dry-run", ()),
                                  ("broken", ("-DryRun",))):
            with self.subTest(scenario=scenario, options=options):
                result = self.run_launcher(scenario, options=options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("LAUNCH", result.stdout)
                self.assertNotIn("PROBE", result.stdout)
                self.assertNotIn("CONFIRM", result.stdout)
                self.assertNotIn("RESTART", result.stdout)


class RunLauncherTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "WSLg log probe requires a POSIX shell")
    def test_wslg_probe_uses_latest_display_state(self):
        wrapper = (ROOT / "scripts/run.ps1").read_text()
        probe = re.search(r"\$probe = @'\n(.*?)\n'@", wrapper, re.S).group(1)
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "weston.log"
            script = probe.replace("/mnt/wslg/weston.log", shlex.quote(str(log)))
            cases = (
                (None, "unavailable"),
                ("Starting Weston\n", "starting"),
                ("RDP backend: use_gfxredir = 0\n", "broken"),
                ("RDP backend: use_gfxredir = 0\nRDP backend: use_gfxredir = 1\n", "ready"),
                ("RDP backend: use_gfxredir = 1\nRDP backend: use_gfxredir = 0\n", "broken"),
            )
            for content, expected in cases:
                with self.subTest(content=content):
                    if content is not None:
                        log.write_text(content)
                    result = subprocess.check_output(["sh", "-c", script], text=True)
                    self.assertEqual(result.strip(), expected)

    @unittest.skipUnless(os.name == "posix", "Linux launcher requires bash")
    def test_linux_defaults_show_window_without_terminal_login(self):
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
            self.assertEqual(args[args.index("-serial") + 1],
                             f"file:{ROOT / '.tmp-aios-boot.log'}")
            self.assertEqual(args.count("-serial"), 1)
            self.assertNotIn("mon:stdio", args)
            env["AIOS_QEMU_HEADLESS"] = "1"
            args = shlex.split(subprocess.check_output(
                ["bash", str(ROOT / "scripts/run.sh"), str(iso)],
                env=env, text=True,
            ))
            self.assertEqual(args[args.index("-display") + 1], "none")
            self.assertEqual(args[args.index("-serial") + 1], "mon:stdio")
            self.assertEqual(args.count("-serial"), 1)

    @unittest.skipUnless(os.name == "nt", "Native launcher requires Windows PowerShell")
    def test_native_defaults_show_window_without_terminal_login(self):
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
            self.assertIn(f"'-serial' 'file:{ROOT / '.tmp-aios-boot.log'}'", command)
            self.assertNotIn("mon:stdio", command)
            self.assertEqual(command.count("'-serial'"), 1)
            env["AIOS_QEMU_HEADLESS"] = "1"
            command = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(ROOT / "scripts/run.ps1"), str(iso),
                 "-Native", "-DryRun"], env=env, text=True,
            )
            self.assertIn("'-display' 'none'", command)
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
