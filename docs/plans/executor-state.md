# Executor State

Run-state artifact for the every-6-hours PR-first executor (repo: rwrife/aios).

## 2026-09-14 11:23 UTC

- Repository preflight: `gh repo view rwrife/aios`, REST repository lookup,
  `git ls-remote --heads origin`, and create/delete temporary ref probe succeeded.
  Authenticated identity: `gh api user --jq .login` -> `rwrife`.
- PR snapshot: zero open PRs initially and immediately before selection; zero
  after implementation merge. No pre-existing PRs merged and no blocked PRs.
  This snapshot excludes the separate docs-only publication of this state file.
- Issues: 28 open at selection and after implementation merge.
  Assigned elsewhere: https://github.com/rwrife/aios/issues/71 (`rwrife`),
  skipped entirely despite sharing this executor's GitHub identity.
- Claimed issue: https://github.com/rwrife/aios/issues/81 — App creation skill/prompt.
  Rationale: avoiding routine metadata and runtime-choice questions keeps the
  primary AI-only chat interaction moving without inventing OS capabilities.
- Claim evidence: `gh issue edit 81 --repo rwrife/aios --add-assignee @me`
  succeeded; `gh issue view 81 --repo rwrife/aios --json assignees` returned
  only `rwrife` at claim and immediately before push. Claims released: none;
  assignment retained because a PR exists, per this job's retention policy.
- New implementation PR: https://github.com/rwrife/aios/pull/120 — MERGED
  at `2026-09-14T11:23:02Z`, squash commit
  `aca1e95c489747a98ba75bfc36504c8a3ef5295b`.
  Readback confirmed MERGED and remote branch deletion. Issue #81 remains OPEN
  with `rwrife` assigned, intentionally linked with Progresses rather than a
  closing keyword.
- Implementation: application-builder instructions infer a short title and
  source request, choose schema-supported defaults, honor explicit requirements,
  disclose ephemeral Notes/game state, and preserve cache-first/verified-launch
  rules. Tool-schema descriptions reinforce metadata inference. Added one
  AgentSession instruction/schema regression test; docs state evidence limits.
- Review correction: an attempted implicit-trigger expansion matched quoted
  examples. It was withdrawn, its tests removed, and trigger frontmatter left
  byte-identical to main. Independent Codex review of all four final files
  returned `passed: true`, no security or logic blockers. Matcher unchanged.
- Verification:
  - New instruction/schema test first failed on missing guidance, then passed.
  - Final focused command:
    `PYTHONPATH=apps:tests python3 -m unittest test_skills test_browser_agent test_applications -q`
    ran 139 tests: zero failures, one optional native-binary skip.
  - `bash scripts/test.sh` ran 459 tests: one failure, nine skips, exit 1.
    Failure: `test_terminal_theme.test_desktop_launch_paths_use_the_themed_launcher`,
    `AssertionError: 2 != 1`, reproduced on untouched main before implementation.
    Wrapper stops at unittest, so subsequent shell/XML checks are not claimed.
  - Initial four-module run including `test_toolhost`: 175 tests, zero failures,
    one skip. Two later four-module reruns hit `ConnectionRefusedError: [Errno 111]
    Connection refused` in the unchanged drip-feed test. That test passed in
    full discovery and individually; untouched main's four-module command passed
    174 tests with one skip. Timing inconsistency recorded, not claimed as a
    reproduced main failure.
  - `git diff --cached --check` passed. No UI/native implementation changed.
  - No live-model or Alpine/QEMU evaluation performed. These tests verify prompt
    delivery and schema, not model adherence, generated app UX, or fewer stalls.
- CI: Validate is `disabled_manually`; no current-head PR runs/checks arrived.
  Branch protection lookup returned HTTP 403:
  `Upgrade to GitHub Pro or make this repository public to enable this feature.`
  This is a repository-plan limitation, not a write-auth blocker. Explicit merge
  used fresh local changed-scope verification, independent review, CLEAN /
  MERGEABLE status and exact-head matching; no auto-merge or ISO CI dispatch.
