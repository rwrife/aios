import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    required property var control
    property bool creating: false
    title: creating ? "Create a private profile" : "Unlock your profile"
    modal: true; width: 440
    standardButtons: control.busy ? Dialog.NoButton : Dialog.Close
    closePolicy: control.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    onOpened: control.setSecureInput(true)
    onClosed: { profileName.clear(); pin.clear(); consent.checked = false; recovery.clear(); control.setSecureInput(false); }
    onActiveFocusChanged: { if (!activeFocus) pin.clear(); }
    Connections {
        target: control
        function onPrivacyLost() { pin.clear(); recovery.clear(); dialog.close(); }
        function onEnrollmentCompleted(secret) { pin.clear(); recovery.text = secret; }
        function onUnlocked() { dialog.close(); }
    }
    ColumnLayout {
        anchors.fill: parent; spacing: 10
        Label {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: "PIN access works without a camera and locks after two minutes. Biometric enrollment is separate."
        }
        TextField { id: profileName; objectName: "profileName"; placeholderText: "Profile name"; maximumLength: 80; Layout.fillWidth: true }
        TextField {
            id: pin; objectName: "enrollmentPin"; placeholderText: "PIN or passphrase"
            echoMode: TextInput.Password; maximumLength: 128
            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
            Layout.fillWidth: true
        }
        CheckBox {
            id: consent; objectName: "profileConsent"; visible: dialog.creating
            text: "Create a local encrypted workspace"
        }
        Button {
            objectName: "profileSubmit"
            text: dialog.creating ? "Create profile" : "Unlock"
            enabled: !control.busy && profileName.text.trim().length > 0 && pin.text.length >= 6 && (!dialog.creating || consent.checked)
            onClicked: {
                if (dialog.creating) control.enroll(profileName.text, pin.text, consent.checked)
                else control.unlock(profileName.text, pin.text)
                pin.clear()
            }
        }
        Label { visible: control.busy; text: "Creating your encrypted workspace…"; Layout.fillWidth: true }
        Label {
            visible: recovery.text.length > 0; Layout.fillWidth: true; wrapMode: Text.Wrap
            text: "Save this recovery code somewhere private. It is shown only now. Then close this dialog and unlock your profile."
        }
        TextArea {
            id: recovery; visible: text.length > 0; readOnly: true; selectByMouse: true
            textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap; Layout.fillWidth: true
        }
        Label { text: control.error; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
    }
}
