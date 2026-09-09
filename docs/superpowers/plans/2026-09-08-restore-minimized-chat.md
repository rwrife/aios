# Restore Minimized Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the desktop chat blob restore minimized chats from newest to oldest before creating a new independent chat.

**Architecture:** `Main.qml` will own a unique most-recently-minimized stack and keep session creation behind `openChat()`. `ChatWindow.qml` will emit lifecycle signals from real window visibility and close events, allowing `Main.qml` to track operating-system minimization as well as the custom button. QML tests will instantiate the real components with fake backend/session objects.

**Tech Stack:** Qt 6 QML, Qt Quick Controls, Qt Test `qmltestrunner`, Markdown documentation

---

## File Map

- Create `tests/qml/tst_chat_launcher.qml`: behavioral regression coverage for the orb and chat-window lifecycle.
- Modify `apps/shell/ChatOrb.qml`: remove only the visible tooltip declarations.
- Modify `apps/shell/ChatWindow.qml`: emit minimized and removal lifecycle signals.
- Modify `apps/shell/Main.qml`: inject testable service references, track minimized windows, restore them, and create sessions only as fallback.
- Modify `README.md`, `docs/specs/chat-desktop.md`, `docs/voice-and-sessions.md`: describe current launcher behavior.
- Modify `docs/qa/chat-test-matrix.md`, `docs/qa/implementation-status.md`, and `docs/plans/finish-line.md`: remove current-behavior claims that every click always creates a session while preserving historical evidence.

### Task 1: Add Failing QML Regression Tests

**Files:**
- Create: `tests/qml/tst_chat_launcher.qml`

- [ ] **Step 1: Write the failing behavioral tests**

Create this QML test with fake backend, session, and session-control objects. It
instantiates the real `ChatOrb`, `Main`, and `ChatWindow` components:

```qml
import QtQuick
import QtQuick.Controls
import QtQuick.Window
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "ChatLauncher"
    when: windowShown
    width: 900
    height: 750

    Theme { id: theme; selected: "blue" }

    Component {
        id: sessionComponent
        QtObject {
            property int number: 0
            property var messages: []
            property bool busy: false
            property bool recording: false
            property bool speaking: false
            property var attachments: []
            property string status: ""
            signal changed()
            signal transcribed(string text)
            function closeSession() {}
            function send(text) {}
            function copy(text) {}
            function readReply(text) {}
            function removeAttachment(index) {}
            function setVoiceActive(active) {}
            function cancelRecording() {}
            function stop() {}
            function attach(file) {}
        }
    }

    QtObject {
        id: backend
        property var config: ({
            theme_color: "blue",
            reduced_motion: true,
            mode: "local",
            model_path: "/starter.gguf",
            voice_mode: "remote",
            voice_url: "",
            live: false
        })
        property int sessionsCreated: 0
        property var createdSessions: []
        property bool busy: false
        property bool configuring: false
        property string status: ""
        property var subscription: ({})
        property string loginUrl: ""
        property string loginCode: ""
        signal configured()
        function createSession() {
            sessionsCreated += 1
            var session = sessionComponent.createObject(test, {number: sessionsCreated})
            createdSessions = createdSessions.concat([session])
            return session
        }
        function terminal() {}
        function power(action) {}
    }

    QtObject {
        id: sessionControl
        property bool enabled: false
        property bool shield: false
        property bool simulator: false
        property string authority: ""
        property var sessions: []
        property string error: ""
        property var challenge: ({})
        function simulate(value) {}
        function activate(title) {}
        function suspend() {}
        function search() {}
        function resume(id) {}
        function launch(app) {}
        function protectedResource() {}
        function cancelChallenge() {}
        function verify(pin) {}
    }

    Component { id: orbComponent; ChatOrb {} }
    Component { id: desktopComponent; Main {} }
    property var desktops: []

    function init() {
        backend.sessionsCreated = 0
        backend.createdSessions = []
        desktops = []
    }

    function cleanup() {
        for (var i = 0; i < desktops.length; ++i)
            desktops[i].destroy()
        for (var j = 0; j < backend.createdSessions.length; ++j)
            backend.createdSessions[j].destroy()
        wait(20)
    }

    function createDesktop() {
        var desktop = desktopComponent.createObject(null, {
            backendApi: backend,
            sessionControlApi: sessionControl
        })
        verify(desktop !== null)
        desktops.push(desktop)
        wait(50)
        return desktop
    }

function test_orb_keeps_accessibility_without_visual_tooltip() {
    var orb = orbComponent.createObject(test, {theme: theme, reducedMotion: true})
    verify(orb !== null)
    compare(orb.Accessible.name, "Start a new chat")
    compare(orb.Accessible.description, "Open a new conversation")
    orb.forceActiveFocus()
    tryCompare(orb, "activeFocus", true)
    wait(950)
    compare(orb.ToolTip.visible, false)
    orb.destroy()
}

function test_restores_minimized_chats_newest_first() {
    var desktop = createDesktop()
    var first = desktop.openChat()
    var second = desktop.openChat()
    compare(backend.sessionsCreated, 2)

    first.showMinimized()
    tryCompare(first, "visibility", Window.Minimized)
    tryCompare(desktop, "minimizedChatCount", 1)
    second.showMinimized()
    tryCompare(second, "visibility", Window.Minimized)
    tryCompare(desktop, "minimizedChatCount", 2)

    compare(desktop.openChat(), second)
    compare(backend.sessionsCreated, 2)
    verify(second.visibility !== Window.Minimized)
    compare(desktop.openChat(), first)
    compare(backend.sessionsCreated, 2)
    verify(first.visibility !== Window.Minimized)

    var third = desktop.openChat()
    verify(third !== first && third !== second)
    compare(backend.sessionsCreated, 3)
}

function test_closing_minimized_chat_removes_it() {
    var desktop = createDesktop()
    var window = desktop.openChat()
    window.showMinimized()
    tryCompare(desktop, "minimizedChatCount", 1)
    window.close()
    tryCompare(desktop, "minimizedChatCount", 0)
    desktop.openChat()
    compare(backend.sessionsCreated, 2)
}
}
```

