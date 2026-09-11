# Scheduled agent jobs and orb attention

Status: milestones 1-2 implemented as the core and isolated execution service.
Milestone 3 ordinary desktop and dedicated native protected configuration are
implemented. The dependent protected foreground-chat prerequisite is now
implemented, including trusted protected model scheduling. Feedback/orb UI and
full image validation remain later dependent layers.
Baseline: `main` at `6986a2d` (2026-09-10).

## Implementation progress

Milestone 1 adds `apps/aios/scheduling.py` and `scheduled_store.py`, with
display-independent tests in `tests/test_scheduling.py` and
`tests/test_scheduled_store.py`. The store supports normalized job snapshots,
revision-checked edits, pause/resume, transactional occurrence claims, manual
request deduplication, bounded execution policies, missed-run coalescing,
terminal results/outbox commits, explicit recovery, and retention. Ownership
comes from the process OS user, not a greeting profile or request field.
Protected process contexts explicitly report unavailable.

CronSim 2.6 is pinned by wheel hash in `apps/requirements.txt`. It has no
runtime dependencies and uses the BSD-3-Clause license (reviewed for
redistribution; retain its license and disclaimer, without endorsement).
The image staging step retains the wheel's `cronsim-2.6.dist-info/LICENSE`.
The adapter uses its numeric field parser only: AIOS supplies DOM/DOW OR
matching and `zoneinfo` round trips to skip gaps and select the first fold.
Alpine includes `tzdata`. Search is bounded to eight years, including the
eight-year leap-day gap around 2100; timestamp support is 1970 through 2199.
Missed counts are calculated by calendar day rather than replaying each minute.
An occurrence dispatched within its scheduled minute is on time; a later
one-shot is recorded as missed. Resuming never catches up paused occurrences.

The SQLite schema/version upgrade is atomic. Job watermarks survive run-history
pruning so clock rollback cannot reclaim recurring occurrences. Manual request
receipts survive result retention until the job is purged; retrying a pruned
result returns unavailable instead of running again. Unread results are never
automatically pruned. The database is capped at 100 MiB, with a dispatch reserve
for active-run completion; storage exceptions propagate to the future service.

Milestone 2 adds `scheduler.py`, `scheduled_jobs.py`, `scheduled_execution.py`,
`scheduled_runner.py`, and `local_runtime.py`, with packaged desktop-session
launchers. The scheduler owns a private, peer-validated Unix socket and a
storage-scoped singleton lease, so alternate runtime directories cannot start
another dispatcher over the same store. Its timer rechecks once per second.
Immutable worker snapshots bind existing current/agent provider routes to
credential-free fingerprints, including local model file identity. Missing or
changed bindings pause the unchanged job with one durable action-needed result.

Each run starts a real worker/agent/tool-host pair under a Linux subreaper.
Run cleanup leases outlive scheduler crashes, and cancellation, deletion,
shutdown and recovery stop browser/MCP descendants before terminal state is
committed. The service itself adopts descendants if a run guardian crashes.
Background calls enforce saved/current capability intersection, reject native
sign-in and nested scheduling, and never save shared conversation history.
The client exposes normalized readback, next-three-occurrence preview, binding
and capability discovery, explicit configured-zone discovery, revision checks,
bounded pagination, results and unread outbox access; exact signatures are in
`docs/agentic-tools.md`.

The desktop-owned local runtime shares one model process across foreground and
background callers, with interactive-first queued admission and connection-held
leases through HTTP response closure. Active background inference is capped at
60 seconds; abandoned leases stop old generation before another is admitted.
Scheduled workers cannot launch a daemon beneath their cleanup guardian.
Output budgets conservatively count UTF-8 bytes (including tool arguments);
compatible providers also receive remaining `max_tokens`. Reported usage is
retained when supplied. This is not a guarantee about unreported provider
reasoning tokens or billing.

Headless tests exercise real local/remote/subscription worker paths with
deterministic provider processes, isolated browser/MCP fixture subprocesses,
cleanup, timeouts, crashes, credentials, revocation, idempotency and snapshots.
Native shell compilation and the real Alpine/browser/model hardware acceptance
remain in the batched image gate; this layer does not build an ISO or claim
live provider account coverage. Outbox storage is ready for the notification
bridge; quiet-hours, actionable-only filtering, and snooze remain saved policy,
not active delivery. Orb delivery remains a later layer.

Milestone 3 ships the `scheduled_jobs` structured tool, `scheduled-jobs` skill,
and native Settings > Scheduled jobs configuration/history window. All use the
same validated scheduler API and readbacks. The model schemas enforce
conservative character/page limits that fit the existing 24 KiB argument
transport even with escaped non-BMP text; the native client retains the service
byte limits. Forms preserve unsaved edits across errors/conflicts, require a
next-three preview before saving, explicitly bind configured task models, and
keep run-now request UUIDs across ambiguous retries.

