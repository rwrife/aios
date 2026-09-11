import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "ScheduledFeedback"
    when: windowShown
    visible: true
    width: 1000
    height: 760

    Theme { id: palette }
    QtObject {
        id: api
        property int sequence: 0
        property var calls: []
        signal completed(string requestId, string action, var response)
        signal invalidated()
        function record(action, args) {
            var id = "request-" + (++sequence)
            calls = calls.concat([{id: id, action: action, args: args || {}}])
            return id
        }
        function health() { return record("health") }
        function unread(limit, after) { return record("unread", {limit: limit, after: after}) }
        function markNotified(run) { return record("mark_notified", {run: run}) }
        function readResult(run) { return record("read_result", {run: run}) }
        function acknowledgeResult(run) { return record("acknowledge_result", {run: run}) }
        function listRuns(job, limit, before) {
            return record("list_runs", {job: job, limit: limit, before: before})
        }
    }
    ScheduledFeedback {
        id: feedback
        jobsApi: api
        active: false
        reducedMotion: false
    }
    ScheduledResultWindow {
        id: window
        jobsApi: api
        feedback: feedback
        theme: palette
    }
    ChatOrb {
        id: orb
        theme: palette
        reducedMotion: feedback.reducedMotion
        attentionState: feedback.attentionState
        unreadCount: feedback.unreadCount
        pulseSerial: feedback.pulseSerial
    }
    SignalSpy { id: pulses; target: feedback; signalName: "pulseRequested" }

    function call(action) {
        for (var i = api.calls.length - 1; i >= 0; --i)
            if (api.calls[i].action === action)
                return api.calls[i]
        return null
    }
    function reply(request, result) {
        api.completed(request.id, request.action, {status: "ok", result: result})
    }
    function row(id, sequence, state, notified, suppressed) {
        return {
            run_id: id, sequence: sequence, job_id: "job-one",
            title: "Morning summary " + id, state: state,
            outcome: state === "succeeded" ? "changed" : state,
            scheduled_at: "2026-09-11T17:00:00Z",
            ended: "2026-09-11T17:01:00Z",
            notified: notified || null,
            suppressed_until: suppressed || null
        }
    }
    function finishMarks() {
        while (call("mark_notified")) {
            var mark = call("mark_notified")
            var count = api.calls.length
            reply(mark, {notified: true})
            if (api.calls.length === count)
                break
        }
    }
    function init() {
        feedback.active = false
        feedback.clearPrivateState()
        feedback.reducedMotion = false
        feedback.pulseSerial = 0
        api.calls = []
        pulses.clear()
        window.close()
        window.result = ({})
        window.history = []
        window.pending = ({})
    }
    function cleanup() {
        feedback.active = false
        window.close()
    }

    function test_deduplicates_orders_and_coalesces_attention() {
        feedback.active = true
        reply(call("health"), {available: true, active_runs: 1})
        reply(call("unread"), [
            row("run-two", 2, "failed"),
            row("run-one", 1, "succeeded"),
            row("run-two", 2, "failed")
        ])
        compare(feedback.unreadCount, 2)
        compare(feedback.entries[0].run_id, "run-two")
        compare(feedback.attentionState, "action-needed")
        finishMarks()
        compare(feedback.pulseSerial, 1)
        compare(pulses.count, 1)
        compare(feedback.runningCount, 1)
        compare(orb.attentionPulseCycles, 3)
        compare(orb.attentionPulseDuration, 6000)
    }

    function test_reconnect_does_not_replay_notified_results() {
        feedback.active = true
        reply(call("health"), {available: true, active_runs: 0})
        reply(call("unread"), [row("run-one", 1, "succeeded", "2026-09-11T17:02:00Z")])
        compare(call("mark_notified"), null)
        compare(feedback.pulseSerial, 0)
        feedback.reconcile()
        reply(call("health"), {available: true, active_runs: 0})
        reply(call("unread"), [row("run-one", 1, "succeeded", "2026-09-11T17:02:00Z")])
        compare(feedback.pulseSerial, 0)
    }

    function test_reduced_motion_and_quiet_release() {
        feedback.reducedMotion = true
        feedback.active = true
        reply(call("health"), {available: true, active_runs: 0})
        reply(call("unread"), [
            row("quiet", 1, "succeeded", null, "2999-01-01T00:00:00Z")
        ])
        compare(call("mark_notified"), null)
        var released = JSON.parse(JSON.stringify(feedback.entries[0]))
        released.suppressed_until = "2000-01-01T00:00:00Z"
        feedback.entries = [released]
        feedback.evaluatePulse()
        finishMarks()
        compare(feedback.pulseSerial, 1)
        compare(pulses.count, 0)
        compare(feedback.attentionState, "unread")
    }

    function test_inbox_open_does_not_ack_and_view_acks_one() {
        feedback.entries = [row("run-one", 1, "succeeded"), row("run-two", 2, "failed")]
        window.show()
        wait(20)
        compare(call("acknowledge_result"), null)
        window.openResult("run-one")
        var read = call("read_result")
        reply(read, {
            id: "run-one", job_id: "job-one", state: "succeeded",
            scheduled_at: "2026-09-11T17:00:00Z",
            started: "2026-09-11T17:00:01Z", ended: "2026-09-11T17:01:00Z",
            result: "Saved answer", error: "",
            snapshot: {title: "Morning summary", prompt: "Summarize", context: "Release"}
        })
        compare(call("acknowledge_result").args.run, "run-one")
        compare(call("list_runs").args.job, "job-one")
        reply(call("acknowledge_result"), {acknowledged: true})
        compare(feedback.unreadCount, 1)
        compare(feedback.entries[0].run_id, "run-two")
        compare(findChild(window, "savedScheduledResult").text, "Saved answer")
    }

    function test_follow_up_is_bounded_and_privacy_clears_immediately() {
        window.result = {
            id: "run-one", state: "succeeded", result: "x".repeat(9000),
            snapshot: {title: "Result", prompt: "p".repeat(1400), context: "c".repeat(3000)}
        }
        var draft = window.followUpDraft()
        verify(draft.length < 12000)
        verify(draft.indexOf("[truncated]") >= 0)
        feedback.entries = [row("run-one", 1, "succeeded")]
        feedback.runningCount = 1
        api.invalidated()
        compare(feedback.unreadCount, 0)
        compare(feedback.runningCount, 0)
        compare(feedback.attentionState, "idle")
        verify(!window.visible)
    }
}
