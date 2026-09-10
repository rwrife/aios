# Multiple desktop sessions and per-chat application taskbars

Status: proposed implementation plan; this PR changes documentation only.

## Outcome

Users can run several independent conversations and background jobs at once,
identify each by a named desktop orb, and return to any session directly. Each
chat shows its owned applications and background processes below the input so
minimized applications always have a restore path. Closing a session ends its
work and all owned applications without affecting other sessions.

## Existing implementation and gaps

- `apps/shell/Main.qml` creates independent `Backend` instances and chat windows,
  but one launcher restores a last-minimized stack before creating another chat.
- `apps/shell/main.cpp` owns per-chat UUIDs, histories, workers, voice and tool
  hosts. `closeSession()` calls `stop()` and disposes the backend; tool-host
  shutdown already participates in child cleanup. Shared model infrastructure
  must remain outside individual session ownership.
- `apps/shell/ChatWindow.qml` reports minimize/close, but has a fixed title and
  no application list. `ChatOrb.qml` is currently a launcher, not a session model.
- `apps/aios/toolhost.py` and `apps/aios/browser.py` own adapters and browser
  launch/cleanup. Browser process groups and socket registrations are useful
  foundations, not a complete desktop application/window registry.
- `SessionControl.h` creates per-chat greeting controls. Ordinary greeting
  accounts do not provide protected workspace ownership. The optional broker in
  `sessions.py`/`sessiond.py` has separate identity and display isolation rules;
  it must not be treated as an already concurrent multi-account desktop.

This plan supersedes the launcher behavior in the September 8 minimized-chat
design when implemented. Existing documentation continues to describe shipped
behavior until then.

## Interaction decisions

1. The prominent orb represents the primary session. Create that session lazily
   on first activation; after creation, activation always opens/restores it.
   Provide a separate accessible **New session** action even when chats are
   minimized. Closing the primary session resets its orb to the empty launcher;
   primary status grants no additional permissions.
2. Every additional live session gets its own orb, including sessions started in
   the background by an authorized process. Keep orbs visible while their chats
   are open or minimized, with a quiet active indicator. Activation focuses or
   restores only the selected session and never creates a duplicate backend.
3. Use descriptive names such as **Research trip** or **Build image**, with stable
   friendly fallbacks such as **Otter · 2**. Allow user renaming. A bounded title
   may be suggested from the first request or supplied by the launching adapter;
   user names take precedence and automatic names should not continually change.
   Duplicate labels receive a stable visible suffix, never an account identifier.
4. Show the same name in the orb label, accessible name and chat title. Show
   idle, working, needs attention and failed states with text/icon cues, not color
   alone. Do not continuously animate busy orbs. Truncate visually but expose
   the complete title on hover and keyboard focus.
5. Arrange session orbs in a bounded desktop strip near the primary orb. Use
   scrolling/overflow with keyboard access rather than shrinking indefinitely
   or overlapping Settings and power controls. Preserve order through minimize,
   restore and title changes. Reflow safely on small screens or monitor changes.
6. Below the composer, show a taskbar only while user-facing child applications
   or jobs exist. Each item shows an icon, short title and state. Selecting a
   window item restores/raises it; an explicit minimize action hides it. For an
   application with several windows, offer a window picker. Background jobs show
   status/details and supported cancel actions, never a false restore button.
   Group implementation helper processes beneath their owning application.
7. Minimizing a chat keeps its jobs running and leaves child windows unchanged.
   Minimizing an application keeps its taskbar entry. Closing an application
   removes its live entry after confirmed exit; failures remain dismissible with
   a useful error. Closing a chat means **End session**, including its children;
   communicate this in the close control's accessible description/tooltip.

## Session and account model

Introduce a shell-owned `SessionRegistry`/list model, independent of QML window
lifetimes. A record contains opaque immutable `sessionId`, title/title source,
primary flag, creation order, work state, presentation state, optional
`parentSessionId`, account associations and references to its backend and window.
Use separate work states (`idle`, `running`, `needs_attention`, `failed`,
`closing`, `closed`) and presentation states (`not_open`, `visible`, `minimized`).
Minimization is never cancellation. Create a record before starting work, roll
back failed creation, and retain a closing record until cleanup is confirmed.

