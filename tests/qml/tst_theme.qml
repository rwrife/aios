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
        property var config: ({theme_color: "blue", reduced_motion: true})
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
        function configure(values) {
            var updated = Object.assign({}, config, values)
            config = updated; configured()
        }
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
            {resolution: {width: 640, height: 480}, mode: "raw"},
            {resolution: {width: 1920, height: 1080}, mode: "large"},
            {resolution: {width: 640, height: 480}, mode: "compressed"}
        ]})
        compare(selected.resolution.width, 640)
        compare(selected.resolution.height, 480)
        compare(selected.mode, "compressed")
        window.destroy()
    }
    function test_unknown_theme_falls_back() {
        palette.selected = "unknown"
        compare(palette.paletteIndex, 0)
        compare(palette.night, "#101b27")
    }
}