`ScheduledJobs.h` exposes fixed asynchronous methods, correlated completions,
and generation invalidation for later feedback/inbox reuse. The native editor
supports title/prompt, once/daily/weekly/numeric cron, explicit IANA zone,
binding/capability readback, notification mode, paginated jobs/history,
pause/resume/delete/run-now/stop, saved result inspection and acknowledgement.
It has been compiled in Alpine with the embedded display both off and on;
headless QML/native tests and Ocean/generated-Sage Xvfb screenshots cover this
configuration surface. A compiled Qt client also exercised the real scheduler,
worker and deterministic provider for CRUD, preview, run deduplication,
persisted results, history and outbox acknowledgement.

Protected foreground chat is a separate broker-owned path; the ordinary
`Backend::run` and `Backend::persist` desktop path remains unchanged and is
never used under a protected display. `SessionControl::createProtectedChat()`
captures the current native lease/work binding and creates a `ProtectedChat`
client whose chat ID and per-chat association key remain private C++ state.
The broker launches one `aios.protected_chat` supervisor per chat through the
existing encrypted artifact alias, cgroup and bubblewrap boundary. That
supervisor owns its worker, tool host, browser and MCP descendants. Conversation
messages are committed by the broker to the encrypted owner journal; no
protected conversation file or provider configuration is written to desktop
XDG storage.

The protected tool host advertises the existing structured `scheduled_jobs`
schema. Its only scheduling route is a private fixed relay socket to its chat
supervisor. The supervisor emits a correlated scheduling event to the broker;
the registered native display supplies no authority fields to Python. The
broker revalidates display PID, lease, work, chat association, owner presence
and scheduler request schema, invokes the existing protected scheduler adapter,
and returns only the normal status envelope. Shielding, lease/work replacement,
display or shell loss, close, cancellation, timeout, broker restart, or relay
failure kills the complete chat cgroup and discards late events before they
reach QML. Reopening creates a new native generation and association key.

The protected adapter runs the same scheduler and worker inside the owner's
encrypted workspace, using broker-held private pipes rather than a public
scheduler socket. Authorization heartbeats expire within two seconds; losing
current authorization cancels its process scope. Artifact bind mounts use
no-follow pinned descriptors, validate encrypted-device/owner/private-mode
identity, and expose only a root-controlled alias to the unprivileged sandbox.
Mutable ancestors, path replacement and scope traversal are rejected. Failed
mount, cgroup-limit setup and sandbox startup clean up the alias and descriptors.

### Milestone 3 validation and kernel reproduction

The frozen configuration layer passed 185 scheduler/tool/identity integration
tests, 119 QML tests, native protocol tests, and one compiled Qt-to-real-scheduler
integration test using an unprivileged Alpine UID and deterministic provider.
The final shell, profile, scheduled-client and display-input targets compiled
with the embedded display enabled. Eleven targeted real Alpine kernel tests
passed; the reproducible full identity harness then passed all 13 tests,
including encrypted scheduling, durable reopen, authorization cancellation,
cross-UID isolation, artifact replacement and partial-start cleanup.

From a Linux/WSL checkout, run the existing disposable-container harness:

```sh
sh scripts/test-identity-linux.sh
```

It requires Docker with privileged private mount/cgroup namespaces, cgroup v2,
loop/device-mapper support, and network access to the pinned Alpine image,
APK repositories and the hash-pinned CronSim wheel. It installs only the
required test dependencies inside the disposable container and stages the
real installed Python layout. The repository mount is read-only; encrypted
images and mount/cgroup fixtures are created only inside the container.
It does not format an existing host block device or build/boot an ISO.

For the single real protected scheduler scenario, the same script accepts
standard unittest selectors:

```sh
sh scripts/test-identity-linux.sh \
  linux_identity_isolation.KernelIsolationTests.test_protected_scheduler_executes_and_cancels_in_encrypted_workspace
```

These checks do not replace real provider-account or final Alpine/QEMU visual
and browser-sandbox acceptance gates.

## Outcome

A user can ask AIOS to run an LLM task once or on a recurring schedule,
inspect and change that schedule, and receive the result even after closing
the originating chat. When a result arrives, AIOS wakes its delivery handler
and draws attention through the desktop orb. Clicking the orb opens the
pending feedback with its job name, execution time, result, and follow-up chat.

