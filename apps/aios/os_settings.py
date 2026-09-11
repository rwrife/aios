"""Structured desktop controls shared by the internal tool host and local MCP."""
import json
import os
import re
import subprocess

from . import core, machine_clock, session_client


TOOL = {
    "type": "function",
    "function": {
        "name": "os_settings",
        "description": "Read/change OS volume, mute, theme, reduced motion and guest machine date_time; open settings or request native sign-in. Read date_time with action read and setting date_time; set requires an ISO date-time with explicit offset (2000-2099). Manual clock changes require stopped time-sync daemons; results report hardware-clock persistence. Read first; authenticate opens trusted UI, never accepts credentials. Check authentication_status afterwards.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "set", "open", "authenticate", "authentication_status"]},
                "setting": {"type": "string", "enum": ["volume", "muted", "theme_color", "reduced_motion", "date_time"]},
                "value": {"oneOf": [{"type": "integer", "minimum": 0, "maximum": 100}, {"type": "boolean"}, {"type": "string", "enum": list(core.THEME_COLORS)}, {"type": "string", "maxLength": 25, "pattern": "^" + machine_clock.DATETIME_PATTERN + "$"}]},
                "section": {"type": "string", "enum": ["sound", "display", "network", "date_time"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def _desktop(action, **values):
    path = os.environ.get("AIOS_DESKTOP_CONTROL_SOCKET")
    if not path:
        raise RuntimeError("Desktop controls are unavailable in this session.")
    reply = session_client.request(path, {"action": action, **values})
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply["result"]


def _audio(*args):
    try:
        result = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5, check=True,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Audio service unavailable; open Sound settings.") from None
    return result.stdout


def _read():
    config = core.load_config()
    result = {key: config[key] for key in ("theme_color", "reduced_motion")}
    result["themes"] = list(core.THEME_COLORS)
    try:
        volumes = re.findall(r"(\d+)%", _audio("get-sink-volume", "@DEFAULT_SINK@"))
        if not volumes:
            raise RuntimeError("Audio volume unavailable.")
        result.update(volume=max(map(int, volumes)), muted=_audio("get-sink-mute", "@DEFAULT_SINK@").strip().endswith("yes"), audio_available=True)
    except RuntimeError:
        result["audio_available"] = False
    return result


def act(arguments):
    if arguments == {"action": "read", "setting": "date_time"}:
        return machine_clock.request_clock({"action": "read"})
    action = arguments.get("action")
    fields = {"read": {"action"}, "set": {"action", "setting", "value"}, "open": {"action", "section"}, "authenticate": {"action"}, "authentication_status": {"action"}}
    if action not in fields or set(arguments) != fields[action]:
        raise ValueError("Invalid OS settings request.")
    if action == "read":
        return _read()
    if action in ("authenticate", "authentication_status"):
        return _desktop(action)
    if action == "open":
        section = arguments["section"]
        if section not in ("sound", "display", "network", "date_time"):
            raise ValueError("Unknown settings section.")
        return _desktop("open", section=section)
    setting, value = arguments["setting"], arguments["value"]
    if setting == "date_time":
        return machine_clock.request_clock({"action": "set", "value": value})
    elif setting == "volume" and type(value) is int and 0 <= value <= 100:
        _audio("set-sink-volume", "@DEFAULT_SINK@", str(value) + "%")
    elif setting == "muted" and type(value) is bool:
        _audio("set-sink-mute", "@DEFAULT_SINK@", "1" if value else "0")
    elif ((setting == "theme_color" and isinstance(value, str) and value in core.THEME_COLORS)
          or (setting == "reduced_motion" and type(value) is bool)):
        # Require a live desktop before persisting a desktop change.
        _desktop("ping")
        core.save_config({setting: value})
        _desktop("appearance", setting=setting, value=value)
    else:
        raise ValueError("Unsupported setting or invalid value.")
    return {"updated": setting, "state": _read()}


def main():
    """Optional local stdio MCP adapter; no shell or generic broker passthrough."""
    import sys
    for line in iter(lambda: sys.stdin.buffer.readline(65537), b""):
        if len(line) > 65536:
            return
        try:
            request = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        if not isinstance(request, dict):
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}), flush=True)
            continue
        if "id" not in request:
            continue
        method = request.get("method")
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            response["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "aios-os", "version": "1.0"}, "instructions": "Read settings before changing them. Authenticate opens native UI; check authentication_status. Never request credentials in chat. Use only advertised actions."}
        elif method == "tools/list":
            function = TOOL["function"]
            response["result"] = {"tools": [{"name": function["name"], "description": function["description"], "inputSchema": function["parameters"]}]}
        elif method == "tools/call":
            try:
                params = request.get("params", {})
                if not isinstance(params, dict) or params.get("name") != "os_settings":
                    raise ValueError("Unknown tool.")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("Invalid arguments.")
                result = act(arguments)
                response["result"] = {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}
            except (ValueError, RuntimeError) as exc:
                response["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except (OSError, TypeError, KeyError):
                response["result"] = {"content": [{"type": "text", "text": "OS operation failed or is unavailable."}], "isError": True}
        elif method == "ping":
            response["result"] = {}
        else:
            response["error"] = {"code": -32601, "message": "Method not found"}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
