# Executor State

Run-state artifact for the every-6-hours PR-first executor (repo: rwrife/aios).

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
