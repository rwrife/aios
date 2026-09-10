import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window
import QtMultimedia

Window {
    id: settings
    required property var backend
    required property var theme
    property var profileControl: null
    property string recognitionNotice: ""
    signal setupRequested()
    title: "AIOS Settings"
    flags: Qt.Window | Qt.FramelessWindowHint
    width: Math.min(820, Screen.width - 32); height: Math.min(620, Screen.height - 48)
    minimumWidth: 540; minimumHeight: 400
    x: (Screen.width-width)/2; y: (Screen.height-height)/2
    color: theme.panel
    function stopCameraPreview() {
        camera.stop()
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
        camera.cameraDevice = device
        const format = previewFormat(device)
        if (format) camera.cameraFormat = format
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
    // Add a section here and its page to the StackLayout below.
    readonly property var sections: ["AI models", "Sound", "Camera", "Network & Wi-Fi", "Display", "Appearance", "About", "Accounts"]
    component Action: Button {
        id: control
        padding: 12
        contentItem: Text { text: control.text; color: theme.ink; font.pixelSize: 14 }
        background: Rectangle { color: control.hovered || control.down ? theme.horizon : theme.input; radius: 6; border.color: control.activeFocus ? theme.accent : theme.line }
    }
    component Note: Text {
        color: theme.muted; font.pixelSize: 14; wrapMode: Text.Wrap
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
            Layout.preferredWidth: 170; Layout.fillHeight: true; spacing: 6
            Text { text: "Settings"; color: theme.ink; font.pixelSize: 23; Layout.bottomMargin: 24 }
            Repeater {
                model: settings.sections
                Button {
                    id: sectionButton
                    required property string modelData; required property int index
                    Layout.fillWidth: true; padding: 12
                    Accessible.name: modelData; Accessible.role: Accessible.PageTab; Accessible.checked: pages.currentIndex === index
                    contentItem: Text { text: modelData; color: pages.currentIndex === index ? theme.ink : theme.muted; font.pixelSize: 14 }
                    background: Rectangle { radius: 6; color: pages.currentIndex === index || sectionButton.hovered ? theme.input : "transparent"; border.width: sectionButton.activeFocus ? 1 : 0; border.color: theme.accent }
                    onClicked: { settings.stopCameraPreview(); pages.currentIndex = index }
                }
            }
            Item { Layout.fillHeight: true }
            Action { objectName: "launchSetup"; text: "Run setup wizard"; Layout.fillWidth: true; onClicked: { settings.close(); settings.setupRequested() } }
            Note { text: backend.config.live ? "Live session\nChanges are lost after reboot." : "This computer"; font.pixelSize: 11 }
        }
        Rectangle { Layout.fillHeight: true; implicitWidth: 1; color: theme.line; opacity: 0.5 }
        ColumnLayout {
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 20
            RowLayout {
                Layout.fillWidth: true
                Item {
                    Layout.fillWidth: true; implicitHeight: 36
                    Text { anchors.verticalCenter: parent.verticalCenter; text: settings.sections[pages.currentIndex]; color: theme.ink; font.pixelSize: 22 }
                    MouseArea { anchors.fill: parent; onPressed: settings.startSystemMove() }
                }
                Button {
                    id: closeButton; implicitWidth: 36; implicitHeight: 36
                    Accessible.name: "Close settings"
                    contentItem: Text { text: "\u00d7"; color: theme.muted; font.pixelSize: 22; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    background: Rectangle { radius: 6; color: closeButton.hovered ? theme.input : "transparent"; border.width: closeButton.activeFocus ? 1 : 0; border.color: theme.accent }
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
                            Layout.fillWidth: true
                            Layout.preferredHeight: width * 9 / 16
                            Layout.minimumHeight: 100
                            color: theme.night; radius: 8
                            VideoOutput { id: viewfinder; anchors.fill: parent; fillMode: VideoOutput.PreserveAspectFit }
                            Text { anchors.centerIn: parent; visible: !camera.active; text: "Camera off"; color: theme.muted }
                        }
                        Note { text: camera.errorString; visible: camera.error !== Camera.NoError; font.pixelSize: 12 }
                        Action {
                            text: camera.active ? "Stop preview" : "Start preview"
                            enabled: devices.videoInputs.length > 0
                            onClicked: {
                                if (camera.active) settings.stopCameraPreview()
                                else {
                                    settings.configureCamera(camera.cameraDevice)
                                    if (settings.profileControl &&
                                            typeof settings.profileControl.setCameraPreviewActive === "function")
                                        settings.profileControl.setCameraPreviewActive(true)
                                    camera.start()
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
                            text: backend.config.camera_device || ""
                            enabled: !backend.configuring
                            maximumLength: 512
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Action {
                                objectName: "recognitionToggle"
                                text: backend.config.camera_recognition === true
                                    ? "Disable facial recognition" : "Enable facial recognition"
                                enabled: !backend.configuring &&
                                    (backend.config.camera_recognition === true || recognitionDevice.text.trim().length > 0)
                                onClicked: {
                                    const enabling = backend.config.camera_recognition !== true
                                    if (!enabling && settings.profileControl &&
                                            typeof settings.profileControl.setRecognitionEnabled === "function")
                                        settings.profileControl.setRecognitionEnabled(false)
                                    backend.configure({
                                        camera_recognition: enabling,
                                        camera_device: enabling ? recognitionDevice.text.trim()
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
                        Note {
                            objectName: "recognitionNotice"
                            text: settings.recognitionNotice
                            visible: text.length > 0
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
            }
            Note { text: backend.status; visible: pages.currentIndex !== 0 && text.length > 0; font.pixelSize: 11 }
        }
    }
    MediaDevices { id: devices }
    AccountSettings { id: accountsPage; parent: pages; control: settings.profileControl }
    ColumnLayout {
        parent: pages
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
    Camera {
        id: camera
        cameraDevice: devices.defaultVideoInput
    }
    CaptureSession { camera: camera; videoOutput: viewfinder }
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
            if (settings.profileControl)
                settings.profileControl.recognitionConfigurationChanged(
                    backend.config.camera_recognition === true)
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
