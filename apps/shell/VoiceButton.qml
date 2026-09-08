import QtQuick
import QtQuick.Controls

Button {
    id: control
    required property var theme
    property bool active: false
    property string tip: active ? "Finish voice recording" : "Dictate a message"
    readonly property bool illuminated: active || down
    implicitWidth: 86; implicitHeight: 36
    Accessible.name: "Voice"
    Accessible.description: active ? "Microphone is recording" : "Start voice recording"
    Accessible.role: Accessible.CheckBox
    Accessible.checked: active
    background: Item {
        Rectangle {
            anchors.fill: parent; anchors.margins: -2; radius: 10
            color: control.theme.accent; opacity: control.illuminated ? 0.08 : 0
            Behavior on opacity { NumberAnimation { duration: 120 } }
        }
        Rectangle {
            anchors.fill: parent; radius: 7
            color: control.illuminated ? control.theme.horizon : control.hovered ? control.theme.panel : "transparent"
            border.color: control.illuminated || control.activeFocus ? control.theme.accent : "transparent"
            border.width: 1
            Behavior on color { ColorAnimation { duration: 120 } }
        }
    }
    contentItem: Row {
        spacing: 7; opacity: control.enabled ? 1 : 0.4
        Item {
            width: 16; height: parent.height
            Row {
                anchors.centerIn: parent; spacing: 2
                Repeater {
                    model: [5, 11, 15, 9, 5]
                    Rectangle {
                        required property int modelData
                        width: 1.5; height: modelData; radius: 0.75
                        anchors.verticalCenter: parent.verticalCenter
                        color: control.illuminated ? control.theme.accent : control.theme.muted
                        Behavior on color { ColorAnimation { duration: 120 } }
                    }
                }
            }
        }
        Text {
            text: "Voice"; height: parent.height; verticalAlignment: Text.AlignVCenter
            font.pixelSize: 13; color: control.illuminated ? control.theme.ink : control.theme.muted
            Behavior on color { ColorAnimation { duration: 120 } }
        }
    }
    ToolTip.visible: hovered || activeFocus
    ToolTip.text: tip
    ToolTip.delay: 600
}
