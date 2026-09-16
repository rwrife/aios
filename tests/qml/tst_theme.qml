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
        property var clockState: ({})
        property bool clockBusy: false
        property string clockNotice: ""
        property var clockRequests: []
        signal clockChanged()
        function clockRequest(value) { clockRequests = clockRequests.concat([value || "read"]) }
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
        property bool cameraPreviewActive: false
        property string cameraPreview: ""
        function setCameraPreviewActive(active) { cameraPreviewActive = active; if (!active) cameraPreview = ""; return true }
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
    function test_date_time_page_data() {
        return [{tag: "Ocean", theme: "blue"}, {tag: "Sage", theme: "sage"}]
    }
    function test_date_time_page(data) {
        backend.config = ({theme_color: data.theme, reduced_motion: true})
        backend.clockState = ({utc: "2026-09-11T14:30:00Z", local: "2026-09-11T14:30:00+00:00", timezone: "UTC", sync_daemons: []})
        backend.clockRequests = []
        backend.clockBusy = false
        var window = createTemporaryObject(settingsComponent, test, {backend: backend, theme: palette, width: 820, height: 620})
        window.show()
        window.openSection("date_time")
        wait(100)
        compare(findChild(window, "settingsPages").currentIndex, 8)
        compare(backend.clockRequests.length, 1)
        var input = findChild(window, "dateTimeInput")
        var apply = findChild(window, "applyClock")
        var setup = findChild(window, "launchSetup")
        verify(!setup.visible)
        verify(input.visible)
        backend.clockChanged()
        compare(input.text, backend.clockState.utc)
        input.text = "2026-09-11T07:30:00-07:00"
        backend.clockChanged()
        compare(input.text, "2026-09-11T07:30:00-07:00")
        compare(input.background.color, palette.input)
        verify(apply.enabled)
        apply.clicked()
        compare(backend.clockRequests[1], input.text)
        backend.clockState = Object.assign({}, backend.clockState, {sync_daemons: ["ntpd"]})
        verify(!apply.enabled)
        backend.clockState = ({})
        verify(!apply.enabled)
        compare(findChild(window, "machineClockValue").text, "Clock unavailable")
        window.close()
    }
    Component {
        id: borderComponent
        Rectangle {
            width: 96; height: 80
            color: borderTheme.night
            property alias frame: outline
            property alias theme: borderTheme
            Theme { id: borderTheme }
            WindowBorder { id: outline; theme: borderTheme }
        }
    }
    function test_window_border_matches_rounded_surface_data() {
        return [{tag: "Ocean", theme: "blue"}, {tag: "Sage", theme: "sage"}]
    }
    function test_window_border_matches_rounded_surface(data) {
        var surface = createTemporaryObject(borderComponent, test.parent)
        surface.theme.selected = data.theme
        verify(waitForRendering(surface))
        compare(surface.frame.radius, surface.theme.windowRadius)
        compare(surface.frame.border.width, 1)
        compare(surface.frame.border.color, surface.theme.waveAlpha(0.5))
        var image = grabImage(surface)
        var background = String(surface.color)
        compare(String(image.pixel(0, 0)), background)
        compare(String(image.pixel(image.width - 1, 0)), background)
        compare(String(image.pixel(0, image.height - 1)), background)
        compare(String(image.pixel(image.width - 1, image.height - 1)), background)
        verify(String(image.pixel(image.width / 2, 0)) !== background)
    }
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
        compare(findChild(window, "settingsWindowSurface").color, palette.panel)
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
    function test_camera_preview_service_lifecycle_data() {
        return [{tag: "Ocean", theme: "blue"}, {tag: "Sage", theme: "sage"}]
    }
    function test_camera_preview_service_lifecycle(data) {
        backend.config = ({theme_color: data.theme, reduced_motion: true})
        var window = settingsComponent.createObject(test, {backend: backend, theme: palette, profileControl: profileControl})
        window.show()
        findChild(window, "settingsPages").currentIndex = 2
        profileControl.setCameraPreviewActive(true)
        tryCompare(window, "previewActive", true)
        profileControl.cameraReleaseRequested()
        tryCompare(window, "previewActive", false)
        profileControl.setCameraPreviewActive(true)
        window.hide()
        tryCompare(profileControl, "cameraPreviewActive", false)
        window.show()
        compare(window.previewActive, false)
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
