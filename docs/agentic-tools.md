# Agentic tools

AIOS can give a tool-capable model a bounded set of structured capabilities.
There are three related pieces:

- **Agent Skills** are installed instructions and metadata that describe how to
  handle a class of requests. A skill can narrow which tools are available.
- **Built-in tools** are AIOS-owned functions. The current registry includes
  `browser`, `application`, `os_settings`, and `scheduled_jobs`.
- **MCP tools** come from explicitly configured local Model Context Protocol
  servers. AIOS validates and renames them before advertising them to a model.

Model text is never interpreted as a command. Only a completed structured call
to a tool advertised for that turn can reach the per-chat tool host.

## Scheduled jobs tool and service

`aios-scheduler` is a single Linux per-OS-user service started by the desktop
session, not by a chat window. The built-in `scheduled_jobs` tool and native
configuration use the same service validation and normalized readbacks.
Python integrations call
`aios.scheduled_jobs.request(value, timeout=10)`. The same protocol is one
UTF-8 JSON line per connection at
`$XDG_RUNTIME_DIR/aios-scheduler/service.sock`. Both endpoints verify `SO_PEERCRED`;
the runtime directory is 0700 and socket 0600. There is no TCP listener.

Every response is `{"status":"ok","result":...}` or
`{"status":"invalid|unavailable|conflict|quota_exceeded|needs_user_action","error":"..."}`.
Requests are capped at 128 KiB, responses at 2 MiB, and pages at 100 entries.
Unexpected fields (including owner, credentials, endpoint, and commands) are
rejected. All IDs are opaque UUID strings. Mutations use integer revisions.

| Action | Required fields besides `action` | Optional fields | Result |
| --- | --- | --- | --- |
| `health` | None | None | `available`, safe `error`, `active_runs`, configured `zone` or null |
| `binding` | `prompt` | None | Effective `provider`, opaque `profile`, `model`, permitted `capabilities`, `warnings`, configured `zone` or null |
| `preview` | `schedule` | None | Up to three `{utc,local}` occurrences |
| `create` | `config` | None | Normalized full job readback |
| `get` | `job_id` | None | Full job readback |
| `list` | None | `limit`, `after` job ID | Job readbacks without prompt/context bodies |
| `update` | `job_id`, `expected_revision`, `config` | None | Full replacement configuration readback |
| `pause`, `resume` | `job_id`, `expected_revision` | None | Full job readback |
| `delete` | `job_id`, `expected_revision` | None | `deleted: true`; stops active work before tombstoning |
| `run_now` | `job_id`, `expected_revision`, `request_id` | None | Durable run; request ID retries never execute twice |
| `cancel_run` | `run_id` | None | Run after process cleanup; terminal runs are unchanged |
| `list_runs` | `job_id` | `limit`, `before` sequence | Run summaries without result bodies/snapshots |
| `read_result` | `run_id` | None | Run including immutable job snapshot, result/error, usage |
| `acknowledge_result` | `run_id` | None | `acknowledged: true` |
| `unread` | None | `limit`, `after` sequence (default 0) | Durable unacknowledged outbox rows |

The model tool advertises a closed, action-specific JSON Schema for every row,
including nested schedule, execution, and notification objects; it is not a
generic service passthrough. Unknown fields are rejected before routing.
`binding` is trusted discovery: copy its provider, opaque fingerprint profile,
and model, choosing only needed returned capabilities. Model create/update
require the opaque profile, not an unbound `current`/`agent` alias; the service
revalidates bindings and the current capability allowlist on save and execution.
There are no owner, credential, endpoint, command, or authentication fields.
Background hosts neither advertise nor accept `scheduled_jobs`, even if its
name appears in saved capabilities.

