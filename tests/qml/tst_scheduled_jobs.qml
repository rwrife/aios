import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "ScheduledJobs"
    when: windowShown
    visible: true
    width: 1000; height: 780
    Theme { id: palette }
    QtObject {
        id: api
        property int sequence: 0
        property var calls: []
        property int disposals: 0
        signal completed(string requestId, string action, var response)
        signal invalidated()
        function record(action, args) {
            var id = "call-" + (++sequence)
            calls = calls.concat([{id: id, action: action, args: args}])
            return id
        }
        function newRequestId() { return "receipt-" + (++sequence) }
        function health() { return record("health", {}) }
        function dispose() { ++disposals }
        function list(limit, after) { return record("list", {limit: limit, after: after}) }
        function get(job) { return record("get", {job: job}) }
        function binding(prompt) { return record("binding", {prompt: prompt}) }
        function preview(schedule) { return record("preview", {schedule: schedule}) }
        function create(config) { return record("create", {config: config}) }
        function update(job, revision, config) { return record("update", {job: job, revision: revision, config: config}) }
        function pause(job, revision) { return record("pause", {job: job, revision: revision}) }
        function resume(job, revision) { return record("resume", {job: job, revision: revision}) }
        function remove(job, revision) { return record("delete", {job: job, revision: revision}) }
        function runNow(job, revision, request) { return record("run_now", {job: job, revision: revision, request: request}) }
        function listRuns(job, limit, before) { return record("list_runs", {job: job, limit: limit, before: before}) }
        function readResult(run) { return record("read_result", {run: run}) }
        function cancelRun(run) { return record("cancel_run", {run: run}) }
        function acknowledgeResult(run) { return record("acknowledge_result", {run: run}) }
    }
    ScheduledJobsWindow { id: window; jobsApi: api; theme: palette }
    function latest() { return api.calls[api.calls.length - 1] }
    function reply(call, result) { api.completed(call.id, call.action, {status: "ok", result: result}) }
    function field(name) { return findChild(window.contentItem, name) }
    function sample() {
        return {id: "job-one", revision: 3, enabled: true, title: "Morning notes", prompt: "Summarize notes",
            context: "Saved context", conversation: "chat-one",
            schedule: {kind: "cron", value: "0 9 * * 1-5", zone: "America/Los_Angeles"},
            execution: {provider: "local", profile: "current@fingerprint", model: "local-model",
                capabilities: ["browser"], timeout_seconds: 300, token_budget: 1000, tool_budget: 4, missed_run: "skip"},
            notification: {mode: "all", quiet_hours: {start: "22:00", end: "07:00", zone: "America/Los_Angeles"}},
            next_local: "2026-12-01T09:00:00-08:00"}
    }
    function preview() {
        window.previewSchedule()
        reply(latest(), [{utc: "2026-12-01T17:00:00Z", local: "2026-12-01T09:00:00-08:00"}])
    }
    function init() {
        window.jobsApi = api; window.ownsJobsApi = false; api.disposals = 0
        window.show()
        window.pending = ({}); window.operation = ""; window.runRequests = ({})
        window.resetForm(); window.ready = true; api.calls = []; palette.selected = "blue"
    }
    function cleanup() { window.close() }
    function test_preview_required_and_invalidated() {
        window.loadJob(sample())
        verify(!field("saveScheduledJob").enabled)
        preview()
        verify(field("saveScheduledJob").enabled)
        field("scheduledZone").text = "Europe/London"
        verify(!field("saveScheduledJob").enabled)
        compare(window.occurrences.length, 0)
    }
    function test_save_is_reachable_by_scrolling() {
        window.loadJob(sample()); preview()
        var form = field("scheduledForm")
        wait(50)
        form.contentItem.contentY = form.contentItem.contentHeight - form.height
        wait(50)
        var save = field("saveScheduledJob")
        var position = save.mapToItem(form, 0, 0)
        verify(position.y >= 0 && position.y + save.height <= form.height)
        mouseClick(save)
        compare(latest().action, "update")
    }
    function test_daily_weekly_cron_once() {
        verify(field("scheduledTiming").height >= 36)
        field("scheduledZone").text = "UTC"
        field("scheduledTime").text = "08:04"
        compare(window.schedule().value, "4 8 * * *")
        field("scheduledTiming").currentIndex = 2
        compare(window.schedule().value, "4 8 * * 1")
        field("scheduledTime").text = "25:01"
        compare(window.schedule(), null)
        window.previewSchedule()
        compare(api.calls.length, 0)
        verify(window.notice.indexOf("HH:MM") >= 0)
        field("scheduledTiming").currentIndex = 0
        field("scheduledOnce").text = "2026-12-01T09:00:00-08:00"
        compare(window.schedule().kind, "once")
        field("scheduledTiming").currentIndex = 3
        compare(window.schedule().value, "0 9 * * 1-5")
    }
    function test_binding_has_no_implicit_zone_or_authority_expansion() {
        field("scheduledPrompt").text = "Read notes"
        window.bindModel()
        reply(latest(), {provider: "local", profile: "current@abc", model: "model", capabilities: [], warnings: [], zone: null})
        compare(field("scheduledZone").text, "")
        compare(window.bindingValue.capabilities.length, 0)
        verify(window.notice.indexOf("explicit IANA") >= 0)
    }
    function test_startup_readiness_does_not_replay_mutation() {
        window.ready = false
        field("scheduledTitle").text = "Pending draft"
        window.checkReadiness()
        reply(latest(), {available: false, error: "Starting protected service"})
        compare(field("scheduledTitle").text, "Pending draft")
        verify(!field("saveScheduledJob").enabled)
        window.checkReadiness()
        reply(latest(), {available: true})
        compare(latest().action, "list")
        compare(field("scheduledTitle").text, "Pending draft")
        verify(window.ready)
    }
    function test_edit_preserves_saved_policy_and_revision() {
        window.loadJob(sample())
        field("scheduledTitle").text = "Updated notes"
        preview(); window.saveJob()
        var call = latest()
        compare(call.action, "update"); compare(call.args.revision, 3)
        compare(call.args.config.execution.profile, "current@fingerprint")
        compare(call.args.config.execution.tool_budget, 4)
        compare(call.args.config.context, "Saved context")
        compare(call.args.config.notification.quiet_hours.start, "22:00")
    }
    function test_conflict_keeps_draft_and_fetches_current() {
        window.loadJob(sample()); field("scheduledTitle").text = "My pending title"
        preview(); window.saveJob()
        var save = latest()
        api.completed(save.id, save.action, {status: "conflict", error: "Job changed"})
        compare(field("scheduledTitle").text, "My pending title")
        compare(latest().action, "get")
        var current = sample(); current.revision = 4; current.title = "Someone else's edit"
        reply(latest(), current)
        compare(window.revision, 3)
        compare(window.conflictJob.revision, 4)
        compare(field("scheduledTitle").text, "My pending title")
        verify(!field("saveScheduledJob").enabled)
    }
    function test_run_now_retry_reuses_receipt() {
        window.loadJob(sample()); window.mutate("run_now")
        var first = latest()
        api.completed(first.id, first.action, {status: "unavailable", error: "Disconnected"})
        window.mutate("run_now")
        compare(latest().args.request, first.args.request)
        reply(latest(), {id: "run-one", state: "queued"})
        compare(latest().action, "list_runs")
        window.mutate("run_now")
        verify(latest().args.request !== first.args.request)
    }
    function test_pause_preserves_pending_prompt() {
        window.loadJob(sample()); field("scheduledPrompt").text = "Unsaved prompt"
        window.mutate("pause")
        var paused = sample(); paused.revision = 4; paused.enabled = false
        reply(latest(), paused)
        compare(field("scheduledPrompt").text, "Unsaved prompt")
        compare(window.revision, 4); verify(!window.job.enabled)
    }
    function test_privacy_loss_discards_inflight_response_and_results() {
        window.loadJob(sample()); window.inspectRun("run-one")
        var read = latest()
        api.invalidated()
        reply(read, {id: "run-one", result: "Private result"})
        compare(Object.keys(window.result).length, 0)
        compare(window.jobs.length, 0)
        compare(field("scheduledPrompt").text, "")
        verify(!window.visible)
    }
    function test_private_window_disposes_scoped_client_on_privacy_loss() {
        window.ownsJobsApi = true; window.loadJob(sample())
        window.inspectRun("run-one")
        var read = latest()
        api.invalidated()
        compare(api.disposals, 1)
        compare(window.jobsApi, null)
        reply(read, {id: "run-one", result: "Old owner result"})
        compare(Object.keys(window.result).length, 0)
        verify(!window.visible)
    }
    function test_history_view_acknowledges_exact_rendered_result() {
        window.loadJob(sample()); window.inspectRun("run-one")
        reply(latest(), {id: "run-one", state: "succeeded", result: "<b>plain text</b>"})
        compare(latest().action, "acknowledge_result")
        compare(latest().args.run, "run-one")
        compare(field("scheduledResult").text, "<b>plain text</b>")
        compare(field("scheduledResult").textFormat, TextEdit.PlainText)
    }
    function test_pagination_cursors() {
        window.jobs = [{id: "last-job"}]; window.refreshJobs(true)
        compare(latest().args.after, "last-job")
        window.loadJob(sample()); window.runs = [{id: "last-run", sequence: 21}]
        window.refreshRuns(true)
        compare(latest().args.before, 21)
    }
    function test_palettes_and_accessible_keyboard_focus() {
        palette.selected = "purple"
        compare(field("scheduledTitle").Accessible.name, "Job title")
        field("scheduledTitle").forceActiveFocus()
        tryCompare(field("scheduledTitle"), "activeFocus", true)
        keyClick(Qt.Key_Tab)
        verify(!field("scheduledTitle").activeFocus)
        verify(window.width < Screen.width * 0.75)
    }
}