- Remaining #81 acceptance: live-model/VM behavior checks, offline framework
  packaging (Node.js/React/Three.js) with a safe expanded application contract,
  and any broader intent activation. No persistent storage or extra native
  templates are promised. Existing triggers or explicit/model skill activation
  remain necessary.
- Run-state publication: separate docs-only branch/PR after implementation
  outcome settled; worktrees are disposable and removed at closeout.
- Self-removal: not triggered; this job remains scheduled every six hours.

## 2026-09-14 00:50 UTC

- PR lane: 0 open PRs at start and at issue selection (freshness re-checked after merge: 0 again). No merges before issue work, no blocked PRs.
- Open issues: 27 at selection; all unassigned except #71 (rwrife, browser-mcp in-flight from merged PR #113 pending owner VM QA); #71 skipped as assigned-elsewhere. Hardware/recognition/voice stage chains left for device-capable runners.
- Selected issue: https://github.com/rwrife/aios/issues/82 — "Agent question/choice prompt".
  Rationale: the chat is the single interaction point; giving the agent a
  structured way to ask multiple-choice questions (and the chat a click-to-answer
  UX) removes the daily stall of vague "please specify" prompts and is fully
  verifiable headless via the QML harness.
- Claim: `gh issue edit 82 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push.
- Implementation (worktree `.worktrees/feat-issue-82-choices`):
  - `apps/aios/agent.py` POLICY: when genuinely blocked, ask one specific
    question and offer 2-6 short options in a ```Choose fence; when a reasonable
    default is clear, pick it and state the choice instead of asking.
  - `apps/shell/Markdown.js`: `splitChoices()` extracts the fence fail-closed —
    streaming/incomplete, single-option, >6-option, >120-char-label, or
    malformed blocks keep rendering as the ordinary code block, so a half-open
    block can never drop content mid-stream.
  - `apps/shell/ChatWindow.qml`: assistant replies route through
    `splitChoices()`; the newest reply's options render as theme-following
    `QuietButton`s (Theme.qml roles, 4px rhythm) whose click sends the label
    verbatim via `session.send()`; older/answered blocks stop offering buttons.
  - Test-harness fix: `collectNamed` now dedupes the children/data
    double-parenting of delegate items (recycled delegates double-counted).
  - `docs/qa/chat-test-matrix.md`: source-level row checked; real-QEMU click
    row left open.
- Verification (targeted QML-suite evidence, not VM green):
  - Alpine 3.23 Qt 6.10.3 `qmltestrunner` offscreen: `tst_chat_launcher`
    25 pass/0 fail (3 new regression tests: parser conservatism, click sends
    label, newest-reply-only). Re-run with the Sage palette: 25/25.
  - RED/GREEN: with `ChatWindow.qml`+`Markdown.js` stashed the new tests FAIL
    (`splitChoices` not a function / buttons never appear), restored → PASS.
  - Python: `test_browser_agent` 55/55 (new POLICY regression test); full
    discovery 458 tests with only the known baseline failure
    `test_terminal_theme.test_desktop_launch_paths_use_the_themed_launcher`
    (re-confirmed failing on untouched main this run).
  - Node behavior probe of `splitChoices` (19 assertions incl. tilde fences,
    list-marker labels, trailing-text retention, fail-closed cases): all pass.
  - Not verified: real Alpine/QEMU X11 session with a live model emitting the
    block and a human click (no display/VM on this runner); stated in the PR,
    and the issue stays open for that release QA.
- New PR: https://github.com/rwrife/aios/pull/118 — MERGED (squash commit 0dd4333de2ce465b8b85deaad42504d608327987, 2026-09-14T00:47:42Z). Issue #82 intentionally stays OPEN (Progresses linkage — in-VM/QEMU release QA of the click journey still pending); assignment released after merge so a VM-capable runner can finish that QA. Post-merge PR-lane re-check: 0 open PRs; remote branch deleted; worktree removed.

## 2026-09-13 20:07 UTC

- PR lane: 0 open PRs at start and at issue selection (freshness re-checked). No merges, no blocked PRs.
- Open issues: 28 at selection; all unassigned except #71 (rwrife, browser-mcp in-flight); #71 skipped as assigned-elsewhere. Other unassigned hardware/recognition/voice stage chains left for device-capable runners.
- Selected issue: https://github.com/rwrife/aios/issues/75 — "Opening another browser instance".
  Rationale: "open X" is a primary AI-only-OS command path; the agent claiming a
  browser is open after the user closed it breaks trust in the single interaction
  point, and the fix is fully verifiable headless via the real compiled browser.
