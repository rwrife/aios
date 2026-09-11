import QtQuick

Item {
    id: feedback
    property var jobsApi: null
    property bool active: false
    property bool reducedMotion: false
    property var entries: []
    property int runningCount: 0
    property string error: ""
    property int pulseSerial: 0
    readonly property int unreadCount: entries.length
    readonly property int actionNeededCount: {
        var count = 0
        for (var i = 0; i < entries.length; ++i) {
            var state = entries[i].state
            if (state === "failed" || state === "needs_user_action"
                    || state === "interrupted" || state === "missed")
                ++count
        }
        return count
    }
    readonly property string attentionState: actionNeededCount > 0 ? "action-needed"
        : unreadCount > 0 ? "unread" : runningCount > 0 ? "running" : "idle"
    property var pending: ({})
    property var pageRows: []
    property int pageCount: 0
    property bool reconciling: false
    property var notifyQueue: []
    property bool marking: false
    signal pulseRequested()
    signal cleared()

    function track(id, purpose) {
        pending[id] = purpose
        return id
    }
    function clearPrivateState() {
        entries = []
        runningCount = 0
        pending = ({})
        pageRows = []
        notifyQueue = []
        marking = false
        reconciling = false
        error = ""
        cleared()
    }
    function reconcile() {
        if (!active || !jobsApi || reconciling)
            return
        reconciling = true
        pageRows = []
        pageCount = 0
        track(jobsApi.health(), "health")
        track(jobsApi.unread(50, 0), "unread")
    }
    function nextPage(after) {
        ++pageCount
        track(jobsApi.unread(50, after), "unread")
    }
    function commitRows() {
        var byRun = ({})
        for (var i = 0; i < pageRows.length; ++i) {
            var row = pageRows[i]
            var previous = byRun[row.run_id]
            if (!previous || Number(row.sequence) > Number(previous.sequence))
                byRun[row.run_id] = row
        }
        var next = []
        for (var run in byRun)
            next.push(byRun[run])
        next.sort(function(left, right) { return Number(right.sequence) - Number(left.sequence) })
        entries = next
        reconciling = false
        evaluatePulse()
    }
    function suppressed(row) {
        return row.suppressed_until && Date.parse(row.suppressed_until) > Date.now()
    }
    function evaluatePulse() {
        if (!active || marking || notifyQueue.length)
            return
        var queue = []
        for (var i = 0; i < entries.length && queue.length < 50; ++i) {
            if (!entries[i].notified && !suppressed(entries[i]))
                queue.push(entries[i].run_id)
        }
        if (!queue.length)
            return
        notifyQueue = queue
        markNext()
    }
    function markNext() {
        if (!active || !jobsApi || !notifyQueue.length) {
            marking = false
            if (active) {
                ++pulseSerial
                if (!reducedMotion)
                    pulseRequested()
            }
            return
        }
        marking = true
        track(jobsApi.markNotified(notifyQueue[0]), "notified")
    }
    function removeRun(runId) {
        var remaining = []
        for (var i = 0; i < entries.length; ++i)
            if (entries[i].run_id !== runId)
                remaining.push(entries[i])
        entries = remaining
    }
    function receive(id, action, response) {
        var purpose = pending[id]
        if (!purpose)
            return
        delete pending[id]
        if (response.status !== "ok") {
            error = response.status + ": " + response.error
            if (purpose === "unread")
                reconciling = false
            if (purpose === "notified") {
                marking = false
                notifyQueue = []
            }
            return
        }
        error = ""
        if (purpose === "health") {
            runningCount = Number(response.result.active_runs || 0)
        } else if (purpose === "unread") {
            var rows = response.result || []
            pageRows = pageRows.concat(rows)
            if (rows.length === 50 && pageCount < 3)
                nextPage(rows[rows.length - 1].sequence)
            else
                commitRows()
        } else if (purpose === "notified") {
            var completed = notifyQueue[0]
            var updated = []
            for (var i = 0; i < entries.length; ++i) {
                var row = entries[i]
                if (row.run_id === completed) {
                    row = JSON.parse(JSON.stringify(row))
                    row.notified = new Date().toISOString()
                }
                updated.push(row)
            }
            entries = updated
            notifyQueue = notifyQueue.slice(1)
            markNext()
        }
    }
    onActiveChanged: {
        if (active) {
            reconcileTimer.restart()
            reconcile()
        } else {
            reconcileTimer.stop()
            clearPrivateState()
        }
    }
    onJobsApiChanged: {
        if (active && jobsApi)
            reconcile()
        else if (!jobsApi)
            clearPrivateState()
    }
    Component.onCompleted: {
        if (active)
            reconcile()
    }
    Connections {
        target: feedback.jobsApi
        ignoreUnknownSignals: true
        function onCompleted(requestId, action, response) {
            feedback.receive(requestId, action, response)
        }
        function onInvalidated() {
            feedback.clearPrivateState()
        }
    }
    Timer {
        id: reconcileTimer
        interval: 5000
        repeat: true
        running: feedback.active
        onTriggered: feedback.reconcile()
    }
    Timer {
        interval: 30000
        repeat: true
        running: feedback.active && feedback.unreadCount > 0
        onTriggered: feedback.evaluatePulse()
    }
}
