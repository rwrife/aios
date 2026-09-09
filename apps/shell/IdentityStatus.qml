import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    required property var control
    width: 340; height: column.implicitHeight + 32
    radius: 12; color: "#172633"; border.color: "#52616c"
    ColumnLayout {
        id: column; anchors.fill: parent; anchors.margins: 16; spacing: 8
        Label { text: control.simulator ? "Identity simulator · no apps execute" : "Experimental session broker"; color: "#bde4e6" }
        Label { text: control.authority; color: "white" }
        ComboBox {
            visible: control.simulator; Layout.fillWidth: true
            model: ["unknown", "user-a", "user-b", "absent", "conflict"]
            onActivated: control.simulate(currentText)
        }
        TextField { id: title; placeholderText: "Work session title"; text: "Build my résumé"; Layout.fillWidth: true }
        RowLayout {
            Button { text: "Start"; onClicked: control.activate(title.text) }
            Button { text: "Suspend"; onClicked: control.suspend() }
            Button { text: "Recent"; onClicked: control.search() }
        }
        Repeater {
            model: control.sessions
            Button { required property var modelData; text: modelData.title; onClicked: control.resume(modelData.id); Layout.fillWidth: true }
        }
        RowLayout {
            Button { text: "Calculator"; onClicked: control.launch("calculator") }
            Button { text: "Editor"; onClicked: control.launch("editor") }
        }
        Button { text: "Protected account"; onClicked: control.protectedResource() }
        Label { text: control.error; color: "#e8bdbd"; wrapMode: Text.Wrap; Layout.fillWidth: true }
    }
}