Allow zero, one or multiple account associations per session. Associations are
labels/context, not permissions or a merged identity. Record an explicit active
account context for each protected operation; never union account capabilities.
Start anonymous by default. Linking, unlinking or choosing an account must use
the native profile flow and cannot silently move history to another owner.
Another account association does not grant that account access to prior messages
or artifacts. Shared ownership/history sharing requires a separate explicit
access design; this feature must not claim to provide it.

Keep local greeting profiles clearly distinguished from protected broker
principals. Broker-backed work requires operation-scoped authorization in that
chat. Creating a related session transfers neither authentication nor secrets;
account hints may be copied as unverified metadata only. Concurrent protected
execution across principals is a later gated phase, requiring broker-owned
workers, per-principal storage/display isolation and explicit revocation tests.
The ordinary desktop phase supports concurrent anonymous/personalized sessions.

Persist names, ordering and permitted account metadata beside versioned session
history with existing owner-only permissions. Do not persist credentials or live
process/window handles. After restart, show retained sessions as stopped history;
never silently restart jobs or reconstruct authentication.

## Child applications and process ownership

Add a trusted application supervisor and an `ApplicationRegistry` model. Each
logical launch gets an opaque `applicationId`, owning `sessionId`, allowlisted
adapter kind, display title, lifecycle state, supported actions, and zero or more
opaque window IDs. Keep process IDs, start identities and containment handles
internal; never use a title or a reusable PID alone as proof of ownership.

Route browser, terminal and other supported launches through this supervisor,
including launches requested by tools. Register before spawning, transition on
confirmed start/exit, and reconcile window appearance/disappearance separately
from process lifetime. Observe all descendants through a session containment
scope; use cgroup-backed teardown where available. Document any fallback's
limitations and do not claim detached-process cleanup until verified. Do not
adopt unrelated desktop applications based on matching titles or PID guesses.

Use fixed trusted window operations for native browser/terminal windows and the
embedded display adapter for protected surfaces. Validate every operation against
session ownership and current handle generation. X11 window metadata alone is
not a security boundary; protected accounts retain their display gate. Unsupported
adapters report `unavailable` rather than exposing a control that does nothing.
Browser minimize chrome ships only alongside a verified taskbar restore path.

Expose bounded snapshot/change events to the shell over a local, authenticated
control channel. Include sequence/generation numbers and full resync after a
disconnect. Use explicit results for started, exited, stale handle, denied,
unavailable and failed. Do not add arbitrary execution, browser JavaScript,
selectors, filesystem access or TCP control. Any model-facing session/application
operations use an allowlist and opaque handles scoped to the caller.

Closing a session first rejects new launches and child-session creation, cancels
workers/audio, requests graceful application shutdown, then escalates within a
bounded timeout to terminate its containment scope. Remove browser registrations,
sockets and temporary resources and only then remove the live orb. Cleanup must
be idempotent across duplicate close, failed startup, worker crash and shell exit.
Avoid blocking the UI thread during graceful waits. Shared model services survive
until their desktop-wide owner shuts down.

Related process-created sessions are owned child sessions by default: closing
the parent recursively ends them. Prevent cycles and enforce configurable count,
depth and resource limits. An explicit user action may make a child independent;
creation metadata remains for context, but ownership changes atomically before
parent teardown. Ordinary user-created sessions are independent. No automatic
detachment or reparenting during close races.

## Implementation sequence

### 1. Session lifecycle foundation

- Extract lifecycle coordination from `main.cpp` into a testable registry while
  retaining existing per-chat backends and history behavior.
- Replace `Main.qml`'s minimized stack with lookup/create/activate/close by ID.
- Add primary-session behavior, naming, state notifications and rollback when
  QML window creation fails. Update preview/screenshot code that matches the
  fixed `AIOS Chat` title to identify windows without relying on display names.
- Verify two sessions can run and close independently before adding UI polish.

### 2. Named desktop orbs