Model-specific bounds deliberately fit the agent's 24 KiB tool-argument limit
and the host's 128 KiB request frame, including worst-case JSON surrogate-pair
escaping: prompt 1,024 characters, context 384, title/model/conversation 50
each. Their maximum UTF-8 sizes are 4,096, 1,536 and 200 bytes respectively.
Zone names are at most 100 ASCII characters, cron expressions 200, timestamps
40, and opaque profile references 72. Up to 32 distinct 64-character capability
names are allowed. Native/service callers retain the larger validated
32 KiB prompt and 64 KiB context byte limits. Do not silently truncate a larger
native configuration to edit it through the model tool.
Model pages default to 10 and cannot exceed 10; native pages can reach 100.
Oversized model readbacks return an explicit unavailable result with smaller
page/native-view guidance rather than silently discarding saved content.

`update` is full replacement, not a patch. Read the job first and preserve
unchanged fields, including its bound route. Revision conflicts require a
fresh read and reconciliation; never overwrite a concurrent edit by blind
retry. For each user-requested manual run generate a UUID `request_id`, retain
it with the original revision, and reuse both after transport uncertainty.
A fresh UUID means a new explicitly authorized run. These durable receipts
survive reconnect and result retention; retries never create another run.
In-flight runs retain the original immutable snapshot when the job is edited.

Page jobs with the last `id` as `after`; page runs with the last `sequence`
as `before`; page unread with the last `sequence` as `after`. Unread results
are durable, not ephemeral notification events: reconcile from sequence zero
on reconnect and deduplicate by run ID. `read_result` does not acknowledge.
Only acknowledge after presenting a result or an explicit mark-read request;
opening/listing the inbox alone never marks all results read.

Unread native rows include bounded job title, job/run IDs, outcome, state,
scheduled/start/end times, the saved notification policy, a computed
`suppressed_until`, and the durable `notified` receipt; prompt and result bodies
still require `read_result`. Actionable-only successful `unchanged` runs remain
in history but do not enter unread feedback. Missing/invalid outcomes, failures,
interruptions, missed runs, and needs-user-action results remain deliverable.

Job configuration follows `scheduling.validate_job`: title, prompt, schedule
`{kind:"once"|"cron",value,zone}`, execution
`{provider,profile,model,capabilities,timeout_seconds,token_budget,tool_budget,missed_run}`,
and optional context, conversation reference, notification policy. Readbacks
include `next_due`, `next_local`, and `next_occurrences` (three-entry preview).
Never substitute UTC when `zone` discovery returns null; obtain an explicit
IANA zone. Preview does not execute or save work.

Binding uses existing initial-skill provider routing and trusted AI models
configuration. The saved profile is an opaque `current@fingerprint` or
`agent@fingerprint` reference to its endpoint/model or local model path, never
a credential. Create/update accept `current` or `agent` to bind explicitly;
existing opaque references are validated, not silently rebound. Missing keys,
changed bindings and rejected authentication produce one durable action-needed
result and pause dispatch for that unchanged job configuration. Resume requires
the user to fix the existing binding or explicitly edit the job.

Each run owns an isolated worker, agent session, tool host and browser session.
Saved capability names are intersected with the current allowlist on the
server before calls. Background MCP requires explicit configured tool names;
wildcard discovery does not grant unattended access. Scheduling and native
authentication are unavailable from background runs. The model client returns unavailable for a bare protected process context.
Only a broker-launched protected foreground chat receives
`AIOS_PROTECTED_CHAT_SOCKET`, a private fixed-action relay owned by that chat's
sandbox supervisor. It accepts only a validated `scheduled_jobs` request and
returns the normal status envelope. A principal descriptor or
`AIOS_SESSION_SOCKET`/`AIOS_SESSION_ID` marker never authorizes scheduling and
never falls back to the desktop socket.

