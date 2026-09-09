import errno
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from aios.applications import APPLICATION_TOOL
from aios.browser import TOOL as BROWSER_TOOL
import aios.toolhost as toolhost
from aios.toolhost import (
    MAX_TOOLS,
    REQUEST_LIMIT,
    RESPONSE_LIMIT,
    SAFE_INVALID_REQUEST,
    SAFE_MCP_BUDGET_WARNING,
    SAFE_MCP_FILTER_WARNING,
    SAFE_MCP_LIMIT_WARNING,
    SAFE_OPERATION_FAILED,
    SAFE_SERVICE_UNAVAILABLE,
    SAFE_SOCKET_EXISTS,
    ToolHost,
    call,
    close_service,
    list_tools,
    request,
    serve,
)


class FakeBrowser:
    def __init__(self, result=None, close_error=None):
        self.result = {"browser": True} if result is None else result
        self.close_error = close_error
        self.calls = []
        self.close_calls = 0

    def act(self, arguments):
        self.calls.append(arguments)
        return self.result

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeApplications:
    def __init__(self):
        self.calls = []

    def search(self, payload):
        self.calls.append(("search", payload))
        return [{"id": "match-1"}]

    def create(self, payload):
        self.calls.append(("create", payload))
        return {"id": "draft-1"}

    def read(self, payload):
        self.calls.append(("read", payload))
        return "<!doctype html><title>Example</title>"

    def write(self, payload):
        self.calls.append(("write", payload))
        return {"written": True}

    def publish(self, payload):
        self.calls.append(("publish", payload))
        return {"published": True}

    def launch(self, payload):
        self.calls.append(("launch", payload))
        return {"launched": True}


class FakeMcp:
    def __init__(self, definitions=None, warnings=None, result=None, close_error=None, definition_batches=None):
        self._definitions = list(definitions or [])
        self._warnings = list(warnings or [])
        self._definition_batches = [list(batch) for batch in (definition_batches or [])]
        self.result = {"text": "ok", "structured": {"ok": True}, "is_error": False} if result is None else result
        self.close_error = close_error
        self.calls = []
        self.definition_calls = 0
        self.close_calls = 0

    def definitions(self):
        self.definition_calls += 1
        if self._definition_batches:
            index = min(self.definition_calls - 1, len(self._definition_batches) - 1)
            return list(self._definition_batches[index]), list(self._warnings)
        return list(self._definitions), list(self._warnings)

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeSocketHost:
    def __init__(self):
        self.calls = []
        self.closed = 0
        self.listed = 0

    def definitions(self):
        self.listed += 1
        return {"tools": [BROWSER_TOOL, APPLICATION_TOOL], "warnings": ["MCP warning."]}

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {"tool": name, "arguments": arguments}

    def close(self):
        self.closed += 1


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} description",
            "parameters": {"type": "object"},
        },
    }


def _large_tool(name, minimum_size=32 * 1024 - 256):
    low, high = 0, 40_000
    best = None
    while low <= high:
        mid = (low + high) // 2
        definition = {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} description",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "blob": {
                            "type": "string",
                            "description": "x" * mid,
                        }
                    },
                },
            },
        }
        if _json_size(definition) >= minimum_size:
            best = definition
            high = mid - 1
        else:
            low = mid + 1
    return best


