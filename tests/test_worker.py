import contextlib
import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.worker = importlib.import_module("aios.worker")

    def events(self, request):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.worker.handle(request)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_tool_socket_routes_every_provider_through_agent_chat(self):
        configurations = [
            {"mode": "local", "agent_mode": "current"},
            {"mode": "remote", "agent_mode": "current"},
            {"mode": "chatgpt", "agent_mode": "current"},
            {"mode": "local", "agent_mode": "chatgpt"},
            {"mode": "remote", "agent_mode": "remote"},
        ]
        forwarded = [
            {"type": "progress", "text": "Using Application"},
            {"type": "token", "text": "done"},
        ]
        for config in configurations:
            with self.subTest(config=config), \
                    mock.patch("aios.worker.load_config", return_value=config) as load, \
                    mock.patch("aios.agent.chat", return_value=iter(forwarded)) as chat:
                events = self.events({
                    "action": "chat",
                    "messages": [{"role": "user", "content": "help"}],
                    "tool_socket": "/private/tools.sock",
                })
                self.assertEqual(events, forwarded + [{"type": "done"}])
                chat.assert_called_once_with(
                    [{"role": "user", "content": "help"}],
                    "/private/tools.sock",
                )
                load.assert_not_called()

    def test_chat_without_tool_socket_uses_core_text_stream(self):
        with mock.patch("aios.worker.chat", return_value=iter(["one", " two"])) as chat, \
                mock.patch("aios.agent.chat") as agent_chat:
            events = self.events({
                "action": "chat",
                "messages": [{"role": "user", "content": "hello"}],
            })
        self.assertEqual(events, [
            {"type": "token", "text": "one"},
            {"type": "token", "text": " two"},
            {"type": "done"},
        ])
        chat.assert_called_once()
        agent_chat.assert_not_called()

    def test_load_redacts_all_credentials(self):
        config = {
            "mode": "remote",
            "api_key": "primary-secret",
            "voice_key": "voice-secret",
            "agent_api_key": "agent-secret",
        }
        with mock.patch("aios.worker.load_config", return_value=config), \
                mock.patch("aios.worker.load_history", return_value=[]), \
                mock.patch("aios.worker.Path") as path:
            path.return_value.exists.return_value = False
            events = self.events({"action": "load"})
        loaded = events[0]["config"]
        self.assertNotIn("api_key", loaded)
        self.assertNotIn("voice_key", loaded)
        self.assertNotIn("agent_api_key", loaded)
        self.assertTrue(loaded["has_key"])
        self.assertTrue(loaded["has_voice_key"])
        self.assertTrue(loaded["has_agent_key"])

    def test_main_uses_safe_error_for_unexpected_failures(self):
        stdin = io.StringIO('{"action":"chat","messages":[]}\n')
        stdout = io.StringIO()
        with mock.patch("aios.worker.chat", side_effect=OSError("/secret/tools.sock token=abc")), \
                mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stdout(stdout), \
                self.assertRaises(SystemExit) as error:
            self.worker.main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {
            "type": "error",
            "text": "Unable to complete the request. Check configuration and available storage.",
        })