Workers use monotonic deadlines, a hard tool-call count and bounded output.
`token_budget` is conservatively enforced as UTF-8 output bytes (including tool
arguments) independently of tokenizer; OpenAI-compatible requests also receive
the remaining `max_tokens`. This bounds accepted/generated visible output,
not a provider's unreported internal reasoning or billing. Results and errors
are committed only after the run subreaper has stopped its descendants,
including browser/MCP processes in separate process groups. A scheduler crash
closes the run control pipe; restart waits for cleanup leases before marking
abandoned runs interrupted. No shared desktop conversation history is written.

The desktop starts `aios-local-runtime service` before the scheduler and stops
it after scheduled workers have exited. This one unprivileged daemon owns
`llama-server`; the shell owns only a readiness client. `core.request` holds
`local_runtime.admission(config, background=..., timeout=...)` until the HTTP
response closes. Waiting interactive requests take priority over waiting
background requests, FIFO within each class. Active local background requests
are capped at 60 seconds, interactive requests at 120 seconds; abandonment or
expiry stops the old model process before admitting another generation.
Background callers never spawn the daemon under their run guardian.
Local model file identity is included in saved bindings, so replacing a model
requires reviewing affected jobs rather than silently switching their model.
Local model loading and lease errors are bounded safe failures, not permission
to fall back to a paid provider. Full hardware/model compatibility and the real
Alpine Chromium sandbox remain part of the later image acceptance milestone.

### Native configuration bridge

The shell exposes one global `scheduledJobs` object implemented by
`apps/shell/ScheduledJobs.h`. Open its native editor through **Settings >
Scheduled jobs > Open scheduled jobs**; `ScheduledJobsWindow.qml` provides
configuration, preview, job controls, history and saved-result inspection.
It uses the same service configuration objects and normalized readbacks as
the Python client, not a separate scheduling implementation.
Protected sessions expose native **Scheduled jobs** in `IdentityStatus.qml`.
Opening it creates a separate native client bound to that SessionControl's
current lease/work, never the global desktop client. Protected natural-language
chat uses the same schemas and scheduler validation through its per-chat relay.

The QML-facing methods are fixed:

| Method | Service action |
| --- | --- |
| `health()` | `health` |
| `binding(prompt)` | `binding` |
| `preview(schedule)` | `preview` |
| `create(config)` | `create` |
| `get(job)` | `get` |
| `list(limit=50, after="")` | `list` |
| `update(job, revision, config)` | `update` |
| `pause(job, revision)`, `resume(job, revision)` | `pause`, `resume` |
| `remove(job, revision)` | `delete` |
| `runNow(job, revision, request)` | `run_now` |
| `cancelRun(run)` | `cancel_run` |
| `listRuns(job, limit=20, before=0)` | `list_runs`; zero omits the cursor |
| `readResult(run)` | `read_result` |
| `acknowledgeResult(run)` | `acknowledge_result` |
| `markNotified(run)` | Native-only `mark_notified`; persists orb attention |
| `unread(limit=50, after=0)` | `unread` |

Every method above is asynchronous: it immediately returns a correlation ID,
then emits `completed(requestId, action, response)` with that ID, the service
action name, and the validated status envelope. Completion is deferred so a
consumer can register the returned ID before observing the reply.
`newRequestId()` is different: it synchronously returns a UUID for a durable
manual-run request. Pass this UUID as `runNow`'s `request` argument and retain
it with the original revision for retries. The asynchronous call's correlation
ID is not the durable deduplication ID.

`scopeGeneration` changes when the bridge is invalidated. Invalidation aborts
pending sockets, drops queued/deferred replies from the old generation and
emits `invalidated()`. Consumers must clear cached jobs, results, bindings,
pending correlations and displayed private content on that signal; they must
not wait for old calls to complete. Reconcile only the newly authorized scope.

No QML method accepts an arbitrary service request, socket path, owner,
credential or authorization capability. Ordinary requests use the fixed
private desktop socket with same-UID peer verification. The protected native
route requires a per-instance trusted C++ context provider and broker transport;
context changes invalidate authorization, and missing context reports
unavailable rather than using desktop storage. The model-facing Python tool
does not inherit this native authorization route. Native requests are bounded
to 128 KiB before protected wrapping, responses to 2 MiB, with at most eight
pending sockets and a 15-second timeout.

