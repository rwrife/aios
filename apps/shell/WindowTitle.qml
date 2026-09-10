import QtQuick

Text {
    required property var theme
    color: theme.ink
    font.pixelSize: 22
    font.weight: Font.Medium
    elide: Text.ElideRight
    verticalAlignment: Text.AlignVCenter
}
