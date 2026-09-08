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
    function openChat() { var window = chatComponent.createObject(desktop, {backend: backend, session: backend.createSession(), theme: theme}); if (window) { window.show(); window.raise(); window.requestActivate() } }
    property var settingsWindow: null
    function openSettings() {
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
            // Broad translucent ribbons drift at different rates. Integer
            // harmonics make the 48-second cycle seamless.
            function wave(u, layer, edge) {
                var t = desktop.phase
                return height * (0.56 + layer * 0.027
                    + Math.sin(u * 5.0 + t + layer * 1.15) * 0.072
                    + Math.cos(u * 2.7 - t * 2 + layer * 0.8) * 0.037
                    + edge * (0.018 + 0.025 * (0.5 + 0.5 * Math.sin(u * 4.0 + t * 2 + layer))))
            }
            for (var layer = 2; layer >= 0; --layer) {
                ctx.beginPath()
                for (var i = 0; i <= 100; ++i) {
                    var u = i / 100
                    if (i === 0) ctx.moveTo(u * width, wave(u, layer, 0))
                    else ctx.lineTo(u * width, wave(u, layer, 0))
                }
                for (var j = 100; j >= 0; --j) ctx.lineTo(j / 100 * width, wave(j / 100, layer, 1))
                ctx.closePath()
                var wash = ctx.createLinearGradient(0, height * 0.4, 0, height * 0.74)
                wash.addColorStop(0, "#a7cdd2"); wash.addColorStop(1, "#52738e")
                ctx.fillStyle = wash; ctx.globalAlpha = 0.045; ctx.fill()
                for (var ribbon = 0; ribbon < 9; ++ribbon) {
                    ctx.beginPath()
                    for (var x = 0; x <= 100; ++x) {
                        var y = wave(x / 100, layer, ribbon / 8)
                        if (x === 0) ctx.moveTo(0, y)
                        else ctx.lineTo(x / 100 * width, y)
                    }
                    ctx.strokeStyle = theme.accent
                    ctx.globalAlpha = (ribbon === 0 ? 0.15 : 0.025 + (8-ribbon)*0.004) * (1-layer*0.18)
                    ctx.lineWidth = ribbon === 0 ? 1.4 : 0.8; ctx.stroke()
                }
            }
        }
    }
    Text { x: 48; y: 36; text: "aios"; color: theme.ink; opacity: 0.65; font.pixelSize: 22; font.letterSpacing: 4 }
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
