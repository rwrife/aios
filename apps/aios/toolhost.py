"""Generic per-chat tool host over browser, applications, and MCP."""

from __future__ import annotations

import errno
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import threading
import time
from typing import Any

from .applications import APPLICATION_TOOL, BUILD_APPLICATION_TOOL, ApplicationStore
from .browser import TOOL as BROWSER_TOOL, Browser
from .mcp import McpRegistry
from . import os_settings

REQUEST_LIMIT = 128 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
MAX_TOOL_NAME_LENGTH = 128
MAX_TOOLS = 256
RETRY_DELAY_SECONDS = 0.05
SOCKET_TIMEOUT = 60
STARTUP_TIMEOUT = 5
SOCKET_BACKLOG = 8
SOCKET_READ_CHUNK = 4096

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
SAFE_SOCKET_EXISTS = "Tool service socket already exists."
SAFE_MCP_FILTER_WARNING = "MCP some tool definitions were ignored."
SAFE_MCP_LIMIT_WARNING = "MCP some tool definitions were ignored because the tool limit was reached."
SAFE_MCP_BUDGET_WARNING = "MCP some tool definitions were ignored because the response size limit was reached."

_APPLICATION_ACTIONS = (
    "search",
    "create",
    "build",
    "read",
    "write",
    "publish",
    "launch",
)
_MCP_WARNING_RESERVE = (
    SAFE_MCP_FILTER_WARNING,
    SAFE_MCP_LIMIT_WARNING,
    SAFE_MCP_BUDGET_WARNING,
)
_RETRIABLE_CONNECT_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK}
_MISSING = object()
_INTEGRATED_APPLICATIONS = {
    "terminal-00000000": {
        "name": "terminal",
        "title": "Terminal",
        "summary": "AIOS integrated terminal.",
    },
    "settings-00000000": {
        "name": "settings",
        "title": "Settings",
        "summary": "AIOS integrated settings.",
    },
}


def integrated_application_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    for app_id, app in _INTEGRATED_APPLICATIONS.items():
        if normalized in (app_id, app["name"], app["title"].casefold()):
            return app_id
    return None


class _ConnectionWriteFailed(Exception):
    pass


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _encode_json(value: Any) -> bytes:
    return _json_dumps(value).encode("utf-8")


def _serialize_line(value: Any) -> bytes:
    return _encode_json(value) + b"\n"


def _envelope_payload(*, result: Any = _MISSING, error: Any = _MISSING) -> dict[str, Any]:
    if (result is _MISSING) == (error is _MISSING):
        raise ValueError("exactly one envelope field is required")
    if result is not _MISSING:
        return {"result": result}
    return {"error": error}


def _encode_envelope(*, result: Any = _MISSING, error: Any = _MISSING) -> bytes:
    return _serialize_line(_envelope_payload(result=result, error=error))


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


def _static_tools(application_definition: dict[str, Any] = APPLICATION_TOOL) -> list[dict[str, Any]]:
    tools = []
    names = set()
    for definition in (BROWSER_TOOL, application_definition, BUILD_APPLICATION_TOOL, os_settings.TOOL):
        name = _definition_name(definition)
        if name is None or name in names:
            raise RuntimeError("Built-in tool definitions are invalid.")
        tools.append(definition)
        names.add(name)
    return tools


def _validate_result_envelope(result: Any) -> None:
    try:
        encoded = _encode_envelope(result=result)
    except (TypeError, ValueError, RecursionError):
        raise RuntimeError(SAFE_RESULT_NOT_JSON) from None
    if len(encoded) > RESPONSE_LIMIT:
        raise RuntimeError(SAFE_RESULT_TOO_LARGE)


def _read_socket_line(connection: socket.socket, limit: int, deadline: float, *, incomplete_error: str) -> bytes:
    frame = bytearray()
    while True:
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            raise socket.timeout()
        connection.settimeout(remaining)
        chunk = connection.recv(min(SOCKET_READ_CHUNK, limit + 1 - len(frame)))
        if not chunk:
            raise RuntimeError(incomplete_error)
        newline = chunk.find(b"\n")
        if newline != -1:
            frame.extend(chunk[:newline + 1])
            if len(frame) > limit:
                raise RuntimeError(incomplete_error)
            # Protocol is one request per connection, so bytes after the first newline are ignored.
            return bytes(frame)
        frame.extend(chunk)
        if len(frame) > limit:
            raise RuntimeError(incomplete_error)


def _existing_socket(path: str) -> bool:
    return os.path.lexists(path)