Example: “Every weekday at 9 AM, summarize the release notes at this URL.”
AIOS saves the prompt, `0 9 * * 1-5`, and an explicit time zone; reports the
next occurrence; executes with the configured model and approved browser
operations; persists the answer; and signals unread feedback through the orb.
The user can say “pause that job,” edit it in Scheduled jobs, or run it now.

“Wake” means starting an agent run or delivering its completed feedback while
AIOS is running. Hardware wake from suspend, execution while powered off,
external webhooks, and server-side/cloud scheduling are outside version one.
After resume or reboot, the missed-run policy below applies. Installed storage
persists jobs; an ordinary live image remains ephemeral.

## Existing integration points

- `apps/aios/worker.py` runs one request per process and emits JSON-line
  token/progress/error/done events. It is not a persistent scheduler.
- `apps/aios/agent.py` provides the bounded tool loop and provider routing;
  `toolhost.py` owns per-chat browser/MCP capabilities and process cleanup.
- `apps/shell/main.cpp` launches workers and tool hosts for interactive chats.
  Background work needs its own lifecycle, independent of those windows.
- `apps/shell/ChatOrb.qml` exposes `activityLevel` and `awakened`, but
  `Main.qml` currently wires the orb only to opening/restoring a chat.
- `core.py` has shared desktop conversation history. Scheduled results must
  have separate storage rather than concurrently overwriting that history.
- `journal.py`, `sessions.py`, and `sessiond.py` provide an experimental
  protected-workspace path. Its broker authorization and encrypted storage
  must remain distinct from ordinary desktop greeting profiles in
  `chat_profiles.py`; those profiles grant no workspace capabilities.

## Proposed architecture

Use a single unprivileged `aios-scheduler` service per OS user, independent of
the shell process. It owns a SQLite job store, timer loop, bounded worker pool,
and durable notification outbox. Cron expressions describe timing only; never
install model-generated commands into a system crontab or execute prompt text.

The flow is:

1. Native settings or a structured scheduling tool validates and saves a job.
2. The scheduler transactionally claims an occurrence and starts an isolated
   worker/tool-host pair with an immutable snapshot of that job revision.
3. The worker executes through the existing agent/provider adapters, streaming
   bounded events to the scheduler. A successful final answer, safe error, or
   request for user action becomes a persisted run result.
4. One transaction commits the terminal run state and its outbox record.
5. A desktop notification bridge reads unread records and updates orb state.
   A signal speeds delivery; reconnecting always reconciles durable state.
6. Opening a result displays the saved assistant answer without requiring
   another model call. Follow-up starts a fresh authorized chat with a bounded
   copy of the relevant job context and result.

Reuse `agent.chat` through an explicit background execution context rather than
duplicating its tool loop. Extract process supervision from the shell where
useful. The service must also handle local-model readiness without depending
on an open chat's model process; share or supervise a single local runtime and
give interactive requests priority.

## Job configuration and tool contract

Add `apps/aios/scheduling.py` for schemas/time calculations,
`scheduled_store.py` for storage, `scheduler.py` for supervision, and
`scheduled_jobs.py` for the validated service client/tool. These names are
proposed new files. Add a shipped `scheduled-jobs` skill and document the
actual interface in `docs/agentic-tools.md` when implementing it.

Expose fixed actions: `create`, `get`, `list`, `update`, `pause`, `resume`,
`delete`, `run_now`, `cancel_run`, `list_runs`, `read_result`, and
`acknowledge_result`. Define action-specific schemas, bounded strings and
pagination, opaque IDs, and expected revisions for mutations. Derive caller
ownership from trusted service context, never a model-supplied owner field.
Return readback including the normalized schedule, time zone, enabled state,
next UTC/local occurrence, revision, and execution/notification policy. Invalid,
unavailable, conflict, quota-exceeded, and needs-user-action are explicit results.

Persist these fields:

| Record | Required information |
| --- | --- |
| Job | ID, owner scope, title, prompt, context snapshot, originating conversation reference if available, schedule kind/value, IANA zone, enabled state, revision, timestamps, next due time |
| Execution policy | Provider/profile reference and model, allowed capabilities, timeout, token/tool budget, missed-run policy |
| Notification policy | All completed results (default) or actionable results only; quiet hours and optional snooze |
| Run | ID, job/revision snapshot, scheduled UTC instant, trigger, state, start/end, bounded result/error, usage when reported |
| Outbox | Run ID, owner scope, outcome, unread/acknowledged state, sequence ID, timestamps |

