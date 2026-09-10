import QtQuick

Rectangle {
    required property var theme
    anchors.fill: parent
    z: 1000000
    color: "transparent"
    border.width: 1
    border.color: theme.waveAlpha(0.5)
    enabled: false
}