def _socket_identity(path: str) -> tuple[int, int] | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _safe_remove_socket(path: str, identity: tuple[int, int] | None) -> bool:
    if identity is None:
        return True
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    if not stat.S_ISSOCK(info.st_mode):
        return False
    if (info.st_dev, info.st_ino) != identity:
        return False
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return True
    return True


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


def _warning_strings(values: list[Any]) -> list[str]:
    warnings = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        warnings.append(value)
        seen.add(value)
    return warnings


def _definitions_result(tools: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    return {"tools": tools, "warnings": warnings}


def _definitions_fit(tools: list[dict[str, Any]], warnings: list[str]) -> bool:
    try:
        return len(_encode_envelope(result=_definitions_result(tools, warnings))) <= RESPONSE_LIMIT
    except (TypeError, ValueError, RecursionError):
        return False


def _pack_definition_warnings(
    tools: list[dict[str, Any]],
    optional_warnings: list[str],
    required_warnings: tuple[str, ...] | list[str] = (),
) -> list[str] | None:
    required = _warning_strings(list(required_warnings))
    if not _definitions_fit(tools, required):
        return None
    kept = []
    for warning in _warning_strings(optional_warnings):
        trial = kept + [warning]
        if _definitions_fit(tools, trial + required):
            kept = trial
    return kept + required


def _service_unavailable_error() -> RuntimeError:
    return RuntimeError(SAFE_SERVICE_UNAVAILABLE)


def _remaining_time(deadline: float) -> float:
    return deadline - time.monotonic()


def _sleep_until_retry(deadline: float) -> None:
    remaining = _remaining_time(deadline)
    if remaining > 0:
        time.sleep(min(RETRY_DELAY_SECONDS, remaining))


def _is_retriable_connect_error(error: BaseException) -> bool:
    if isinstance(error, (FileNotFoundError, ConnectionRefusedError, NotADirectoryError, TimeoutError, socket.timeout)):
        return True
    if isinstance(error, BlockingIOError):
        return getattr(error, "errno", errno.EAGAIN) in _RETRIABLE_CONNECT_ERRNOS
    return False


def _connect_client(path: str, deadline: float) -> socket.socket:
    while True:
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            raise _service_unavailable_error() from None
        client = socket.socket(socket.AF_UNIX)
        try:
            client.settimeout(remaining)
            client.connect(path)
            return client
        except BaseException as error:
            client.close()
            if _is_retriable_connect_error(error):
                if _remaining_time(deadline) <= 0:
                    raise _service_unavailable_error() from None
                _sleep_until_retry(deadline)
                continue
            if isinstance(error, OSError):
                raise _service_unavailable_error() from None
            raise


def _probe_socket_live(path: str) -> bool:
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(RETRY_DELAY_SECONDS)
        try:
            probe.connect(path)
            return True
        except (ConnectionRefusedError, FileNotFoundError):
            return False
        except (TimeoutError, socket.timeout, BlockingIOError, NotADirectoryError, OSError):
            raise RuntimeError(SAFE_SOCKET_EXISTS) from None


def _prepare_socket_path(path: str) -> None:
    if not _existing_socket(path):
        return
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(SAFE_SOCKET_EXISTS)
    identity = info.st_dev, info.st_ino
    if _probe_socket_live(path):
        raise RuntimeError(SAFE_SOCKET_EXISTS)
    if not _safe_remove_socket(path, identity):
        raise RuntimeError(SAFE_SOCKET_EXISTS)


def _safe_error_text(error: BaseException) -> str:
    message = str(error)
    return message if message else SAFE_OPERATION_FAILED


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


def _write_socket_line(connection: socket.socket, payload: dict[str, Any], deadline: float) -> None:
    encoded = _response_bytes(payload)
    view = memoryview(encoded)
    sent = 0
    while sent < len(view):
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            raise _ConnectionWriteFailed
        connection.settimeout(remaining)
        try:
            chunk = view[sent : min(sent + SOCKET_READ_CHUNK, len(view))]
            written = connection.send(chunk)
        except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
            raise _ConnectionWriteFailed from None
        if written <= 0:
            raise _ConnectionWriteFailed
        sent += written


class ToolHost:
    def __init__(self, browser: Browser | None = None, applications: ApplicationStore | None = None, mcp: McpRegistry | None = None):
        self.browser = browser if browser is not None else Browser()
        self.applications = (
            applications if applications is not None
            else ApplicationStore(native_templates=())
        )
        self.mcp = mcp if mcp is not None else McpRegistry()
        self._advertised_mcp = {}
        self._closed = False
        self._close_lock = threading.Lock()

    def definitions(self) -> dict[str, Any]:
        definition_factory = getattr(self.applications, "definition", None)
        application_definition = definition_factory() if callable(definition_factory) else APPLICATION_TOOL
        if not isinstance(application_definition, dict):
            application_definition = APPLICATION_TOOL
        tools = _static_tools(application_definition)
        names = {item["function"]["name"] for item in tools}
        mcp_definitions, mcp_warnings = self.mcp.definitions()
        source_warnings = _warning_strings(mcp_warnings)
        filtered = False
        trimmed = False
        budgeted = False
        advertised_mcp = {}

        for definition in mcp_definitions:
            name = _definition_name(definition)
            if name is None or name in names:
                filtered = True
                continue
            if len(tools) >= MAX_TOOLS:
                trimmed = True
                continue
            candidate_tools = tools + [definition]
            if not _definitions_fit(candidate_tools, list(_MCP_WARNING_RESERVE)):
                budgeted = True
                continue
            tools = candidate_tools
            names.add(name)
            advertised_mcp[name] = definition

        summary_warnings = []
        if filtered:
            summary_warnings.append(SAFE_MCP_FILTER_WARNING)
        if trimmed:
            summary_warnings.append(SAFE_MCP_LIMIT_WARNING)
        if budgeted:
            summary_warnings.append(SAFE_MCP_BUDGET_WARNING)
        warnings = _pack_definition_warnings(tools, source_warnings, summary_warnings)
        if warnings is None:
            warnings = _warning_strings(summary_warnings)

        self._advertised_mcp = dict(advertised_mcp)
        return _definitions_result(tools, warnings)

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a built-in or MCP tool.

        MCP calls require a preceding definitions() refresh and are limited to
        the last advertised MCP tool set.
        """
        if not isinstance(name, str):
            raise ValueError(SAFE_TOOL_NAME)
        if not isinstance(arguments, dict):
            raise ValueError(SAFE_TOOL_ARGUMENTS)
        if name == "browser":
            result = self.browser.act(arguments)
        elif name == "application":
            result = self._call_application(arguments)
        elif name == "build_application":
            result = self._call_application({
                "action": "build",
                "runtime": "web",
                **arguments,
            })
        elif name == "os_settings":
            result = os_settings.act(arguments)
        elif name in self._advertised_mcp:
            result = self.mcp.call(name, arguments)
        else:
            raise ValueError(SAFE_UNKNOWN_TOOL)
        _validate_result_envelope(result)
        return result

    def _call_application(self, arguments: dict[str, Any]) -> Any:
        action = arguments.get("action")
        if action not in _APPLICATION_ACTIONS:
            raise ValueError(SAFE_APPLICATION_ACTION)
        if action == "search":
            query = arguments.get("query")
            if not isinstance(query, str):
                raise ValueError("Enter a search query.")
            query_tokens = set(query.casefold().replace("&", " ").split())
            integrated = [
                {
                    "id": app_id,
                    "title": app["title"],
                    "summary": app["summary"],
                    "exact": query.strip().casefold() == app["title"].casefold(),
                    "runtime": "integrated",
                    "template": None,
                }
                for app_id, app in _INTEGRATED_APPLICATIONS.items()
                if app["name"] in query_tokens
            ]
            return {"matches": [*integrated, *self.applications.search(arguments)][:5]}
        app_id = integrated_application_id(arguments.get("id")) if action == "launch" else None
        if app_id is not None:
            app = _INTEGRATED_APPLICATIONS[app_id]
            opened = os_settings.open_application(app["name"])
            launched = (
                isinstance(opened, dict)
                and (opened.get("opened") is True or opened.get("requested") is True)
            )
            return {
                "launched": launched,
                "id": app_id,
                "title": app["title"],
                "runtime": "integrated",
            }
        method = getattr(self.applications, action)
        result = method(arguments)
        if action == "read":
            return {"html": result}
        return result

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        def close_adapter(closer) -> None:
            try:
                closer()
            except Exception:
                pass

        threads = []
        for name, closer in (
            ("browser", getattr(self.browser, "close", None)),
            ("applications", getattr(self.applications, "close", None)),
            ("mcp", getattr(self.mcp, "close", None)),
        ):
            if closer is None:
                continue
            thread = threading.Thread(
                target=close_adapter,
                args=(closer,),
                name=f"toolhost-close-{name}",
            )
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()


def _validated_timeout(value: Any, label: str) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"Choose a valid tool {label}.") from None
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(f"Choose a valid tool {label}.")
    return duration


def request(
    path: os.PathLike[str] | str,
    value: dict[str, Any],
    timeout: float = SOCKET_TIMEOUT,
    startup_timeout: float = STARTUP_TIMEOUT,
) -> Any:
    if not isinstance(value, dict):
        raise ValueError(SAFE_INVALID_REQUEST)
    try:
        request_bytes = _serialize_line(value)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Tool request must be JSON-compatible.") from None
    if len(request_bytes) > REQUEST_LIMIT:
        raise ValueError("Tool request is too large.")

    operation_timeout = _validated_timeout(timeout, "timeout")
    connect_timeout = _validated_timeout(startup_timeout, "startup timeout")
    start = time.monotonic()
    deadline = start + operation_timeout
    connect_deadline = min(deadline, start + connect_timeout)
    client = _connect_client(os.fspath(path), connect_deadline)
    try:
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            raise _service_unavailable_error() from None
        client.settimeout(remaining)
        client.sendall(request_bytes)
        remaining = _remaining_time(deadline)
        if remaining <= 0:
            raise _service_unavailable_error() from None
        raw = _read_socket_line(client, RESPONSE_LIMIT, deadline, incomplete_error=SAFE_INCOMPLETE_RESPONSE)
    except RuntimeError:
        raise
    except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
        raise _service_unavailable_error() from None
    finally:
        client.close()
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


def list_tools(
    path: os.PathLike[str] | str,
    timeout: float = SOCKET_TIMEOUT,
    startup_timeout: float = STARTUP_TIMEOUT,
) -> dict[str, Any]:
    return request(path, {"action": "list"}, timeout=timeout, startup_timeout=startup_timeout)


def call(
    path: os.PathLike[str] | str,
    name: str,
    arguments: dict[str, Any],
    timeout: float = SOCKET_TIMEOUT,
    startup_timeout: float = STARTUP_TIMEOUT,
) -> Any:
    return request(
        path,
        {"action": "call", "name": name, "arguments": arguments},
        timeout=timeout,
        startup_timeout=startup_timeout,
    )


def close_service(
    path: os.PathLike[str] | str,
    timeout: float = SOCKET_TIMEOUT,
    startup_timeout: float = STARTUP_TIMEOUT,
) -> Any:
    return request(path, {"action": "close"}, timeout=timeout, startup_timeout=startup_timeout)


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


def serve(
    path: os.PathLike[str] | str,
    host: ToolHost | None = None,
    connection_timeout: float = SOCKET_TIMEOUT,
) -> None:
    host = host if host is not None else ToolHost()
    path_text = os.fspath(path)
    created_socket = False
    socket_identity = None
    previous_handlers = _install_signal_handlers()
    try:
        _prepare_socket_path(path_text)
        with socket.socket(socket.AF_UNIX) as server:
            old_umask = os.umask(0o177)
            try:
                try:
                    server.bind(path_text)
                except OSError:
                    if _existing_socket(path_text):
                        raise RuntimeError(SAFE_SOCKET_EXISTS) from None
                    raise _service_unavailable_error() from None
            finally:
                os.umask(old_umask)
            created_socket = True
            socket_identity = _socket_identity(path_text)
            os.chmod(path_text, 0o600)
            server.listen(SOCKET_BACKLOG)
            stop_requested = False
            connection_timeout = max(0.0, float(connection_timeout))
            while not stop_requested:
                connection, _ = server.accept()
                with connection:
                    try:
                        deadline = time.monotonic() + connection_timeout
                        try:
                            raw = _read_socket_line(
                                connection,
                                REQUEST_LIMIT,
                                deadline,
                                incomplete_error=SAFE_INVALID_REQUEST,
                            )
                        except (TimeoutError, socket.timeout, OSError, RuntimeError):
                            raise ValueError(SAFE_INVALID_REQUEST) from None
                        action, name, arguments = _parse_request(raw)
                        if action == "list":
                            payload = {"result": host.definitions()}
                        elif action == "call":
                            payload = {"result": host.call(name, arguments)}
                        else:
                            payload = {"result": {"closed": True}}
                            stop_requested = True
                    except (ValueError, RuntimeError) as error:
                        payload = {"error": _safe_error_text(error)}
                    except Exception:
                        payload = {"error": SAFE_OPERATION_FAILED}
                    try:
                        write_deadline = time.monotonic() + connection_timeout
                        _write_socket_line(connection, payload, write_deadline)
                    except _ConnectionWriteFailed:
                        pass
    finally:
        try:
            host.close()
        finally:
            if created_socket:
                _safe_remove_socket(path_text, socket_identity)
            _restore_signal_handlers(previous_handlers)


if __name__ == "__main__":
    import sys

    serve(sys.argv[1])
