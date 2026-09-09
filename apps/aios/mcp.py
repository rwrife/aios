"""Allowlisted stdio MCP tools."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import selectors
import signal
import stat
import subprocess
import threading
import time

from . import core

CONFIG_LIMIT = 256 * 1024
LINE_LIMIT = 4 * 1024 * 1024
RESULT_LIMIT = 64 * 1024
MAX_SERVERS = 16
MAX_PAGES = 20
MAX_TOOLS = 256
QUEUE_LIMIT = 128
DESCRIPTION_LIMIT = 300
SCHEMA_LIMIT = 32 * 1024
SCHEMA_DEPTH_LIMIT = 32
DEFAULT_FAILURE_COOLDOWN = 5
PROTOCOL_VERSION = "2025-06-18"
INHERITED_ENV = ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL")

SAFE_SERVER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _warning(message):
    return "MCP " + message


def _safe_server_message(name, reason):
    return _warning(f'server "{name}" {reason}.')


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _check_json_value(value):
    try:
        _json_dumps(value)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("invalid value") from None


def _valid_input_schema(schema):
    if not isinstance(schema, dict) or schema.get("type") not in (None, "object"):
        return False
    try:
        encoded = _json_dumps(schema).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return False
    if len(encoded) > SCHEMA_LIMIT:
        return False
    stack = [(schema, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > SCHEMA_DEPTH_LIMIT:
            return False
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values() if isinstance(item, (dict, list)))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value if isinstance(item, (dict, list)))
    return True


def _sanitize_identifier(value):
    lowered = value.lower()
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    return cleaned


def _exposed_name(server_name, tool_name):
    server_slug = _sanitize_identifier(server_name)
    tool_slug = _sanitize_identifier(tool_name)
    if not server_slug or not tool_slug:
        return None
    base = f"mcp_{server_slug}_{tool_slug}"
    if len(base) <= 64:
        return base
    digest = hashlib.sha256((server_name + "\0" + tool_name).encode("utf-8")).hexdigest()[:12]
    keep = 64 - len("mcp__") - len(digest)
    if keep < 3:
        return None
    combined = (server_slug + "_" + tool_slug)[:keep].rstrip("-_")
    if not combined:
        return None
    return f"mcp_{combined}_{digest}"


def _tool_description(server_name, tool):
    detail = tool.get("description") or tool.get("title") or tool["name"]
    text = f"{server_name}: {detail}".replace("\r", " ").replace("\n", " ").strip()
    return text[:DESCRIPTION_LIMIT]


def _config_path(path):
    return Path(path) if path is not None else core.config_dir() / "mcp.json"


def _validate_env(env):
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ValueError("env")
    clean = {}
    for key, value in env.items():
        if (not isinstance(key, str) or not SAFE_ENV_NAME.fullmatch(key) or "\0" in key
                or not isinstance(value, str) or "\0" in value):
            raise ValueError("env")
        clean[key] = value
    return clean


def _validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("settings")
    if not set(settings).issubset({"command", "args", "env", "tools"}):
        raise ValueError("settings")
    command = settings.get("command")
    if not isinstance(command, str) or not command.strip() or "\0" in command:
        raise ValueError("command")
    args = settings.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise ValueError("args")
    tools = settings.get("tools")
    if not isinstance(tools, list) or any(not isinstance(tool, str) or not tool or "\0" in tool for tool in tools):
        raise ValueError("tools")
    return {
        "command": command,
        "args": list(args),
        "env": _validate_env(settings.get("env", {})),
        "tools": list(tools),
    }


class _ConfigNotRegular(Exception):
    pass


class _ConfigTooLarge(Exception):
    pass


def _read_config(path):
    if os.name == "posix" and hasattr(os, "O_NOFOLLOW"):
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _ConfigNotRegular
            if info.st_size > CONFIG_LIMIT:
                raise _ConfigTooLarge
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, CONFIG_LIMIT + 1 - total))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                total += len(chunk)
                if total > CONFIG_LIMIT:
                    raise _ConfigTooLarge
        finally:
            os.close(descriptor)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _ConfigNotRegular
    if info.st_size > CONFIG_LIMIT:
        raise _ConfigTooLarge
    data = Path(path).read_bytes()
    if len(data) > CONFIG_LIMIT:
        raise _ConfigTooLarge
    return data


def _load_servers(config_path):
    path = _config_path(config_path)
    try:
        data = _read_config(path)
    except FileNotFoundError:
        return [], []
    except _ConfigNotRegular:
        return [], [_warning("configuration file was ignored because it is not a regular file")]
    except _ConfigTooLarge:
        return [], [_warning("configuration file was ignored because it is too large")]
    except OSError:
        return [], [_warning("configuration file was ignored because it is invalid")]
    try:
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return [], [_warning("configuration file was ignored because it is invalid")]
    if not isinstance(value, dict) or set(value) != {"servers"} or not isinstance(value.get("servers"), dict):
        return [], [_warning("configuration file was ignored because it must contain only a servers object")]
    servers = value["servers"]
    if len(servers) > MAX_SERVERS:
        return [], [_warning("configuration file was ignored because it defines too many servers")]
    cleaned = []
    warnings = []
    for name, settings in servers.items():
        if not isinstance(name, str) or not SAFE_SERVER_NAME.fullmatch(name):
            warnings.append(_warning('server configuration was ignored because a server name is invalid'))
            continue
        try:
            cleaned.append((name, _validate_settings(settings)))
        except ValueError:
            warnings.append(_safe_server_message(name, "has invalid configuration and was ignored"))
    return cleaned, warnings


def _child_env(explicit_env):
    env = {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
    env.update(explicit_env)
    return env


class McpClient:
    def __init__(self, server_name, settings, request_timeout=10):
        self.server_name = server_name
        self.settings = settings
        self.request_timeout = request_timeout
        self.process = None
        self._process_group = None
        self._reader = None
        self._events = queue.Queue(maxsize=QUEUE_LIMIT)
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._response_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._issued_ids = set()
        self._active_ids = set()
        self._sequence = 0
        self._closed = False
        self._fatal = None
        self._fatal_event = threading.Event()
        self._tools_lock = threading.Lock()
        self._tools = None
        self._tools_dirty = True
        self._tool_changes = 0

    def start(self):
        if self._closed:
            raise RuntimeError("MCP connection closed.")
        if self.process is not None:
            if self._fatal is not None or self.process.poll() is not None:
                raise RuntimeError("MCP connection closed.")
            return
        options = {
            "args": [self.settings["command"], *self.settings["args"]],
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "env": _child_env(self.settings["env"]),
            "bufsize": 0,
        }
        if os.name == "posix":
            options["start_new_session"] = True
        try:
            self.process = subprocess.Popen(**options)
            if os.name == "posix":
                self._process_group = os.getpgid(self.process.pid)
                os.set_blocking(self.process.stdin.fileno(), False)
            self._reader = threading.Thread(target=self._read_loop, name=f"mcp-{self.server_name}", daemon=True)
            self._reader.start()
            self._initialize()
        except OSError:
            self.close()
            raise RuntimeError("MCP server could not start.") from None
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._set_fatal("closed")
            process = self.process
            process_group = self._process_group
            stdin = getattr(process, "stdin", None) if process else None
            stdout = getattr(process, "stdout", None) if process else None
            if process:
                if os.name == "posix":
                    group_alive = process_group is not None and self._signal_group(process_group, signal.SIGTERM)
                    try:
                        process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
                    deadline = time.monotonic() + 0.2
                    while group_alive and time.monotonic() < deadline:
                        group_alive = self._group_exists(process_group)
                        if not group_alive:
                            break
                        time.sleep(0.01)
                    if group_alive:
                        self._signal_group(process_group, signal.SIGKILL)
                elif process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except OSError:
                            pass
            if self._reader:
                self._reader.join(timeout=0.25)
            for stream in (stdin, stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if self._reader and self._reader.is_alive():
                self._reader.join(timeout=0.1)
            if process:
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            self.process = None
            self._process_group = None

    @staticmethod
    def _signal_group(process_group, sig):
        try:
            os.killpg(process_group, sig)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def _group_exists(process_group):
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def is_usable(self):
        process = self.process
        return (
            not self._closed
            and self._fatal is None
            and process is not None
            and process.poll() is None
        )

    def _set_fatal(self, message):
        with self._response_lock:
            if self._fatal is None:
                self._fatal = message
            self._fatal_event.set()
        try:
            self._events.put_nowait(None)
        except queue.Full:
            pass

    def _read_loop(self):
        try:
            while True:
                line = self.process.stdout.readline(LINE_LIMIT + 1)
                if not line:
                    self._set_fatal("closed")
                    return
                if len(line) > LINE_LIMIT or not line.endswith(b"\n"):
                    self._set_fatal("invalid")
                    return
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self._set_fatal("invalid")
                    return
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    self._set_fatal("invalid")
                    return
                if "id" in value and "method" in value:
                    self._reject(value["id"])
                    continue
                if value.get("method") == "notifications/tools/list_changed":
                    with self._tools_lock:
                        self._tool_changes += 1
                        self._tools_dirty = True
                    continue
                if value.get("method") is not None:
                    continue
                response_id = value.get("id")
                with self._response_lock:
                    if response_id not in self._issued_ids or response_id not in self._active_ids:
                        continue
                try:
                    self._events.put_nowait(value)
                except queue.Full:
                    self._set_fatal("busy")
                    return
        except Exception:
            self._set_fatal("closed")

    def _send(self, value, deadline=None):
        process = self.process
        if self._closed or self._fatal is not None or process is None or process.stdin is None:
            raise RuntimeError("MCP connection closed.")
        try:
            data = (_json_dumps(value) + "\n").encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ValueError("MCP message is not JSON-compatible.") from None
        if len(data) > LINE_LIMIT:
            raise RuntimeError("MCP request was too large.")
        deadline = deadline if deadline is not None else time.monotonic() + self.request_timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._send_lock.acquire(timeout=remaining):
            self._set_fatal("blocked")
            raise RuntimeError("MCP request timed out.")
        try:
            if os.name == "posix":
                self._write_posix(process.stdin.fileno(), data, deadline)
            else:
                self._write_fallback(process.stdin, data, deadline)
        except (BrokenPipeError, OSError):
            self._set_fatal("closed")
            raise RuntimeError("MCP connection closed.") from None
        finally:
            self._send_lock.release()

    def _write_posix(self, descriptor, data, deadline):
        pending = memoryview(data)
        selector = selectors.DefaultSelector()
        try:
            selector.register(descriptor, selectors.EVENT_WRITE)
            while pending:
                if self._closed or self._fatal is not None:
                    raise RuntimeError("MCP connection closed.")
                if time.monotonic() >= deadline:
                    self._set_fatal("blocked")
                    raise RuntimeError("MCP request timed out.")
                try:
                    written = os.write(descriptor, pending)
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        self._set_fatal("blocked")
                        raise RuntimeError("MCP request timed out.")
                    continue
                if written <= 0:
                    self._set_fatal("closed")
                    raise RuntimeError("MCP connection closed.")
                pending = pending[written:]
        finally:
            selector.close()

    def _write_fallback(self, stream, data, deadline):
        result = queue.Queue(maxsize=1)

        def write():
            try:
                offset = 0
                while offset < len(data):
                    written = stream.write(data[offset:])
                    if not written:
                        raise BrokenPipeError
                    offset += written
                result.put_nowait(None)
            except BaseException as error:
                result.put_nowait(error)

        writer = threading.Thread(target=write, name=f"mcp-{self.server_name}-writer", daemon=True)
        writer.start()
        remaining = deadline - time.monotonic()
        writer.join(max(0, remaining))
        if writer.is_alive():
            self._set_fatal("blocked")
            process = self.process
            if process and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            raise RuntimeError("MCP request timed out.")
        error = result.get_nowait()
        if error is not None:
            raise error

    def _receive(self, timeout):
        if timeout <= 0:
            raise RuntimeError("MCP request timed out.")
        deadline = time.monotonic() + timeout
        while True:
            if self._fatal_event.is_set():
                raise RuntimeError("MCP connection closed.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("MCP request timed out.")
            try:
                value = self._events.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if self._fatal_event.is_set() or value is None:
                raise RuntimeError("MCP connection closed.")
            return value

    def _reject(self, request_id):
        try:
            self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "This operation is not available in AIOS."},
            }, time.monotonic() + self.request_timeout)
        except RuntimeError:
            self._set_fatal("blocked")

    def _request(self, method, params=None):
        self.start()
        deadline = time.monotonic() + self.request_timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._request_lock.acquire(timeout=remaining):
            raise RuntimeError("MCP request timed out.")
        request_id = None
        try:
            self._sequence += 1
            request_id = self._sequence
            with self._response_lock:
                self._issued_ids.add(request_id)
                self._active_ids.add(request_id)
            self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
                deadline,
            )
            while True:
                value = self._receive(deadline - time.monotonic())
                if value.get("id") != request_id:
                    continue
                if "error" in value:
                    raise RuntimeError("MCP request failed.")
                return value.get("result")
        finally:
            if request_id is not None:
                with self._response_lock:
                    self._active_ids.discard(request_id)
                    self._issued_ids.discard(request_id)
                while True:
                    try:
                        self._events.get_nowait()
                    except queue.Empty:
                        break
            self._request_lock.release()

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "AIOS", "version": "0.1"},
        })
        if (not isinstance(result, dict) or result.get("protocolVersion") != PROTOCOL_VERSION
                or not isinstance(result.get("capabilities"), dict)
                or not isinstance(result["capabilities"].get("tools"), dict)):
            raise RuntimeError("MCP server rejected initialization.")
        self._send(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            time.monotonic() + self.request_timeout,
        )

    def list_tools(self):
        with self._tools_lock:
            if self._tools is not None and not self._tools_dirty:
                return list(self._tools)
            changes = self._tool_changes
        tools = []
        seen = set()
        cursor = None
        for _ in range(MAX_PAGES):
            params = {} if cursor is None else {"cursor": cursor}
            result = self._request("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise RuntimeError("MCP tools list was invalid.")
            for item in result["tools"]:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]
                        or not _valid_input_schema(item.get("inputSchema"))):
                    raise RuntimeError("MCP tools list was invalid.")
                if "description" in item and item["description"] is not None and not isinstance(item["description"], str):
                    raise RuntimeError("MCP tools list was invalid.")
                if "title" in item and item["title"] is not None and not isinstance(item["title"], str):
                    raise RuntimeError("MCP tools list was invalid.")
                if item["name"] in seen:
                    raise RuntimeError("MCP tools list was invalid.")
                seen.add(item["name"])
                tools.append(item)
                if len(tools) > MAX_TOOLS:
                    raise RuntimeError("MCP tools list was too large.")
            cursor = result.get("nextCursor")
            if cursor is None:
                with self._tools_lock:
                    self._tools = tools
                    self._tools_dirty = self._tool_changes != changes
                    return list(self._tools)
            if not isinstance(cursor, str) or not cursor:
                raise RuntimeError("MCP tools list was invalid.")
        raise RuntimeError("MCP tools list was too large.")

    def call_tool(self, tool_name, arguments):
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        if not isinstance(result, dict):
            raise RuntimeError("MCP tool returned an invalid result.")
        content = result.get("content", [])
        if not isinstance(content, list):
            raise RuntimeError("MCP tool returned an invalid result.")
        text = []
        for item in content:
            if not isinstance(item, dict):
                raise RuntimeError("MCP tool returned an invalid result.")
            kind = item.get("type")
            if kind == "text":
                if not isinstance(item.get("text"), str):
                    raise RuntimeError("MCP tool returned an invalid result.")
                text.append(item["text"])
                continue
            if kind in ("image", "audio", "resource", "resource_link"):
                raise RuntimeError("MCP tool returned unsupported content.")
            raise RuntimeError("MCP tool returned unsupported content.")
        structured = result.get("structuredContent")
        if structured is not None:
            try:
                _check_json_value(structured)
            except ValueError:
                raise RuntimeError("MCP tool returned an invalid result.") from None
        normalized = {
            "text": "\n".join(text),
            "structured": structured,
            "is_error": bool(result.get("isError", False)),
        }
        if len(_json_dumps(normalized).encode("utf-8")) > RESULT_LIMIT:
            raise RuntimeError("MCP tool result was too large.")
        return normalized


class McpRegistry:
    def __init__(
        self,
        config_path=None,
        request_timeout=10,
        failure_cooldown=DEFAULT_FAILURE_COOLDOWN,
        clock=time.monotonic,
    ):
        self.config_path = _config_path(config_path)
        self.request_timeout = request_timeout
        self.failure_cooldown = failure_cooldown
        self._clock = clock
        self._clients = {}
        self._failures = {}
        self._tool_map = {}

    def _drop_client(self, name):
        client = self._clients.pop(name, None)
        if client is not None:
            client.close()

    def _record_failure(self, name, settings):
        self._failures[name] = (settings, self._clock() + self.failure_cooldown)

    def _in_cooldown(self, name, settings):
        failure = self._failures.get(name)
        if failure is None:
            return False
        failed_settings, retry_at = failure
        if failed_settings != settings:
            self._failures.pop(name, None)
            return False
        if self._clock() >= retry_at:
            self._failures.pop(name, None)
            return False
        return True

    def _drop_tool_mappings(self, server_name):
        self._tool_map = {
            exposed: target
            for exposed, target in self._tool_map.items()
            if target[0] != server_name
        }

    def definitions(self):
        servers, warnings = _load_servers(self.config_path)
        active = {name for name, _ in servers}
        for name in list(self._clients):
            if name not in active:
                self._drop_client(name)
        for name in list(self._failures):
            if name not in active:
                self._failures.pop(name, None)
        definitions = []
        tool_map = {}
        for name, settings in servers:
            if not settings["tools"]:
                self._drop_client(name)
                self._failures.pop(name, None)
                continue
            client = self._clients.get(name)
            if client is not None and client.settings != settings:
                self._drop_client(name)
                self._failures.pop(name, None)
                client = None
            if client is not None and client._closed:
                self._drop_client(name)
                client = None
            elif client is not None and not client.is_usable():
                self._drop_client(name)
                self._record_failure(name, settings)
                warnings.append(_safe_server_message(name, "is unavailable"))
                continue
            if self._in_cooldown(name, settings):
                warnings.append(_safe_server_message(name, "is unavailable"))
                continue
            try:
                if client is None:
                    client = McpClient(name, settings, self.request_timeout)
                    self._clients[name] = client
                tools = client.list_tools()
            except RuntimeError:
                self._drop_client(name)
                self._record_failure(name, settings)
                warnings.append(_safe_server_message(name, "is unavailable"))
                continue
            self._failures.pop(name, None)
            selected = tools if "*" in settings["tools"] else [tool for tool in tools if tool["name"] in settings["tools"]]
            pending_defs = []
            pending_map = {}
            invalid = False
            for tool in selected:
                exposed = _exposed_name(name, tool["name"])
                if not exposed or exposed in pending_map:
                    invalid = True
                    break
                pending_defs.append({
                    "type": "function",
                    "function": {
                        "name": exposed,
                        "description": _tool_description(name, tool),
                        "parameters": tool["inputSchema"],
                    },
                })
                pending_map[exposed] = (name, tool["name"])
            if invalid or any(exposed in tool_map for exposed in pending_map):
                warnings.append(_safe_server_message(name, "has conflicting tool names and was ignored"))
                continue
            if len(definitions) + len(pending_defs) > MAX_TOOLS:
                warnings.append(_safe_server_message(name, "has too many tools and was ignored"))
                continue
            definitions.extend(pending_defs)
            tool_map.update(pending_map)
        self._tool_map = tool_map
        return definitions, warnings

    def call(self, exposed_name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool arguments must be an object.")
        try:
            _check_json_value(arguments)
        except ValueError:
            raise ValueError("MCP tool arguments must be JSON-compatible.") from None
        target = self._tool_map.get(exposed_name)
        was_known = target is not None
        if target is not None:
            server_name, tool_name = target
            servers, _ = _load_servers(self.config_path)
            configured = dict(servers).get(server_name)
            client = self._clients.get(server_name)
            if configured is not None and client is not None and configured == client.settings and (
                "*" in configured["tools"] or tool_name in configured["tools"]
            ) and client.is_usable():
                with client._tools_lock:
                    tools_dirty = client._tools_dirty
                if not tools_dirty:
                    return self._call_client(server_name, tool_name, client, arguments)
        self.definitions()
        target = self._tool_map.get(exposed_name)
        if target is None:
            if was_known:
                raise RuntimeError("MCP tool is unavailable.")
            raise ValueError("Unknown MCP tool.")
        server_name, tool_name = target
        client = self._clients.get(server_name)
        if client is None or not client.is_usable():
            raise RuntimeError("MCP tool is unavailable.")
        return self._call_client(server_name, tool_name, client, arguments)

    def _call_client(self, server_name, tool_name, client, arguments):
        try:
            return client.call_tool(tool_name, arguments)
        except RuntimeError:
            settings = client.settings
            if self._clients.get(server_name) is client:
                self._drop_client(server_name)
            self._record_failure(server_name, settings)
            self._drop_tool_mappings(server_name)
            raise RuntimeError("MCP tool is unavailable.") from None

    def close(self):
        for name in list(self._clients):
            self._drop_client(name)
