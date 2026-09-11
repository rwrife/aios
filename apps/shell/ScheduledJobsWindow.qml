import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import "WindowSizing.js" as WindowSizing

Window {
    id: root
    required property var jobsApi
    required property var theme
    property bool ownsJobsApi: false
    title: "Scheduled jobs"
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "transparent"
    width: WindowSizing.extent(1040, Screen.width, Screen.desktopAvailableWidth)
    height: WindowSizing.extent(780, Screen.height, Screen.desktopAvailableHeight)
    minimumWidth: WindowSizing.extent(560, Screen.width, Screen.desktopAvailableWidth)
    minimumHeight: WindowSizing.extent(440, Screen.height, Screen.desktopAvailableHeight)
    x: Screen.virtualX + (Screen.width - width) / 2
    y: Screen.virtualY + (Screen.height - height) / 2
    property var jobs: []
    property var job: ({})
    property var runs: []
    property var result: ({})
    property var bindingValue: ({})
    property var conflictJob: ({})
    property var pending: ({})
    property var runRequests: ({})
    property var occurrences: []
    property string previewSignature: ""
    property string notice: ""
    property string operation: ""
    property bool moreJobs: false
    property bool moreRuns: false
    property bool viewingOlderRuns: false
    property bool history: false
    property bool deleteConfirm: false
    property bool loadingForm: false
    property bool ready: false
    property bool reconcileAfterReady: false
    property int readinessAttempts: 0
    property int revision: 0
    readonly property bool busy: operation !== ""
    readonly property bool saved: !!job.id

    function clone(value) { return JSON.parse(JSON.stringify(value)) }
    function track(id, purpose, details) {
        pending[id] = {purpose: purpose, details: details || {}}
        return id
    }
    function checkReadiness() {
        if (!jobsApi) { notice = "Scheduled jobs service is unavailable."; return }
        ++readinessAttempts
        track(jobsApi.health(), "health")
    }
    function refreshJobs(more) {
        if (!jobsApi) { notice = "Scheduled jobs service is unavailable."; return }
        for (var id in pending) if (pending[id].purpose === "jobs") return
        track(jobsApi.list(50, more && jobs.length ? jobs[jobs.length - 1].id : ""), "jobs", {more: !!more})
    }
    function resetForm() {
        loadingForm = true
        job = ({}); revision = 0; conflictJob = ({}); result = ({}); runs = []
        bindingValue = ({}); occurrences = []; previewSignature = ""
        titleField.text = ""; promptField.text = ""; zoneField.text = ""
        timing.currentIndex = 1; timeField.text = "09:00"; weekday.currentIndex = 1
        onceField.text = ""; cronField.text = "0 9 * * 1-5"; notification.currentIndex = 0
        history = false; viewingOlderRuns = false; deleteConfirm = false; notice = ""
        loadingForm = false
    }
    function loadJob(value) {
        resetForm()
        loadingForm = true
        job = value; revision = value.revision
        titleField.text = value.title; promptField.text = value.prompt
        zoneField.text = value.schedule.zone
        if (value.schedule.kind === "once") {
            timing.currentIndex = 0; onceField.text = value.schedule.value
        } else {
            timing.currentIndex = 3; cronField.text = value.schedule.value
        }
        bindingValue = clone(value.execution)
        notification.currentIndex = value.notification.mode === "actionable" ? 1 : 0
        loadingForm = false
    }
    function selectJob(id) {
        if (busy) return
        operation = "get"
        track(jobsApi.get(id), "select")
    }
    function schedule() {
        var expression = cronField.text
        if (timing.currentIndex === 1 || timing.currentIndex === 2) {
            if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(timeField.text))
                return null
            var parts = timeField.text.split(":")
            expression = Number(parts[1]) + " " + Number(parts[0]) + " * * "
                + (timing.currentIndex === 1 ? "*" : weekday.currentIndex)
        }
        return {kind: timing.currentIndex === 0 ? "once" : "cron",
                value: timing.currentIndex === 0 ? onceField.text : expression,
                zone: zoneField.text.trim()}
    }
    function invalidatePreview() {
        if (loadingForm) return
        previewSignature = ""; occurrences = []
    }
    function previewSchedule() {
        var value = schedule()
        if (!value) { notice = "Enter a time as HH:MM (00:00 through 23:59)."; return }
        operation = "preview"; notice = ""
        track(jobsApi.preview(value), "preview", {signature: JSON.stringify(value)})
    }
    function bindModel() {
        operation = "binding"; notice = ""
        track(jobsApi.binding(promptField.text), "binding", {prompt: promptField.text})
    }
    function configuration() {
        var value = {title: titleField.text, prompt: promptField.text, schedule: schedule(),
                     execution: clone(bindingValue),
                     notification: saved ? clone(job.notification) : {mode: "all"}}
        value.notification.mode = notification.currentIndex === 1 ? "actionable" : "all"
        if (saved) { value.context = job.context; value.conversation = job.conversation }
        return value
    }
    function saveJob() {
        if (busy || !previewSignature || previewSignature !== JSON.stringify(schedule())) return
        operation = "save"; notice = ""
        track(saved ? jobsApi.update(job.id, revision, configuration()) : jobsApi.create(configuration()), "save")
    }
    function refreshRuns(more) {
        if (!saved) return
        for (var id in pending)
            if (pending[id].purpose === "runs" && pending[id].details.job === job.id) return
        viewingOlderRuns = !!more
        track(jobsApi.listRuns(job.id, 20, more && runs.length ? runs[runs.length - 1].sequence : 0),
              "runs", {job: job.id, more: !!more})
    }
    function mutate(action) {
        if (busy || !saved) return
        operation = action; notice = ""
        if (action === "run_now") {
            var key = job.id + ":" + revision
            // Keep the receipt on ambiguous transport failure; retries reuse it.
            if (!runRequests[key]) runRequests[key] = jobsApi.newRequestId()
            track(jobsApi.runNow(job.id, revision, runRequests[key]), "run", {key: key})
        } else if (action === "delete") {
            track(jobsApi.remove(job.id, revision), "delete")
        } else {
            track(action === "pause" ? jobsApi.pause(job.id, revision) : jobsApi.resume(job.id, revision), "state")
        }
    }
    function inspectRun(id) {
        operation = "result"
        track(jobsApi.readResult(id), "result", {job: job.id})
    }
    function stopRun(id) {
        operation = "stop"
        track(jobsApi.cancelRun(id), "stop")
    }
    function receive(id, action, response) {
        var request = pending[id]
        if (!request) return
        delete pending[id]
        var purpose = request.purpose
        if (purpose !== "jobs" && purpose !== "runs" && purpose !== "conflict"
                && purpose !== "health" && purpose !== "reconcile") operation = ""
        if (response.status !== "ok") {
            notice = response.status + ": " + response.error
            if (purpose === "health") {
                if (readinessAttempts < 10 && visible) readinessTimer.start()
                return
            }
            if (response.status === "conflict" && saved && purpose !== "conflict")
                track(jobsApi.get(job.id), "conflict")
            if (response.status === "unavailable" && purpose !== "conflict" && purpose !== "reconcile") {
                ready = false; readinessAttempts = 0; reconcileAfterReady = saved
                if (visible) readinessTimer.start()
            }
            return
        }
        var value = response.result
        if (purpose === "health") {
            ready = value.available === true
            if (ready) {
                readinessTimer.stop()
                refreshJobs(false)
                if (reconcileAfterReady && saved) track(jobsApi.get(job.id), "reconcile")
                reconcileAfterReady = false
            } else {
                notice = value.error || "Scheduled jobs service is starting or unavailable."
                if (readinessAttempts < 10 && visible) readinessTimer.start()
            }
        } else if (purpose === "reconcile") {
            if (value.revision !== revision) conflictJob = value
            else job = value
        } else if (purpose === "jobs") {
            jobs = request.details.more ? jobs.concat(value) : value
            moreJobs = value.length === 50
        } else if (purpose === "select" || purpose === "save") {
            loadJob(value)
            if (purpose === "save") { notice = "Saved. Next run: " + (value.next_local || "none") + "."; refreshJobs(false) }
        } else if (purpose === "binding" && request.details.prompt === promptField.text) {
            var previous = saved ? job.execution : {}
            bindingValue = {provider: value.provider, profile: value.profile, model: value.model,
                capabilities: value.capabilities, timeout_seconds: previous.timeout_seconds || 900,
                token_budget: previous.token_budget || 8192,
                tool_budget: previous.tool_budget === undefined ? 16 : previous.tool_budget,
                missed_run: previous.missed_run || "coalesce"}
            if (!zoneField.text && value.zone) zoneField.text = value.zone
            notice = (value.warnings || []).join("\n")
            if (!zoneField.text) notice += "\nNo configured time zone. Enter an explicit IANA zone."
        } else if (purpose === "preview" && request.details.signature === JSON.stringify(schedule())) {
            occurrences = value
            previewSignature = value.length ? request.details.signature : ""
            if (!value.length) notice = "Choose a future occurrence."
        } else if (purpose === "conflict") {
            conflictJob = value
        } else if (purpose === "state") {
            // Readback updates execution state, never the unsaved form.
            job = value; revision = value.revision; refreshJobs(false)
            notice = value.enabled ? "Resumed." : "Paused. Active runs continue until stopped."
        } else if (purpose === "delete") {
            resetForm(); refreshJobs(false); notice = "Deleted. Retained results follow the service retention policy."
        } else if (purpose === "run") {
            delete runRequests[request.details.key]
            history = true; refreshRuns(false); notice = "Run queued."
        } else if (purpose === "runs" && request.details.job === job.id) {
            runs = request.details.more ? runs.concat(value) : value; moreRuns = value.length === 20
        } else if (purpose === "result" && request.details.job === job.id) {
            result = value
            track(jobsApi.acknowledgeResult(value.id), "acknowledge")
        } else if (purpose === "stop") {
            if (result.id === value.id) result = value
            refreshRuns(false); notice = "Run stopped."
        } else if (purpose === "acknowledge") {
            notice = "Result marked read."
        }
    }
    onVisibleChanged: {
        if (visible) { ready = false; readinessAttempts = 0; checkReadiness() }
        else { pending = ({}); operation = ""; readinessTimer.stop() }
    }
    onClosing: {
        if (ownsJobsApi && jobsApi) {
            var api = jobsApi
            jobsApi = null
            api.dispose()
        }
    }
    Shortcut { sequence: "Escape"; enabled: root.visible; onActivated: root.close() }
    Connections {
        target: root.jobsApi
        function onCompleted(requestId, action, response) { root.receive(requestId, action, response) }
        function onInvalidated() {
            root.pending = ({}); root.operation = ""; root.jobs = []; root.runRequests = ({})
            root.ready = false; root.reconcileAfterReady = false; readinessTimer.stop()
            root.resetForm(); root.close()
        }
    }
    Timer { id: readinessTimer; interval: 1000; onTriggered: root.checkReadiness() }
    Timer {
        interval: 5000; repeat: true
        running: root.visible && root.history && root.saved && !root.busy && !root.viewingOlderRuns
        onTriggered: root.refreshRuns(false)
    }
    component Note: Text {
        color: theme.muted; font.family: "DejaVu Sans"; font.pixelSize: 13
        textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true
    }
    component Action: Button {
        id: control
        padding: 8; implicitHeight: 36; hoverEnabled: true
        Accessible.name: text
        contentItem: Text {
            text: control.text; color: control.enabled ? theme.ink : theme.muted
            font.family: "DejaVu Sans"; font.pixelSize: 13
            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 4; color: control.hovered ? theme.horizon : theme.input
            border.width: 1; border.color: control.activeFocus ? theme.accent : theme.line
        }
    }
    component Field: TextField {
        id: field
        Layout.fillWidth: true; color: theme.ink; placeholderTextColor: theme.muted
        font.family: "DejaVu Sans"; font.pixelSize: 14
        padding: 8; selectByMouse: true
        background: Rectangle { color: theme.input; radius: 4; border.width: 1; border.color: field.activeFocus ? theme.accent : theme.line }
    }
    component Choice: ComboBox {
        id: choice
        implicitHeight: 36; Layout.minimumHeight: 36
        Layout.fillWidth: true; font.family: "DejaVu Sans"; font.pixelSize: 13
        palette.button: theme.input; palette.buttonText: theme.ink
        palette.base: theme.input; palette.text: theme.ink
        palette.highlight: theme.horizon; palette.highlightedText: theme.ink
        background: Rectangle { color: theme.input; radius: 4; border.width: 1; border.color: choice.activeFocus ? theme.accent : theme.line }
    }
    Rectangle { anchors.fill: parent; color: theme.panel; radius: theme.windowRadius }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 24; spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Item {
                Layout.fillWidth: true; Layout.preferredHeight: 36
                WindowTitle { anchors.fill: parent; theme: root.theme; text: root.title }
                MouseArea { anchors.fill: parent; onPressed: root.startSystemMove() }
            }
            WindowControlButton { theme: root.theme; symbol: "\u00d7"; tip: "Close scheduled jobs"; onClicked: root.close() }
        }
        Note {
            text: root.jobsApi && root.jobsApi.protectedWorkspace === true
                ? "Jobs stay in this encrypted workspace. Locking or leaving it stops active runs and hides results."
                : "Runs while AIOS is on. Installed storage persists jobs; ordinary live sessions are temporary."
        }
        Note { objectName: "scheduledNotice"; text: root.notice; visible: text.length > 0; color: theme.ink }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 16
            ColumnLayout {
                Layout.preferredWidth: Math.min(200, root.width * 0.26); Layout.fillHeight: true; spacing: 8
                Action { objectName: "newScheduledJob"; text: "New job"; Layout.fillWidth: true; enabled: !root.busy; onClicked: root.resetForm() }
                Action { text: "Refresh list"; Layout.fillWidth: true; enabled: !root.busy; onClicked: root.refreshJobs(false) }
                ListView {
                    id: jobList; objectName: "scheduledJobList"
                    Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 4
                    model: root.jobs
                    ScrollBar.vertical: ScrollBar {}
                    delegate: Action {
                        required property var modelData
                        width: jobList.width; implicitHeight: 64; enabled: !root.busy
                        text: modelData.title + "\n" + (modelData.enabled ? "Enabled" : "Paused")
                        Accessible.description: modelData.next_local || "No next run"
                        background: Rectangle {
                            radius: 4; color: modelData.id === root.job.id ? theme.horizon : theme.input
                            border.width: 1; border.color: parent.activeFocus ? theme.accent : theme.line
                        }
                        onClicked: root.selectJob(modelData.id)
                    }
                }
                Note { visible: root.jobs.length === 0; text: "No saved jobs." }
                Action { text: "More jobs"; Layout.fillWidth: true; visible: root.moreJobs; enabled: !root.busy; onClicked: root.refreshJobs(true) }
            }
            Rectangle { Layout.fillHeight: true; implicitWidth: 1; color: theme.line }
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true; spacing: 8
                RowLayout {
                    Layout.fillWidth: true
                    Action { text: "Configuration"; enabled: !root.busy; onClicked: root.history = false }
                    Action { objectName: "scheduledHistory"; text: "History"; enabled: root.saved && !root.busy; onClicked: { root.history = true; root.refreshRuns(false) } }
                    Item { Layout.fillWidth: true }
                }
                ScrollView {
                    id: form; objectName: "scheduledForm"
                    visible: !root.history; Layout.fillWidth: true; Layout.fillHeight: true
                    clip: true; contentWidth: availableWidth
                    ColumnLayout {
                        width: form.availableWidth; spacing: 8
                        enabled: !root.busy && root.ready
                        Note { text: "Title" }
                        Field { id: titleField; objectName: "scheduledTitle"; Accessible.name: "Job title"; maximumLength: 200 }
                        Note { text: "Task prompt" }
                        TextArea {
                            id: promptField; objectName: "scheduledPrompt"
                            Layout.fillWidth: true; Layout.minimumHeight: 112
                            Accessible.name: "Scheduled task prompt"; selectByMouse: true
                            wrapMode: TextEdit.Wrap; color: theme.ink; padding: 8
                            font.family: "DejaVu Sans"; font.pixelSize: 14
                            background: Rectangle { color: theme.input; radius: 4; border.width: 1; border.color: promptField.activeFocus ? theme.accent : theme.line }
                        }
                        Note { text: "Timing" }
                        Choice { id: timing; objectName: "scheduledTiming"; Accessible.name: "Schedule kind"; model: ["Once", "Daily", "Weekly", "Custom numeric cron"]; currentIndex: 1; onCurrentIndexChanged: root.invalidatePreview() }
                        Field { id: onceField; objectName: "scheduledOnce"; visible: timing.currentIndex === 0; Accessible.name: "Once timestamp with UTC offset"; placeholderText: "2026-12-01T09:00:00-08:00"; onTextChanged: root.invalidatePreview() }
                        Note { visible: timing.currentIndex === 0; text: "Include an explicit UTC offset. Preview shows the instant in the selected zone." }
                        Field { id: timeField; objectName: "scheduledTime"; visible: timing.currentIndex === 1 || timing.currentIndex === 2; text: "09:00"; Accessible.name: "Time in selected zone"; placeholderText: "HH:MM"; onTextChanged: root.invalidatePreview() }
                        Choice { id: weekday; visible: timing.currentIndex === 2; Accessible.name: "Day of week"; model: ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]; currentIndex: 1; onCurrentIndexChanged: root.invalidatePreview() }
                        Field { id: cronField; objectName: "scheduledCron"; visible: timing.currentIndex === 3; Accessible.name: "Five numeric cron fields"; text: "0 9 * * 1-5"; onTextChanged: root.invalidatePreview() }
                        Note { visible: timing.currentIndex === 3; text: "Minute hour day-of-month month weekday. Numbers, lists, ranges, steps and * only; no commands." }
                        Note { text: "IANA time zone (required)" }
                        Field { id: zoneField; objectName: "scheduledZone"; Accessible.name: "IANA time zone"; placeholderText: "America/Los_Angeles"; maximumLength: 100; onTextChanged: root.invalidatePreview() }
                        Note { text: "Model and binding" }
                        Note {
                            objectName: "scheduledBinding"
                            text: root.bindingValue.model ? root.bindingValue.provider + " / " + root.bindingValue.model + "\n" + root.bindingValue.profile
                                + "\nCapabilities: " + root.bindingValue.capabilities.join(", ") : "No model bound. Resolve the configured task model before saving."
                        }
                        Action { objectName: "scheduledBind"; text: "Use configured task model"; enabled: promptField.text.trim().length > 0; onClicked: root.bindModel() }
                        Note { text: "Choose models in Settings > AI models. This button explicitly rebinds the job; saved jobs never silently switch providers or gain tools." }
                        Note { text: "Notifications" }
                        Choice { id: notification; Accessible.name: "Notification mode"; model: ["All completed results", "Actionable results only"] }
                        Action { objectName: "scheduledPreview"; text: "Preview next occurrences"; onClicked: root.previewSchedule() }
                        Repeater {
                            model: root.occurrences
                            Note { required property var modelData; text: modelData.local + "\nUTC: " + modelData.utc }
                        }
                        Note { visible: root.conflictJob.id !== undefined; text: "This job changed. Your form is preserved. Current saved revision: " + (root.conflictJob.revision || "") }
                        Note {
                            visible: root.conflictJob.id !== undefined
                            text: root.conflictJob.id ? root.conflictJob.title + "\n" + root.conflictJob.prompt + "\n"
                                + JSON.stringify(root.conflictJob.schedule) : ""
                        }
                        Action {
                            visible: root.conflictJob.id !== undefined; text: "Keep draft against this revision"
                            onClicked: { root.job = root.conflictJob; root.revision = root.conflictJob.revision; root.conflictJob = ({}); root.invalidatePreview(); root.notice = "Review and preview your draft before saving." }
                        }
                        Action { visible: root.conflictJob.id !== undefined; text: "Discard draft and load saved job"; onClicked: root.loadJob(root.conflictJob) }
                        Action {
                            objectName: "saveScheduledJob"; text: root.saved ? "Save changes" : "Create job"
                            enabled: !!root.bindingValue.model && titleField.text.trim().length > 0 && promptField.text.trim().length > 0
                                && root.previewSignature !== "" && !root.conflictJob.id
                            onClicked: root.saveJob()
                        }
                        Flow {
                            Layout.fillWidth: true; spacing: 8; visible: root.saved
                            Action { objectName: "scheduledPause"; text: root.job.enabled ? "Pause" : "Resume"; onClicked: root.mutate(root.job.enabled ? "pause" : "resume") }
                            Action { objectName: "scheduledRunNow"; text: "Run now"; onClicked: root.mutate("run_now") }
                            Action { text: "Delete"; onClicked: root.deleteConfirm = true }
                        }
                        Note { visible: root.deleteConfirm; text: "Delete this job and stop its active runs? Retained history is kept for 30 days / 1,000 runs; unread results are not automatically pruned." }
                        Action { visible: root.deleteConfirm; text: "Confirm delete"; onClicked: root.mutate("delete") }
                        Action { visible: root.deleteConfirm; text: "Keep job"; onClicked: root.deleteConfirm = false }
                    }
                }
                ScrollView {
                    id: historyView; objectName: "scheduledRunHistory"
                    visible: root.history; Layout.fillWidth: true; Layout.fillHeight: true
                    clip: true; contentWidth: availableWidth
                    ColumnLayout {
                        width: historyView.availableWidth; spacing: 8
                        Action { text: "Refresh history"; enabled: !root.busy; onClicked: root.refreshRuns(false) }
                        Note { visible: root.runs.length === 0; text: "No runs yet." }
                        Repeater {
                            model: root.runs
                            ColumnLayout {
                                required property var modelData
                                Layout.fillWidth: true; spacing: 4
                                Note { text: modelData.state + " / " + modelData.trigger + " / revision " + modelData.revision + "\n" + modelData.scheduled_at }
                                RowLayout {
                                    Action { text: "Inspect result"; enabled: !root.busy; onClicked: root.inspectRun(modelData.id) }
                                    Action { text: "Stop"; visible: modelData.state === "queued" || modelData.state === "running"; enabled: !root.busy; onClicked: root.stopRun(modelData.id) }
                                }
                            }
                        }
                        Action { text: "Older runs"; visible: root.moreRuns; enabled: !root.busy; onClicked: root.refreshRuns(true) }
                        Note { visible: !!root.result.id; text: "Saved result / " + (root.result.state || "") }
                        TextArea {
                            objectName: "scheduledResult"; visible: !!root.result.id
                            Layout.fillWidth: true; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                            textFormat: TextEdit.PlainText; color: theme.ink
                            font.family: "DejaVu Sans"; font.pixelSize: 14
                            Accessible.name: "Saved scheduled job result"
                            text: (root.result.result || "") + (root.result.error ? "\n" + root.result.error : "")
                            background: Rectangle { color: theme.input; radius: 4 }
                        }
                        Note { visible: !!root.result.id; text: root.result.id ? "Run: " + root.result.id + "\nStarted: " + (root.result.started || "not started") + "\nEnded: " + (root.result.ended || "not ended") : "" }
                        Action {
                            text: "Mark result read"; visible: !!root.result.id && root.result.state !== "queued" && root.result.state !== "running"; enabled: !root.busy
                            onClicked: { root.operation = "acknowledge"; root.track(root.jobsApi.acknowledgeResult(root.result.id), "acknowledge") }
                        }
                    }
                }
            }
        }
    }
    WindowBorder { theme: root.theme }
}
