"""Fixture MCP server for stdio integration tests."""
import json
import os
import sys


SCENARIO = os.environ.get("AIOS_MCP_SCENARIO", "happy")
LOG_PATH = os.environ.get("AIOS_MCP_LOG")
ENV_PATH = os.environ.get("AIOS_MCP_ENV_LOG")
SECRET = os.environ.get("AIOS_MCP_SECRET", "secret-provider-token")

LIST_GENERATION = 0
LIST_CHANGED_SENT = False
SERVER_REQUEST_SENT = False


def write_env_snapshot():
    if ENV_PATH:
        with open(ENV_PATH, "w", encoding="utf-8") as stream:
            json.dump(dict(sorted(os.environ.items())), stream, ensure_ascii=False)


def log_value(value):
    if not LOG_PATH:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def send(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def send_raw(raw):
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def respond(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def fail(request_id, message, code=-32000):
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def notify(method, params=None):
    send({"jsonrpc": "2.0", "method": method, "params": params or {}})


def server_request():
    send({"jsonrpc": "2.0", "id": "srv-1", "method": "sampling/createMessage", "params": {"secret": SECRET}})


def tool(name, description=None, title=None):
    value = {
        "name": name,
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
    }
    if description is not None:
        value["description"] = description
    if title is not None:
        value["title"] = title
    return value


def tools_page(cursor):
    global LIST_GENERATION
    if SCENARIO == "paginate-list-changed":
        if LIST_GENERATION == 0:
            if not cursor:
                return {
                    "tools": [tool("echo", "Echo back text")],
                    "nextCursor": "page-2",
                }
            LIST_GENERATION = 1
            return {
                "tools": [tool("sum", title="Add numbers")],
            }
        return {
            "tools": [tool("updated", "Updated tool after refresh")],
        }
    if SCENARIO == "multi-tools":
        return {"tools": [tool("echo", "Echo"), tool("spare", "Spare tool")]}
    if SCENARIO == "duplicate-tools":
        return {"tools": [tool("echo", "First"), tool("echo", "Second")]}
    if SCENARIO == "invalid-tool":
        return {"tools": [tool("", "Blank")]}
    return {"tools": [tool("echo", "Echo back text"), tool("hidden", "Hidden tool")]}


def call_result(arguments):
    if SCENARIO == "unsupported-content":
        return {"content": [{"type": "image", "data": "abcd", "mimeType": "image/png"}]}
    if SCENARIO == "oversized-result":
        return {"content": [{"type": "text", "text": "x" * (70 * 1024)}]}
    if SCENARIO == "call-nondict":
        return []
    value = arguments.get("value", "")
    return {
        "content": [{"type": "text", "text": "echo:" + value}],
        "structuredContent": {"value": value},
        "isError": False,
    }


def handle_initialize(value):
    global SERVER_REQUEST_SENT
    request_id = value["id"]
    if SCENARIO == "init-timeout":
        return
    if SCENARIO == "malformed-line":
        send_raw(b"not-json\n")
        return
    if SCENARIO == "incomplete-line":
        send_raw(b'{"jsonrpc":"2.0"')
        return
    if SCENARIO == "oversized-line":
        payload = b'{"jsonrpc":"2.0","value":"' + (b"x" * (4 * 1024 * 1024 + 32)) + b'"}\n'
        send_raw(payload)
        return
    if SCENARIO == "wrong-version":
        respond(request_id, {"protocolVersion": "2024-01-01", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture"}})
        return
    if SCENARIO == "missing-capability":
        respond(request_id, {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fixture"}})
        return
    respond(request_id, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture"}})
    if SCENARIO == "server-request" and not SERVER_REQUEST_SENT:
        SERVER_REQUEST_SENT = True
        server_request()


def handle_tools_list(value):
    global LIST_CHANGED_SENT
    if SCENARIO == "rpc-error":
        fail(value["id"], SECRET)
        return
    page = tools_page(value.get("params", {}).get("cursor"))
    respond(value["id"], page)
    if SCENARIO == "paginate-list-changed" and not LIST_CHANGED_SENT and page.get("nextCursor") is None:
        LIST_CHANGED_SENT = True
        notify("notifications/tools/list_changed", {})


def handle_tools_call(value):
    if SCENARIO == "call-error":
        fail(value["id"], SECRET)
        return
    respond(value["id"], call_result(value.get("params", {}).get("arguments", {})))


write_env_snapshot()

for raw in sys.stdin.buffer:
    message = json.loads(raw.decode("utf-8"))
    log_value(message)
    method = message.get("method")
    if method == "initialize":
        handle_initialize(message)
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        handle_tools_list(message)
    elif method == "tools/call":
        handle_tools_call(message)
