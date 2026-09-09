import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Dialog {
    id: dialog
    required property var control
    property bool creating: false
    property bool recovering: false
    property string selectedProfile: ""
    property string photoRgb: ""
    property string photoPreview: ""
    property string validationError: ""
    readonly property bool greetingOnly: control && control.greetingOnly === true
    title: greetingOnly ? "Your name and PIN" : creating ? "Create a private profile" : recovering ? "Reset your PIN" : "Unlock your profile"
    modal: true; width: 440
    standardButtons: control.busy ? Dialog.NoButton : Dialog.Close
    closePolicy: control.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    onOpened: {
        validationError = ""; control.setSecureInput(true)
        if (selectedProfile) { profileName.text = selectedProfile; pin.forceActiveFocus(); }
        else profileName.forceActiveFocus()
    }
    onClosed: { profileName.clear(); selectedProfile = ""; photoRgb = ""; photoPreview = ""; usePhoto.checked = false; validationError = ""; pin.clear(); recoveryInput.clear(); recovering = false; consent.checked = false; recovery.clear(); control.setSecureInput(false); }
    function submit() {
        if (control.busy) return
        validationError = ""
        if (!profileName.text.trim()) { validationError = "Enter your name."; profileName.forceActiveFocus(); return; }
        if (dialog.creating || dialog.recovering) {
            var numeric = /^[0-9]+$/.test(pin.text)
            if ((numeric && pin.text.length < 4) || (!numeric && pin.text.length < 10) || /[\x00-\x1f]/.test(pin.text)) {
                validationError = "Use at least 4 digits for a PIN, or 10 characters for a passphrase."; pin.forceActiveFocus(); return;
            }
        } else if (!pin.text.length) { validationError = "Enter your PIN or password."; pin.forceActiveFocus(); return; }
        if (dialog.creating && !dialog.greetingOnly && !consent.checked) { validationError = "Confirm that you want to create a local encrypted workspace."; consent.forceActiveFocus(); return; }
        if (dialog.recovering && !recoveryInput.text.length) { validationError = "Enter your recovery code."; recoveryInput.forceActiveFocus(); return; }
        if (dialog.creating && dialog.photoRgb) control.enrollProfile(profileName.text, pin.text, dialog.greetingOnly || consent.checked, dialog.photoRgb)
        else if (dialog.creating) control.enroll(profileName.text, pin.text, dialog.greetingOnly || consent.checked)
        else if (dialog.recovering) control.recover(profileName.text, recoveryInput.text, pin.text)
        else control.unlock(profileName.text, pin.text)
        pin.clear(); recoveryInput.clear()
    }
    Connections {
        target: dialog.contentItem.Window.window
        function onActiveChanged() { if (!dialog.contentItem.Window.window.active) { pin.clear(); recoveryInput.clear(); } }
    }
    Shortcut { sequence: "Alt+U"; enabled: dialog.opened && savedAccounts.visible; onActivated: savedAccounts.forceActiveFocus() }
    Connections {
        target: control
        ignoreUnknownSignals: true
        function onPrivacyLost() { pin.clear(); recovery.clear(); dialog.close(); }
        function onEnrollmentCompleted(secret) { pin.clear(); recoveryInput.clear(); recovery.text = secret; }
        function onUnlocked() { dialog.close(); }
        function onPhotoCaptured(preview, rgb) { if (dialog.opened && dialog.creating) { dialog.photoPreview = preview; dialog.photoRgb = rgb; } }
    }
    contentItem: ColumnLayout {
        spacing: 10
        Label {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: dialog.greetingOnly ? "Create a profile for your greeting, or choose a saved name to sign in." : "PIN access works without a camera and locks after two minutes. Biometric enrollment is separate."
        }
        ComboBox {
            id: savedAccounts; objectName: "savedAccounts"; Accessible.name: "Saved accounts"
            visible: dialog.greetingOnly && control.profiles && control.profiles.length > 0
            Layout.fillWidth: true; textRole: "name"; model: control.profiles || []
            currentIndex: -1; displayText: currentIndex < 0 ? "Choose a saved profile…" : currentText
            onActivated: { profileName.text = currentText; dialog.creating = false; validationError = ""; pin.clear(); pin.forceActiveFocus(); }
        }
        TextField {
            id: profileName; objectName: "profileName"; placeholderText: "Your name"; Accessible.name: "Your name"
            maximumLength: 80; Layout.fillWidth: true; KeyNavigation.tab: pin
            onAccepted: pin.forceActiveFocus()
        }
        Button { visible: dialog.greetingOnly && !dialog.creating; text: "Create a different profile"; onClicked: { dialog.creating = true; profileName.clear(); pin.clear(); } }
        CheckBox {
            objectName: "recoverProfile"; visible: !dialog.creating && !dialog.greetingOnly
            text: "I have a recovery code"; checked: dialog.recovering
            onToggled: { dialog.recovering = checked; pin.clear(); recoveryInput.clear(); recovery.clear(); }
        }
        TextField {
            id: recoveryInput; objectName: "recoveryInput"; visible: dialog.recovering
            placeholderText: "Recovery code"; maximumLength: 128; echoMode: TextInput.Password
            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
            Layout.fillWidth: true
        }
        TextField {
            id: pin; objectName: "enrollmentPin"; placeholderText: dialog.recovering ? "New PIN or passphrase" : "PIN or passphrase"
            Accessible.name: "PIN or passphrase"; KeyNavigation.tab: submitButton; KeyNavigation.backtab: profileName
            onAccepted: dialog.submit()
            echoMode: TextInput.Password; maximumLength: 128
            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
            Layout.fillWidth: true
        }
        Label { text: dialog.creating ? "PIN: 4 or more digits. Passphrase: 10 or more characters. No camera or photo needed." : "Enter your account PIN or password. No camera needed."; wrapMode: Text.Wrap; Layout.fillWidth: true }
        CheckBox {
            id: consent; objectName: "profileConsent"; visible: dialog.creating && !dialog.greetingOnly
            text: dialog.greetingOnly ? "Save my greeting profile on this device" : "Create a local encrypted workspace"
        }
        Button {
            id: submitButton; objectName: "profileSubmit"
            text: dialog.creating ? "Create profile" : dialog.recovering ? "Reset PIN" : "Unlock"
            enabled: !control.busy
            onClicked: dialog.submit()
        }
        Label { objectName: "profileValidation"; visible: text.length > 0; text: dialog.validationError; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
        CheckBox { id: usePhoto; objectName: "useProfilePhoto"; visible: dialog.creating; text: "Add an optional photo"; onToggled: if (!checked) { dialog.photoRgb = ""; dialog.photoPreview = ""; } }
        RowLayout {
            visible: dialog.creating && usePhoto.checked
            Image { source: dialog.photoPreview; cache: false; visible: source.toString().length > 0; Layout.preferredWidth: 64; Layout.preferredHeight: 64 }
            Button { text: dialog.photoRgb ? "Retake photo" : "Take profile photo"; onClicked: control.takeProfilePhoto() }
            Button { text: "Remove"; visible: dialog.photoRgb.length > 0; onClicked: { dialog.photoRgb = ""; dialog.photoPreview = ""; } }
        }
        Label { visible: dialog.greetingOnly && savedAccounts.visible; text: "Alt+U: saved accounts · Tab: next field · Enter: submit"; wrapMode: Text.Wrap; Layout.fillWidth: true }
        Label { visible: control.busy; text: dialog.greetingOnly ? "Opening your profile…" : "Creating your encrypted workspace…"; Layout.fillWidth: true }
        Label { visible: dialog.greetingOnly; text: "This personalizes your chat. Protected workspaces use separate sign-in."; Layout.fillWidth: true; wrapMode: Text.Wrap }
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
