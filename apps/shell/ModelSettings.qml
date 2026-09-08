import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: settings
    required property var backend
    required property var theme
    signal closeRequested()
    property bool showClose: true
    property alias currentTab: settingsTab.currentIndex
    Component.onCompleted: reload()
    component QuietButton: Button {
        id: button
        property string tip: text
        Accessible.name: tip
        implicitWidth: Math.max(36, implicitContentWidth + 16); implicitHeight: 36
        contentItem: Text { text: button.text; color: button.enabled ? theme.ink : theme.muted; opacity: button.enabled ? 0.8 : 0.4; font.pixelSize: 17; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { color: button.hovered || button.down ? theme.input : "transparent"; radius: 7; border.width: button.activeFocus ? 1 : 0; border.color: theme.accent }
        ToolTip.visible: hovered || activeFocus; ToolTip.text: tip; ToolTip.delay: 600
    }
    component Field: TextField {
        color: theme.ink; placeholderTextColor: theme.muted; selectByMouse: true
        font.pixelSize: 13; padding: 10
        background: Rectangle { color: theme.input; radius: 5; border.color: parent.activeFocus ? theme.accent : theme.line }
    }
    component Choice: ComboBox {
        id: choice
        contentItem: Text { text: choice.displayText; color: theme.ink; leftPadding: 10; verticalAlignment: Text.AlignVCenter; font.pixelSize: 13 }
        background: Rectangle { color: theme.input; radius: 5; border.color: theme.line; implicitHeight: 38 }
        indicator: Text { text: "⌄"; color: theme.muted; x: choice.width - 24; y: 6; font.pixelSize: 17 }
        delegate: ItemDelegate {
            id: choiceDelegate
            required property string modelData
            width: choice.width; text: modelData
            contentItem: Text { text: choiceDelegate.text; color: theme.ink; padding: 8 }
            background: Rectangle { color: choiceDelegate.highlighted || choiceDelegate.hovered ? theme.horizon : theme.input }
        }
        popup.background: Rectangle { color: theme.input; border.color: theme.line }
    }
    function reload() {
            mode.currentIndex = backend.config.mode === "remote" ? 1 : 0; endpoint.text = backend.config.url || ""; modelId.text = backend.config.model || "local"; modelPath.text = backend.config.model_path || ""; apiKey.text = ""
            voiceMode.currentIndex = backend.config.voice_mode === "local" ? 1 : 0; voiceUrl.text = backend.config.voice_url || ""; voiceKey.text = ""; sttModel.text = backend.config.stt_model || "whisper-1"; ttsModel.text = backend.config.tts_model || "tts-1"; voiceName.text = backend.config.voice_name || "alloy"; speechPath.text = backend.config.speech_model_path || ""
        }
        ColumnLayout {
            anchors.fill: parent
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                Choice { id: settingsTab; model: ["Model", "Voice"]; Layout.fillWidth: true }
                QuietButton { text: "×"; tip: "Close settings"; visible: settings.showClose; onClicked: settings.closeRequested() }
            }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true; contentWidth: availableWidth
                ColumnLayout {
                    width: parent.width; spacing: 10
                    ColumnLayout {
                        visible: settingsTab.currentIndex === 0; Layout.fillWidth: true; spacing: 10
                        Choice { id: mode; model: ["On this computer", "Remote service"]; Layout.fillWidth: true }
                        Field { id: modelPath; visible: mode.currentIndex === 0; placeholderText: "GGUF model path"; Layout.fillWidth: true }
                        QuietButton { visible: mode.currentIndex === 0; text: backend.busy ? "Cancel download" : "Download starter model · 101 MiB"; onClicked: backend.busy ? backend.stop() : backend.setupLocal() }
                        Text { visible: mode.currentIndex === 0; text: "SmolLM2 135M · Apache-2.0\nA small model for trying chat, with limited reasoning ability."; color: theme.muted; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Field { id: endpoint; visible: mode.currentIndex === 1; placeholderText: "Service URL · https://…/v1"; Layout.fillWidth: true }
                        Field { id: modelId; visible: mode.currentIndex === 1; placeholderText: "Model ID"; Layout.fillWidth: true }
                        Field { id: apiKey; visible: mode.currentIndex === 1; placeholderText: "API key · blank keeps saved key"; echoMode: TextInput.Password; Layout.fillWidth: true }
                    }
                    ColumnLayout {
                        visible: settingsTab.currentIndex === 1; Layout.fillWidth: true; spacing: 10
                        Choice { id: voiceMode; model: ["Remote voice service", "On-device voice"]; Layout.fillWidth: true }
                        Text { text: voiceMode.currentIndex === 0 ? "Recordings go to this voice service when you finish recording. Transcripts stay in the composer until you send them." : "Speech stays on this computer. The small speech model recognizes English; replies use a synthesized local voice."; color: theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Field { id: voiceUrl; visible: voiceMode.currentIndex === 0; placeholderText: "Voice URL · https://…/v1"; Layout.fillWidth: true }
                        Field { id: voiceKey; visible: voiceMode.currentIndex === 0; placeholderText: "Voice API key · blank keeps saved key"; echoMode: TextInput.Password; Layout.fillWidth: true }
                        Field { id: sttModel; visible: voiceMode.currentIndex === 0; placeholderText: "Transcription model"; Layout.fillWidth: true }
                        Field { id: ttsModel; visible: voiceMode.currentIndex === 0; placeholderText: "Speech model"; Layout.fillWidth: true }
                        Field { id: voiceName; visible: voiceMode.currentIndex === 0; placeholderText: "Voice name"; Layout.fillWidth: true }
                        Field { id: speechPath; visible: voiceMode.currentIndex === 1; placeholderText: "Whisper speech model path"; Layout.fillWidth: true }
                        QuietButton { visible: voiceMode.currentIndex === 1; text: backend.busy ? "Cancel download" : "Download English speech model · 75 MiB"; onClicked: backend.busy ? backend.stop() : backend.setupVoice() }
                        Text { visible: voiceMode.currentIndex === 1; text: "Whisper tiny.en · MIT · Checksum verified"; color: theme.muted; font.pixelSize: 11 }
                    }
                }
            }
            Text { text: backend.status; visible: text.length > 0; color: theme.muted; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
            RowLayout {
                Item { Layout.fillWidth: true }
                QuietButton { text: "Save"; enabled: !backend.busy; onClicked: {
                    var config
                    if (settingsTab.currentIndex === 0) {
                        config = {mode: mode.currentIndex === 0 ? "local" : "remote", url: endpoint.text || "http://127.0.0.1:8080/v1", model: modelId.text || "local", model_path: modelPath.text}
                        if (apiKey.text) config.api_key = apiKey.text
                    } else {
                        config = {voice_mode: voiceMode.currentIndex === 0 ? "remote" : "local", voice_url: voiceUrl.text, stt_model: sttModel.text, tts_model: ttsModel.text, voice_name: voiceName.text, speech_model_path: speechPath.text}
                        if (voiceKey.text) config.voice_key = voiceKey.text
                    }
                    backend.configure(config)
                } }
            }
        }
        Connections { target: backend; function onConfigured() { settings.reload() } }
}
