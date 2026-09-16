import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes
import QtQuick.Window

Window {
    id: desktop
    property bool chatPreview: Qt.application.arguments.indexOf("--chat") >= 0
    property bool windowed: chatPreview || Qt.application.arguments.indexOf("--windowed") >= 0
    visible: !chatPreview
    title: "AIOS Desktop"
    width: windowed ? Math.min(1100, Screen.width - 80) : Screen.width
    height: windowed ? Math.min(760, Screen.height - 80) : Screen.height
    // A normal window can be raised over applications even with the below hint.
    // The desktop role keeps wallpaper and its controls in the desktop layer.
    flags: windowed ? Qt.Window : Qt.Desktop | Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint
    color: theme.night
    property var backendApi: typeof backend === "undefined" ? null : backend
    property var sessionControlApi: typeof sessionControl === "undefined" ? null : sessionControl
    property var displayBridgeApi: typeof displayBridge === "undefined" ? ({enabled: false}) : displayBridge
    property var awayChats: []
    readonly property int awayChatCount: awayChats.length
    property date currentTime: new Date()
    Theme { id: theme; selected: backendApi.config.theme_color || "blue"; reducedMotion: desktop.reducedMotion }
    Connections { target: theme; function onWaveChanged() { waves.requestPaint() } }
    property bool reducedMotion: backendApi.config.reduced_motion === true
    function removeAwayChat(window) {
        var remaining = []
        for (var i = 0; i < awayChats.length; ++i) {
            if (awayChats[i] !== window)
                remaining.push(awayChats[i])
        }
        awayChats = remaining
    }
    function trackAwayChat(window) {
        removeAwayChat(window)
        var updated = awayChats.slice()
        updated.push(window)
        awayChats = updated
    }
    function restoreAwayChat() {
        var pending = awayChats.slice()
        while (pending.length > 0) {
            var window = pending.pop()
            awayChats = pending.slice()
            if (!window || window.isClosing)
                continue
            if (window.visibility !== Window.Minimized && window.visibility !== Window.Hidden)
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
        var restored = restoreAwayChat()
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
        window.putAway.connect(function() { desktop.trackAwayChat(window) })
        window.removed.connect(function() { desktop.removeAwayChat(window) })
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
        function onSettingsRequested(section) {
            desktop.openSettings()
            if (desktop.settingsWindow) desktop.settingsWindow.openSection(section)
        }
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
    Timer {
        interval: 1000
        repeat: true
        running: desktop.visible
        onTriggered: desktop.currentTime = new Date()
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
    Text {
        id: wordmark
        objectName: "desktopWordmark"
        x: 48
        y: 36
        text: "aios"
        color: theme.ink
        opacity: 0.65
        font.pixelSize: 22
        font.letterSpacing: 4
    }
    Text {
        objectName: "desktopClock"
        anchors.right: parent.right
        anchors.rightMargin: 48
        // The clock renders smaller than the wordmark; matching font
        // baselines keeps their visible bottoms aligned instead of leaving
        // the clock floating above the title.
        anchors.baseline: wordmark.baseline
        text: Qt.formatTime(desktop.currentTime, "h:mm AP")
        color: wordmark.color
        opacity: wordmark.opacity
        font.letterSpacing: wordmark.font.letterSpacing
        font.pixelSize: wordmark.font.pixelSize * 0.75
    }
    Loader {
        x: 410; y: 84; width: Math.max(0, desktop.width - 440); height: Math.max(0, desktop.height - 180)
        active: displayBridgeApi.enabled && sessionControlApi.embeddedDisplay
        onActiveChanged: {
            if (active) setSource("PrivateDisplay.qml", {control: sessionControlApi, bridge: displayBridgeApi})
            else setSource("")
        }
    }
    IdentityStatus { x: 48; y: 84; z: 100; visible: sessionControlApi.enabled; control: sessionControlApi; theme: theme }
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
        QuietButton {
            objectName: "desktopSettingsButton"
            tip: "Settings"
            implicitWidth: 44
            fadesWhenIdle: true
            onClicked: desktop.openSettings()
            contentItem: Canvas { onPaint: {
                var c = getContext("2d"); c.reset(); c.strokeStyle = theme.ink; c.lineWidth = 1.5;
                for (var i = 0; i < 3; i++) {
                    var x = width/2 - 7 + i*7; var y = height/2 + (i === 1 ? -3 : 3);
                    c.beginPath(); c.moveTo(x,height/2-9); c.lineTo(x,height/2+9); c.stroke();
                    c.fillStyle = theme.panel; c.fillRect(x-2,y-2,4,4); c.strokeRect(x-2,y-2,4,4);
                }
            } }
        }
        QuietButton {
            objectName: "desktopTerminalButton"
            text: ">_"
            tip: "Terminal"
            fadesWhenIdle: true
            onClicked: sessionControlApi.enabled ? sessionControlApi.launch("terminal") : backendApi.terminal()
        }
        QuietButton {
            id: volumeButton
            objectName: "volumeButton"
            tip: backendApi.volumeAvailable ? (backendApi.muted ? "Volume muted" : "Volume · " + backendApi.volume + "%") : "Volume"
            implicitWidth: 44
            fadesWhenIdle: true
            onClicked: volumePopup.open()
            contentItem: SpeakerIcon {
                muted: backendApi.muted
                volume: backendApi.volume
                glyphObjectName: "volumeButtonGlyph"
            }
        }
        QuietButton {
            objectName: "desktopPowerButton"
            tip: "Power"
            implicitWidth: 44
            fadesWhenIdle: true
            onClicked: powerDialog.open()
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
        property bool outlined: false
        property bool fadesWhenIdle: false
        Accessible.name: tip
        hoverEnabled: true
        opacity: fadesWhenIdle && !hovered && !activeFocus && !down ? 0.65 : 1
        implicitWidth: Math.max(44, implicitContentWidth + 24); implicitHeight: 44
        contentItem: Text { text: control.text; color: control.enabled ? theme.ink : theme.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; font.pixelSize: 16 }
        background: Rectangle {
            radius: 8
            color: control.down || control.hovered ? theme.input : "transparent"
            border.width: control.activeFocus ? 2 : control.outlined ? 1 : 0
            border.color: control.activeFocus ? theme.accent : theme.line
        }
        Behavior on opacity { NumberAnimation { duration: desktop.reducedMotion ? 0 : 120 } }
        ToolTip.visible: hovered || activeFocus; ToolTip.text: tip; ToolTip.delay: activeFocus ? 0 : 700
    }
    component SpeakerIcon: Item {
        id: speakerIcon
        property bool muted: false
        property real volume: 50
        property string glyphObjectName
        implicitWidth: 22
        implicitHeight: 22
        Shape {
            objectName: speakerIcon.glyphObjectName
            anchors.centerIn: parent
            width: 22
            height: 22
            ShapePath {
                strokeColor: theme.ink
                strokeWidth: 1.7
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                startX: 1.5
                startY: 9
                PathLine { x: 6.5; y: 9 }
                PathLine { x: 12; y: 4.5 }
                PathLine { x: 12; y: 17.5 }
                PathLine { x: 6.5; y: 13 }
                PathLine { x: 1.5; y: 13 }
                PathLine { x: 1.5; y: 9 }
            }
            ShapePath {
                strokeColor: speakerIcon.muted || speakerIcon.volume <= 0 ? theme.ink : "transparent"
                strokeWidth: 1.7
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                startX: 15
                startY: 7.5
                PathLine { x: 21; y: 14.5 }
            }
            ShapePath {
                strokeColor: speakerIcon.muted || speakerIcon.volume <= 0 ? theme.ink : "transparent"
                strokeWidth: 1.7
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                startX: 21
                startY: 7.5
                PathLine { x: 15; y: 14.5 }
            }
            ShapePath {
                strokeColor: !speakerIcon.muted && speakerIcon.volume > 0 ? theme.ink : "transparent"
                strokeWidth: 1.7
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                startX: 14.25
                startY: 7.1
                PathArc { x: 14.25; y: 14.9; radiusX: 4.5; radiusY: 4.5; direction: PathArc.Clockwise }
            }
            ShapePath {
                strokeColor: !speakerIcon.muted && speakerIcon.volume > 50 ? theme.ink : "transparent"
                strokeWidth: 1.7
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                startX: 16
                startY: 4.1
                PathArc { x: 16; y: 17.9; radiusX: 8; radiusY: 8; direction: PathArc.Clockwise }
            }
        }
    }
    component Field: TextField {
        color: theme.ink; placeholderTextColor: theme.muted; selectByMouse: true
        font.pixelSize: 14; padding: 12
        background: Rectangle { color: theme.input; radius: 6; border.color: parent.activeFocus ? theme.accent : theme.line }
    }
    Component { id: chatComponent; ChatWindow {} }
    Timer {
        id: volumeCommit
        interval: 100
        onTriggered: backendApi.setVolume(Math.round(volumeSlider.value))
    }
    Popup {
        id: volumePopup
        objectName: "volumePopup"
        parent: Overlay.overlay
        width: 76
        height: 276
        padding: 8
        modal: false
        dim: false
        focus: true
        popupType: Popup.Item
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        property bool syncingVolume: false
        function setVolumeImmediately(value) {
            volumeCommit.stop()
            syncingVolume = true
            volumeSlider.value = Math.max(0, Math.min(100, value))
            syncingVolume = false
            backendApi.setVolume(Math.round(volumeSlider.value))
        }
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
            radius: theme.windowRadius
            border.color: theme.line
        }
        contentItem: ColumnLayout {
            spacing: 4
            QuietButton {
                id: volumeUpButton
                objectName: "volumeUpButton"
                text: "+"
                tip: "Increase volume"
                enabled: backendApi.volumeAvailable
                Layout.alignment: Qt.AlignHCenter
                onClicked: volumePopup.setVolumeImmediately(volumeSlider.value + 5)
                contentItem: Text {
                    text: "+"
                    color: volumeUpButton.enabled ? theme.ink : theme.muted
                    font.pixelSize: 22
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
            Slider {
                id: volumeSlider
                objectName: "volumeSlider"
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: 44
                Layout.preferredHeight: 120
                orientation: Qt.Vertical
                from: 0
                to: 100
                stepSize: 1
                topPadding: 10
                bottomPadding: 10
                leftPadding: 0
                rightPadding: 0
                enabled: backendApi.volumeAvailable
                Accessible.name: "Speaker volume"
                onValueChanged: {
                    if (volumePopup.opened && !volumePopup.syncingVolume)
                        volumeCommit.restart()
                }
                background: Rectangle {
                    x: volumeSlider.leftPadding + (volumeSlider.availableWidth - width) / 2
                    y: volumeSlider.topPadding
                    implicitWidth: 4
                    implicitHeight: 100
                    width: 4
                    height: volumeSlider.availableHeight
                    radius: 2
                    color: theme.line
                    Rectangle {
                        anchors.bottom: parent.bottom
                        width: parent.width
                        height: parent.height * volumeSlider.value / 100
                        radius: parent.radius
                        color: volumeSlider.enabled ? theme.accent : theme.muted
                    }
                }
                handle: Rectangle {
                    x: volumeSlider.leftPadding + (volumeSlider.availableWidth - width) / 2
                    y: volumeSlider.topPadding + volumeSlider.visualPosition * (volumeSlider.availableHeight - height)
                    implicitWidth: 22
                    implicitHeight: 22
                    radius: 11
                    color: volumeSlider.enabled ? theme.ink : theme.muted
                    border.width: 2
                    border.color: theme.panel
                }
            }
            QuietButton {
                id: volumeDownButton
                objectName: "volumeDownButton"
                text: "-"
                tip: "Decrease volume"
                enabled: backendApi.volumeAvailable
                Layout.alignment: Qt.AlignHCenter
                onClicked: volumePopup.setVolumeImmediately(volumeSlider.value - 5)
                contentItem: Text {
                    text: "\u2212"
                    color: volumeDownButton.enabled ? theme.ink : theme.muted
                    font.pixelSize: 22
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
            }
            QuietButton {
                id: muteButton
                objectName: "muteButton"
                tip: backendApi.muted ? "Unmute speaker" : "Mute speaker"
                enabled: backendApi.volumeAvailable
                Layout.alignment: Qt.AlignHCenter
                onClicked: backendApi.setMuted(!backendApi.muted)
                contentItem: SpeakerIcon {
                    muted: backendApi.muted
                    volume: backendApi.volume
                    glyphObjectName: "muteButtonGlyph"
                }
            }
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
            radius: theme.windowRadius
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
                    objectName: "restartAction"
                    text: "Restart"
                    symbol: "restart"
                    onClicked: { powerDialog.close(); backendApi.power("reboot") }
                }
                PowerAction {
                    objectName: "shutdownAction"
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
        Layout.preferredWidth: 1
        implicitHeight: 108
        hoverEnabled: true
        Accessible.name: text
        contentItem: ColumnLayout {
            spacing: 12
            Canvas {
                id: powerActionIcon
                objectName: action.symbol + "ActionIcon"
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: 34
                Layout.preferredHeight: 34
                readonly property real strokeWidth: 2.5
                onPaint: {
                    var c = getContext("2d"); c.reset()
                    c.strokeStyle = theme.accent; c.lineWidth = strokeWidth; c.lineCap = "round"; c.lineJoin = "round"
                    c.beginPath()
                    if (action.symbol === "power") {
                        c.arc(17, 18, 11, -Math.PI / 3, Math.PI * 4 / 3)
                        c.stroke(); c.beginPath(); c.moveTo(17, 3); c.lineTo(17, 15)
                    } else {
                        c.save(); c.translate(width, 0); c.scale(-1, 1)
                        c.arc(17, 17, 11, -Math.PI / 2, Math.PI)
                        c.stroke(); c.beginPath(); c.moveTo(2, 22); c.lineTo(6, 17); c.lineTo(10, 22)
                        c.stroke(); c.restore()
                        return
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
