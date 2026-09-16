import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

Dialog {
    id: faceEnrollment; objectName: "faceEnrollmentDialog"
    component EnrollmentButton: Button {
        padding: 8
        leftPadding: 16; rightPadding: 16
        font.family: "DejaVu Sans"; font.pixelSize: 14
        background: Rectangle {
            color: parent.down ? faceEnrollment.theme.horizon : faceEnrollment.theme.input
            border.width: 1
            border.color: parent.activeFocus ? faceEnrollment.theme.accent : faceEnrollment.theme.line
        }
        contentItem: Text {
            text: parent.text; font: parent.font
            color: parent.enabled ? faceEnrollment.theme.ink : faceEnrollment.theme.muted
            horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        }
    }
    property var control: null
    property var theme: fallbackTheme
    readonly property bool capturing: control && control.recognitionState === "enrolling"
    property real captureProgress: 0
    onCapturingChanged: captureProgress = 0
    Timer {
        id: progressTimer; objectName: "recognitionProgressTimer"
        property double started: 0
        interval: faceEnrollment.theme.reducedMotion ? 2000 : 40
        repeat: true
        running: faceEnrollment.capturing && cameraImage.source.toString().length > 0
        onRunningChanged: if (running) started = Date.now()
        onTriggered: faceEnrollment.captureProgress = Math.min(0.98, (Date.now() - started) / 20000)
    }
    Theme { id: fallbackTheme }
    palette.window: theme.panel
    palette.base: theme.input
    palette.text: theme.ink
    palette.placeholderText: theme.muted
    palette.windowText: theme.ink
    palette.button: theme.input
    palette.buttonText: theme.ink
    palette.highlight: theme.accent
    palette.highlightedText: theme.night
    signal completed(string id)
    readonly property bool available: !!control && !!control.greetingOnly &&
        (control.recognitionState === "ready" || control.recognitionState === "manual-only")
    function openForProfile(id, name) { accountId = id; accountName = name; open() }
    property string accountId: ""
    property string accountName: ""
    parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(440, parent ? parent.width - 32 : 440)
    title: "Set up face recognition"; modal: true
    height: Math.min(implicitHeight, parent ? parent.height - 24 : implicitHeight)
    padding: 16
    font.family: "DejaVu Sans"; font.pixelSize: 14
    background: Rectangle { color: faceEnrollment.theme.panel; border.color: faceEnrollment.theme.line }
    standardButtons: Dialog.NoButton
    footer: DialogButtonBox {
        padding: 12
        background: Rectangle { color: faceEnrollment.theme.panel }
        EnrollmentButton { text: "Cancel"; onClicked: faceEnrollment.reject() }
    }
    closePolicy: faceEnrollment.control && faceEnrollment.control.recognitionState === "enrolling"
        ? Popup.NoAutoClose : Popup.CloseOnEscape
    onOpened: { if (control) control.setSecureInput(true); recognitionPin.forceActiveFocus(); }
    onClosed: {
        recognitionPin.clear(); recognitionConsent.checked = false; captureProgress = 0
        accountId = ""; accountName = ""
        if (faceEnrollment.control) { faceEnrollment.control.setCameraPreviewActive(false); faceEnrollment.control.setSecureInput(false); }
    }
    Connections {
        target: faceEnrollment.control; ignoreUnknownSignals: true
        function onRecognitionEnrollmentCompleted(id) {
            if (!faceEnrollment.opened || id !== faceEnrollment.accountId) return
            faceEnrollment.close()
            faceEnrollment.completed(id)
        }
        function onPrivacyLost() { faceEnrollment.close() }
    }
    contentItem: ScrollView {
        id: enrollmentScroll
        implicitHeight: enrollmentBody.implicitHeight
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            id: enrollmentBody
            width: enrollmentScroll.availableWidth
        Label {
            visible: !faceEnrollment.capturing
            text: "Verify “" + faceEnrollment.accountName + "” with its PIN, then look at the camera. Face matches only suggest this account and never replace the PIN."
            textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true
        }
        Label {
            objectName: "recognitionAvailability"
            visible: !faceEnrollment.available && (!faceEnrollment.control || faceEnrollment.control.recognitionState !== "enrolling")
            text: faceEnrollment.control && faceEnrollment.control.recognitionState === "disabled"
                ? "Enable optional facial recognition in Settings before enrolling this account."
                : "Face recognition is not available on this device yet. You can keep using your PIN and set this up later."
            wrapMode: Text.Wrap; Layout.fillWidth: true
        }
        TextField {
            id: recognitionPin; objectName: "recognitionPin"
            visible: !faceEnrollment.capturing
            Accessible.name: "Account PIN or password"
            placeholderText: "Account PIN or password"; echoMode: TextInput.Password
            maximumLength: 128; Layout.fillWidth: true
            inputMethodHints: Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
        }
        CheckBox {
            id: recognitionConsent; objectName: "recognitionConsent"
            visible: !faceEnrollment.capturing
            onCheckedChanged: if (faceEnrollment.control) faceEnrollment.control.setCameraPreviewActive(checked && faceEnrollment.available)
            text: "Store encrypted face templates locally for account suggestions"
            Layout.fillWidth: true; Layout.minimumWidth: 0
            contentItem: Text {
                text: recognitionConsent.text; font: recognitionConsent.font
                color: faceEnrollment.theme.ink; wrapMode: Text.Wrap
                leftPadding: recognitionConsent.indicator.width + recognitionConsent.spacing
                verticalAlignment: Text.AlignVCenter
            }
        }
        Item {
            visible: cameraImage.source.toString().length > 0
            Layout.fillWidth: true; Layout.preferredHeight: faceEnrollment.capturing ? 260 : 180
            Image {
                id: cameraImage; objectName: "recognitionPreview"
                anchors.fill: parent
                source: faceEnrollment.control && faceEnrollment.control.cameraPreview ? faceEnrollment.control.cameraPreview : ""
                cache: false; fillMode: Image.PreserveAspectFit
                mirror: true
                Accessible.name: "Camera preview"
            }
            Canvas {
                id: captureRing; objectName: "recognitionCaptureRing"
                anchors.fill: parent
                property real progress: faceEnrollment.capturing ? faceEnrollment.captureProgress : 0
                onProgressChanged: requestPaint()
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()
                onPaint: {
                    const ctx = getContext("2d")
                    ctx.reset()
                    const rx = Math.min(width * 0.24, height * 0.32)
                    const ry = height * 0.40
                    function arc(color, end, thickness) {
                        ctx.save(); ctx.translate(width / 2, height / 2); ctx.scale(rx, ry)
                        ctx.beginPath(); ctx.arc(0, 0, 1, -Math.PI / 2, end, false)
                        ctx.restore(); ctx.strokeStyle = color; ctx.lineWidth = thickness; ctx.stroke()
                    }
                    arc(faceEnrollment.theme.line, 1.5 * Math.PI, 1)
                    if (progress > 0) arc(faceEnrollment.theme.success, -Math.PI / 2 + progress * 2 * Math.PI, 3)
                }
            }
        }
        EnrollmentButton {
            objectName: "confirmRecognitionEnrollment"; text: "Next"
            visible: !faceEnrollment.capturing
            enabled: faceEnrollment.control && faceEnrollment.available &&
                     recognitionPin.text.length > 0 && recognitionConsent.checked && cameraImage.source.toString().length > 0
            onClicked: {
                faceEnrollment.control.setCameraPreviewActive(false)
                faceEnrollment.control.enrollRecognition(faceEnrollment.accountId,
                    recognitionPin.text, recognitionConsent.checked)
                recognitionPin.clear()
            }
        }
        Label {
            objectName: "recognitionGuidance"
            visible: faceEnrollment.control && faceEnrollment.control.recognitionState === "enrolling"
            text: "Look straight ahead while the green line fills. Capturing your photos…"
            wrapMode: Text.Wrap; Layout.fillWidth: true
        }
        Label { text: faceEnrollment.control ? faceEnrollment.control.error : ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; Layout.fillWidth: true }
    }
    }
    Connections {
        target: faceEnrollment.contentItem.Window.window
        function onActiveChanged() { if (!faceEnrollment.contentItem.Window.window.active) recognitionPin.clear(); }
    }
}
