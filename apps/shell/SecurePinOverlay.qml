import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Popup {
    id: prompt
    required property var control
    anchors.centerIn: parent; width: 460
    modal: true; z: 1000000; closePolicy: Popup.NoAutoClose
    visible: control.enabled && Object.keys(control.challenge).length > 0
    onVisibleChanged: {
        pin.clear(); control.setSecureInput(visible)
        if (visible) pin.forceActiveFocus()
    }
    Connections {
        target: prompt.parent.Window.window
        function onActiveChanged() {
            if (prompt.visible && !prompt.parent.Window.window.active) {
                pin.clear(); control.cancelChallenge()
            }
        }
    }
    Timer { interval: 30000; running: prompt.visible; onTriggered: { pin.clear(); control.cancelChallenge() } }
    contentItem: ColumnLayout {
        spacing: 12
        Label { text: "Authorize access"; font.pixelSize: 22 }
        Label {
            text: (control.challenge.operation || "") + "\n" + (control.challenge.resource || "")
            textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true
        }
        TextField {
            id: pin; objectName: "secureOverlayPin"; echoMode: TextInput.Password
            maximumLength: 128; placeholderText: "PIN or passphrase"; Layout.fillWidth: true
            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
        }
        RowLayout {
            Button { text: "Cancel"; onClicked: { pin.clear(); control.cancelChallenge() } }
            Button { text: "Authorize"; onClicked: { control.verify(pin.text); pin.clear() } }
        }
    }
}
