import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: picker
    required property var backend
    required property var theme
    signal installStarted()
    property string buttonName: "installLocalModel"
    readonly property var inventory: backend.localModels || ({})
    readonly property var entries: inventory.models || []
    readonly property var selected: entries[choice.currentIndex] || ({})
    spacing: 8
    function gib(bytes) { return (bytes / 1073741824).toFixed(1) + " GiB" }
    onVisibleChanged: if (visible && backend.refreshLocalModels) backend.refreshLocalModels()
    Component.onCompleted: if (backend.refreshLocalModels) backend.refreshLocalModels()
    ComboBox {
        id: choice; objectName: "localModelChoice"
        Layout.fillWidth: true; enabled: !backend.busy && !backend.configuring
        model: picker.entries.map(function(m) { return m.name + (m.installed ? " · Downloaded" : "") })
        Accessible.name: "Local chat model"
    }
    Text {
        Layout.fillWidth: true; wrapMode: Text.Wrap; color: theme.muted; font.pixelSize: 12
        text: picker.entries.length ? (picker.selected.tool_use ? "Supports tool use" : "Basic chat · limited tool ability")
              + " · " + picker.selected.license + "\n"
              + picker.gib(picker.selected.bytes) + " download · about " + picker.selected.ram_gib + " GiB total RAM\n"
              + picker.selected.note : "Loading local models…"
    }
    Text {
        Layout.fillWidth: true; wrapMode: Text.Wrap; color: theme.muted; font.pixelSize: 12
        text: picker.entries.length ? (picker.inventory.ram_bytes ? picker.gib(picker.inventory.ram_bytes) + " RAM detected" : "RAM could not be detected; check the guidance above")
              + " · " + picker.gib(picker.inventory.free_disk_bytes) + " disk free\n"
              + "RAM guidance includes room for the desktop and an 8K context. Close other apps if memory is tight. Downloads leave 0.5 GiB disk space free." : ""
    }
    Text {
        objectName: "localModelReason"
        Layout.fillWidth: true; wrapMode: Text.Wrap; color: theme.muted; font.pixelSize: 12
        visible: text.length > 0; text: picker.selected.reason || ""
    }
    Flow {
        Layout.fillWidth: true; spacing: 8
        Button {
            objectName: picker.buttonName
            text: picker.selected.installed ? "Use selected model" : "Download and use model"
            enabled: !!picker.selected.available && !backend.busy && !backend.configuring
            onClicked: { backend.setupLocal(picker.selected.id); picker.installStarted() }
        }
        Button {
            text: "Refresh"; enabled: !backend.busy && !backend.configuring
            onClicked: backend.refreshLocalModels()
        }
    }
}
