# Agentic Tools and Application Builder Design

## Status

Approved for autonomous implementation on 2026-09-08.

## Context

AIOS currently has two bounded agent paths:

- OpenAI-compatible Chat Completions with one structured browser tool.
- A pinned Codex App Server used for ChatGPT subscription chat, with all host
  tools disabled and the same browser capability supplied as a dynamic tool.

The bundled SmolLM2 135M model is useful for basic offline chat but is not a
reliable planner or coding model. AIOS also ships a development toolchain and a
manual `aios-new-app` example, but chat cannot discover reusable skills, connect
to MCP servers, create an application, cache it, or launch it.

## Goals

1. Load standards-compatible Agent Skills from built-in and user directories.
2. Connect explicitly configured stdio MCP servers and expose approved tools to
   local, remote, and ChatGPT subscription models.
3. Replace the browser-only agent path with a reusable structured tool registry.
4. Ship an `application-builder` skill that can satisfy requests such as
   "I need a calculator."
5. Search a persistent application cache before building, then launch either the
   cached or newly generated application.
6. Let users explicitly route skill-driven agent work to the current model,
   ChatGPT subscription, or a separate OpenAI-compatible remote model.
7. Preserve the existing rule that model prose is never executed.

## Non-goals

- Arbitrary shell or host filesystem access controlled by a model.
- Dependency installation or package-manager execution.
- Native/backend application generation in the first release.
- MCP Streamable HTTP, MCP prompts, MCP resources, sampling, or elicitation.
- An online skill or MCP marketplace.
- Silent remote fallback when the user has not selected a remote agent provider.

The initial application builder targets self-contained offline browser
applications. This covers calculators, timers, converters, games, dashboards,
forms, and similar small utilities while keeping the execution boundary narrow.
Native, backend, and dependency-based builders can be added later after AIOS has
an approval UI and a stronger build sandbox.

## Assumptions

- Agent Skills use the `agentskills.io` directory format with a required
  `SKILL.md` containing YAML frontmatter and Markdown instructions.
- The first MCP transport is stdio, which the MCP specification recommends that
  clients support whenever possible.
- Configuring an MCP server and its tool allowlist is an explicit trust decision
  by the local user.
- Ordinary chat keeps using the selected chat provider. Only an activated skill
  marked `remote-preferred` uses the separately selected agent provider.
- The existing ChatGPT subscription sign-in and selected subscription model are
  reused when ChatGPT is selected for agent tasks.

## Considered Approaches

### 1. Native capability registry

Generalize the existing browser loop into a small AIOS-owned registry for
built-in tools, skills, and MCP tools. Reuse the current OpenAI-compatible and
Codex transports.

This is the selected approach. It matches the repository's dependency-light
Python architecture, retains existing safety properties, and creates clear
interfaces that can be tested independently.

### 2. Add each capability directly to `agent.py`

This would produce the smallest first diff, but browser dispatch, skill parsing,
MCP lifecycle handling, application storage, and provider routing would become
one coupled loop. It would be difficult to extend without repeatedly changing
provider-specific code.

### 3. Adopt an external agent framework

An external framework could provide orchestration and MCP support immediately,
but it would significantly increase image size, dependency maintenance, and
provider coupling. It would also duplicate the bounded transports and Codex
adapter already implemented by AIOS.

## Architecture

### Skill catalog

`apps/aios/skills.py` discovers skill directories in this order:

1. `/usr/local/share/aios/skills`
2. `$XDG_CONFIG_HOME/aios/skills`

User skills override built-in skills with the same valid name. The loader reads
only `SKILL.md`, validates the required Agent Skills fields, caps file size, and
uses progressive disclosure:

- Initial model context receives each skill's name and description.
- A matching skill's complete instructions are injected only when activated.
- Referenced files are not automatically read in this release.

AIOS supports the standard `allowed-tools` field plus these optional string
metadata keys:

- `aios-triggers`: comma-separated words or phrases for deterministic initial
  activation.
- `aios-model`: `current` or `remote-preferred`.

The agent also exposes a local `activate_skill` function so a capable model can
activate a skill by name after reading the catalog. Explicit `/skill-name`
requests activate that skill without model inference. At most three skills may
be active in one turn.

The image ships an `application-builder` skill. Its trigger metadata includes
terms such as app, application, calculator, timer, converter, game, dashboard,
and tracker. Its instructions require searching the cache before creating a new
application and permit only the application tools.

### Tool host

`apps/aios/toolhost.py` replaces the browser-specific broker with one private
per-chat Unix-socket service. It owns:

- The existing `Browser` instance.
- The built-in application tool.
- One lazy stdio MCP client per enabled server.

The wire protocol remains bounded JSON lines and supports two operations:

- `list`: return model-facing function definitions and display metadata.
- `call`: invoke one named function with a JSON object.

Tool names are unique and stable. Built-ins retain simple names such as
`browser` and `application`. MCP names use
`mcp_<sanitized-server-name>_<sanitized-tool-name>`. Duplicate or invalid names
disable the conflicting server instead of selecting one nondeterministically.

The C++ shell starts the tool host lazily for chat and ties it to the chat
window's lifetime, preserving the current private-socket and process-cleanup
behavior. Stop closes browser and MCP processes. Application windows launched
by the application runner have their own lifetime.

### Provider-neutral agent loop

`apps/aios/agent.py` becomes the provider-neutral owner of:

- Conversation validation.
- Skill discovery and activation.
- Tool filtering from `allowed-tools`.
- Bounded structured call parsing.
- Tool result truncation and history compaction.
- Progress events for the UI.

The existing OpenAI-compatible path receives the selected tool schemas through
Chat Completions. The ChatGPT subscription adapter receives the same schemas as
Codex dynamic tools and dispatches calls through the same tool host. Neither
provider may invoke a tool that was not advertised for that turn.

The OpenAI-compatible loop remains limited to eight rounds and four calls per
round. The ChatGPT adapter remains limited to 32 calls. Tool argument payloads,
tool results, and retained observations have explicit size caps.

### Agent model routing

Model settings add an **Agent tasks** provider with three choices:

- **Current chat model**: no provider switch.
- **ChatGPT subscription**: use the existing Codex authentication and selected
  subscription model.
- **Remote service**: use a separate HTTPS OpenAI-compatible endpoint, model,
  and API key.

The default is **Current chat model**, so upgrading does not transmit local
prompts remotely. The separate remote credentials use the existing mode-0600
configuration file, URL validation, redirect rejection, key redaction, and
key-clearing behavior when the endpoint changes.

Routing occurs only when an activated skill declares
`metadata.aios-model: remote-preferred`. If the selected agent provider is not
configured or ChatGPT is signed out, AIOS returns a clear configuration error;
it does not silently fall back to another paid service. Browser-only chat without
an activated remote-preferred skill keeps the current provider behavior.

### MCP client

`apps/aios/mcp.py` loads
`$XDG_CONFIG_HOME/aios/mcp.json`. Each server entry contains:

```json
{
  "command": "executable",
  "args": ["arg"],
  "env": {"NAME": "value"},
  "tools": ["allowed_tool_name"]
}
```

`tools` is required and may contain `"*"` to expose every discovered tool. The
client launches commands directly without a shell, performs the MCP
`initialize` request and `notifications/initialized` notification, paginates
`tools/list`, and invokes `tools/call`.

The client advertises no roots, sampling, or elicitation capabilities.
Server-initiated requests receive a method-not-supported response. Notifications
are ignored except that `notifications/tools/list_changed` invalidates the cached
tool list. All requests have timeouts, stdout is limited to valid newline-delimited
JSON-RPC, stderr is drained without entering chat, results are capped, and the
process is terminated on chat close.

MCP processes inherit only the normal executable path, home/XDG locations, and
locale. Variables whose names indicate keys, tokens, secrets, or passwords are
removed. A server that needs a credential must receive it explicitly through
its configured `env` object.

Only text and structured MCP tool results are passed to the model in this
release. Image, audio, and embedded-resource results return an unsupported-content
error. MCP descriptions, annotations, and results are treated as untrusted data.

### Application cache and runner

Applications live under
`$XDG_DATA_HOME/aios/applications/<application-id>`. The built-in `application`
function supports:

- `search`: token-match published manifests against a user request.
- `create`: allocate a new safe workspace and draft manifest.
- `read`: read the bounded application document from that workspace.
- `write`: atomically write the self-contained `index.html`.
- `publish`: validate required files and persist searchable metadata.
- `launch`: start a published application.

Application IDs and relative paths are strictly validated. Symlinks, absolute
paths, traversal, hidden control files, extra files, and oversized documents are
rejected. The first release stores one `index.html` with inline CSS, JavaScript,
and optional SVG. External scripts, styles, fonts, media, and package
dependencies are not supported.

Published manifests contain title, summary, normalized source request, search
keywords, entrypoint, timestamps, and a content digest. `search` ranks exact
normalized requests first, then keyword overlap. A repeated calculator request
therefore launches the prior result instead of rebuilding it.

