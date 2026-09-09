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
    Theme { id: theme; selected: backend.config.theme_color || "blue" }
    Connections { target: theme; function onWaveChanged() { waves.requestPaint() } }
    property bool reducedMotion: backend.config.reduced_motion === true
    function openChat() { if (sessionControl.enabled) return; var window = chatComponent.createObject(desktop, {backend: backend, session: backend.createSession(), theme: theme}); if (window) { window.show(); window.raise(); window.requestActivate() } }
    property var settingsWindow: null
    function openSettings() {
        if (sessionControl.enabled) return
        if (!settingsWindow) settingsWindow = settingsComponent.createObject(desktop, {backend: backend, theme: theme})
        if (settingsWindow) { settingsWindow.show(); settingsWindow.raise(); settingsWindow.requestActivate() }
    }
    Component { id: settingsComponent; SettingsWindow {} }
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
        active: displayBridge.enabled && sessionControl.embeddedDisplay
        onActiveChanged: {
            if (active) setSource("PrivateDisplay.qml", {control: sessionControl, bridge: displayBridge})
            else setSource("")
        }
    }
    IdentityStatus { x: 48; y: 84; z: 100; visible: sessionControl.enabled; control: sessionControl }
    Loader { active: !displayBridge.enabled; sourceComponent: Component { PrivacyShield { control: sessionControl } } }
    Loader { active: !displayBridge.enabled; sourceComponent: Component { SecurePinPrompt { control: sessionControl } } }
    SecurePinOverlay { parent: desktop.contentItem; control: sessionControl; visible: displayBridge.enabled && sessionControl.enabled && Object.keys(sessionControl.challenge).length > 0 }
    Rectangle {
        anchors.fill: parent; z: 100000; color: "#101b27"
        visible: displayBridge.enabled && sessionControl.enabled && sessionControl.shield
        MouseArea { anchors.fill: parent }
        Column {
            anchors.centerIn: parent; spacing: 16
            Label { text: "Personal work is hidden"; color: "white"; font.pixelSize: 28 }
            Button { text: "Return to anonymous"; onClicked: sessionControl.suspend() }
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
        QuietButton { text: ">_"; tip: "Terminal"; onClicked: sessionControl.enabled ? sessionControl.launch("terminal") : backend.terminal() }
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
    Component { id: chatComponent; ChatWindow {} }
    Dialog {
        id: powerDialog; parent: desktop.contentItem; anchors.centerIn: parent; title: "AIOS Power"; modal: true; width: 360; popupType: Popup.Window
        background: Rectangle { color: theme.panel; border.color: theme.line; radius: 12 }
        contentItem: Row { spacing: 12
            QuietButton { text: "Cancel"; onClicked: powerDialog.close() }
            QuietButton { text: "Restart"; onClicked: { powerDialog.close(); backend.power("reboot") } }
            QuietButton { text: "Shut down"; onClicked: { powerDialog.close(); backend.power("poweroff") } }
        }
    }

}
