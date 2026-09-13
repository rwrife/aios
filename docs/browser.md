# Agent-controlled browser

AIOS includes a private Qt WebEngine browser with Chromium rendering and an
AIOS-native window. Chromium's stock tabs, omnibox, menus, profile controls and
extension UI are not exposed. The browser bar contains only a web address,
Back, Refresh and Stop. Its colors follow the selected desktop theme, and its
one-pixel outline uses the same translucent line treatment as the XMB
background contours.

There is no browser launcher on the desktop or in the Openbox menu. Ask chat to
open a website. A browser is created lazily for that chat and remains available
between messages until the chat is stopped or closed. The browser has one page,
not user-visible tabs. When a user asks to open a generic name that matches no
document or application ("open cnn", "open amazon"), the agent convention is to
treat it as a website request and open https://www.<name>.com, searching first
when the domain is ambiguous. "Click on <label>" or "go to <label>" means to
snapshot the page and click the control whose text matches that label. Plain
"scroll down/up" scrolls about 300 pixels by default; an optional bounded
whole-pixel `amount` (1-2000) overrides the distance.

Browser is one built-in in the shared per-chat tool registry, alongside the
application tool and explicitly approved MCP tools. The provider-neutral agent
advertises the filtered registry to the chosen local, remote, or ChatGPT model.
Use a model/provider that supports structured tool calls.
The bundled Qwen3 0.6B starter is for simple chat and tool bootstrap, but is not
a reliable browser agent. Critical setup controls invoke fixed backend actions
directly and do not depend on model-generated tool calls.

Supported actions are open or navigate, take a semantic snapshot, click or type
into an observed control, press Enter/Tab/Escape, scroll, go back or forward,
refresh, stop, query the single page handle, and close the browser. Snapshots
include bounded visible text and up to 40 labeled controls. IDs expire after the
next snapshot. There is no arbitrary JavaScript, selector or XPath input, shell
command, local-file navigation, file upload, download, remote debugging, or
screenshot tool. Canvas-only applications and cross-origin frames are not
covered yet.

Each chat owns a private local tool-host socket and a lazily opened browser with
a temporary off-the-record WebEngine profile. The tool host launches
`aios-browser` with the chat's theme and session identifier, then communicates
through an owner-only Unix socket in a private temporary directory. Browser
state survives between messages in that chat; other chats have separate
profiles and pages. Closing the chat or pressing Stop terminates its tool host,
browser, and MCP children and removes the browser's profile and runtime
registration. If the window is closed manually, the next `open` action starts a
fresh browser.

Qt WebEngine and its Chromium renderer run as the ordinary desktop user with
the renderer sandbox enabled. Browser permissions, downloads,
certificate exceptions, clipboard access, screen capture and fullscreen
requests are denied by default. A link click that asks for a new tab
(`target=_blank`/`_new`) is redirected to the same single private page instead
of being silently dropped, so a link click is never a dead end; `window.open`
popups stay denied. The shared tool host accepts only validated
registry operations. Web content is marked untrusted and the agent is instructed
to follow the user's task rather than page instructions. The tool loop executes
only completed structured calls (never model prose), with at most eight rounds
and four calls per round. Page content sent to a remote model goes to the
configured model provider, like other chat content. There is no
microphone/wake-word integration.

Skills, MCP tools, routing, and shared limits are documented in
[agentic tools](agentic-tools.md).

## Local skill and MCP control

The same bounded protocol used by chat is available to local tools running as
the desktop user. Active browsers publish private descriptor files under:

```text
$XDG_RUNTIME_DIR/aios/browsers/
```

Each descriptor contains a protocol version, session ID, Unix socket path,
process ID and action allowlist. Descriptor files and sockets are owner-only.
Python integrations can use:

```python
from aios.browser import call, discover

browser = discover()[0]
snapshot = call(browser["socket"], {"action": "snapshot"})
```

Requests and responses are one compact JSON object per line. Integrations must
use only an advertised action and element IDs from the latest snapshot. The
socket is intentionally per-chat and local-only; no TCP listener or reusable
authentication token is created.

Do not set `QTWEBENGINE_DISABLE_SANDBOX` or pass `--no-sandbox`.
Installed systems receive browser-engine fixes through Alpine's
`qt6-qtwebengine` updates; live images need rebuilding to pick up newer
packages. Allow additional memory for the browser alongside local models.

Developer smoke test, from an X11 session with `aios-browser` installed:

```sh
PYTHONPATH=apps python3 scripts/test-browser.py
```

Backend protocol tests run with `bash scripts/test.sh`.
