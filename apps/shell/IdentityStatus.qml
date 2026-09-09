import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: panel
    required property var control
    width: 340; height: column.implicitHeight + 32
    radius: 12; color: "#172633"; border.color: "#52616c"
    EnrollmentFlow { id: enrollment; control: panel.control }
    Connections {
        target: control
        function onPrivacyLost() {
            title.clear(); note.clear(); documentText.clear(); documentName.clear();
            documentStatus.text = ""; documentDialog.close();
        }
        function onDocumentLoaded(content) { documentText.text = content; documentStatus.text = "Opened"; }
        function onDocumentSaved() { documentStatus.text = "Saved to your private workspace"; }
    }
    Dialog {
        id: documentDialog; title: "Session document"; modal: true
        width: 600; height: 440
        standardButtons: Dialog.Close
        ColumnLayout {
            anchors.fill: parent
            TextField { id: documentName; placeholderText: "Resume.txt"; Layout.fillWidth: true }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea { id: documentText; objectName: "sessionDocument"; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap }
            }
            RowLayout {
                Button { text: "Open"; onClicked: control.readDocument(documentName.text) }
                Button { text: "Save"; onClicked: { documentStatus.text = "Saving…"; control.saveDocument(documentName.text, documentText.text); } }
                Label { id: documentStatus; textFormat: Text.PlainText }
            }
            Label { text: control.error; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
        }
    }
    ColumnLayout {
        id: column; anchors.fill: parent; anchors.margins: 16; spacing: 8
        enabled: !control.busy
        UserBubble { control: panel.control; Layout.alignment: Qt.AlignHCenter }
        Label { text: control.simulator ? "Identity simulator · no apps execute" : "Experimental session broker"; color: "#bde4e6" }
        Label { text: control.authority; color: "white" }
        RowLayout {
            Button { text: "Create profile"; enabled: control.personalAvailable; onClicked: { enrollment.creating = true; enrollment.open(); } }
            Button { text: "Unlock with PIN"; enabled: control.personalAvailable; onClicked: { enrollment.creating = false; enrollment.open(); } }
        }
        Label { visible: !control.personalAvailable; text: "Private profiles need a protected display."; color: "#bde4e6"; wrapMode: Text.Wrap; Layout.fillWidth: true }
        ComboBox {
            visible: control.simulator; Layout.fillWidth: true
            model: ["unknown", "user-a", "user-b", "absent", "conflict"]
            onActivated: control.simulate(currentText)
        }
        TextField { id: title; objectName: "sessionTitle"; placeholderText: "Work session title"; Layout.fillWidth: true }
        RowLayout {
            Button { text: "Start"; onClicked: control.activate(title.text) }
            Button { text: "Suspend"; onClicked: control.suspend() }
            Button { text: "Recent"; onClicked: control.search() }
        }
        ScrollView {
            Layout.fillWidth: true; Layout.preferredHeight: 160
            visible: control.messages !== undefined && control.messages.length > 0
            Column {
                width: parent.width; spacing: 6
                Repeater {
                    model: control.messages
                    Label {
                        required property var modelData
                        width: parent.width
                        text: modelData.role + ": " + modelData.content
                        textFormat: Text.PlainText; wrapMode: Text.Wrap; color: "white"
                    }
                }
            }
        }
        Button { text: "Older messages"; visible: control.olderMessages === true; onClicked: control.history(true) }
        TextField {
            id: note; objectName: "sessionNote"; placeholderText: "Save a note to this session"
            Layout.fillWidth: true; maximumLength: 4000
            onAccepted: { control.note(text); clear(); }
        }
        Repeater {
            model: control.sessions
            Button { required property var modelData; text: modelData.title; onClicked: control.resume(modelData.id); Layout.fillWidth: true }
        }
        RowLayout {
            Button { text: "Calculator"; onClicked: control.launch("calculator") }
            Button { text: "Editor"; onClicked: control.launch("editor") }
            Button { text: "Document"; onClicked: { documentName.text = "Resume.txt"; documentDialog.open(); } }
        }
        Button { text: "Protected account"; onClicked: control.protectedResource() }
        Label { text: control.error; color: "#e8bdbd"; wrapMode: Text.Wrap; Layout.fillWidth: true }
    }
}
