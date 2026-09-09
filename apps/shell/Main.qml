import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Window {
    id: desktop
    property bool chatPreview: Qt.application.arguments.indexOf("--chat") >= 0
    property bool windowed: chatPreview || Qt.application.arguments.indexOf("--windowed") >= 0
    visible: !chatPreview
    title: "AIOS Desktop"
    width: windowed ? Math.min(1100, Screen.width - 80) : Screen.width
    height: windowed ? Math.min(760, Screen.height - 80) : Screen.height
    flags: windowed ? Qt.Window : Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint
    color: theme.night
    property var backendApi: typeof backend === "undefined" ? null : backend
    property var sessionControlApi: typeof sessionControl === "undefined" ? null : sessionControl
    property var displayBridgeApi: typeof displayBridge === "undefined" ? ({enabled: false}) : displayBridge
    property var minimizedChats: []
    readonly property int minimizedChatCount: minimizedChats.length
    Theme { id: theme; selected: backendApi.config.theme_color || "blue" }
    Connections { target: theme; function onWaveChanged() { waves.requestPaint() } }
    property bool reducedMotion: backendApi.config.reduced_motion === true
    function removeMinimizedChat(window) {
        var remaining = []
        for (var i = 0; i < minimizedChats.length; ++i) {
            if (minimizedChats[i] !== window)
                remaining.push(minimizedChats[i])
        }
        minimizedChats = remaining
    }
    function trackMinimizedChat(window) {
        removeMinimizedChat(window)
        var updated = minimizedChats.slice()
        updated.push(window)
        minimizedChats = updated
    }
    function restoreMinimizedChat() {
        var pending = minimizedChats.slice()
        while (pending.length > 0) {
            var window = pending.pop()
            minimizedChats = pending.slice()
            if (!window || window.visibility !== Window.Minimized)
                continue
            window.showNormal()
            window.raise()
            window.requestActivate()
            return window
        }
        return null
    }
    function openChat() {
        if (sessionControlApi.enabled)
            return null
        var restored = restoreMinimizedChat()
        if (restored)
            return restored
        var window = chatComponent.createObject(desktop, {
            backend: backendApi,
            session: backendApi.createSession(),
            theme: theme,
            profileControl: sessionControlApi.chatProfile(),
            ownsProfileControl: true
        })
        if (!window)
            return null
        window.minimized.connect(function() { desktop.trackMinimizedChat(window) })
        window.removed.connect(function() { desktop.removeMinimizedChat(window) })
        window.show()
        window.raise()
        window.requestActivate()
        return window
    }
    property var settingsWindow: null
    property var setupWindow: null
    function openSetup() {
        if (sessionControlApi.enabled) return
        if (!setupWindow) setupWindow = setupComponent.createObject(desktop, {backend: backendApi, theme: theme, profileControl: sessionControlApi})
        if (setupWindow) { setupWindow.show(); setupWindow.raise(); setupWindow.requestActivate() }
    }
    Connections {
        target: backendApi
        function onLoaded() { if (backendApi.setupPending()) desktop.openSetup() }
    }
    Component { id: setupComponent; SetupWizard {} }
    function openSettings() {
        if (sessionControlApi.enabled) return
        if (!settingsWindow) settingsWindow = settingsComponent.createObject(desktop, {backend: backendApi, theme: theme, profileControl: sessionControlApi})
        if (settingsWindow) { settingsWindow.show(); settingsWindow.raise(); settingsWindow.requestActivate() }
    }
    Component { id: settingsComponent; SettingsWindow { onSetupRequested: desktop.openSetup() } }
    property real phase: 0
    NumberAnimation on phase { from: 0; to: Math.PI * 2; duration: 48000; loops: Animation.Infinite; running: !desktop.reducedMotion && desktop.visible }
    onReducedMotionChanged: waves.requestPaint()
    Timer {
        interval: 33; repeat: true
        running: !desktop.reducedMotion && desktop.visible
        onTriggered: waves.requestPaint()
    }
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
            // Three open crests, each with a contour-following fade below it.
            // All harmonics repeat seamlessly after the 48-second cycle.
            function wave(u, layer) {
                var t = desktop.phase
                return height * (0.54 + layer * 0.028
                    + Math.sin(u * 5.0 + t + layer * 1.45) * 0.078
                    + Math.cos(u * 2.7 - t * 2 + layer * 0.85) * 0.035)
            }
            var fadeDepth = Math.min(130, height * 0.16)
            // Short vertical tiles keep the gradient attached to the curve,
            // rather than filling a closed ribbon with a visible lower edge.
            for (var layer = 2; layer >= 0; --layer) {
                for (var x = 0; x < width; x += 10) {
                    var nextX = Math.min(width, x + 10)
                    var y0 = wave(x / width, layer), y1 = wave(nextX / width, layer)
                    var top = (y0 + y1) / 2
                    var fade = ctx.createLinearGradient(0, top, 0, top + fadeDepth)
                    fade.addColorStop(0, theme.waveAlpha(0.18))
                    fade.addColorStop(0.25, theme.waveAlpha(0.09))
                    fade.addColorStop(0.65, theme.waveAlpha(0.02))
                    fade.addColorStop(1, theme.waveAlpha(0))
                    ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(nextX, y1)
                    ctx.lineTo(nextX, y1 + fadeDepth); ctx.lineTo(x, y0 + fadeDepth); ctx.closePath()
                    ctx.fillStyle = fade; ctx.fill()
                }
            }
            // Draw crests last so crossing fades never soften their solid edge.
            for (var crest = 2; crest >= 0; --crest) {
                ctx.beginPath()
                for (var i = 0; i <= 160; ++i) {
                    var u = i / 160, y = wave(u, crest)
                    if (i === 0) ctx.moveTo(0, y); else ctx.lineTo(u * width, y)
                }
                ctx.lineJoin = "round"
                ctx.strokeStyle = theme.waveAlpha(0.08); ctx.lineWidth = 3.5; ctx.stroke()
                ctx.strokeStyle = theme.waveAlpha(0.50); ctx.lineWidth = 1.3; ctx.stroke()
            }
        }
    }
    Text { x: 48; y: 36; text: "aios"; color: theme.ink; opacity: 0.65; font.pixelSize: 22; font.letterSpacing: 4 }
    Loader {
        x: 410; y: 84; width: Math.max(0, desktop.width - 440); height: Math.max(0, desktop.height - 180)
        active: displayBridgeApi.enabled && sessionControlApi.embeddedDisplay
        onActiveChanged: {
            if (active) setSource("PrivateDisplay.qml", {control: sessionControlApi, bridge: displayBridgeApi})
            else setSource("")
        }
    }
    IdentityStatus { x: 48; y: 84; z: 100; visible: sessionControlApi.enabled; control: sessionControlApi }
    Loader { active: !displayBridgeApi.enabled; sourceComponent: Component { PrivacyShield { control: sessionControlApi } } }
    Loader { active: !displayBridgeApi.enabled; sourceComponent: Component { SecurePinPrompt { control: sessionControlApi } } }
    SecurePinOverlay { parent: desktop.contentItem; control: sessionControlApi; visible: displayBridgeApi.enabled && sessionControlApi.enabled && Object.keys(sessionControlApi.challenge).length > 0 }
    Rectangle {
        anchors.fill: parent; z: 100000; color: "#101b27"
        visible: displayBridgeApi.enabled && sessionControlApi.enabled && sessionControlApi.shield
        MouseArea { anchors.fill: parent }
        Column {
            anchors.centerIn: parent; spacing: 16
            Label { text: "Personal work is hidden"; color: "white"; font.pixelSize: 28 }
            Button { text: "Return to anonymous"; onClicked: sessionControlApi.suspend() }
        }
    }
    ChatOrb {
        id: launcher
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom; anchors.bottomMargin: 8
        theme: theme; reducedMotion: desktop.reducedMotion
        onClicked: desktop.openChat()
    }
    Row {
        anchors.right: parent.right; anchors.rightMargin: 32; anchors.verticalCenter: launcher.verticalCenter; spacing: 12
        QuietButton { tip: "Settings"; implicitWidth: 44; onClicked: desktop.openSettings()
            contentItem: Canvas { onPaint: {
                var c = getContext("2d"); c.reset(); c.strokeStyle = theme.ink; c.lineWidth = 1.5;
                for (var i = 0; i < 3; i++) {
                    var x = width/2 - 7 + i*7; var y = height/2 + (i === 1 ? -3 : 3);
                    c.beginPath(); c.moveTo(x,height/2-9); c.lineTo(x,height/2+9); c.stroke();
                    c.fillStyle = theme.panel; c.fillRect(x-2,y-2,4,4); c.strokeRect(x-2,y-2,4,4);
                }
            } }
        }
        QuietButton { text: ">_"; tip: "Terminal"; onClicked: sessionControlApi.enabled ? sessionControlApi.launch("terminal") : backendApi.terminal() }
        QuietButton {
            id: volumeButton
            objectName: "volumeButton"
            tip: backendApi.volumeAvailable ? (backendApi.muted ? "Volume muted" : "Volume · " + backendApi.volume + "%") : "Volume"
            implicitWidth: 44
            onClicked: volumePopup.open()
            contentItem: SpeakerIcon { muted: backendApi.muted; volume: backendApi.volume }
        }
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
    component SpeakerIcon: Item {
        id: speakerIcon
        property bool muted: false
        property real volume: 50
        implicitWidth: 20
        implicitHeight: 20
        Canvas {
            id: speakerCanvas
            anchors.fill: parent
            onPaint: {
                var c = getContext("2d")
                c.reset()
                c.strokeStyle = theme.ink
                c.lineWidth = 1.5
                c.lineCap = "round"
                c.lineJoin = "round"
                c.beginPath()
                c.moveTo(2.5, 8)
                c.lineTo(6.5, 8)
                c.lineTo(11, 4.5)
                c.lineTo(11, 15.5)
                c.lineTo(6.5, 12)
                c.lineTo(2.5, 12)
                c.closePath()
                c.stroke()
                if (speakerIcon.muted || speakerIcon.volume <= 0) {
                    c.beginPath()
                    c.moveTo(14, 7)
                    c.lineTo(19, 13)
                    c.moveTo(19, 7)
                    c.lineTo(14, 13)
                    c.stroke()
                    return
                }
                c.beginPath()
                c.arc(11, 10, 4, -Math.PI / 3, Math.PI / 3)
                c.stroke()
                if (speakerIcon.volume > 50) {
                    c.beginPath()
                    c.arc(11, 10, 7, -Math.PI / 3, Math.PI / 3)
                    c.stroke()
                }
            }
        }
        onMutedChanged: speakerCanvas.requestPaint()
        onVolumeChanged: speakerCanvas.requestPaint()
        onWidthChanged: speakerCanvas.requestPaint()
        onHeightChanged: speakerCanvas.requestPaint()
    }
    component Field: TextField {
        color: theme.ink; placeholderTextColor: theme.muted; selectByMouse: true
        font.pixelSize: 14; padding: 12
        background: Rectangle { color: theme.input; radius: 6; border.color: parent.activeFocus ? theme.accent : theme.line }
    }
    Component { id: chatComponent; ChatWindow {} }
    Popup {
        id: volumePopup
        objectName: "volumePopup"
        parent: Overlay.overlay
        width: 88
        height: 236
        padding: 10
        modal: false
        dim: false
        focus: true
        popupType: Popup.Item
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property bool syncingVolume: false
        function positionAboveButton() {
            var point = volumeButton.mapToItem(parent, 0, 0)
            x = Math.max(8, Math.min(parent.width - width - 8, point.x + (volumeButton.width - width) / 2))
            y = Math.max(8, point.y - height - 8)
        }
        onAboutToShow: positionAboveButton()
        onOpened: {
            backendApi.refreshVolume()
            syncingVolume = true
            volumeSlider.value = backendApi.volume
            syncingVolume = false
            volumeSlider.forceActiveFocus(Qt.TabFocusReason)
        }
        background: Rectangle {
            color: theme.panel
            radius: 16
            border.color: theme.line
        }
        contentItem: ColumnLayout {
            spacing: 8
            QuietButton {
                id: muteButton
                objectName: "muteButton"
                tip: backendApi.muted ? "Unmute speaker" : "Mute speaker"
                enabled: backendApi.volumeAvailable
                Layout.alignment: Qt.AlignHCenter
                onClicked: backendApi.setMuted(!backendApi.muted)
                contentItem: SpeakerIcon { muted: backendApi.muted; volume: backendApi.volume }
            }
            Slider {
                id: volumeSlider
                objectName: "volumeSlider"
                Layout.alignment: Qt.AlignHCenter
                Layout.fillHeight: true
                orientation: Qt.Vertical
                from: 0
                to: 100
                stepSize: 1
                value: backendApi.volume
                enabled: backendApi.volumeAvailable
                Accessible.name: "Speaker volume"
                onValueChanged: {
                    if (volumePopup.opened && !volumePopup.syncingVolume)
                        volumeCommit.restart()
                }
            }
            Text {
                Layout.alignment: Qt.AlignHCenter
                text: backendApi.volumeAvailable ? Math.round(volumeSlider.value) + "%" : "—"
                color: backendApi.volumeAvailable ? theme.ink : theme.muted
                font.pixelSize: 12
            }
        }
        Timer {
            id: volumeCommit
            interval: 100
            onTriggered: backendApi.setVolume(Math.round(volumeSlider.value))
        }
        Connections {
            target: backendApi
            function onVolumeChanged() {
                if (!volumeSlider.pressed) {
                    volumePopup.syncingVolume = true
                    volumeSlider.value = backendApi.volume
                    volumePopup.syncingVolume = false
                }
            }
        }
    }
    // Hallmark · pre-emit critique: P4 H5 E4 S4 R5 V4
    Popup {
        id: powerDialog
        objectName: "powerDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(420, parent.width - 32)
        padding: 28
        modal: true
        dim: true
        focus: true
        // Keep rounded corners on the desktop surface, without native window edges.
        popupType: Popup.Item
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onOpened: cancelPower.forceActiveFocus(Qt.TabFocusReason)
        Overlay.modal: Rectangle {
            color: Qt.rgba(theme.night.r, theme.night.g, theme.night.b, 0.62)
            Behavior on opacity { NumberAnimation { duration: desktop.reducedMotion ? 0 : 160 } }
        }
        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: desktop.reducedMotion ? 0 : 160; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1; to: 0; duration: desktop.reducedMotion ? 0 : 100 }
        }
        background: Rectangle {
            color: theme.panel
            radius: 24
            border.color: Qt.rgba(theme.line.r, theme.line.g, theme.line.b, 0.55)
        }
        contentItem: ColumnLayout {
            spacing: 20
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: "Ready to leave?"
                    color: theme.ink
                    font.pixelSize: 26
                    font.weight: Font.DemiBold
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                }
                Text {
                    text: "Save your work before you go."
                    color: theme.muted
                    font.pixelSize: 15
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                }
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                PowerAction {
                    text: "Restart"
                    symbol: "restart"
                    onClicked: { powerDialog.close(); backendApi.power("reboot") }
                }
                PowerAction {
                    text: "Shut down"
                    symbol: "power"
                    onClicked: { powerDialog.close(); backendApi.power("poweroff") }
                }
            }
            QuietButton {
                id: cancelPower
                text: "Cancel"
                Layout.fillWidth: true
                onClicked: powerDialog.close()
                ToolTip.visible: false
            }
        }
    }
    component PowerAction: Button {
        id: action
        property string symbol
        Layout.fillWidth: true
        implicitHeight: 108
        hoverEnabled: true
        Accessible.name: text
        contentItem: ColumnLayout {
            spacing: 12
            Canvas {
                id: powerActionIcon
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: 28
                Layout.preferredHeight: 28
                onPaint: {
                    var c = getContext("2d"); c.reset()
                    c.strokeStyle = theme.accent; c.lineWidth = 1.8; c.lineCap = "round"; c.lineJoin = "round"
                    c.beginPath()
                    if (action.symbol === "power") {
                        c.arc(14, 15, 9, -Math.PI / 3, Math.PI * 4 / 3)
                        c.stroke(); c.beginPath(); c.moveTo(14, 3); c.lineTo(14, 13)
                    } else {
                        c.arc(14, 14, 9, -Math.PI / 2, Math.PI)
                        c.stroke(); c.beginPath(); c.moveTo(3, 9); c.lineTo(5, 15); c.lineTo(11, 13)
                    }
                    c.stroke()
                }
                Connections { target: theme; function onAccentChanged() { powerActionIcon.requestPaint() } }
            }
            Text {
                text: action.text
                color: theme.ink
                font.pixelSize: 16
                Layout.alignment: Qt.AlignHCenter
            }
        }
        background: Rectangle {
            radius: 16
            color: action.down ? theme.line : action.hovered ? Qt.lighter(theme.input, 1.18) : theme.input
            border.width: action.visualFocus ? 2 : 0
            border.color: theme.accent
            Behavior on color { ColorAnimation { duration: desktop.reducedMotion ? 0 : 100 } }
        }
        opacity: enabled ? 1 : 0.5
    }

}
