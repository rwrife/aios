import QtQuick
import QtTest
import "../../apps/shell/WindowSizing.js" as WindowSizing

TestCase {
    name: "WindowSizing"

    function test_screen_budget_data() {
        return [
            {tag: "small", width: 640, height: 480},
            {tag: "svga", width: 800, height: 600},
            {tag: "xga", width: 1024, height: 768},
            {tag: "hd", width: 1920, height: 1080},
            {tag: "4k", width: 3840, height: 2160},
            {tag: "workarea", width: 1366, height: 700}
        ]
    }

    function test_screen_budget(data) {
        var windows = [
            {width: 740, height: 650, minimumWidth: 0, minimumHeight: 0},
            {width: 820, height: 620, minimumWidth: 540, minimumHeight: 400},
            {width: 720, height: 640, minimumWidth: 440, minimumHeight: 360},
            {width: 360, height: 520, minimumWidth: 320, minimumHeight: 460}
        ]
        for (var i = 0; i < windows.length; ++i) {
            var window = windows[i]
            var width = WindowSizing.extent(window.width, data.width, data.width)
            var height = WindowSizing.extent(window.height, data.height, data.height)
            verify(width < data.width * 0.75)
            verify(height < data.height * 0.75)
            verify(width <= window.width)
            verify(height <= window.height)
            verify(WindowSizing.extent(window.minimumWidth, data.width, data.width) <= width)
            verify(WindowSizing.extent(window.minimumHeight, data.height, data.height) <= height)
        }
    }

    function test_large_screens_preserve_preferred_sizes() {
        compare(WindowSizing.extent(740, 1920, 1920), 740)
        compare(WindowSizing.extent(650, 1080, 1080), 650)
    }

    function test_multi_monitor_desktop_does_not_inflate_window() {
        compare(WindowSizing.extent(740, 800, 2720), 560)
        compare(WindowSizing.extent(650, 600, 1680), 420)
    }

    function test_reserved_desktop_space_reduces_budget() {
        compare(WindowSizing.extent(740, 800, 760), 532)
        compare(WindowSizing.extent(650, 600, 560), 392)
    }
}
