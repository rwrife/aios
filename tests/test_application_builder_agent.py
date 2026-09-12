import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))

from aios.applications import ApplicationStore
from aios.toolhost import ToolHost, close_service, list_tools, serve


USER_REQUEST = "create a calculator application"
APP_TITLE = "Calculator"
APP_SUMMARY = "A trusted native calculator for basic arithmetic."
APP_KEYWORDS = ["arithmetic", "calculator", "native"]
MODEL_NAME = "calculator-workflow-test"
CALCULATOR_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calculator</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;background:#eef2f7;color:#172033}
main{width:min(22rem,90vw);background:#fff;padding:1rem;border-radius:1rem;box-shadow:0 .5rem 2rem #0002}
#display{box-sizing:border-box;width:100%;min-height:4rem;padding:.75rem;text-align:right;font-size:2rem;border:2px solid #64748b;border-radius:.5rem;background:#f8fafc}
.keys{display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem;margin-top:.75rem}
button{min-height:3.5rem;border:0;border-radius:.5rem;font:inherit;font-size:1.15rem;background:#dbeafe;color:#172033;cursor:pointer}
button:focus-visible{outline:3px solid #2563eb;outline-offset:2px}
.operator{background:#bfdbfe}.clear{background:#fecaca}.equals{background:#2563eb;color:#fff}
</style>
</head>
<body>
<main aria-labelledby="title">
<h1 id="title">Calculator</h1>
<output id="display" aria-live="polite" aria-label="Calculator output">0</output>
<div class="keys" aria-label="Calculator buttons">
<button class="clear" data-action="clear">Clear</button>
<button data-action="sign">+/-</button>
<button data-action="decimal">.</button>
<button class="operator" data-op="/">&#247;</button>
<button data-digit="7">7</button><button data-digit="8">8</button><button data-digit="9">9</button><button class="operator" data-op="*">&#215;</button>
<button data-digit="4">4</button><button data-digit="5">5</button><button data-digit="6">6</button><button class="operator" data-op="-">-</button>
<button data-digit="1">1</button><button data-digit="2">2</button><button data-digit="3">3</button><button class="operator" data-op="+">+</button>
<button data-digit="0">0</button><button class="equals" data-action="equals">=</button>
</div>
</main>
<script>
const display=document.querySelector('#display');let value='0',stored=null,operation=null,replace=true;
const show=()=>{display.textContent=value};
const calculate=()=>{if(stored===null||operation===null)return;const a=stored,b=Number(value);let result=0;
if(operation==='+')result=a+b;else if(operation==='-')result=a-b;else if(operation==='*')result=a*b;else result=b===0?NaN:a/b;
value=Number.isFinite(result)?String(Number(result.toPrecision(12))):'Error';stored=null;operation=null;replace=true;show()};
document.querySelector('.keys').addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;
if(button.dataset.digit!==undefined){value=replace||value==='Error'?button.dataset.digit:(value==='0'?button.dataset.digit:value+button.dataset.digit);replace=false}
else if(button.dataset.action==='decimal'){if(replace||value==='Error'){value='0.';replace=false}else if(!value.includes('.'))value+='.'}
else if(button.dataset.action==='clear'){value='0';stored=null;operation=null;replace=true}
else if(button.dataset.action==='sign'&&value!=='Error')value=String(-Number(value));
else if(button.dataset.action==='equals')calculate();
else if(button.dataset.op){if(operation&&!replace)calculate();stored=Number(value);operation=button.dataset.op;replace=true}
show()});
</script>
</body>
</html>
"""


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _tool_call(call_id, arguments):
    return {
        "tool_calls": [{
            "index": 0,
            "id": call_id,
            "type": "function",
            "function": {
                "name": "application",
                "arguments": json.dumps(arguments, separators=(",", ":")),
            },
        }]
    }


class _NeverBrowser:
    def __init__(self):
        self.calls = []
        self.close_calls = 0

    def act(self, arguments):
        self.calls.append(arguments)
        raise AssertionError("The calculator workflow must not call the browser.")

    def close(self):
        self.close_calls += 1


class _EmptyMcpRegistry:
    def __init__(self):
        self.definition_calls = 0
        self.calls = []
        self.close_calls = 0

    def definitions(self):
        self.definition_calls += 1
        return [], []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        raise AssertionError("The calculator workflow must not call MCP.")

    def close(self):
        self.close_calls += 1


class _ModelState:
    def __init__(self):
        self.requests = []
        self.headers = []
        self.actions = {1: [], 2: []}
        self.completed_runs = 0
        self.application_id = None
        self.errors = []

    def _validate_request(self, body, headers):
        _require(body.get("model") == MODEL_NAME, "Unexpected model name.")
        _require(body.get("stream") is True, "Chat Completions streaming must be enabled.")
        _require(body.get("tool_choice") == "auto", "Tool choice must remain automatic.")
        _require(set(body) == {"model", "messages", "tools", "tool_choice", "stream"}, "Unexpected request fields.")
        tool_names = [tool["function"]["name"] for tool in body["tools"]]
        _require(tool_names == ["activate_skill", "application"], f"Unexpected tools: {tool_names!r}")
        system = body["messages"][0]
        _require(system.get("role") == "system", "The first message must be the system prompt.")
        prompt = system.get("content", "")
        _require('Activated skill: "application-builder"' in prompt, "Application builder was not active.")
        _require("Search first for an existing cached app" in prompt, "Shipped cache-first instructions were absent.")
        _require("Inspect the advertised `application` tool schema" in prompt, "Schema inspection instructions were absent.")
        _require("trusted native `calculator` template" in prompt, "Native calculator instructions were absent.")
        _require("self-contained `index.html`" in prompt, "Web fallback instructions were absent.")
        _require("finish by launching it in the same turn" in prompt, "Base launch policy was absent.")
        _require("Publication is optional and separate" in prompt, "Optional publication instructions were absent.")
        _require("then `launch`" in prompt and "then `publish`" not in prompt, "Direct native launch instructions were absent.")
        application_tools = [
            tool for tool in body["tools"]
            if tool.get("function", {}).get("name") == "application"
        ]
        _require(len(application_tools) == 1, "Expected one application tool definition.")
        parameters = application_tools[0]["function"]["parameters"]
        properties = parameters["properties"]
        _require(properties.get("runtime", {}).get("enum") == ["web", "native"], "Native runtime was not advertised.")
        _require(properties.get("template", {}).get("enum") == ["calculator"], "Calculator template was not advertised.")
        serialized = json.dumps(body).casefold()
        for forbidden in ("api_key", "agent_api_key", "authorization", "bearer "):
            _require(forbidden not in serialized, f"Request body contained {forbidden!r}.")
        _require("authorization" not in headers, "The empty-key loopback request must not have Authorization.")

        calls = {}
        for message in body["messages"]:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = call.get("id")
                    _require(call_id and call_id not in calls, "Assistant tool call IDs must be unique.")
                    calls[call_id] = call
            elif message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                _require(call_id in calls, f"Tool result {call_id!r} has no matching assistant call.")

    def _latest_result(self, body, expected_call_id):
        tool_messages = [message for message in body["messages"] if message.get("role") == "tool"]
        _require(tool_messages, "Expected a tool result.")
        message = tool_messages[-1]
        _require(message.get("tool_call_id") == expected_call_id, "Tool result used the wrong tool_call_id.")
        result = json.loads(message["content"])
        _require(isinstance(result, dict), "Tool result must be a JSON object.")
        return result

    def response(self, body, headers):
        self._validate_request(body, headers)
        self.requests.append(json.loads(json.dumps(body)))
        self.headers.append(dict(headers))
        tool_count = sum(message.get("role") == "tool" for message in body["messages"])
        run = self.completed_runs + 1
        _require(run in (1, 2), "The fake model received an unexpected extra conversation.")

        if run == 1:
            if tool_count == 0:
                arguments = {"action": "search", "query": USER_REQUEST}
                self.actions[run].append(arguments)
                return _tool_call("run1-search", arguments), "tool_calls"
            if tool_count == 1:
                result = self._latest_result(body, "run1-search")
                matches = result.get("matches")
                _require(isinstance(matches, list) and len(matches) == 1, f"Expected one legacy web match, got {result!r}.")
                _require(matches[0].get("exact") is True, "Legacy web match was not exact.")
                _require(matches[0].get("runtime") == "web", "Expected the existing match to use the web runtime.")
                _require(matches[0].get("template") is None, "Legacy web match unexpectedly had a template.")
                arguments = {
                    "action": "create",
                    "title": APP_TITLE,
                    "request": USER_REQUEST,
                    "runtime": "native",
                    "template": "calculator",
                }
                self.actions[run].append(arguments)
                return _tool_call("run1-create", arguments), "tool_calls"
            if tool_count == 2:
                result = self._latest_result(body, "run1-create")
                app_id = result.get("id")
                _require(isinstance(app_id, str) and app_id.startswith("calculator-"), "Create did not return a calculator ID.")
                _require(result.get("runtime") == "native", "Create did not return the native runtime.")
                _require(result.get("template") == "calculator", "Create did not return the calculator template.")
                self.application_id = app_id
                arguments = {"action": "launch", "id": self.application_id}
                self.actions[run].append(arguments)
                return _tool_call("run1-launch", arguments), "tool_calls"
            if tool_count == 3:
                result = self._latest_result(body, "run1-launch")
                _require(result.get("launched") is True, f"Launch failed: {result!r}")
                _require(result.get("id") == self.application_id, "Launch returned the wrong application ID.")
                _require(result.get("runtime") == "native", "Launch did not report the native runtime.")
                _require(result.get("template") == "calculator", "Launch did not report the calculator template.")
                self.completed_runs = 1
                return {"content": "Native calculator launched."}, "stop"
        else:
            if tool_count == 0:
                arguments = {"action": "search", "query": USER_REQUEST}
                self.actions[run].append(arguments)
                return _tool_call("run2-search", arguments), "tool_calls"
            if tool_count == 1:
                result = self._latest_result(body, "run2-search")
                matches = result.get("matches")
                _require(isinstance(matches, list) and len(matches) == 2, f"Expected native and web matches, got {result!r}.")
                match = matches[0]
                _require(match.get("id") == self.application_id, "Search returned the wrong cached application.")
                _require(match.get("exact") is True, "Cached search match was not exact.")
                _require(match.get("runtime") == "native", "Native cached match did not outrank legacy web.")
                _require(match.get("template") == "calculator", "Cached native match had the wrong template.")
                _require(matches[1].get("runtime") == "web", "Expected the legacy web match second.")
                arguments = {"action": "launch", "id": match["id"]}
                self.actions[run].append(arguments)
                return _tool_call("run2-launch", arguments), "tool_calls"
            if tool_count == 2:
                result = self._latest_result(body, "run2-launch")
                _require(result.get("launched") is True, f"Cached launch failed: {result!r}")
                _require(result.get("id") == self.application_id, "Cached launch returned the wrong ID.")
                self.completed_runs = 2
                return {"content": "Reused and launched the native calculator."}, "stop"
        raise AssertionError(f"Unexpected run {run} tool count {tool_count}.")


class _ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        state = self.server.state
        try:
            _require(self.path == "/v1/chat/completions", f"Unexpected model path {self.path!r}.")
            length = int(self.headers["Content-Length"])
            _require(0 < length <= 1024 * 1024, "Invalid model request size.")
            body = json.loads(self.rfile.read(length))
            headers = {name.casefold(): value for name, value in self.headers.items()}
            delta, finish_reason = state.response(body, headers)
            event = {
                "id": "chatcmpl-calculator-test",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            payload = (
                "data: " + json.dumps(event, separators=(",", ":")) + "\n\n"
                "data: [DONE]\n\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except BaseException:
            state.errors.append(traceback.format_exc())
            payload = b"model fixture failed"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "AF_UNIX required")
class ApplicationBuilderAgentTests(unittest.TestCase):
    def _run_worker(self, env, tool_socket):
        request = {
            "action": "chat",
            "messages": [{"role": "user", "content": USER_REQUEST}],
            "tool_socket": os.fspath(tool_socket),
        }
        return subprocess.run(
            [sys.executable, "-m", "aios.worker"],
            input=json.dumps(request) + "\n",
            text=True,
            capture_output=True,
            cwd=ROOT,
            env=env,
            timeout=30,
            check=False,
        )

    def _events(self, result):
        self.assertEqual(result.returncode, 0, f"worker stderr:\n{result.stderr}\nworker stdout:\n{result.stdout}")
        self.assertEqual(result.stderr, "")
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertNotIn("error", [event.get("type") for event in events])
        self.assertEqual(events[-1], {"type": "done"})
        self.assertTrue(any(event.get("type") == "progress" for event in events))
        self.assertTrue(any(event.get("type") == "token" for event in events))
        return events

    def test_real_worker_launches_draft_then_reuses_explicitly_published_calculator(self):
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        temp_path = None
        runtime_path = None
        with tempfile.TemporaryDirectory(prefix="application-builder-e2e-", dir=scratch) as temp, \
                tempfile.TemporaryDirectory(prefix=".aios-e2e-", dir=Path.home()) as runtime:
            temp_path = Path(temp)
            runtime_path = Path(runtime)
            config_root = temp_path / "config"
            data_root = temp_path / "data"
            skill_target = config_root / "aios" / "skills" / "application-builder"
            skill_target.mkdir(parents=True)
            shipped_skill = ROOT / "apps" / "skills" / "application-builder" / "SKILL.md"
            shutil.copy2(shipped_skill, skill_target / "SKILL.md")

            state = _ModelState()
            server = HTTPServer(("127.0.0.1", 0), _ModelHandler)
            server.state = state
            server_thread = threading.Thread(target=server.serve_forever, name="calculator-model", daemon=True)
            server_thread.start()
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1"
            config_file = config_root / "aios" / "config.json"
            config_file.write_text(json.dumps({
                "mode": "remote",
                "url": endpoint,
                "model": MODEL_NAME,
                "api_key": "",
                "agent_mode": "current",
            }), encoding="utf-8")
            config_file.chmod(0o600)

            launched = []
            browser = _NeverBrowser()
            mcp = _EmptyMcpRegistry()
            application_root = data_root / "aios" / "applications"
            native_host = temp_path / "aios-app-host"
            native_host.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            native_host.chmod(0o700)
            self.assertTrue(os.access(native_host, os.X_OK))
            store = ApplicationStore(
                root=application_root,
                launcher=lambda folder: launched.append(Path(folder)),
                native_host=native_host,
            )
            self.assertEqual(store.native_templates, ("calculator",))
            legacy = store.create({"title": APP_TITLE, "request": USER_REQUEST})
            store.write({"id": legacy["id"], "html": CALCULATOR_HTML})
            store.publish({
                "id": legacy["id"],
                "summary": "Legacy offline web calculator.",
                "keywords": ["calculator", "legacy", "offline"],
            })
            legacy_folder = application_root / legacy["id"]
            host = ToolHost(browser=browser, applications=store, mcp=mcp)
            # WSL cannot bind AF_UNIX sockets on a mounted Windows filesystem.
            tool_socket = runtime_path / "tools.sock"
            tool_thread = threading.Thread(
                target=serve,
                args=(tool_socket, host),
                name="calculator-toolhost",
                daemon=True,
            )
            tool_thread.start()

            env = os.environ.copy()
            env.update({
                "XDG_CONFIG_HOME": os.fspath(config_root),
                "XDG_DATA_HOME": os.fspath(data_root),
                "PYTHONPATH": os.pathsep.join(filter(None, [
                    os.fspath(ROOT / "apps"),
                    env.get("PYTHONPATH", ""),
                ])),
            })

            cleanup_error = None
            try:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        listed = list_tools(tool_socket, timeout=0.2, startup_timeout=0.2)
                        break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            self.fail("ToolHost socket did not become ready.")
                        time.sleep(0.02)
                self.assertEqual(
                    [tool["function"]["name"] for tool in listed["tools"]],
                    ["browser", "application", "os_settings"],
                )
                self.assertEqual(listed["warnings"], [])

                first_worker = self._run_worker(env, tool_socket)
                self.assertEqual(state.errors, [], "\n".join(state.errors))
                first_events = self._events(first_worker)
                self.assertEqual(
                    [event["text"] for event in first_events if event["type"] == "progress"],
                    [
                        "Application: search",
                        "Application: create",
                        "Application: launch",
                    ],
                )
                self.assertEqual(
                    "".join(event["text"] for event in first_events if event["type"] == "token"),
                    "Native calculator launched.",
                )
                self.assertEqual(
                    [action["action"] for action in state.actions[1]],
                    ["search", "create", "launch"],
                )

                self.assertIsNotNone(state.application_id)
                app_folder = application_root / state.application_id
                self.assertNotEqual(app_folder, legacy_folder)
                self.assertEqual({path.name for path in app_folder.iterdir()}, {".draft.json"})
                self.assertEqual(launched[0].name, state.application_id)
                self.assertNotEqual(launched[0], app_folder)
                self.assertFalse(launched[0].exists())
                self.assertEqual(len(store.search({"query": USER_REQUEST})), 1)
                # Publication is a separate explicit action, not part of the app request.
                store.publish({"id": state.application_id, "summary": APP_SUMMARY, "keywords": APP_KEYWORDS})
                self.assertEqual({path.name for path in app_folder.iterdir()}, {"manifest.json"})
                manifest_path = app_folder / "manifest.json"
                manifest_bytes = manifest_path.read_bytes()
                manifest = json.loads(manifest_bytes)
                self.assertEqual(manifest["title"], APP_TITLE)
                self.assertEqual(manifest["request"], USER_REQUEST)
                self.assertEqual(manifest["summary"], APP_SUMMARY)
                self.assertEqual(manifest["keywords"], APP_KEYWORDS)
                self.assertEqual(manifest["runtime"], "native")
                self.assertEqual(manifest["template"], "calculator")
                self.assertNotIn("entrypoint", manifest)
                self.assertNotIn("sha256", manifest)
                self.assertFalse((app_folder / "index.html").exists())
                self.assertFalse((app_folder / ".draft.json").exists())
                self.assertFalse(any(path.name.endswith((".tmp", ".temp")) for path in app_folder.iterdir()))
                self.assertEqual(len(launched), 1)
                before = (manifest_bytes, manifest_path.stat().st_mtime_ns)

                second_events = self._events(self._run_worker(env, tool_socket))
                self.assertEqual(
                    [event["text"] for event in second_events if event["type"] == "progress"],
                    ["Application: search", "Application: launch"],
                )
                final_text = "".join(event["text"] for event in second_events if event["type"] == "token")
                self.assertEqual(final_text, "Reused and launched the native calculator.")
                self.assertIn("native", final_text.casefold())
                self.assertEqual(
                    [action["action"] for action in state.actions[2]],
                    ["search", "launch"],
                )
                second_transcript_actions = []
                for body in state.requests[-3:]:
                    for message in body["messages"]:
                        for call in message.get("tool_calls") or []:
                            if call["function"]["name"] == "application":
                                second_transcript_actions.append(
                                    json.loads(call["function"]["arguments"])["action"]
                                )
                self.assertEqual(sorted(set(second_transcript_actions)), ["launch", "search"])
                self.assertFalse(
                    {"create", "publish", "write", "read"}
                    & set(second_transcript_actions)
                )
                self.assertEqual(
                    (manifest_path.read_bytes(), manifest_path.stat().st_mtime_ns),
                    before,
                )
                self.assertEqual(len(launched), 2)
                self.assertEqual(launched[1], app_folder)
                self.assertEqual(state.completed_runs, 2)
                self.assertEqual(len(state.requests), 7)
                self.assertEqual(state.errors, [], "\n".join(state.errors))
                self.assertEqual(browser.calls, [])
                self.assertEqual(mcp.calls, [])
            finally:
                if tool_thread.is_alive() and tool_socket.exists():
                    try:
                        close_service(tool_socket, timeout=2, startup_timeout=0.5)
                    except BaseException as error:
                        cleanup_error = error
                tool_thread.join(5)
                server.shutdown()
                server.server_close()
                server_thread.join(5)

            self.assertIsNone(cleanup_error)
            self.assertFalse(tool_thread.is_alive())
            self.assertFalse(tool_socket.exists())
            self.assertFalse(server_thread.is_alive())
            self.assertEqual(browser.close_calls, 1)
            self.assertEqual(mcp.close_calls, 1)

        self.assertIsNotNone(temp_path)
        self.assertFalse(temp_path.exists())
        self.assertIsNotNone(runtime_path)
        self.assertFalse(runtime_path.exists())


if __name__ == "__main__":
    unittest.main()
