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
    function test_unknown_theme_falls_back() {
        palette.selected = "unknown"
        compare(palette.paletteIndex, 0)
        compare(palette.night, "#101b27")
    }
}
