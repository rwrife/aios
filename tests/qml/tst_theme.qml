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
    function test_setup_action_keeps_full_label_data() {
        return [{tag: "default", width: 820}, {tag: "minimum", width: 540}]
    }
    function test_setup_action_keeps_full_label(data) {
        var window = settingsComponent.createObject(test, {
            backend: backend, theme: palette, width: data.width, height: 620
        })
        window.show()
        var setup = findChild(window, "launchSetup")
        waitForRendering(setup)
        compare(setup.contentItem.truncated, false)
        verify(setup.width >= 170)
        window.destroy()
    }
    function test_pick_color_updates_existing_window() {
        var window = settingsComponent.createObject(test, {backend: backend, theme: palette})
        verify(window !== null)
        window.show()
        compare(findChild(window, "settingsWindowSurface").radius, palette.windowRadius)
        compare(findChild(window, "settingsWindowTitle").font.pixelSize, 22)
        compare(findChild(window, "settingsCloseButton").implicitWidth, 36)
        compare(findChild(window, "settingsCloseButton").implicitHeight, 36)
        compare(findChild(window, "settingsCloseButton").background.radius, 8)
        var setup = findChild(window, "launchSetup")
        compare(setup.implicitHeight, 44)
        compare(setup.contentItem.horizontalAlignment, Text.AlignHCenter)
        compare(setup.contentItem.elide, Text.ElideRight)
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
    function test_unknown_theme_falls_back() {
        palette.selected = "unknown"
        compare(palette.paletteIndex, 0)
        compare(palette.night, "#101b27")
    }
}
