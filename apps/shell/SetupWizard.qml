import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import QtMultimedia

Window {
    id: wizard
    required property var backend
    required property var theme
    required property var profileControl
    title: "Welcome to AIOS"
    width: Math.min(720, Screen.width - 32)
    height: Math.min(640, Screen.height - 48)
    minimumWidth: 440; minimumHeight: 360
    x: (Screen.width - width) / 2; y: (Screen.height - height) / 2
    color: theme.panel
    property int step: 0
    property bool ownsOperation: false
    readonly property var steps: ["Welcome", "Internet", "Account", "Camera", "Models", "Ready"]
    function cancelOperation() {
        if (ownsOperation && backend.busy) backend.stop()
        ownsOperation = false
    }
    function previewFormat(device) {
        const formats = device.videoFormats || []
        for (let i = 0; i < formats.length; ++i) {
            const format = formats[i]
            if (format.resolution.width === 640 && format.resolution.height === 480 &&
                    format.pixelFormat === VideoFrameFormat.Format_YUYV)
                return format
        }
        for (let i = 0; i < formats.length; ++i) {
            const format = formats[i]
            if (format.resolution.width === 640 && format.resolution.height === 480)
                return format
        }
        return formats.length ? formats[0] : undefined
    }
    function configureCamera(device) {
        camera.cameraDevice = device
        const format = previewFormat(device)
        if (format) camera.cameraFormat = format
    }
    function moveTo(value) {
        camera.stop()
        cancelOperation()
        step = value
    }
    onVisibleChanged: {
        if (visible) step = 0
        else { camera.stop(); cancelOperation() }
    }
    onClosing: { camera.stop(); cancelOperation(); backend.dismissSetup() }
    Connections {
        target: backend
        function onChanged() { if (!backend.busy) wizard.ownsOperation = false }
    }
    Shortcut { sequence: "Escape"; onActivated: wizard.close() }
    component Note: Text {
        Layout.fillWidth: true; wrapMode: Text.Wrap
        color: theme.muted; font.pixelSize: 15
    }
    component Action: Button {
        id: button
        padding: 12
        Accessible.name: text
        contentItem: Text { text: button.text; color: button.enabled ? theme.ink : theme.muted; font.pixelSize: 14; horizontalAlignment: Text.AlignHCenter }
        background: Rectangle { radius: 7; color: button.hovered ? theme.horizon : theme.input; border.color: button.activeFocus ? theme.accent : theme.line }
    }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 24; spacing: 16
        RowLayout {
            Layout.fillWidth: true
            Text { text: wizard.steps[wizard.step]; color: theme.ink; font.pixelSize: 26; Layout.fillWidth: true }
            Action { objectName: "closeSetup"; text: "Close setup"; onClicked: wizard.close() }
        }
        Note { text: "Step " + (wizard.step + 1) + " of " + wizard.steps.length + " · Every step is optional"; font.pixelSize: 12 }
        ScrollView {
            Layout.fillWidth: true; Layout.fillHeight: true
            contentWidth: availableWidth; clip: true
            ColumnLayout {
                width: parent.width; spacing: 16
                ColumnLayout {
                    visible: wizard.step === 0; Layout.fillWidth: true; spacing: 16
                    Note { text: "Make AIOS yours"; color: theme.ink; font.pixelSize: 22 }
                    Note { text: "Connect to the internet, connect an account, check your camera, and prepare local AI models. You can skip any step or close setup at any time." }
                    Note { text: "The bundled Qwen3 starter works offline. No account or camera is required to use the desktop." }
                    Note { text: "Return any time from Settings → Run setup wizard. Completed changes are kept; leaving a step cancels its unfinished sign-in or download." }
                    Note { visible: backend.config.live === true; text: "You are in a live session. Setup choices are temporary and will be lost after reboot." }
                }
                ColumnLayout {
                    visible: wizard.step === 1; Layout.fillWidth: true; spacing: 16
                    Note { text: "Connect with Wi-Fi or Ethernet to sign in and download models. You can continue offline." }
                    Action { objectName: "setupNetwork"; text: "Open network setup"; onClicked: backend.openSystemSettings("network") }
                    Note { text: "Choose Activate a connection, then select your network. Use the arrow keys and Enter. Close the network window to return here." }
                    Note { text: "In a virtual machine, the connection usually appears as wired Ethernet, even if your host uses Wi-Fi."; font.pixelSize: 12 }
                }
                ColumnLayout {
                    visible: wizard.step === 2; Layout.fillWidth: true; spacing: 12
                    Note { text: "Set up an account"; color: theme.ink; font.pixelSize: 20 }
                    Note { text: "Create an optional local profile to save your name, PIN, and profile photo on this computer." }
                    Action {
                        objectName: "setupLocalAccount"; text: "Create local profile"
                        enabled: profileControl && profileControl.personalAvailable && !profileControl.busy
                        onClicked: { enrollment.creating = true; enrollment.open() }
                    }
                    Note { visible: !profileControl || !profileControl.personalAvailable; text: "Local profile creation is unavailable on this system."; font.pixelSize: 12 }
                    Note { text: "A camera can add a profile photo when available. Optional face suggestions are configured separately in Settings after creating a profile; they never replace the account PIN."; font.pixelSize: 12 }
                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: theme.line }
                    Note { text: "Connect a ChatGPT account"; color: theme.ink; font.pixelSize: 20 }
                    Note { text: "Sign in in your browser, or follow the provider's account creation flow there. Model access depends on your plan. This service account is separate from the local profile above." }
                    Note { text: backend.subscription.signed_in ? "Connected: " + (backend.subscription.email || "ChatGPT account") : "An account is optional. Local chat works without one." }
                    Action {
                        objectName: "setupSignIn"; text: "Sign in / set up account"; enabled: !backend.busy && !backend.configuring
                        onClicked: { backend.subscriptionAction("login", true); wizard.ownsOperation = true }
                    }
                    Note { text: backend.loginUrl; visible: text.length > 0; wrapMode: Text.WrapAnywhere }
                    Note { text: backend.loginCode; visible: text.length > 0; color: theme.ink; font.pixelSize: 24 }
                    Action { text: "Open sign-in in browser"; visible: backend.loginUrl.length > 0; onClicked: backend.openSubscriptionLogin() }
                    Action { text: "Copy sign-in address"; visible: backend.loginUrl.length > 0; onClicked: backend.copy(backend.loginUrl) }
                    Action { text: "Copy sign-in code"; visible: backend.loginCode.length > 0; onClicked: backend.copy(backend.loginCode) }
                    Note { text: "After connecting, choose ChatGPT subscription in Settings → AI models to use it for chat."; font.pixelSize: 12 }
                }
                ColumnLayout {
                    visible: wizard.step === 3; Layout.fillWidth: true; spacing: 12
                    Note { text: devices.videoInputs.length ? "Camera detected. You can check its picture below." : "No camera detected. You can skip this step or connect a camera." }
                    Note { text: "Biometric sign-in is unavailable in this build. A camera alone does not enable face login; recognition requires a supported enrollment service and calibrated models." }
                    ComboBox {
                        Layout.fillWidth: true; model: devices.videoInputs; textRole: "description"
                        enabled: devices.videoInputs.length > 0
                        onActivated: { camera.stop(); wizard.configureCamera(devices.videoInputs[currentIndex]) }
                    }
                    Rectangle {
                        Layout.fillWidth: true; implicitHeight: 160; color: theme.night; radius: 8
                        VideoOutput { id: preview; anchors.fill: parent; fillMode: VideoOutput.PreserveAspectFit }
                        Text { anchors.centerIn: parent; visible: !camera.active; text: "Camera off"; color: theme.muted }
                    }
                    Action {
                        objectName: "setupCamera"
                        text: camera.active ? "Stop camera check" : "Start camera check"
                        enabled: devices.videoInputs.length > 0
                        onClicked: {
                            if (camera.active) camera.stop()
                            else {
                                wizard.configureCamera(camera.cameraDevice)
                                camera.start()
                            }
                        }
                    }
                    Note { text: camera.errorString; visible: camera.error !== Camera.NoError }
                    Note { text: "The camera starts only when you ask. Nothing is saved or uploaded. Leaving this step turns it off."; font.pixelSize: 12 }
                }
                ColumnLayout {
                    visible: wizard.step === 4; Layout.fillWidth: true; spacing: 16
                    Note { text: "Local models keep chat and speech on this computer. Downloads need internet access and free disk space." }
                    Note { text: backend.config.model_path ? "A local chat model is configured. You can keep it and continue." : "Prepare the Qwen3 starter to chat offline." }
                    LocalModels {
                        Layout.fillWidth: true; backend: wizard.backend; theme: wizard.theme
                        buttonName: "setupModel"
                        onInstallStarted: wizard.ownsOperation = true
                    }
                    Action {
                        objectName: "setupVoice"; text: "Download local speech model · 75 MiB"; enabled: !backend.busy && !backend.configuring
                        onClicked: { backend.setupVoice(); wizard.ownsOperation = true }
                    }
                    Note { text: backend.config.speech_model_path ? "A local speech model is configured." : "Speech is optional. You can type instead."; font.pixelSize: 12 }
                    Note { text: "Choose a stronger curated Qwen3 model, import another GGUF, or configure a remote provider later in Settings → AI models." }
                }
                ColumnLayout {
                    visible: wizard.step === 5; Layout.fillWidth: true; spacing: 16
                    Note { text: "You're ready to explore"; color: theme.ink; font.pixelSize: 22 }
                    Note { text: "Open the glowing chat button at the bottom of the desktop to start a conversation. Settings holds your model, sound, network, display, and appearance choices." }
                    Note { text: "Skipped steps are fine. Run this wizard again whenever you're ready." }
                }
            }
        }
        Note { objectName: "setupStatus"; text: backend.status; visible: wizard.step > 0 && text.length > 0; font.pixelSize: 12 }
        Action { text: "Cancel current action"; visible: wizard.ownsOperation && backend.busy; onClicked: wizard.cancelOperation() }
        RowLayout {
            Layout.fillWidth: true
            Action { objectName: "setupBack"; text: "Back"; enabled: wizard.step > 0; onClicked: wizard.moveTo(wizard.step - 1) }
            Item { Layout.fillWidth: true }
            Action { objectName: "setupSkip"; text: "Skip"; visible: wizard.step > 0 && wizard.step < 5; onClicked: wizard.moveTo(wizard.step + 1) }
            Action { objectName: "setupNext"; text: wizard.step === 5 ? "Finish" : "Continue"; onClicked: wizard.step === 5 ? wizard.close() : wizard.moveTo(wizard.step + 1) }
        }
    }
    MediaDevices { id: devices }
    Camera {
        id: camera
        cameraDevice: devices.defaultVideoInput
    }
    CaptureSession { camera: camera; videoOutput: preview }
    EnrollmentFlow { id: enrollment; parent: Overlay.overlay; anchors.centerIn: parent; control: wizard.profileControl }
}
