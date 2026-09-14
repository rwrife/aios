import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from aios import os_command
from aios.applications import APPLICATION_TOOL
from aios.toolhost import ToolHost


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(["ignored"], returncode, stdout, stderr)


class OsCommandTests(unittest.TestCase):
    def test_schema_lists_allowlisted_commands_only(self):
        function = os_command.TOOL["function"]
        self.assertEqual(function["name"], "os_command")
        parameters = function["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["required"], ["command"])
        self.assertEqual(parameters["properties"]["command"]["enum"], list(os_command.COMMANDS))
        self.assertIn("ls", os_command.COMMANDS)
        self.assertIn("cat", os_command.COMMANDS)
        self.assertIn("echo", os_command.COMMANDS)
        for blocked in ("sh", "bash", "busybox", "find", "sudo", "doas", "python", "python3"):
            self.assertNotIn(blocked, os_command.COMMANDS)

    def test_invalid_requests_never_launch(self):
        with mock.patch.object(os_command.subprocess, "run") as run:
            for request in (
                {},
                {"command": "ls", "shell": True},
                {"command": "/bin/ls"},
                {"command": "ls;id"},
                {"command": "sh"},
                {"command": "find"},
                {"command": "echo", "args": "hello"},
                {"command": "echo", "args": ["hello\0world"]},
                {"command": "echo", "args": ["x" * 4097]},
                {"command": "echo", "stdin": "x" * (os_command.MAX_STDIN_BYTES + 1)},
            ):
                with self.subTest(request=request), self.assertRaises(ValueError):
                    os_command.act(request)
            run.assert_not_called()

    def test_runs_argv_without_a_shell(self):
        with mock.patch.object(os_command, "_resolve", return_value="/bin/echo"), \
                mock.patch.object(os_command.subprocess, "run", return_value=_completed("hello\n")) as run, \
                mock.patch.object(os_command.os.path, "isdir", return_value=True):
            result = os_command.act({"command": "echo", "args": ["hello"], "cwd": "/tmp"})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"], "hello\n")
        self.assertFalse(result["truncated"])
        self.assertEqual(run.call_args.kwargs["args"], ["/bin/echo", "hello"])
        self.assertFalse(run.call_args.kwargs["check"])
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["env"]["PATH"], "/usr/bin:/bin")

    def test_passes_stdin_and_clips_output(self):
        huge = "n" * (os_command.OUTPUT_LIMIT + 50)
        with mock.patch.object(os_command, "_resolve", return_value="/usr/bin/tee"), \
                mock.patch.object(os_command.subprocess, "run", return_value=_completed(huge, "err\n", 1)) as run, \
                mock.patch.object(os_command.os.path, "isdir", return_value=True):
            result = os_command.act({"command": "tee", "args": ["out.txt"], "stdin": "payload", "cwd": "/tmp"})
        self.assertEqual(run.call_args.kwargs["input"], "payload")
        self.assertNotIn("stdin", run.call_args.kwargs)
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout"].encode("utf-8")), os_command.OUTPUT_LIMIT)
        self.assertEqual(result["stderr"], "err\n")

    def test_timeout_and_missing_binary(self):
        with mock.patch.object(os_command, "_resolve", return_value="/bin/sleep"), \
                mock.patch.object(os_command.subprocess, "run", side_effect=subprocess.TimeoutExpired("sleep", 10)), \
                mock.patch.object(os_command.os.path, "isdir", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                os_command.act({"command": "date"})
        with mock.patch.object(os_command.os.path, "isfile", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "ls is unavailable"):
                os_command.act({"command": "ls"})

    def test_working_directory_must_exist(self):
        with mock.patch.object(os_command.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "Working directory"):
                os_command.act({"command": "pwd", "cwd": "/no/such/aios-os-command-dir"})
            run.assert_not_called()

    @unittest.skipUnless(
        os.path.isfile("/bin/echo") or os.path.isfile("/usr/bin/echo"),
        "POSIX echo required",
    )
    def test_real_echo_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            result = os_command.act({"command": "echo", "args": ["hello", "aios"], "cwd": temp})
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout"].strip(), "hello aios")
        self.assertFalse(result["truncated"])
        self.assertTrue(result["path"].endswith("/echo"))

    def test_real_stdio_mcp_lists_and_rejects_unknown_command(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "os_command", "arguments": {"command": "sh"}}},
        ]
        completed = subprocess.run(
            [sys.executable, "-m", "aios.os_command"],
            input="\n".join(map(json.dumps, messages)) + "\n",
            capture_output=True, text=True, timeout=5, check=True,
        )
        listed, failed = map(json.loads, completed.stdout.splitlines())
        self.assertEqual(listed["result"]["tools"][0]["name"], "os_command")
        self.assertIn("ls", listed["result"]["tools"][0]["inputSchema"]["properties"]["command"]["enum"])
        self.assertTrue(failed["result"]["isError"])
        self.assertIn("Invalid", failed["result"]["content"][0]["text"])

    def test_toolhost_advertises_and_dispatches(self):
        class Fake:
            def close(self):
                pass

            def definitions(self):
                return [], []

            def call(self, name, arguments):
                raise AssertionError(name)

            def definition(self):
                return APPLICATION_TOOL

        host = ToolHost(browser=Fake(), applications=Fake(), mcp=Fake())
        names = [item["function"]["name"] for item in host.definitions()["tools"]]
        self.assertEqual(names, ["browser", "application", "os_settings", "os_command"])
        with mock.patch("aios.toolhost.os_command.act", return_value={"exit_code": 0, "stdout": ""}) as act:
            self.assertEqual(host.call("os_command", {"command": "pwd"}), {"exit_code": 0, "stdout": ""})
            act.assert_called_once_with({"command": "pwd"})


if __name__ == "__main__":
    unittest.main()
