import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Window {
    id: desktop
    visible: true
    title: "AIOS Desktop"
    width: Screen.width; height: Screen.height
    flags: Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint
    color: theme.night
    QtObject {
        id: theme
        readonly property color night: "#101b27"
        readonly property color horizon: "#354e60"
        readonly property color panel: "#172633"
        readonly property color input: "#203340"
        readonly property color ink: "#f1f5f6"
        readonly property color muted: "#b2c3cd"
        readonly property color accent: "#bde4e6"
        readonly property color line: "#4c6574"
    }
    property bool reducedMotion: backend.config.reduced_motion === true
    function openChat() { chatWindow.show(); chatWindow.raise(); chatWindow.requestActivate(); composer.forceActiveFocus() }
    property real phase: 0
    NumberAnimation on phase { from: 0; to: Math.PI * 2; duration: 26000; loops: Animation.Infinite; running: !desktop.reducedMotion && !chatWindow.visible }
    onPhaseChanged: waves.requestPaint()
    Rectangle { anchors.fill: parent; gradient: Gradient {
        GradientStop { position: 0; color: theme.night }
        GradientStop { position: 0.68; color: theme.horizon }
        GradientStop { position: 1; color: theme.night }
    } }
    Canvas {
        id: waves; anchors.fill: parent
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            var ctx = getContext("2d"); ctx.reset();
            for (var ribbon = 0; ribbon < 10; ribbon++) {
                ctx.beginPath();
                for (var x = 0; x <= width; x += 8) {
                    var y = height * 0.57 + Math.sin(x / width * 5.2 + desktop.phase + ribbon * 0.07) * height * 0.08
                        + Math.cos(x / width * 2.8 - desktop.phase) * height * 0.045 + ribbon * 4;
                    if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                }
                ctx.strokeStyle = theme.accent; ctx.globalAlpha = 0.045 + (9-ribbon)*0.007;
                ctx.lineWidth = ribbon === 0 ? 2 : 1; ctx.stroke();
            }
        }
    }
    Text { x: 48; y: 36; text: "aios"; color: theme.ink; opacity: 0.65; font.pixelSize: 22; font.letterSpacing: 4 }
    Column {
        anchors.centerIn: parent; spacing: 20
        Button {
            id: launcher; width: 96; height: 96; anchors.horizontalCenter: parent.horizontalCenter
            Accessible.name: "Open chat"
            background: Rectangle {
                color: launcher.hovered ? theme.input : "transparent"; radius: 48
                border.width: launcher.activeFocus ? 2 : 0; border.color: theme.accent
                Behavior on color { ColorAnimation { duration: 150 } }
            }
            contentItem: Item {
                Rectangle { width: 44; height: 34; radius: 10; anchors.centerIn: parent; color: "transparent"; border.color: theme.ink; border.width: 2
                    Rectangle { x: 9; y: 29; width: 11; height: 2; rotation: -40; color: theme.ink }
                    Row { anchors.centerIn: parent; spacing: 5; Repeater { model: 3; Rectangle { width: 3; height: 3; radius: 2; color: theme.ink } } }
                }
            }
            onClicked: desktop.openChat()
        }
        Text { text: "Chat"; color: theme.ink; font.pixelSize: 18; anchors.horizontalCenter: parent.horizontalCenter; font.letterSpacing: 1 }
    }
    Row {
        anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 32; spacing: 12
        QuietButton { text: ">_"; tip: "Terminal"; onClicked: backend.terminal() }
        QuietButton { tip: "Power"; implicitWidth: 44; onClicked: powerDialog.open()
            contentItem: Canvas { implicitWidth: 20; implicitHeight: 20; onPaint: {
                var c = getContext("2d"); c.reset(); c.strokeStyle = theme.ink; c.lineWidth = 1.5;
                c.beginPath(); c.arc(width/2, height/2, 7, -Math.PI/3, Math.PI*4/3); c.stroke();
                c.beginPath(); c.moveTo(width/2, height/2-10); c.lineTo(width/2, height/2-2); c.stroke();
            } }
        }
    }
    component QuietButton: Button {
        id: control
        property string tip: text
        Accessible.name: tip
        implicitWidth: Math.max(44, implicitContentWidth + 24); implicitHeight: 44
        contentItem: Text { text: control.text; color: control.enabled ? theme.ink : theme.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 16 }
        background: Rectangle { radius: 8; color: control.down || control.hovered ? theme.input : "transparent"; border.width: control.activeFocus ? 2 : 0; border.color: theme.accent }
        ToolTip.visible: hovered || activeFocus; ToolTip.text: tip; ToolTip.delay: activeFocus ? 0 : 700
    }
    component Field: TextField {
        color: theme.ink; placeholderTextColor: theme.muted; selectByMouse: true
        font.pixelSize: 14; padding: 12
        background: Rectangle { color: theme.input; radius: 6; border.color: parent.activeFocus ? theme.accent : theme.line }
    }
    Window {
        id: chatWindow; title: "AIOS Chat"; visible: false
        flags: Qt.Window | Qt.FramelessWindowHint
        width: Math.min(780, Screen.width - 48); height: Math.min(680, Screen.height - 80)
        x: (Screen.width - width)/2; y: (Screen.height - height)/2
        color: theme.panel
        onClosing: function(close) { close.accepted = false; hide() }
        ColumnLayout {
            anchors.fill: parent; anchors.margins: 24; spacing: 12
            RowLayout {
                Layout.fillWidth: true
                ColumnLayout { spacing: 3
                    Text { text: "Chat"; color: theme.ink; font.pixelSize: 23 }
                    Text { text: backend.config.mode === "remote" ? backend.config.model : "On this computer"; color: theme.muted; font.pixelSize: 12 }
                }
                Item { Layout.fillWidth: true }
                QuietButton { text: "New"; enabled: !backend.busy; onClicked: backend.newChat() }
                QuietButton { text: "Model"; enabled: !backend.busy; onClicked: settings.open() }
                QuietButton { text: "×"; tip: "Back to desktop"; onClicked: chatWindow.hide() }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: theme.line; opacity: 0.5 }
            Item {
                Layout.fillWidth: true; Layout.fillHeight: true
                Column {
                    visible: backend.messages.length === 0; anchors.centerIn: parent; width: parent.width; spacing: 16
                    Text { text: "A little space to think."; color: theme.ink; font.pixelSize: 26; anchors.horizontalCenter: parent.horizontalCenter }
                    Text { text: "Choose a model, then make yourself at home."; color: theme.muted; font.pixelSize: 14; anchors.horizontalCenter: parent.horizontalCenter }
                    QuietButton { text: "Choose a model"; anchors.horizontalCenter: parent.horizontalCenter; onClicked: settings.open() }
                }
                ListView {
                    id: conversation; anchors.fill: parent; clip: true; spacing: 20
                    model: backend.messages
                    onCountChanged: Qt.callLater(positionViewAtEnd)
                    ScrollBar.vertical: ScrollBar {}
                    delegate: Column {
                        required property var modelData
                        width: conversation.width - 12; spacing: 7
                        Text { text: modelData.role === "user" ? "You" : "AI"; color: theme.muted; font.pixelSize: 12 }
                        TextEdit { width: parent.width; text: modelData.content || "…"; color: theme.ink; font.pixelSize: 16; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText }
                    }
                }
            }
            Text { Layout.fillWidth: true; text: backend.status; visible: text.length > 0; color: theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap }
            Rectangle {
                Layout.fillWidth: true; implicitHeight: Math.min(150, Math.max(80, composer.contentHeight + 28)); color: theme.input; radius: 12; border.color: composer.activeFocus ? theme.accent : theme.line
                ScrollView { anchors.fill: parent; anchors.margins: 12
                    TextArea { id: composer; placeholderText: "Ask anything…"; placeholderTextColor: theme.muted; color: theme.ink; font.pixelSize: 16; wrapMode: TextEdit.Wrap; selectByMouse: true
                        background: null
                        Keys.onReturnPressed: function(event) { if (!(event.modifiers & Qt.ShiftModifier)) { if (!backend.busy && text.trim()) { backend.send(text); text = ""; } event.accepted = true; } else event.accepted = false }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: backend.config.live ? "Live session · Changes are lost after reboot" : "Enter to send · Shift + Enter for a new line"; color: theme.muted; font.pixelSize: 11; Layout.fillWidth: true }
                QuietButton { text: backend.busy ? "Stop" : "Send"; enabled: backend.busy || composer.text.trim().length > 0; onClicked: { if (backend.busy) backend.stop(); else { backend.send(composer.text); composer.text = ""; } } }
            }
        }
        Popup {
            id: settings; parent: chatWindow.contentItem; anchors.centerIn: parent; width: Math.min(520, parent.width - 32); modal: true; padding: 24
            closePolicy: Popup.CloseOnEscape
            background: Rectangle { color: theme.panel; radius: 12; border.color: theme.line }
            onOpened: { mode.currentIndex = backend.config.mode === "remote" ? 1 : 0; endpoint.text = backend.config.url || ""; modelId.text = backend.config.model || ""; modelPath.text = backend.config.model_path || ""; apiKey.text = ""; motion.checked = desktop.reducedMotion }
            contentItem: ColumnLayout { spacing: 12
                Text { text: "Your model"; font.pixelSize: 24; color: theme.ink }
                ComboBox { id: mode; model: ["Local GGUF model", "Remote endpoint"]; Layout.fillWidth: true
                    contentItem: Text { text: mode.displayText; color: theme.ink; leftPadding: 12; verticalAlignment: Text.AlignVCenter; font.pixelSize: 14 }
                    background: Rectangle { color: theme.input; border.color: mode.activeFocus ? theme.accent : theme.line; radius: 6; implicitHeight: 42 }
                    indicator: Text { text: "⌄"; color: theme.ink; x: mode.width - 28; y: 8; font.pixelSize: 18 }
                    delegate: ItemDelegate { required property string modelData; width: mode.width; text: modelData
                        contentItem: Text { text: parent.text; color: theme.ink; padding: 8 }
                        background: Rectangle { color: parent.highlighted || parent.hovered ? theme.horizon : theme.input }
                    }
                    popup.background: Rectangle { color: theme.input; border.color: theme.line }
                }
                Text { text: mode.currentIndex === 0 ? "Import a GGUF file already on this computer.\nThe model runs locally; no API key is needed." : "Use an OpenAI-compatible HTTPS endpoint."; color: theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true }
                Field { id: modelPath; visible: mode.currentIndex === 0; placeholderText: "/home/aios/models/model.gguf"; Layout.fillWidth: true }
                QuietButton { visible: mode.currentIndex === 0; text: backend.busy ? "Cancel download" : "Download small starter model (101 MiB)"; onClicked: backend.busy ? backend.stop() : backend.setupLocal() }
                Text { visible: mode.currentIndex === 0; text: "SmolLM2 135M · Apache-2.0 · Limited reasoning ability.\nDownloaded from Hugging Face and checksum verified."; color: theme.muted; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
                Field { id: endpoint; visible: mode.currentIndex === 1; placeholderText: "https://your-provider.example/v1"; Layout.fillWidth: true }
                Field { id: modelId; visible: mode.currentIndex === 1; placeholderText: "Model ID"; Layout.fillWidth: true }
                Field { id: apiKey; visible: mode.currentIndex === 1; placeholderText: "API key (leave blank to keep saved key)"; echoMode: TextInput.Password; Layout.fillWidth: true }
                CheckBox { id: motion; text: "Reduce background motion"; palette.windowText: theme.ink
                    indicator: Rectangle { x: 0; y: (motion.height-height)/2; width: 22; height: 22; radius: 4; color: theme.input; border.color: motion.activeFocus ? theme.accent : theme.line
                        Text { anchors.centerIn: parent; text: motion.checked ? "✓" : ""; color: theme.accent }
                    }
                }
                Text { text: backend.status; color: theme.muted; wrapMode: Text.Wrap; Layout.fillWidth: true; font.pixelSize: 12 }
                RowLayout { Item { Layout.fillWidth: true }
                    QuietButton { text: "Cancel"; onClicked: settings.close() }
                    QuietButton { text: "Save"; onClicked: {
                        var config = {mode: mode.currentIndex === 0 ? "local" : "remote", url: endpoint.text || "http://127.0.0.1:8080/v1", model: modelId.text || "local", model_path: modelPath.text, reduced_motion: motion.checked};
                        if (apiKey.text) config.api_key = apiKey.text;
                        backend.configure(config)
                    } }
                }
            }
        }
    }
    Dialog {
        id: powerDialog; parent: desktop.contentItem; anchors.centerIn: parent; title: "AIOS Power"; modal: true; width: 360; popupType: Popup.Window
        background: Rectangle { color: theme.panel; border.color: theme.line; radius: 12 }
        contentItem: Row { spacing: 12
            QuietButton { text: "Cancel"; onClicked: powerDialog.close() }
            QuietButton { text: "Restart"; onClicked: { powerDialog.close(); backend.power("reboot") } }
            QuietButton { text: "Shut down"; onClicked: { powerDialog.close(); backend.power("poweroff") } }
        }
    }
    Connections { target: backend; function onConfigured() { settings.close() } }
}
