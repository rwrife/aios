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
    property string subscriptionModel: ""
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
        contentItem: Text { text: choice.displayText; textFormat: Text.PlainText; color: theme.ink; leftPadding: 10; verticalAlignment: Text.AlignVCenter; font.pixelSize: 13 }
        background: Rectangle { color: theme.input; radius: 5; border.color: theme.line; implicitHeight: 38 }
        indicator: Text { text: "⌄"; color: theme.muted; x: choice.width - 24; y: 6; font.pixelSize: 17 }
        delegate: ItemDelegate {
            id: choiceDelegate
            required property string modelData
            width: choice.width; text: modelData
            contentItem: Text { text: choiceDelegate.text; textFormat: Text.PlainText; color: theme.ink; padding: 8 }
            background: Rectangle { color: choiceDelegate.highlighted || choiceDelegate.hovered ? theme.horizon : theme.input }
        }
        popup.background: Rectangle { color: theme.input; border.color: theme.line }
    }
    function reload() {
            mode.currentIndex = backend.config.mode === "chatgpt" ? 2 : (backend.config.mode === "remote" ? 1 : 0); endpoint.text = backend.config.url || ""; modelId.text = backend.config.model || "local"; modelPath.text = backend.config.model_path || ""; apiKey.text = ""
            agentMode.currentIndex = backend.config.agent_mode === "chatgpt" ? 1 : (backend.config.agent_mode === "remote" ? 2 : 0); agentUrl.text = backend.config.agent_url || ""; agentModel.text = backend.config.agent_model || ""; agentKey.text = ""
            subscriptionModel = backend.config.subscription_model || ""
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
                        Choice { id: mode; objectName: "modelProvider"; model: ["On this computer", "Remote service", "ChatGPT subscription"]; Layout.fillWidth: true }
                        Field { id: modelPath; visible: mode.currentIndex === 0; placeholderText: "GGUF model path"; Layout.fillWidth: true }
                        QuietButton { visible: mode.currentIndex === 0; text: backend.busy ? "Cancel" : "Use starter model"; onClicked: backend.busy ? backend.stop() : backend.setupLocal() }
                        Text { visible: mode.currentIndex === 0; text: "SmolLM2 135M · Apache-2.0\nA small model for trying chat. Browser actions need a tool-capable model."; color: theme.muted; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Field { id: endpoint; visible: mode.currentIndex === 1; placeholderText: "Service URL · https://…/v1"; Layout.fillWidth: true }
                        Field { id: modelId; visible: mode.currentIndex === 1; placeholderText: "Model ID"; Layout.fillWidth: true }
                        Field { id: apiKey; visible: mode.currentIndex === 1; placeholderText: "API key · blank keeps saved key"; echoMode: TextInput.Password; Layout.fillWidth: true }
                        Text { text: "Agent tasks"; textFormat: Text.PlainText; color: theme.ink; font.pixelSize: 13; font.bold: true; Layout.topMargin: 6 }
                        Text { text: "Only skills marked for stronger reasoning use this provider. Ordinary chat stays on the primary model."; textFormat: Text.PlainText; color: theme.muted; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
                        Choice { id: agentMode; objectName: "agentProvider"; model: ["Current chat model", "ChatGPT subscription", "Remote service"]; Layout.fillWidth: true }
                        Field { id: agentUrl; objectName: 'agentUrl'; visible: agentMode.currentIndex === 2; placeholderText: "Agent endpoint · https://…/v1"; Layout.fillWidth: true }
                        Field { id: agentModel; objectName: 'agentModel'; visible: agentMode.currentIndex === 2; placeholderText: "Agent model ID"; Layout.fillWidth: true }
                        Field { id: agentKey; objectName: 'agentKey'; visible: agentMode.currentIndex === 2; placeholderText: "Agent API key · blank keeps saved key"; echoMode: TextInput.Password; Layout.fillWidth: true }
                        ColumnLayout {
                            id: subscriptionSettings; objectName: 'subscriptionSettings'
                            visible: mode.currentIndex === 2 || agentMode.currentIndex === 1; Layout.fillWidth: true; spacing: 8
                            Text { text: "Use your ChatGPT subscription. Available models and usage limits depend on your plan. Voice uses its own settings."; textFormat: Text.PlainText; color: theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap; Layout.fillWidth: true }
                            Text { text: backend.subscription.signed_in ? (backend.subscription.email + " · " + backend.subscription.plan) : "Check your account or sign in to connect."; textFormat: Text.PlainText; color: theme.ink; font.pixelSize: 12; wrapMode: Text.Wrap; Layout.fillWidth: true }
                            Flow {
                                Layout.fillWidth: true; spacing: 8
                                QuietButton { text: "Sign in"; enabled: !backend.busy; onClicked: backend.subscriptionAction("login", false) }
                                QuietButton { objectName: "deviceLogin"; text: "Use a code"; tip: "Sign in from another browser or device"; enabled: !backend.busy; onClicked: backend.subscriptionAction("login", true) }
                                QuietButton { text: "Refresh"; enabled: !backend.busy; onClicked: backend.subscriptionAction("status") }
                                QuietButton { text: "Sign out"; enabled: !backend.busy; onClicked: backend.subscriptionAction("logout") }
                            }
                            ColumnLayout {
                                visible: backend.loginUrl.length > 0; Layout.fillWidth: true
                                Text { text: backend.loginCode; textFormat: Text.PlainText; visible: text.length > 0; color: theme.ink; font.pixelSize: 20; Layout.fillWidth: true }
                                Text { text: backend.loginUrl; textFormat: Text.PlainText; color: theme.muted; font.pixelSize: 11; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                                Flow {
                                    Layout.fillWidth: true
                                    QuietButton { text: "Open browser"; onClicked: backend.openSubscriptionLogin() }
                                    QuietButton { text: "Copy address"; onClicked: backend.copy(backend.loginUrl) }
                                    QuietButton { text: "Copy code"; visible: backend.loginCode.length > 0; onClicked: backend.copy(backend.loginCode) }
                                    QuietButton { objectName: "cancelLogin"; text: "Cancel"; onClicked: backend.stop() }
                                }
                            }
                            Choice {
                                id: subscriptionChoice; objectName: "subscriptionModel"; Layout.fillWidth: true
                                property var entries: backend.subscription.models || []
                                model: ["Automatic"].concat(entries.map(function(m) { return m.name }))
                                currentIndex: { var i = entries.findIndex(function(m) { return m.id === settings.subscriptionModel }); return i < 0 ? 0 : i + 1 }
                                onActivated: settings.subscriptionModel = currentIndex === 0 ? "" : entries[currentIndex - 1].id
                            }
                            Text { visible: settings.subscriptionModel.length > 0; text: "Selected: " + settings.subscriptionModel; textFormat: Text.PlainText; color: theme.muted; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
                            Text {
                                Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: theme.muted; font.pixelSize: 11
                                text: {
                                    var limits = backend.subscription.limits
                                    if (!limits) return "Refresh to check usage."
                                    var lines = []
                                    for (var key of ["primary", "secondary"]) {
                                        var window = limits[key]
                                        if (window && typeof window.usedPercent === "number") lines.push(Math.max(0, 100 - window.usedPercent) + "% remaining" + (window.resetsAt ? " · resets " + new Date(window.resetsAt * 1000).toLocaleString() : ""))
                                    }
                                    return lines.length ? lines.join("\n") : "Usage information unavailable."
                                }
                            }
                        }
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
            Text { text: backend.status; textFormat: Text.PlainText; visible: text.length > 0; color: theme.muted; font.pixelSize: 11; wrapMode: Text.Wrap; Layout.fillWidth: true }
            RowLayout {
                Item { Layout.fillWidth: true }
                QuietButton { objectName: "saveModel"; text: "Save"; enabled: !backend.busy; onClicked: {
                    var config
                    if (settingsTab.currentIndex === 0) {
                        config = {
                            mode: ["local", "remote", "chatgpt"][mode.currentIndex],
                            url: endpoint.text || "http://127.0.0.1:8080/v1",
                            model: modelId.text || "local",
                            model_path: modelPath.text,
                            subscription_model: settings.subscriptionModel,
                            agent_mode: ["current", "chatgpt", "remote"][agentMode.currentIndex],
                            agent_url: agentUrl.text,
                            agent_model: agentModel.text
                        }
                        if (apiKey.text) config.api_key = apiKey.text
                        if (agentKey.text) config.agent_api_key = agentKey.text
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
