# Agent-controlled browser

Chromium and the matching Alpine ChromeDriver package are included in the image.
There is no browser launcher on the desktop or in the Openbox menu. Chromium's
application entry is hidden for the desktop user. Ask chat to open a website.
Normal Chromium tabs, address bar and navigation controls remain available once
it opens; a minimal browser frame is future work. This is a desktop workflow,
not an operating-system prohibition on launching a binary from the terminal.

The desktop's Chat Completions agent advertises a browser function to the chosen
local or remote model. Use a model/provider that supports structured tool calls.
The 135M starter model is for simple chat and is not a reliable browser agent.
The local server enables Jinja tool templates and an 8192-token context. Models
whose GGUF templates do not support tools may need a different model/template.
The CLI's plain chat command remains text-only.

Supported actions: open a page/new tab, navigate, read a page snapshot, click or
fill observed controls, press Enter/Tab/Escape, scroll, go back/forward, list and
switch tabs, and close the browser. Snapshots include bounded visible text and
up to 40 labeled controls. IDs expire after the next snapshot. There is no raw
JavaScript, shell-command, local-file navigation, file-upload or screenshot tool.
Canvas-only applications and complex nested frames are not covered yet.

Each chat owns a private local socket and a lazily opened browser with a temporary
profile. Browser state survives between messages in that chat; other chats have
separate cookies and tabs. Closing the chat or pressing Stop terminates its
browser service and removes its temporary profile. Closing Chromium manually
may require asking the agent to close and reopen its browser session.

Chromium runs as the ordinary desktop user with its sandbox enabled. The private
broker accepts only fixed operations, and ChromeDriver permits loopback clients.
Web content is marked untrusted and the agent is instructed to follow the user's
task rather than page instructions. The tool loop executes only completed,
structured calls (never model prose), with at most eight rounds and four calls
per round. Page content sent to a remote model goes to the configured model
provider, like other chat content. There is no microphone/wake-word integration.

On installed systems Chromium must receive security updates through Alpine's
package updates. Live images need rebuilding to pick up newer Chromium packages.
Allow additional memory for Chromium alongside local models; 6 GiB or more is a
better starting point for combined use than the minimal offline boot test.

Protocol references: [Chat Completions function calling](https://developers.openai.com/api/docs/guides/function-calling),
[WebDriver](https://www.w3.org/TR/webdriver2/), and
[ChromeDriver security considerations](https://developer.chrome.com/docs/chromedriver/security-considerations).

Developer smoke test (opens Chromium against a temporary local fixture only):
`PYTHONPATH=apps python3 scripts/test-browser.py` from the repository in an X11
session as a normal user. Backend protocol tests run with `bash scripts/test.sh`.
