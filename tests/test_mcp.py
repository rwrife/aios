import json
import os
from pathlib import Path
import queue
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
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
        self.registries = []

    def tearDown(self):
        for registry in reversed(self.registries):
            registry.close()
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

    def make_registry(self, timeout=1.0, **kwargs):
        registry = mcp.McpRegistry(config_path=self.config_path, request_timeout=timeout, **kwargs)
        self.registries.append(registry)
        return registry

    def bounded(self, function, timeout=1.0, cleanup=None):
        outcomes = queue.Queue()

        def invoke():
            try:
                outcomes.put((True, function()))
            except BaseException as error:
                outcomes.put((False, error))

        thread = threading.Thread(target=invoke, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(timeout)
        elapsed = time.monotonic() - started
        finished = not thread.is_alive()
        if not finished and cleanup is not None:
            cleanup()
            thread.join(1)
        outcome = outcomes.get_nowait() if not outcomes.empty() else None
        return finished, outcome, elapsed

    def kill_process_group(self, process, process_group=None):
        if process is None:
            return
        if os.name == "posix":
            if process_group is None and process.poll() is None:
                try:
                    process_group = os.getpgid(process.pid)
                except ProcessLookupError:
                    return
            if process_group is None:
                return
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()

    def kill_registry_processes(self, registry):
        for client in list(registry._clients.values()):
            self.kill_process_group(client.process, client._process_group)

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

    def test_call_uses_valid_cached_mapping_but_still_detects_config_changes(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"]),
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
            self.assertEqual(warnings, [])
            self.assertEqual([item["function"]["name"] for item in definitions], ["mcp_fixture_echo"])
            with patch.object(registry, "definitions", wraps=registry.definitions) as refresh:
                result = registry.call("mcp_fixture_echo", {"value": "cached"})
                refresh.assert_not_called()
                self.write_config({
                    "fixture": self.server_settings(tools=[]),
                })
                with self.assertRaises(RuntimeError):
                    registry.call("mcp_fixture_echo", {"value": "disabled"})
                refresh.assert_called_once()
        finally:
            registry.close()
        self.assertEqual(result["text"], "echo:cached")
        list_calls = [item for item in self.read_log() if item.get("method") == "tools/list"]
        self.assertEqual(len(list_calls), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX pipe behavior required")
    def test_child_pipes_are_unbuffered(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"]),
        })
        registry = self.make_registry()
        try:
            registry.definitions()
            client = registry._clients["fixture"]
            self.assertEqual(client.process.stdin.__class__.__name__, "FileIO")
            self.assertEqual(client.process.stdout.__class__.__name__, "FileIO")
        finally:
            registry.close()

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

    def test_unknown_server_setting_is_rejected(self):
        settings = self.server_settings()
        settings["requestTimout"] = 1
        self.write_config({"fixture": settings})
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(definitions, [])
        self.assertTrue(warnings)
        self.assertFalse(self.log.exists())

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW required")
    def test_config_is_opened_with_nofollow(self):
        self.write_config({})
        original_open = os.open
        flags_seen = []

        def tracking_open(path, flags, *args):
            flags_seen.append(flags)
            return original_open(path, flags, *args)

        with patch.object(mcp.os, "open", side_effect=tracking_open):
            servers, warnings = mcp._load_servers(self.config_path)
        self.assertEqual(servers, [])
        self.assertEqual(warnings, [])
        self.assertTrue(flags_seen)
        self.assertTrue(flags_seen[0] & os.O_NOFOLLOW)

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

    @unittest.skipUnless(os.name == "posix", "POSIX pipe behavior required")
    def test_large_call_to_nonreading_server_obeys_request_timeout(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"], scenario="stop-reading-after-list"),
        })
        registry = self.make_registry(timeout=0.3)
        try:
            finished, outcome, elapsed = self.bounded(
                registry.definitions,
                timeout=0.9,
                cleanup=lambda: self.kill_registry_processes(registry),
            )
            self.assertTrue(finished, f"definitions blocked for {elapsed:.3f}s")
            self.assertTrue(outcome[0])
            client = registry._clients["fixture"]
            process = client.process
            process_group = client._process_group
            finished, outcome, elapsed = self.bounded(
                lambda: registry.call("mcp_fixture_echo", {"value": "x" * (1024 * 1024)}),
                timeout=0.9,
                cleanup=lambda: self.kill_process_group(process, process_group),
            )
            self.assertTrue(finished, f"call blocked for {elapsed:.3f}s")
            self.assertFalse(outcome[0])
            self.assertIsInstance(outcome[1], RuntimeError)
            self.assertLess(elapsed, 0.9)
            self.assertEqual(client._issued_ids, set())
            self.assertEqual(client._active_ids, set())
            self.assertEqual(registry._failures["fixture"][0]["tools"], ["echo"])
        finally:
            self.kill_registry_processes(registry)
            registry.close()

    @unittest.skipUnless(os.name == "posix", "POSIX pipe behavior required")
    def test_server_request_flood_keeps_call_and_close_bounded(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"], scenario="flood-client-requests"),
        })
        registry = self.make_registry(timeout=0.3)
        try:
            finished, outcome, elapsed = self.bounded(
                registry.definitions,
                timeout=0.9,
                cleanup=lambda: self.kill_registry_processes(registry),
            )
            self.assertTrue(finished, f"definitions blocked for {elapsed:.3f}s")
            self.assertTrue(outcome[0])
            client = registry._clients["fixture"]
            process = client.process
            process_group = client._process_group
            time.sleep(0.1)
            finished, outcome, elapsed = self.bounded(
                lambda: registry.call("mcp_fixture_echo", {"value": "x" * (512 * 1024)}),
                timeout=0.9,
                cleanup=lambda: self.kill_process_group(process, process_group),
            )
            self.assertTrue(finished, f"call blocked for {elapsed:.3f}s")
            self.assertFalse(outcome[0])
            self.assertIsInstance(outcome[1], RuntimeError)
            finished, outcome, elapsed = self.bounded(
                registry.close,
                timeout=0.9,
                cleanup=lambda: self.kill_process_group(process, process_group),
            )
            self.assertTrue(finished, f"close blocked for {elapsed:.3f}s")
            self.assertTrue(outcome[0])
        finally:
            self.kill_registry_processes(registry)
            registry.close()

    def test_outbound_messages_over_line_limit_are_rejected_before_write(self):
        writes = []

        class Stdin:
            def write(self, data):
                writes.append(data)

            def flush(self):
                pass

        client = mcp.McpClient("fixture", self.server_settings(), request_timeout=0.1)
        client.process = SimpleNamespace(stdin=Stdin())
        with self.assertRaises((ValueError, RuntimeError)):
            client._send({"value": "x" * mcp.LINE_LIMIT})
        self.assertEqual(writes, [])

    def test_reader_recursion_error_sets_fatal_without_escaping(self):
        class Stdout:
            def readline(self, limit):
                return b'{"jsonrpc":"2.0"}\n'

        client = mcp.McpClient("fixture", self.server_settings(), request_timeout=0.1)
        client.process = SimpleNamespace(stdout=Stdout())
        with patch.object(mcp.json, "loads", side_effect=RecursionError):
            client._read_loop()
        self.assertIsNotNone(client._fatal)

    def test_fatal_state_wakes_waiter_even_when_event_queue_is_full(self):
        client = mcp.McpClient("fixture", self.server_settings(), request_timeout=0.1)
        for _ in range(mcp.QUEUE_LIMIT):
            client._events.put_nowait({"jsonrpc": "2.0", "id": "noise"})
        client._set_fatal("busy")
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            client._receive(0.5)
        self.assertLess(time.monotonic() - started, 0.1)

    def test_unsolicited_future_response_cannot_preanswer_request(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"], scenario="spoof-future-id"),
        })
        registry = self.make_registry()
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(warnings, [])
        self.assertEqual([item["function"]["name"] for item in definitions], ["mcp_fixture_echo"])

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

    def test_failure_cooldown_avoids_respawn_and_retries_after_clock_advance(self):
        now = [100.0]
        self.write_config({
            "fixture": self.server_settings(tools=["*"], scenario="malformed-line"),
        })
        starts = []
        original = mcp.subprocess.Popen

        def spawn(*args, **kwargs):
            starts.append(now[0])
            return original(*args, **kwargs)

        with patch.object(mcp.subprocess, "Popen", side_effect=spawn):
            registry = self.make_registry(failure_cooldown=5, clock=lambda: now[0])
            try:
                for _ in range(3):
                    definitions, warnings = registry.definitions()
                    self.assertEqual(definitions, [])
                    self.assertTrue(warnings)
                self.assertEqual(len(starts), 1)
                now[0] += 5.1
                registry.definitions()
                self.assertEqual(len(starts), 2)
            finally:
                registry.close()

    def test_config_change_clears_failure_cooldown(self):
        now = [100.0]
        self.write_config({
            "fixture": self.server_settings(tools=["*"], scenario="malformed-line"),
        })
        registry = self.make_registry(failure_cooldown=5, clock=lambda: now[0])
        try:
            definitions, warnings = registry.definitions()
            self.assertEqual(definitions, [])
            self.assertTrue(warnings)
            self.write_config({
                "fixture": self.server_settings(tools=["echo"], scenario="happy"),
            })
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(warnings, [])
        self.assertEqual([item["function"]["name"] for item in definitions], ["mcp_fixture_echo"])

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
                    definitions, warnings = registry.definitions()
                    self.assertEqual(warnings, [])
                    self.assertEqual([item["function"]["name"] for item in definitions], ["mcp_fixture_echo"])
                    with self.assertRaises(RuntimeError) as error:
                        registry.call("mcp_fixture_echo", {"value": "hello"})
                finally:
                    registry.close()
                self.assertNotIn("secret-provider-token", str(error.exception))

    def test_call_arguments_must_be_json_compatible_without_repr_leakage(self):
        class SecretObject:
            def __repr__(self):
                return "secret-provider-token"

        self.write_config({
            "fixture": self.server_settings(tools=["echo"]),
        })
        registry = self.make_registry()
        try:
            registry.definitions()
            for arguments in ({"value": SecretObject()}, {"value": float("nan")}):
                with self.subTest(arguments=type(arguments["value"]).__name__):
                    with self.assertRaises((ValueError, RuntimeError)) as error:
                        registry.call("mcp_fixture_echo", arguments)
                    self.assertNotIn("secret-provider-token", str(error.exception))
        finally:
            registry.close()

    def test_invalid_huge_and_deep_tool_schemas_are_rejected(self):
        for scenario in ("wrong-schema-type", "huge-schema", "deep-schema"):
            with self.subTest(scenario=scenario):
                self.write_config({
                    "fixture": self.server_settings(tools=["*"], scenario=scenario),
                })
                registry = self.make_registry()
                try:
                    definitions, warnings = registry.definitions()
                finally:
                    registry.close()
                self.assertEqual(definitions, [])
                self.assertTrue(warnings)

    def test_total_exposed_definitions_are_capped(self):
        servers = {}
        for index in range(16):
            servers[f"server-{index}"] = self.server_settings(
                tools=["*"],
                scenario="many-tools",
                env={
                    "AIOS_MCP_LOG": str(self.root / f"server-{index}.jsonl"),
                    "AIOS_MCP_ENV_LOG": str(self.root / f"server-{index}-env.json"),
                    "AIOS_MCP_TOOL_COUNT": "20",
                },
            )
        self.write_config(servers)
        registry = self.make_registry(timeout=0.5)
        try:
            definitions, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertLessEqual(len(definitions), 256)
        self.assertTrue(warnings)

    def test_dead_server_does_not_advertise_cached_tools(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"], scenario="exit-after-list"),
        })
        registry = self.make_registry()
        try:
            first, warnings = registry.definitions()
            self.assertEqual(len(first), 1)
            self.assertEqual(warnings, [])
            client = registry._clients["fixture"]
            client.process.wait(timeout=1)
            second, warnings = registry.definitions()
        finally:
            registry.close()
        self.assertEqual(second, [])
        self.assertTrue(warnings)

    def test_registry_replaces_closed_client_and_client_cannot_restart(self):
        self.write_config({
            "fixture": self.server_settings(tools=["echo"]),
        })
        registry = self.make_registry()
        try:
            registry.definitions()
            first = registry._clients["fixture"]
            first.close()
            with self.assertRaises(RuntimeError):
                first.start()
            definitions, warnings = registry.definitions()
            second = registry._clients["fixture"]
        finally:
            registry.close()
        self.assertIsNot(first, second)
        self.assertEqual(warnings, [])
        self.assertEqual(len(definitions), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_close_kills_process_group_after_direct_child_exits(self):
        grandchild_path = self.root / "grandchild.pid"
        self.write_config({
            "fixture": self.server_settings(
                tools=["echo"],
                scenario="grandchild-holds-stdout",
                env={"AIOS_MCP_GRANDCHILD_PID": str(grandchild_path)},
            ),
        })
        registry = self.make_registry()
        try:
            finished, outcome, elapsed = self.bounded(
                registry.definitions,
                timeout=2.0,
                cleanup=lambda: self.kill_registry_processes(registry),
            )
            self.assertTrue(finished, f"definitions blocked for {elapsed:.3f}s")
            self.assertTrue(outcome[0])
            definitions, warnings = outcome[1]
            self.assertEqual(len(definitions), 1)
            self.assertEqual(warnings, [])
            client = registry._clients["fixture"]
            process = client.process
            process_group = client._process_group
            process.wait(timeout=1)
            deadline = time.monotonic() + 1
            while not grandchild_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(grandchild_path.exists())
            grandchild_pid = int(grandchild_path.read_text(encoding="utf-8"))
            finished, outcome, elapsed = self.bounded(
                registry.close,
                timeout=0.75,
                cleanup=lambda: self.kill_process_group(process, process_group),
            )
            self.assertTrue(finished, f"close blocked for {elapsed:.3f}s")
            self.assertTrue(outcome[0])
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild_pid, 0)
        finally:
            self.kill_registry_processes(registry)
            registry.close()

    def test_tool_cache_has_a_dedicated_lock(self):
        client = mcp.McpClient("fixture", self.server_settings(), request_timeout=0.1)
        self.assertTrue(hasattr(client, "_tools_lock"))

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
