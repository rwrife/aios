import QtQuick
import QtQuick.Controls
import QtQuick.Window

Window {
    required property var control
    flags: Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
    visibility: control.enabled && control.shield ? Window.FullScreen : Window.Hidden
    color: "#101b27"
    onVisibleChanged: if (visible) { raise(); requestActivate() }
    Column {
        anchors.centerIn: parent; spacing: 16
        Label { text: "Personal work is hidden"; color: "white"; font.pixelSize: 28 }
        Label { text: "Return to anonymous mode to continue."; color: "#bde4e6" }
        Button { text: "Return to anonymous"; onClicked: control.suspend() }
    }
}