`apps/aios/app_runner.py` serves a trusted wrapper and one published application
document on a random loopback port, then launches Chromium in application mode
with a temporary profile and its sandbox enabled. The runner:

- Embeds generated content in an iframe with only `allow-scripts`; forms,
  downloads, popups, same-origin privileges, and top-level navigation remain
  disabled by the iframe sandbox.
- Adds a restrictive Content Security Policy that permits inline script/style
  needed by the app but blocks connections, external resources, frames, forms,
  plugins, and base-URL changes.
- Serves no arbitrary paths and provides no directory listing.
- Stops when the Chromium application window exits.

The runner is launched as a separate process so the application can remain open
after its originating chat turn completes. No generated program is executed as
a host process. The iframe and CSP are the security boundary for generated
content; this is not presented as a general-purpose hostile-code sandbox.

## Data Flow

### New application

1. The user says, "I need a calculator."
2. The skill catalog deterministically activates `application-builder`.
3. The router selects the explicitly configured agent provider.
4. The model calls `application.search`.
5. No matching manifest is found.
6. The model calls `application.create`, then writes one self-contained HTML
   document.
7. The model calls `application.publish`; AIOS validates and hashes the app.
8. The model calls `application.launch`.
9. The runner opens the calculator in a Chromium application window.
10. Chat reports the application name and cache location.

### Cached application

1. The skill activates and calls `application.search`.
2. AIOS returns the best published match.
3. The model calls `application.launch`.
4. No files are regenerated.

### MCP tool

1. The tool host starts an explicitly configured MCP server on first use.
2. The client negotiates the protocol and lists its tools.
3. Only allowlisted tools are advertised to the model.
4. A structured model call is forwarded as `tools/call`.
5. Bounded, supported result content is marked untrusted and returned to the
   model.

## Error Handling

- Invalid skill metadata skips that skill and records a user-safe catalog warning.
- A requested invalid skill produces a clear activation error.
- Tool-host startup failure fails the agent turn without falling back to prose
  execution.
- MCP protocol errors identify the server and operation without exposing stderr,
  environment values, credentials, or raw provider diagnostics.
- Application draft failures do not publish a partial manifest.
- Launch failure leaves the valid cached application intact for retry.
- Remote agent authentication and rate-limit errors reuse sanitized provider
  messages.
- An incomplete or malformed tool call executes nothing.

## Testing

Backend tests will cover:

- Agent Skills parsing, precedence, validation, explicit activation, trigger
  activation, progressive disclosure, and allowed-tool filtering.
- Agent provider configuration, secret preservation/redaction, URL validation,
  and remote-preferred routing.
- Generic tool listing and dispatch for both Chat Completions and ChatGPT
  subscription adapters.
- MCP initialization, pagination, tool allowlists, calls, notifications,
  unsupported server requests, timeouts, oversized output, malformed stdout,
  and process cleanup using a local fixture server.
- Application path traversal and symlink rejection, atomic writes, size limits,
  manifest publication, cache ranking, iframe sandboxing, CSP headers, launch
  cleanup, and persistence.
- A scripted end-to-end calculator turn that searches, creates, writes,
  publishes, and launches on a cache miss, followed by a cache-hit turn that
  performs no writes.
- Existing browser, voice, attachment, subscription, and core tests.

QML tests will cover loading and saving the agent provider fields without
displaying stored keys. Native shell compilation verifies the renamed tool-host
process integration.

## Documentation and Packaging

- Package the built-in skill directory with the Python application files.
- Document skill locations, the supported Agent Skills subset, MCP configuration,
  tool-name mapping, trust boundaries, agent provider selection, application
  cache behavior, and current limitations.
- Update the architecture, browser, ChatGPT subscription, and QA status
  documentation where behavior changes.
- Add no new runtime language or agent-framework dependency.

## Success Criteria

1. With a remote agent provider configured, "I need a calculator" creates,
   publishes, and opens a working offline calculator when no cache entry exists.
2. Repeating the request launches the cached calculator without rewriting it.
3. Ordinary local chat does not contact the agent provider.
4. An enabled fixture MCP server exposes only allowlisted tools and completes a
   real initialize/list/call sequence.
5. Local, remote OpenAI-compatible, and ChatGPT subscription transports use the
   same validated tool definitions and dispatcher.
6. Model prose, malformed tool calls, and unapproved MCP tools execute nothing.
7. Existing browser behavior and process cleanup remain intact.
