import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ColumnLayout {
    id: accounts
    property var control: null
    property var theme: fallbackTheme
    Theme { id: fallbackTheme }
    property color ink: "#e4edf1"
    property string result: ""
    spacing: 12
    function refresh() { if (control && control.greetingOnly) control.listProfiles() }
    onVisibleChanged: { if (visible) refresh(); else { deletion.close(); faceEnrollment.close(); } }
    Component.onCompleted: if (visible) refresh()
    Label { text: "Accounts on this device"; color: accounts.ink; font.pixelSize: 20 }
    Label { text: "Sign in, add optional face recognition, or delete an account. Face enrollment and deletion require that account’s PIN or password."; color: accounts.ink; wrapMode: Text.Wrap; Layout.fillWidth: true }
    ScrollView {
        Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 100
        ColumnLayout {
            width: parent.width
            Repeater {
                model: accounts.control && accounts.control.profiles ? accounts.control.profiles : []
                RowLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    Label { text: modelData.name; color: accounts.ink; textFormat: Text.PlainText; elide: Text.ElideRight; Layout.fillWidth: true }
                    Button { text: "Sign in"; onClicked: { enrollment.creating = false; enrollment.selectedProfile = modelData.name; enrollment.selectedProfileId = modelData.id; enrollment.open(); } }
                    Button {
                        objectName: "enrollRecognition"; text: "Face recognition…"
                        enabled: accounts.control && accounts.control.greetingOnly && !accounts.control.busy
                        onClicked: {
                            accounts.result = ""
                            faceEnrollment.openForProfile(modelData.id, modelData.name)
                        }
                    }
                    Button {
                        objectName: "deleteAccount"; text: "Delete…"
                        enabled: accounts.control && accounts.control.greetingOnly && !accounts.control.busy
                        onClicked: { deletion.accountId = modelData.id; deletion.accountName = modelData.name; deletion.open(); }
                    }
                }
            }
        }
    }
    Button {
        text: "New account"; enabled: accounts.control && accounts.control.personalAvailable
        onClicked: { enrollment.creating = true; enrollment.open(); }
    }
    Label { text: accounts.result; color: accounts.ink; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
    Label { visible: !accounts.control || !accounts.control.greetingOnly; text: "Protected workspace deletion is not available in this panel."; color: accounts.ink; wrapMode: Text.Wrap; Layout.fillWidth: true }
    EnrollmentFlow { id: enrollment; parent: Overlay.overlay; anchors.centerIn: parent; control: accounts.control || unavailable; theme: accounts.theme }
    QtObject { id: unavailable; property bool busy: false; property string error: ""; function setSecureInput(active) {} }
    FaceEnrollmentDialog {
        id: faceEnrollment
        control: accounts.control
        theme: accounts.theme
        onCompleted: accounts.result = "Face recognition was added. Account access still requires the PIN."
    }
    Dialog {
        id: deletion; objectName: "deleteAccountDialog"
        property string accountId: ""
        property string accountName: ""
        parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(400, parent ? parent.width - 32 : 400)
        title: "Delete account?"; modal: true
        standardButtons: Dialog.Cancel
        closePolicy: Popup.CloseOnEscape
        onOpened: { accounts.result = ""; accounts.control.setSecureInput(true); pin.forceActiveFocus(); }
        onClosed: { pin.clear(); accountId = ""; accountName = ""; if (accounts.control) accounts.control.setSecureInput(false); }
        function remove() {
            if (!accounts.control.busy && pin.text.length > 0) {
                accounts.control.deleteAccount(accountId, pin.text); pin.clear()
            }
        }
        contentItem: ColumnLayout {
            Label { text: "Delete “" + deletion.accountName + "” from this device?"; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
            Label { text: "This removes the saved name, profile photo and PIN. Existing chat messages are not deleted."; wrapMode: Text.Wrap; Layout.fillWidth: true }
            TextField {
                id: pin; objectName: "deleteAccountPin"; placeholderText: "Account PIN or password"
                echoMode: TextInput.Password; maximumLength: 128; Layout.fillWidth: true
                inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                onAccepted: deletion.remove()
            }
            Button { objectName: "confirmDeleteAccount"; text: "Delete account"; enabled: accounts.control && !accounts.control.busy && pin.text.length > 0; onClicked: deletion.remove() }
            Label { text: accounts.control ? accounts.control.error : ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
        }
        Connections { target: deletion.contentItem.Window.window; function onActiveChanged() { if (!deletion.contentItem.Window.window.active) pin.clear(); } }
    }
    Connections {
        target: accounts.control; ignoreUnknownSignals: true
        function onAccountDeleted(id) { deletion.close(); accounts.result = "Account deleted."; }
        function onPrivacyLost() { deletion.close(); faceEnrollment.close() }
        function onUnlocked() { accounts.refresh() }
    }
}
