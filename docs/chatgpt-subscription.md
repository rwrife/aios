# ChatGPT subscription

In Settings → AI models, select **ChatGPT subscription**, then **Sign in**.
The sign-in browser must run inside AIOS for its localhost callback to work.
For a VM, **Use a code** lets you open the displayed address on another computer
or phone. Enter the code and finish sign-in there. Device authorization may need
to be enabled for your account/workspace. **Cancel** stops a pending sign-in.

Select an available model (or Automatic), then **Save**. **Refresh** reloads the
account, available models, and usage/reset information. Sign-in itself does not
change your selected provider. **Sign out** removes AIOS's saved credentials;
finish active subscription chats first. Each chat window has its own history.

This consumes the account's Codex subscription allowance, not API-key credit.
Models and limits depend on the account and workspace. There is no automatic
fallback to paid API access. Speech remains a separate local/remote setting.
AIOS does not collect your password or copy credentials from another Codex app.

Credentials are managed/refreshed by Codex in
`$XDG_CONFIG_HOME/aios/codex` (normally `~/.config/aios/codex`), inside a
user-only directory. Installed systems retain sign-in; live sessions lose it
on reboot unless their home directory is persisted. Treat this directory as
sensitive. Account data and sign-in links are not written to conversations.

CLI equivalents:

```sh
aios-llm subscription login --device
aios-llm subscription status
aios-llm configure --mode chatgpt
aios-llm chat 'Hello'
aios-llm subscription logout
```

## Implementation

The image includes Codex 0.153.4, using checksum-verified upstream musl binaries
for x86_64/aarch64 and the upstream license. `scripts/stage-codex.py` records the
version and checksums. The Python adapter speaks JSON-RPC over private stdio;
there is no listening AIOS proxy. Each worker owns a server process, with Linux
parent-death cleanup and bounded waits. Stop/close terminates that operation.
Authentication mutations take an exclusive lock; chats hold a shared lock.

Each request creates an ephemeral thread, seeds the current window's prior
messages through `thread/inject_items`, and streams one turn. Codex does not
retain a second durable conversation. Browser observations are retained only
within that turn, matching AIOS's existing agent. Previous tools are not replayed.
The runtime runs in an empty temporary working directory with no execution
environments, disabled host tools, read-only sandbox and no approval escalation.
AIOS supplies only the same validated, filtered dynamic tools used by the
OpenAI-compatible agent path. These can include built-in browser/application
functions and user-approved MCP tools; Codex's own shell, browser, MCP, skills,
web search, and other host tools remain disabled. Other client requests are
rejected.

Deterministic trigger activation and explicit `/skill-name` activation happen
before the ephemeral thread starts, so those skill instructions and permissions
are present immediately. If the model calls `activate_skill` during a ChatGPT
turn, its tool permissions narrow at once, but its newly loaded instructions
take effect on the next user turn because the running thread's base instructions
are not rewritten mid-turn. See [agentic tools](agentic-tools.md).

The dynamic-tool and history-injection interfaces are experimental. Upgrade the
pinned binary only after checking generated schemas and running the subscription
tests. See [App Server](https://learn.chatgpt.com/docs/app-server) and
[authentication](https://learn.chatgpt.com/docs/auth).

## Release checks

Run `scripts/test.sh`, the Qt tests, and an Alpine shell build. The optional
`AIOS_CODEX_SMOKE=1` test exercises the real pinned binary with a fresh, signed-out
home, without using personal credentials. Before release, exercise browser and
device login with an eligible test account in a booted image, then check streaming,
multi-turn context, two separate windows, browser actions, Stop, account limits,
logout, and reboot persistence. A network/login test requires user interaction.