- [ ] **Step 2: Run the new test and confirm the red state**

Run:

```powershell
docker run --rm -v "${PWD}:/workspace" -w /workspace alpine:3.23 sh -lc "apk add --no-cache qt6-qtdeclarative-dev qt6-qtmultimedia >/dev/null && QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software /usr/lib/qt6/bin/qmltestrunner -input tests/qml/tst_chat_launcher.qml"
```

Expected: FAIL because the existing orb tooltip becomes visible and `Main.qml`
does not yet expose `backendApi`, `sessionControlApi`, or minimized-chat tracking.

### Task 2: Implement Minimized-Chat Lifecycle

**Files:**
- Modify: `apps/shell/ChatOrb.qml`
- Modify: `apps/shell/ChatWindow.qml`
- Modify: `apps/shell/Main.qml`
- Test: `tests/qml/tst_chat_launcher.qml`

- [ ] **Step 1: Remove the orb's visible tooltip**

Delete only these lines from `ChatOrb.qml`; retain both `Accessible` properties:

```qml
ToolTip.visible: hovered || activeFocus
ToolTip.text: "New chat"
ToolTip.delay: 900
```

- [ ] **Step 2: Emit lifecycle signals from the real chat window**

Add signals and connect them to actual `Window` state:

```qml
signal minimized()
signal removed()
onVisibilityChanged: {
    if (visibility === Window.Minimized)
        minimized()
}
onClosing: {
    removed()
    session.closeSession()
    Qt.callLater(chat.destroy)
}
```

This replaces the existing one-line `onClosing`; the minimize button remains
`chat.showMinimized()` so both that button and operating-system controls flow
through `onVisibilityChanged`.

- [ ] **Step 3: Add explicit tracking and restore behavior to `Main.qml`**

Add injectable aliases for production context objects, a unique stack, and the
tracking functions:

```qml
property var backendApi: typeof backend === "undefined" ? null : backend
property var sessionControlApi: typeof sessionControl === "undefined" ? null : sessionControl
property var minimizedChats: []
readonly property int minimizedChatCount: minimizedChats.length

function removeMinimizedChat(window) {
    var remaining = []
    for (var i = 0; i < minimizedChats.length; ++i) {
        if (minimizedChats[i] !== window)
            remaining.push(minimizedChats[i])
    }
    minimizedChats = remaining
}

function trackMinimizedChat(window) {
    removeMinimizedChat(window)
    var updated = minimizedChats.slice()
    updated.push(window)
    minimizedChats = updated
}

function restoreMinimizedChat() {
    var pending = minimizedChats.slice()
    while (pending.length > 0) {
        var window = pending.pop()
        minimizedChats = pending.slice()
        if (!window || window.visibility !== Window.Minimized)
            continue
        window.showNormal()
        window.raise()
        window.requestActivate()
        return window
    }
    return null
}
```