Internally, the protected bridge sends the broker
`{action:"scheduled_jobs", lease:<current broker lease>,
scope:<active work-session UUID>, request:<normal scheduling request>}`.
These fields come only from the trusted native context provider, never a
model argument. `SessionControl::schedulingContext()` is a native-only getter,
not a QML-invokable method or property; authority fields stay out of QML.
The active-work UUID comes from the broker status `session` field and is sent
as `scope` in scheduling requests. `SessionControl::createScheduledJobs()` binds
one client to that exact native lease/work snapshot; it cannot silently rebind
when the active workspace changes. `protectedWorkspace` is informational, not
authority. Scope loss or broker transport failure invalidates the client,
erases its context provider, and closes/clears the owned native window; reopening
requires a fresh native binding. `dispose()` releases an owned client on close.
The bridge verifies a root `SO_PEERCRED` broker peer. The broker verifies the
trusted shell UID and original registered display PID, validated
personal mode and current owner/work/lease presence; a sign-in completion
status is not an authorization capability. The broker's normal successful
response wrapper contains the scheduler status envelope as its `result`.
The native bridge unwraps that envelope before emitting `completed`.
An agent tool cannot call this privileged broker directly. The global
`scheduledJobs` object never receives a protected context provider; only the
dedicated native widget's instance does. A descriptor, informational sign-in
completion, or QML property cannot authorize or select its lease/work.
`Main.qml` opens protected chats only through
`SessionControl::createProtectedChat()`. `ProtectedChat.h` retains the captured
lease/work, broker-issued chat ID, and a distinct per-chat association key as
private native state; none are QML properties or method parameters. Calls use
only fixed `chat_open`, `chat_send`, `chat_poll`, `chat_stop`, and `chat_close`
broker actions. The broker accepts them only from the registered display PID
and requires the exact current lease/work/chat/key generation.

The broker launches `aios.protected_chat` inside the owner encrypted workspace
with `AIOS_PRINCIPAL`. It starts the ordinary worker and tool host there, so
provider configuration, application data, MCP configuration, browser state and
tool outputs remain inside encrypted storage or ephemeral process memory.
Messages are persisted through the broker-owned encrypted journal, not
`core.py` desktop conversation history. The supervisor exposes one private
`scheduled_jobs` relay to its tool host and no broker socket, lease, work,
owner, PID, or generic JSON passthrough. Privacy loss invalidates native
callbacks, clears and closes the window, kills the complete process cgroup, and
rejects stale or cross-chat replies. Protected chat currently omits attachment
and voice capture controls; those inputs never fall back to desktop processing.
During isolated scheduler startup the broker can return `unavailable` with
"Protected scheduling is starting; retry shortly". The native editor retries
health at most ten times at one-second intervals, never replays mutations,
preserves pending forms, and reconciles current revisions after recovery.
Preserve a manual run's durable request UUID and original revision rather than
creating a second run request.

`ScheduledFeedback.qml` is the reconnecting shell consumer. It polls health and
up to four 50-row unread pages, deduplicates by run ID, orders by outbox
sequence, and never treats an ephemeral completion signal as the source of
truth. Before starting one coalesced orb pulse it persists `mark_notified` for
each eligible run, so reconnect and shell restart do not replay attention.
Quiet hours and snooze leave that receipt unset until their computed release;
reduced motion never starts the pulse.

`ScheduledResultWindow.qml` opens without acknowledging the inbox. Rendering
one saved result or choosing **Mark read** acknowledges only that run. **New
chat** creates a fresh chat. **Follow up** creates a fresh chat with a bounded,
unsent draft containing the saved title/task/context/result; it does not replay
tools or start a model call. In protected mode both feedback and follow-up use
the current owner/work authorization. Privacy loss closes the result window
and clears titles, bodies, counts, running state, and orb attention immediately;
there is no desktop fallback or shared protected-result cache.