- Claim: `gh issue edit 75 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push.
- Root cause: each chat's agent talks to its browser through the long-lived
  `Browser` client in `apps/aios/browser.py`, which tracked only "never opened"
  vs "process alive". When the user closed the window (process exits, or WM
  kills it), a later `navigate` died client-side with "Open the browser first."
  while the model — having seen a successful open earlier in the conversation —
  often just reported the browser as opened without calling any tool.
- Implementation (worktree `/home/rwrife/repos/aios-wt/issue-75`):
  - `apps/aios/browser.py`: a dead process is now a "closed window" state —
    `navigate` transparently relaunches the browser and sends `open` (so
    "open linkedin" reopens the page in a fresh window), other page actions
    raise "The browser window was closed…", and the tool description teaches
    the user can close the window at any time.
  - `apps/aios/agent.py` POLICY: never assume an earlier open is still on
    screen; report open only from the newest tool result; re-open via open/navigate.
  - `scripts/test-browser.py`: new QA journey — SIGTERM the browser process,
    assert snapshot reports "window was closed", assert `navigate` reopens the
    page. `docs/browser.md` updated.
- Verification (targeted native-execution + suite evidence, not VM green):
  - Real compiled `aios-browser` (fresh `origin/main` build, Alpine 3.23 Qt
    6.10.3 container): full `scripts/test-browser.py` GREEN with the fix
    client (all prior asserts + new closed-window journey PASS).
  - RED/GREEN: identical harness against main's client FAILs at the new
    assert with the old "Open the browser first." error.
  - Python suites: `test_browser_agent`+`test_toolhost`+`test_browser_shell`
    101/101 (3 new regression tests). Full `scripts/test.sh` 457 tests with
    only the known baseline failure
    `test_terminal_theme.test_desktop_launch_paths_use_the_themed_launcher`
    (pre-existing on main).
  - Not verified: real Alpine/QEMU X11 session with Chromium sandbox enabled
    (no display/VM on this runner); stated in the PR, and the issue stays open
    for that release QA.
- New PR: https://github.com/rwrife/aios/pull/116 — MERGED (squash commit 944f992902d7ee4c7e64eee55ffb5cc83be8bc4e, 2026-09-13T20:37:52Z). Issue #75 intentionally stays OPEN (Progresses linkage — in-VM/QEMU sandboxed release QA of the new closed-window journey still pending); assignment released after merge so a VM-capable runner can finish that QA. Post-merge PR-lane re-check: 0 open PRs; remote branch deleted; worktree removed.

## 2026-09-13 12:55 UTC

- PR lane: 1 open PR at start — https://github.com/rwrife/aios/pull/113 (#71 browser actions, authored by rwrife). It documented a never-executed native surface (`apps/browser/main.cpp` scroll path) and was held open under the native-surface gate. This run closed that gap: in an Alpine 3.23 Qt 6.10.3 Docker container the PR head compiled cleanly (`ninja aios-browser`), and the compiled binary was executed headless (`QT_QPA_PLATFORM=offscreen`, non-root, local HTTP server). Live probes: `scroll amount=300` → viewport.y==300; default up/down == 300 px; raw-socket probes bypassing the Python validator proved C++-side re-validation (5000/0/300.5 rejected with "whole-pixel…between 1 and 2000", boundary 1/2000 accepted). PR body updated with this evidence, then merged (squash 7e5b834dc3cc41969520fdcde398c283568f47fc, 2026-09-13T12:31:12Z). Issue #71 stays OPEN (Progresses linkage — in-VM/sandboxed release QA and live chat-phrasing observation still pending); assignment retained as in-flight lock.
- Freshness re-check after merge: 0 open PRs → issue lane unblocked.
- Open issues: 27 at selection; all unassigned except #71 (retained above); none skipped as assigned-elsewhere.
- Selected issue: https://github.com/rwrife/aios/issues/70 — "Browser links do not work".
  Rationale: broken links break the browser tool the AI uses to operate the
  web on the user's behalf — a core path of the AI-only OS UX.
- Reproduced first, headlessly: Alpine 3.23 Qt 6.10.3 offscreen container,
  main's compiled `aios-browser`, page with same-frame + `target="_blank"`
  links. Same-frame click navigated fine; the `_blank` click silently opened
  nothing (`createWindow` returned nullptr) while the pending click snapshot
  still returned the old page — matching the issue report ("clicking on links
  … appear to do nothing"). Probing also showed returning `this` from
  `createWindow` does NOT navigate (Qt discards it), which is why the shipped
  fix redirects anchor targets in the fixed element-action script instead.
- Claim: `gh issue edit 70 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push.
- Implementation (worktree `.worktrees/fix-issue-70-links`): the shared
  element-action prefix in `apps/browser/main.cpp` now rewrites anchor
  `target=_blank/_new` to `_self` before `element.click()`, so link clicks
  that would die against the popup deny continue in the single private page;
  `window.open`/popup requests remain blocked. Static regression tests in
  `tests/test_browser_shell.py`; `scripts/test-browser.py` QA now exercises a
  `target=_blank` link (RED on main's binary, GREEN on the fix) and catches
  `ValueError` from the client-side scroll validator (pre-existing latent
  crash in the in-VM QA script introduced by #113, fixed in the same batch to
  unblock release QA). `docs/browser.md` updated.
- Verification (targeted native-execution evidence, not VM green):
  - Fix compiled cleanly with Alpine 3.23 Qt 6.10.3 (`ninja aios-browser`).
  - Full `scripts/test-browser.py` executed headless against the fixed binary: PASS (open/read/type/click/same-frame+blank-link navigation/back/tabs/scroll-300/scroll-reject incl. new blank-link asserts). Identical harness against main's pre-fix binary FAILS at the blank-link assert (RED/GREEN).
  - Static suites: `tests.test_browser_shell` + `tests.test_browser_agent` 63/63; full `scripts/test.sh` 455 tests with only the known baseline failure `test_terminal_theme.test_desktop_launch_paths_use_the_themed_launcher` (re-confirmed failing on untouched main this run).
  - Not verified: real Alpine/QEMU X11 session with the Chromium sandbox enabled (no display here); stated in the PR.
- New PR: https://github.com/rwrife/aios/pull/114 — MERGED (squash commit 162a08ce28e682527c307626aab5892d3d82a10c, 2026-09-13T13:07:23Z). The merge auto-closed #70 via the body's "Fixes #70:" title-adjacent keyword; since the PR's own acceptance linkage was `Progresses` (in-VM sandboxed release QA still pending), the executor reopened #70 (REST `state=open`), posted an evidence comment (issuecomment-5653473608), and released the assignment so a VM-capable runner can claim the remaining release QA. Final state at run end: PR #114 MERGED, #70 OPEN/unassigned, #71 OPEN/rwrife-assigned (PR #113 merged; Progresses linkage), remote branch deleted, worktree removed.

## 2026-09-13 08:10 UTC

- PR lane: 0 open PRs at start and at issue selection (freshness re-checked). No merges before issue work, no blocked PRs.
- Open issues: 28 at selection time; none assigned and none PR-linked (none skipped as assigned-elsewhere).
- Selected issue: https://github.com/rwrife/aios/issues/71 — "browser mcp actions".
  Rationale: the AI chat is the single interaction point, and browsing via chat
  is a primary agent surface; the issue's conventions ("open cnn" -> website,
  "click on X" -> snapshot+click, ~300px scroll) make everyday browser requests
  work without user micromanagement.