class DesktopSourceTests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text()

    def test_shell_starts_toolhost_and_routes_private_tool_socket(self):
        source = self.read("apps/shell/main.cpp")
        self.assertIn("QProcess tools;", source)
        self.assertIn("QTemporaryDir toolDirectory;", source)
        self.assertIn('"/tools.sock"', source)
        self.assertIn('{"-m", "aios.toolhost", socket}', source)
        self.assertIn('request.insert("tool_socket", socket)', source)
        self.assertIn('env.insert("AIOS_BROWSER_SESSION", sessionId)', source)
        self.assertIn('env.insert("AIOS_BROWSER_THEME", m_config.value("theme_color", "blue").toString())', source)
        self.assertIn("tieToDesktop(tools)", source)
        self.assertIn("ToolHostGracefulWaitMs", source)
        self.assertIn('action == "open_application"', source)
        self.assertIn('name == "terminal"', source)
        self.assertIn('name == "settings"', source)
        self.assertEqual(source.count("waitForFinished(ToolHostGracefulWaitMs)"), 2)

    def test_shell_never_caches_plaintext_keys_and_clears_pending_errors(self):
        source = self.read("apps/shell/main.cpp")
        self.assertIn('it.key() != "api_key" && it.key() != "voice_key" && it.key() != "agent_api_key"', source)
        self.assertGreaterEqual(source.count("pendingConfig.clear()"), 3)
        finished = source[source.index('qOverload<int,QProcess::ExitStatus>(&QProcess::finished)'):]
        self.assertRegex(
            finished,
            r'if \(action == "configure"\) \{ pendingConfig\.clear\(\); m_configuring = false; emit changed\(\); \}',
        )

    def test_agent_controls_and_plain_text_defenses_are_declared(self):
        model = self.read("apps/shell/ModelSettings.qml")
        self.assertIn('objectName: "agentProvider"', model)
        self.assertIn("objectName: 'agentUrl'", model)
        self.assertIn("objectName: 'agentModel'", model)
        self.assertIn("objectName: 'agentKey'", model)
        self.assertIn("objectName: 'subscriptionSettings'", model)
        self.assertIn("textFormat: Text.PlainText", model)
        self.assertIn("agent_api_key", model)

        chat = self.read("apps/shell/ChatWindow.qml")
        self.assertRegex(chat, r"session\.status;[^}]*textFormat:\s*Text\.PlainText")
        self.assertRegex(chat, r"id:\s*chip;[^}]*textFormat:\s*Text\.PlainText")
        settings = self.read("apps/shell/SettingsWindow.qml")
        note = settings[settings.index("component Note: Text"):settings.index("RowLayout {")]
        self.assertIn("textFormat: Text.PlainText", note)

    def test_build_packages_examples_and_skills_at_exact_paths_without_nesting(self):
        source = self.read("scripts/build-apps.sh")
        tests = self.read("scripts/test-identity-display.sh")
        self.assertIn('rm -rf "$DEST/usr/local/share/aios/examples"', source)
        self.assertIn('cp -R "$ROOT/examples" "$DEST/usr/local/share/aios/examples"', source)
        self.assertIn('rm -rf "$DEST/usr/local/share/aios/skills"', source)
        self.assertIn('cp -R "$ROOT/apps/skills" "$DEST/usr/local/share/aios/skills"', source)
        self.assertIn('AIOS_APP_HOST_TEST_BINARY=/tmp/display-shell/aios-app-host', tests)
        self.assertNotIn('cp -R "$ROOT/examples" "$DEST/usr/local/share/aios/"', source)
        self.assertEqual(source.count('rm -rf "$DEST/usr/local/share/aios/'), 2)
        self.assertTrue((ROOT / "apps/skills/application-builder/SKILL.md").is_file())

    def test_native_host_has_fixed_resource_and_protocol(self):
        source = self.read("apps/shell/app_host.cpp")
        self.assertIn('QStringLiteral("qrc:/AppHost.qml")', source)
        self.assertIn('QByteArrayLiteral("calculator")', source)
        self.assertIn('"ready\\n"', source)
        self.assertNotIn("AIOS_APP_TEMPLATE) +", source)
        self.assertNotIn("argv[1]", source)

    def test_calculator_avoids_dynamic_or_external_code(self):
        source = self.read("apps/shell/AppHost.qml")
        for forbidden in ("eval(", "Function(", "Loader", "XmlHttpRequest", "WebSocket"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("textFormat: Text.PlainText", source)
        self.assertIn("function press(key)", source)
        self.assertIn("function reset()", source)

    def test_calculator_button_clicks_do_not_steal_focus(self):
        source = self.read("apps/shell/AppHost.qml")
        button_section = source[source.index("component CalcButton: Button {"):source.index("FocusScope {")]
        self.assertNotIn("keyboardFocus.forceActiveFocus()", button_section)
        self.assertIn("Component.onCompleted: keyboardFocus.forceActiveFocus()", source)

    def test_cmake_builds_resources_and_installs_host(self):
        source = self.read("apps/shell/CMakeLists.txt")
        self.assertIn("qt_add_executable(aios-app-host app_host.cpp)", source)
        self.assertIn("qt_add_resources(aios-app-host app_host_ui", source)
        self.assertIn("AppHost.qml", source)
        self.assertIn(
            "install(TARGETS aios-shell aios-browser aios-app-host RUNTIME DESTINATION bin)",
            source,
        )

    def test_preview_exports_built_native_host(self):
        source = self.read("scripts/preview-chat.sh")
        self.assertIn(
            "cmake --build /preview-build --target aios-shell aios-browser aios-app-host",
            source,
        )
        self.assertIn("export AIOS_APP_HOST=/preview-build/aios-app-host", source)


if __name__ == "__main__":
    unittest.main()
