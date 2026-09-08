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
    NumberAnimation on phase { from: 0; to: Math.PI * 2; duration: 26000; loops: Animation.Infinite; running: !desktop.reducedMotion && backend.sessionCount === 0 }
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
            Accessible.name: "Start a new chat"
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
