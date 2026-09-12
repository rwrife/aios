#!/usr/bin/env python3
"""Install AIOS in QEMU, then validate the installed system across two boots.

The only destructive host operation is replacing the qcow2 path supplied on
the command line. QEMU is deliberately started without networking.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time


MAX_SERIAL_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_BYTES = 128 * 1024
UPLOAD_CHUNK_BYTES = 768

FIRST_ACCEPTANCE = r'''#!/usr/bin/env python3
import base64
import http.server
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

from aios import core, scheduled_jobs
from aios.browser import Browser, discover


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def call(action, **values):
    reply = scheduled_jobs.request({"action": action, **values})
    require(reply.get("status") == "ok", f"{action} failed: {reply}")
    return reply["result"]


def command_lines():
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
            owner = entry.stat().st_uid
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        values = [part.decode(errors="replace") for part in raw.split(b"\0") if part]
        if values:
            found.append({"pid": int(entry.name), "uid": owner, "argv": values})
    return found


class BrowserHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.startswith("/set/"):
            identity = self.path.rsplit("/", 1)[-1]
            body = (
                "<title>Browser " + identity + "</title><h1>Browser " + identity
                + "</h1><label>Name <input></label><button onclick=\"document.querySelector("
                  "'#result').innerText='Hello '+document.querySelector('input').value\">"
                  "Greet</button><p id=\"result\"></p><a href=\"/echo\">Echo</a>"
                  "<div style=\"height:1800px\"></div><button>Bottom control</button>"
                  "<p>Bottom marker</p>"
            ).encode()
            self.send_response(200)
            self.send_header("Set-Cookie", f"browser_owner={identity}; Path=/")
        elif self.path == "/echo":
            cookie = self.headers.get("Cookie", "")
            body = f"<title>Cookie echo</title><h1>{cookie}</h1>".encode()
            self.send_response(200)
        else:
            body = b"<title>Next page</title><h1>Navigation works</h1>"
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


def descendants(pid):
    processes = command_lines()
    parents = {}
    for entry in processes:
        try:
            stat_fields = Path(f"/proc/{entry['pid']}/stat").read_text().split(") ", 1)[1].split()
            parents[entry["pid"]] = int(stat_fields[1])
        except (FileNotFoundError, IndexError, ValueError):
            pass
    selected = {pid}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if parent in selected and child not in selected:
                selected.add(child)
                changed = True
    return [entry for entry in processes if entry["pid"] in selected]


def browser_acceptance():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BrowserHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first = Browser(session="installed-browser-one", theme="blue")
    second = Browser(session="installed-browser-two", theme="sage")
    evidence = {}
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        page = first.act({"action": "open", "url": base + "/set/one"})
        require(page["title"] == "Browser one", f"first browser failed to load: {page}")
        entry = next(control["element"] for control in page["controls"]
                     if control["tag"] == "input")
        page = first.act({"action": "type", "element": entry, "text": "AIOS"})
        button = next(control["element"] for control in page["controls"]
                      if control["label"] == "Greet")
        page = first.act({"action": "click", "element": button})
        require("Hello AIOS" in page["text"], "bounded browser input/click failed")
        require(not any(control["label"] == "Bottom control" for control in page["controls"]),
                "browser snapshot was not viewport bounded")
        for _ in range(30):
            page = first.act({"action": "scroll", "direction": "down"})
            if any(control["label"] == "Bottom control" for control in page["controls"]):
                break
        require("Bottom marker" in page["text"], "browser scrolling did not reach page bottom")
        page = first.act({"action": "navigate", "url": base + "/next"})
        require(page["title"] == "Next page", "browser navigation failed")
        require(first.act({"action": "back"})["title"] == "Browser one",
                "browser back navigation failed")

        page = second.act({"action": "open", "url": base + "/set/two"})
        require(page["title"] == "Browser two", f"second browser failed to load: {page}")
        first_cookie = first.act({"action": "navigate", "url": base + "/echo"})
        second_cookie = second.act({"action": "navigate", "url": base + "/echo"})
        require("browser_owner=one" in first_cookie["text"]
                and "browser_owner=two" not in first_cookie["text"],
                "first browser profile crossed into the second")
        require("browser_owner=two" in second_cookie["text"]
                and "browser_owner=one" not in second_cookie["text"],
                "second browser profile crossed into the first")

        registrations = discover()
        sessions = {item["session"] for item in registrations}
        require({"installed-browser-one", "installed-browser-two"} <= sessions,
                f"concurrent browser registrations missing: {registrations}")
        trees = descendants(first.process.pid) + descendants(second.process.pid)
        browser_related = [
            item for item in command_lines()
            if item["uid"] == os.getuid()
            and any("aios-browser" in argument
                    or "QtWebEngineProcess" in argument
                    or argument.startswith("--type=")
                    for argument in item["argv"])
        ]
        by_pid = {item["pid"]: item for item in trees + browser_related}
        trees = list(by_pid.values())
        command_text = "\n".join(" ".join(item["argv"]) for item in trees)
        require("--no-sandbox" not in command_text,
                "Chromium sandbox was disabled in a browser process")
        engine_status = []
        for item in trees:
            if any("QtWebEngineProcess" in argument for argument in item["argv"]):
                status = Path(f"/proc/{item['pid']}/status").read_text()
                fields = {}
                for line in status.splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        fields[key] = value.strip()
                engine_status.append({
                    "pid": item["pid"],
                    "type": next((argument for argument in item["argv"]
                                  if argument.startswith("--type=")), ""),
                    "no_zygote_sandbox": "--no-zygote-sandbox" in item["argv"],
                    "no_new_privs": fields.get("NoNewPrivs"),
                    "seccomp": fields.get("Seccomp"),
                })
        require(len(engine_status) >= 2,
                f"Chromium process separation was not observed: {trees}")
        require(any(not item["no_zygote_sandbox"] for item in engine_status),
                f"Chromium sandboxed zygote was not observed: {engine_status}")
        evidence = {
            "sessions": sorted(sessions),
            "isolated_cookies": True,
            "sandbox_disabled_flag_present": False,
            "engine_status": engine_status,
            "browser_processes": trees,
        }
    finally:
        pids = set()
        for browser in (first, second):
            if browser.process:
                pids.update(item["pid"] for item in descendants(browser.process.pid))
            browser.close()
        server.shutdown()
        server.server_close()
        thread.join()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining_pids = [pid for pid in pids if Path(f"/proc/{pid}").exists()]
        if not remaining_pids:
            break
        time.sleep(0.1)
    require(not remaining_pids, f"browser processes survived cleanup: {remaining_pids}")
    remaining_sessions = {item["session"] for item in discover()}
    require("installed-browser-one" not in remaining_sessions
            and "installed-browser-two" not in remaining_sessions,
            f"browser registrations survived cleanup: {remaining_sessions}")
    evidence["cleanup"] = {"processes_removed": True, "registrations_removed": True}
    return evidence


uid = os.getuid()
require(uid != 0, "acceptance must run as the aios user")
require(Path("/etc/aios-mode").read_text().strip() == "installed",
        "/etc/aios-mode does not identify an installed system")
runtime = Path(os.environ["XDG_RUNTIME_DIR"])
runtime_info = runtime.lstat()
require(stat.S_ISDIR(runtime_info.st_mode), "XDG_RUNTIME_DIR is not a directory")
require(runtime_info.st_uid == uid, "XDG_RUNTIME_DIR has the wrong owner")
require(stat.S_IMODE(runtime_info.st_mode) == 0o700, "XDG_RUNTIME_DIR is not mode 0700")

deadline = time.monotonic() + 180
processes = []
while time.monotonic() < deadline:
    processes = command_lines()
    shell = [item for item in processes
             if item["uid"] == uid and any(Path(arg).name == "aios-shell" for arg in item["argv"])]
    runtime_process = [item for item in processes
                       if item["uid"] == uid and "aios.local_runtime" in item["argv"]]
    scheduler = [item for item in processes
                 if item["uid"] == uid and "aios.scheduler" in item["argv"]]
    health = scheduled_jobs.request({"action": "health"}, timeout=0.5)
    if shell and runtime_process and scheduler and health.get("status") == "ok":
        break
    time.sleep(1)
else:
    raise AssertionError("desktop, local runtime, and scheduler did not all become ready")

browser = browser_acceptance()
binding = call("binding", prompt="Return a short installed-system acceptance phrase.")
require(binding["provider"] == "local", f"bundled local provider not selected: {binding}")
require(binding["model"] == "local", f"bundled local model not selected: {binding}")
require("@" in binding["profile"], "provider binding was not made immutable")
schedule = {"kind": "cron", "value": "0 0 1 1 *", "zone": "UTC"}
preview = call("preview", schedule=schedule)
require(len(preview) == 3, f"cron preview was incomplete: {preview}")
config = {
    "title": "Installed QEMU acceptance",
    "prompt": "Reply with one short sentence confirming the installed scheduler acceptance run.",
    "schedule": schedule,
    "execution": {
        "provider": binding["provider"],
        "profile": binding["profile"],
        "model": binding["model"],
        "capabilities": [],
        "timeout_seconds": 300,
        "token_budget": 1024,
        "tool_budget": 0,
        "missed_run": "coalesce",
    },
    "notification": {"mode": "all"},
}
job = call("create", config=config)
request_id = str(uuid.uuid4())
run = call("run_now", job_id=job["id"], expected_revision=job["revision"],
           request_id=request_id)
duplicate = call("run_now", job_id=job["id"], expected_revision=job["revision"],
                 request_id=request_id)
require(duplicate["id"] == run["id"], "run_now UUID deduplication created another run")

deadline = time.monotonic() + 360
while time.monotonic() < deadline:
    result = call("read_result", run_id=run["id"])
    if result["state"] not in ("queued", "running"):
        break
    time.sleep(1)
else:
    raise AssertionError("scheduled local-model run did not reach a terminal state")
require(result["state"] == "succeeded", f"scheduled run failed: {result}")
require(bool(result["result"].strip()), "scheduled run returned an empty result")
unread = call("unread", limit=20)
require(any(item["run_id"] == run["id"] for item in unread),
        "successful result was not present in unread feedback")

database = core.data_dir() / "scheduled" / "jobs.sqlite3"
require(database.is_file() and database.stat().st_size > 0, "scheduler database was not saved")
state = {
    "job_id": job["id"],
    "run_id": run["id"],
    "request_id": request_id,
    "result": result["result"],
}
state_path = core.data_dir() / "installed-qemu-acceptance.json"
with state_path.open("w") as stream:
    json.dump(state, stream, ensure_ascii=False)
    stream.flush()
    os.fsync(stream.fileno())
state_path.chmod(0o600)

evidence = {
    "phase": "first-installed-boot",
    "mode": Path("/etc/aios-mode").read_text().strip(),
    "uid": uid,
    "runtime": {
        "path": str(runtime),
        "owner": runtime_info.st_uid,
        "mode": oct(stat.S_IMODE(runtime_info.st_mode)),
    },
    "processes": {
        "aios_shell": [{"pid": item["pid"], "argv": item["argv"]} for item in shell],
        "local_runtime": [{"pid": item["pid"], "argv": item["argv"]}
                          for item in runtime_process],
        "scheduler": [{"pid": item["pid"], "argv": item["argv"]} for item in scheduler],
    },
    "health": health["result"],
    "binding": binding,
    "browser": browser,
    "preview": preview,
    "job": job,
    "run": result,
    "deduplicated_run_id": duplicate["id"],
    "unread_run_ids": [item["run_id"] for item in unread],
    "database": {"path": str(database), "bytes": database.stat().st_size},
    "acknowledged": False,
}
time.sleep(6)
encoded = base64.b64encode(
    json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).decode()
print("AIOS_EVIDENCE_FIRST:" + encoded, flush=True)
'''

SECOND_ACCEPTANCE = r'''#!/usr/bin/env python3
import base64
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from aios import core, scheduled_jobs


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def request(action, **values):
    return scheduled_jobs.request({"action": action, **values})


def call(action, **values):
    reply = request(action, **values)
    require(reply.get("status") == "ok", f"{action} failed: {reply}")
    return reply["result"]


def scheduler_pids():
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = [part.decode(errors="replace")
                    for part in (entry / "cmdline").read_bytes().split(b"\0") if part]
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if "aios.scheduler" in argv:
            found.append(int(entry.name))
    return found


state_path = core.data_dir() / "installed-qemu-acceptance.json"
state = json.loads(state_path.read_text())
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    initial_health = request("health")
    if initial_health.get("status") == "ok":
        break
    time.sleep(1)
else:
    raise AssertionError("scheduler did not become ready after reboot")
job = call("get", job_id=state["job_id"])
result = call("read_result", run_id=state["run_id"])
require(result["state"] == "succeeded", f"persisted run state changed: {result}")
require(result["result"] == state["result"], "persisted result did not match exactly")
unread_before = call("unread", limit=20)
require(any(item["run_id"] == state["run_id"] for item in unread_before),
        "unread result did not survive reboot")
call("acknowledge_result", run_id=state["run_id"])
unread_after = call("unread", limit=20)
require(all(item["run_id"] != state["run_id"] for item in unread_after),
        "acknowledged result remained unread")

paused = call("pause", job_id=job["id"], expected_revision=job["revision"])
require(not paused["enabled"], "pause did not disable the job")
resumed = call("resume", job_id=job["id"], expected_revision=paused["revision"])
require(resumed["enabled"], "resume did not enable the job")

database = core.data_dir() / "scheduled" / "jobs.sqlite3"
before_files = sorted(
    ({"name": path.name, "bytes": path.stat().st_size}
     for path in database.parent.glob("jobs.sqlite3*") if path.is_file()),
    key=lambda item: item["name"],
)
require(any(item["name"] == "jobs.sqlite3" and item["bytes"] > 0 for item in before_files),
        "scheduler database file is missing before service stop")
old_pids = scheduler_pids()
require(len(old_pids) == 1, f"expected one scheduler process, found {old_pids}")
os.kill(old_pids[0], signal.SIGTERM)
deadline = time.monotonic() + 30
while time.monotonic() < deadline and scheduler_pids():
    time.sleep(0.1)
require(not scheduler_pids(), "scheduler did not stop")
unavailable = request("health")
require(unavailable["status"] == "unavailable",
        f"scheduler request remained available after stop: {unavailable}")
after_files = sorted(
    ({"name": path.name, "bytes": path.stat().st_size}
     for path in database.parent.glob("jobs.sqlite3*") if path.is_file()),
    key=lambda item: item["name"],
)
require(any(item["name"] == "jobs.sqlite3" and item["bytes"] > 0 for item in after_files),
        "scheduler database disappeared when service stopped")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    stored = connection.execute(
        "SELECT state,result FROM runs WHERE id=?", (state["run_id"],)).fetchone()
require(stored == ("succeeded", state["result"]),
        "database contents changed while scheduler was stopped")

restart_log = (core.data_dir() / "scheduled" / "acceptance-restart.log").open("ab")
restarted = subprocess.Popen(
    [sys.executable, "-m", "aios.scheduler"],
    stdin=subprocess.DEVNULL, stdout=restart_log, stderr=subprocess.STDOUT,
    start_new_session=True,
)
restart_log.close()
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    if restarted.poll() is not None:
        raise AssertionError(f"scheduler restart exited with {restarted.returncode}")
    health = request("health")
    if health.get("status") == "ok":
        break
    time.sleep(0.2)
else:
    raise AssertionError("scheduler did not become available after restart")
restored_job = call("get", job_id=state["job_id"])
restored_result = call("read_result", run_id=state["run_id"])
require(restored_result["result"] == state["result"],
        "scheduler restart did not restore the exact result")

deleted = call("delete", job_id=restored_job["id"],
               expected_revision=restored_job["revision"])
require(deleted == {"deleted": True}, f"unexpected delete result: {deleted}")
missing_job = request("get", job_id=state["job_id"])
require(missing_job["status"] == "unavailable",
        "deleted job remained accessible")
saved_result = call("read_result", run_id=state["run_id"])
require(saved_result["state"] == "succeeded"
        and saved_result["result"] == state["result"],
        "delete did not preserve the saved run and result")
list_after_delete = request("list_runs", job_id=state["job_id"], limit=20)
require(list_after_delete["status"] == "unavailable",
        "deleted job unexpectedly remained listable")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    tombstone = connection.execute(
        "SELECT deleted,config FROM jobs WHERE id=?", (state["job_id"],)).fetchone()
    saved = connection.execute(
        "SELECT state,result FROM runs WHERE id=?", (state["run_id"],)).fetchone()
require(tombstone == (1, "{}"), f"job configuration was not tombstoned: {tombstone}")
require(saved == ("succeeded", state["result"]),
        "run/result rows were not preserved after delete")

evidence = {
    "phase": "second-installed-boot",
    "job_id": state["job_id"],
    "run_id": state["run_id"],
    "result": saved_result,
    "exact_result_match": True,
    "unread_before_ack": [item["run_id"] for item in unread_before],
    "unread_after_ack": [item["run_id"] for item in unread_after],
    "pause_revision": paused["revision"],
    "resume_revision": resumed["revision"],
    "scheduler": {
        "stopped_pid": old_pids[0],
        "unavailable_while_stopped": unavailable,
        "database_files_before_stop": before_files,
        "database_files_after_stop": after_files,
        "restarted_pid": restarted.pid,
        "health_after_restart": health["result"],
    },
    "delete": {
        "response": deleted,
        "job_readback": missing_job,
        "list_runs_readback": list_after_delete,
        "tombstone": {"deleted": tombstone[0], "config": tombstone[1]},
        "saved_run_and_result_preserved": True,
    },
}
time.sleep(6)
encoded = base64.b64encode(
    json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).decode()
print("AIOS_EVIDENCE_SECOND:" + encoded, flush=True)
'''

REMINDER_ACCEPTANCE = r'''#!/usr/bin/env python3
import base64
from datetime import datetime, timedelta, timezone
import http.server
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from aios import core, scheduled_jobs


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def request(action, **values):
    return scheduled_jobs.request({"action": action, **values})


def call(action, **values):
    reply = request(action, **values)
    require(reply.get("status") == "ok", f"{action} failed: {reply}")
    return reply["result"]


def execution(binding):
    return {
        "provider": binding["provider"],
        "profile": binding["profile"],
        "model": binding["model"],
        "capabilities": [],
        "timeout_seconds": 300,
        "token_budget": 1024,
        "tool_budget": 0,
        "missed_run": "coalesce",
    }


def wait_run(run_id, timeout=360):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = call("read_result", run_id=run_id)
        if value["state"] not in ("queued", "running"):
            return value
        time.sleep(0.2)
    raise AssertionError(f"run did not finish: {run_id}")


def run_now(job):
    run = call("run_now", job_id=job["id"], expected_revision=job["revision"],
               request_id=str(uuid.uuid4()))
    return wait_run(run["id"])


def window_state():
    try:
        clients = subprocess.run(
            ["xprop", "-root", "_NET_CLIENT_LIST"],
            check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        active = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        return {"clients": clients, "active": active}
    except (FileNotFoundError, subprocess.SubprocessError):
        return {"clients": "unavailable", "active": "unavailable"}


class Provider(http.server.BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        prompt = " ".join(
            str(item.get("content", "")) for item in body["messages"]
            if item.get("role") == "user")
        answer = ("Remote recovery succeeded."
                  if "RECOVERY" in prompt else "Remote installed answer.")
        event = {
            "choices": [{
                "index": 0,
                "delta": {"content": answer},
                "finish_reason": "stop",
            }]
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(
            ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
        self.wfile.flush()


original = core.load_config()
provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
provider.daemon_threads = True
thread = threading.Thread(target=provider.serve_forever, daemon=True)
thread.start()
route_evidence = {}
jobs = []
try:
    remote = dict(original)
    remote.update({
        "mode": "remote",
        "url": f"http://127.0.0.1:{provider.server_port}/v1",
        "model": "installed-remote",
        "api_key": "installed-fixture-key",
        "agent_mode": "current",
    })
    core.write_json(core.config_dir() / "config.json", remote)
    remote_binding = call("binding", prompt="Run a remote installed acceptance.")
    require(remote_binding["provider"] == "remote"
            and remote_binding["model"] == "installed-remote",
            f"remote route was not selected: {remote_binding}")
    remote_job = call("create", config={
        "title": "Remote route acceptance",
        "prompt": "Return the remote installed acceptance.",
        "schedule": {"kind": "cron", "value": "0 0 1 1 *", "zone": "UTC"},
        "execution": execution(remote_binding),
        "notification": {"mode": "all"},
    })
    jobs.append(remote_job)
    remote_result = run_now(remote_job)
    require(remote_result["state"] == "succeeded"
            and remote_result["result"] == "Remote installed answer.",
            f"remote route failed: {remote_result}")
    call("acknowledge_result", run_id=remote_result["id"])

    changed = dict(remote)
    changed["model"] = "changed-model"
    core.write_json(core.config_dir() / "config.json", changed)
    changed_result = run_now(remote_job)
    require(changed_result["state"] == "needs_user_action",
            f"changed binding did not require user action: {changed_result}")
    paused = call("get", job_id=remote_job["id"])
    require(not paused["enabled"], "changed binding did not pause the job")
    changed_unread = [
        item for item in call("unread", limit=50)
        if item["run_id"] == changed_result["id"]]
    require(len(changed_unread) == 1,
            f"changed binding did not create one deduplicated notice: {changed_unread}")
    core.write_json(core.config_dir() / "config.json", remote)
    resumed = call("resume", job_id=paused["id"], expected_revision=paused["revision"])
    recovered = run_now(resumed)
    require(recovered["state"] == "succeeded"
            and recovered["result"] == "Remote installed answer.",
            f"changed binding did not recover: {recovered}")
    call("acknowledge_result", run_id=changed_result["id"])
    call("acknowledge_result", run_id=recovered["id"])

    missing = dict(remote)
    missing["api_key"] = ""
    missing_job = call("create", config={
        "title": "Missing credential acceptance",
        "prompt": "RECOVERY",
        "schedule": {"kind": "cron", "value": "0 0 1 1 *", "zone": "UTC"},
        "execution": execution(remote_binding),
        "notification": {"mode": "all"},
    })
    jobs.append(missing_job)
    core.write_json(core.config_dir() / "config.json", missing)
    missing_result = run_now(missing_job)
    require(missing_result["state"] == "needs_user_action",
            f"missing credentials did not require user action: {missing_result}")
    missing_paused = call("get", job_id=missing_job["id"])
    require(not missing_paused["enabled"], "missing credentials did not pause the job")
    core.write_json(core.config_dir() / "config.json", remote)
    missing_resumed = call(
        "resume", job_id=missing_paused["id"],
        expected_revision=missing_paused["revision"])
    missing_recovered = run_now(missing_resumed)
    require(missing_recovered["state"] == "succeeded"
            and missing_recovered["result"] == "Remote recovery succeeded.",
            f"credential recovery failed: {missing_recovered}")
    call("acknowledge_result", run_id=missing_result["id"])
    call("acknowledge_result", run_id=missing_recovered["id"])

    subscription = dict(original)
    subscription.update({"mode": "chatgpt", "subscription_model": "installed-subscription"})
    core.write_json(core.config_dir() / "config.json", subscription)
    subscription_binding = call("binding", prompt="Run a subscription acceptance.")
    require(subscription_binding["provider"] == "subscription",
            f"subscription route was not selected: {subscription_binding}")
    subscription_job = call("create", config={
        "title": "Subscription route acceptance",
        "prompt": "Return the subscription acceptance.",
        "schedule": {"kind": "cron", "value": "0 0 1 1 *", "zone": "UTC"},
        "execution": execution(subscription_binding),
        "notification": {"mode": "all"},
    })
    jobs.append(subscription_job)
    subscription_result = run_now(subscription_job)
    require(subscription_result["state"] == "needs_user_action",
            f"signed-out subscription did not require user action: {subscription_result}")
    require("Sign in with ChatGPT" in subscription_result["error"],
            f"subscription error was not clear: {subscription_result}")
    call("acknowledge_result", run_id=subscription_result["id"])
    route_evidence = {
        "remote_binding": remote_binding,
        "remote_result": remote_result,
        "changed_binding": changed_result,
        "changed_binding_notice_count": len(changed_unread),
        "changed_binding_recovery": recovered,
        "missing_credentials": missing_result,
        "missing_credentials_recovery": missing_recovered,
        "subscription_binding": subscription_binding,
        "subscription_missing_account": subscription_result,
        "provider_request_count": len(Provider.requests),
        "silent_fallback": False,
    }
finally:
    local = dict(original)
    local.update({
        "mode": "local",
        "model": "local",
        "api_key": "",
        "subscription_model": "",
        "agent_mode": "current",
    })
    core.write_json(core.config_dir() / "config.json", local)
    provider.shutdown()
    provider.server_close()
    thread.join()

health = call("health")
require(health["zone"] == "America/Los_Angeles",
        f"configured IANA zone was not discovered: {health}")
phrase = "remind me in 10 minutes that I need to leave"
binding = call("binding", prompt=phrase)
require(binding["provider"] == "local",
        f"reminder did not return to the configured local route: {binding}")
target = datetime.now(timezone.utc) + timedelta(minutes=10)
due = target.replace(second=0, microsecond=0)
if due < target:
    due += timedelta(minutes=1)
schedule = {
    "kind": "once",
    "value": due.isoformat().replace("+00:00", "Z"),
    "zone": health["zone"],
}
preview = call("preview", schedule=schedule)
require(
    len(preview) == 1
    and datetime.fromisoformat(preview[0]["utc"].replace("Z", "+00:00")) == due,
        f"one-shot reminder preview was incorrect: {preview}")
reminder = call("create", config={
    "title": "Leave reminder",
    "prompt": "Remind me that I need to leave. Reply with one concise notice.",
    "context": "",
    "conversation": "",
    "schedule": schedule,
    "execution": execution(binding),
    "notification": {"mode": "all"},
})
jobs.append(reminder)
require(
    reminder["schedule"]["kind"] == "once"
    and reminder["schedule"]["zone"] == schedule["zone"]
    and datetime.fromisoformat(
        reminder["schedule"]["value"].replace("Z", "+00:00")) == due,
    f"reminder readback changed: {reminder}")
require(datetime.fromisoformat(reminder["next_due"].replace("Z", "+00:00")) == due,
        f"reminder due time changed: {reminder}")
require(reminder["conversation"] == "",
        "reminder retained a source-chat dependency")
before_windows = window_state()

deadline = time.monotonic() + 780
reminder_result = None
while time.monotonic() < deadline:
    runs = call("list_runs", job_id=reminder["id"], limit=10)
    if runs:
        value = call("read_result", run_id=runs[0]["id"])
        if value["state"] not in ("queued", "running"):
            reminder_result = value
            break
    time.sleep(1)
require(reminder_result is not None, "ten-minute reminder did not run at its due time")
require(reminder_result["state"] == "succeeded",
        f"ten-minute reminder failed: {reminder_result}")
require(0 < len(reminder_result["result"].strip()) <= 240,
        f"reminder notice was not concise: {reminder_result['result']!r}")
unread = call("unread", limit=50)
require(any(item["run_id"] == reminder_result["id"] for item in unread),
        "reminder result did not create unread orb feedback")
after_windows = window_state()
require(before_windows == after_windows,
        f"background reminder stole focus or opened a window: {before_windows} -> {after_windows}")

state_path = core.data_dir() / "installed-reminder-acceptance.json"
state = {
    "job_id": reminder["id"],
    "run_id": reminder_result["id"],
    "result": reminder_result["result"],
}
core.write_json(state_path, state)
evidence = {
    "phase": "installed-reminder",
    "phrase": phrase,
    "configured_zone": health["zone"],
    "schedule": schedule,
    "preview": preview,
    "saved_job": reminder,
    "source_chat_closed": True,
    "result": reminder_result,
    "concise_notice": True,
    "unread_run_ids": [item["run_id"] for item in unread],
    "window_state_before": before_windows,
    "window_state_after": after_windows,
    "no_focus_steal_or_automatic_chat": True,
    "route_recovery": route_evidence,
    "missing_zone_behavior": "First installed boot returned zone=null; clients must ask.",
    "suspend_power_behavior": (
        "AIOS does not hardware-wake. While suspended or powered off, execution waits "
        "for resume/boot and the configured missed-run policy applies."
    ),
}
time.sleep(6)
encoded = base64.b64encode(
    json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).decode()
print("AIOS_EVIDENCE_REMINDER:" + encoded, flush=True)
'''

PALETTE_ACCEPTANCE = r'''#!/usr/bin/env python3
import base64
import json
import time
import uuid

from aios import core, scheduled_jobs


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def call(action, **values):
    reply = scheduled_jobs.request({"action": action, **values})
    require(reply.get("status") == "ok", f"{action} failed: {reply}")
    return reply["result"]


config = core.load_config()
require(config["theme_color"] == "sage", f"generated palette was not active: {config}")
require(config["reduced_motion"] is True, f"reduced motion was not active: {config}")
binding = call("binding", prompt="Create an action-needed visual acceptance.")
require(binding["provider"] == "local", f"local binding was unavailable: {binding}")
job = call("create", config={
    "title": "Action needed acceptance",
    "prompt": "Return a short acceptance.",
    "schedule": {"kind": "cron", "value": "0 0 1 1 *", "zone": "UTC"},
    "execution": {
        "provider": binding["provider"],
        "profile": binding["profile"],
        "model": binding["model"],
        "capabilities": [],
        "timeout_seconds": 300,
        "token_budget": 1024,
        "tool_budget": 0,
        "missed_run": "coalesce",
    },
    "notification": {"mode": "all"},
})
changed = dict(config)
changed.update({
    "mode": "remote",
    "url": "http://127.0.0.1:9/v1",
    "model": "changed-route",
    "api_key": "",
})
core.write_json(core.config_dir() / "config.json", changed)
try:
    run = call(
        "run_now", job_id=job["id"], expected_revision=job["revision"],
        request_id=str(uuid.uuid4()))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = call("read_result", run_id=run["id"])
        if result["state"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    else:
        raise AssertionError("action-needed run did not finish")
    require(result["state"] == "needs_user_action",
            f"action-needed visual state was not produced: {result}")
    unread = call("unread", limit=50)
    require(any(item["run_id"] == result["id"] for item in unread),
            "action-needed result was not unread")
finally:
    core.write_json(core.config_dir() / "config.json", config)

time.sleep(6)
evidence = {
    "phase": "generated-palette-reduced-motion",
    "theme_color": config["theme_color"],
    "reduced_motion": config["reduced_motion"],
    "result": result,
    "unread_run_ids": [item["run_id"] for item in unread],
    "steady_attention_only": True,
}
encoded = base64.b64encode(
    json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).decode()
print("AIOS_EVIDENCE_PALETTE:" + encoded, flush=True)
'''

RUNNING_ACCEPTANCE = r'''#!/usr/bin/env python3
import http.server
import json
import threading
import time
import uuid

from aios import core, scheduled_jobs


class Provider(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(60)


def call(action, **values):
    reply = scheduled_jobs.request({"action": action, **values})
    if reply.get("status") != "ok":
        raise AssertionError(f"{action} failed: {reply}")
    return reply["result"]


for item in call("unread", limit=50):
    call("acknowledge_result", run_id=item["run_id"])

original = core.load_config()
provider = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
provider.daemon_threads = True
thread = threading.Thread(target=provider.serve_forever, daemon=True)
thread.start()
remote = dict(original)
remote.update({
    "mode": "remote",
    "url": f"http://127.0.0.1:{provider.server_port}/v1",
    "model": "running-visual",
    "api_key": "running-fixture-key",
    "agent_mode": "current",
})
core.write_json(core.config_dir() / "config.json", remote)
try:
    binding = call("binding", prompt="Hold a scheduled visual acceptance open.")
    job = call("create", config={
        "title": "Running visual acceptance",
        "prompt": "Hold the provider request while the running state is captured.",
        "schedule": {"kind": "cron", "value": "0 0 1 1 *", "zone": "UTC"},
        "execution": {
            "provider": binding["provider"],
            "profile": binding["profile"],
            "model": binding["model"],
            "capabilities": [],
            "timeout_seconds": 120,
            "token_budget": 1024,
            "tool_budget": 0,
            "missed_run": "coalesce",
        },
        "notification": {"mode": "all"},
    })
    run = call(
        "run_now", job_id=job["id"], expected_revision=job["revision"],
        request_id=str(uuid.uuid4()))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = call("read_result", run_id=run["id"])
        if result["state"] == "running":
            break
        if result["state"] not in ("queued", "running"):
            raise AssertionError(f"run ended before capture: {result}")
        time.sleep(0.1)
    else:
        raise AssertionError("run did not enter running state")
    evidence = {
        "phase": "ocean-running",
        "theme_color": original["theme_color"],
        "reduced_motion": original["reduced_motion"],
        "job_id": job["id"],
        "run_id": run["id"],
        "state": result["state"],
    }
    core.write_json(core.data_dir() / "running-visual-acceptance.json", evidence)
    (scheduled_jobs.runtime_dir() / "running-visual-ready").write_text("ready\n")
    time.sleep(30)
finally:
    core.write_json(core.config_dir() / "config.json", original)
    provider.shutdown()
    provider.server_close()
    thread.join()
'''


def parse_args():
    parser = argparse.ArgumentParser(
        description="Install an AIOS ISO into qcow2 and validate two installed boots.")
    parser.add_argument("iso", type=Path, help="AIOS ISO to install")
    parser.add_argument("disk", type=Path, help="Explicit qcow2 path to create or replace")
    parser.add_argument("--disk-size", default="24G", help="qemu-img size (default: 24G)")
    parser.add_argument("--artifacts", "--artifact-dir", dest="artifacts", type=Path,
                        help="Logs, JSON evidence, screenshots, and QEMU control sockets")
    parser.add_argument("--memory-mb", type=int, default=8192)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--total-timeout", type=int, default=4200)
    parser.add_argument("--install-timeout", type=int, default=1800)
    parser.add_argument("--boot-timeout", type=int, default=300)
    parser.add_argument("--acceptance-timeout", type=int, default=900)
    parser.add_argument("--shutdown-timeout", type=int, default=120)
    parser.add_argument("--qemu", default="qemu-system-x86_64")
    parser.add_argument("--qemu-img", default="qemu-img")
    parser.add_argument(
        "--reuse-installed", action="store_true",
        help="Skip disk creation and installation; validate an existing installed qcow2")
    parser.add_argument(
        "--reminder-only", action="store_true",
        help="With --reuse-installed, run only provider and reminder acceptance")
    parser.add_argument(
        "--capture-reminder-only", action="store_true",
        help="With --reuse-installed, capture an already delivered reminder")
    parser.add_argument(
        "--palette-only", action="store_true",
        help="With --reuse-installed, capture generated-palette reduced-motion states")
    parser.add_argument(
        "--running-only", action="store_true",
        help="With --reuse-installed, capture the Ocean running state")
    args = parser.parse_args()
    if args.memory_mb < 2048 or args.cpus < 1:
        parser.error("memory must be at least 2048 MiB and CPUs must be positive")
    if (args.reminder_only or args.capture_reminder_only
            or args.palette_only or args.running_only) and not args.reuse_installed:
        parser.error("reminder-only modes require --reuse-installed")
    if sum((args.reminder_only, args.capture_reminder_only,
            args.palette_only, args.running_only)) > 1:
        parser.error("choose only one single-stage mode")
    for name in ("total_timeout", "install_timeout", "boot_timeout",
                 "acceptance_timeout", "shutdown_timeout"):
        if getattr(args, name) <= 0:
            parser.error("timeouts must be positive")
    if not re.fullmatch(r"[1-9][0-9]*(?:[KMGTP])?", args.disk_size,
                        flags=re.IGNORECASE):
        parser.error("--disk-size must be a positive qemu-img size such as 24G")
    return args


def remaining(deadline, requested):
    value = min(requested, deadline - time.monotonic())
    if value <= 0:
        raise TimeoutError("overall installed-system test timeout expired")
    return value


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


class SerialConsole:
    def __init__(self, path, process, deadline):
        self.path = path
        self.process = process
        self.deadline = deadline
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(0.5)
        self.output = bytearray()
        self.cursor = 0

    def connect(self, timeout):
        deadline = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("QEMU exited before its serial console became ready")
            try:
                self.socket.connect(str(self.path))
                return
            except (ConnectionRefusedError, FileNotFoundError):
                time.sleep(0.1)
        raise TimeoutError("QEMU serial console did not become ready")

    def close(self):
        self.socket.close()

    def send_line(self, value):
        self.socket.sendall(value.encode() + b"\n")

    def _receive(self):
        if self.process.poll() is not None:
            try:
                data = self.socket.recv(65536)
            except OSError:
                data = b""
            if data:
                self.output.extend(data)
            raise RuntimeError(
                f"QEMU exited unexpectedly with status {self.process.returncode}. "
                f"Last serial output:\n{self.tail()}")
        try:
            data = self.socket.recv(65536)
        except socket.timeout:
            return
        if not data:
            raise RuntimeError("QEMU serial console closed unexpectedly. Last output:\n" +
                               self.tail())
        self.output.extend(data)
        if len(self.output) > MAX_SERIAL_BYTES:
            raise RuntimeError("guest serial output exceeded the 64 MiB safety limit")

    def expect(self, pattern, timeout, *, start=None):
        deadline = min(self.deadline, time.monotonic() + timeout)
        offset = self.cursor if start is None else start
        compiled = re.compile(pattern, re.DOTALL) if isinstance(pattern, bytes) else pattern
        while time.monotonic() < deadline:
            match = compiled.search(self.output, offset)
            if match:
                self.cursor = match.end()
                return match
            self._receive()
        raise TimeoutError(
            f"serial pattern {compiled.pattern!r} not seen. Last output:\n{self.tail()}")

    def run(self, command, timeout=30):
        token = os.urandom(8).hex()
        start = len(self.output)
        trailer = (
            f"; rc=$?; printf '\\nAIOS_RC_%s_%s\\n' {shlex.quote(token)} \"$rc\""
        )
        self.send_line(command + trailer)
        match = self.expect(
            rb"\r?\nAIOS_RC_" + token.encode() + rb"_([0-9]+)\r?\n",
            timeout, start=start)
        status = int(match.group(1))
        if status:
            text = bytes(self.output[start:match.start()]).decode(errors="replace")
            raise RuntimeError(
                f"guest command failed with status {status}: {command}\n{text[-4000:]}")
        return bytes(self.output[start:match.start()]).decode(errors="replace")

    def upload_text(self, remote_path, text, timeout=120):
        raw = text.encode()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"serial upload is bounded to {MAX_UPLOAD_BYTES} bytes, got {len(raw)}")
        encoded = base64.b64encode(raw).decode()
        destination = shlex.quote(remote_path)
        staging = shlex.quote(remote_path + ".base64")
        self.run(f": > {staging}", timeout)
        for offset in range(0, len(encoded), UPLOAD_CHUNK_BYTES):
            chunk = encoded[offset:offset + UPLOAD_CHUNK_BYTES]
            self.run(f"printf %s {shlex.quote(chunk)} >> {staging}", timeout)
        digest = hashlib.sha256(raw).hexdigest()
        self.run(
            f"base64 -d {staging} > {destination} && rm -f {staging} && "
            f"chmod 700 {destination} && "
            f"[ \"$(sha256sum {destination} | awk '{{print $1}}')\" = {shlex.quote(digest)} ]",
            timeout)

    def execute_as_aios(self, remote_path, evidence_marker, timeout):
        uid_text = self.run("id -u aios", timeout=10)
        matches = re.findall(r"(?m)^([0-9]+)\r?$", uid_text)
        if not matches:
            raise RuntimeError("could not determine the installed aios UID")
        uid = matches[-1]
        self.run(f"chown aios:aios {shlex.quote(remote_path)}", timeout=10)
        command = (
            f"su -s /bin/sh aios -c "
            + shlex.quote(
                f"HOME=/home/aios USER=aios LOGNAME=aios "
                f"XDG_CONFIG_HOME=/home/aios/.config "
                f"XDG_DATA_HOME=/home/aios/.local/share "
                f"XDG_RUNTIME_DIR=/run/user/{uid} "
                f"DISPLAY=:0 "
                f"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
                f"PYTHONPATH=/usr/local/share/aios "
                f"/usr/bin/python3 {shlex.quote(remote_path)}"
            )
        )
        token = os.urandom(8).hex()
        start = len(self.output)
        trailer = (
            f"; rc=$?; printf '\\nAIOS_USER_RC_%s_%s\\n' "
            f"{shlex.quote(token)} \"$rc\""
        )
        self.send_line(command + trailer)
        match = self.expect(
            (rb"(?:\r?\n)?" + evidence_marker.encode()
             + rb":([A-Za-z0-9+/=]+)\r?\n|(?:\r?\n)AIOS_USER_RC_"
             + token.encode() + rb"_([0-9]+)\r?\n"),
            timeout, start=start)
        if match.group(2) is not None:
            raise RuntimeError(
                f"guest acceptance script failed with status {int(match.group(2))}. "
                f"Last output:\n{self.tail()}")
        try:
            evidence = json.loads(base64.b64decode(match.group(1), validate=True))
        except (ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("guest returned invalid JSON evidence") from error
        completed = self.expect(
            rb"\r?\nAIOS_USER_RC_" + token.encode() + rb"_([0-9]+)\r?\n",
            min(30, timeout))
        if int(completed.group(1)):
            raise RuntimeError(
                f"guest acceptance script exited with status {int(completed.group(1))}")
        return evidence

    def tail(self, size=6000):
        return bytes(self.output[-size:]).decode(errors="replace")


class QemuVM:
    def __init__(self, args, phase, artifacts, deadline, *, iso=False):
        self.args = args
        self.phase = phase
        self.artifacts = artifacts
        self.deadline = deadline
        suffix = f"{os.getpid()}-{phase}"
        self.serial_path = artifacts / f"serial-{suffix}.sock"
        self.qmp_path = artifacts / f"qmp-{suffix}.sock"
        for path in (self.serial_path, self.qmp_path):
            if len(os.fsencode(path)) >= 100:
                raise ValueError(
                    f"QEMU Unix socket path is too long ({path}); choose a shorter --artifacts path")
            if path.exists() or path.is_symlink():
                mode = path.lstat().st_mode
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError(f"refusing to replace non-socket control path: {path}")
                path.unlink()
        safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.disk.stem)[:32] or "disk"
        name = f"AIOS-installed-{safe_stem}-{os.getpid()}-{phase}"
        command = [
            args.qemu,
            "-m", str(args.memory_mb),
            "-smp", str(args.cpus),
            "-name", name,
            "-drive", f"file={args.disk},format=qcow2,if=virtio",
            "-boot", "d" if iso else "c",
            "-display", "none",
            "-nic", "none",
            "-no-reboot",
            "-serial", f"unix:{self.serial_path},server=on,wait=off",
            "-qmp", f"unix:{self.qmp_path},server=on,wait=off",
        ]
        if iso:
            command += ["-cdrom", str(args.iso)]
        if os.access("/dev/kvm", os.R_OK | os.W_OK):
            command += ["-enable-kvm", "-cpu", "host"]
        self.command = command
        self.stderr_stream = None
        self.process = None
        self.console = None

    def __enter__(self):
        try:
            write_json(self.artifacts / f"{self.phase}-qemu.json", {
                "command": self.command,
                "kvm": "-enable-kvm" in self.command,
                "name": self.command[self.command.index("-name") + 1],
            })
            self.stderr_stream = (
                self.artifacts / f"{self.phase}-qemu.stderr.log").open("wb")
            self.process = subprocess.Popen(
                self.command, stdout=subprocess.DEVNULL, stderr=self.stderr_stream)
            self.console = SerialConsole(self.serial_path, self.process, self.deadline)
            self.console.connect(remaining(self.deadline, self.args.boot_timeout))
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        if self.console is not None:
            (self.artifacts / f"{self.phase}-serial.log").write_bytes(self.console.output)
            self.console.close()
        if self.process is not None and self.process.poll() is None:
            try:
                self.qmp("quit", timeout=3)
            except Exception:
                if self.process.poll() is None:
                    self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.stderr_stream is not None:
            self.stderr_stream.close()
        for path in (self.serial_path, self.qmp_path):
            if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
                path.unlink()

    def qmp(self, execute, arguments=None, timeout=10):
        deadline = min(self.deadline, time.monotonic() + timeout)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            control.settimeout(0.5)
            while True:
                try:
                    control.connect(str(self.qmp_path))
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    if self.process.poll() is not None:
                        raise RuntimeError("QEMU exited before QMP became ready")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("QMP socket did not become ready")
                    time.sleep(0.1)
            control.settimeout(max(0.1, deadline - time.monotonic()))
            with control.makefile("rwb", buffering=0) as stream:
                greeting = self._qmp_response(stream, deadline)
                if "QMP" not in greeting:
                    raise RuntimeError(f"invalid QMP greeting: {greeting}")
                stream.write(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
                self._qmp_return(stream, deadline)
                command = {"execute": execute}
                if arguments is not None:
                    command["arguments"] = arguments
                stream.write(json.dumps(command).encode() + b"\n")
                return self._qmp_return(stream, deadline)

    @staticmethod
    def _qmp_response(stream, deadline):
        while time.monotonic() < deadline:
            try:
                line = stream.readline()
            except socket.timeout:
                continue
            if line:
                return json.loads(line)
        raise TimeoutError("QMP response timed out")

    def _qmp_return(self, stream, deadline):
        while True:
            response = self._qmp_response(stream, deadline)
            if "error" in response:
                raise RuntimeError(f"QMP command failed: {response['error']}")
            if "return" in response:
                return response["return"]

    def screenshot(self, filename, *, require_visible=True):
        destination = self.artifacts / filename
        self.qmp("screendump", {"filename": str(destination.resolve())}, timeout=30)
        raw = destination.read_bytes()
        try:
            magic, dimensions, maximum, pixels = raw.split(b"\n", 3)
            width, height = map(int, dimensions.split())
        except (ValueError, IndexError) as error:
            raise RuntimeError(f"QMP produced an invalid screenshot: {destination}") from error
        if (magic != b"P6" or maximum != b"255"
                or len(pixels) != width * height * 3):
            raise RuntimeError(f"QMP produced an invalid screenshot: {destination}")
        colors = {pixels[offset:offset + 3] for offset in range(0, len(pixels), 3)}
        visible = sum(max(pixels[offset:offset + 3]) > 32
                      for offset in range(0, len(pixels), 3))
        if require_visible and (len(colors) < 16 or visible < width * height // 50):
            raise RuntimeError(f"QMP screenshot is black or blank: {destination}")
        return destination

    def status(self):
        return self.qmp("query-status")

    def click(self, x, y, *, width=1280, height=800):
        self.qmp("input-send-event", {"events": [
            {"type": "abs", "data": {"axis": "x", "value": round(x * 32767 / width)}},
            {"type": "abs", "data": {"axis": "y", "value": round(y * 32767 / height)}},
        ]})
        self.qmp("input-send-event", {"events": [
            {"type": "btn", "data": {"button": "left", "down": True}},
        ]})
        time.sleep(0.1)
        self.qmp("input-send-event", {"events": [
            {"type": "btn", "data": {"button": "left", "down": False}},
        ]})

    def move_pointer(self, x, y, *, width=1280, height=800):
        self.qmp("input-send-event", {"events": [
            {"type": "abs", "data": {"axis": "x", "value": round(x * 32767 / width)}},
            {"type": "abs", "data": {"axis": "y", "value": round(y * 32767 / height)}},
        ]})

    def send_keys(self, *keys):
        self.qmp("send-key", {
            "keys": [{"type": "qcode", "data": key} for key in keys],
        })

    def wait_for_exit(self, timeout):
        try:
            return self.process.wait(timeout=min(timeout, remaining(self.deadline, timeout)))
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"{self.phase} guest did not power off") from error


def login_root(console, timeout):
    console.expect(rb"aios login:\s*$", timeout)
    console.send_line("root")
    console.expect(rb"aios:~#", timeout)


def install(args, artifacts, deadline):
    started = time.time()
    with QemuVM(args, "install", artifacts, deadline, iso=True) as vm:
        console = vm.console
        console.expect(rb"boot:\s*$", remaining(deadline, args.boot_timeout))
        console.send_line("install")
        console.expect(rb"aios login:\s*$", remaining(deadline, args.boot_timeout))
        console.send_line("root")
        console.expect(rb"aios:~#\s*$", remaining(deadline, args.boot_timeout))
        start = len(console.output)
        console.send_line("/usr/local/sbin/aios-install")
        console.expect(rb"Full disk path \(or Enter to cancel\):\s*$",
                       remaining(deadline, args.install_timeout), start=start)
        console.send_line("/dev/vda")
        console.expect(
            rb"Type ERASE /dev/vda to permanently erase this disk:\s*$",
            remaining(deadline, args.install_timeout))
        console.send_line("ERASE /dev/vda")
        console.expect(
            rb"Installation complete\. Shut down, remove the ISO/USB, then boot from disk\.",
            remaining(deadline, args.install_timeout))
        # The serial installer intentionally runs before the graphical session.
        screenshot = vm.screenshot("install-complete.ppm", require_visible=False)
        status = vm.status()
        write_json(artifacts / "installation.json", {
            "disk": str(args.disk),
            "disk_size": args.disk_size,
            "iso": str(args.iso),
            "qmp_status_before_poweroff": status,
            "screenshot": screenshot.name,
            "started_unix": started,
            "completed_unix": time.time(),
        })
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(f"installer VM exited with status {exit_code}")


def installed_boot(args, artifacts, deadline, phase, script, marker, evidence_name):
    with QemuVM(args, phase, artifacts, deadline) as vm:
        console = vm.console
        login_root(console, remaining(deadline, args.boot_timeout))
        if phase == "installed-first":
            console.run(
                "install -d -o aios -g aios -m 700 /home/aios/.config/aios && "
                "printf '[General]\\ndismissed=true\\n' "
                "> /home/aios/.config/aios/setup.conf && "
                "chown aios:aios /home/aios/.config/aios/setup.conf && "
                "chmod 600 /home/aios/.config/aios/setup.conf",
                timeout=10)
        remote_path = f"/home/aios/.local/state/aios/{phase}-acceptance.py"
        console.run(
            "install -d -o aios -g aios -m 700 /home/aios/.local/state/aios",
            timeout=30)
        console.upload_text(remote_path, script,
                            timeout=remaining(deadline, args.acceptance_timeout))
        evidence = console.execute_as_aios(
            remote_path, marker, remaining(deadline, args.acceptance_timeout))
        screenshot = vm.screenshot(f"{phase}.ppm")
        evidence["host_capture"] = {
            "qmp_status_before_poweroff": vm.status(),
            "screenshot": screenshot.name,
        }
        write_json(artifacts / evidence_name, evidence)
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(f"{phase} VM exited with status {exit_code}")


def reminder_boot(args, artifacts, deadline):
    phase = "installed-reminder"
    with QemuVM(args, phase, artifacts, deadline) as vm:
        console = vm.console
        login_root(console, remaining(deadline, args.boot_timeout))
        console.run("printf '%s\\n' America/Los_Angeles > /etc/timezone", timeout=10)
        time.sleep(2)
        vm.send_keys("alt", "f4")
        time.sleep(2)
        remote_path = "/home/aios/.local/state/aios/installed-reminder-acceptance.py"
        console.run(
            "install -d -o aios -g aios -m 700 /home/aios/.local/state/aios",
            timeout=30)
        console.upload_text(
            remote_path, REMINDER_ACCEPTANCE,
            timeout=remaining(deadline, args.acceptance_timeout))
        evidence = console.execute_as_aios(
            remote_path, "AIOS_EVIDENCE_REMINDER",
            remaining(deadline, args.acceptance_timeout))
        vm.move_pointer(50, 50)
        time.sleep(2)
        unread = vm.screenshot("reminder-unread.ppm")
        vm.click(640, 724)
        time.sleep(2)
        inbox = vm.screenshot("reminder-inbox.ppm")
        # The newest unread reminder is the first inbox row; View is its explicit action.
        vm.click(270, 326)
        time.sleep(3)
        result = vm.screenshot("reminder-result.ppm")
        evidence["host_capture"] = {
            "qmp_status_before_poweroff": vm.status(),
            "screenshots": [unread.name, inbox.name, result.name],
            "orb_click": {"x": 640, "y": 724},
            "result_click": {"x": 270, "y": 326},
        }
        write_json(artifacts / "reminder.json", evidence)
        console.run(
            "su -s /bin/sh aios -c "
            + shlex.quote(
                "HOME=/home/aios XDG_CONFIG_HOME=/home/aios/.config "
                "PYTHONPATH=/usr/local/share/aios /usr/bin/python3 -c "
                + shlex.quote(
                    "from aios import core; value=core.load_config(); "
                    "value.update(theme_color='sage', reduced_motion=True); "
                    "core.write_json(core.config_dir() / 'config.json', value)"
                )
            ),
            timeout=15)
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(f"{phase} VM exited with status {exit_code}")


def palette_boot(args, artifacts, deadline):
    phase = "installed-palette"
    with QemuVM(args, phase, artifacts, deadline) as vm:
        console = vm.console
        login_root(console, remaining(deadline, args.boot_timeout))
        vm.move_pointer(50, 50)
        time.sleep(6)
        idle = vm.screenshot("palette-idle-reduced.ppm")
        remote_path = "/home/aios/.local/state/aios/installed-palette-acceptance.py"
        console.upload_text(
            remote_path, PALETTE_ACCEPTANCE,
            timeout=remaining(deadline, args.acceptance_timeout))
        evidence = console.execute_as_aios(
            remote_path, "AIOS_EVIDENCE_PALETTE",
            remaining(deadline, args.acceptance_timeout))
        vm.move_pointer(50, 50)
        time.sleep(2)
        action = vm.screenshot("palette-action-needed-reduced.ppm")
        evidence["host_capture"] = {
            "qmp_status_before_poweroff": vm.status(),
            "screenshots": [idle.name, action.name],
        }
        write_json(artifacts / "palette.json", evidence)
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(f"{phase} VM exited with status {exit_code}")


def running_boot(args, artifacts, deadline):
    with QemuVM(args, "installed-running-prep", artifacts, deadline) as vm:
        console = vm.console
        login_root(console, remaining(deadline, args.boot_timeout))
        script = (
            "from aios import core; value=core.load_config(); "
            "value.update(theme_color='blue', reduced_motion=False); "
            "core.write_json(core.config_dir() / 'config.json', value)"
        )
        console.run(
            "su -s /bin/sh aios -c "
            + shlex.quote(
                "HOME=/home/aios XDG_CONFIG_HOME=/home/aios/.config "
                "PYTHONPATH=/usr/local/share/aios /usr/bin/python3 -c "
                + shlex.quote(script)
            ),
            timeout=15)
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(f"installed-running-prep VM exited with status {exit_code}")

    phase = "installed-running"
    with QemuVM(args, phase, artifacts, deadline) as vm:
        console = vm.console
        login_root(console, remaining(deadline, args.boot_timeout))
        remote_path = "/home/aios/.local/state/aios/installed-running-acceptance.py"
        console.upload_text(
            remote_path, RUNNING_ACCEPTANCE,
            timeout=remaining(deadline, args.acceptance_timeout))
        uid_text = console.run("id -u aios", timeout=10)
        uid = re.findall(r"(?m)^([0-9]+)\r?$", uid_text)[-1]
        console.run(f"chown aios:aios {shlex.quote(remote_path)}", timeout=10)
        command = (
            "su -s /bin/sh aios -c "
            + shlex.quote(
                f"HOME=/home/aios USER=aios LOGNAME=aios "
                f"XDG_CONFIG_HOME=/home/aios/.config "
                f"XDG_DATA_HOME=/home/aios/.local/share "
                f"XDG_RUNTIME_DIR=/run/user/{uid} DISPLAY=:0 "
                f"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
                f"PYTHONPATH=/usr/local/share/aios "
                f"setsid /usr/bin/python3 {shlex.quote(remote_path)} "
                f">/home/aios/.local/state/aios/running-visual.log 2>&1 &"
            )
        )
        console.run(command, timeout=10)
        deadline_ready = time.monotonic() + 60
        while time.monotonic() < deadline_ready:
            output = console.run(
                f"test -f /run/user/{uid}/aios-scheduler/running-visual-ready "
                "&& echo ready || true",
                timeout=10)
            if re.search(r"(?m)^ready\r?$", output):
                break
            time.sleep(1)
        else:
            log = console.run(
                "tail -100 /home/aios/.local/state/aios/running-visual.log || true",
                timeout=10)
            raise TimeoutError(f"running visual did not become ready:\n{log}")
        time.sleep(6)
        vm.move_pointer(50, 50)
        time.sleep(2)
        screenshot = vm.screenshot("ocean-running.ppm")
        state = console.run(
            "cat /home/aios/.local/share/aios/running-visual-acceptance.json",
            timeout=10)
        write_json(artifacts / "running.json", {
            "saved_state_output": state,
            "screenshot": screenshot.name,
            "qmp_status_before_poweroff": vm.status(),
        })
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(f"{phase} VM exited with status {exit_code}")


def capture_reminder_boot(args, artifacts, deadline):
    with QemuVM(args, "installed-reminder-prep", artifacts, deadline) as vm:
        console = vm.console
        login_root(console, remaining(deadline, args.boot_timeout))
        console.run(
            "install -d -o aios -g aios -m 700 /home/aios/.config/aios && "
            "printf '[General]\\ndismissed=true\\n' "
            "> /home/aios/.config/aios/setup.conf && "
            "chown aios:aios /home/aios/.config/aios/setup.conf && "
            "chmod 600 /home/aios/.config/aios/setup.conf",
            timeout=10)
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(
                f"installed-reminder-prep VM exited with status {exit_code}")

    phase = "installed-reminder-capture"
    with QemuVM(args, phase, artifacts, deadline) as vm:
        console = vm.console
        login_root(console, remaining(deadline, args.boot_timeout))
        state = console.run(
            "cat /home/aios/.local/share/aios/installed-reminder-acceptance.json",
            timeout=10)
        vm.move_pointer(50, 50)
        time.sleep(8)
        unread = vm.screenshot("reminder-unread.ppm")
        vm.click(640, 724)
        time.sleep(2)
        inbox = vm.screenshot("reminder-inbox.ppm")
        vm.click(270, 326)
        time.sleep(3)
        result = vm.screenshot("reminder-result.ppm")
        write_json(artifacts / "reminder-capture.json", {
            "saved_state_output": state,
            "screenshots": [unread.name, inbox.name, result.name],
            "orb_click": {"x": 640, "y": 724},
            "result_click": {"x": 270, "y": 326},
            "qmp_status_before_poweroff": vm.status(),
        })
        console.send_line("poweroff")
        exit_code = vm.wait_for_exit(remaining(deadline, args.shutdown_timeout))
        if exit_code != 0:
            raise RuntimeError(f"{phase} VM exited with status {exit_code}")


def main():
    args = parse_args()
    if os.name != "posix":
        raise RuntimeError("this QEMU serial/QMP test must run on Linux (including WSL)")
    args.iso = args.iso.expanduser().resolve()
    args.disk = args.disk.expanduser().absolute()
    if not args.iso.is_file():
        raise FileNotFoundError(f"ISO does not exist: {args.iso}")
    if args.disk.suffix.lower() != ".qcow2":
        raise ValueError("the explicit disk path must end in .qcow2")
    if args.disk == args.iso:
        raise ValueError("the explicit qcow2 path must differ from the ISO path")
    if shutil.which(args.qemu) is None:
        raise FileNotFoundError(f"QEMU executable not found: {args.qemu}")
    if shutil.which(args.qemu_img) is None:
        raise FileNotFoundError(f"qemu-img executable not found: {args.qemu_img}")

    artifacts = (args.artifacts.expanduser().resolve() if args.artifacts else
                 args.disk.parent / f"{args.disk.stem}-installed-test-artifacts")
    if artifacts == args.disk:
        raise ValueError("artifact directory must differ from the explicit qcow2 path")
    artifacts.mkdir(parents=True, exist_ok=True)
    if not artifacts.is_dir():
        raise NotADirectoryError(f"artifact path is not a directory: {artifacts}")
    args.artifacts = artifacts
    for phase in ("install", "installed-first", "installed-second"):
        for kind in ("serial", "qmp"):
            control_path = artifacts / f"{kind}-{os.getpid()}-{phase}.sock"
            if len(os.fsencode(control_path)) >= 100:
                raise ValueError(
                    f"QEMU Unix socket path is too long ({control_path}); "
                    "choose a shorter --artifacts path")

    if not args.disk.parent.is_dir():
        raise FileNotFoundError(
            f"qcow2 parent directory does not exist: {args.disk.parent}")
    if args.reuse_installed and not args.disk.is_file():
        raise FileNotFoundError(f"installed qcow2 does not exist: {args.disk}")
    if not args.reuse_installed and (args.disk.exists() or args.disk.is_symlink()):
        mode = args.disk.lstat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise RuntimeError(
                f"refusing to replace non-file disk path: {args.disk}")
        args.disk.unlink()
    if not args.reuse_installed:
        subprocess.run(
            [args.qemu_img, "create", "-f", "qcow2", str(args.disk), args.disk_size],
            check=True)

    deadline = time.monotonic() + args.total_timeout
    summary = {
        "iso": str(args.iso),
        "disk": str(args.disk),
        "disk_size": args.disk_size,
        "artifacts": str(artifacts),
        "started_unix": time.time(),
        "status": "running",
    }
    write_json(artifacts / "summary.json", summary)
    try:
        if args.capture_reminder_only:
            print("Capturing delivered reminder UI ...", flush=True)
            capture_reminder_boot(args, artifacts, deadline)
            summary.update(
                status="passed",
                completed_unix=time.time(),
                evidence=["reminder-capture.json"],
                screenshots=[
                    "reminder-unread.ppm", "reminder-inbox.ppm", "reminder-result.ppm"],
            )
            write_json(artifacts / "summary.json", summary)
            print(f"PASS: delivered reminder UI capture ({artifacts})", flush=True)
            return
        if args.reminder_only:
            print("Validating provider recovery and the ten-minute reminder ...", flush=True)
            reminder_boot(args, artifacts, deadline)
            print("Capturing generated-palette reduced-motion states ...", flush=True)
            palette_boot(args, artifacts, deadline)
            summary.update(
                status="passed",
                completed_unix=time.time(),
                evidence=["reminder.json", "palette.json"],
                screenshots=[
                    "reminder-unread.ppm", "reminder-inbox.ppm", "reminder-result.ppm",
                    "palette-idle-reduced.ppm", "palette-action-needed-reduced.ppm"],
            )
            write_json(artifacts / "summary.json", summary)
            print(
                f"PASS: provider recovery and reminder delivery ({artifacts})",
                flush=True,
            )
            return
        if args.palette_only:
            print("Capturing generated-palette reduced-motion states ...", flush=True)
            palette_boot(args, artifacts, deadline)
            summary.update(
                status="passed",
                completed_unix=time.time(),
                evidence=["palette.json"],
                screenshots=["palette-idle-reduced.ppm", "palette-action-needed-reduced.ppm"],
            )
            write_json(artifacts / "summary.json", summary)
            print(
                f"PASS: generated-palette reduced-motion states ({artifacts})",
                flush=True,
            )
            return
        if args.running_only:
            print("Capturing Ocean running state ...", flush=True)
            running_boot(args, artifacts, deadline)
            summary.update(
                status="passed",
                completed_unix=time.time(),
                evidence=["running.json"],
                screenshots=["ocean-running.ppm"],
            )
            write_json(artifacts / "summary.json", summary)
            print(f"PASS: Ocean running state ({artifacts})", flush=True)
            return
        if not args.reuse_installed:
            print(f"Installing {args.iso.name} into {args.disk} ...", flush=True)
            install(args, artifacts, deadline)
        print("Validating first installed boot and local scheduled run ...", flush=True)
        installed_boot(
            args, artifacts, deadline, "installed-first",
            FIRST_ACCEPTANCE, "AIOS_EVIDENCE_FIRST", "first-boot.json")
        print("Rebooting to validate persistence and scheduler lifecycle ...", flush=True)
        installed_boot(
            args, artifacts, deadline, "installed-second",
            SECOND_ACCEPTANCE, "AIOS_EVIDENCE_SECOND", "second-boot.json")
        print("Validating provider recovery and the ten-minute reminder ...", flush=True)
        reminder_boot(args, artifacts, deadline)
        print("Capturing generated-palette reduced-motion states ...", flush=True)
        palette_boot(args, artifacts, deadline)
        print("Capturing Ocean running state ...", flush=True)
        running_boot(args, artifacts, deadline)
    except BaseException as error:
        summary.update(status="failed", completed_unix=time.time(),
                       error=f"{type(error).__name__}: {error}")
        write_json(artifacts / "summary.json", summary)
        raise
    summary.update(
        status="passed",
        completed_unix=time.time(),
        evidence=[
            "installation.json", "first-boot.json", "second-boot.json", "reminder.json",
            "palette.json", "running.json"],
        screenshots=[
            "install-complete.ppm", "installed-first.ppm", "installed-second.ppm",
            "reminder-unread.ppm", "reminder-inbox.ppm", "reminder-result.ppm",
            "palette-idle-reduced.ppm", "palette-action-needed-reduced.ppm",
            "ocean-running.ppm"],
    )
    write_json(artifacts / "summary.json", summary)
    print(
        "PASS: real install, installed boot, local scheduled run, reboot persistence, "
        "scheduler restart, browser isolation, provider recovery, reminder delivery, "
        f"and delete semantics ({artifacts})",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
