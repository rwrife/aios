import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    property var control: null
    property color ink: "#e4edf1"
    property color surface: "#263944"
    readonly property var profile: control && control.profile ? control.profile : ({})
    readonly property string name: profile.name || ""
    readonly property string greeting: name ? "Hello, " + name + ". How may I help you?" : "Welcome. Set up an account with a PIN to make this space yours."
    implicitWidth: 52; implicitHeight: 52
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
    Popup {
        id: picker; objectName: "profilePicker"
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(380, parent ? parent.width - 32 : 380)
        modal: true; padding: 20
        onAboutToHide: if (root.control) root.control.setSecureInput(false)
        onOpened: if (root.control) root.control.setSecureInput(true)
        contentItem: ColumnLayout {
            spacing: 12
            Label { text: "Who’s here?"; font.pixelSize: 22 }
            Label { text: "Choose your profile, then enter your PIN or password."; wrapMode: Text.Wrap; Layout.fillWidth: true }
            ScrollView {
                Layout.fillWidth: true; Layout.preferredHeight: Math.min(220, choices.implicitHeight)
                Layout.minimumHeight: Layout.preferredHeight
                clip: true
                Column {
                    id: choices; width: parent.width; spacing: 4
                    Repeater {
                        model: root.control && root.control.profiles ? root.control.profiles : []
                        Button {
                            required property var modelData
                            objectName: "chooseProfile"; width: choices.width; implicitHeight: 40
                            contentItem: Text { text: modelData.name; textFormat: Text.PlainText; elide: Text.ElideRight; color: root.ink }
                            onClicked: {
                                picker.close(); enrollment.creating = false
                                enrollment.selectedProfile = modelData.name; enrollment.open()
                            }
                        }
                    }
                }
            }
            Button {
                objectName: "createUser"; text: "Set up a new account"; Layout.fillWidth: true
                enabled: root.control && root.control.personalAvailable
                onClicked: { picker.close(); enrollment.creating = true; enrollment.open(); }
            }
            Label {
                visible: !root.control || !root.control.personalAvailable
                text: "Account access is available in the protected desktop. You can keep chatting as a guest here."
                wrapMode: Text.Wrap; Layout.fillWidth: true
            }
            Button { text: "Continue as guest"; onClicked: picker.close() }
        }
    }
    Connections {
        target: root.control
        function onPrivacyLost() { picker.close() }
    }
}
