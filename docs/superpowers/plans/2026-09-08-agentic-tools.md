# Agentic Tools and Application Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add standards-compatible skills, allowlisted stdio MCP tools, explicit remote agent routing, and a cached built-in application builder that can create and launch a calculator or similar offline utility.

**Architecture:** Replace the browser-only broker with a per-chat tool host that owns browser, application, and MCP adapters. A provider-neutral agent session loads skills, filters tools, selects the configured agent provider for remote-preferred skills, and supplies identical validated function definitions to OpenAI-compatible Chat Completions and the existing Codex App Server adapter.

**Tech Stack:** Python 3 standard library, Qt 6/QML/C++, OpenAI-compatible Chat Completions, Codex App Server JSON-RPC, MCP 2025-06-18 stdio JSON-RPC, Chromium, `unittest`.

---

## File Map

| File | Responsibility |
|---|---|
| `apps/aios/skills.py` | Parse, validate, discover, match, and progressively activate `SKILL.md` directories. |
| `apps/skills/application-builder/SKILL.md` | Built-in instructions and trigger metadata for cached offline application creation. |
| `apps/aios/applications.py` | Safe application cache, manifest lifecycle, model-facing application tool, and runner launch. |
| `apps/aios/app_runner.py` | Trusted loopback wrapper, sandboxed iframe, CSP headers, and Chromium application lifecycle. |
| `apps/aios/mcp.py` | Strict MCP config loading, stdio lifecycle, tool discovery, allowlisting, calls, and cleanup. |
| `apps/aios/toolhost.py` | Private Unix-socket registry for browser, application, and MCP tools. |
| `apps/aios/browser.py` | Retain browser implementation and move its function schema beside the adapter. |
| `apps/aios/agent.py` | Provider-neutral skill state, tool filtering/dispatch, OpenAI-compatible loop, and routing decision. |
| `apps/aios/core.py` | Current/agent endpoint selection, model selection, credentials, and sanitized transport errors. |
| `apps/aios/subscription.py` | Supply generic dynamic tools to Codex and dispatch through the shared agent session. |
| `apps/aios/worker.py` | Send desktop chat through the provider-neutral agent whenever a tool socket is present. |
| `apps/aios/cli.py` | Configure the separate agent provider and key. |
| `apps/shell/main.cpp` | Start/stop the generic tool host instead of the browser-only broker. |
| `apps/shell/ModelSettings.qml` | Configure current, ChatGPT, or remote service for agent tasks. |
| `scripts/build-apps.sh` | Package built-in skills with AIOS. |
| `tests/test_skills.py` | Skill format, precedence, activation, and filtering tests. |
| `tests/test_applications.py` | Cache, path, manifest, launch, iframe, and CSP tests. |
| `tests/fixtures/mcp_server.py` | Deterministic MCP protocol peer. |
| `tests/test_mcp.py` | MCP lifecycle, allowlist, error, timeout, and cleanup tests. |
| `tests/test_toolhost.py` | Generic registry and private socket dispatch tests. |
| `tests/test_browser_agent.py` | Generalized OpenAI tool-loop and application-builder flow tests. |
| `tests/test_subscription.py` | Generic Codex dynamic-tool and routing tests. |
| `tests/test_core.py` | Agent provider configuration and transport tests. |
| `tests/qml/tst_subscription.qml` | Agent provider field persistence and secret-redaction tests. |
| `README.md`, `docs/architecture.md`, `docs/browser.md`, `docs/chatgpt-subscription.md`, `docs/specs/chat-desktop.md`, `docs/agentic-tools.md`, `docs/qa/implementation-status.md` | User guidance, architecture, trust boundaries, and validation status. |

### Task 1: Add the Agent Skills catalog

**Files:**
- Create: `apps/aios/skills.py`
- Create: `apps/skills/application-builder/SKILL.md`
- Create: `tests/test_skills.py`

- [ ] **Step 1: Write failing discovery and activation tests**

Create `tests/test_skills.py` with tests that use temporary built-in and user
roots rather than the host configuration:

```python
import tempfile
import unittest
from pathlib import Path

from aios import skills


def write_skill(root, name, description, body="Instructions.", metadata="", allowed=""):
    folder = Path(root) / name
    folder.mkdir(parents=True)
    fields = [
        "---",
        f"name: {name}",
        f"description: {description}",
    ]
    if metadata:
        fields.extend(["metadata:", *["  " + line for line in metadata.splitlines()]])
    if allowed:
        fields.append(f"allowed-tools: {allowed}")
    fields.extend(["---", body])
    (folder / "SKILL.md").write_text("\n".join(fields), encoding="utf-8")


class SkillTests(unittest.TestCase):
    def test_user_skill_overrides_builtin_and_matches_trigger(self):
        with tempfile.TemporaryDirectory() as temp:
            builtin = Path(temp) / "builtin"
            user = Path(temp) / "user"
            write_skill(
                builtin, "application-builder", "Build local applications.",
                metadata='aios-triggers: "app, calculator"\naios-model: remote-preferred',
                allowed="application",
            )
            write_skill(user, "application-builder", "User application rules.", body="Use the user rules.")
            catalog = skills.load_skills([builtin, user])
            self.assertEqual(catalog["application-builder"].description, "User application rules.")
            self.assertEqual(catalog["application-builder"].instructions, "Use the user rules.")

            catalog = skills.load_skills([builtin])
            selected = skills.initial_skills(catalog, "I need a calculator")
            self.assertEqual([skill.name for skill in selected], ["application-builder"])
            self.assertEqual(selected[0].allowed_tools, ("application",))
            self.assertEqual(selected[0].model, "remote-preferred")

    def test_explicit_skill_and_catalog_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            write_skill(temp, "notes", "Organize notes.", body="Keep notes concise.")
            catalog = skills.load_skills([Path(temp)])
            selected = skills.initial_skills(catalog, "/notes sort these")
            self.assertEqual([skill.name for skill in selected], ["notes"])
            prompt = skills.catalog_prompt(catalog)
            self.assertIn("notes: Organize notes.", prompt)
            self.assertNotIn("Keep notes concise.", prompt)

    def test_invalid_skills_are_reported_without_loading(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "Bad_Name"
            folder.mkdir()
            (folder / "SKILL.md").write_text(
                "---\nname: Bad_Name\ndescription: invalid\n---\nbody",
                encoding="utf-8",
            )
            catalog, warnings = skills.load_skills([Path(temp)], include_warnings=True)
            self.assertEqual(catalog, {})
            self.assertEqual(len(warnings), 1)
            self.assertNotIn(str(folder), warnings[0])
```

- [ ] **Step 2: Run the tests and confirm the module is missing**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_skills -v
```

Expected: `ImportError` for `aios.skills`.

- [ ] **Step 3: Implement the strict skill loader**

Create `apps/aios/skills.py` with these public interfaces:

```python
"""Standards-compatible Agent Skills discovery with bounded frontmatter parsing."""
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import core

SYSTEM_SKILLS = Path("/usr/local/share/aios/skills")
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SKILL_BYTES = 48 * 1024
MAX_ACTIVE_SKILLS = 3
MAX_SKILLS = 64


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    allowed_tools: tuple[str, ...]
    triggers: tuple[str, ...]
    model: str


def skill_roots():
    return [SYSTEM_SKILLS, core.config_dir() / "skills"]


