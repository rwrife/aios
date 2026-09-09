import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

// This surface is only as trusted as the enclosing compositor. X11 is not a
// secure PIN boundary; production activation remains gated in the broker.
Window {
    id: prompt
    required property var control
    visible: control.enabled && Object.keys(control.challenge).length > 0
    width: 460; height: 280; title: "AIOS authorization"
    modality: Qt.ApplicationModal
    flags: Qt.Dialog | Qt.WindowStaysOnTopHint
    color: "#172633"
    onVisibleChanged: {
        pin.clear()
        if (visible) { raise(); requestActivate(); pin.forceActiveFocus() }
    }
    onActiveChanged: if (visible && !active) { pin.clear(); control.cancelChallenge() }
    onClosing: { pin.clear(); control.cancelChallenge() }
    Timer { interval: 30000; running: prompt.visible; onTriggered: { pin.clear(); control.cancelChallenge() } }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 24; spacing: 12
        Label { text: control.simulator ? "Simulator PIN" : "Authorize access"; color: "white"; font.pixelSize: 22 }
        Label { text: (control.challenge.owner || "") + "\n" + (control.challenge.operation || "") + "\n" + (control.challenge.resource || ""); color: "#bde4e6"; wrapMode: Text.Wrap; Layout.fillWidth: true }
        TextField { id: pin; objectName: "securePinInput"; echoMode: TextInput.Password; maximumLength: 128; placeholderText: "PIN or passphrase"; Layout.fillWidth: true }
        RowLayout {
            Button { text: "Cancel"; onClicked: { pin.clear(); control.cancelChallenge() } }
            Button { text: "Authorize"; onClicked: { control.verify(pin.text); pin.clear() } }
        }
    }
}