- Claim: `gh issue edit 71 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push.
- Implementation: POLICY lines in `apps/aios/agent.py` teach the generic-name
  → https://www.<name>.com convention and snapshot-then-click-by-label behavior;
  the `scroll` action now moves a bounded whole-pixel amount (optional
  `amount` 1-2000, default 300 px) in `apps/browser/main.cpp`, validated
  client-side in `apps/aios/browser.py` (tool schema + act()) and again in the
  browser socket protocol; `docs/browser.md` documents the conventions;
  `scripts/test-browser.py` (in-VM QA) asserts the 300 px viewport delta and
  rejection of out-of-range amounts.
- Verification (targeted + suite evidence, not VM green):
  - Python suite (`scripts/test.sh`): 453 tests, 1 failure
    (`test_terminal_theme.test_desktop_launch_paths_use_the_themed_launcher`),
    re-confirmed as pre-existing baseline drift by running the same test on
    untouched `main`.
  - Targeted: `tests.test_browser_shell` + `tests.test_browser_agent` 61 pass/0 fail
    (3 new tests: client-side amount bounds, POLICY convention tokens,
    shell-side amount wiring/validation).
  - RED/GREEN: with implementation files stashed the new tests FAIL (9 failures),
    PASS when restored.
  - Not verified: `aios-browser` C++ does not compile on this runner (no Qt6
    toolchain) and `scripts/test-browser.py` needs a real Alpine/QEMU X11
    session, so the new scroll path was not executed live. PR uses
    `Progresses #71` accordingly; issue stays open pending VM validation.
  - CI: `Validate` workflow remains `disabled_manually`; branch protection
    structurally absent.
