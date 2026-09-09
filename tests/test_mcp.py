import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from aios import core, mcp


class McpTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_root = self.root / "config"
        self.data_root = self.root / "data"
        self.cache_root = self.root / "cache"
        self.log = self.root / "mcp-log.jsonl"
        self.env_log = self.root / "child-env.json"
        self.fixture = Path(__file__).parent / "fixtures" / "mcp_server.py"
        self.env = patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.config_root),
                "XDG_DATA_HOME": str(self.data_root),
                "XDG_CACHE_HOME": str(self.cache_root),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "OPENAI_API_KEY": "must-not-inherit",
                "CODEX_API_KEY": "must-not-inherit",
                "PASSWORD": "must-not-inherit",
                "TOKEN": "must-not-inherit",
            },
            clear=False,
        )
        self.env.start()
        self.config_path = core.config_dir() / "mcp.json"

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_config(self, servers=None, raw_text=None):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if raw_text is not None:
            self.config_path.write_text(raw_text, encoding="utf-8")
        else:
            self.config_path.write_text(json.dumps({"servers": servers or {}}, ensure_ascii=False), encoding="utf-8")

    def server_settings(self, scenario="happy", tools=None, env=None, command=None, args=None):
        variables = {
            "AIOS_MCP_LOG": str(self.log),
            "AIOS_MCP_ENV_LOG": str(self.env_log),
            "AIOS_MCP_SCENARIO": scenario,
            "AIOS_MCP_SECRET": "secret-provider-token",
        }
        if env:
            variables.update(env)
        return {
            "command": command or sys.executable,
            "args": args or [str(self.fixture)],
            "env": variables,
            "tools": tools if tools is not None else ["echo"],
        }

    def make_registry(self, timeout=0.25):
        return mcp.McpRegistry(config_path=self.config_path, request_timeout=timeout)

    def read_log(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def read_child_env(self):
        return json.loads(self.env_log.read_text(encoding="utf-8"))

    def test_missing_config_has_no_servers_or_warnings(self):
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(definitions, [])
        self.assertEqual(warnings, [])

    def test_nonobject_config_is_ignored_with_warning(self):
        self.write_config(raw_text="123")
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(definitions, [])
        self.assertTrue(warnings)
        self.assertIn("MCP", warnings[0])

    def test_initialize_list_call_sequence_and_allowlist(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"]),
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
            self.assertEqual(warnings, [])
            self.assertEqual([item["function"]["name"] for item in definitions], ["mcp_fixture_echo"])
            self.assertTrue(definitions[0]["function"]["description"].startswith("fixture: "))
            self.assertEqual(definitions[0]["function"]["parameters"]["type"], "object")

            result = registry.call("mcp_fixture_echo", {"value": "hello"})
        finally:
            registry.close()
        self.assertEqual(result, {"text": "echo:hello", "structured": {"value": "hello"}, "is_error": False})
        methods = [item["method"] for item in self.read_log() if "method" in item]
        self.assertEqual(methods[:4], ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertNotIn("hidden", json.dumps(definitions))

    def test_star_allowlist_paginates_and_refreshes_on_list_changed(self):
        self.write_config({
            "fixture": self.server_settings(scenario="paginate-list-changed", tools=["*"]),
        })
        registry = self.make_registry()
        try:
            first, warnings = registry.definitions()
            self.assertEqual(warnings, [])
            self.assertEqual([item["function"]["name"] for item in first], ["mcp_fixture_echo", "mcp_fixture_sum"])

            time.sleep(0.05)
            second, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(warnings, [])
        self.assertEqual([item["function"]["name"] for item in second], ["mcp_fixture_updated"])
        list_calls = [item for item in self.read_log() if item.get("method") == "tools/list"]
        self.assertEqual(len(list_calls), 3)

    def test_empty_allowlist_does_not_start_server(self):
        self.write_config({
            "fixture": self.server_settings(tools=[], command="definitely-not-a-real-command-secret"),
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(definitions, [])
        self.assertEqual(warnings, [])
        self.assertFalse(self.log.exists())

    def test_config_schema_symlink_size_and_env_rejection_are_safe(self):
        cases = []

        self.write_config(raw_text=json.dumps({"servers": {"bad name": self.server_settings()}}, ensure_ascii=False))
        cases.append(("invalid-name", self.config_path))

        large_path = self.config_path.parent / "large.json"
        large_path.write_bytes(b"{" + (b"x" * (256 * 1024 + 1)) + b"}")
        cases.append(("oversized", large_path))

        target = self.config_path.parent / "target.json"
        target.write_text(json.dumps({"servers": {}}, ensure_ascii=False), encoding="utf-8")
        symlink_path = self.config_path.parent / "symlink.json"
        symlink_path.symlink_to(target)
        cases.append(("symlink", symlink_path))

        env_path = self.config_path.parent / "bad-env.json"
        env_path.write_text(
            json.dumps({"servers": {"fixture": self.server_settings(env={"BAD-NAME": "value"}, tools=["*"])}}),
            encoding="utf-8",
        )
        cases.append(("invalid-env", env_path))

        too_many = {
            f"s{i}": self.server_settings(command=sys.executable, args=[str(self.fixture)], tools=["*"])
            for i in range(17)
        }
        many_path = self.config_path.parent / "many.json"
        many_path.write_text(json.dumps({"servers": too_many}), encoding="utf-8")
        cases.append(("too-many", many_path))

        for name, path in cases:
            with self.subTest(name=name):
                registry = mcp.McpRegistry(config_path=path, request_timeout=0.2)
                try:
                    definitions, warnings = registry.definitions()
                finally:
                    registry.close()
                self.assertEqual(definitions, [])
                self.assertTrue(warnings)
                text = " ".join(warnings)
                self.assertIn("MCP", text)
                self.assertNotIn("secret-provider-token", text)
                self.assertNotIn("definitely-not-a-real-command-secret", text)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_env_inheritance_is_filtered_and_explicit_values_override(self):
        self.write_config({
            "fixture": self.server_settings(
                tools=["echo"],
                env={"EXPLICIT_ONE": "yes", "PATH": "/custom/bin"},
            )
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(len(definitions), 1)
        self.assertEqual(warnings, [])
        child_env = self.read_child_env()
        self.assertEqual(child_env["PATH"], "/custom/bin")
        self.assertEqual(child_env["EXPLICIT_ONE"], "yes")
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL"):
            self.assertEqual(child_env[key], os.environ[key])
        for forbidden in ("OPENAI_API_KEY", "CODEX_API_KEY", "PASSWORD", "TOKEN"):
            self.assertNotIn(forbidden, child_env)

    def test_duplicate_invalid_and_conflicting_names_disable_servers_atomically(self):
        self.write_config({
            "alpha": self.server_settings(tools=["echo"], scenario="happy"),
            "ALPHA": self.server_settings(tools=["*"], scenario="multi-tools", env={"AIOS_MCP_LOG": str(self.root / "alpha-two.jsonl")}),
            "broken": self.server_settings(tools=["*"], scenario="duplicate-tools", env={"AIOS_MCP_LOG": str(self.root / "broken.jsonl")}),
            "good": self.server_settings(tools=["*"], scenario="multi-tools", env={"AIOS_MCP_LOG": str(self.root / "good.jsonl")}),
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        names = [item["function"]["name"] for item in definitions]
        self.assertEqual(names, ["mcp_alpha_echo", "mcp_good_echo", "mcp_good_spare"])
        joined = " ".join(warnings)
        self.assertIn("ALPHA", joined)
        self.assertIn("broken", joined)
        self.assertNotIn("mcp_alpha_spare", names)
        self.assertNotIn("mcp_broken_echo", names)

    def test_server_requests_are_rejected_with_method_not_found(self):
        self.write_config({
            "fixture": self.server_settings(tools=["*"], scenario="server-request"),
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(len(definitions), 2)
        self.assertEqual(warnings, [])
        response = next(item for item in self.read_log() if item.get("id") == "srv-1")
        self.assertEqual(response["error"]["code"], -32601)

    def test_config_change_restarts_existing_server(self):
        self.write_config({
            "fixture": self.server_settings(tools=["*"], scenario="happy"),
        })
        registry = self.make_registry()
        try:
            first, warnings = registry.definitions()
            self.assertEqual(warnings, [])
            self.assertEqual([item["function"]["name"] for item in first], ["mcp_fixture_echo", "mcp_fixture_hidden"])

            second_log = self.root / "second-log.jsonl"
            self.write_config({
                "fixture": self.server_settings(
                    tools=["*"],
                    scenario="multi-tools",
                    env={"AIOS_MCP_LOG": str(second_log), "AIOS_MCP_ENV_LOG": str(self.env_log)},
                ),
            })
            second, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(warnings, [])
        self.assertEqual([item["function"]["name"] for item in second], ["mcp_fixture_echo", "mcp_fixture_spare"])
        methods = [item["method"] for item in self.read_log() if "method" in item]
        self.assertEqual(methods.count("initialize"), 1)
        second_methods = [json.loads(line)["method"] for line in second_log.read_text(encoding="utf-8").splitlines() if "method" in json.loads(line)]
        self.assertEqual(second_methods[:2], ["initialize", "notifications/initialized"])

    def test_timeout_protocol_failures_and_start_failures_are_safely_redacted(self):
        for scenario in ("init-timeout", "malformed-line", "incomplete-line", "wrong-version", "missing-capability", "oversized-line", "rpc-error"):
            with self.subTest(scenario=scenario):
                self.write_config({
                    "fixture": self.server_settings(tools=["*"], scenario=scenario),
                })
                spawned = []
                original = mcp.subprocess.Popen

                def spawn(*args, **kwargs):
                    process = original(*args, **kwargs)
                    spawned.append(process)
                    return process

                with patch.object(mcp.subprocess, "Popen", side_effect=spawn):
                    registry = self.make_registry(timeout=0.15)
                    try:
                        definitions, warnings = registry.definitions()
                    finally:
                        registry.close()
                self.assertEqual(definitions, [])
                self.assertTrue(warnings)
                text = " ".join(warnings)
                self.assertNotIn("secret-provider-token", text)
                self.assertNotIn(str(self.fixture), text)
                for process in spawned:
                    self.assertIsNotNone(process.poll())

        self.write_config({
            "fixture": self.server_settings(tools=["*"], command="definitely-not-a-real-command-secret"),
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(definitions, [])
        self.assertTrue(warnings)
        self.assertNotIn("definitely-not-a-real-command-secret", " ".join(warnings))

    def test_call_validation_and_result_normalization_fail_safely(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"]),
        })
        registry = self.make_registry()
        try:
            registry.definitions()
            with self.assertRaises(ValueError):
                registry.call("missing_tool", {})
            with self.assertRaises(ValueError):
                registry.call("mcp_fixture_echo", [])
        finally:
            registry.close()

        for scenario in ("unsupported-content", "oversized-result", "call-nondict", "call-error"):
            with self.subTest(scenario=scenario):
                self.write_config({
                    "fixture": self.server_settings(tools=["echo"], scenario=scenario),
                })
                registry = self.make_registry()
                try:
                    registry.definitions()
                    with self.assertRaises(RuntimeError) as error:
                        registry.call("mcp_fixture_echo", {"value": "hello"})
                finally:
                    registry.close()
                self.assertNotIn("secret-provider-token", str(error.exception))

    def test_close_cleans_up_processes_and_is_idempotent(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"], scenario="happy"),
        })
        registry = self.make_registry()
        definitions, warnings = registry.definitions()
        self.assertEqual(len(definitions), 1)
        self.assertEqual(warnings, [])
        client = registry._clients["fixture"]
        process = client.process
        thread = client._reader
        self.assertIsNone(process.poll())
        registry.close()
        registry.close()
        thread.join(timeout=1)
        self.assertIsNotNone(process.poll())
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
