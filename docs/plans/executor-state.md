# Executor State

Run-state artifact for the every-6-hours PR-first executor (repo: rwrife/aios).

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
- New PR: https://github.com/rwrife/aios/pull/114 — Progresses #70 (in-VM sandboxed release QA still outstanding); issue assignment retained while the PR is in flight.

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
