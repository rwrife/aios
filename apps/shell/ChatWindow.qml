import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import QtQuick.Dialogs
import "WindowSizing.js" as WindowSizing

Window {
    id: chat
    required property var backend
    required property var session
    required property var theme
    property var profileControl: null
    property bool ownsProfileControl: false
    title: "AIOS Chat"
    visible: true
    flags: Qt.application.arguments.indexOf("--chat") >= 0 ? Qt.Window : Qt.Window | Qt.FramelessWindowHint
    width: WindowSizing.extent(740, Screen.width, Screen.desktopAvailableWidth)
    height: WindowSizing.extent(650, Screen.height, Screen.desktopAvailableHeight)
    x: Screen.virtualX + (Screen.width - width)/2; y: Screen.virtualY + (Screen.height - height)/2
    color: "transparent"
    signal minimized()
    signal removed()
    onVisibilityChanged: function() {
        if (chat.visibility === Window.Minimized)
            minimized()
    }
    onClosing: {
        removed()
        session.closeSession()
        if (ownsProfileControl && profileControl) profileControl.dispose()
        Qt.callLater(chat.destroy)
    }
    Component.onCompleted: { conversation.syncMessages(); composer.forceActiveFocus() }
    function submit() {
        if (session.busy || session.recording) return;
        if (!composer.text.trim() && !session.attachments.length) return;
        if (backend.config.mode !== "remote" && !backend.config.model_path) { options.open(); return }
        conversation.cancelFlick(); conversation.followLatest = true
        session.send(composer.text); composer.clear(); conversation.scrollToLatest()
    }
    Rectangle {
        objectName: "chatWindowSurface"
        anchors.fill: parent
        color: theme.panel
        radius: theme.windowRadius
        border.color: theme.line
    }
    component QuietButton: Button {
        id: button
        property string tip: text
        Accessible.name: tip
        implicitWidth: Math.max(36, implicitContentWidth + 16); implicitHeight: 36
        contentItem: Text { text: button.text; color: button.enabled ? theme.ink : theme.muted; opacity: button.enabled ? 0.8 : 0.4; font.pixelSize: 17; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { color: button.hovered || button.down ? theme.input : "transparent"; radius: 7; border.width: button.activeFocus ? 1 : 0; border.color: theme.accent }
        ToolTip.visible: hovered || activeFocus; ToolTip.text: tip; ToolTip.delay: 600
    }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 24; spacing: 10
        Item {
            id: chatHeader
            objectName: "chatHeader"
            Layout.fillWidth: true
            Layout.preferredHeight: 44
            Layout.minimumHeight: 44
            Layout.maximumHeight: 44
            Item {
                anchors.left: parent.left
                anchors.right: userProfile.left
                anchors.rightMargin: 12
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                WindowTitle { objectName: "chatWindowTitle"; anchors.fill: parent; theme: chat.theme; text: "Chat" }
                MouseArea { anchors.fill: parent; onPressed: chat.startSystemMove() }
            }
            UserBubble {
                id: userProfile
                objectName: "chatProfile"
                anchors.centerIn: parent
                width: 40
                height: 40
                control: chat.profileControl
                ink: theme.ink
                surface: theme.input
            }
            RowLayout {
                objectName: "chatWindowControls"
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                WindowControlButton { objectName: "chatSettingsButton"; theme: chat.theme; symbol: "⋯"; tip: "Chat settings"; onClicked: options.open() }
                WindowControlButton { theme: chat.theme; symbol: "−"; tip: "Minimize chat"; onClicked: chat.showMinimized() }
                WindowControlButton { objectName: "chatCloseButton"; theme: chat.theme; symbol: "×"; tip: "Close this chat"; onClicked: chat.close() }
            }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            Column {
                visible: session.messages.length === 0; anchors.centerIn: parent; width: parent.width - 24; spacing: 18
                Text { width: parent.width; text: userProfile.greeting; textFormat: Text.PlainText; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; color: theme.ink; opacity: 0.8; font.pixelSize: 24 }
                Button { objectName: "setupAccount"; visible: !userProfile.name; text: "Set up an account"; anchors.horizontalCenter: parent.horizontalCenter; onClicked: userProfile.createAccount() }
            }
            ListView {
                id: conversation; objectName: "conversation"
                visible: count > 0
                anchors.fill: parent; clip: true; spacing: 24
                model: ListModel { id: messageRows; dynamicRoles: true }
                property bool followLatest: true
                function syncMessages() {
                    // A QVariantList replacement resets ListView on every token.
                    // Update rows in place so streaming preserves layout and position.
                    var messages = session.messages
                    while (messageRows.count > messages.length) messageRows.remove(messageRows.count - 1)
                    for (var i = 0; i < messages.length; ++i) {
                        if (i >= messageRows.count) messageRows.append({message: messages[i]})
                        else if (JSON.stringify(messageRows.get(i).message) !== JSON.stringify(messages[i]))
                            messageRows.setProperty(i, "message", messages[i])
                    }
                    scrollToLatest()
                }
                function scrollToLatest() {
                    Qt.callLater(function() {
                        if (!conversation.followLatest || conversation.moving || scrollBar.pressed) return
                        conversation.forceLayout()
                        conversation.positionViewAtEnd()
                    })
                }
                onContentHeightChanged: scrollToLatest()
                onHeightChanged: scrollToLatest()
                onMovementStarted: followLatest = false
                onMovementEnded: { followLatest = atYEnd; if (followLatest) scrollToLatest() }
                ScrollBar.vertical: ScrollBar {
                    id: scrollBar
                    onPressedChanged: {
                        conversation.followLatest = !pressed && conversation.atYEnd
                        if (conversation.followLatest) conversation.scrollToLatest()
                    }
                }
                delegate: Column {
                    required property var message
                    readonly property var modelData: message
                    width: conversation.width - 12; spacing: 7
                    onHeightChanged: conversation.scrollToLatest()
                    Text { text: modelData.role === "user" ? "You" : "AI"; color: theme.muted; opacity: 0.65; font.pixelSize: 11 }
                    TextEdit { id: reply; width: parent.width; text: modelData.display_text || modelData.content || "…"; color: theme.ink; font.pixelSize: 16; wrapMode: TextEdit.Wrap; readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText }
                    Row {
                        visible: modelData.role === "assistant" && modelData.content.length > 0 && !session.busy
                        spacing: 2; opacity: reply.activeFocus || replyActions.containsMouse ? 1 : 0.45
                        HoverHandler { id: replyHover }
                        property bool containsMouse: replyHover.hovered
                        id: replyActions
                        QuietButton { tip: "Copy reply"; onClicked: session.copy(modelData.content)
                            contentItem: Item {
                                Rectangle { x: (parent.width-12)/2; y: (parent.height-14)/2; width: 9; height: 11; radius: 1; color: "transparent"; border.color: theme.muted }
                                Rectangle { x: (parent.width-12)/2+3; y: (parent.height-14)/2+3; width: 9; height: 11; radius: 1; color: theme.panel; border.color: theme.muted }
                            }
                        }
                        QuietButton { text: session.speaking ? "■" : "♪"; tip: session.speaking ? "Stop spoken reply" : "Read aloud · synthesized voice"; onClicked: session.readReply(modelData.content) }
                    }
                }
            }
        }
        Text { Layout.fillWidth: true; visible: text.length > 0; text: session.recording ? "Listening… tap the microphone to finish" : session.status; textFormat: Text.PlainText; color: session.recording ? theme.accent : theme.muted; font.pixelSize: 12; wrapMode: Text.Wrap }
        Flow {
            Layout.fillWidth: true; visible: session.attachments.length > 0; spacing: 6
            Repeater {
                model: session.attachments
                Rectangle {
                    required property string modelData; required property int index
                    width: Math.min(230, chip.implicitWidth + 42); height: 28; radius: 6; color: theme.input
                    Text { id: chip; text: modelData; textFormat: Text.PlainText; color: theme.muted; font.pixelSize: 11; anchors.left: parent.left; anchors.leftMargin: 8; anchors.right: remove.left; anchors.verticalCenter: parent.verticalCenter; elide: Text.ElideMiddle }
                    QuietButton { id: remove; text: "×"; tip: "Remove " + modelData; anchors.right: parent.right; width: 28; height: 28; onClicked: session.removeAttachment(index) }
                }
            }
        }
        Rectangle {
            Layout.fillWidth: true; implicitHeight: Math.min(180, Math.max(112, composer.contentHeight + 62))
            color: theme.input; radius: 12; border.color: composer.activeFocus ? theme.line : "transparent"
            ScrollView {
                anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: tools.top; anchors.margins: 12
                TextArea {
                    id: composer; objectName: "composer"; placeholderText: "Message…"; placeholderTextColor: theme.muted; color: theme.ink; font.pixelSize: 16; wrapMode: TextEdit.Wrap; selectByMouse: true; background: null
                    Keys.onReturnPressed: function(event) { if (!(event.modifiers & Qt.ShiftModifier)) { chat.submit(); event.accepted = true } else event.accepted = false }
                }
            }
            RowLayout {
                id: tools; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 8; spacing: 3
                QuietButton { text: "+"; tip: "Attach text, source code or PDF"; enabled: !session.busy && !session.recording; onClicked: attachmentDialog.open() }
                VoiceButton {
                    theme: chat.theme; active: session.recording; enabled: !session.busy
                    onClicked: {
                        if (!session.recording && backend.config.voice_mode !== "local" && !backend.config.voice_url) { modelSettings.currentTab = 1; options.open() }
                        else session.setVoiceActive(!session.recording)
                    }
                }
                QuietButton { visible: session.recording; text: "×"; tip: "Discard recording"; onClicked: session.cancelRecording() }
                Item { Layout.fillWidth: true }
                QuietButton { text: session.busy ? "■" : "↑"; tip: session.busy ? "Stop" : "Send message"; enabled: session.busy || (!session.recording && (composer.text.trim().length > 0 || session.attachments.length > 0)); onClicked: session.busy ? session.stop() : chat.submit() }
            }
        }
        Text { visible: backend.config.live === true; text: "Live session · Chats are lost after reboot"; color: theme.muted; opacity: 0.5; font.pixelSize: 10 }
    }
    FileDialog {
        id: attachmentDialog; title: "Attach a file"; fileMode: FileDialog.OpenFile
        nameFilters: ["Text, source code and PDF (*.txt *.md *.csv *.json *.py *.cpp *.h *.js *.ts *.qml *.html *.css *.sh *.log *.pdf)", "All files (*)"]
        onAccepted: session.attach(selectedFile)
    }
    Popup {
        id: options; parent: chat.contentItem; anchors.centerIn: parent
        objectName: "chatSettingsPopup"
        width: Math.min(490, parent.width - 24); height: Math.min(560, parent.height - 24)
        modal: true; focus: true; padding: 20; closePolicy: Popup.CloseOnEscape
        background: Rectangle {
            color: theme.panel; radius: theme.windowRadius
            WindowBorder { objectName: "chatSettingsBorder"; theme: chat.theme; radius: parent.radius }
        }
        onOpened: { modelSettings.reload(); if (optionsTabs.currentIndex === 1) chatAccounts.refresh(); }
        contentItem: ColumnLayout {
            spacing: 12
            RowLayout {
                objectName: "chatSettingsHeader"
                Layout.fillWidth: true
                Layout.minimumHeight: 44
                Layout.maximumHeight: 44
                WindowTitle { objectName: "chatSettingsTitle"; theme: chat.theme; text: "Chat settings"; Layout.fillWidth: true }
                WindowControlButton {
                    objectName: "closeChatSettings"
                    theme: chat.theme
                    symbol: "\u00d7"
                    tip: "Close chat settings"
                    onClicked: options.close()
                }
            }
            TabBar {
                id: optionsTabs; objectName: "chatSettingsTabs"; Layout.fillWidth: true
                TabButton { text: "AI and voice" }
                TabButton { objectName: "accountsTab"; text: "Accounts" }
            }
            StackLayout {
                currentIndex: optionsTabs.currentIndex; Layout.fillWidth: true; Layout.fillHeight: true
                ModelSettings { id: modelSettings; objectName: "chatModelSettings"; showClose: false; backend: chat.backend; theme: chat.theme; onCloseRequested: options.close() }
                AccountSettings { id: chatAccounts; control: chat.profileControl }
            }
        }
    }
    Connections {
        target: session
        function onChanged() { conversation.syncMessages() }
        function onTranscribed(text) { composer.text += (composer.text ? " " : "") + text; composer.forceActiveFocus() }
    }
    WindowBorder { theme: chat.theme }
}
