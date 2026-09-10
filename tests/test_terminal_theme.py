import unittest
import re
from pathlib import Path
from aios.terminal_theme import HUES, OCEAN, palette


ROOT = Path(__file__).resolve().parents[1]


class TerminalThemeTests(unittest.TestCase):
    def test_palette_tracks_desktop_choices(self):
        qml = (ROOT / 'apps/shell/Theme.qml').read_text()
        choices = dict((key, float(hue)) for key, hue in
                       re.findall(r'key: "(\w+)".*?hue: ([0-9.]+)', qml))
        self.assertEqual(HUES, choices)
        self.assertEqual(palette('blue'), OCEAN)
        self.assertEqual(palette('unknown'), OCEAN)
        self.assertEqual(len({palette(key)[2] for key in choices}), 8)
        for key in choices:
            for color in palette(key):
                self.assertRegex(color, r'^#[0-9a-f]{6}$')

    def test_launcher_uses_aios_palette_and_readable_type(self):
        launcher = (ROOT / "distro/alpine/overlay/usr/local/bin/aios-terminal").read_text()
        for value in ('"$panel"', "#f1f5f6", '"$line"', '"$accent"'):
            self.assertIn(value, launcher)
        self.assertIn('-fs 14', launcher)
        self.assertIn('-b 18', launcher)
        self.assertIn('-bw 1', launcher)
        self.assertIn('-class AIOS-Terminal', launcher)

    def test_compositor_rounds_only_native_browser_and_terminal(self):
        config = (ROOT / "distro/alpine/overlay/etc/xdg/picom.conf").read_text()
        self.assertIn('backend = "xrender"', config)
        terminal_rule = re.search(
            r'\{\s*match = "class_g = \'AIOS-Terminal\'";(.*?)\}', config, re.S)
        browser_rule = re.search(
            r'\{\s*match = "class_i = \'aios-browser\'";(.*?)\}', config, re.S)
        self.assertIsNotNone(terminal_rule)
        self.assertIsNotNone(browser_rule)
        self.assertIn("opacity = 0.92;", terminal_rule.group(1))
        self.assertIn("corner-radius = 16;", terminal_rule.group(1))
        self.assertNotIn("opacity", browser_rule.group(1))
        self.assertIn("corner-radius = 16;", browser_rule.group(1))
        self.assertEqual(config.count("corner-radius = 16;"), 2)
        self.assertIn('picom', (ROOT / "distro/alpine/apks/world.x11").read_text().splitlines())

    def test_openbox_chrome_uses_aios_window_colors(self):
        theme = (ROOT / "distro/alpine/overlay/usr/share/themes/AIOS/openbox-3/themerc").read_text()
        openbox = (ROOT / "distro/alpine/overlay/etc/xdg/openbox/rc.xml").read_text()
        self.assertIn("<name>AIOS</name>", openbox)
        self.assertIn("window.active.title.bg.color: #172633", theme)
        self.assertIn("window.active.label.text.color: #f1f5f6", theme)
        self.assertIn("window.active.button.hover.image.color: #b2c3cd", theme)
        self.assertIn("border.width: 1", theme)
        self.assertIn("<keepBorder>yes</keepBorder>", openbox)
        self.assertIn("<titleLayout>LC</titleLayout>", openbox)
        self.assertIn('<application class="AIOS-Terminal">', openbox)
        self.assertIn("<decor>yes</decor>", openbox)
        terminal_rule = re.search(
            r'<application class="AIOS-Terminal">(.*?)</application>',
            openbox, re.S)
        browser_rule = re.search(
            r'<application name="aios-browser" class="AIOS Browser">'
            r'(.*?)</application>',
            openbox, re.S)
        self.assertIsNotNone(terminal_rule)
        self.assertIsNotNone(browser_rule)
        self.assertIn("<width>69%</width>", terminal_rule.group(1))
        self.assertIn("<height>60%</height>", terminal_rule.group(1))
        self.assertNotIn("<size>", browser_rule.group(1))

    def test_desktop_uses_gstreamer_and_lazily_creates_media_player(self):
        world = (ROOT / "distro/alpine/apks/world.ai").read_text().splitlines()
        session = (ROOT / "distro/alpine/overlay/usr/local/bin/aios-session").read_text()
        voice = (ROOT / "apps/shell/voice.h").read_text()
        self.assertIn("qt6-qtmultimedia-gstreamer", world)
        self.assertIn("gst-plugins-good", world)
        self.assertNotIn("qt6-qtmultimedia-ffmpeg", world)
        self.assertIn('QT_MEDIA_BACKEND="${QT_MEDIA_BACKEND:-gstreamer}"', session)
        self.assertIn("QMediaPlayer *player = nullptr;", voice)
        self.assertIn("void ensurePlayer()", voice)
        build = (ROOT / "scripts/build-apps.sh").read_text()
        container = (ROOT / "scripts/container-build.sh").read_text()
        preview = (ROOT / "scripts/preview-chat.sh").read_text()
        display = (ROOT / "scripts/test-identity-display.sh").read_text()
        self.assertIn("QT_MEDIA_BACKEND=gstreamer", build)
        self.assertIn("QT_MEDIA_BACKEND=gstreamer", preview)
        self.assertIn("gst-plugins-good", container)
        self.assertIn("gst-plugins-good", preview)
        self.assertIn("gst-inspect-1.0 v4l2src", display)

    def test_desktop_launch_paths_use_the_themed_launcher(self):
        main = (ROOT / "apps/shell/main.cpp").read_text()
        openbox = (ROOT / "distro/alpine/overlay/etc/xdg/openbox/rc.xml").read_text()
        session = (ROOT / "distro/alpine/overlay/usr/local/bin/aios-session").read_text()
        self.assertEqual(main.count('program = "aios-terminal"'), 1)
        self.assertIn('startDetached("aios-terminal"', main)
        self.assertIn('<command>aios-terminal</command>', openbox)
        self.assertIn('aios-terminal -title "AIOS Recovery"', session)

    def test_desktop_renderer_supports_camera_video_frames(self):
        session = (ROOT / "distro/alpine/overlay/usr/local/bin/aios-session").read_text()
        self.assertNotIn('QT_QUICK_BACKEND', session)
        self.assertIn('Mesa provides a software OpenGL fallback', session)


if __name__ == "__main__":
    unittest.main()
