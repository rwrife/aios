import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TerminalThemeTests(unittest.TestCase):
    def test_launcher_uses_aios_palette_and_readable_type(self):
        launcher = (ROOT / "distro/alpine/overlay/usr/local/bin/aios-terminal").read_text()
        for value in ("#172633", "#f1f5f6", "#4c6574", "#bde4e6"):
            self.assertIn(value, launcher)
        self.assertIn('-fs 14', launcher)
        self.assertIn('-b 18', launcher)
        self.assertIn('-class AIOS-Terminal', launcher)

    def test_compositor_targets_only_terminal_surface(self):
        config = (ROOT / "distro/alpine/overlay/etc/xdg/picom.conf").read_text()
        self.assertIn("92:class_g = 'AIOS-Terminal'", config)
        self.assertIn('backend = "xrender"', config)
        self.assertIn('picom', (ROOT / "distro/alpine/apks/world.x11").read_text().splitlines())

    def test_openbox_chrome_uses_aios_window_colors(self):
        theme = (ROOT / "distro/alpine/overlay/usr/share/themes/AIOS/openbox-3/themerc").read_text()
        openbox = (ROOT / "distro/alpine/overlay/etc/xdg/openbox/rc.xml").read_text()
        self.assertIn("<name>AIOS</name>", openbox)
        self.assertIn("window.active.title.bg.color: #172633", theme)
        self.assertIn("window.active.label.text.color: #b2c3cd", theme)
        self.assertIn("window.active.button.hover.image.color: #bde4e6", theme)

    def test_desktop_launch_paths_use_the_themed_launcher(self):
        main = (ROOT / "apps/shell/main.cpp").read_text()
        openbox = (ROOT / "distro/alpine/overlay/etc/xdg/openbox/rc.xml").read_text()
        session = (ROOT / "distro/alpine/overlay/usr/local/bin/aios-session").read_text()
        self.assertEqual(main.count('program = "aios-terminal"'), 1)
        self.assertIn('startDetached("aios-terminal"', main)
        self.assertIn('<command>aios-terminal</command>', openbox)
        self.assertIn('aios-terminal -title "AIOS Recovery"', session)


if __name__ == "__main__":
    unittest.main()
