import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import "WindowSizing.js" as WindowSizing

Window {
    id: root
    required property var jobsApi
    required property var feedback
    required property var theme
    property var result: ({})
    property var history: []
    property var pending: ({})
    property string notice: ""
    property bool loading: false
    title: "Scheduled results"
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "transparent"
    width: WindowSizing.extent(960, Screen.width, Screen.desktopAvailableWidth)
    height: WindowSizing.extent(720, Screen.height, Screen.desktopAvailableHeight)
    minimumWidth: WindowSizing.extent(560, Screen.width, Screen.desktopAvailableWidth)
    minimumHeight: WindowSizing.extent(440, Screen.height, Screen.desktopAvailableHeight)
    x: Screen.virtualX + (Screen.width - width) / 2
    y: Screen.virtualY + (Screen.height - height) / 2
    signal newChatRequested()
    signal followUpRequested(string draft)

    function track(id, purpose, details) {
        pending[id] = {purpose: purpose, details: details || {}}
        return id
    }
    function openResult(runId) {
        if (loading)
            return
        loading = true
        notice = ""
        track(jobsApi.readResult(runId), "read", {run: runId})
    }
    function markRead(runId) {
        if (!runId)
            return
        track(jobsApi.acknowledgeResult(runId), "acknowledge", {run: runId})
    }
    function bounded(value, limit) {
        value = value || ""
        return value.length > limit ? value.slice(0, limit) + "\n[truncated]" : value
    }
    function followUpDraft() {
        if (!result.id)
            return ""
        var snapshot = result.snapshot || {}
        var body = result.result || result.error || "No saved result body."
        return "Follow up on this scheduled result.\n\nJob: "
            + bounded(snapshot.title || "Scheduled job", 200)
            + "\nOriginal task:\n" + bounded(snapshot.prompt || "", 1024)
            + (snapshot.context ? "\nSaved context:\n" + bounded(snapshot.context, 2048) : "")
            + "\n\nSaved result (" + (result.state || "completed") + "):\n"
            + bounded(body, 8192)
    }
    function receive(id, action, response) {
        var request = pending[id]
        if (!request)
            return
        delete pending[id]
        if (response.status !== "ok") {
            loading = false
            notice = response.status + ": " + response.error
            return
        }
        if (request.purpose === "read") {
            result = response.result
            loading = false
            track(jobsApi.listRuns(result.job_id, 10, 0), "history")
            markRead(result.id)
        } else if (request.purpose === "history") {
            history = response.result || []
        } else if (request.purpose === "acknowledge") {
            feedback.removeRun(request.details.run)
            notice = "Result marked read."
        }
    }
    onVisibleChanged: {
        if (visible) {
            pending = ({})
            notice = ""
        }
    }
    Shortcut { sequence: "Escape"; enabled: root.visible; onActivated: root.close() }
    Connections {
        target: root.jobsApi
        function onCompleted(requestId, action, response) {
            root.receive(requestId, action, response)
        }
        function onInvalidated() {
            root.result = ({})
            root.history = []
            root.pending = ({})
            root.close()
        }
    }
    component Note: Text {
        color: theme.muted
        font.family: "DejaVu Sans"
        font.pixelSize: 13
        textFormat: Text.PlainText
        wrapMode: Text.Wrap
        Layout.fillWidth: true
    }
    component Action: Button {
        id: action
        padding: 8
        implicitHeight: 36
        hoverEnabled: true
        Accessible.name: text
        Accessible.description: text
        contentItem: Text {
            text: action.text
            color: action.enabled ? theme.ink : theme.muted
            font.family: "DejaVu Sans"
            font.pixelSize: 13
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 4
            color: action.hovered ? theme.horizon : theme.input
            border.width: 1
            border.color: action.activeFocus ? theme.accent : theme.line
        }
        ToolTip.visible: hovered || activeFocus
        ToolTip.text: action.text
        ToolTip.delay: activeFocus ? 0 : 700
    }
    Rectangle {
        anchors.fill: parent
        color: theme.panel
        radius: theme.windowRadius
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 24
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 36
                WindowTitle { anchors.fill: parent; theme: root.theme; text: root.title }
                MouseArea { anchors.fill: parent; onPressed: root.startSystemMove() }
            }
            WindowControlButton {
                theme: root.theme
                symbol: "\u00d7"
                tip: "Close scheduled results"
                onClicked: root.close()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Note {
                text: feedback.unreadCount + (feedback.unreadCount === 1 ? " unread result" : " unread results")
            }
            Action {
                objectName: "resultNewChat"
                text: "New chat"
                onClicked: root.newChatRequested()
            }
        }
        Note {
            visible: notice.length > 0
            text: notice
            color: theme.ink
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 16
            ColumnLayout {
                Layout.preferredWidth: Math.min(300, root.width * 0.34)
                Layout.fillHeight: true
                ListView {
                    id: inbox
                    objectName: "scheduledResultInbox"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 8
                    model: feedback.entries
                    ScrollBar.vertical: ScrollBar {}
                    delegate: Rectangle {
                        required property var modelData
                        width: inbox.width
                        height: 112
                        radius: 4
                        color: root.result.id === modelData.run_id ? theme.horizon : theme.input
                        border.width: 1
                        border.color: theme.line
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 8
                            spacing: 4
                            Text {
                                Layout.fillWidth: true
                                text: modelData.title
                                color: theme.ink
                                font.family: "DejaVu Sans"
                                font.pixelSize: 14
                                elide: Text.ElideRight
                            }
                            Note {
                                text: modelData.state + " \u00b7 " + (modelData.ended || modelData.scheduled_at)
                                elide: Text.ElideRight
                            }
                            RowLayout {
                                Action {
                                    objectName: "viewScheduledResult"
                                    text: "View result"
                                    onClicked: root.openResult(modelData.run_id)
                                }
                                Action {
                                    objectName: "markScheduledResultRead"
                                    text: "Mark read"
                                    onClicked: root.markRead(modelData.run_id)
                                }
                            }
                        }
                    }
                }
                Note {
                    visible: feedback.entries.length === 0
                    text: "No unread scheduled results."
                }
            }
            Rectangle { Layout.fillHeight: true; implicitWidth: 1; color: theme.line }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 8
                Note {
                    visible: !result.id
                    text: "Select a result to view its saved answer. Opening this inbox does not mark anything read."
                }
                Text {
                    visible: !!result.id
                    Layout.fillWidth: true
                    text: result.snapshot ? result.snapshot.title : ""
                    color: theme.ink
                    font.family: "DejaVu Sans"
                    font.pixelSize: 20
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                }
                Note {
                    visible: !!result.id
                    text: result.id ? (result.state + "\nScheduled: " + result.scheduled_at
                        + "\nStarted: " + (result.started || "not started")
                        + "\nFinished: " + (result.ended || "not finished")) : ""
                }
                ScrollView {
                    visible: !!result.id
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    TextArea {
                        id: savedResult
                        objectName: "savedScheduledResult"
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        textFormat: TextEdit.PlainText
                        color: theme.ink
                        font.family: "DejaVu Sans"
                        font.pixelSize: 14
                        Accessible.name: "Saved scheduled result"
                        text: (result.result || "") + (result.error ? "\n" + result.error : "")
                        background: Rectangle { color: theme.input; radius: 4 }
                    }
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: 8
                    visible: !!result.id
                    Action {
                        objectName: "followUpScheduledResult"
                        text: "Follow up"
                        onClicked: root.followUpRequested(root.followUpDraft())
                    }
                    Action {
                        text: "Mark read"
                        onClicked: root.markRead(result.id)
                    }
                }
                Note {
                    visible: history.length > 0
                    text: "Recent run history: " + history.map(function(run) {
                        return run.state + " " + run.scheduled_at
                    }).join(" \u00b7 ")
                }
            }
        }
    }
    WindowBorder { theme: root.theme }
}
