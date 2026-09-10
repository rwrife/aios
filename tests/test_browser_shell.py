import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BrowserShellTests(unittest.TestCase):
    def test_webengine_browser_is_built_and_packaged(self):
        cmake = (ROOT / 'apps/shell/CMakeLists.txt').read_text(encoding='utf-8')
        runtime = (ROOT / 'distro/alpine/apks/world.ai').read_text(encoding='utf-8').splitlines()
        development = (ROOT / 'distro/alpine/apks/world.devel').read_text(encoding='utf-8').splitlines()
        profile = (ROOT / 'distro/alpine/profiles/mkimg.aios.sh').read_text(encoding='utf-8')
        boot_test = (ROOT / 'scripts/test-boot.py').read_text(encoding='utf-8')
        self.assertIn('WebEngineWidgets', cmake)
        self.assertIn('aios-browser', cmake)
        self.assertIn('qt6-qtwebengine', runtime)
        self.assertIn('qt6-qtwebengine-dev', development)
        self.assertIn('rootflags=size=75%', profile)
        self.assertIn('parser.add_argument("--memory-mb", type=int, default=8192)', boot_test)
        self.assertIn('parser.add_argument("--cpus", type=int, default=4)', boot_test)
        self.assertIn('"-m", str(args.memory_mb)', boot_test)
        self.assertIn('"-smp", str(args.cpus)', boot_test)
        self.assertNotIn('chromium-chromedriver', runtime)

    def test_browser_exposes_only_bounded_local_actions(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text(encoding='utf-8')
        client = (ROOT / 'apps/aios/browser.py').read_text(encoding='utf-8')
        for action in ('snapshot', 'click', 'type', 'back', 'reload', 'stop'):
            self.assertIn(f'"{action}"', source)
            self.assertIn(f'"{action}"', client)
        self.assertIn('QLocalServer::UserAccessOption', source)
        self.assertIn('QWebEngineScript::ApplicationWorld', source)
        self.assertIn('QWebEngineView::loadStarted', source)
        self.assertIn('pendingNavigation && !ok', source)
        self.assertIn('NoPersistentCookies', source)
        self.assertIn('permission.deny()', source)
        self.assertIn('javaScriptPrompt', source)
        self.assertIn('m_snapshotInFlight', source)
        self.assertNotIn('remote-debugging-port', source)

    def test_browser_and_frameless_windows_use_xmb_hairlines(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text(encoding='utf-8')
        border = (ROOT / 'apps/shell/WindowBorder.qml').read_text(encoding='utf-8')
        chat = (ROOT / 'apps/shell/ChatWindow.qml').read_text(encoding='utf-8')
        settings = (ROOT / 'apps/shell/SettingsWindow.qml').read_text(encoding='utf-8')
        self.assertIn('contour.setAlphaF(0.5)', source)
        self.assertIn('border.width: 1', border)
        self.assertIn('theme.waveAlpha(0.5)', border)
        self.assertIn('WindowBorder { theme: chat.theme }', chat)
        self.assertIn('WindowBorder { theme: settings.theme }', settings)

    def test_browser_address_tracks_committed_redirects(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text(encoding='utf-8')
        self.assertIn('m_view->setFocus();', source)
        self.assertIn('m_address->setText(url == QUrl("about:blank")', source)
        self.assertNotIn('if (!m_address->hasFocus())', source)

    def test_window_title_uses_page_title_without_browser_suffix(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text(encoding='utf-8')
        self.assertIn('setWindowTitle("Browser")', source)
        self.assertIn('setWindowTitle(title.trimmed().isEmpty() ? "Browser" : title)', source)
        self.assertNotIn('setWindowTitle("AIOS Browser")', source)
        self.assertIn('app.setApplicationName("AIOS Browser")', source)

    def test_manual_browser_controls_interrupt_agent_operations(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text(encoding='utf-8')
        self.assertEqual(source.count('interruptPendingForUserAction();'), 4)
        self.assertIn('Browser operation interrupted by a manual browser control.', source)

    def test_browser_bounds_initial_frame_and_removes_minimize(self):
        source = (ROOT / 'apps/browser/main.cpp').read_text(encoding='utf-8')
        geometry = (ROOT / 'apps/window_geometry.h').read_text(encoding='utf-8')
        self.assertIn('setWindowFlag(Qt::CustomizeWindowHint)', source)
        self.assertIn('setWindowFlag(Qt::WindowMinimizeButtonHint, false)', source)
        self.assertIn('screen()->availableGeometry()', source)
        self.assertIn('windowHandle()->frameMargins()', source)
        self.assertIn('whenWindowFrameReady(windowHandle()', source)
        self.assertIn('initialWindowSize(minimumSize(), available, frame)', source)
        self.assertIn('m_initialGeometryApplied', source)
        self.assertIn('available.width() * 0.7', geometry)
        self.assertIn('available.height() * 0.7', geometry)
        self.assertIn('- frame.top() - frame.bottom()', geometry)
        self.assertNotIn('setMaximumSize(', source)


if __name__ == '__main__':
    unittest.main()