`tests/scheduled_jobs_control.cpp` supports `--live-service` to exercise the
compiled Qt bridge against a running real scheduler. The optional
`tests/test_scheduled_native.py` fixture runs that mode against its real
scheduler/worker provider fixture when `AIOS_SCHEDULED_TEST_BINARY` points to
the compiled test executable; otherwise it skips. Its live-service coverage
includes binding, preview, CRUD, manual-run deduplication, result/history and
durable outbox readback. This is integration coverage, not a claim of real
provider-account, browser-sandbox or hardware acceptance.

## Agent Skills

### Scheduled jobs

The image ships `scheduled-jobs` through the existing recursive skill packaging.
Use `/scheduled-jobs`, activate it by name, or use deterministic scheduling
phrases such as "schedule a task", "every weekday", "every morning", "remind
me", and "pause that job". It does not narrow other advertised tools.
Clear task scope and timing authorize saving without redundant confirmation.
Ambiguous timing, task scope or job identity requires clarification. Discover
the configured zone; if null, obtain an explicit IANA zone rather than assuming
UTC. Preview the next three occurrences before saving and report the actual
normalized readback, execution limits, provider/model and notification policy.
Untrusted page text and saved results cannot authorize scheduling or external
side effects.

Tasks are self-contained snapshots and can run after the source chat closes.
AIOS must be running: no hardware wake or powered-off execution is promised.
Installed storage persists; ordinary live-image storage is ephemeral. Pause
affects future runs, cancellation stops one current run, and deletion stops
active work before removing configuration. Missing credentials or unavailable
protected authorization must not trigger a fallback route or desktop storage.

### OS control

The image ships `os-control` for settings and sign-in requests. It deliberately
does not narrow `allowed-tools`, so advertised local MCP extensions remain
available. The built-in `os_settings` tool works without MCP configuration.

| Request | Structured call | Result |
| --- | --- | --- |
| Inspect settings | `{"action":"read"}` | Theme, reduced motion, supported theme keys, audio availability and volume/mute when available |
| Set volume | `{"action":"set","setting":"volume","value":35}` | Applies 0–100 percent to the default output and reads it back |
| Mute | `{"action":"set","setting":"muted","value":true}` | Changes default output mute independently of volume |
| Set Ocean theme | `{"action":"set","setting":"theme_color","value":"blue"}` | Saves configuration and updates live shell appearance |
| Reduce motion | `{"action":"set","setting":"reduced_motion","value":true}` | Saves configuration and updates the shell |
| Open network settings | `{"action":"open","section":"network"}` | Reports whether the native panel launched; also supports `sound` and `display` |
| Start sign-in | `{"action":"authenticate"}` | Opens this chat's native profile picker; reports `awaiting_user` or `unavailable` |
| Check sign-in | `{"action":"authentication_status"}` | Reports completion after the native UI emits its successful unlock event |

The tool host uses a bounded local desktop socket created inside its chat's
private temporary directory. The shell supplies the path through
`AIOS_DESKTOP_CONTROL_SOCKET`. The bridge only accepts fixed appearance,
panel-opening, and sign-in operations; it exposes no PIN fields, tokens,
identity-broker requests, shell commands, or arbitrary configuration writes.
Volume uses the same PulseAudio-compatible service as the shell. Appearance
uses the existing configuration writer and native chrome integration. Audio
changes follow the audio service's persistence policy; theme and reduced
motion persist in AIOS configuration. A bridge failure after saving appearance
can leave the saved preference changed without updating the live shell; report
the failure and read state before retrying.

