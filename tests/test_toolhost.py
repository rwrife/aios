import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from aios.applications import APPLICATION_TOOL
from aios.toolhost import (
    REQUEST_LIMIT,
    RESPONSE_LIMIT,
    TOOL,
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
    def __init__(self, definitions=None, warnings=None, result=None, close_error=None):
        self._definitions = list(definitions or [])
        self._warnings = list(warnings or [])
        self.result = {"text": "ok", "structured": {"ok": True}, "is_error": False} if result is None else result
        self.close_error = close_error
        self.calls = []
        self.definition_calls = 0
        self.close_calls = 0

    def definitions(self):
        self.definition_calls += 1
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
        return {"tools": [TOOL, APPLICATION_TOOL], "warnings": ["MCP warning."]}

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {"tool": name, "arguments": arguments}

    def close(self):
        self.closed += 1


def _tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} description",
            "parameters": {"type": "object"},
        },
    }


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


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

        parameters = TOOL["function"]["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(TOOL["function"]["name"], "browser")
        self.assertIn("untrusted", TOOL["function"]["description"].lower())
        self.assertEqual(parameters["properties"]["action"]["enum"][0], "open")
        self.assertEqual(parameters["properties"]["direction"]["enum"], ["up", "down"])
        self.assertEqual(parameters["required"], ["action"])

    def test_browser_application_and_mcp_dispatch(self):
        browser = FakeBrowser(result={"snapshot": True})
        applications = FakeApplications()
        mcp_result = {"text": "ok", "structured": {"value": 1}, "is_error": False}
        mcp = FakeMcp(result=mcp_result)
        host = ToolHost(browser=browser, applications=applications, mcp=mcp)

        self.assertEqual(host.call("browser", {"action": "snapshot"}), {"snapshot": True})
        self.assertEqual(host.call("application", {"action": "search", "query": "hello"}), {"matches": [{"id": "match-1"}]})
        self.assertEqual(host.call("application", {"action": "read", "id": "draft-1"}), {"html": "<!doctype html><title>Example</title>"})
        self.assertEqual(host.call("application", {"action": "create", "title": "App", "request": "Build app"}), {"id": "draft-1"})
        self.assertEqual(host.call("mcp_fixture_echo", {"value": 1}), mcp_result)

        self.assertEqual(browser.calls, [{"action": "snapshot"}])
        self.assertEqual(applications.calls[0], ("search", {"action": "search", "query": "hello"}))
        self.assertEqual(applications.calls[1], ("read", {"action": "read", "id": "draft-1"}))
        self.assertEqual(applications.calls[2], ("create", {"action": "create", "title": "App", "request": "Build app"}))
        self.assertEqual(mcp.calls, [("mcp_fixture_echo", {"value": 1})])

    def test_unknown_tool_action_and_nondict_arguments_never_invoke_adapters(self):
        browser = FakeBrowser()
        applications = FakeApplications()
        mcp = FakeMcp()
        host = ToolHost(browser=browser, applications=applications, mcp=mcp)

        with self.assertRaises(ValueError):
            host.call("missing", {})
        with self.assertRaises(ValueError):
            host.call("application", {"action": "delete"})
        with self.assertRaises(ValueError):
            host.call("browser", [])
        with self.assertRaises(ValueError):
            host.call(1, {})

        self.assertEqual(browser.calls, [])
        self.assertEqual(applications.calls, [])
        self.assertEqual(mcp.calls, [])

    def test_mcp_error_result_passes_through_unchanged(self):
        result = {"text": "failed", "structured": {"why": "nope"}, "is_error": True}
        host = ToolHost(browser=FakeBrowser(), applications=FakeApplications(), mcp=FakeMcp(result=result))
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
        self.assertEqual(len(names), 256)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(result["warnings"][0], "MCP original warning.")
        self.assertIn("MCP some tool definitions were ignored.", result["warnings"])
        self.assertIn("MCP some tool definitions were ignored because the tool limit was reached.", result["warnings"])
        self.assertNotIn("shadow", " ".join(result["warnings"]))

    def test_close_is_idempotent_and_attempts_browser_and_mcp_once(self):
        browser = FakeBrowser(close_error=RuntimeError("secret-browser"))
        mcp = FakeMcp(close_error=RuntimeError("secret-mcp"))
        host = ToolHost(browser=browser, applications=FakeApplications(), mcp=mcp)

        host.close()
        host.close()

        self.assertEqual(browser.close_calls, 1)
        self.assertEqual(mcp.close_calls, 1)

    def test_host_rejects_non_json_and_oversized_results(self):
        browser = FakeBrowser(result={"value": object()})
        host = ToolHost(browser=browser, applications=FakeApplications(), mcp=FakeMcp())
        with self.assertRaisesRegex(RuntimeError, "JSON-compatible"):
            host.call("browser", {"action": "snapshot"})

        browser = FakeBrowser(result={"value": "x" * (RESPONSE_LIMIT + 1)})
        host = ToolHost(browser=browser, applications=FakeApplications(), mcp=FakeMcp())
        with self.assertRaisesRegex(RuntimeError, "too large"):
            host.call("browser", {"action": "snapshot"})

    def test_socket_round_trip_mode_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            host = FakeSocketHost()
            thread = threading.Thread(target=serve, args=(path, host), daemon=True)
            thread.start()
            self.assertTrue(_wait_until(path.exists), "socket was not created")

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(list_tools(path), {"tools": [TOOL, APPLICATION_TOOL], "warnings": ["MCP warning."]})
            self.assertEqual(call(path, "browser", {"action": "snapshot"}), {"tool": "browser", "arguments": {"action": "snapshot"}})
            self.assertEqual(close_service(path), {"closed": True})

            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(host.listed, 1)
            self.assertEqual(host.calls, [("browser", {"action": "snapshot"})])
            self.assertEqual(host.closed, 1)
            self.assertFalse(path.exists())

    def test_malformed_incomplete_and_oversized_request_never_calls_host(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "toolhost.sock"
            host = FakeSocketHost()
            thread = threading.Thread(target=serve, args=(path, host), daemon=True)
            thread.start()
            self.assertTrue(_wait_until(path.exists), "socket was not created")

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

            self.assertEqual(host.listed, 0)
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
                    self.assertTrue(_wait_until(path.exists), "socket was not created")
                    with self.assertRaises(RuntimeError):
                        call(path, "browser", {"action": "snapshot"})
                    self.assertEqual(close_service(path), {"closed": True})
                    thread.join(2)
                    self.assertFalse(thread.is_alive())

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

            for payload in (b'{"result":1}', b"[]\n", b'{"error":5}\n'):
                with self.subTest(payload=payload):
                    thread = threading.Thread(target=run_server, args=(payload,), daemon=True)
                    thread.start()
                    self.assertTrue(_wait_until(path.exists), "socket was not created")
                    with self.assertRaises(RuntimeError):
                        request(path, {"action": "list"}, timeout=1)
                    thread.join(2)
                    if path.exists():
                        path.unlink()

    def test_startup_connect_timeout_is_bounded(self):
        fake_socket = mock.Mock()
        fake_socket.__enter__ = mock.Mock(return_value=fake_socket)
        fake_socket.__exit__ = mock.Mock(return_value=False)
        fake_socket.connect.side_effect = FileNotFoundError()
        times = iter([100.0, 100.0, 101.0, 103.0, 105.1])

        with mock.patch("aios.toolhost.socket.socket", return_value=fake_socket), \
            mock.patch("aios.toolhost.time.monotonic", side_effect=lambda: next(times)), \
            mock.patch("aios.toolhost.time.sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                request("/missing.sock", {"action": "list"}, timeout=60)

        self.assertGreaterEqual(fake_socket.connect.call_count, 1)
        self.assertTrue(sleep.called)

    def test_signal_handlers_are_restored_in_main_thread(self):
        host = FakeSocketHost()
        fake_server = mock.Mock()
        fake_server.__enter__ = mock.Mock(return_value=fake_server)
        fake_server.__exit__ = mock.Mock(return_value=False)
        fake_server.accept.side_effect = SystemExit(0)

        signal_calls = []
        current = threading.current_thread()

        def fake_signal(sig, handler):
            signal_calls.append((sig, handler))
            return f"previous-{sig}"

        with mock.patch("aios.toolhost.os.path.lexists", return_value=False), \
            mock.patch("aios.toolhost.socket.socket", return_value=fake_server), \
            mock.patch("aios.toolhost.threading.current_thread", return_value=current), \
            mock.patch("aios.toolhost.threading.main_thread", return_value=current), \
            mock.patch("aios.toolhost.signal.signal", side_effect=fake_signal), \
            mock.patch("aios.toolhost.os.chmod"), \
            mock.patch("aios.toolhost.os.lstat", return_value=mock.Mock(st_mode=stat.S_IFSOCK)), \
            mock.patch("aios.toolhost.Path.unlink") as unlink:
            with self.assertRaises(SystemExit):
                serve("/tmp/fake.sock", host)

        self.assertEqual(host.closed, 1)
        self.assertGreaterEqual(len(signal_calls), 2)
        self.assertTrue(unlink.called)


if __name__ == "__main__":
    unittest.main()