Use native controls for title, prompt, once/daily/weekly/custom cron timing,
zone, model, notification mode, and next-run preview. Provide run history,
pause/resume, edit, delete, run-now, and stop controls. Natural-language setup
uses the same service and returns the saved configuration in chat. Ask only
when timing or task scope is ambiguous; a clear scheduling request authorizes
saving it. Never silently assume a zone when no configured zone exists.

## Timing, persistence, and recovery

- Version one accepts a single future timestamp or five-field cron with
  numeric values, lists, ranges, steps, and `*`. Minute resolution; no seconds,
  macros, shell suffixes, environment assignments, or arbitrary commands.
  Define standard cron day matching: restricted day-of-month and day-of-week
  fields use OR. Sunday accepts 0 or 7. Validate bounded expression length and
  search horizon; report impossible schedules rather than looping indefinitely.
- Store IANA zones with jobs and occurrences as UTC. Ship time-zone data in
  Alpine and use a pinned, license-reviewed cron parser behind a narrow adapter
  with `zoneinfo`. Select it in milestone 1 against the specified tests.
  Skip nonexistent local times in spring; run once at the first occurrence of
  an ambiguous local time in autumn. A desktop zone change leaves saved zones
  unchanged until edited. Show the next three occurrences before saving edits.
- Use wall time for due dates and monotonic time for execution deadlines.
  Recompute after clock changes/resume, with a bounded timer recheck (at most
  30 seconds). Never rerun an already claimed occurrence after clock rollback.
- Default missed-run policy: coalesce to the latest missed occurrence within
  24 hours, once; skip older occurrences and record the missed count. Offer
  `skip` as an alternative. An expired one-shot becomes `missed` with feedback.
  Pause suppresses catch-up; resume computes a new future occurrence.
- A unique `(job_id, scheduled_at_utc)` key prevents duplicate recurring
  claims. Manual runs have separate request-id deduplication. One active run
  per job; initially at most two per user, with one local-model run at a time.
  Coalesce overlap instead of growing an unbounded backlog.
- States: `queued -> running -> succeeded | failed | cancelled |
  needs_user_action | interrupted`; `missed` records work that did not start.
  Pause affects future runs; cancellation explicitly stops current work.
  Deleting a job disables it and cancels current/queued work before purging
  configuration; retained results follow an explicit user-visible retention policy.
- Enforce singleton ownership, transactional claims and schema migrations.
  Recover abandoned runs as interrupted; do not blindly retry a run that may
  have performed side effects. Retrying requires a new explicit run. A failed
  occurrence does not disable the next scheduled occurrence; persistent missing
  credentials pause dispatch and produce one deduplicated action-needed notice.
- SQLite transactions provide durable local state, not exactly-once external
  effects. Commit results before notifying. Replayed outbox events deduplicate
  by run ID. Disk-full failures stop dispatch and show service health errors;
  never claim that an unpersisted result was delivered.
- Start with limits of 100 enabled jobs/user, 15 minutes/run, 32 KiB prompt,
  64 KiB context, and 64 KiB final result. Enforce model-output/tool-call budgets
  in addition to existing agent limits. Keep terminal history for 30 days/1,000
  runs and a 100 MiB store budget; preserve unread results until acknowledged
  or explicitly deleted, and pause dispatch if they exhaust the budget.

## Ownership and execution boundaries

The scheduler socket lives in the user's private runtime directory, with
restricted permissions, peer-credential checks, bounded messages, and no TCP
listener. Jobs/results live under that user's XDG data directory with mode 0700
directories and 0600 files. Credentials remain in existing provider storage;
job snapshots contain references, never API keys, PINs, or authentication tokens.

Bind the effective provider/model at creation using existing routing, show it
in readback, and record the actual route on each run. Configuration changes
that invalidate that binding produce an action-needed result; no silent paid
fallback or model change. Scheduling has no special authority to broaden tools.

Each run receives a fresh private tool-host context and temporary browser
profile, with the existing least-privilege restrictions and guaranteed process
cleanup on completion, timeout, cancellation, or service shutdown. Enforce the
intersection of saved capabilities and currently permitted tools. Background
runs cannot create further schedules or trigger native sign-in dialogs by
default. Missing protected authorization becomes `needs_user_action`; opening
feedback can initiate native authentication in a fresh per-chat context.

For the ordinary desktop, ownership is the OS user, not a greeting profile.
For experimental protected workspaces, store and run jobs inside the authorized
owner workspace via the broker, with dispatch gated on current authorization.
Privacy loss cancels protected execution, revokes access, and hides feedback;
locked workspaces do not execute unattended in version one. Do not move their
prompts, results, or credentials into the shared desktop scheduler database.
Implement this adapter before enabling scheduling in protected mode; explicitly
report unavailable there until its authorization and display tests pass.