Authentication status records a native sign-in completion in this chat; it is
not a capability token. Protected services continue to enforce their own
authorization. The model never supplies the PIN. While the user is choosing a
profile or entering a PIN, return control to them rather than polling. A
cancelled picker may remain `awaiting_user`; that never means authentication
succeeded. Identity changes and privacy loss clear completion status.

For a separate local MCP client, the same implementation is available as
`python3 -m aios.os_settings`. Configure it with an explicit tool allowlist:

```json
{
  "servers": {
    "os": {
      "command": "python3",
      "args": ["-m", "aios.os_settings"],
      "tools": ["os_settings"]
    }
  }
}
```

AIOS advertises this optional tool as `mcp_os_os_settings`. The module must be
on the server's Python path (installed at `/usr/local/share/aios`). Audio needs
access to the user's audio runtime. Appearance, native panels and authentication
also require `AIOS_DESKTOP_CONTROL_SOCKET` to point to the live chat bridge;
external clients must explicitly supply that environment value and, if needed,
`PYTHONPATH` and `XDG_RUNTIME_DIR` in the server's `env`. Do not persist temporary
chat socket paths as permanent installation defaults. Prefer the built-in tool
inside AIOS, which receives the live socket automatically. MCP initialization
and the tool description include the read/change/check and native sign-in rules.

Extend OS control through named settings/actions or allowlisted local MCP
operations. Each extension should advertise exact inputs, return bounded
non-secret results and actual completion state, and reuse existing service
authorization. Opening a panel alone is not automation of every control inside
it. Unsupported settings must be reported rather than emulated through model
prose or arbitrary command execution.

### Discovery

AIOS searches these directories in order:

1. `/usr/local/share/aios/skills` for skills shipped in the image.
2. `$XDG_CONFIG_HOME/aios/skills` (normally `~/.config/aios/skills`) for user
   skills.

A valid skill is a directory named with lowercase letters, numbers, and hyphens
and containing `SKILL.md`. A valid user skill with the same name replaces the
built-in definition.

AIOS implements a deliberately small Agent Skills subset:

- YAML-like frontmatter bounded by `---`, followed by Markdown instructions.
- Required string fields `name` and `description`. `name` must match the
  directory name.
- Optional string fields `compatibility` and `allowed-tools`.
  `compatibility` is informational in this release. `allowed-tools` is a
  whitespace-separated list of AIOS model-facing tool names.
- Optional `metadata` with nested string fields. AIOS uses
  `aios-triggers`, a comma-separated list of deterministic phrases, and
  `aios-model`, which is `current` or `remote-preferred`.
- Quoted or unquoted scalar strings only. Lists, objects, anchors, executable
  YAML tags, and automatic loading of referenced files are not supported.

Example:

```markdown
---
name: example-helper
description: Helps with a narrowly defined example workflow.
compatibility: AIOS 0.1
allowed-tools: application mcp_notes_lookup
metadata:
  aios-triggers: run the example helper, use example helper
  aios-model: current
---

Follow the user's request using only the advertised structured tools.
```

Skills use progressive disclosure. At the start of a turn, the model sees only
the installed names and descriptions. AIOS injects full instructions for a
skill selected by an exact leading `/skill-name` or deterministic trigger.
The model can also call `activate_skill` by exact installed name. Up to 64
skills are loaded, each `SKILL.md` is limited to 48 KiB, and at most three
skills can be active in a turn.

## Application builder and cache

The image ships `application-builder`. It is intended for requests with clear
build intent, such as:

> I need a calculator

Triggers use phrases such as "need a calculator", "build a timer", or "make an
application". A bare noun such as "calculator" or a request to explain a
calculator does not activate the builder.

The skill permits only the `application` tool and requires this workflow:

1. Search the cache with the complete user request.
2. Inspect the advertised application schema. `runtime` and `template` appear
   only when an executable `aios-app-host` is available. The advertised native
   templates are the complete native capability set for that turn.