Replace `openChat()` with:

```qml
function openChat() {
    if (sessionControlApi.enabled)
        return null
    var restored = restoreMinimizedChat()
    if (restored)
        return restored
    var window = chatComponent.createObject(desktop, {
        backend: backendApi,
        session: backendApi.createSession(),
        theme: theme
    })
    if (!window)
        return null
    window.minimized.connect(function() { desktop.trackMinimizedChat(window) })
    window.removed.connect(function() { desktop.removeMinimizedChat(window) })
    window.show()
    window.raise()
    window.requestActivate()
    return window
}
```

Use `backendApi` and `sessionControlApi` consistently throughout `Main.qml`, while
their defaults continue to use the production context properties.

- [ ] **Step 4: Run the focused QML test**

Run the Docker command from Task 1.

Expected: PASS for tooltip accessibility, minimize tracking, reverse-order
restoration, close cleanup, no extra session creation, and creation with visible
chats.

- [ ] **Step 5: Commit behavior and tests**

```powershell
git add -- apps/shell/ChatOrb.qml apps/shell/ChatWindow.qml apps/shell/Main.qml tests/qml/tst_chat_launcher.qml
git commit -m "Restore minimized chats from launcher" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 3: Update Current Behavior Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/specs/chat-desktop.md`
- Modify: `docs/voice-and-sessions.md`
- Modify: `docs/qa/chat-test-matrix.md`
- Modify: `docs/qa/implementation-status.md`
- Modify: `docs/plans/finish-line.md`

- [ ] **Step 1: Replace contradictory current-behavior wording**

State consistently that the blob restores minimized chats newest-first and starts
a new independent session only when no minimized chat remains. Keep visible chats
independent and allow additional chats to be created.

In QA history, preserve the r7 evidence that multiple sessions were independent,
but remove the implication that the current launcher always creates a new session.
Add a source-level QML regression-test entry without claiming a new ISO or manual
VM validation.

- [ ] **Step 2: Review the documentation diff**

Run:

```powershell
git --no-pager diff --check
git --no-pager diff -- README.md docs/specs/chat-desktop.md docs/voice-and-sessions.md docs/qa/chat-test-matrix.md docs/qa/implementation-status.md docs/plans/finish-line.md
```

Expected: no whitespace errors and no unrelated documentation changes.

- [ ] **Step 3: Commit documentation**

```powershell
git add -- README.md docs/specs/chat-desktop.md docs/voice-and-sessions.md docs/qa/chat-test-matrix.md docs/qa/implementation-status.md docs/plans/finish-line.md
git commit -m "Document minimized chat restore behavior" -m "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"
```

### Task 4: Publish and Validate

**Files:**
- No additional source changes expected

- [ ] **Step 1: Push the implementation branch**

```powershell
git push -u origin rwrife-restore-minimized-chat
```

- [ ] **Step 2: Create the pull request**

Create a PR against `main` summarizing minimized-chat restoration, lifecycle
tracking, tooltip removal, documentation updates, and QML regression coverage.

- [ ] **Step 3: Run the existing complete source validation**

Run:

```powershell
docker run --rm -v "${PWD}:/workspace" -w /workspace alpine:3.23 sh -lc "apk add --no-cache bash python3 py3-cryptography qt6-qtdeclarative-dev qt6-qtmultimedia >/dev/null && QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software /usr/lib/qt6/bin/qmltestrunner -input tests/qml && bash scripts/test.sh"
```

Expected: all QML and Python/shell/XML checks pass.

- [ ] **Step 4: Confirm final repository state**

Run:

```powershell
git status --short --branch
git --no-pager log --oneline main..HEAD
```

Expected: the branch tracks its remote, the worktree is clean, and commits are
limited to the design, plan, behavior/tests, and directly related documentation.
