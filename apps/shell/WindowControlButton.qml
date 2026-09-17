import QtQuick
import QtQuick.Controls

Button {
    id: control
    required property var theme
    property string symbol
    property string tip: symbol
    Accessible.name: tip
    implicitWidth: 36
    implicitHeight: 36
    hoverEnabled: true
    contentItem: Item {
        readonly property color iconColor: control.enabled ? control.theme.ink : control.theme.muted
        opacity: control.enabled ? 0.8 : 0.4
        Text {
            anchors.fill: parent
            visible: control.symbol !== "copy"
            text: control.symbol
            color: parent.iconColor
            font.pixelSize: 22
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        Rectangle {
            visible: control.symbol === "copy"
            x: 9
            y: 8
            width: 13
            height: 13
            color: "transparent"
            border.width: 1
            border.color: parent.iconColor
        }
        Rectangle {
            visible: control.symbol === "copy"
            x: 13
            y: 12
            width: 13
            height: 13
            color: "transparent"
            border.width: 1
            border.color: parent.iconColor
        }
    }
    background: Rectangle {
        radius: 8
        color: control.down || control.hovered ? control.theme.input : "transparent"
        border.width: control.activeFocus ? 2 : 0
        border.color: control.theme.accent
    }
    ToolTip.visible: hovered || activeFocus
    ToolTip.text: tip
    ToolTip.delay: activeFocus ? 0 : 700
}