3. For a supported request, prefer an exact or strong cached native match.
   Calculator is currently the only native template. If search finds only a
   legacy web calculator while the native calculator template is advertised,
   create the native calculator rather than treating the web result as a
   permanent preference.
4. Create a native calculator with `runtime: "native"` and
   `template: "calculator"`, publish its summary and keywords, then launch it.
   Native creation never uses `read`, `write`, generated HTML, or model-supplied
   native source.
5. When native support is absent or the requested app type has no advertised
   template, use the web fallback: create a draft, write one self-contained
   offline `index.html`, publish its metadata and digest, and launch it.
6. Report success only after launch returns `launched: true`. A
   `launched: false` result is terminal for that attempt: report its safe reason
   without rebuilding, re-publishing, or retrying in a loop.

Published applications are stored under:

```text
$XDG_DATA_HOME/aios/applications/<application-id>/
```

Native cache entries contain only `manifest.json`, including the trusted runtime
and template identifiers. Web cache entries contain `index.html` and
`manifest.json`. Web documents are limited to 16 KiB and must not depend on
remote scripts, styles, fonts, media, packages, or extra asset files. Search
ranks exact normalized request matches first, then title and keyword overlap;
native entries win equal-ranked ties.

Native templates are trusted, compiled Qt implementations shipped with AIOS.
The model selects an advertised template and supplies metadata only; it never
generates or compiles C++ or QML. Native launch is considered ready only after
the host presents its first frame and completes the readiness handshake.

For web apps, the runner verifies the manifest and SHA-256 digest, serves an
AIOS wrapper and the app from a temporary loopback HTTP server, and opens
Chromium in app mode. Readiness is acknowledged only after Chromium makes the
first `/app` request. The generated document runs in a sandboxed iframe with a
restrictive Content Security Policy. Chromium keeps its normal process sandbox,
blocks non-loopback name resolution for the app window, and uses a temporary
profile. Either runtime returns `launched: false` with a bounded safe reason when
its readiness check fails.

This is not arbitrary application generation. The model cannot run host shell
commands, install dependencies, write outside the application cache, or create
backend services. It can select only precompiled native templates advertised by
AIOS; calculator is currently the sole template. All other supported generated
utilities remain frontend-only web applications in the sandbox. Arbitrary
native applications and arbitrary backend applications are not supported.

## Agent task provider routing

Ordinary chat always stays on the selected primary provider. A separate
**Agent tasks** setting matters only when an active skill declares
`metadata.aios-model: remote-preferred`:

- **Current chat model** keeps the task on the current local, remote, or ChatGPT
  chat provider.
- **ChatGPT subscription** uses the signed-in subscription and selected
  subscription model.
- **Remote service** crosses an explicit boundary to a separately configured
  OpenAI-compatible endpoint, model, and API key.

The default is Current chat model. AIOS never silently falls back to a paid
provider. A missing remote profile or signed-out ChatGPT subscription produces
a configuration error instead.

CLI configuration uses:

```sh
aios-llm configure --agent-mode current
aios-llm configure --agent-mode chatgpt
aios-llm configure --agent-mode remote \
  --agent-url https://agent-provider.example/v1 \
  --agent-model MODEL \
  --ask-agent-key
```

`--ask-agent-key` reads the credential without putting it in shell history.
Primary chat continues to use `--mode`, `--url`, `--model`, and `--ask-key`.
Changing either endpoint clears only the corresponding saved key unless a
replacement is supplied.

## MCP configuration

AIOS currently supports stdio MCP servers and tools only. It does not expose MCP
resources, prompts, sampling, elicitation, roots, or Streamable HTTP.

Create `$XDG_CONFIG_HOME/aios/mcp.json` with exactly one top-level `servers`
object:

```json
{
  "servers": {
    "notes": {
      "command": "/usr/bin/node",
      "args": ["/home/aios/tools/notes-server.js", "--stdio"],
      "env": {
        "NOTES_DATABASE": "/home/aios/.local/share/notes.db",
        "NOTES_TOKEN": "replace-with-a-user-managed-secret"
      },
      "tools": ["lookup", "store"]
    },
    "calculator-data": {
      "command": "/usr/local/bin/calculator-data-mcp",
      "args": [],
      "env": {},
      "tools": ["*"]
    }
  }
}
```

For each server:

- `command` is required and is executed directly, never through a shell.
- `args` is an optional array of literal argument strings.
- `env` is an optional object of explicit string environment variables.
- `tools` is required. List exact discovered MCP tool names, or use `"*"` to
  approve all tools reported by that server. An empty list disables the server.

The configured executable and allowlist are a user trust decision. An MCP
server is local code with the operating-system rights of the desktop user; its
own behavior is not sandboxed by the MCP protocol. Review the command and server
source before enabling it. If `mcp.json` contains credentials, make it readable
and writable only by the user, for example `chmod 600 ~/.config/aios/mcp.json`.

MCP processes inherit only `PATH`, `HOME`, the XDG config/data/cache locations,
and locale variables. Keys, tokens, secrets, passwords, and other ambient
environment variables are not inherited. Credentials needed by a server must
be placed explicitly in its `env` object.

AIOS starts enabled servers lazily for a chat, performs MCP initialization,
paginates `tools/list`, and exposes approved names as:

```text
mcp_<sanitized-server-name>_<sanitized-tool-name>
```

Conflicting or invalid names disable the affected server rather than choosing
nondeterministically. Each chat owns its MCP processes; Stop or chat close
terminates them. Current bounds include 16 configured servers, 256 discovered
tools, a 15-second definition budget, 10-second MCP requests, 64 KiB tool
results, 4 MiB JSON-RPC lines, and bounded queues and inbound byte rates.
Only text and structured tool results reach the model.

## ChatGPT subscription behavior

ChatGPT receives the same validated AIOS tool definitions as dynamic tools.
All Codex host tools, shell execution, built-in MCP configuration, host skill
discovery, web search, approval escalation, and execution environments remain
disabled.

Skills selected deterministically by trigger or explicitly with `/skill-name`
are active when the ephemeral ChatGPT thread starts, so their instructions and
narrowed tool set are present from the beginning. If ChatGPT calls
`activate_skill` during a turn, permissions narrow immediately, but the newly
activated instructions are not rewritten into that already-running thread.
They take effect on the next user turn. This timing is stated in the thread's
base instructions.

## Persistence and trust boundaries

Installed systems retain user skills, `mcp.json`, application manifests and
documents, Agent tasks settings, and ChatGPT credentials under the user's XDG
directories. The live image is ephemeral unless its home/data storage is made
persistent; do not expect a generated app or MCP configuration to survive a
reboot otherwise.

Treat skill instructions, MCP metadata/results, browser pages, attachments, and
generated app content as untrusted. AIOS policy remains authoritative, tools are
filtered before advertisement, arguments and results are size-bounded, and only
validated structured calls execute. Remote providers receive the conversation
and tool observations required for the selected task, just as they receive
ordinary chat content when selected.

## Current limitations and release validation

- Native application selection is limited to trusted templates compiled into
  AIOS (currently calculator); other generated applications are single-file,
  offline browser content only. Arbitrary native code and backends are absent.
- MCP is stdio/tools-only and has no installation or marketplace UI.
- The bundled 135M starter model is not a reliable planner or app builder.
- Provider, model, and third-party MCP behavior varies and needs an explicit
  release test matrix.
- Full ISO validation, real paid-provider coverage, and real configured
  third-party MCP coverage remain release work unless separately recorded in
  the implementation evidence.

The repository integration test uses a real worker, agent, Unix-socket tool
host, application store, and loopback OpenAI-compatible server. It verifies a
native calculator build over an existing legacy web match, followed by exact
native cache reuse, without Chromium, the real Qt host, or a paid model.