- New PR: https://github.com/rwrife/aios/pull/113 — OPEN (Progresses #71; assignment retained as in-flight lock).

## 2026-09-13 01:50 UTC

- PR lane: 0 open PRs at start and at issue selection (freshness re-checked). No merges before issue work, no blocked PRs.
- Open issues: 29 at selection time; none assigned and none PR-linked (none skipped as assigned-elsewhere).
- Selected issue: https://github.com/rwrife/aios/issues/69 — "Add markdown and code block support".
  Rationale: the AI chat is the single interaction point of the OS, and every
  model reply arrives as raw Markdown; the chat rendering it as plain text
  garbles the primary UX surface daily. Fully verifiable headless via the QML
  harness.
- Claim: `gh issue edit 69 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push.
- Implementation: new `apps/shell/Markdown.js` (dependency-free, `.pragma
  library` Markdown→HTML converter: headings, lists, fenced/inline code,
  bold/italic/strikethrough, blockquotes, rules, http(s)/mailto links only,
  full HTML escaping of input); `ChatWindow.qml` renders assistant replies
  through it as theme-aware rich text (ink/muted/input/accent roles from
  Theme.qml) while user messages stay PlainText; `Markdown.js` added to the
  shell CMake resource list. Copy/read-aloud still use the original Markdown.
- Verification (targeted QML-suite evidence, not full-harness green):
  - Alpine 3.23 Qt 6.10.3 `qmltestrunner` offscreen: `tst_chat_launcher`
    22 pass/0 fail (3 new regression tests: converter blocks, hostile-input
    escaping, end-to-end delegate rendering); `tst_setup` 9, `tst_local_models`
    6, `tst_theme` 12, `tst_chat_scroll` 6, `tst_subscription` 7,
    `tst_app_host` 14 — all pass.
  - RED/GREEN: new delegate test FAILs with the ChatWindow.qml change stashed
    (21 pass/1 fail), PASSes restored.
  - Python suite (`scripts/test.sh`): 449 tests, 1 failure
    (`test_terminal_theme.test_desktop_launch_paths_use_the_themed_launcher`),
    pre-existing baseline drift on main (untouched by this change).
  - Not verified: in-VM QEMU GUI session (no display on this runner).
  - CI: `Validate` workflow remains `disabled_manually`; branch protection
    structurally absent, so merge was gated on fresh local verification.
- New PR: https://github.com/rwrife/aios/pull/111 — MERGED (squash commit 018f0d3f409872c2abb883c65d8e427331928275, 2026-09-13T01:48:25Z); issue #69 closed at merge; assignment retained through the PR, cleared by the Closes linkage.
- Post-merge PR-lane re-check: 0 open PRs; remote branch deleted; worktree removed.

## 2026-09-12 18:55 UTC

- PR lane: 0 open PRs at start and at issue selection (freshness re-checked). No merges before issue work, no blocked PRs.
- Open issues: 28; none assigned and none PR-linked at selection time (none skipped as assigned-elsewhere).
- Selected issue: https://github.com/rwrife/aios/issues/73 — "desktop clock alignment".
  Rationale: the desktop wordmark + clock are the persistent chrome of the
  AI-only desktop; the clock floated ~20px above the title because both texts
  shared `y` while the clock renders at 75% pixel size. Fully verifiable
  headless via the QML harness.
- Claim: `gh issue edit 73 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push.
- Implementation: `desktopClock` in `Main.qml` now anchors `baseline` to the
  wordmark baseline instead of copying its `y`; the smaller clock's font
  baseline (and, with descender-free text, its visible bottom) matches the
  title's. Added regression test `test_desktop_clock_is_bottom_aligned_with_wordmark`.
