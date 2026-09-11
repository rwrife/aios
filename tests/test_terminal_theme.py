import unittest
import re
import os
import subprocess
import tempfile
from pathlib import Path
from aios.terminal_theme import HUES, OCEAN, palette


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "Desktop startup requires a POSIX shell")
class CompositorSelectionTests(unittest.TestCase):
    def test_renderer_selection(self):
        cases = (
            ("Mesa software", "    Accelerated: no\nOpenGL renderer string: llvmpipe (LLVM)\n", 0, "xrender"),
            ("Software without acceleration field", "OpenGL renderer string: softpipe\n", 0, "xrender"),
            ("Mesa hardware", "    Accelerated: yes\nOpenGL renderer string: Mesa Intel UHD\n", 0, "glx"),
            ("NVIDIA hardware", "OpenGL renderer string: NVIDIA GeForce\n", 0, "glx"),
            ("Failed probe", "Cannot open display\n", 1, "xrender"),
            ("Timed out probe", "", 124, "xrender"),
            ("Incomplete probe", "name of display: :0\n", 0, "xrender"),
        )
        for name, output, status, backend in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                bin_dir = home / "bin"
                bin_dir.mkdir()
                stubs = {
                    "xsetroot": "exit 0",
                    "openbox": "exit 0",
                    "pulseaudio": "exit 0",
                    "glxinfo": 'printf "%s" "$PROBE_OUTPUT"; exit "$PROBE_STATUS"',
                    "picom": 'printf "%s\\n" "$@" >"$HOME/compositor-args"',
                    "aios-shell": (
                        'for n in $(seq 1 100); do '
                        '[ ! -f "$HOME/compositor-args" ] || exit 0; sleep 0.02; done; exit 1'
                    ),
                }
                for command, body in stubs.items():
                    path = bin_dir / command
                    path.write_text("#!/bin/sh\n" + body + "\n")
                    path.chmod(0o755)
                subprocess.run(
                    ["sh", str(ROOT / "distro/alpine/overlay/usr/local/bin/aios-session")],
                    env=dict(os.environ, HOME=str(home),
                             PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                             PROBE_OUTPUT=output, PROBE_STATUS=str(status)),
                    capture_output=True, text=True, check=True, timeout=10,
                )
                args = (home / "compositor-args").read_text().splitlines()
                self.assertIn("/etc/xdg/picom.conf", args)
                if backend == "xrender":
                    self.assertEqual(args[-3:], ["--backend", "xrender", "--no-vsync"])
                else:
                    self.assertNotIn("--backend", args)
                    self.assertNotIn("--no-vsync", args)
                state = home / ".local/state/aios"
                self.assertEqual((state / "graphics.log").read_text(), output)
                self.assertIn("AIOS compositor: " + backend,
                              (state / "compositor.log").read_text())


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

    def test_compositor_preserves_square_native_frames(self):
        config = (ROOT / "distro/alpine/overlay/etc/xdg/picom.conf").read_text()
        self.assertIn('backend = "glx"', config)
        self.assertIn("use-damage = false;", config)
        world = (ROOT / "distro/alpine/apks/world.x11").read_text().splitlines()
        self.assertIn("mesa-dri-gallium", world)
        self.assertIn("mesa-utils", world)
        terminal_rule = re.search(
            r'\{\s*match = "class_g = \'AIOS-Terminal\'";(.*?)\}', config, re.S)
        self.assertIsNotNone(terminal_rule)
        self.assertIn("opacity = 0.92;", terminal_rule.group(1))
        self.assertEqual(re.findall(r'corner-radius\s*=\s*(\d+)\s*;', config), ["0"])
        self.assertNotIn("corner-radius", terminal_rule.group(1))
        self.assertNotIn("aios-browser", config)
        self.assertEqual(config.count("opacity = 0.92;"), 1)
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
        self.assertNotIn("<size>", browser_rule.group(1))

    def test_native_close_icon_has_twelve_pixel_visible_mark(self):
        icon = (ROOT / "distro/alpine/overlay/usr/share/themes/AIOS/openbox-3/close.xbm").read_text()
        self.assertIn("#define close_width 14", icon)
        self.assertIn("#define close_height 14", icon)
        data = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-f]{2})", icon))
        self.assertEqual(len(data), 28)
        pixels = {(x, y) for y in range(14) for x in range(14)
                  if data[y * 2 + x // 8] & (1 << (x % 8))}
        self.assertEqual((min(x for x, y in pixels), max(x for x, y in pixels)), (1, 12))
        self.assertEqual((min(y for x, y in pixels), max(y for x, y in pixels)), (1, 12))
        self.assertEqual(pixels, {(13 - x, y) for x, y in pixels})
        self.assertEqual(pixels, {(x, 13 - y) for x, y in pixels})

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
        container = (ROOT / "scripts/container-build.sh").read_text()
        preview = (ROOT / "scripts/preview-chat.sh").read_text()
        display = (ROOT / "scripts/test-identity-display.sh").read_text()
        self.assertIn("QT_MEDIA_BACKEND=gstreamer", display)
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