def _frame_payload_with_exact_size(template, limit):
    payload = json.loads(json.dumps(template))
    container = payload
    if "result" in payload:
        container = payload["result"]
    elif "arguments" in payload:
        container = payload["arguments"]
    base = _json_size(payload) + 1
    container["blob"] = "x" * (limit - base)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    if len(encoded) != limit:
        raise AssertionError((len(encoded), limit))
    return payload, encoded


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _wait_for_request_success(path, request_call, timeout=2.0):
    result_box = {}

    def ready():
        try:
            result_box["value"] = request_call(path)
            return True
        except RuntimeError:
            return False

    if not _wait_until(ready, timeout):
        return False, None
    return True, result_box["value"]


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX required")
class ToolHostTests(unittest.TestCase):
    def test_browser_tool_schema_and_definitions_order(self):
        browser = FakeBrowser()
        applications = FakeApplications()
        mcp = FakeMcp(definitions=[_tool("mcp_alpha_tool")], warnings=["MCP retained warning."])
        host = ToolHost(browser=browser, applications=applications, mcp=mcp)

        definitions = host.definitions()

        names = [item["function"]["name"] for item in definitions["tools"]]
        self.assertEqual(names, ["browser", "application", "mcp_alpha_tool"])
        self.assertEqual(definitions["warnings"], ["MCP retained warning."])

        parameters = BROWSER_TOOL["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(BROWSER_TOOL["function"]["name"], "browser")
        self.assertIn("untrusted", BROWSER_TOOL["function"]["description"].lower())
        self.assertEqual(parameters["properties"]["action"]["enum"][0], "open")
        self.assertEqual(parameters["properties"]["direction"]["enum"], ["up", "down"])
        self.assertEqual(parameters["required"], ["action"])
        self.assertEqual(parameters["properties"]["url"]["description"], "HTTP(S) URL for open or navigate")
        self.assertEqual(parameters["properties"]["element"]["description"], "Element ID from the most recent snapshot")
        self.assertEqual(parameters["properties"]["text"]["description"], "Text to type, or Enter/Tab/Escape for press")
        self.assertEqual(parameters["properties"]["tab"]["description"], "Handle returned by tabs")

    def test_browser_application_and_mcp_dispatch_requires_advertisement(self):
        browser = FakeBrowser(result={"snapshot": True})
        applications = FakeApplications()
        mcp_result = {"text": "ok", "structured": {"value": 1}, "is_error": False}
        mcp = FakeMcp(definitions=[_tool("mcp_fixture_echo")], result=mcp_result)
        host = ToolHost(browser=browser, applications=applications, mcp=mcp)

        self.assertEqual(host.call("browser", {"action": "snapshot"}), {"snapshot": True})
        self.assertEqual(host.call("application", {"action": "search", "query": "hello"}), {"matches": [{"id": "match-1"}]})
        self.assertEqual(host.call("application", {"action": "read", "id": "draft-1"}), {"html": "<!doctype html><title>Example</title>"})
        self.assertEqual(host.call("application", {"action": "create", "title": "App", "request": "Build app"}), {"id": "draft-1"})
        with self.assertRaises(ValueError):
            host.call("mcp_fixture_echo", {"value": 1})

        advertised = host.definitions()
        self.assertEqual([item["function"]["name"] for item in advertised["tools"]][-1], "mcp_fixture_echo")
        self.assertEqual(host.call("mcp_fixture_echo", {"value": 1}), mcp_result)

        self.assertEqual(browser.calls, [{"action": "snapshot"}])
        self.assertEqual(applications.calls[0], ("search", {"action": "search", "query": "hello"}))
        self.assertEqual(applications.calls[1], ("read", {"action": "read", "id": "draft-1"}))
        self.assertEqual(applications.calls[2], ("create", {"action": "create", "title": "App", "request": "Build app"}))
        self.assertEqual(mcp.calls, [("mcp_fixture_echo", {"value": 1})])

    def test_unadvertised_prefixed_names_and_definition_changes_are_rejected(self):
        mcp = FakeMcp(definition_batches=[[_tool("mcp_alpha")], [_tool("mcp_beta")]])
        host = ToolHost(browser=FakeBrowser(), applications=FakeApplications(), mcp=mcp)

        self.assertEqual([item["function"]["name"] for item in host.definitions()["tools"]][-1], "mcp_alpha")
        self.assertEqual(host.call("mcp_alpha", {"value": 1}), mcp.result)
        with self.assertRaises(ValueError):
            host.call("mcp_missing", {"value": 2})

        self.assertEqual([item["function"]["name"] for item in host.definitions()["tools"]][-1], "mcp_beta")
        with self.assertRaises(ValueError):
            host.call("mcp_alpha", {"value": 3})
        self.assertEqual(host.call("mcp_beta", {"value": 4}), mcp.result)
        self.assertEqual(mcp.calls, [("mcp_alpha", {"value": 1}), ("mcp_beta", {"value": 4})])

    def test_mcp_error_result_passes_through_unchanged(self):
        result = {"text": "failed", "structured": {"why": "nope"}, "is_error": True}
        host = ToolHost(
            browser=FakeBrowser(),
            applications=FakeApplications(),
            mcp=FakeMcp(definitions=[_tool("mcp_fixture_echo")], result=result),
        )
        host.definitions()
        self.assertEqual(host.call("mcp_fixture_echo", {"value": "x"}), result)

    def test_invalid_conflicting_and_excess_definitions_are_filtered(self):
        many = [_tool(f"mcp_tool_{index}") for index in range(300)]
        definitions = [
            _tool("mcp_first"),
            "bad-definition",
            {"type": "function", "function": {"name": "browser", "description": "shadow", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "mcp_first", "description": "duplicate", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": 5, "description": "bad", "parameters": {"type": "object"}}},
        ] + many
        host = ToolHost(browser=FakeBrowser(), applications=FakeApplications(), mcp=FakeMcp(definitions=definitions, warnings=["MCP original warning."]))

        result = host.definitions()
        names = [item["function"]["name"] for item in result["tools"]]

        self.assertEqual(names[:3], ["browser", "application", "mcp_first"])
        self.assertEqual(len(names), MAX_TOOLS)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(result["warnings"][0], "MCP original warning.")
        self.assertIn(SAFE_MCP_FILTER_WARNING, result["warnings"])
        self.assertIn(SAFE_MCP_LIMIT_WARNING, result["warnings"])
        self.assertNotIn("shadow", " ".join(result["warnings"]))

    def test_large_mcp_catalog_is_budgeted_to_response_envelope(self):
        large_definitions = [_large_tool(f"mcp_big_{index}") for index in range(120)]
        host = ToolHost(browser=FakeBrowser(), applications=FakeApplications(), mcp=FakeMcp(definitions=large_definitions))

        result = host.definitions()
        names = [item["function"]["name"] for item in result["tools"]]
        encoded = json.dumps({"result": result}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"

        self.assertLessEqual(len(encoded), RESPONSE_LIMIT)
        self.assertEqual(names[:2], ["browser", "application"])
        self.assertIn(SAFE_MCP_BUDGET_WARNING, result["warnings"])
        self.assertLess(len(names), len(large_definitions) + 2)

        omitted = next(name for name in [item["function"]["name"] for item in large_definitions] if name not in names)
        kept = next(name for name in names if name.startswith("mcp_big_"))
        self.assertEqual(host.call(kept, {"value": 1}), {"text": "ok", "structured": {"ok": True}, "is_error": False})
        with self.assertRaises(ValueError):
            host.call(omitted, {"value": 2})

    def test_definitions_pack_warnings_only_once_for_large_catalog(self):
        definitions = [_tool(f"mcp_tool_{index:03d}") for index in range(256)]
        warnings = [f"warning-{index}" for index in range(17)]
        host = ToolHost(browser=FakeBrowser(), applications=FakeApplications(), mcp=FakeMcp(definitions=definitions, warnings=warnings))

        with mock.patch("aios.toolhost._pack_definition_warnings", wraps=toolhost._pack_definition_warnings) as pack:
            started = time.monotonic()
            result = host.definitions()
            elapsed = time.monotonic() - started

        encoded = json.dumps({"result": result}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        self.assertLessEqual(pack.call_count, 2)
        self.assertLess(elapsed, 5.0)
        self.assertLessEqual(len(encoded), RESPONSE_LIMIT)
        self.assertEqual(len(result["tools"]), MAX_TOOLS)
        self.assertEqual(result["warnings"], warnings + [SAFE_MCP_LIMIT_WARNING])

    def test_close_is_idempotent_and_attempts_browser_and_mcp_once(self):
        browser = FakeBrowser(close_error=RuntimeError("secret-browser"))
        mcp = FakeMcp(close_error=RuntimeError("secret-mcp"))
        host = ToolHost(browser=browser, applications=FakeApplications(), mcp=mcp)

        host.close()
        host.close()

        self.assertEqual(browser.close_calls, 1)
        self.assertEqual(mcp.close_calls, 1)

    def test_host_rejects_non_json_and_oversized_results_using_result_envelope(self):
        browser = FakeBrowser(result={"value": object()})
        host = ToolHost(browser=browser, applications=FakeApplications(), mcp=FakeMcp())
        with self.assertRaisesRegex(RuntimeError, "JSON-compatible"):
            host.call("browser", {"action": "snapshot"})

        overhead = _json_size({"result": {"value": ""}}) + 1
        browser = FakeBrowser(result={"value": "x" * (RESPONSE_LIMIT - overhead + 1)})
        host = ToolHost(browser=browser, applications=FakeApplications(), mcp=FakeMcp())
        self.assertLess(_json_size(browser.result) + 1, RESPONSE_LIMIT)
        with self.assertRaisesRegex(RuntimeError, "too large"):
            host.call("browser", {"action": "snapshot"})

    def test_socket_round_trip_mode_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            host = FakeSocketHost()
            thread = threading.Thread(target=serve, args=(path, host), daemon=True)
            thread.start()
            ok, listed = _wait_for_request_success(path, lambda target: list_tools(target, timeout=0.1))
            self.assertTrue(ok, "socket was not created")

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(listed, {"tools": [BROWSER_TOOL, APPLICATION_TOOL], "warnings": ["MCP warning."]})
            self.assertEqual(call(path, "browser", {"action": "snapshot"}), {"tool": "browser", "arguments": {"action": "snapshot"}})
            self.assertEqual(close_service(path), {"closed": True})

            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(host.listed, 1)
            self.assertEqual(host.calls, [("browser", {"action": "snapshot"})])
            self.assertEqual(host.closed, 1)
            self.assertFalse(path.exists())

    def test_stalled_large_response_does_not_kill_the_host(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stalled-write.sock"
            large_result, _ = _frame_payload_with_exact_size({"result": {"blob": ""}}, RESPONSE_LIMIT)

            class LargeThenSmallHost(FakeSocketHost):
                def __init__(self, large_result):
                    super().__init__()
                    self.large_result = large_result

                def call(self, name, arguments):
                    self.calls.append((name, arguments))
                    if len(self.calls) == 1:
                        return self.large_result
                    return {"tool": name, "arguments": arguments}

            host = LargeThenSmallHost(large_result["result"])
            thread = threading.Thread(
                target=serve,
                args=(path, host),
                kwargs={"connection_timeout": 0.25},
                daemon=True,
            )
            thread.start()
            self.assertTrue(_wait_until(path.exists), "socket was not created")

            stalled_request = b'{"action":"call","name":"browser","arguments":{"action":"snapshot"}}\n'
            with socket.socket(socket.AF_UNIX) as stalled_client:
                stalled_client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                stalled_client.settimeout(1)
                stalled_client.connect(os.fspath(path))
                stalled_client.sendall(stalled_request)

                self.assertTrue(_wait_until(lambda: len(host.calls) == 1), "first request was not handled")

                result_box = {}
                error_box = {}
                second_done = threading.Event()

                def second_client():
                    try:
                        result_box["value"] = call(path, "browser", {"action": "snapshot"}, timeout=2.0)
                    except BaseException as error:
                        error_box["error"] = error
                    finally:
                        second_done.set()

                second_thread = threading.Thread(target=second_client, daemon=True)
                second_thread.start()
                self.assertTrue(second_done.wait(4.0), "server did not recover from the stalled write")
                second_thread.join(1)
                self.assertNotIn("error", error_box)
                self.assertEqual(result_box["value"], {"tool": "browser", "arguments": {"action": "snapshot"}})
                self.assertEqual(host.calls, [
                    ("browser", {"action": "snapshot"}),
                    ("browser", {"action": "snapshot"}),
                ])
                self.assertEqual(close_service(path), {"closed": True})

            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(host.closed, 1)
            self.assertFalse(path.exists())

    def test_request_times_out_against_drip_fed_response_with_absolute_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "drip-response.sock"
            payload = b'{"result":{"ok":true}}\n'
            accepted = threading.Event()

            def run_server():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(os.fspath(path))
                    server.listen(1)
                    connection, _ = server.accept()
                    accepted.set()
                    with connection:
                        connection.recv(4096)
                        for byte in payload:
                            try:
                                connection.sendall(bytes([byte]))
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                return
                            time.sleep(0.12)

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            self.assertTrue(_wait_until(path.exists), "socket was not created")

            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, SAFE_SERVICE_UNAVAILABLE):
                request(path, {"action": "list"}, timeout=0.35)
            elapsed = time.monotonic() - started

            thread.join(2)
            self.assertTrue(accepted.is_set())
            self.assertLess(elapsed, 0.8)

    def test_drip_fed_request_does_not_block_second_client_past_one_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "drip-request.sock"
            host = FakeSocketHost()
            sent_first = threading.Event()
            stop_drip = threading.Event()

            thread = threading.Thread(target=serve, args=(path, host), kwargs={"connection_timeout": 0.35}, daemon=True)
            thread.start()
            self.assertTrue(_wait_until(path.exists), "socket was not created")

            client = socket.socket(socket.AF_UNIX)
            self.addCleanup(client.close)
            client.settimeout(1)
            client.connect(os.fspath(path))

            def drip_request():
                for index, byte in enumerate(b'{"action":"list"'):
                    if stop_drip.is_set():
                        return
                    try:
                        client.sendall(bytes([byte]))
                    except OSError:
                        return
                    if index == 0:
                        sent_first.set()
                    time.sleep(0.12)

            drip = threading.Thread(target=drip_request, daemon=True)
            drip.start()
            self.assertTrue(sent_first.wait(1.0), "drip client did not start")
            time.sleep(0.05)

            started = time.monotonic()
            listed = list_tools(path, timeout=1.0)
            elapsed = time.monotonic() - started

            stop_drip.set()
            drip.join(2)
            response = client.recv(4096)

            self.assertEqual(listed, {"tools": [BROWSER_TOOL, APPLICATION_TOOL], "warnings": ["MCP warning."]})
            self.assertLess(elapsed, 0.8)
            self.assertEqual(json.loads(response.decode("utf-8")), {"error": SAFE_INVALID_REQUEST})

            self.assertEqual(close_service(path), {"closed": True})
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_response_and_request_boundary_frames_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            response_path = Path(temp) / "response-boundary.sock"
            expected_result, response_payload = _frame_payload_with_exact_size({"result": {"blob": ""}}, RESPONSE_LIMIT)

            def run_response_server():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(os.fspath(response_path))
                    server.listen(1)
                    connection, _ = server.accept()
                    with connection:
                        connection.recv(4096)
                        connection.sendall(response_payload)

            response_thread = threading.Thread(target=run_response_server, daemon=True)
            response_thread.start()
            self.assertTrue(_wait_until(response_path.exists), "response boundary socket was not created")

            self.assertEqual(request(response_path, {"action": "list"}, timeout=1.0), expected_result["result"])
            response_thread.join(2)

            request_path = Path(temp) / "request-boundary.sock"
            host = FakeSocketHost()
            request_thread = threading.Thread(target=serve, args=(request_path, host), kwargs={"connection_timeout": 0.5}, daemon=True)
            request_thread.start()
            self.assertTrue(_wait_until(request_path.exists), "request boundary socket was not created")

            request_value, request_payload = _frame_payload_with_exact_size(
                {"action": "call", "name": "browser", "arguments": {"blob": ""}},
                REQUEST_LIMIT,
            )

            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(1)
                client.connect(os.fspath(request_path))
                client.sendall(request_payload)
                raw = toolhost._read_socket_line(
                    client,
                    RESPONSE_LIMIT,
                    time.monotonic() + 1.0,
                    incomplete_error="test incomplete",
                )

            self.assertEqual(
                json.loads(raw.decode("utf-8")),
                {"result": {"tool": "browser", "arguments": request_value["arguments"]}},
            )
            self.assertEqual(host.calls, [("browser", request_value["arguments"])])
            self.assertEqual(close_service(request_path), {"closed": True})
            request_thread.join(2)
            self.assertFalse(request_thread.is_alive())

    def test_real_toolhost_round_trip_enforces_advertised_mcp_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            mcp = FakeMcp(
                definition_batches=[[_tool("mcp_alpha")], [_tool("mcp_beta")]],
                result={"text": "ok", "structured": {"wire": True}, "is_error": False},
            )
            host = ToolHost(browser=FakeBrowser(), applications=FakeApplications(), mcp=mcp)
            thread = threading.Thread(target=serve, args=(path, host), daemon=True)
            thread.start()

            ok, listed = _wait_for_request_success(path, lambda target: list_tools(target, timeout=0.1))
            self.assertTrue(ok, "service did not start")
            self.assertEqual([item["function"]["name"] for item in listed["tools"]][-1], "mcp_alpha")
            self.assertEqual(call(path, "mcp_alpha", {"value": 1}), {"text": "ok", "structured": {"wire": True}, "is_error": False})
            with self.assertRaisesRegex(RuntimeError, "Unknown tool"):
                call(path, "mcp_ghost", {"value": 2})

            relisted = list_tools(path)
            self.assertEqual([item["function"]["name"] for item in relisted["tools"]][-1], "mcp_beta")
            with self.assertRaisesRegex(RuntimeError, "Unknown tool"):
                call(path, "mcp_alpha", {"value": 3})
            self.assertEqual(call(path, "mcp_beta", {"value": 4}), {"text": "ok", "structured": {"wire": True}, "is_error": False})
            self.assertEqual(close_service(path), {"closed": True})

            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(mcp.calls, [("mcp_alpha", {"value": 1}), ("mcp_beta", {"value": 4})])

    def test_malformed_incomplete_and_oversized_request_never_calls_host(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            host = FakeSocketHost()
            thread = threading.Thread(target=serve, args=(path, host), daemon=True)
            thread.start()
            ok, _ = _wait_for_request_success(path, lambda target: list_tools(target, timeout=0.1))
            self.assertTrue(ok, "socket was not created")

            for payload in (
                b"{not json}\n",
                b'{"action":"list"',
                b'{"action":"' + b"x" * REQUEST_LIMIT + b'"}\n',
            ):
                with self.subTest(payload=payload[:20]):
                    with socket.socket(socket.AF_UNIX) as client:
                        client.settimeout(1)
                        client.connect(os.fspath(path))
                        client.sendall(payload)
                        client.shutdown(socket.SHUT_WR)
                        data = client.recv(4096)
                    self.assertTrue(data.endswith(b"\n"))
                    self.assertEqual(json.loads(data.decode("utf-8")), {"error": "Invalid tool request."})

            self.assertEqual(host.calls, [])
            self.assertEqual(close_service(path), {"closed": True})
            thread.join(2)

    def test_server_returns_safe_error_for_invalid_or_oversized_result(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"

            class ResultHost(FakeSocketHost):
                def __init__(self, result):
                    super().__init__()
                    self.result = result

                def call(self, name, arguments):
                    self.calls.append((name, arguments))
                    return self.result

            for result in (object(), {"blob": "x" * (RESPONSE_LIMIT + 1)}):
                with self.subTest(kind=type(result).__name__):
                    host = ResultHost(result)
                    thread = threading.Thread(target=serve, args=(path, host), daemon=True)
                    thread.start()
                    ok, _ = _wait_for_request_success(path, lambda target: list_tools(target, timeout=0.1))
                    self.assertTrue(ok, "socket was not created")
                    with self.assertRaises(RuntimeError):
                        call(path, "browser", {"action": "snapshot"})
                    self.assertEqual(close_service(path), {"closed": True})
                    thread.join(2)
                    self.assertFalse(thread.is_alive())

    def test_server_uses_safe_fallback_for_empty_adapter_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"

            class EmptyErrorHost(FakeSocketHost):
                def call(self, name, arguments):
                    self.calls.append((name, arguments))
                    raise RuntimeError("")

            host = EmptyErrorHost()
            thread = threading.Thread(target=serve, args=(path, host), daemon=True)
            thread.start()
            ok, _ = _wait_for_request_success(path, lambda target: list_tools(target, timeout=0.1))
            self.assertTrue(ok, "socket was not created")

            with self.assertRaisesRegex(RuntimeError, SAFE_OPERATION_FAILED):
                call(path, "browser", {"action": "snapshot"})

            self.assertEqual(close_service(path), {"closed": True})
            thread.join(2)

    def test_client_rejects_invalid_request_before_connect_and_handles_bad_response(self):
        with mock.patch("aios.toolhost.socket.socket") as socket_factory:
            with self.assertRaises(ValueError):
                request("/missing.sock", {"blob": "x" * REQUEST_LIMIT})
            with self.assertRaises(ValueError):
                request("/missing.sock", {"blob": object()})
            socket_factory.assert_not_called()

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reply.sock"

            def run_server(payload):
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(os.fspath(path))
                    server.listen(1)
                    connection, _ = server.accept()
                    with connection:
                        connection.recv(4096)
                        connection.sendall(payload)

            for payload, error_text in (
                (b'{"result":1}', toolhost.SAFE_INCOMPLETE_RESPONSE),
                (b"[]\n", toolhost.SAFE_INVALID_RESPONSE),
                (b'{"error":5}\n', toolhost.SAFE_INVALID_RESPONSE),
            ):
                with self.subTest(payload=payload):
                    thread = threading.Thread(target=run_server, args=(payload,), daemon=True)
                    thread.start()
                    self.assertTrue(_wait_until(path.exists), "socket was not created")
                    with self.assertRaisesRegex(RuntimeError, error_text):
                        request(path, {"action": "list"}, timeout=1)
                    thread.join(2)
                    if path.exists():
                        path.unlink()

    def test_request_uses_fresh_socket_per_retry_and_retries_eagain(self):
        first = mock.Mock()
        first.connect.side_effect = BlockingIOError(errno.EAGAIN, "again")
        second = mock.Mock()
        second.recv.return_value = b'{"result":{"ok":true}}\n'
        sockets = [first, second]

        with mock.patch("aios.toolhost.socket.socket", side_effect=sockets) as factory, \
            mock.patch("aios.toolhost.time.monotonic", side_effect=[10.0] * 20), \
            mock.patch("aios.toolhost.time.sleep") as sleep:
            result = request("/missing.sock", {"action": "list"}, timeout=1)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(first.connect.call_count, 1)
        first.close.assert_called_once()
        second.connect.assert_called_once()
        second.sendall.assert_called_once()
        second.close.assert_called_once()
        self.assertTrue(sleep.called)

    def test_startup_connect_timeout_is_bounded(self):
        first = mock.Mock()
        second = mock.Mock()
        first.connect.side_effect = FileNotFoundError()
        second.connect.side_effect = socket.timeout()

        with mock.patch("aios.toolhost.socket.socket", side_effect=[first, second]), \
            mock.patch("aios.toolhost.time.monotonic", side_effect=[100.0, 100.0, 100.2, 100.21, 100.45, 100.51, 100.52, 100.53]), \
            mock.patch("aios.toolhost.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, SAFE_SERVICE_UNAVAILABLE):
                request("/missing.sock", {"action": "list"}, timeout=0.5)

        first.close.assert_called_once()
        second.close.assert_called_once()
        self.assertTrue(sleep.called)

    def test_request_times_out_against_stalled_server_with_safe_error_and_bounded_time(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stall.sock"
            stop = threading.Event()
            accepted = threading.Event()

            def run_server():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(os.fspath(path))
                    server.listen(1)
                    connection, _ = server.accept()
                    accepted.set()
                    with connection:
                        stop.wait(1.0)

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            self.assertTrue(_wait_until(path.exists), "socket was not created")

            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, SAFE_SERVICE_UNAVAILABLE):
                request(path, {"action": "list"}, timeout=0.2)
            elapsed = time.monotonic() - started

            stop.set()
            thread.join(2)
            self.assertTrue(accepted.is_set())
            self.assertLess(elapsed, 0.8)

    def test_request_reset_server_returns_safe_error_and_bounded_time(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reset.sock"
            accepted = threading.Event()

            def run_server():
                with socket.socket(socket.AF_UNIX) as server:
                    server.bind(os.fspath(path))
                    server.listen(1)
                    connection, _ = server.accept()
                    accepted.set()
                    with connection:
                        linger = struct.pack("ii", 1, 0)
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                        try:
                            connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            self.assertTrue(_wait_until(path.exists), "socket was not created")

            started = time.monotonic()
            with self.assertRaises(RuntimeError) as raised:
                request(path, {"action": "list"}, timeout=0.5)
            elapsed = time.monotonic() - started

            thread.join(2)
            self.assertTrue(accepted.is_set())
            self.assertIn(str(raised.exception), {SAFE_SERVICE_UNAVAILABLE, toolhost.SAFE_INCOMPLETE_RESPONSE})
            self.assertLess(elapsed, 1.0)

    def test_signal_handlers_are_restored_in_main_thread(self):
        host = FakeSocketHost()
        fake_server = mock.Mock()
        fake_server.__enter__ = mock.Mock(return_value=fake_server)
        fake_server.__exit__ = mock.Mock(return_value=False)
        fake_server.bind.return_value = None
        fake_server.listen.return_value = None
        fake_server.accept.side_effect = SystemExit(0)

        signal_calls = []
        current = threading.current_thread()

        def fake_signal(sig, handler):
            signal_calls.append((sig, handler))
            if callable(handler):
                return f"previous-{sig}"
            return None

        with mock.patch("aios.toolhost.os.path.lexists", return_value=False), \
            mock.patch("aios.toolhost.socket.socket", return_value=fake_server), \
            mock.patch("aios.toolhost.threading.current_thread", return_value=current), \
            mock.patch("aios.toolhost.threading.main_thread", return_value=current), \
            mock.patch("aios.toolhost.signal.signal", side_effect=fake_signal), \
            mock.patch("aios.toolhost.os.umask", side_effect=[0o022, 0o022]), \
            mock.patch("aios.toolhost.os.chmod"), \
            mock.patch("aios.toolhost.os.lstat", return_value=mock.Mock(st_mode=stat.S_IFSOCK, st_dev=1, st_ino=2)), \
            mock.patch("aios.toolhost.Path.unlink") as unlink:
            with self.assertRaises(SystemExit):
                serve("fake.sock", host)

        install_calls = signal_calls[:len(toolhost._socket_signals())]
        restore_calls = signal_calls[len(toolhost._socket_signals()):]
        self.assertEqual([sig for sig, _ in install_calls], toolhost._socket_signals())
        expected_restore = [(sig, f"previous-{sig}") for sig in reversed(toolhost._socket_signals())]
        self.assertEqual(restore_calls, expected_restore)
        self.assertEqual(host.closed, 1)
        self.assertTrue(unlink.called)

    def test_umask_is_restored_when_bind_fails(self):
        host = FakeSocketHost()
        fake_server = mock.Mock()
        fake_server.__enter__ = mock.Mock(return_value=fake_server)
        fake_server.__exit__ = mock.Mock(return_value=False)
        fake_server.bind.side_effect = OSError("boom")
        current = object()

        with mock.patch("aios.toolhost.os.path.lexists", return_value=False), \
            mock.patch("aios.toolhost.socket.socket", return_value=fake_server), \
            mock.patch("aios.toolhost.threading.current_thread", return_value=current), \
            mock.patch("aios.toolhost.threading.main_thread", return_value=None), \
            mock.patch("aios.toolhost.os.umask", side_effect=[0o027, 0o177]) as umask, \
            mock.patch("aios.toolhost.os.lstat", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, SAFE_SERVICE_UNAVAILABLE):
                serve("fake.sock", host)

        self.assertEqual(host.closed, 1)
        self.assertEqual(umask.call_args_list, [mock.call(0o177), mock.call(0o027)])

    def test_bind_race_returns_safe_socket_exists(self):
        host = FakeSocketHost()
        fake_server = mock.Mock()
        fake_server.__enter__ = mock.Mock(return_value=fake_server)
        fake_server.__exit__ = mock.Mock(return_value=False)
        fake_server.bind.side_effect = OSError("boom")
        current = object()

        with mock.patch("aios.toolhost._existing_socket", side_effect=[False, True]), \
            mock.patch("aios.toolhost.socket.socket", return_value=fake_server), \
            mock.patch("aios.toolhost.threading.current_thread", return_value=current), \
            mock.patch("aios.toolhost.threading.main_thread", return_value=None), \
            mock.patch("aios.toolhost.os.umask", side_effect=[0o027, 0o177]), \
            mock.patch("aios.toolhost.os.lstat", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, SAFE_SOCKET_EXISTS):
                serve("fake.sock", host)

        self.assertEqual(host.closed, 1)

    def test_stale_socket_is_reclaimed_and_restarted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            stale = socket.socket(socket.AF_UNIX)
            stale.bind(os.fspath(path))
            stale.close()

            host = FakeSocketHost()
            thread = threading.Thread(target=serve, args=(path, host), daemon=True)
            thread.start()

            ok, listed = _wait_for_request_success(path, lambda target: list_tools(target, timeout=0.1))
            self.assertTrue(ok, "stale socket was not reclaimed")
            self.assertEqual(listed["tools"], [BROWSER_TOOL, APPLICATION_TOOL])

            self.assertEqual(close_service(path), {"closed": True})
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_live_socket_path_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            live = socket.socket(socket.AF_UNIX)
            self.addCleanup(live.close)
            live.bind(os.fspath(path))
            live.listen(1)

            host = FakeSocketHost()
            with self.assertRaises(RuntimeError):
                serve(path, host)

            self.assertTrue(path.exists())
            probe = socket.socket(socket.AF_UNIX)
            try:
                probe.settimeout(0.2)
                probe.connect(os.fspath(path))
            finally:
                probe.close()

    def test_non_socket_path_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            path.write_text("not a socket", encoding="utf-8")
            host = FakeSocketHost()

            with self.assertRaises(RuntimeError):
                serve(path, host)

            self.assertTrue(path.exists())
            self.assertTrue(path.is_file())

    def test_cleanup_does_not_remove_replacement_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            original = socket.socket(socket.AF_UNIX)
            replacement = socket.socket(socket.AF_UNIX)
            try:
                original.bind(os.fspath(path))
                identity = toolhost._socket_identity(os.fspath(path))
                path.unlink()
                replacement.bind(os.fspath(path))

                toolhost._safe_remove_socket(os.fspath(path), identity)

                self.assertTrue(path.exists())
                probe = socket.socket(socket.AF_UNIX)
                try:
                    probe.settimeout(0.2)
                    replacement.listen(1)
                    probe.connect(os.fspath(path))
                finally:
                    probe.close()
            finally:
                original.close()
                replacement.close()


if __name__ == "__main__":
    unittest.main()
