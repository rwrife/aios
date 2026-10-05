"""Allowlisted OS programs shared by the internal tool host and local MCP."""
import json
import os
import subprocess
from .principals import current as current_principal

COMMANDS = (
    "basename", "cat", "cp", "date", "df", "dirname", "du", "echo", "grep",
    "head", "hostname", "id", "ln", "ls", "mkdir", "mv", "printf", "pwd",
    "readlink", "realpath", "rm", "rmdir", "stat", "tail", "tee", "touch",
    "uname", "wc", "whoami",
)
BIN_DIRS = ("/usr/bin", "/bin")
INHERITED_ENV = (
    "HOME", "USER", "LOGNAME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    "XDG_CACHE_HOME", "LANG", "LC_ALL",
)
MAX_ARGS = 32
MAX_ARG_BYTES = 4096
MAX_STDIN_BYTES = 16 * 1024
OUTPUT_LIMIT = 32 * 1024
TIMEOUT_SECONDS = 10
GUEST_COMMANDS = ("echo", "printf", "pwd", "whoami", "id", "uname", "hostname")

TOOL = {
    "type": "function",
    "function": {
        "name": "os_command",
        "description": (
            "Run an allowlisted OS program with argv only: cat, ls, echo, pwd, "
            "mkdir, rmdir, rm, cp, mv, ln, touch, head, tail, tee, printf, wc, "
            "stat, realpath, readlink, dirname, basename, grep, date, "
            "whoami, id, uname, hostname, df, du. No shell, pipes, redirects, or "
            "unlisted binaries. Read a file with cat and the path in args. Write "
            "a file with tee, the path in args, and the contents in stdin; echo "
            "and printf print to stdout only. Optional cwd and stdin; default cwd "
            "is HOME. Returns exit_code, stdout, stderr; output truncates at "
            "32KiB per stream. Times out after 10s."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": list(COMMANDS)},
                "args": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": MAX_ARG_BYTES},
                    "maxItems": MAX_ARGS,
                },
                "cwd": {"type": "string", "maxLength": MAX_ARG_BYTES},
                "stdin": {"type": "string", "maxLength": MAX_STDIN_BYTES},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}


def definition():
    """Advertise only actions executable in the current broker context."""
    principal = current_principal()
    if principal is None or principal.owner is not None:
        return TOOL
    return {
        **TOOL,
        "function": {
            **TOOL["function"],
            "description": "Guest: echo/printf may print text; pwd/whoami/id/uname/hostname accept no args. File operations require an authenticated owner. No shell, cwd override or stdin.",
            "parameters": {
                **TOOL["function"]["parameters"],
                "properties": {
                    "command": {"type": "string", "enum": list(GUEST_COMMANDS)},
                    "args": TOOL["function"]["parameters"]["properties"]["args"],
                },
            },
        },
    }

def _bounded_text(value, limit, label):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("Invalid OS command request.")
    encoded = value.encode("utf-8")
    if len(encoded) > limit:
        raise ValueError(label)
    return value


def _clip(text):
    encoded = text.encode("utf-8")
    if len(encoded) <= OUTPUT_LIMIT:
        return text, False
    return encoded[:OUTPUT_LIMIT].decode("utf-8", "ignore"), True


def _resolve(command):
    if command not in COMMANDS:
        raise ValueError("Unknown command.")
    for directory in BIN_DIRS:
        candidate = os.path.join(directory, command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(command + " is unavailable.")


def _working_directory(cwd):
    if cwd is None:
        cwd = os.environ.get("HOME") or os.path.expanduser("~") or os.getcwd()
    else:
        cwd = _bounded_text(cwd, MAX_ARG_BYTES, "Working directory is too large.")
    if not os.path.isdir(cwd):
        raise RuntimeError("Working directory is unavailable.")
    return cwd


def _environment(cwd, guest=False):
    env = {"PATH": "/usr/bin:/bin", "PWD": cwd, "LC_ALL": "C"}
    if guest:
        # Do not pass inherited personal-home or XDG pointers to guest tools.
        env["HOME"] = cwd
        return env
    for key in INHERITED_ENV:
        value = os.environ.get(key)
        if value and "\0" not in value:
            env[key] = value
    return env


def act(arguments):
    if not isinstance(arguments, dict) or not arguments or set(arguments) - {"command", "args", "cwd", "stdin"}:
        raise ValueError("Invalid OS command request.")
    command = arguments.get("command")
    if command not in COMMANDS:
        raise ValueError("Invalid OS command request.")
    principal = current_principal()
    guest = principal is not None and principal.owner is None
    if guest:
        if command not in GUEST_COMMANDS or arguments.get("cwd") is not None or arguments.get("stdin") is not None:
            raise ValueError("File operations are unavailable in a guest session. Sign in to access files.")
    args = arguments.get("args", [])
    if not isinstance(args, list) or len(args) > MAX_ARGS:
        raise ValueError("Invalid OS command request.")
    argv_tail = []
    for item in args:
        argv_tail.append(_bounded_text(item, MAX_ARG_BYTES, "Argument is too large."))
    if guest and command not in ("echo", "printf") and argv_tail:
        # In particular hostname -F reads files and hostname NAME mutates the
        # kernel hostname; never mistake an inspection binary for a safe argv.
        raise ValueError("Guest inspection commands accept no arguments.")
    stdin = arguments.get("stdin")
    if stdin is not None:
        stdin = _bounded_text(stdin, MAX_STDIN_BYTES, "Standard input is too large.")
    if guest:
        assert principal is not None
        cwd = _working_directory(str(principal.workspace))
    else:
        cwd = _working_directory(arguments.get("cwd"))
    executable = _resolve(command)
    argv = [executable, *argv_tail]
    kwargs = {
        "args": argv,
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": TIMEOUT_SECONDS,
        "cwd": cwd,
        "env": _environment(cwd, guest=guest),
        "check": False,
    }
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    try:
        completed = subprocess.run(**kwargs)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Command timed out after 10 seconds.") from None
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(command + " failed or is unavailable.") from None
    stdout, stdout_cut = _clip(completed.stdout or "")
    stderr, stderr_cut = _clip(completed.stderr or "")
    return {
        "command": command,
        "path": executable,
        "argv": argv,
        "cwd": cwd,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": stdout_cut or stderr_cut,
    }


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
            response["result"] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aios-os-command", "version": "1.0"},
                "instructions": (
                    "Run only allowlisted programs as argv. No shell, pipes, or "
                    "redirects. Inspect exit_code, stdout, and stderr. Never request "
                    "unlisted binaries."
                ),
            }
        elif method == "tools/list":
            function = definition()["function"]
            response["result"] = {"tools": [{"name": function["name"], "description": function["description"], "inputSchema": function["parameters"]}]}
        elif method == "tools/call":
            try:
                params = request.get("params", {})
                if not isinstance(params, dict) or params.get("name") != "os_command":
                    raise ValueError("Unknown tool.")
                payload = params.get("arguments", {})
                if not isinstance(payload, dict):
                    raise ValueError("Invalid arguments.")
                result = act(payload)
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
