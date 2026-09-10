import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BrowserShellTests(unittest.TestCase):
    def test_webengine_browser_is_built_and_packaged(self):
        cmake = (ROOT / 'apps/shell/CMakeLists.txt').read_text()
        runtime = (ROOT / 'distro/alpine/apks/world.ai').read_text().splitlines()
        development = (ROOT / 'distro/alpine/apks/world.devel').read_text().splitlines()
        self.assertIn('WebEngineWidgets', cmake)
        self.assertIn('aios-browser', cmake)
        self.assertIn('qt6-qtwebengine', runtime)
        self.assertIn('qt6-qtwebengine-dev', development)
        self.assertNotIn('chromium-chromedriver', runtime)

    def test_browser_exposes_only_bounded_local_actions(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text()
        client = (ROOT / 'apps/aios/browser.py').read_text()
        for action in ('snapshot', 'click', 'type', 'back', 'reload', 'stop'):
            self.assertIn(f'"{action}"', source)
            self.assertIn(f"'{action}'", client)
        self.assertIn('QLocalServer::UserAccessOption', source)
        self.assertIn('QWebEngineScript::ApplicationWorld', source)
        self.assertIn('NoPersistentCookies', source)
        self.assertIn('permission.deny()', source)
        self.assertIn('javaScriptPrompt', source)
        self.assertNotIn('remote-debugging-port', source)

    def test_browser_and_frameless_windows_use_xmb_hairlines(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text()
        border = (ROOT / 'apps/shell/WindowBorder.qml').read_text()
        chat = (ROOT / 'apps/shell/ChatWindow.qml').read_text()
        settings = (ROOT / 'apps/shell/SettingsWindow.qml').read_text()
        self.assertIn('contour.setAlphaF(0.5)', source)
        self.assertIn('border.width: 1', border)
        self.assertIn('theme.waveAlpha(0.5)', border)
        self.assertIn('WindowBorder { theme: chat.theme }', chat)
        self.assertIn('WindowBorder { theme: settings.theme }', settings)


if __name__ == '__main__':
    unittest.main()