def _scalar(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("Skill fields must be strings.")
        return parsed
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def _frontmatter(text):
    lines = text.splitlines()
    if len(lines) < 4 or lines[0] != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter.")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise ValueError("SKILL.md frontmatter is not closed.") from None
    values, metadata, section = {}, {}, None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  ") and section == "metadata":
            key, separator, value = line.strip().partition(":")
            if not separator or not key:
                raise ValueError("Invalid skill metadata.")
            metadata[key] = _scalar(value)
            continue
        key, separator, value = line.partition(":")
        if not separator or line.startswith((" ", "\t")):
            raise ValueError("Invalid skill frontmatter.")
        section = key if key == "metadata" and not value.strip() else None
        if section is None:
            values[key] = _scalar(value)
    return values, metadata, "\n".join(lines[end + 1:]).strip()


def _load(path):
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError("Skill instructions are too large.")
    values, metadata, body = _frontmatter(path.read_text(encoding="utf-8"))
    name = values.get("name", "")
    description = values.get("description", "")
    if not NAME.fullmatch(name) or path.parent.name != name:
        raise ValueError("Skill name is invalid.")
    if not description or len(description) > 1024 or not body:
        raise ValueError("Skill description or instructions are invalid.")
    allowed = tuple(dict.fromkeys(values.get("allowed-tools", "").split()))
    triggers = tuple(
        phrase.strip().casefold()
        for phrase in metadata.get("aios-triggers", "").split(",")
        if phrase.strip()
    )
    model = metadata.get("aios-model", "current")
    if model not in ("current", "remote-preferred"):
        raise ValueError("Skill model preference is invalid.")
    return Skill(name, description, body, allowed, triggers, model)


def load_skills(roots=None, include_warnings=False):
    catalog, warnings = {}, []
    for root in roots or skill_roots():
        if not Path(root).is_dir():
            continue
        for folder in sorted(Path(root).iterdir()):
            path = folder / "SKILL.md"
            if not folder.is_dir() or not path.is_file():
                continue
            try:
                if folder.name not in catalog and len(catalog) >= MAX_SKILLS:
                    warnings.append("Additional skills were skipped because the catalog limit was reached.")
                    continue
                catalog[folder.name] = _load(path)
            except (OSError, UnicodeError, ValueError) as error:
                warnings.append(f"Skill {folder.name!r} was skipped: {error}")
    return (catalog, warnings) if include_warnings else catalog


def initial_skills(catalog, prompt):
    text = prompt.casefold()
    explicit = text.split(maxsplit=1)[0][1:] if text.startswith("/") else ""
    normalized = " " + " ".join(re.findall(r"[a-z0-9]+", text)) + " "
    selected = []
    for skill in catalog.values():
        matched = any(
            " " + " ".join(re.findall(r"[a-z0-9]+", trigger)) + " " in normalized
            for trigger in skill.triggers
        )
        if skill.name == explicit or matched:
            selected.append(skill)
    return selected[:MAX_ACTIVE_SKILLS]


def catalog_prompt(catalog):
    if not catalog:
        return "No optional skills are installed."
    return "Available skills:\n" + "\n".join(
        f"- {skill.name}: {skill.description}" for skill in catalog.values()
    )
```

- [ ] **Step 4: Add the built-in application-builder skill**

Create `apps/skills/application-builder/SKILL.md`:

```markdown
---
name: application-builder
description: Build, cache, and launch a small offline application when the user asks for a calculator, timer, converter, tracker, dashboard, game, form, or similar local utility.
compatibility: AIOS offline browser applications
metadata:
  aios-triggers: "build an app,build a calculator,make an app,make a calculator,need an app,need a calculator,calculator,timer,converter,tracker,dashboard,game"
  aios-model: remote-preferred
allowed-tools: application
---

Use the application tool to deliver a working local utility.

1. Call `search` with the user's complete request before creating anything.
2. If a strong cached match exists, call `launch` and report that it was reused.
3. On a cache miss, call `create` with a short title and the complete request.
4. Produce one self-contained `index.html` with inline CSS and JavaScript. Do not reference remote resources, packages, fonts, APIs, or additional files.
5. Make the interface keyboard accessible, responsive, and immediately usable.
6. Call `write`, then `publish` with a concise summary and search keywords.
7. Call `launch`. Do not claim success unless launch returns `launched: true`.
8. If any tool fails, explain the specific failure and leave a valid published cache entry unchanged.
```

- [ ] **Step 5: Run skill tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_skills -v
```

Expected: all skill tests pass.

- [ ] **Step 6: Commit the skill catalog**

Run:

```powershell
git add apps\aios\skills.py apps\skills\application-builder\SKILL.md tests\test_skills.py
git commit -m "Add Agent Skills discovery" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Add the persistent application cache

**Files:**
- Create: `apps/aios/applications.py`
- Create: `tests/test_applications.py`

- [ ] **Step 1: Write failing cache and safety tests**

Create the store tests:

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from aios.applications import ApplicationStore


class ApplicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.launcher = Mock()
        self.store = ApplicationStore(self.root, launcher=self.launcher)

    def tearDown(self):
        self.temp.cleanup()

    def publish_calculator(self):
        created = self.store.act({
            "action": "create",
            "title": "Pocket Calculator",
            "request": "I need a calculator",
        })
        app_id = created["id"]
        self.store.act({
            "action": "write",
            "id": app_id,
            "html": "<!doctype html><title>Calculator</title><button>1</button>",
        })
        self.store.act({
            "action": "publish",
            "id": app_id,
            "summary": "A basic calculator.",
            "keywords": ["calculator", "math"],
        })
        return app_id

    def test_publish_search_and_launch(self):
        app_id = self.publish_calculator()
        result = self.store.act({"action": "search", "query": "calculator"})
        self.assertEqual(result["matches"][0]["id"], app_id)
        launched = self.store.act({"action": "launch", "id": app_id})
        self.assertEqual(launched, {"launched": True, "id": app_id, "title": "Pocket Calculator"})
        self.launcher.assert_called_once_with(self.root / app_id)

    def test_exact_request_outranks_keyword_overlap(self):
        first = self.publish_calculator()
        created = self.store.act({"action": "create", "title": "Math Pad", "request": "math helper"})
        self.store.act({"action": "write", "id": created["id"], "html": "<!doctype html><title>Math</title>"})
        self.store.act({
            "action": "publish", "id": created["id"],
            "summary": "Math helper.", "keywords": ["calculator"],
        })
        matches = self.store.act({"action": "search", "query": "I need a calculator"})["matches"]
        self.assertEqual(matches[0]["id"], first)

    def test_rejects_unknown_ids_symlinks_and_oversized_html(self):
        with self.assertRaises(ValueError):
            self.store.act({"action": "read", "id": "../escape"})
        created = self.store.act({"action": "create", "title": "Safe", "request": "safe app"})
        with self.assertRaises(ValueError):
            self.store.act({"action": "write", "id": created["id"], "html": "x" * (16 * 1024 + 1)})
        outside = self.root / "outside"
        outside.write_text("secret")
        index = self.root / created["id"] / "index.html"
        try:
            index.symlink_to(outside)
        except OSError:
            self.skipTest("This test environment cannot create symlinks.")
        with self.assertRaises(ValueError):
            self.store.act({"action": "read", "id": created["id"]})

    def test_launch_rejects_modified_published_content(self):
        app_id = self.publish_calculator()
        (self.root / app_id / "index.html").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.act({"action": "launch", "id": app_id})
        self.launcher.assert_not_called()
```

- [ ] **Step 2: Run the tests and confirm failure**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_applications.ApplicationStoreTests -v
```

Expected: `ModuleNotFoundError` for `aios.applications`.

- [ ] **Step 3: Implement the application tool and store**

Create `apps/aios/applications.py` around this complete public contract:

```python
"""Bounded cache and model-facing tool for self-contained offline applications."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import core

MAX_HTML = 16 * 1024
ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{8}$")
WORD = re.compile(r"[a-z0-9]+")
APPLICATION_TOOL = {"type": "function", "function": {
    "name": "application",
    "description": "Search, create, write, publish, and launch a cached offline AIOS application.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "create", "read", "write", "publish", "launch"]},
            "query": {"type": "string"},
            "id": {"type": "string"},
            "title": {"type": "string"},
            "request": {"type": "string"},
            "html": {"type": "string"},
            "summary": {"type": "string"},
            "keywords": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}}


def _normalized(value):
    if not isinstance(value, str):
        raise ValueError("Application text must be a string.")
    return " ".join(WORD.findall(value.casefold()))


def _slug(title):
    value = "-".join(WORD.findall(title.casefold()))[:40].strip("-")
    return value or "application"


def _atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ApplicationStore:
    def __init__(self, root=None, launcher=None):
        self.root = Path(root or (core.data_dir() / "applications"))
        if self.root.is_symlink():
            raise ValueError("Application cache directory cannot be a symlink.")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.launcher = launcher or self._launch

    def _folder(self, app_id):
        if not isinstance(app_id, str) or not ID.fullmatch(app_id):
            raise ValueError("Choose a valid application ID.")
        folder = self.root / app_id
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("Application was not found.")
        return folder

    def _json(self, path):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Application metadata is invalid.")
        return value

    def _manifest(self, folder):
        path = folder / "manifest.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("Application is not published.")
        return self._json(path)

    def _index(self, folder):
        path = folder / "index.html"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_HTML:
            raise ValueError("Application document is invalid.")
        return path

    def _launch(self, folder):
        subprocess.Popen(
            [sys.executable, "-m", "aios.app_runner", str(folder)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def act(self, arguments):
        if not isinstance(arguments, dict):
            raise ValueError("Application arguments must be an object.")
        action = arguments.get("action")
        if action == "search":
            query = _normalized(arguments.get("query", ""))
            if not query:
                raise ValueError("Enter an application search.")
            query_words = set(query.split())
            matches = []
            for folder in self.root.iterdir():
                try:
                    manifest = self._manifest(folder)
                    exact = manifest["request"] == query
                    words = set(manifest.get("keywords", [])) | set(_normalized(manifest["title"]).split())
                    score = 1000 if exact else len(query_words & words)
                    if score:
                        matches.append((score, manifest["updated_at"], {
                            "id": manifest["id"], "title": manifest["title"],
                            "summary": manifest["summary"], "exact": exact,
                        }))
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
            matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return {"matches": [item[2] for item in matches[:5]]}
        if action == "create":
            raw_title = arguments.get("title")
            title = raw_title.strip() if isinstance(raw_title, str) else ""
            request = _normalized(arguments.get("request", ""))
            if not title or len(title) > 100 or not request or len(request) > 2000:
                raise ValueError("Enter a title and source request.")
            app_id = f"{_slug(title)}-{uuid.uuid4().hex[:8]}"
            folder = self.root / app_id
            folder.mkdir(mode=0o700)
            core.write_json(folder / ".draft.json", {
                "id": app_id, "title": title, "request": request,
                "created_at": int(time.time()),
            })
            return {"id": app_id, "title": title}
        folder = self._folder(arguments.get("id"))
        if action == "write":
            html = arguments.get("html")
            if not isinstance(html, str) or not html.lstrip().lower().startswith("<!doctype html"):
                raise ValueError("Write one complete HTML document.")
            if len(html.encode("utf-8")) > MAX_HTML:
                raise ValueError("Application document is too large.")
            _atomic_text(folder / "index.html", html)
            return {"written": True, "bytes": len(html.encode("utf-8"))}
        if action == "read":
            return {"html": self._index(folder).read_text(encoding="utf-8")}
        if action == "publish":
            draft_path = folder / ".draft.json"
            if draft_path.is_symlink() or not draft_path.is_file():
                raise ValueError("Application draft is missing.")
            draft = self._json(draft_path)
            index = self._index(folder)
            raw_summary = arguments.get("summary")
            summary = raw_summary.strip() if isinstance(raw_summary, str) else ""
            keywords = arguments.get("keywords", [])
            if (
                not summary or len(summary) > 300 or not isinstance(keywords, list)
                or len(keywords) > 20
            ):
                raise ValueError("Enter an application summary and keywords.")
            clean_keywords = sorted({
                word for item in keywords if isinstance(item, str)
                for word in WORD.findall(item.casefold())
            })[:20]
            content = index.read_bytes()
            now = int(time.time())
            manifest = {
                **draft, "summary": summary, "keywords": clean_keywords,
                "entrypoint": "index.html", "sha256": hashlib.sha256(content).hexdigest(),
                "updated_at": now,
            }
            core.write_json(folder / "manifest.json", manifest)
            draft_path.unlink()
            return {"published": True, "id": manifest["id"], "sha256": manifest["sha256"]}
        if action == "launch":
            manifest = self._manifest(folder)
            digest = hashlib.sha256(self._index(folder).read_bytes()).hexdigest()
            if digest != manifest.get("sha256"):
                raise ValueError("Published application content changed.")
            self.launcher(folder)
            return {"launched": True, "id": manifest["id"], "title": manifest["title"]}
        raise ValueError("Unknown application action.")
```

- [ ] **Step 4: Run application store tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_applications.ApplicationStoreTests -v
```

Expected: all store tests pass.

- [ ] **Step 5: Commit the application cache**

Run:

```powershell
git add apps\aios\applications.py tests\test_applications.py
git commit -m "Add cached application store" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 3: Add the sandboxed application runner

**Files:**
- Create: `apps/aios/app_runner.py`
- Modify: `tests/test_applications.py`

- [ ] **Step 1: Add failing HTTP boundary tests**

Append tests that start the handler without launching Chromium:

```python
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from aios import app_runner


class ApplicationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            app_runner.handler_for("<!doctype html><script>document.body.textContent='ok'</script>"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_wrapper_uses_script_only_iframe_sandbox(self):
        with urllib.request.urlopen(self.base + "/") as response:
            body = response.read().decode()
            policy = response.headers["Content-Security-Policy"]
        self.assertIn('sandbox="allow-scripts"', body)
        self.assertIn('src="/app"', body)
        self.assertIn("frame-src 'self'", policy)

    def test_app_response_blocks_external_capabilities(self):
        with urllib.request.urlopen(self.base + "/app") as response:
            body = response.read().decode()
            policy = response.headers["Content-Security-Policy"]
        self.assertIn("document.body", body)
        self.assertIn("connect-src 'none'", policy)
        self.assertIn("form-action 'none'", policy)
        self.assertIn("base-uri 'none'", policy)

    def test_unknown_paths_do_not_expose_files(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/manifest.json")
        self.assertEqual(caught.exception.code, 404)
```

- [ ] **Step 2: Run the runner tests and confirm failure**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_applications.ApplicationRunnerTests -v
```

Expected: `ImportError` for `aios.app_runner`.

- [ ] **Step 3: Implement the trusted wrapper and Chromium lifecycle**

Create `apps/aios/app_runner.py`:

```python
"""Serve generated HTML inside a trusted sandboxed wrapper and Chromium app window."""
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .applications import MAX_HTML

WRAPPER_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; base-uri 'none'"
APP_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'none'; font-src 'none'; media-src 'none'; "
    "object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'"
)


def handler_for(document):
    wrapper = (
        "<!doctype html><meta charset=utf-8><title>AIOS Application</title>"
        "<style>html,body,iframe{width:100%;height:100%;margin:0;border:0;background:#fff}</style>"
        '<iframe sandbox="allow-scripts" src="/app" title="AIOS application"></iframe>'
    ).encode()
    application = document.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/":
                body, policy = wrapper, WRAPPER_CSP
            elif self.path == "/app":
                body, policy = application, APP_CSP
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", policy)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def load_document(folder):
    folder = Path(folder)
    manifest_path = folder / "manifest.json"
    index_path = folder / "index.html"
    if folder.is_symlink() or manifest_path.is_symlink() or index_path.is_symlink():
        raise ValueError("Application files are invalid.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Application manifest is invalid.")
    if index_path.stat().st_size > MAX_HTML:
        raise ValueError("Application document is too large.")
    content = index_path.read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest.get("sha256"):
        raise ValueError("Published application content changed.")
    return content.decode("utf-8")


def run(folder):
    document = load_document(folder)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(document))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        with tempfile.TemporaryDirectory(prefix="aios-app-") as profile:
            process = subprocess.Popen([
                "chromium", "--app=" + url, "--user-data-dir=" + profile,
                "--no-first-run", "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            process.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    try:
        run(sys.argv[1])
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        raise SystemExit(1)
```

- [ ] **Step 4: Run all application tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_applications -v
```

Expected: all application store and runner tests pass.

- [ ] **Step 5: Commit the runner**

Run:

```powershell
git add apps\aios\app_runner.py tests\test_applications.py
git commit -m "Sandbox generated application windows" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 4: Add the stdio MCP client

**Files:**
- Create: `apps/aios/mcp.py`
- Create: `tests/fixtures/mcp_server.py`
- Create: `tests/test_mcp.py`

- [ ] **Step 1: Create a deterministic MCP fixture**

Create `tests/fixtures/mcp_server.py` that logs requests and supports normal,
paginated, changed-list, server-request, oversized, malformed, and timeout
scenarios selected by `AIOS_MCP_SCENARIO`:

```python
import json
import os
import sys
import time

scenario = os.environ.get("AIOS_MCP_SCENARIO", "")
log_path = os.environ.get("AIOS_MCP_LOG")


def send(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    value = json.loads(line)
    if log_path:
        with open(log_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")
    method = value.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": value["id"], "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }})
    elif method == "notifications/initialized":
        if scenario == "changed":
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    elif method == "tools/list":
        if scenario == "malformed":
            print("not-json", flush=True)
            continue
        cursor = value.get("params", {}).get("cursor")
        tools = [{"name": "echo", "description": "Echo text.", "inputSchema": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        }}]
        result = {"tools": tools}
        if scenario == "paginate" and not cursor:
            result["nextCursor"] = "second"
        elif scenario == "paginate":
            result["tools"] = [{"name": "second", "description": "Second tool.", "inputSchema": {"type": "object"}}]
        send({"jsonrpc": "2.0", "id": value["id"], "result": result})
        if scenario == "changed":
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    elif method == "tools/call":
        if scenario == "timeout":
            time.sleep(60)
        elif scenario == "server-request":
            send({"jsonrpc": "2.0", "id": "server-call", "method": "roots/list", "params": {}})
            text = value["params"]["arguments"]["text"]
            send({"jsonrpc": "2.0", "id": value["id"], "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }})
        elif scenario == "oversized":
            send({"jsonrpc": "2.0", "id": value["id"], "result": {
                "content": [{"type": "text", "text": "x" * 70000}],
            }})
        else:
            text = value["params"]["arguments"]["text"]
            send({"jsonrpc": "2.0", "id": value["id"], "result": {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"echo": text},
                "isError": False,
            }})
```

- [ ] **Step 2: Write failing lifecycle and allowlist tests**

Create `tests/test_mcp.py`:

```python
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aios.mcp import McpRegistry


class McpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "requests.jsonl"
        self.fixture = str(Path(__file__).parent / "fixtures" / "mcp_server.py")

    def tearDown(self):
        self.temp.cleanup()

    def config(self, tools, scenario=""):
        path = self.root / "mcp.json"
        path.write_text(json.dumps({"servers": {"fixture": {
            "command": sys.executable,
            "args": [self.fixture],
            "env": {"AIOS_MCP_LOG": str(self.log), "AIOS_MCP_SCENARIO": scenario},
            "tools": tools,
        }}}), encoding="utf-8")
        return path

    def test_initialize_list_allowlist_call_and_cleanup(self):
        registry = McpRegistry(self.config(["echo"]))
        definitions, warnings = registry.definitions()
        self.assertEqual(warnings, [])
        self.assertEqual([tool["function"]["name"] for tool in definitions], ["mcp_fixture_echo"])
        result = registry.call("mcp_fixture_echo", {"text": "hello"})
        self.assertEqual(result["structured"], {"echo": "hello"})
        registry.close()
        methods = [json.loads(line)["method"] for line in self.log.read_text().splitlines()]
        self.assertEqual(methods[:3], ["initialize", "notifications/initialized", "tools/list"])
        self.assertIn("tools/call", methods)

    def test_star_allowlist_paginates(self):
        registry = McpRegistry(self.config(["*"], "paginate"))
        definitions, _ = registry.definitions()
        self.assertEqual(
            [tool["function"]["name"] for tool in definitions],
            ["mcp_fixture_echo", "mcp_fixture_second"],
        )
        registry.close()

    def test_unapproved_tool_is_not_callable(self):
        registry = McpRegistry(self.config([]))
        definitions, _ = registry.definitions()
        self.assertEqual(definitions, [])
        with self.assertRaises(ValueError):
            registry.call("mcp_fixture_echo", {"text": "blocked"})
        registry.close()

    def test_server_requests_are_rejected_and_list_changes_refresh(self):
        registry = McpRegistry(self.config(["echo"], "server-request"))
        registry.definitions()
        self.assertEqual(registry.call("mcp_fixture_echo", {"text": "hello"})["text"], "hello")
        registry.close()
        responses = [json.loads(line) for line in self.log.read_text().splitlines()]
        rejection = next(item for item in responses if item.get("id") == "server-call")
        self.assertEqual(rejection["error"]["code"], -32601)

        self.log.unlink(missing_ok=True)
        changed = McpRegistry(self.config(["echo"], "changed"))
        changed.definitions()
        time.sleep(0.05)
        changed.definitions()
        changed.close()
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertGreaterEqual(
            sum(item.get("method") == "tools/list" for item in requests), 2
        )

    def test_timeout_oversized_and_malformed_output_are_sanitized(self):
        for scenario in ("timeout", "oversized", "malformed"):
            registry = McpRegistry(self.config(["echo"], scenario), timeout=0.1)
            if scenario != "malformed":
                registry.definitions()
                with self.assertRaises(RuntimeError):
                    registry.call("mcp_fixture_echo", {"text": "hello"})
            else:
                definitions, warnings = registry.definitions()
                self.assertEqual(definitions, [])
                self.assertEqual(len(warnings), 1)
            registry.close()
```

- [ ] **Step 3: Run MCP tests and confirm failure**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_mcp -v
```

Expected: `ModuleNotFoundError` for `aios.mcp`.

- [ ] **Step 4: Implement bounded JSON-RPC and registry mapping**

Create `apps/aios/mcp.py` with:

```python
"""Allowlisted MCP 2025-06-18 stdio tools with bounded JSON-RPC transport."""
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from . import core

PROTOCOL = "2025-06-18"
MAX_MESSAGE = 4 * 1024 * 1024
MAX_RESULT = 64 * 1024
SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


def _tool_name(server, tool):
    left = SAFE_NAME.sub("_", server).strip("_").lower()
    right = SAFE_NAME.sub("_", tool).strip("_").lower()
    value = f"mcp_{left}_{right}"
    if not left or not right or len(value) > 64:
        raise ValueError("MCP tool name is invalid.")
    return value


def _environment(extra):
    inherited = {}
    for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL"):
        if key in os.environ:
            inherited[key] = os.environ[key]
    if not isinstance(extra, dict):
        raise ValueError("MCP environment must be an object.")
    for key, value in extra.items():
        if not isinstance(key, str) or not isinstance(value, str) or "\0" in key + value:
            raise ValueError("MCP environment values must be strings.")
        inherited[key] = value
    return inherited


class McpClient:
    def __init__(self, name, settings, timeout=30):
        self.name = name
        self.settings = settings
        self.timeout = timeout
        self.process = None
        self.events = queue.Queue(maxsize=1024)
        self.sequence = 0
        self.tools_stale = True
        self.cached_tools = []

    def start(self):
        if self.process:
            return
        command = self.settings.get("command")
        args = self.settings.get("args", [])
        if not isinstance(command, str) or not command or not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("MCP command and arguments are invalid.")
        try:
            self.process = subprocess.Popen(
                [command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=_environment(self.settings.get("env", {})),
            )
            threading.Thread(target=self._read, daemon=True).start()
            result = self.request("initialize", {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "aios", "version": "0.1"},
            })
            if result.get("protocolVersion") != PROTOCOL or "tools" not in result.get("capabilities", {}):
                raise RuntimeError("MCP server does not support the required protocol and tools.")
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_MESSAGE + 1)
                if not line or len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                    break
                value = json.loads(line)
                if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                    break
                if value.get("method") == "notifications/tools/list_changed":
                    self.tools_stale = True
                self.events.put(value)
        except (OSError, ValueError):
            pass
        finally:
            self.events.put(None)

    def send(self, value):
        try:
            self.process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise RuntimeError(f"MCP server {self.name!r} closed unexpectedly.") from None

    def _receive(self, timeout):
        try:
            value = self.events.get(timeout=max(timeout, 0))
        except queue.Empty:
            raise RuntimeError(f"MCP server {self.name!r} timed out.") from None
        if value is None:
            raise RuntimeError(f"MCP server {self.name!r} closed unexpectedly.")
        return value

    def request(self, method, params=None):
        self.start() if method != "initialize" and not self.process else None
        self.sequence += 1
        request_id = self.sequence
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            value = self._receive(deadline - time.monotonic())
            if value.get("id") == request_id and "method" not in value:
                if "error" in value:
                    raise RuntimeError(f"MCP server {self.name!r} rejected {method}.")
                result = value.get("result", {})
                if not isinstance(result, dict):
                    raise RuntimeError(f"MCP server {self.name!r} returned an invalid result.")
                return result
            if "id" in value and "method" in value:
                self.send({"jsonrpc": "2.0", "id": value["id"], "error": {
                    "code": -32601, "message": "This client capability is not available.",
                }})
            elif value.get("method") == "notifications/tools/list_changed":
                self.tools_stale = True
            elif "method" in value:
                continue
            else:
                raise RuntimeError(f"MCP server {self.name!r} returned an unexpected response.")

    def tools(self):
        self.start()
        if not self.tools_stale:
            return self.cached_tools
        found, cursor = [], None
        for _ in range(20):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools", [])
            if not isinstance(page, list):
                raise RuntimeError(f"MCP server {self.name!r} returned invalid tools.")
            found.extend(page)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        if cursor or len(found) > 256:
            raise RuntimeError(f"MCP server {self.name!r} returned too many tools.")
        self.cached_tools, self.tools_stale = found, False
        return found

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        content, structured = [], result.get("structuredContent")
        for item in result.get("content", []):
            if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
                raise RuntimeError(f"MCP server {self.name!r} returned unsupported content.")
            content.append(item["text"])
        value = {"text": "\n".join(content), "structured": structured, "is_error": bool(result.get("isError"))}
        if len(json.dumps(value).encode()) > MAX_RESULT:
            raise RuntimeError(f"MCP server {self.name!r} returned too much data.")
        return value

    def close(self):
        if not self.process:
            return
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        self.process = None


class McpRegistry:
    def __init__(self, path=None, timeout=30):
        self.path = Path(path or (core.config_dir() / "mcp.json"))
        self.timeout = timeout
        self.clients = {}
        self.mapping = {}

    def _settings(self):
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        servers = value.get("servers") if isinstance(value, dict) else None
        if not isinstance(servers, dict):
            raise ValueError("MCP configuration must contain a servers object.")
        return servers

    def definitions(self):
        definitions, warnings, names = [], [], set()
        self.mapping = {}
        try:
            settings = self._settings()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return [], [str(error)]
        for server_name, server in settings.items():
            try:
                if (
                    not isinstance(server, dict) or not isinstance(server.get("tools"), list)
                    or not all(isinstance(name, str) for name in server["tools"])
                ):
                    raise ValueError("MCP server requires a tools allowlist.")
                allowed = server["tools"]
                if not allowed:
                    continue
                client = self.clients.setdefault(server_name, McpClient(server_name, server, self.timeout))
                server_definitions, server_mapping, server_names = [], {}, set()
                for tool in client.tools():
                    original = tool.get("name")
                    if not isinstance(original, str) or not original:
                        raise ValueError("MCP server returned a tool without a valid name.")
                    if "*" not in allowed and original not in allowed:
                        continue
                    exposed = _tool_name(server_name, original)
                    if exposed in names or exposed in server_names:
                        raise ValueError("MCP tool name conflicts with another tool.")
                    schema = tool.get("inputSchema", {"type": "object"})
                    if not isinstance(schema, dict):
                        raise ValueError("MCP server returned an invalid input schema.")
                    definition = {"type": "function", "function": {
                        "name": exposed,
                        "description": f"MCP {server_name}: {tool.get('description', original)}",
                        "parameters": schema,
                    }}
                    server_definitions.append(definition)
                    server_mapping[exposed] = (client, original)
                    server_names.add(exposed)
                definitions.extend(server_definitions)
                self.mapping.update(server_mapping)
                names.update(server_names)
            except (OSError, RuntimeError, ValueError) as error:
                warnings.append(f"MCP server {server_name!r} was skipped: {error}")
        return definitions, warnings

    def call(self, exposed, arguments):
        if exposed not in self.mapping:
            raise ValueError("MCP tool is not approved.")
        client, original = self.mapping[exposed]
        return client.call(original, arguments)

    def close(self):
        for client in self.clients.values():
            client.close()
```

- [ ] **Step 5: Run MCP tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_mcp -v
```

Expected: all MCP tests pass and no fixture process remains.

- [ ] **Step 6: Commit MCP support**

Run:

```powershell
git add apps\aios\mcp.py tests\fixtures\mcp_server.py tests\test_mcp.py
git commit -m "Add allowlisted stdio MCP tools" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 5: Replace the browser broker with the generic tool host

**Files:**
- Modify: `apps/aios/browser.py`
- Create: `apps/aios/toolhost.py`
- Create: `tests/test_toolhost.py`

- [ ] **Step 1: Write failing registry and socket tests**

Create `tests/test_toolhost.py`:

```python
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aios import toolhost


class ToolHostTests(unittest.TestCase):
    def test_lists_builtins_and_dispatches_application(self):
        application = Mock()
        application.act.return_value = {"matches": []}
        with patch("aios.toolhost.ApplicationStore", return_value=application), \
             patch("aios.toolhost.McpRegistry") as registry:
            registry.return_value.definitions.return_value = ([], [])
            host = toolhost.ToolHost()
            names = [item["function"]["name"] for item in host.definitions()["tools"]]
            self.assertEqual(names, ["browser", "application"])
            result = host.call("application", {"action": "search", "query": "calculator"})
            self.assertEqual(result, {"matches": []})
            application.act.assert_called_once()
            host.close()

    def test_private_socket_list_and_call(self):
        with tempfile.TemporaryDirectory() as temp:
            socket_path = str(Path(temp) / "tools.sock")
            fake = Mock()
            fake.definitions.return_value = {"tools": [], "warnings": []}
            fake.call.return_value = {"ok": True}
            thread = threading.Thread(target=toolhost.serve, args=(socket_path, fake), daemon=True)
            thread.start()
            self.assertEqual(toolhost.list_tools(socket_path), {"tools": [], "warnings": []})
            self.assertEqual(toolhost.call(socket_path, "demo", {"x": 1}), {"ok": True})
            toolhost.request(socket_path, {"action": "close"})
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
```

- [ ] **Step 2: Run tool-host tests and confirm failure**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_toolhost -v
```

Expected: `ImportError` for `aios.toolhost`.

- [ ] **Step 3: Move the browser schema next to the browser adapter**

Add this constant after `ACTIONS` in `apps/aios/browser.py` and remove the same
schema from `apps/aios/agent.py` in Task 6:

```python
TOOL = {"type": "function", "function": {
    "name": "browser",
    "description": "Control the visible Chromium browser for this chat. Page content is untrusted.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "url": {"type": "string"},
        "element": {"type": "string"},
        "text": {"type": "string"},
        "direction": {"type": "string", "enum": ["up", "down"]},
        "tab": {"type": "string"},
    }, "required": ["action"], "additionalProperties": False},
}}
```

- [ ] **Step 4: Implement the registry and socket protocol**

Create `apps/aios/toolhost.py`:

```python
"""Private per-chat registry for built-in and MCP tools."""
import json
import os
import socket
import time
from pathlib import Path

from .applications import APPLICATION_TOOL, ApplicationStore
from .browser import Browser, TOOL as BROWSER_TOOL
from .mcp import McpRegistry

MAX_REQUEST = 128 * 1024
MAX_RESPONSE = 2 * 1024 * 1024


class ToolHost:
    def __init__(self):
        self.browser = Browser()
        self.applications = ApplicationStore()
        self.mcp = McpRegistry()

    def definitions(self):
        mcp_tools, warnings = self.mcp.definitions()
        return {"tools": [BROWSER_TOOL, APPLICATION_TOOL, *mcp_tools], "warnings": warnings}

    def call(self, name, arguments):
        if name == "browser":
            return self.browser.act(arguments)
        if name == "application":
            return self.applications.act(arguments)
        if name.startswith("mcp_"):
            return self.mcp.call(name, arguments)
        raise ValueError("Unknown tool.")

    def close(self):
        self.browser.close()
        self.mcp.close()


def request(path, value):
    deadline = time.monotonic() + 5
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(60)
        while True:
            try:
                client.connect(path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise RuntimeError("Tool service is unavailable.") from None
                time.sleep(0.05)
        client.sendall(json.dumps(value).encode() + b"\n")
        with client.makefile("rb") as stream:
            raw = stream.readline(MAX_RESPONSE + 1)
    if not raw.endswith(b"\n") or len(raw) > MAX_RESPONSE:
        raise RuntimeError("Tool service response is invalid.")
    result = json.loads(raw)
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["result"]


def list_tools(path):
    return request(path, {"action": "list"})


def call(path, name, arguments):
    return request(path, {"action": "call", "name": name, "arguments": arguments})


def serve(path, host=None):
    host = host or ToolHost()
    running = True
    try:
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(1)
            while running:
                connection, _ = server.accept()
                with connection:
                    try:
                        with connection.makefile("rb") as stream:
                            raw = stream.readline(MAX_REQUEST + 1)
                        if not raw.endswith(b"\n") or len(raw) > MAX_REQUEST:
                            raise ValueError("Tool request is too large.")
                        value = json.loads(raw)
                        action = value.get("action")
                        if action == "list":
                            result = host.definitions()
                        elif action == "call":
                            result = host.call(value.get("name"), value.get("arguments"))
                        elif action == "close":
                            result, running = {"closed": True}, False
                        else:
                            raise ValueError("Unknown tool-host action.")
                        response = {"result": result}
                    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
                        response = {"error": str(error)}
                    encoded = json.dumps(response).encode() + b"\n"
                    if len(encoded) > MAX_RESPONSE:
                        encoded = json.dumps({"error": "Tool response is too large."}).encode() + b"\n"
                    connection.sendall(encoded)
    finally:
        host.close()
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    import sys
    serve(sys.argv[1])
```

- [ ] **Step 5: Run tool-host and existing browser tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_toolhost tests.test_browser_agent -v
```

Expected: all tool-host and existing browser-agent tests pass.

- [ ] **Step 6: Commit the generic host**

Run:

```powershell
git add apps\aios\browser.py apps\aios\toolhost.py tests\test_toolhost.py
git commit -m "Add generic per-chat tool host" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 6: Generalize the agent loop and add skill activation

**Files:**
- Modify: `apps/aios/agent.py`
- Modify: `tests/test_browser_agent.py`

- [ ] **Step 1: Replace browser-only tests with generic agent-session tests**

Keep the existing fragmented-call, incomplete-call, prose-non-execution, and
round-limit cases, but mock `toolhost.list_tools` and `toolhost.call`. Add:

```python
from aios import agent, skills
```

Change scripted request helpers from `def request(route, body):` to
`def request(route, body, **_):` because the generalized loop supplies the
selected transport profile as a keyword argument.

```python
@patch("aios.agent.skills.load_skills")
@patch("aios.agent.toolhost.list_tools")
def test_application_skill_filters_tools_and_marks_remote_preference(list_tools, load_skills):
    load_skills.return_value = {
        "application-builder": skills.Skill(
            "application-builder", "Build apps.", "Search before building.",
            ("application",), ("calculator",), "remote-preferred",
        )
    }
    list_tools.return_value = {"tools": [
        {"type": "function", "function": {"name": "browser", "description": "browser", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "application", "description": "app", "parameters": {"type": "object"}}},
    ], "warnings": []}
    session = agent.AgentSession([{"role": "user", "content": "I need a calculator"}], "tools.sock")
    self.assertTrue(session.remote_preferred)
    self.assertEqual(
        [tool["function"]["name"] for tool in session.tools()],
        ["activate_skill", "application"],
    )
    self.assertIn("Search before building.", session.system_prompt())


@patch("aios.agent.toolhost.call", return_value={"matches": []})
def test_dispatches_only_advertised_tool(call):
    with patch("aios.agent.toolhost.list_tools", return_value={"tools": [
        {"type": "function", "function": {
            "name": "application", "description": "app",
            "parameters": {"type": "object"},
        }},
    ], "warnings": []}):
        session = agent.AgentSession(
            [{"role": "user", "content": "hello"}], "tools.sock", catalog={}
        )
    session.advertised = {"application"}
    self.assertEqual(
        session.dispatch("application", {"action": "search", "query": "calculator"}),
        {"matches": []},
    )
    with self.assertRaises(ValueError):
        session.dispatch("browser", {"action": "open", "url": "https://example.com"})
    call.assert_called_once()
```

Add a scripted application flow where the model emits `search`, `create`,
`write`, `publish`, and `launch` calls and assert all five structured calls are
forwarded in order. The final response must be plain text from the model, not a
tool result fabricated by AIOS.

- [ ] **Step 2: Run the updated tests and confirm failures**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_browser_agent -v
```

Expected: failures for missing `AgentSession` and old browser-only dispatch.

- [ ] **Step 3: Implement `AgentSession` and the generic OpenAI loop**

Replace `apps/aios/agent.py` with a provider-neutral session. Preserve the
existing `POLICY` safety statements and add skill/tool untrusted-data language:

```python
ACTIVATE_TOOL = {"type": "function", "function": {
    "name": "activate_skill",
    "description": "Load one installed skill's instructions by exact name.",
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string"},
    }, "required": ["name"], "additionalProperties": False},
}}


class AgentSession:
    def __init__(self, messages, tool_socket, catalog=None):
        if not messages or any(
            message.get("role") not in ("user", "assistant", "system")
            or not isinstance(message.get("content"), str)
            for message in messages
        ):
            raise ValueError("Invalid conversation.")
        self.messages = [{"role": item["role"], "content": item["content"]} for item in messages]
        self.tool_socket = tool_socket
        loaded = skills.load_skills(include_warnings=True) if catalog is None else (catalog, [])
        self.catalog, self.warnings = loaded
        self.active = {
            skill.name: skill
            for skill in skills.initial_skills(self.catalog, self.messages[-1]["content"])
        }
        listed = toolhost.list_tools(tool_socket)
        valid_tools = [
            item for item in listed.get("tools", [])
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
            and isinstance(item["function"].get("name"), str)
        ]
        if len(valid_tools) > 63:
            raise RuntimeError("Too many tools are configured.")
        self.host_tools = {item["function"]["name"]: item for item in valid_tools}
        self.warnings.extend(listed.get("warnings", []))
        self.advertised = set()

    @property
    def remote_preferred(self):
        return any(skill.model == "remote-preferred" for skill in self.active.values())

    def system_prompt(self):
        active = "\n\n".join(
            f"Activated skill {skill.name}:\n{skill.instructions}"
            for skill in self.active.values()
        )
        warnings = "\n".join(f"- {warning}" for warning in self.warnings)
        return "\n\n".join(part for part in (
            POLICY,
            skills.catalog_prompt(self.catalog),
            active,
            "Capability warnings:\n" + warnings if warnings else "",
        ) if part)

    def tools(self):
        allowed = {
            name for skill in self.active.values() for name in skill.allowed_tools
        }
        selected = self.host_tools.values() if not allowed else (
            tool for name, tool in self.host_tools.items() if name in allowed
        )
        tools = [ACTIVATE_TOOL, *selected]
        self.advertised = {tool["function"]["name"] for tool in tools}
        return tools

    def dispatch(self, name, arguments):
        if name not in self.advertised:
            raise ValueError("The model requested an unavailable tool.")
        if name == "activate_skill":
            requested = arguments.get("name") if isinstance(arguments, dict) else None
            skill = self.catalog.get(requested)
            if not skill:
                raise ValueError("Skill is not installed.")
            if requested not in self.active and len(self.active) >= skills.MAX_ACTIVE_SKILLS:
                raise ValueError("Too many skills are active.")
            self.active[requested] = skill
            return {"activated": requested, "instructions": skill.instructions}
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        result = toolhost.call(self.tool_socket, name, arguments)
        if len(json.dumps(result).encode("utf-8")) > 64 * 1024:
            raise RuntimeError("Tool returned too much data.")
        return result

    def progress(self, name, arguments):
        action = arguments.get("action") if isinstance(arguments, dict) else None
        label = name.replace("_", " ").title()
        return label + (" · " + action if isinstance(action, str) else "")
```

Implement the OpenAI-compatible loop:

```python
def openai_chat(session, profile="current"):
    history = [{"role": "system", "content": session.system_prompt()}, *session.messages]
    for _ in range(8):
        calls, content, finish = {}, "", None
        tools = session.tools()
        history[0]["content"] = session.system_prompt()
        body = {
            "model": core.model_name(profile),
            "messages": history,
            "tools": tools,
            "tool_choice": "auto",
            "stream": True,
        }
        with core.request("/chat/completions", body, profile=profile) as response:
            for event in core.sse_events(response):
                if event == "[DONE]":
                    break
                value = json.loads(event)
                if value.get("error"):
                    raise RuntimeError("The model could not complete this response.")
                for choice in value.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta", {})
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                    token = delta.get("content")
                    if isinstance(token, str):
                        content += token
                        yield {"type": "token", "text": token}
                    for part in delta.get("tool_calls", []):
                        index = part.get("index", 0)
                        if not isinstance(index, int) or not 0 <= index < 4:
                            raise RuntimeError("The model requested too many tools at once.")
                        entry = calls.setdefault(index, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        entry["id"] += part.get("id", "")
                        function = part.get("function", {})
                        for key in ("name", "arguments"):
                            fragment = function.get(key, "")
                            if not isinstance(fragment, str):
                                raise RuntimeError("The model tool request was invalid.")
                            entry["function"][key] += fragment
                        if len(json.dumps(entry).encode("utf-8")) > 24 * 1024:
                            raise RuntimeError("The model tool request was too large.")
        if calls:
            if finish != "tool_calls":
                raise RuntimeError("The model tool request was incomplete; no action was taken.")
            ordered = [calls[index] for index in sorted(calls)]
            if any(
                not call["id"] or call["function"]["name"] not in session.advertised
                for call in ordered
            ):
                raise RuntimeError("The model requested an unsupported tool.")
            history.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": ordered,
            })
            for entry in ordered:
                try:
                    arguments = json.loads(entry["function"]["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object.")
                    name = entry["function"]["name"]
                    yield {"type": "progress", "text": session.progress(name, arguments)}
                    result = session.dispatch(name, arguments)
                except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as error:
                    result = {"error": str(error)}
                history.append({
                    "role": "tool",
                    "tool_call_id": entry["id"],
                    "content": json.dumps(result),
                })
            prior_results = [item for item in history if item["role"] == "tool"][:-2]
            for item in prior_results:
                item["content"] = '{"previous_tool_result_omitted":true}'
            if content:
                yield {"type": "token", "text": "\n\n"}
        elif finish:
            return
        else:
            raise RuntimeError("The connection ended before the reply completed.")
    raise RuntimeError("Agent tool limit reached. Send another message to continue.")
```

Expose:

```python
def chat(messages, tool_socket):
    session = AgentSession(messages, tool_socket)
    provider, profile = select_provider(session, core.load_config())
    if provider == "chatgpt":
        from .subscription import chat as subscription_chat
        yield from subscription_chat(messages, session=session)
    else:
        yield from openai_chat(session, profile)
```

Add this initial routing helper; Task 7 extends it without changing callers:

```python
def select_provider(session, config):
    current = config["mode"]
    return (current, None) if current == "chatgpt" else (current, "current")
```

After the generic agent tests pass, delete the obsolete socket client, `serve`
function, and `__main__` block from `apps/aios/browser.py`. Remove imports used
only by that old broker while retaining imports required by `Browser`.

- [ ] **Step 4: Run generalized agent tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_browser_agent tests.test_skills tests.test_toolhost -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit the generic agent loop**

Run:

```powershell
git add apps\aios\agent.py tests\test_browser_agent.py
git commit -m "Generalize structured agent tools" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 7: Add explicit agent provider routing

**Files:**
- Modify: `apps/aios/core.py`
- Modify: `apps/aios/agent.py`
- Modify: `apps/aios/cli.py`
- Modify: `tests/test_core.py`
- Modify: `tests/test_browser_agent.py`

- [ ] **Step 1: Add failing configuration and routing tests**

Add to `tests/test_core.py`:

```python
def test_agent_provider_preserves_secret_and_requires_https(self):
    core.save_config({
        "agent_mode": "remote",
        "agent_url": "https://agent.example/v1",
        "agent_model": "coding-model",
        "agent_api_key": "agent-secret",
    })
    core.save_config({"theme_color": "teal"})
    config = core.load_config()
    self.assertEqual(config["agent_api_key"], "agent-secret")
    core.save_config({"agent_url": "https://other.example/v1"})
    self.assertEqual(core.load_config()["agent_api_key"], "")
    with self.assertRaises(ValueError):
        core.save_config({"agent_url": "http://agent.example/v1"})


def test_agent_request_uses_separate_endpoint_and_key(self):
    core.save_config({
        "mode": "local",
        "agent_mode": "remote",
        "agent_url": "https://agent.example/v1",
        "agent_model": "coding-model",
        "agent_api_key": "agent-secret",
    })
    with patch.object(core.urllib.request, "build_opener") as build_opener:
        opener = build_opener.return_value
        opener.open.side_effect = OSError("stop")
        with self.assertRaises(RuntimeError):
            core.request("/chat/completions", {}, profile="agent")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://agent.example/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer agent-secret")
    self.assertEqual(core.model_name("agent"), "coding-model")
```

Add routing assertions to `tests/test_browser_agent.py`:

```python
def test_remote_preferred_routes_only_when_skill_is_active(self):
    config = {"mode": "local", "agent_mode": "remote"}
    current = SimpleNamespace(remote_preferred=False)
    remote = SimpleNamespace(remote_preferred=True)
    self.assertEqual(agent.select_provider(current, config), ("local", "current"))
    self.assertEqual(agent.select_provider(remote, config), ("remote", "agent"))
    config["agent_mode"] = "chatgpt"
    self.assertEqual(agent.select_provider(remote, config), ("chatgpt", None))
```

Add `from types import SimpleNamespace` to `tests/test_browser_agent.py`.

- [ ] **Step 2: Run targeted tests and confirm failure**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_core tests.test_browser_agent -v
```

Expected: missing agent configuration fields, `profile`, `model_name`, and
routing behavior.

- [ ] **Step 3: Extend configuration and transport selection**

In `core.load_config()`, add:

```python
"agent_mode": "current",
"agent_url": "",
"agent_model": "",
"agent_api_key": "",
```

Add all four fields to `defaults_keys()`. In `save_config()`:

```python
if (
    "agent_url" in values
    and values["agent_url"].rstrip("/") != config["agent_url"].rstrip("/")
    and "agent_api_key" not in values
):
    config["agent_api_key"] = ""
if config["agent_mode"] not in ("current", "chatgpt", "remote"):
    raise ValueError("Choose the current model, ChatGPT, or a remote agent service.")
if config["agent_url"]:
    config["agent_url"] = validate_url(str(config["agent_url"]))
if config["agent_mode"] == "remote" and (
    not config["agent_url"] or not str(config["agent_model"]).strip()
):
    raise ValueError("Enter an agent service URL and model ID.")
```

Refactor the transport helpers:

```python
def model_name(profile="current"):
    config = load_config()
    if profile == "agent":
        if config["agent_mode"] != "remote":
            raise ValueError("A remote agent service is not selected.")
        return config["agent_model"]
    return "local" if config["mode"] == "local" else config["model"]


def request(route, body=None, timeout=90, profile="current"):
    config = load_config()
    if profile == "agent":
        if config["agent_mode"] != "remote":
            raise ValueError("A remote agent service is not selected.")
        base = validate_url(config["agent_url"])
        key = config["agent_api_key"]
    else:
        if config["mode"] == "chatgpt":
            raise ValueError("ChatGPT subscriptions use the subscription connection, not an API endpoint.")
        base = "http://127.0.0.1:8080/v1" if config["mode"] == "local" else validate_url(config["url"])
        key = config["api_key"] if config["mode"] == "remote" else ""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(
        base + route,
        headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        return urllib.request.build_opener(NoRedirect).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        reasons = {
            401: "Authentication failed. Check your API key.",
            403: "This model is not permitted.",
            404: "Endpoint or model not found. Check the URL and model ID.",
            429: "The provider is busy or rate limited. Try again shortly.",
            503: "The model is loading or unavailable. Try again shortly.",
        }
        raise RuntimeError(
            reasons.get(exc.code, f"The model endpoint returned HTTP {exc.code}.")
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError(
            "Cannot reach the model. Check your connection and model service."
        ) from None
```

Update `core.chat()` to call `model_name()` and the default request profile.

- [ ] **Step 4: Implement the routing matrix**

In `apps/aios/agent.py`:

```python
def select_provider(session, config):
    current = config["mode"]
    if not session.remote_preferred or config.get("agent_mode", "current") == "current":
        return (current, None) if current == "chatgpt" else (current, "current")
    if config["agent_mode"] == "chatgpt":
        return "chatgpt", None
    if config["agent_mode"] == "remote":
        if not config.get("agent_url") or not config.get("agent_model"):
            raise ValueError("Configure the remote agent service in AI models settings.")
        return "remote", "agent"
    raise ValueError("Choose an agent provider in AI models settings.")
```

This function must run before the first provider request. It must not switch
providers for ordinary browser chat or for skills whose model preference is
`current`.

- [ ] **Step 5: Add CLI configuration flags**

In `apps/aios/cli.py`, extend `configure`:

```python
configure.add_argument("--agent-mode", choices=("current", "chatgpt", "remote"))
configure.add_argument("--agent-url")
configure.add_argument("--agent-model")
configure.add_argument("--ask-agent-key", action="store_true")
```

Include `agent_mode`, `agent_url`, and `agent_model` in the saved value names.
When `--ask-agent-key` is supplied:

```python
values["agent_api_key"] = getpass.getpass("Agent API key: ")
```

- [ ] **Step 6: Run provider tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_core tests.test_browser_agent -v
```

Expected: all core and agent tests pass.

- [ ] **Step 7: Commit provider routing**

Run:

```powershell
git add apps\aios\core.py apps\aios\agent.py apps\aios\cli.py tests\test_core.py tests\test_browser_agent.py
git commit -m "Route skills to configured agent models" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 8: Supply generic tools through ChatGPT subscription

**Files:**
- Modify: `apps/aios/subscription.py`
- Modify: `tests/fixtures/codex_server.py`
- Modify: `tests/test_subscription.py`

- [ ] **Step 1: Add failing generic dynamic-tool tests**

Extend the Codex fixture with an `application` scenario that requests:

```python
send({"id": "tool-request", "method": "item/tool/call", "params": {
    "threadId": thread_id,
    "tool": "application",
    "arguments": {"action": "search", "query": "calculator"},
}})
```

Add:

```python
from unittest.mock import Mock, patch


def test_generic_dynamic_tool_uses_shared_agent_session(self):
    os.environ["AIOS_FAKE_SCENARIO"] = "application"
    session = Mock()
    session.system_prompt.return_value = "AIOS policy"
    session.codex_tools.return_value = [{
        "type": "function", "name": "application",
        "description": "Applications", "inputSchema": {"type": "object"},
    }]
    session.dispatch.return_value = {"matches": []}
    session.progress.return_value = "Application · search"
    events = list(subscription.chat(
        [{"role": "user", "content": "I need a calculator"}],
        session=session,
    ))
    session.dispatch.assert_called_once_with(
        "application", {"action": "search", "query": "calculator"},
    )
    self.assertIn({"type": "progress", "text": "Application · search"}, events)
```

Retain the existing unsupported-tool test and change it to assert that the
shared session rejects the call and no external adapter runs.
Replace the existing browser-socket bridge test with a shared session whose
`codex_tools()` advertises `browser` and whose `dispatch()` assertion verifies
the browser arguments.

- [ ] **Step 2: Run subscription tests and confirm failure**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_subscription -v
```

Expected: `subscription.chat()` does not accept `session`.

- [ ] **Step 3: Convert AgentSession tools to Codex dynamic tools**

Add to `AgentSession`:

```python
def codex_tools(self):
    return [{
        "type": "function",
        "name": tool["function"]["name"],
        "description": tool["function"]["description"],
        "inputSchema": tool["function"]["parameters"],
    } for tool in self.tools()]
```

- [ ] **Step 4: Generalize `subscription.chat`**

Change the signature and retain only the policy import from `agent`:

```python
def chat(messages, session=None):
```

Preserve plain ChatGPT CLI chat by using no dynamic tools when `session is None`.
For desktop agent chat:

```python
base_instructions = session.system_prompt() if session else POLICY
tools = session.codex_tools() if session else []
```

Use those values in `thread/start`. For `item/tool/call`:

```python
if not session or count > 32 or not isinstance(args, dict) or len(json.dumps(args)) > 24000:
    raise RuntimeError("ChatGPT requested an unsupported tool or reached the action limit.")
yield {"type": "progress", "text": session.progress(params.get("tool"), args)}
try:
    result = session.dispatch(params.get("tool"), args)
except (ValueError, RuntimeError, OSError):
    result = {"error": "Tool action failed. Review the request and try again."}
server.send({"id": event["id"], "result": {
    "success": "error" not in result,
    "contentItems": [{"type": "inputText", "text": json.dumps(result)}],
}})
```

Delete the browser-specific imports and action validation from this function.
Keep account locking, ephemeral threads, read-only Codex sandbox, no approval
escalation, history injection, 32-call cap, sanitized errors, and Stop cleanup.

- [ ] **Step 5: Run subscription and agent tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_subscription tests.test_browser_agent -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit shared ChatGPT tools**

Run:

```powershell
git add apps\aios\agent.py apps\aios\subscription.py tests\fixtures\codex_server.py tests\test_subscription.py
git commit -m "Share agent tools with ChatGPT" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 9: Wire the desktop worker, process lifetime, and settings UI

**Files:**
- Modify: `apps/aios/worker.py`
- Modify: `apps/shell/main.cpp`
- Modify: `apps/shell/ModelSettings.qml`
- Modify: `tests/qml/tst_subscription.qml`
- Modify: `scripts/build-apps.sh`

- [ ] **Step 1: Add failing QML tests for agent settings**

Extend the fake backend config in `tests/qml/tst_subscription.qml`:

```qml
agent_mode: "remote",
agent_url: "https://agent.example/v1",
agent_model: "coding-model",
has_agent_key: true
```

Add object names and assertions:

```qml
compare(findChild(settings, "agentProvider").currentIndex, 2)
compare(findChild(settings, "agentUrl").text, "https://agent.example/v1")
compare(findChild(settings, "agentModel").text, "coding-model")
compare(findChild(settings, "agentKey").text, "")
```

Activate **Current chat model**, click Save, and assert
`backend.config.agent_mode === "current"` while the stored key is not placed in
the QML config object.

- [ ] **Step 2: Run QML tests and confirm failure**

Run in the existing Linux build environment:

```sh
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software /usr/lib/qt6/bin/qmltestrunner -input tests/qml
```

Expected: agent setting controls are not found.

- [ ] **Step 3: Route every desktop chat through the generic agent**

In `apps/aios/worker.py`, redact the new key on load:

```python
config["has_agent_key"] = bool(config.pop("agent_api_key"))
```

In the C++ `saved` handler, exclude `agent_api_key` alongside `api_key` and
`voice_key` when copying `pendingConfig` into the UI-visible configuration map.

Replace the chat branch with:

```python
elif action == "chat":
    if request.get("tool_socket"):
        from .agent import chat as agent_chat
        for event in agent_chat(request["messages"], request["tool_socket"]):
            print(json.dumps(event), flush=True)
    else:
        for text in chat(request["messages"]):
            emit("token", text=text)
    emit("done")
```

This keeps CLI chat text-only and ensures desktop local, remote, and ChatGPT
providers share the same agent session.

- [ ] **Step 4: Replace browser process ownership with tool-host ownership**

In `apps/shell/main.cpp`:

1. Rename member `browser` to `tools`.
2. Rename `browserDirectory` to `toolDirectory`.
3. Apply `tieToDesktop(tools)`.
4. Stop and clean `tools` wherever browser cleanup currently occurs.
5. In `run()`, start:

```cpp
const auto socket = toolDirectory.path() + "/tools.sock";
if (tools.state() == QProcess::NotRunning) {
    tools.setProcessEnvironment(env);
    tools.setStandardOutputFile(QProcess::nullDevice());
    tools.setStandardErrorFile(QProcess::nullDevice());
    QFile::remove(socket);
    tools.start("python3", {"-m", "aios.toolhost", socket});
}
request.insert("tool_socket", socket);
```

Remove the old `browser_socket` field. Preserve lazy startup, private temporary
directory permissions, parent-death behavior, and per-chat cleanup.

- [ ] **Step 5: Add Agent tasks controls**

In `ModelSettings.qml`, add properties/controls with these object names:

```qml
Choice {
    id: agentMode
    objectName: "agentProvider"
    model: ["Current chat model", "ChatGPT subscription", "Remote service"]
    Layout.fillWidth: true
}
Field {
    id: agentUrl
    objectName: "agentUrl"
    visible: agentMode.currentIndex === 2
    placeholderText: "Agent service URL · https://…/v1"
    Layout.fillWidth: true
}
Field {
    id: agentModel
    objectName: "agentModel"
    visible: agentMode.currentIndex === 2
    placeholderText: "Agent model ID"
    Layout.fillWidth: true
}
Field {
    id: agentKey
    objectName: "agentKey"
    visible: agentMode.currentIndex === 2
    placeholderText: "Agent API key · blank keeps saved key"
    echoMode: TextInput.Password
    Layout.fillWidth: true
}
Text {
    text: "Skills marked for stronger reasoning use this provider. Ordinary chat stays on the model selected above."
    color: theme.muted
    font.pixelSize: 11
    wrapMode: Text.Wrap
    Layout.fillWidth: true
}
```

In `reload()` map `current`, `chatgpt`, and `remote` to indexes 0, 1, and 2,
populate URL/model, and always clear the key field. Include these values in the
model Save object:

```qml
agent_mode: ["current", "chatgpt", "remote"][agentMode.currentIndex],
agent_url: agentUrl.text,
agent_model: agentModel.text
```

Add `agent_api_key` only when the user typed a non-empty value. Show the existing
ChatGPT account/sign-in controls when either the primary provider or Agent tasks
provider selects ChatGPT.

- [ ] **Step 6: Package built-in skills**

In `scripts/build-apps.sh`, after copying examples:

```sh
cp -R "$ROOT/apps/skills" "$DEST/usr/local/share/aios/"
```

The resulting image path must be
`/usr/local/share/aios/skills/application-builder/SKILL.md`.

- [ ] **Step 7: Run worker, QML, and shell checks**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_core tests.test_browser_agent tests.test_subscription -v
```

Then run the existing Qt test/build commands in the Linux build environment:

```sh
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software /usr/lib/qt6/bin/qmltestrunner -input tests/qml
cmake -S apps/shell -B /tmp/aios-shell-build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/aios-shell-build
```

Expected: Python tests pass, QML tests pass, and `aios-shell` builds.

- [ ] **Step 8: Commit desktop integration**

Run:

```powershell
git add apps\aios\worker.py apps\shell\main.cpp apps\shell\ModelSettings.qml tests\qml\tst_subscription.qml scripts\build-apps.sh
git commit -m "Wire agent capabilities into desktop chat" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 10: Complete end-to-end behavior and documentation

**Files:**
- Modify: `tests/test_browser_agent.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/browser.md`
- Modify: `docs/chatgpt-subscription.md`
- Modify: `docs/specs/chat-desktop.md`
- Create: `docs/agentic-tools.md`
- Modify: `docs/qa/implementation-status.md`

- [ ] **Step 1: Add cache-miss and cache-hit agent tests**

Use the existing streamed Chat Completions fixture helper to script:

1. `application.search` returning no matches.
2. `application.create`.
3. `application.write` with a complete calculator document.
4. `application.publish`.
5. `application.launch`.
6. A final text response.

Assert the function call names and arguments in order. Then run a second scripted
turn where `search` returns an exact match and assert only `search` and `launch`
are dispatched. Include this calculator document in the fixture:

```html
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calculator</title>
<style>
body{font:18px system-ui;margin:0;display:grid;place-items:center;min-height:100vh;background:#172633;color:#f1f5f6}
main{width:min(92vw,360px);background:#203340;padding:20px;border-radius:16px}
output{display:block;min-height:48px;text-align:right;font-size:32px;overflow-wrap:anywhere}
.keys{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
button{font:inherit;padding:14px;border:0;border-radius:10px}
</style>
<main aria-label="Calculator">
<output id="display" aria-live="polite">0</output>
<div class="keys" id="keys"></div>
</main>
<script>
const keys=["7","8","9","÷","4","5","6","×","1","2","3","−","0",".","=","+","C"];
const display=document.querySelector("#display");
let value="0",stored=null,operator=null,replace=true;
for(const key of keys){
  const button=document.createElement("button");
  button.textContent=key;
  button.onclick=()=>press(key);
  document.querySelector("#keys").append(button);
}
function press(key){
  if(key==="C"){value="0";stored=operator=null;replace=true;}
  else if(/[0-9.]/.test(key)){
    if(replace){value=key==="."?"0.":key;replace=false;}
    else if(key!=="."||!value.includes("."))value+=key;
  }else if(key==="="&&operator&&stored!==null){
    const right=Number(value);
    value=String(operator==="+"?stored+right:operator==="−"?stored-right:operator==="×"?stored*right:stored/right);
    stored=operator=null;replace=true;
  }else if(["+","−","×","÷"].includes(key)){
    stored=Number(value);operator=key;replace=true;
  }
  display.textContent=value;
}
</script>
</html>
```

- [ ] **Step 2: Run the end-to-end agent tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_browser_agent -v
```

Expected: cache-miss flow dispatches five application actions and cache-hit flow
dispatches two.

- [ ] **Step 3: Write user-facing configuration documentation**

Create `docs/agentic-tools.md` with:

- Built-in and user skill paths.
- Supported `SKILL.md` fields and AIOS metadata keys.
- The complete `mcp.json` shape with an allowlisted local fixture example.
- A requirement that `mcp.json` remain user-readable only when it contains
  explicit server credentials.
- The `mcp_<server>_<tool>` name mapping.
- Agent tasks provider behavior and explicit remote-data boundary.
- Application cache path and exact cache reuse behavior.
- The one-document offline application limitation.
- MCP stdio/tools-only limitation.
- Security statement: model prose never executes; configured MCP commands are a
  user trust decision; generated HTML runs only in the sandboxed wrapper.

Use this runnable MCP example:

```json
{
  "servers": {
    "weather": {
      "command": "python3",
      "args": ["/home/aios/tools/weather_server.py"],
      "env": {},
      "tools": ["current_weather"]
    }
  }
}
```

- [ ] **Step 4: Update existing docs without unrelated rewrites**

Make these precise additions:

- `README.md`: mention Agent Skills, allowlisted stdio MCP tools, Agent tasks
  provider selection, and the calculator example; link `docs/agentic-tools.md`.
- `docs/architecture.md`: replace browser-only worker language with the generic
  per-chat tool host and describe explicit remote-preferred routing.
- `docs/browser.md`: state browser is one built-in tool in the shared registry
  and retains its existing operation limits.
- `docs/chatgpt-subscription.md`: state Codex receives only the validated dynamic
  tools selected by AIOS and still has its host tools disabled.
- `docs/specs/chat-desktop.md`: add the user-visible skill, MCP, explicit remote
  agent routing, cached application, and structured-tool safety behavior.
- `docs/qa/implementation-status.md`: add an Agentic tools checkpoint listing
  tests actually executed; do not claim ISO or hardware validation unless run.

- [ ] **Step 5: Run documentation-adjacent source tests**

Run:

```powershell
$env:PYTHONPATH='apps'; python -m unittest tests.test_skills tests.test_applications tests.test_mcp tests.test_toolhost tests.test_browser_agent tests.test_core tests.test_subscription -v
```

Expected: all selected backend tests pass.

- [ ] **Step 6: Commit end-to-end behavior and docs**

Run:

```powershell
git add tests\test_browser_agent.py README.md docs\architecture.md docs\browser.md docs\chatgpt-subscription.md docs\specs\chat-desktop.md docs\agentic-tools.md docs\qa\implementation-status.md
git commit -m "Document and verify agentic application building" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 11: Push the branch and open the pull request before final validation

**Files:**
- No source changes required.

- [ ] **Step 1: Confirm the feature worktree and branch**

Run:

```powershell
git status --short --branch
git log --oneline --decorate -12
```

Expected: branch `agentic-tools-support`; only intentional changes, if any, are
present.

- [ ] **Step 2: Push the branch**

Run:

```powershell
git push -u origin agentic-tools-support
```

Expected: the remote branch is created or updated successfully.

- [ ] **Step 3: Open the pull request**

Use the app-native pull request tool with:

```text
Title: Add skills, MCP tools, and cached application building

Body:
## Summary
- add standards-compatible Agent Skills with a built-in application builder
- add allowlisted stdio MCP tools behind a generic per-chat tool host
- route remote-preferred skills to an explicitly selected ChatGPT or remote agent model
- cache and launch sandboxed offline applications such as calculators

## Validation
- targeted backend tests completed before opening this PR
- full source/QML/build validation follows on this branch
```

Expected: a non-draft PR targeting `main`.

### Task 12: Run final validation and update the PR

**Files:**
- Modify only files required to fix failures caused by this feature.

- [ ] **Step 1: Run the complete existing source test command**

Run in the repository's Linux/WSL environment:

```sh
bash scripts/test.sh
```

Expected: all Python tests, shell syntax checks, and Openbox XML validation pass.

- [ ] **Step 2: Run all QML tests and native shell build**

Run:

```sh
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software /usr/lib/qt6/bin/qmltestrunner -input tests/qml
cmake -S apps/shell -B /tmp/aios-shell-build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/aios-shell-build
```

Expected: all QML tests pass and `aios-shell` builds.

- [ ] **Step 3: Run the MCP fixture and calculator smoke tests together**

Run:

```sh
PYTHONPATH=apps python3 -m unittest \
  tests.test_mcp \
  tests.test_applications \
  tests.test_browser_agent \
  tests.test_subscription -v
```

Expected: all protocol, cache, agent, and provider integration tests pass.

- [ ] **Step 4: Commit and push validation fixes**

If validation required code changes, commit only those fixes:

```powershell
git add -u
git commit -m "Fix agentic tools validation issues" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
git push
```

If no changes were required, do not create an empty commit.

- [ ] **Step 5: Inspect the final diff**

Run:

```powershell
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git status --short
```

Expected: no whitespace errors, no uncommitted files, and only files listed in
this plan or directly required by a discovered integration failure.

- [ ] **Step 6: Update the pull request validation section**

Use the app-native pull request update tool to replace the provisional validation
text with the exact commands and results from Steps 1-3. Do not claim a full ISO
boot, real paid-provider call, or physical hardware validation unless those were
actually executed.
