import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ScrollView {
    id: page
    required property var backend
    required property var theme
    property bool active: false
    readonly property var state: backend.clockState || ({})
    readonly property bool available: typeof state.utc === "string"
    readonly property bool syncing: (state.sync_daemons || []).length > 0
    clip: true
    contentWidth: availableWidth
    onActiveChanged: if (active) backend.clockRequest()
    Connections {
        target: page.backend
        function onClockChanged() {
            if (!instant.text && page.available) instant.text = page.state.utc
        }
    }
    component Note: Text {
        Layout.fillWidth: true
        color: page.theme.muted
        font.pixelSize: 13
        wrapMode: Text.Wrap
        textFormat: Text.PlainText
    }
    component Action: Button {
        id: action
        padding: 12
        implicitHeight: 44
        hoverEnabled: true
        Accessible.name: text
        contentItem: Text {
            text: action.text
            color: action.enabled ? page.theme.ink : page.theme.muted
            font.pixelSize: 14
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            color: action.hovered ? page.theme.horizon : page.theme.input
            radius: 8
            border.width: 1
            border.color: action.activeFocus ? page.theme.accent : page.theme.line
        }
    }
    ColumnLayout {
        width: page.availableWidth
        spacing: 16
        Note { text: "Read or change this machine's system clock. All applications and chats use this clock." }
        Text {
            Layout.fillWidth: true
            text: page.available ? page.state.utc : "Clock unavailable"
            objectName: "machineClockValue"
            color: page.theme.ink
            font.pixelSize: 20
            wrapMode: Text.Wrap
            textFormat: Text.PlainText
            Accessible.name: "Machine clock, UTC: " + text
        }
        Note { text: page.available ? "Local: " + page.state.local + " (" + page.state.timezone + ")" : "Refresh to read the guest clock." }
        Action {
            objectName: "refreshClock"
            text: page.backend.clockBusy ? "Working..." : "Refresh clock"
            enabled: !page.backend.clockBusy
            onClicked: page.backend.clockRequest()
        }
        Note {
            text: page.syncing
                ? "Automatic time sync is running (" + page.state.sync_daemons.join(", ") + "). An administrator must stop it before a manual change."
                : "Manual time. Values shown are from the last read, not a live preview."
            visible: page.available
        }
        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: page.theme.line }
        Note { text: "New date and time"; color: page.theme.ink }
        TextField {
            id: instant
            objectName: "dateTimeInput"
            Layout.fillWidth: true
            enabled: !page.backend.clockBusy
            selectByMouse: true
            maximumLength: 25
            placeholderText: "2026-09-11T14:30:00Z"
            Accessible.name: "New date and time with UTC offset"
            color: page.theme.ink
            placeholderTextColor: page.theme.muted
            font.pixelSize: 14
            padding: 12
            background: Rectangle {
                color: page.theme.input; radius: 8
                border.width: 1
                border.color: instant.activeFocus ? page.theme.accent : page.theme.line
            }
        }
        Note { text: "Use YYYY-MM-DDTHH:MM:SSZ for UTC, or an explicit offset such as 2026-09-11T07:30:00-07:00. Supported years: 2000-2099. This does not change the system timezone." }
        Action {
            objectName: "applyClock"
            text: "Set machine time"
            enabled: page.available && !page.syncing && !page.backend.clockBusy && instant.text.length > 0
            onClicked: page.backend.clockRequest(instant.text)
        }
        Note {
            objectName: "clockNotice"
            text: page.backend.clockNotice || ""
            visible: text.length > 0
            Accessible.role: Accessible.StaticText
        }
        Note { text: "AIOS also tries to save the UTC hardware clock. If unavailable, changes last only until reboot. A virtual machine's RTC policy may restore the host's time at the next boot."; font.pixelSize: 12 }
    }
}
