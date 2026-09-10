import QtQuick
import QtTest
import "../../apps/shell"

TestCase {
    id: test
    name: "ThemeSettings"
    when: windowShown
    width: 900; height: 750
    Theme { id: palette; selected: backend.config.theme_color || "blue" }
    QtObject {
        id: backend
        property var config: ({
            theme_color: "blue", reduced_motion: true,
            camera_recognition: false,
            camera_device: "/dev/v4l/by-id/test-video-index0"
        })
        property bool busy: false
        property bool configuring: false
        property string status: ""
        property var subscription: ({})
        property string loginUrl: ""
        property string loginCode: ""
        property var systemInfo: ({
            version: "0.1.0", build: "20260909.1705", commit: "99dd1ecf8c77",
            os: "AIOS Test", kernel: "Linux 6.12", architecture: "x86_64",
            cpu: "Test CPU · 2 logical CPUs", memory: "4.0 GiB"
        })
        signal configured()
        function defaultRecognitionCamera() {
            return "/dev/v4l/by-id/test-video-index0"
        }
        function configure(values) {
            var updated = Object.assign({}, config, values)
            config = updated; configured()
        }
    }
    QtObject {
        id: profileControl
        property string recognitionState: "disabled"
        property int purges: 0
        property bool recognitionEnabled: false
        property bool busy: false
        property bool greetingOnly: true
        property bool personalAvailable: true
        property string error: ""
        property var profile: ({})
        property var profiles: []
        signal cameraReleaseRequested()
        signal recognitionDataPurged()
        signal recognitionDataPurgeFailed(string message)
        function listProfiles() {}
        function setSecureInput(active) {}
        function setCameraPreviewActive(active) {}
        function setRecognitionEnabled(enabled) {
            recognitionEnabled = enabled
            recognitionState = enabled ? "ready" : "disabled"
        }
        function recognitionConfigurationChanged(enabled) {
            setRecognitionEnabled(enabled)
        }
        function purgeRecognitionData() { purges++; recognitionDataPurged() }
    }
    Component { id: settingsComponent; SettingsWindow {} }
    function test_pick_color_updates_existing_window() {
        var window = settingsComponent.createObject(test, {backend: backend, theme: palette})
        verify(window !== null)
        window.show()
        findChild(window, "settingsPages").currentIndex = 5
        wait(100)
        compare(palette.choices.length, 8)
        var original = String(palette.horizon)
        var choices = findChild(window, "themeChoices")
        compare(choices.count, 8)
        var swatch = choices.itemAt(5)
        verify(swatch.visible)
        mouseClick(swatch)
        tryCompare(backend.config, "theme_color", "rose")
        tryVerify(function() { return String(palette.horizon) !== original })
        compare(window.color, palette.panel)
        verify(swatch.checked)
        verify(!choices.itemAt(0).checked)
        backend.configuring = true
        verify(!choices.itemAt(2).enabled)
        backend.configuring = false
        window.destroy()
    }
    function test_background_motion_can_be_enabled() {
        backend.config = ({theme_color: "blue", reduced_motion: true})
        var window = settingsComponent.createObject(test, {backend: backend, theme: palette})
        verify(window !== null)
        window.show()
        findChild(window, "settingsPages").currentIndex = 5
        wait(100)
        var motionToggle = findChild(window, "motionToggle")
        verify(motionToggle !== null)
        compare(motionToggle.text, "Enable motion")
        mouseClick(motionToggle)
        tryCompare(backend.config, "reduced_motion", false)
        compare(motionToggle.text, "Reduce motion")
        window.destroy()
    }
    function test_about_shows_build_and_hardware_information() {
        var window = settingsComponent.createObject(test, {backend: backend, theme: palette})
        verify(window !== null)
        window.show()
        findChild(window, "settingsPages").currentIndex = 6
        wait(100)
        compare(findChild(window, "aboutBuild").text, "20260909.1705")
        compare(findChild(window, "aboutCommit").text, "99dd1ecf8c77")
        compare(findChild(window, "aboutCpu").text, "Test CPU · 2 logical CPUs")
        compare(findChild(window, "aboutMemory").text, "4.0 GiB")
        window.destroy()
    }
    function test_camera_preview_uses_bounded_format_without_runtime_enums() {
        var window = settingsComponent.createObject(test, {backend: backend, theme: palette})
        verify(window !== null)
        var selected = window.previewFormat({videoFormats: [
            {resolution: {width: 640, height: 360}, mode: "raw"},
            {resolution: {width: 1920, height: 1080}, mode: "large"},
            {resolution: {width: 640, height: 360}, mode: "compressed"}
        ]})
        compare(selected.resolution.width, 640)
        compare(selected.resolution.height, 360)
        compare(selected.mode, "compressed")
        window.show()
        findChild(window, "settingsPages").currentIndex = 2
        wait(100)
        var preview = findChild(window, "cameraPreview")
        verify(Math.abs(preview.width / preview.height - 16 / 9) < 0.02)
        verify(Math.abs(preview.width / preview.parent.width - 0.75) < 0.02)
        window.destroy()
    }
    function test_facial_recognition_has_separate_toggle_and_purge_controls() {
        backend.config = ({
            theme_color: "blue", reduced_motion: true,
            camera_recognition: false,
            camera_device: ""
        })
        profileControl.purges = 0
        profileControl.setRecognitionEnabled(false)
        var window = settingsComponent.createObject(test, {
            backend: backend, theme: palette, profileControl: profileControl
        })
        verify(window !== null)
        window.show()
        findChild(window, "settingsPages").currentIndex = 2
        wait(100)
        var scroll = findChild(window, "cameraSettingsScroll")
        scroll.contentItem.contentY = Math.max(
            0, scroll.contentItem.contentHeight - scroll.height)
        wait(50)
        var status = findChild(window, "recognitionStatus")
        var toggle = findChild(window, "recognitionToggle")
        compare(findChild(window, "recognitionDevice").text,
                "/dev/v4l/by-id/test-video-index0")
        verify(status.text.indexOf("Off.") === 0)
        compare(toggle.text, "Enable facial recognition")
        verify(toggle.enabled)
        mouseClick(toggle)
        tryCompare(backend.config, "camera_recognition", true)
        tryCompare(toggle, "text", "Disable facial recognition")
        verify(status.text.indexOf("On.") === 0)
        mouseClick(toggle)
        tryCompare(backend.config, "camera_recognition", false)
        compare(profileControl.purges, 0)
        mouseClick(findChild(window, "purgeRecognition"))
        var prompt = findChild(window, "purgeRecognitionDialog")
        tryCompare(prompt, "opened", true)
        mouseClick(findChild(window, "confirmPurgeRecognition"))
        compare(profileControl.purges, 1)
        tryCompare(prompt, "opened", false)
        compare(findChild(window, "recognitionNotice").text,
                "Facial recognition data was purged.")
        window.destroy()
    }
    function test_unknown_theme_falls_back() {
        palette.selected = "unknown"
        compare(palette.paletteIndex, 0)
        compare(palette.night, "#101b27")
    }
}
