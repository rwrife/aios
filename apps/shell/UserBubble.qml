import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQml.Models

Item {
    id: root
    property var control: null
    property color ink: "#e4edf1"
    property color surface: "#263944"
    readonly property var profile: control && control.profile ? control.profile : ({})
    readonly property string name: profile.name || ""
    readonly property string greeting: name ? "Hello, " + name + ". How may I help you?" : "Welcome. Set up an account with a PIN to make this space yours."
    implicitWidth: 52; implicitHeight: 52
    function createAccount() { enrollment.creating = true; enrollment.open(); }
    function openPicker() {
        if (control && control.personalAvailable) control.listProfiles()
        picker.open()
    }
    Button {
        id: bubble; objectName: "userBubble"; anchors.fill: parent
        Accessible.name: root.name ? "User profile: " + root.name : "Choose or create a user profile"
        onClicked: root.openPicker()
        ToolTip.visible: hovered || activeFocus
        ToolTip.text: root.name || "Your profile"
        background: Rectangle { radius: width / 2; color: root.surface; border.color: bubble.activeFocus ? root.ink : "#58717e" }
        contentItem: Canvas {
            id: portrait; property string photo: root.profile.photo || ""
            property string previousPhoto: ""
            onPhotoChanged: { if (previousPhoto) unloadImage(previousPhoto); previousPhoto = photo; if (photo) loadImage(photo); requestPaint(); }
            onImageLoaded: requestPaint()
            onPaint: {
                var c = getContext("2d"); c.reset()
                c.save(); c.beginPath(); c.arc(width/2, height/2, Math.min(width,height)/2, 0, 2*Math.PI); c.clip()
                if (photo && isImageLoaded(photo)) c.drawImage(photo, 0, 0, width, height)
                else {
                    c.fillStyle = root.ink
                    c.beginPath(); c.arc(width/2, height*0.34, width*0.15, 0, 2*Math.PI); c.fill()
                    c.beginPath(); c.ellipse(width*0.22, height*0.56, width*0.56, height*0.46); c.fill()
                }
                c.restore()
            }
        }
    }
    QtObject { id: unavailable; property bool busy: false; property string error: ""; function setSecureInput(active) {} }
    EnrollmentFlow { id: enrollment; objectName: "bubbleEnrollment"; parent: Overlay.overlay; anchors.centerIn: parent; control: root.control || unavailable }
    Menu {
        id: picker; objectName: "profilePicker"
        parent: root; x: (root.width - width) / 2; y: root.height + 8
        width: 260; height: Math.min(implicitHeight, 320)
        popupType: Popup.Item
        palette.window: root.surface
        palette.base: root.surface
        palette.text: root.ink
        palette.windowText: root.ink
        palette.buttonText: root.ink
        palette.highlight: "#405968"
        palette.highlightedText: root.ink
        background: Rectangle { color: root.surface; border.color: "#58717e"; radius: 8 }
        onAboutToHide: if (root.control) root.control.setSecureInput(false)
        onOpened: { currentIndex = -1; if (root.control) root.control.setSecureInput(true); }
        Instantiator {
            model: root.control && root.control.profiles ? root.control.profiles : []
            delegate: MenuItem {
                required property var modelData
                objectName: "chooseProfile"; text: modelData.name
                background: Rectangle { color: parent.highlighted ? "#405968" : "transparent"; radius: 4 }
                contentItem: Text { text: modelData.name; textFormat: Text.PlainText; elide: Text.ElideRight; color: root.ink; verticalAlignment: Text.AlignVCenter }
                onTriggered: {
                    picker.close()
                    enrollment.creating = false
                    enrollment.selectedProfile = modelData.name; enrollment.open()
                }
            }
            onObjectAdded: (index, object) => picker.insertItem(index, object)
            onObjectRemoved: (index, object) => picker.removeItem(object)
        }
        MenuItem {
            enabled: false
            visible: !root.control || !root.control.profiles || root.control.profiles.length === 0
            height: visible ? implicitHeight : 0
            text: root.control && root.control.busy ? "Loading accounts…" : "No saved accounts"
        }
        MenuSeparator { contentItem: Rectangle { implicitWidth: 240; implicitHeight: 1; color: "#58717e" } }
        MenuItem {
            objectName: "createUser"; text: "New account"
            enabled: root.control && root.control.personalAvailable
            onTriggered: { picker.close(); root.createAccount(); }
        }
    }
    Connections {
        target: root.control
        function onPrivacyLost() { picker.close() }
    }
}
