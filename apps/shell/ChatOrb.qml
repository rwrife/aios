import QtQuick
import QtQuick.Controls

Button {
    id: orb
    required property var theme
    property bool reducedMotion: false
    // Future audio/wake-word integration can drive these without changing the renderer.
    property real activityLevel: 0
    property bool awakened: false
    property string attentionState: "idle"
    property int unreadCount: 0
    property int pulseSerial: 0
    readonly property int attentionPulseCycles: 3
    readonly property int attentionPulseDuration: 6000
    property real phase: 0
    readonly property real energy: Math.max(0, Math.min(1, activityLevel))
    property real brightness: hovered || visualFocus || down || awakened ? 1 : energy
    Behavior on brightness { NumberAnimation { duration: orb.reducedMotion ? 0 : 240 } }
    onBrightnessChanged: surface.requestPaint()
    implicitWidth: 136; implicitHeight: 136
    padding: 0
    Accessible.name: unreadCount > 0 ? "Open scheduled results" : "Start a new chat"
    Accessible.description: unreadCount > 0
        ? unreadCount + (unreadCount === 1 ? " unread scheduled result" : " unread scheduled results")
        : "Open a new conversation"
    ToolTip.visible: hovered || activeFocus
    ToolTip.text: unreadCount > 0 ? "Open scheduled results" : "Start a new chat"
    ToolTip.delay: activeFocus ? 0 : 700
    background: Item {}
    contentItem: Canvas {
        id: surface
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            var c = getContext("2d"); c.reset();
            var t = orb.reducedMotion ? 0 : orb.phase;
            var cx = width/2, cy = height/2;
            var b = orb.brightness;
            function tone(dim, bright) {
                var d = parseInt(dim.slice(1), 16), l = parseInt(bright.slice(1), 16);
                var color = Qt.rgba(((d >> 16) + ((l >> 16) - (d >> 16))*b)/255,
                    (((d >> 8)&255) + (((l >> 8)&255)-((d >> 8)&255))*b)/255,
                    ((d&255) + ((l&255)-(d&255))*b)/255, 1);
                if (orb.theme.paletteIndex > 0) {
                    var lightness = (Math.max(color.r, color.g, color.b) + Math.min(color.r, color.g, color.b)) / 2
                    return Qt.hsla(orb.theme.hue, orb.theme.saturation + b * 0.1, lightness, 1)
                }
                return color;
            }
            var radius = Math.min(width,height) * (orb.down ? 0.285 : 0.31);
            radius *= 1 + 0.035*Math.sin(t) + orb.energy*0.07;
            var halo = c.createRadialGradient(cx,cy,10,cx,cy,width*0.49);
            halo.addColorStop(0, tone("#263c4a", "#6fbbc6"));
            halo.addColorStop(0.55, tone("#1c2e3c", "#315b73"));
            halo.addColorStop(1,"transparent");
            c.fillStyle = halo; c.fillRect(0,0,width,height);
            // Smooth periodic harmonics make a seamless, slowly changing silhouette.
            function shape(scale, offset) {
                c.beginPath();
                for (var i=0; i<=120; i++) {
                    var a=i/120*Math.PI*2;
                    var r=radius*scale*(1+0.085*Math.sin(3*a+t+offset)+0.055*Math.cos(2*a-t*2+offset)+0.025*Math.sin(5*a+t*3));
                    var x=cx+Math.cos(a)*r, y=cy+Math.sin(a)*r;
                    if (i===0) c.moveTo(x,y); else c.lineTo(x,y);
                }
                c.closePath();
            }
            shape(1,0);
            var skin=c.createLinearGradient(cx-radius,cy-radius,cx+radius,cy+radius);
            skin.addColorStop(0,tone("#647b82", "#d4f3ec")); skin.addColorStop(0.33,tone("#4b6570", "#9de8e3"));
            skin.addColorStop(0.7,tone("#3a5267", "#598eae")); skin.addColorStop(1,tone("#4b5069", "#796fa6"));
            c.fillStyle=skin; c.fill();
            c.strokeStyle=tone("#617c87", "#dcfff6"); c.lineWidth=1; c.stroke();
            c.save(); c.clip();
            var glow=c.createRadialGradient(cx-radius*0.35,cy-radius*0.45,0,cx,cy,radius*1.3);
            glow.addColorStop(0,"#ddfff1"); glow.addColorStop(0.35,"#a2ded9"); glow.addColorStop(1,"transparent");
            c.globalAlpha=0.06 + 0.39*b; c.fillStyle=glow; c.fillRect(0,0,width,height);
            shape(0.80,0.9); c.globalAlpha=0.07 + 0.16*b; c.strokeStyle="#e3fff7"; c.lineWidth=1.2; c.stroke();
            shape(0.64,2.1); c.globalAlpha=0.035 + 0.085*b; c.stroke();
            c.restore();
            if (orb.visualFocus) {
                c.beginPath(); c.arc(cx,cy,width*0.46,0,Math.PI*2);
                c.strokeStyle=orb.theme.accent; c.lineWidth=1.5; c.stroke();
            }
            if (orb.attentionState === "unread" || orb.attentionState === "action-needed") {
                c.beginPath(); c.arc(cx,cy,width*0.42,0,Math.PI*2);
                c.strokeStyle=orb.theme.accent; c.globalAlpha=1; c.lineWidth=1.5; c.stroke();
            }
        }
    }
    Rectangle {
        id: pulseContour
        anchors.centerIn: parent
        width: parent.width * 0.84
        height: width
        radius: width / 2
        color: "transparent"
        border.width: 2
        border.color: orb.theme.accent
        opacity: 0
        visible: !orb.reducedMotion
    }
    SequentialAnimation {
        id: attentionPulse
        loops: orb.attentionPulseCycles
        NumberAnimation { target: pulseContour; property: "opacity"; from: 0; to: 0.9; duration: 500 }
        PauseAnimation { duration: 1000 }
        NumberAnimation { target: pulseContour; property: "opacity"; from: 0.9; to: 0; duration: 500 }
    }
    Rectangle {
        visible: orb.unreadCount > 0
        anchors.right: parent.right
        anchors.rightMargin: 14
        anchors.top: parent.top
        anchors.topMargin: 14
        implicitWidth: Math.max(24, countText.implicitWidth + 12)
        implicitHeight: 24
        radius: 12
        color: orb.theme.panel
        border.width: 1
        border.color: orb.theme.accent
        Text {
            id: countText
            anchors.centerIn: parent
            text: orb.unreadCount > 99 ? "99+" : orb.unreadCount
            color: orb.theme.accent
            font.family: "DejaVu Sans"
            font.pixelSize: 12
        }
    }
    Timer {
        interval: 33; repeat: true; running: orb.visible && !orb.reducedMotion
        onTriggered: { orb.phase = (orb.phase + Math.PI*2*0.033/14) % (Math.PI*2); surface.requestPaint() }
    }
    onHoveredChanged: surface.requestPaint()
    onDownChanged: surface.requestPaint()
    onActiveFocusChanged: surface.requestPaint()
    onReducedMotionChanged: surface.requestPaint()
    onActivityLevelChanged: surface.requestPaint()
    onAwakenedChanged: surface.requestPaint()
    onAttentionStateChanged: surface.requestPaint()
    onPulseSerialChanged: {
        attentionPulse.stop()
        pulseContour.opacity = 0
        if (!reducedMotion && unreadCount > 0)
            attentionPulse.start()
    }
    Connections { target: orb.theme; ignoreUnknownSignals: true; function onPaletteIndexChanged() { surface.requestPaint() } }
}
