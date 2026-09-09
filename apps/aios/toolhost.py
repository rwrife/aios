"""Generic per-chat tool host over browser, applications, and MCP."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import stat
import threading
import time
from typing import Any

from .applications import APPLICATION_TOOL, ApplicationStore
from .browser import TOOL as BROWSER_TOOL, Browser
from .mcp import McpRegistry

REQUEST_LIMIT = 128 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
MAX_TOOL_NAME_LENGTH = 128
MAX_TOOLS = 256
STARTUP_RETRY_SECONDS = 5.0
RETRY_DELAY_SECONDS = 0.05
SOCKET_TIMEOUT = 60

SAFE_INVALID_REQUEST = "Invalid tool request."
SAFE_INVALID_RESPONSE = "Tool service returned an invalid response."
SAFE_INCOMPLETE_RESPONSE = "Tool service response was incomplete."
SAFE_SERVICE_UNAVAILABLE = "Tool service is unavailable."
SAFE_OPERATION_FAILED = "Tool operation failed."
SAFE_RESPONSE_TOO_LARGE = "Tool response is too large."
SAFE_RESULT_TOO_LARGE = "Tool result is too large."
SAFE_RESULT_NOT_JSON = "Tool result must be JSON-compatible."
SAFE_UNKNOWN_TOOL = "Unknown tool."
SAFE_TOOL_NAME = "Tool name must be a string."
SAFE_TOOL_ARGUMENTS = "Tool arguments must be an object."
SAFE_APPLICATION_ACTION = "Unknown application action."
SAFE_MCP_FILTER_WARNING = "MCP some tool definitions were ignored."
SAFE_MCP_LIMIT_WARNING = "MCP some tool definitions were ignored because the tool limit was reached."

TOOL = BROWSER_TOOL

_STATIC_TOOLS = (BROWSER_TOOL, APPLICATION_TOOL)
_APPLICATION_ACTIONS = (
    "search",
    "create",
    "read",
    "write",
    "publish",
    "launch",
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _encode_json(value: Any) -> bytes:
    return _json_dumps(value).encode("utf-8")


def _serialize_line(value: Any) -> bytes:
    return _encode_json(value) + b"\n"


def _definition_name(definition: Any) -> str | None:
    if not isinstance(definition, dict) or definition.get("type") != "function":
        return None
    function = definition.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    description = function.get("description")
    parameters = function.get("parameters")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(description, str) or not isinstance(parameters, dict):
        return None
    try:
        _encode_json(definition)
    except (TypeError, ValueError, RecursionError):
        return None
    return name


def _static_tools() -> list[dict[str, Any]]:
    tools = []
    names = set()
    for definition in _STATIC_TOOLS:
        name = _definition_name(definition)
        if name is None or name in names:
            raise RuntimeError("Built-in tool definitions are invalid.")
        tools.append(definition)
        names.add(name)
    return tools


def _bounded_json_line(value: Any, limit: int, too_large_message: str) -> bytes:
    try:
        encoded = _serialize_line(value)
    except (TypeError, ValueError, RecursionError):
        raise RuntimeError(SAFE_RESULT_NOT_JSON) from None
    if len(encoded) > limit:
        raise RuntimeError(too_large_message)
    return encoded


def _readline(stream: Any, limit: int) -> bytes:
    raw = stream.readline(limit + 1)
    if not raw.endswith(b"\n"):
        raise RuntimeError(SAFE_INCOMPLETE_RESPONSE)
    if len(raw) > limit:
        raise RuntimeError(SAFE_INCOMPLETE_RESPONSE)
    return raw


def _existing_socket(path: str) -> bool:
    return os.path.lexists(path)


def _safe_remove_socket(path: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(mode):
        Path(path).unlink(missing_ok=True)


def _socket_signals() -> list[int]:
    signals = [signal.SIGTERM]
    hangup = getattr(signal, "SIGHUP", None)
    if hangup is not None:
        signals.append(hangup)
    return signals


def _install_signal_handlers():
    if threading.current_thread() is not threading.main_thread():
        return []

    def terminate(*_):
        raise SystemExit(0)

    previous = []
    for signum in _socket_signals():
        previous.append((signum, signal.signal(signum, terminate)))
    return previous


def _restore_signal_handlers(previous) -> None:
    for signum, handler in reversed(previous):
        signal.signal(signum, handler)


class ToolHost:
    def __init__(self, browser: Browser | None = None, applications: ApplicationStore | None = None, mcp: McpRegistry | None = None):
        self.browser = browser if browser is not None else Browser()
        self.applications = applications if applications is not None else ApplicationStore()
        self.mcp = mcp if mcp is not None else McpRegistry()
        self._closed = False

    def definitions(self) -> dict[str, Any]:
        tools = _static_tools()
        names = {item["function"]["name"] for item in tools}
        mcp_definitions, mcp_warnings = self.mcp.definitions()
        warnings = [warning for warning in mcp_warnings if isinstance(warning, str)]
        filtered = False
        trimmed = False

        for definition in mcp_definitions:
            name = _definition_name(definition)
            if name is None or name in names:
                filtered = True
                continue
            if len(tools) >= MAX_TOOLS:
                trimmed = True
                continue
            tools.append(definition)
            names.add(name)

        if filtered:
            warnings.append(SAFE_MCP_FILTER_WARNING)
        if trimmed:
            warnings.append(SAFE_MCP_LIMIT_WARNING)
        return {"tools": tools, "warnings": warnings}

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if not isinstance(name, str):
            raise ValueError(SAFE_TOOL_NAME)
        if not isinstance(arguments, dict):
            raise ValueError(SAFE_TOOL_ARGUMENTS)
        if name == "browser":
            result = self.browser.act(arguments)
        elif name == "application":
            result = self._call_application(arguments)
        elif name.startswith("mcp_"):
            result = self.mcp.call(name, arguments)
        else:
            raise ValueError(SAFE_UNKNOWN_TOOL)
        _bounded_json_line(result, RESPONSE_LIMIT, SAFE_RESULT_TOO_LARGE)
        return result

    def _call_application(self, arguments: dict[str, Any]) -> Any:
        action = arguments.get("action")
        if action not in _APPLICATION_ACTIONS:
            raise ValueError(SAFE_APPLICATION_ACTION)
        method = getattr(self.applications, action)
        result = method(arguments)
        if action == "search":
            return {"matches": result}
        if action == "read":
            return {"html": result}
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for closer in (getattr(self.browser, "close", None), getattr(self.mcp, "close", None)):
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                pass


def request(path: os.PathLike[str] | str, value: dict[str, Any], timeout: float = SOCKET_TIMEOUT) -> Any:
    if not isinstance(value, dict):
        raise ValueError(SAFE_INVALID_REQUEST)
    try:
        request_bytes = _serialize_line(value)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Tool request must be JSON-compatible.") from None
    if len(request_bytes) > REQUEST_LIMIT:
        raise ValueError("Tool request is too large.")

    deadline = time.monotonic() + min(float(timeout), STARTUP_RETRY_SECONDS)
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        while True:
            try:
                client.connect(os.fspath(path))
                break
            except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError, socket.timeout):
                if time.monotonic() >= deadline:
                    raise RuntimeError(SAFE_SERVICE_UNAVAILABLE) from None
                time.sleep(RETRY_DELAY_SECONDS)
            except OSError:
                raise RuntimeError(SAFE_SERVICE_UNAVAILABLE) from None
        client.sendall(request_bytes)
        with client.makefile("rb") as stream:
            raw = _readline(stream, RESPONSE_LIMIT)
    try:
        response = json.loads(raw)
    except (TypeError, ValueError):
        raise RuntimeError(SAFE_INVALID_RESPONSE) from None
    if not isinstance(response, dict):
        raise RuntimeError(SAFE_INVALID_RESPONSE)
    has_result = "result" in response
    has_error = "error" in response
    if has_result == has_error:
        raise RuntimeError(SAFE_INVALID_RESPONSE)
    if has_error:
        if not isinstance(response["error"], str) or not response["error"]:
            raise RuntimeError(SAFE_INVALID_RESPONSE)
        raise RuntimeError(response["error"])
    return response["result"]


def list_tools(path: os.PathLike[str] | str, timeout: float = SOCKET_TIMEOUT) -> dict[str, Any]:
    return request(path, {"action": "list"}, timeout=timeout)


def call(path: os.PathLike[str] | str, name: str, arguments: dict[str, Any], timeout: float = SOCKET_TIMEOUT) -> Any:
    return request(path, {"action": "call", "name": name, "arguments": arguments}, timeout=timeout)


def close_service(path: os.PathLike[str] | str, timeout: float = SOCKET_TIMEOUT) -> Any:
    return request(path, {"action": "close"}, timeout=timeout)


def _parse_request(raw: bytes) -> tuple[str, str | None, dict[str, Any] | None]:
    if not raw.endswith(b"\n") or len(raw) > REQUEST_LIMIT:
        raise ValueError(SAFE_INVALID_REQUEST)
    try:
        request_value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError(SAFE_INVALID_REQUEST) from None
    if not isinstance(request_value, dict):
        raise ValueError(SAFE_INVALID_REQUEST)
    action = request_value.get("action")
    keys = set(request_value)
    if action == "list" and keys == {"action"}:
        return "list", None, None
    if action == "close" and keys == {"action"}:
        return "close", None, None
    if action == "call" and keys == {"action", "name", "arguments"}:
        name = request_value.get("name")
        arguments = request_value.get("arguments")
        if (not isinstance(name, str) or not name or len(name) > MAX_TOOL_NAME_LENGTH
                or not isinstance(arguments, dict)):
            raise ValueError(SAFE_INVALID_REQUEST)
        return "call", name, arguments
    raise ValueError(SAFE_INVALID_REQUEST)


def _response_bytes(payload: dict[str, Any]) -> bytes:
    try:
        encoded = _serialize_line(payload)
    except (TypeError, ValueError, RecursionError):
        encoded = _serialize_line({"error": SAFE_OPERATION_FAILED})
    if len(encoded) <= RESPONSE_LIMIT:
        return encoded
    fallback = _serialize_line({"error": SAFE_RESPONSE_TOO_LARGE})
    if len(fallback) > RESPONSE_LIMIT:
        return b'{"error":"Tool operation failed."}\n'
    return fallback


def serve(path: os.PathLike[str] | str, host: ToolHost | None = None) -> None:
    host = host if host is not None else ToolHost()
    path_text = os.fspath(path)
    created_socket = False
    previous_handlers = _install_signal_handlers()
    try:
        if _existing_socket(path_text):
            raise RuntimeError("Tool service socket already exists.")
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(path_text)
            created_socket = True
            os.chmod(path_text, 0o600)
            server.listen(1)
            stop_requested = False
            while not stop_requested:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(SOCKET_TIMEOUT)
                    try:
                        with connection.makefile("rb") as stream:
                            raw = stream.readline(REQUEST_LIMIT + 1)
                        action, name, arguments = _parse_request(raw)
                        if action == "list":
                            payload = {"result": host.definitions()}
                        elif action == "call":
                            payload = {"result": host.call(name, arguments)}
                        else:
                            payload = {"result": {"closed": True}}
                            stop_requested = True
                    except (ValueError, RuntimeError) as error:
                        payload = {"error": str(error)}
                    except Exception:
                        payload = {"error": SAFE_OPERATION_FAILED}
                    try:
                        connection.sendall(_response_bytes(payload))
                    except (BrokenPipeError, ConnectionResetError):
                        pass
    finally:
        try:
            host.close()
        finally:
            if created_socket:
                _safe_remove_socket(path_text)
            _restore_signal_handlers(previous_handlers)


if __name__ == "__main__":
    import sys

    serve(sys.argv[1])