## Feedback and orb behavior

Extend the shell with a reconnecting `ScheduledJobs` bridge and a bounded
result inbox. Saved feedback survives closing the source chat, shell restart,
and temporary disconnects. Delivery never injects into an active streaming
turn or rewrites shared history. Clicking a result opens a dedicated result
view and a Follow up action, even when the original chat no longer exists.

The default is feedback for every completed answer. Optional actionable-only
mode uses a validated `changed | unchanged | needs_user_action` outcome supplied
by the job; a missing/invalid outcome defaults to notification. Errors and
requests for user action remain visible. Persist both quiet and notified runs.
Completion handling cannot autonomously create another LLM run or repeat tools.

Drive an attention state independent of `activityLevel`: idle, running, unread,
or action-needed. Unread/action-needed takes priority over running. For a new
eligible result, set an accent-colored orb contour and pulse its opacity three
times over six seconds, then keep a steady accent marker and unread count.
This gives the requested color flash through an opacity overlay using
`Theme.accent`/`wave`, without rapid flashing or a new palette. Reduced motion
uses only the steady marker and count. Coalesce simultaneous results into one
attention sequence; never pulse on every token or replay pulses on reconnect.

While feedback is pending, orb activation opens the inbox; provide a clearly
named New chat action there. Otherwise retain existing open/restore-chat
behavior. Update accessible names/descriptions and keyboard-focus tooltips.
Do not steal keyboard focus, open unsolicited windows, or speak automatically.
Quiet hours/snooze defer pulses, not persistence; pulse once for remaining
unread results when suppression ends. Viewing a rendered result or Mark read
acknowledges that result; opening the inbox alone does not clear all feedback.
On privacy loss hide titles, bodies, counts, and attention state; reconcile
only the currently authorized owner's unread results after native unlock.

## Implementation milestones and acceptance gates

1. **Durable scheduling core.** Add schema, migrations, cron adapter, fake clock,
   occurrence calculation, quotas, and transactional store tests. Gate: once
   and recurring schedules survive reopen; DST, leap days, month ends, invalid
   input, clock jumps, edits, pause/resume, overlap, and claim races are covered.
2. **Service and isolated execution.** Add the per-user supervisor, packaged
   launcher and desktop-session startup/teardown (including Alpine packaging),
   local-model lifecycle, background execution context, and cancellation.
   Gate: real worker/agent/tool-host plus a fake provider execute with the chat
   closed; timeout, crash recovery, missing credentials, output bounds, and
   browser/MCP process cleanup pass without a display server where possible.
3. **Configuration surface.** Ship the structured tool, skill, documentation,
   and Scheduled jobs settings/history view. Gate: natural-language and native
   configuration use the same validation/readback; run-now is idempotent;
   edits do not mutate in-flight snapshots; capability and provider changes
   cannot silently expand authorization. Add the protected-workspace adapter
   and cross-owner/locked-workspace tests before enabling that mode.
   **Required dependent prerequisite:** complete protected foreground chat,
   encrypted conversation persistence and its registered-display scheduling
   relay before enabling protected model/chat scheduling. The configuration PR
   completes ordinary desktop setup and dedicated native protected configuration,
   not this foreground-chat gate.
4. **Reliable feedback and orb.** Add transactional outbox, reconnecting shell
   bridge, inbox/follow-up flow, acknowledgement, and attention overlay. Gate:
   duplicate/out-of-order events, shell restart, an active chat turn, closed
   source chat, multiple results, quiet hours, and privacy changes do not lose,
   duplicate, leak, or prematurely acknowledge results. QML tests cover mouse,
   keyboard, accessibility, focus, unread precedence, and reduced motion.
5. **Batched image validation and release.** Build locally in WSL and boot the
   real Alpine image in QEMU with a unique descriptive `-name`. Wait for boot.
   Capture Ocean and generated-palette screenshots for idle/running/unread,
   action-needed, inbox, and reduced-motion states; verify window sizing and
   shared chrome. Exercise scheduled browser page loading, input, navigation,
   scrolling, cleanup, and two isolated concurrent sessions with the Chromium
   sandbox enabled. Test installed-storage reboot/resume, live-image behavior,
   offline/provider recovery, and local/remote/subscription routing. Record
   actual coverage and outstanding provider/hardware limitations in the PR.

Deliver the milestones as reviewable dependent implementation PRs. The feature
is complete when a user can configure a recurring LLM task, close its chat,
receive persisted feedback through the orb, inspect/follow up on the answer,
and pause or delete the job, with recovery and privacy gates passing. Disabling
the service must stop new work while leaving saved jobs/results intact for
inspection or a later compatible restart.