- Add session orb delegates and overflow layout using `ChatOrb.qml` and Theme.
- Bind names, state, focus and window visibility to the registry; add New session
  and Rename actions and keyboard traversal.
- Update `tests/qml/tst_chat_launcher.qml` to assert targeted restore and explicit
  creation instead of last-minimized ordering. Cover duplicate names and overflow.

### 3. Application supervision and control

- Add the application model and trusted control protocol; integrate toolhost,
  browser and terminal launch adapters and all existing launch entry points.
- Implement process containment, window registration, reconciliation, bounded
  teardown and per-session authorization of control requests.
- Add display-independent protocol tests for malformed messages, foreign IDs,
  stale generations, out-of-order events, startup failure and cleanup races.

### 4. Per-chat taskbar and browser minimize

- Add `SessionTaskbar.qml` below the input in `ChatWindow.qml` and register new
  components in `apps/shell/CMakeLists.txt`.
- Implement window restore/minimize, multiple-window selection, background job
  details, overflow and exit/failure updates. Preserve composer focus and scrolling.
- Enable native browser minimize only after its own registration and restore
  capability are confirmed. Preserve minimal browser chrome and per-chat profiles.

### 5. Process-created sessions and account associations

- Add scoped create-session operations through the trusted supervisor; require
  idempotency keys, bounded titles, ownership validation and resource limits.
- Create a visible orb immediately without stealing focus, register its parent,
  and support attention state and explicit user detachment.
- Add account association UI and persistence with the separation described above.
  Test independent greetings and revocation; leave protected concurrent accounts
  disabled until the broker/isolation prerequisites pass their release gates.
- If exposed through OS control, update `apps/skills/os-control/SKILL.md`, the
  `os_settings` schema and `docs/agentic-tools.md` together with values, readback,
  persistence and error semantics.

### 6. Batched integration and release validation

- Update `docs/voice-and-sessions.md`, `docs/specs/chat-desktop.md`,
  `docs/identity-sessions.md` and `docs/qa/chat-test-matrix.md` to shipped behavior.
- Run backend/protocol tests without a display server. Add behavioral QML checks
  for targeted restore, no duplicate sessions, taskbar visibility, window exit,
  minimize, rename, keyboard navigation and failed creation rollback.
- Batch a local WSL build and real Alpine QEMU validation with a unique VM title;
  wait for boot completion. Do not build ISO images through GitHub.
- Capture Ocean and one generated-palette screenshots with zero, one and many
  sessions; include taskbar overflow, multiple windows, background work and small
  screens. Check reduced motion, accessible labels, immediate focus tooltips,
  four-pixel spacing, Theme colors and existing border/window sizing rules.
- In the real sandbox-enabled Alpine browser, verify loading, text input,
  navigation, scrolling, minimize/restore, two concurrent isolated chats and
  cleanup. Exercise terminal windows, forked descendants, orphan recovery,
  toolhost crashes and closing a parent while a child is starting.

## Acceptance scenarios

| Scenario | Required result |
| --- | --- |
| Minimize A while B works | Both named orbs remain; selecting A restores only A; B continues. |
| New session while A is minimized | A remains intact; a distinct session/orb is created. |
| Background process creates C | One C orb appears without focus theft; retries do not duplicate it. |
| A opens browser and terminal | A's taskbar lists both; B's lists neither. |
| Minimize either application | Its taskbar entry restores the correct existing window. |
| Application has no window | Status is visible; restore is absent. |
| Close A | A's descendants, registrations and owned child sessions end; B and shared services survive. |
| Launch races with close | Launch is rejected or included in teardown; no orphan survives. |
| Account links differ across chats | Greetings/context remain separate; capabilities never transfer. |
| Shell restarts | Saved names/history survive; jobs are stopped and authentication is cleared. |
| Many sessions or failed control adapter | All sessions remain reachable; unsupported actions are explicit. |

Ship the visible multi-session/taskbar milestone only when phases 1–4 and their
relevant release checks pass together. Phase 5 completes process-created sessions
and account associations. Concurrent protected multi-principal execution remains
an explicit follow-on dependency, not an implied capability of multiple orbs.
