import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import QtMultimedia
import "WindowSizing.js" as WindowSizing

Window {
    id: settings
    required property var backend
    required property var theme
    property var profileControl: null
    property string recognitionNotice: ""
    property var selectedCameraDevice: devices.defaultVideoInput
    readonly property var activeCamera: cameraLoader.item
    signal setupRequested()
    title: "AIOS Settings"
    flags: Qt.Window | Qt.FramelessWindowHint
    width: WindowSizing.extent(820, Screen.width, Screen.desktopAvailableWidth)
    height: WindowSizing.extent(620, Screen.height, Screen.desktopAvailableHeight)
    minimumWidth: WindowSizing.extent(540, Screen.width, Screen.desktopAvailableWidth)
    minimumHeight: WindowSizing.extent(400, Screen.height, Screen.desktopAvailableHeight)
    x: Screen.virtualX + (Screen.width-width)/2; y: Screen.virtualY + (Screen.height-height)/2
    color: "transparent"
    function stopCameraPreview() {
        if (activeCamera) activeCamera.stop()
        cameraSession.camera = null
        cameraLoader.active = false
        if (profileControl && typeof profileControl.setCameraPreviewActive === "function")
            profileControl.setCameraPreviewActive(false)
    }
    function previewFormat(device) {
        const formats = device.videoFormats || []
        for (let i = formats.length - 1; i >= 0; --i) {
            const format = formats[i]
            if (format.resolution.width === 640 && format.resolution.height === 360)
                return format
        }
        return formats.length ? formats[0] : undefined
    }
    function configureCamera(device) {
        selectedCameraDevice = device
        if (activeCamera) {
            activeCamera.cameraDevice = device
            const format = previewFormat(device)
            if (format) activeCamera.cameraFormat = format
        }
    }
    function recognitionStatusText() {
        if (backend.config.camera_recognition !== true)
            return "Off. AIOS will not use the camera for account suggestions. Enrolled face data is kept until you purge it."
        if (!profileControl)
            return "On, but facial recognition is unavailable. Account sign-in continues to use the PIN."
        if (profileControl.recognitionState === "manual-only")
            return "On, but no face data is enrolled. Add face recognition from Accounts."
        if (profileControl.recognitionState === "unavailable")
            return "On, but the camera or recognition model is unavailable. Account sign-in continues to use the PIN."
        if (profileControl.recognitionState === "enrolling")
            return "On. Facial recognition enrollment is in progress."
        if (profileControl.recognitionState === "ready")
            return "On. AIOS may occasionally use the camera to suggest an enrolled account. A PIN is still required."
        return "On. Facial recognition is starting. A PIN is still required."
    }
    onVisibleChanged: { if (!visible) stopCameraPreview(); else { models.reload(); if (pages.currentIndex === 7) accountsPage.refresh(); } }
    onClosing: stopCameraPreview()
    Rectangle {
        objectName: "settingsWindowSurface"
        anchors.fill: parent
        color: theme.panel
        radius: theme.windowRadius
        border.color: theme.line
    }
    // Add a section here and its page to the StackLayout below.
    readonly property var sections: ["AI models", "Sound", "Camera", "Network & Wi-Fi", "Display", "Appearance", "About", "Accounts", "Date & Time"]
    function openSection(section) {
        if (section === "date_time") {
            stopCameraPreview()
            pages.currentIndex = 8
        }
    }
    component Action: Button {
        id: control
        hoverEnabled: true
        implicitHeight: 44
        padding: 12
        contentItem: Text {
            text: control.text
            color: control.enabled ? theme.ink : theme.muted
            font.pixelSize: 14
            font.weight: Font.Medium
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            color: control.hovered || control.down ? theme.horizon : theme.input
            radius: 8
            border.width: control.activeFocus ? 2 : 1
            border.color: control.activeFocus ? theme.accent : theme.line
        }
    }
    component Note: Text {
        color: theme.muted; font.pixelSize: 14; wrapMode: Text.Wrap; textFormat: Text.PlainText
        Layout.fillWidth: true
    }
    component InfoRow: RowLayout {
        id: infoRow
        required property string label
        required property string value
        required property string fieldName
        Layout.fillWidth: true; spacing: 20
        Text {
            text: infoRow.label; color: theme.muted; font.pixelSize: 13
            Layout.preferredWidth: 110
        }
        Text {
            objectName: infoRow.fieldName
            text: infoRow.value; color: theme.ink; font.pixelSize: 13
            wrapMode: Text.Wrap; Layout.fillWidth: true
        }
    }
    RowLayout {
        anchors.fill: parent; anchors.margins: 24; spacing: 24
        ColumnLayout {
            Layout.preferredWidth: 170; Layout.minimumWidth: Math.max(170, setupAction.implicitWidth); Layout.fillHeight: true; spacing: 6
            WindowTitle {
                objectName: "settingsWindowTitle"
                theme: settings.theme
                text: "Settings"
                Layout.preferredHeight: 44
                Layout.fillWidth: true
                Layout.bottomMargin: 16
            }
            ScrollView {
                id: sectionScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth
                ColumnLayout {
                    width: sectionScroll.availableWidth
                    spacing: 4
                    Repeater {
                        objectName: "settingsSections"
                        model: settings.sections
                        Button {
                            id: sectionButton
                            required property string modelData; required property int index
                            Layout.fillWidth: true; padding: 8
                            Accessible.name: modelData; Accessible.role: Accessible.PageTab; Accessible.checked: pages.currentIndex === index
                            contentItem: Text { text: modelData; color: pages.currentIndex === index ? theme.ink : theme.muted; font.pixelSize: 14 }
                            background: Rectangle { radius: 6; color: pages.currentIndex === index || sectionButton.hovered ? theme.input : "transparent"; border.width: sectionButton.activeFocus ? 1 : 0; border.color: theme.accent }
                            onClicked: { settings.stopCameraPreview(); pages.currentIndex = index }
                        }
                    }
                }
            }
            Action {
                id: setupAction
                objectName: "launchSetup"
                text: "Run setup wizard"
                Accessible.name: "Run setup wizard"
                Layout.fillWidth: true
                onClicked: { settings.close(); settings.setupRequested() }
            }
            Note { text: backend.config.live ? "Live session\nChanges are lost after reboot." : "This computer"; font.pixelSize: 11 }
        }
        Rectangle { Layout.fillHeight: true; implicitWidth: 1; color: theme.line; opacity: 0.5 }
        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 20
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                Layout.minimumHeight: 44
                Layout.maximumHeight: 44
                Item {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    WindowTitle { anchors.fill: parent; theme: settings.theme; text: settings.sections[pages.currentIndex] }
                    MouseArea { anchors.fill: parent; onPressed: settings.startSystemMove() }
                }
                WindowControlButton {
                    objectName: "settingsCloseButton"
                    theme: settings.theme
                    symbol: "\u00d7"
                    tip: "Close settings"
                    onClicked: settings.close()
                }
            }
            StackLayout {
                id: pages; objectName: "settingsPages"; Layout.fillWidth: true; Layout.fillHeight: true
                ModelSettings { id: models; showClose: false; backend: settings.backend; theme: settings.theme; onCloseRequested: settings.close() }
                ColumnLayout {
                    spacing: 16
                    Note { text: "Choose your speakers and microphone, adjust volume, mute devices, and manage audio used by individual applications." }
                    Action { text: "Open sound controls"; onClicked: backend.openSystemSettings("sound") }
                    Note { text: "Select the fallback input and output devices in sound controls to use them for new voice recordings and spoken replies."; font.pixelSize: 12 }
                    Item { Layout.fillHeight: true }
                }
                ScrollView {
                    id: cameraPage
                    objectName: "cameraSettingsScroll"
                    clip: true
                    contentWidth: availableWidth
                    ColumnLayout {
                        width: cameraPage.availableWidth
                        spacing: 12
                        Note { text: devices.videoInputs.length ? "Choose a webcam and preview its picture. The camera turns off when you leave this section or close Settings." : "No webcam detected. Connect a camera to preview it here." }
                        ComboBox {
                            id: cameraChoice; Layout.fillWidth: true; model: devices.videoInputs; textRole: "description"
                            enabled: devices.videoInputs.length > 0
                            onActivated: { settings.stopCameraPreview(); settings.configureCamera(devices.videoInputs[currentIndex]) }
                        }
                        Rectangle {
                            objectName: "cameraPreview"
                            Layout.preferredWidth: parent.width * 0.75
                            Layout.preferredHeight: width * 9 / 16
                            Layout.minimumHeight: 100
                            Layout.alignment: Qt.AlignHCenter
                            color: theme.night; radius: 8
                            VideoOutput { id: viewfinder; anchors.fill: parent; fillMode: VideoOutput.PreserveAspectFit }
                            Text { anchors.centerIn: parent; visible: !settings.activeCamera || !settings.activeCamera.active; text: "Camera off"; color: theme.muted }
                        }
                        Note {
                            text: settings.activeCamera ? settings.activeCamera.errorString : ""
                            visible: settings.activeCamera && settings.activeCamera.error !== Camera.NoError
                            font.pixelSize: 12
                        }
                        Action {
                            text: settings.activeCamera && settings.activeCamera.active ? "Stop preview" : "Start preview"
                            enabled: devices.videoInputs.length > 0
                            onClicked: {
                                if (settings.activeCamera && settings.activeCamera.active) settings.stopCameraPreview()
                                else {
                                    if (settings.profileControl &&
                                            typeof settings.profileControl.setCameraPreviewActive === "function" &&
                                            settings.profileControl.setCameraPreviewActive(true) === false)
                                        return
                                    cameraLoader.active = true
                                    settings.configureCamera(settings.selectedCameraDevice)
                                    cameraSession.camera = settings.activeCamera
                                    settings.activeCamera.start()
                                }
                            }
                        }
                        Note { text: "Preview stays on this computer. Camera attachments and video calls are not enabled."; font.pixelSize: 12 }
                        Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: theme.line; opacity: 0.5 }
                        Text { text: "Facial recognition"; color: theme.ink; font.pixelSize: 17 }
                        Note {
                            objectName: "recognitionStatus"
                            text: settings.recognitionStatusText()
                            font.pixelSize: 12
                        }
                        Note { text: "Recognition camera"; color: theme.ink; font.pixelSize: 12 }
                        TextField {
                            id: recognitionDevice; objectName: "recognitionDevice"
                            Layout.fillWidth: true
                            placeholderText: "/dev/v4l/by-id/...-video-index0"
                            text: backend.config.camera_device ||
                                (typeof backend.defaultRecognitionCamera === "function"
                                    ? backend.defaultRecognitionCamera() : "")
                            enabled: !backend.configuring
                            maximumLength: 512
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Action {
                                objectName: "recognitionToggle"
                                text: backend.config.camera_recognition === true
                                    ? "Disable facial recognition" : "Enable facial recognition"
                                enabled: !backend.configuring
                                onClicked: {
                                    const enabling = backend.config.camera_recognition !== true
                                    const device = recognitionDevice.text.trim()
                                    if (enabling && device.length === 0) {
                                        settings.recognitionNotice =
                                            "No stable local camera path was found. Reconnect the camera and try again."
                                        return
                                    }
                                    settings.recognitionNotice = ""
                                    if (!enabling && settings.profileControl &&
                                            typeof settings.profileControl.setRecognitionEnabled === "function")
                                        settings.profileControl.setRecognitionEnabled(false)
                                    backend.configure({
                                        camera_recognition: enabling,
                                        camera_device: enabling ? device
                                            : (backend.config.camera_device || "")
                                    })
                                }
                            }
                            Action {
                                objectName: "saveRecognitionCamera"
                                text: "Save camera"
                                visible: backend.config.camera_recognition === true &&
                                    recognitionDevice.text.trim() !== (backend.config.camera_device || "")
                                enabled: !backend.configuring && recognitionDevice.text.trim().length > 0
                                onClicked: backend.configure({camera_device: recognitionDevice.text.trim()})
                            }
                        }
                        Note {
                            objectName: "recognitionNotice"
                            text: settings.recognitionNotice
                            visible: text.length > 0
                            font.pixelSize: 12
                        }
                        Action {
                            objectName: "purgeRecognition"
                            text: "Purge facial recognition data\u2026"
                            enabled: settings.profileControl && !backend.configuring
                            onClicked: purgeRecognitionDialog.open()
                        }
                        Note {
                            text: "Purging permanently deletes every enrolled face template. It does not delete accounts, profile photos, or PINs."
                            font.pixelSize: 12
                        }
                    }
                }
                ColumnLayout {
                    spacing: 16
                    Note { text: "Connect to Wi-Fi, select a wired connection, or edit saved network details." }
                    Action { text: "Open network setup"; onClicked: backend.openSystemSettings("network") }
                    Note { text: "Use the arrow keys and Enter in network setup. Choose Activate a connection to join Wi-Fi, or Edit a connection for addresses and DNS."; font.pixelSize: 12 }
                    Item { Layout.fillHeight: true }
                }
                ColumnLayout {
                    spacing: 16
                    Note { text: "Adjust the resolution, orientation and arrangement of connected displays." }
                    Action { text: "Open display controls"; onClicked: backend.openSystemSettings("display") }
                    Note { text: "Apply a layout to use it now. To restore it at login, save it as ~/.screenlayout/default.sh in display controls."; font.pixelSize: 12 }
                    Item { Layout.fillHeight: true }
                }
                ColumnLayout {
                    spacing: 16
                    Note { text: "Theme color" }
                    GridLayout {
                        columns: 4; columnSpacing: 12; rowSpacing: 12; uniformCellWidths: true
                        Layout.fillWidth: true
                        Repeater {
                            objectName: "themeChoices"
                            model: theme.choices
                            Button {
                                id: swatch
                                required property var modelData
                                objectName: "theme-" + modelData.key
                                Layout.fillWidth: true; implicitWidth: 96; implicitHeight: 72
                                checkable: true
                                checked: (backend.config.theme_color || "blue") === modelData.key
                                enabled: !backend.busy && backend.configuring !== true
                                Accessible.name: modelData.name + " theme"
                                Accessible.role: Accessible.RadioButton
                                Accessible.checked: checked
                                onClicked: backend.configure({theme_color: modelData.key})
                                background: Rectangle {
                                    radius: 8; color: swatch.hovered ? theme.input : "transparent"
                                    border.width: swatch.checked || swatch.activeFocus ? 1 : 0
                                    border.color: theme.accent
                                }
                                contentItem: Column {
                                    spacing: 8
                                    Rectangle {
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        width: 26; height: 26; radius: 13; color: swatch.modelData.swatch
                                        Text { anchors.centerIn: parent; text: swatch.checked ? "✓" : ""; color: "#15202b"; font.pixelSize: 17 }
                                    }
                                    Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; text: swatch.modelData.name; color: theme.ink; font.pixelSize: 12 }
                                }
                            }
                        }
                    }
                    Note { text: "Background animation" }
                    Action {
                        objectName: "motionToggle"
                        text: backend.config.reduced_motion ? "Enable motion" : "Reduce motion"
                        onClicked: backend.configure({reduced_motion: !backend.config.reduced_motion})
                    }
                    Item { Layout.fillHeight: true }
                }
                ColumnLayout {
                    spacing: 16
                    Note { text: "Build and system information for this computer." }
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: aboutDetails.implicitHeight + 32
                        color: theme.input; radius: 8
                        ColumnLayout {
                            id: aboutDetails
                            anchors.fill: parent; anchors.margins: 16; spacing: 12
                            InfoRow { label: "AIOS version"; value: backend.systemInfo.version; fieldName: "aboutVersion" }
                            InfoRow { label: "Build"; value: backend.systemInfo.build; fieldName: "aboutBuild" }
                            InfoRow { label: "Commit"; value: backend.systemInfo.commit; fieldName: "aboutCommit" }
                            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: theme.line }
                            InfoRow { label: "Operating system"; value: backend.systemInfo.os; fieldName: "aboutOs" }
                            InfoRow { label: "Kernel"; value: backend.systemInfo.kernel; fieldName: "aboutKernel" }
                            InfoRow { label: "Architecture"; value: backend.systemInfo.architecture; fieldName: "aboutArchitecture" }
                            InfoRow { label: "CPU"; value: backend.systemInfo.cpu; fieldName: "aboutCpu" }
                            InfoRow { label: "Memory"; value: backend.systemInfo.memory; fieldName: "aboutMemory" }
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
                AccountSettings { id: accountsPage; control: settings.profileControl }
                DateTimeSettings { backend: settings.backend; theme: settings.theme; active: settings.visible && pages.currentIndex === 8 }
            }
            Note { text: backend.status; visible: pages.currentIndex !== 0 && pages.currentIndex !== 8 && text.length > 0; font.pixelSize: 11 }
        }
    }
    MediaDevices { id: devices }
    WindowBorder { theme: settings.theme }
    Camera {
        id: camera
        cameraDevice: devices.defaultVideoInput
    }
    Loader {
        id: cameraLoader
        active: false
        sourceComponent: Camera { cameraDevice: settings.selectedCameraDevice }
    }
    CaptureSession { id: cameraSession; videoOutput: viewfinder }
    Dialog {
        id: purgeRecognitionDialog
        objectName: "purgeRecognitionDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(420, parent ? parent.width - 32 : 420)
        title: "Purge facial recognition data?"
        modal: true
        standardButtons: Dialog.Cancel
        closePolicy: Popup.CloseOnEscape
        contentItem: ColumnLayout {
            spacing: 12
            Label {
                Layout.fillWidth: true
                text: "This permanently deletes all enrolled face templates from this computer. Accounts, profile photos, and PINs are not deleted."
                wrapMode: Text.Wrap
            }
            Button {
                objectName: "confirmPurgeRecognition"
                text: "Purge facial recognition data"
                enabled: settings.profileControl !== null
                onClicked: {
                    settings.recognitionNotice = "Purging facial recognition data\u2026"
                    settings.profileControl.purgeRecognitionData()
                    purgeRecognitionDialog.close()
                }
            }
        }
    }
    Connections {
        target: backend
        function onConfigured() {
            if (pages.currentIndex === 2)
                settings.recognitionNotice = backend.config.camera_recognition === true
                    ? "Facial recognition enabled." : "Facial recognition disabled."
            if (settings.profileControl)
                settings.profileControl.recognitionConfigurationChanged(
                    backend.config.camera_recognition === true)
        }
    }
    Connections {
        target: devices
        function onVideoInputsChanged() {
            if (!(backend.config.camera_device || "") && !recognitionDevice.activeFocus &&
                    typeof backend.defaultRecognitionCamera === "function")
                recognitionDevice.text = backend.defaultRecognitionCamera()
        }
    }
    Connections {
        target: settings.profileControl
        ignoreUnknownSignals: true
        function onCameraReleaseRequested() { settings.stopCameraPreview() }
        function onRecognitionDataPurged() {
            settings.recognitionNotice = "Facial recognition data was purged."
        }
        function onRecognitionDataPurgeFailed(message) {
            settings.recognitionNotice = message
        }
    }
}
