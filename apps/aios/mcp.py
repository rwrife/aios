"""Allowlisted stdio MCP tools."""
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
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
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("invalid float")
        return
    if isinstance(value, list):
        for item in value:
            _check_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("invalid key")
            _check_json_value(item)
        return
    raise ValueError("invalid value")


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


def _load_servers(config_path):
    path = _config_path(config_path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return [], []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return [], [_warning("configuration file was ignored because it is not a regular file")]
    if info.st_size > CONFIG_LIMIT:
        return [], [_warning("configuration file was ignored because it is too large")]
    try:
        data = path.read_bytes()
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
        self._reader = None
        self._events = queue.Queue(maxsize=QUEUE_LIMIT)
        self._send_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._responses = {}
        self._sequence = 0
        self._closed = False
        self._fatal = None
        self._tools = None
        self._tools_dirty = True
        self._tool_changes = 0

    def start(self):
        if self.process is not None:
            return
        options = {
            "args": [self.settings["command"], *self.settings["args"]],
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "env": _child_env(self.settings["env"]),
        }
        if os.name == "posix":
            options["start_new_session"] = True
        try:
            self.process = subprocess.Popen(**options)
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
        if self._closed:
            return
        self._closed = True
        process = self.process
        stdin = getattr(process, "stdin", None) if process else None
        stdout = getattr(process, "stdout", None) if process else None
        if stdin:
            try:
                stdin.close()
            except OSError:
                pass
        if process and process.poll() is None:
            time.sleep(0.05)
            if process.poll() is None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                else:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        if stdout:
            try:
                stdout.close()
            except OSError:
                pass
        if self._reader:
            self._reader.join(timeout=1)
        self.process = None

    def _set_fatal(self, message):
        if self._fatal is None:
            self._fatal = message
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
                    self._tool_changes += 1
                    self._tools_dirty = True
                    continue
                if value.get("method") is not None:
                    continue
                try:
                    self._events.put_nowait(value)
                except queue.Full:
                    self._set_fatal("busy")
                    return
        except OSError:
            self._set_fatal("closed")

    def _send(self, value):
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("MCP connection closed.")
        data = (_json_dumps(value) + "\n").encode("utf-8")
        try:
            with self._send_lock:
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._set_fatal("closed")
            raise RuntimeError("MCP connection closed.") from None

    def _receive(self, timeout):
        if timeout <= 0:
            raise RuntimeError("MCP request timed out.")
        try:
            value = self._events.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("MCP request timed out.") from None
        if value is None:
            raise RuntimeError("MCP connection closed.") from None
        return value

    def _reject(self, request_id):
        try:
            self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "This operation is not available in AIOS."},
            })
        except RuntimeError:
            pass

    def _request(self, method, params=None):
        self.start()
        with self._request_lock:
            self._sequence += 1
            request_id = self._sequence
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            deadline = time.monotonic() + self.request_timeout
            while True:
                if request_id in self._responses:
                    value = self._responses.pop(request_id)
                else:
                    value = self._receive(deadline - time.monotonic())
                response_id = value.get("id")
                if response_id != request_id:
                    if len(self._responses) >= QUEUE_LIMIT:
                        raise RuntimeError("MCP connection is busy.")
                    self._responses[response_id] = value
                    continue
                if "error" in value:
                    raise RuntimeError("MCP request failed.")
                return value.get("result")

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
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def list_tools(self):
        if self._tools is not None and not self._tools_dirty:
            return list(self._tools)
        tools = []
        seen = set()
        cursor = None
        changes = self._tool_changes
        for _ in range(MAX_PAGES):
            params = {} if cursor is None else {"cursor": cursor}
            result = self._request("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise RuntimeError("MCP tools list was invalid.")
            for item in result["tools"]:
                if (not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]
                        or not isinstance(item.get("inputSchema"), dict)):
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
    def __init__(self, config_path=None, request_timeout=10):
        self.config_path = _config_path(config_path)
        self.request_timeout = request_timeout
        self._clients = {}
        self._tool_map = {}

    def definitions(self):
        servers, warnings = _load_servers(self.config_path)
        active = {name for name, _ in servers}
        for name in list(self._clients):
            if name not in active:
                self._clients.pop(name).close()
        definitions = []
        tool_map = {}
        for name, settings in servers:
            if not settings["tools"]:
                if name in self._clients:
                    self._clients.pop(name).close()
                continue
            try:
                client = self._clients.get(name)
                if client is not None and client.settings != settings:
                    client.close()
                    client = None
                    self._clients.pop(name, None)
                if client is None:
                    client = McpClient(name, settings, self.request_timeout)
                    self._clients[name] = client
                tools = client.list_tools()
            except RuntimeError:
                if name in self._clients:
                    self._clients.pop(name).close()
                warnings.append(_safe_server_message(name, "is unavailable"))
                continue
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
            definitions.extend(pending_defs)
            tool_map.update(pending_map)
        self._tool_map = tool_map
        return definitions, warnings

    def call(self, exposed_name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("MCP tool arguments must be an object.")
        self.definitions()
        if exposed_name not in self._tool_map:
            raise ValueError("Unknown MCP tool.")
        server_name, tool_name = self._tool_map[exposed_name]
        client = self._clients.get(server_name)
        if client is None:
            raise RuntimeError("MCP tool is unavailable.")
        return client.call_tool(tool_name, arguments)

    def close(self):
        for name in list(self._clients):
            self._clients.pop(name).close()
