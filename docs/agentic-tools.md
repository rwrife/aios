# Agentic tools

AIOS can give a tool-capable model a bounded set of structured capabilities.
There are three related pieces:

- **Agent Skills** are installed instructions and metadata that describe how to
  handle a class of requests. A skill can narrow which tools are available.
- **Built-in tools** are AIOS-owned functions. The current registry includes
  `browser` and `application`.
- **MCP tools** come from explicitly configured local Model Context Protocol
  servers. AIOS validates and renames them before advertising them to a model.

Model text is never interpreted as a command. Only a completed structured call
to a tool advertised for that turn can reach the per-chat tool host.

## Agent Skills

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
2. Reuse an exact match when available; otherwise consider keyword matches.
3. If no suitable app exists, create a draft, write one self-contained offline
   `index.html`, publish its metadata and digest, and launch it.
4. Report success only after launch returns `launched: true`.

Published applications are stored under:

```text
$XDG_DATA_HOME/aios/applications/<application-id>/
```

Each published directory contains only `index.html` and `manifest.json`.
Documents are limited to 16 KiB and must not depend on remote scripts, styles,
fonts, media, packages, or extra asset files. Search ranks exact normalized
request matches first, then title and keyword overlap.

The runner verifies the manifest and SHA-256 digest, serves an AIOS wrapper and
the app from a temporary loopback HTTP server, and opens Chromium in app mode.
The generated document runs in a sandboxed iframe with a restrictive Content
Security Policy. Chromium keeps its normal process sandbox, blocks non-loopback
name resolution for the app window, and uses a temporary profile.

This is not arbitrary application generation. The model cannot run host shell
commands, install dependencies, write outside the application cache, or create
backend services or native applications. Those capabilities require a future
approval UI and stronger build sandbox.

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

- Application generation is single-file, offline browser content only.
- MCP is stdio/tools-only and has no installation or marketplace UI.
- The bundled 135M starter model is not a reliable planner or app builder.
- Provider, model, and third-party MCP behavior varies and needs an explicit
  release test matrix.
- Full ISO validation, real paid-provider coverage, and real configured
  third-party MCP coverage remain release work unless separately recorded in
  the implementation evidence.

The repository integration test uses a real worker, agent, Unix-socket tool
host, application store, and loopback OpenAI-compatible server. It verifies a
calculator build followed by exact cache reuse without Chromium or a paid model.
