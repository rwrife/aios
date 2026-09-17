import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import "WindowSizing.js" as WindowSizing

Window {
    id: debugWindow
    required property var session
    required property var theme
    title: "Chat debug trace"
    visible: true
    flags: Qt.Window | Qt.FramelessWindowHint
    width: WindowSizing.extent(640, Screen.width, Screen.desktopAvailableWidth)
    height: WindowSizing.extent(440, Screen.height, Screen.desktopAvailableHeight)
    x: Screen.virtualX + (Screen.width - width) / 2
    y: Screen.virtualY + WindowSizing.topCenterY(Screen.height, height)
    color: "transparent"

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
            Layout.preferredHeight: 40
            WindowTitle {
                Layout.fillWidth: true
                theme: debugWindow.theme
                text: "Raw model trace"
            }
            WindowControlButton {
                theme: debugWindow.theme
                symbol: "⌫"
                tip: "Clear debug trace"
                enabled: session.debugLog.length > 0
                onClicked: session.clearDebugLog()
            }
            WindowControlButton {
                theme: debugWindow.theme
                symbol: "copy"
                tip: "Copy debug trace"
                enabled: session.debugLog.length > 0
                onClicked: session.copy(session.debugLog)
            }
            WindowControlButton {
                theme: debugWindow.theme
                symbol: "×"
                tip: "Close debug trace"
                onClicked: debugWindow.close()
            }
        }

        Text {
            Layout.fillWidth: true
            text: "Per-chat, in-memory provider requests, responses, and tool activity. Authorization headers are not recorded."
            color: theme.muted
            font.pixelSize: 12
            wrapMode: Text.Wrap
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            background: Rectangle {
                color: theme.night
                radius: 8
                border.width: 1
                border.color: theme.line
            }
            TextArea {
                objectName: "chatDebugText"
                text: session.debugLog.length > 0 ? session.debugLog : "No model activity recorded yet."
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.NoWrap
                color: session.debugLog.length > 0 ? theme.ink : theme.muted
                font.family: "DejaVu Sans Mono"
                font.pixelSize: 12
                padding: 12
                background: null
            }
        }
    }

    WindowBorder { theme: debugWindow.theme }
}