- Verification (targeted QML-suite evidence, not full-harness green):
  - Alpine 3.23 Qt6 `qmltestrunner` offscreen: `tst_chat_launcher` 19 pass/0 fail
    (new regression test included); `tst_setup` 9, `tst_local_models` 6,
    `tst_theme` 12, `tst_chat_scroll` 6 — all pass.
  - RED/GREEN: new alignment test FAILs with the Main.qml change stashed
    (18 pass/1 fail), PASSes with it restored.
  - Not verified: in-VM QEMU GUI session (no display on this runner).
  - CI: `Validate` workflow remains `disabled_manually`; no checks arrive for PRs.
    Branch protection is structurally absent (free private repo), so the merge
    was gated on fresh local verification instead of CI.
- New PR: https://github.com/rwrife/aios/pull/109 — MERGED (squash commit af45809edcebd0cc2e1804f02d321cf59bd7a0b2, 2026-09-12T18:52:29Z); issue #73 closed at merge; assignment retained through the PR, cleared by the Closes linkage.
- Post-merge PR-lane re-check: 0 open PRs; remote branch deleted; worktree removed.

## 2026-09-12 12:40 UTC

- PR lane: 0 open PRs at start and at issue selection (freshness re-checked). No merges, no blocked PRs.
- Open issues: 30; none assigned and none PR-linked at selection time (none skipped as assigned-elsewhere).
- Selected issue: https://github.com/rwrife/aios/issues/72 — "Move chat window default location".
  Rationale: the chat window opening dead-center covers the chat orb, the single
  interaction point of the AI-only OS; moving it up restores unobstructed access
  to the primary UX entry and is fully verifiable headless via the QML harness.
