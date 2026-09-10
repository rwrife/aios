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
    contentItem: Text {
        text: control.symbol
        color: control.enabled ? control.theme.ink : control.theme.muted
        opacity: control.enabled ? 0.8 : 0.4
        font.pixelSize: 22
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
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