- Claim: `gh issue edit 72 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push.
- Implementation: `WindowSizing.js` gains `topCenterY(screenHeight, windowHeight)`
  (centers a window on the top 75% band, clamped at the top edge); `ChatWindow.qml`
  uses it for its initial `y` instead of full-screen centering. Horizontal centering
  and the 70% size clamp are unchanged; users can still move/resize after opening.
- Verification (targeted QML-suite evidence, not full-harness green):
  - Alpine 3.23 Qt6 `qmltestrunner` offscreen: `tst_chat_launcher` 18 pass/0 fail
    (new regression test included); `tst_setup` 9, `tst_local_models` 6,
    `tst_theme` 12, `tst_chat_scroll` 6, `tst_subscription` 7, `tst_app_host` 14 — all pass.
  - RED/GREEN: new `test_chat_opens_in_top_region_clear_of_the_orb` FAILs with the
    app changes stashed (17 pass/1 fail), PASSes with them restored.
  - Not verified: in-VM QEMU GUI session (no display on this runner).
  - CI: `Validate` workflow remains `disabled_manually`; no checks arrive for PRs.
- New PR: https://github.com/rwrife/aios/pull/107 — MERGED (squash commit bf18aa954a3d93fe9c2350f36b3760f02be430a5, 2026-09-12T12:41:38Z); issue #72 closed at merge; assignment retained through the PR, cleared by the Closes linkage.
- Post-merge PR-lane re-check: 0 open PRs; remote branch deleted; worktree removed.

## 2026-09-12 01:35 UTC

- PR lane: 1 open PR at start.
  - Merged: https://github.com/rwrife/aios/pull/62 (squash, base `rwrife-scheduled-result-feedback`, build check SUCCESS, mergeable CLEAN; remote branch `rwrife-scheduled-jobs-release` deleted after merge).
  - Blocked PRs: none.
- Open issues: 26 at selection time; none assigned or PR-linked.
- Selected issue: https://github.com/rwrife/aios/issues/78 — "Download and use model button doesn't select the model".
  Rationale: the model picker is the first-run path to a working on-device chat
  model; a picker that misreports the active model directly breaks the AI-only
  setup flow, and the fix is fully verifiable headless (backend unit tests +
  real Qt qmltestrunner).
- Implementation: `apps/aios/local_models.py` exposes a `selected` flag derived
  from the configured `mode`/`model_path`; `apps/shell/LocalModels.qml` re-follows
  the configured model on inventory refreshes while preserving manual browsing.
- Verification:
  - Canonical Python unit tests: `tests.test_local_models` 12/12 pass.
  - Full python suite: 449 tests, 1 failure
    (`test_terminal_theme.test_desktop_launch_paths_use_the_themed_launcher`),
    which reproduces identically on `main` on this host (error overlap with
    changed paths is empty; classified as pre-existing baseline drift — the
    repo Validate workflow is currently red on main regardless).
  - QML harness (Alpine 3.23, Qt 6.10.3 `qmltestrunner`, offscreen software
    backend): `tst_local_models` 6 pass / 0 fail, with RED/GREEN proof (new
    regression test fails without the QML fix); `tst_chat_launcher` 16 pass;
    `tst_setup` 9 pass.
  - Not verified: in-VM QEMU GUI session (no display on this runner).
- New PR: https://github.com/rwrife/aios/pull/103 — MERGED (squash commit b273f45b989335d9ed9bd0fd9e69df792ceab569, 2026-09-12T02:30:23Z); issue #78 closed at merge.
- Note: the repo `Validate` workflow is currently `disabled_manually`; every run since 2026-09-11 20:06 UTC concluded `failure` with zero jobs (startup-style failure, Actions service operational). PR #103 therefore had no CI checks. Local canonical + QML harness verification (above) is the evidence trail; restoring the workflow is left to the owner.

## 2026-09-12 10:15 UTC

- PR lane: 0 open PRs at start and at issue selection (freshness re-checked). No merges, no blocked PRs.
- Open issues: 24; none assigned and none PR-linked at selection time (none skipped as assigned-elsewhere).
- Selected issue: https://github.com/rwrife/aios/issues/76 — "multiple unwanted chat instances created".
  Rationale: duplicate stacked chat windows break the single-interaction-point UX
  (the orb is the primary entry to every conversation) and were fully fixable +
  testable headless via the QML harness.
- Claim: `gh issue edit 76 --add-assignee @me` → readback `assignees=[rwrife]` (self); re-checked before push (still rwrife).
- Root cause: chat windows are frameless, so the window manager can `hide` them
  (focus-out/desktop click) rather than minimize; the launcher only tracked
  `Window.Minimized`, so hidden chats stayed invisible to `openChat()` and each
  orb click created a new window + session.
- Implementation: `ChatWindow.qml` emits `putAway()` when a shown window becomes
  Minimized or Hidden (with an `isClosing` guard); `Main.qml` tracks away chats
  newest-first and restores one before creating a session. Docs updated.
- Verification (targeted QML-suite evidence, not full-harness green):
  - Alpine 3.23 Qt6 `qmltestrunner` offscreen: `tst_chat_launcher` 17 pass/0 fail;
    `tst_setup` 9, `tst_local_models` 6, `tst_theme` 12, `tst_chat_scroll` 6 — all pass.
  - RED/GREEN: new hidden-restore + renamed tracking tests FAIL with app changes
    stashed, PASS with them restored.
  - Not verified: in-VM QEMU GUI session (no display on this runner).
  - CI: `Validate` workflow remains `disabled_manually`; no checks arrive for PRs.
- New PR: https://github.com/rwrife/aios/pull/105 — MERGED (squash commit 5a5585e5fd93f6c860489c24573915561bf1e6aa, 2026-09-12T10:11:42Z); issue #76 closed at merge; assignment retained through the PR, cleared by the Closes linkage.
- Post-merge PR-lane re-check: 0 open PRs; remote branch deleted; worktree removed.
