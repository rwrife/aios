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

    def test_browser_socket_does_not_activate_agent_tools(self):
        with mock.patch("aios.worker.chat", return_value=iter(["plain"])) as chat, \
                mock.patch("aios.agent.chat") as agent_chat:
            events = self.events({
                "action": "chat",
                "messages": [{"role": "user", "content": "hello"}],
                "browser_socket": "/private/browser.sock",
            })
        self.assertEqual(events, [
            {"type": "token", "text": "plain"},
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
        self.assertNotIn('request.insert("browser_socket"', source)
        self.assertNotIn('"aios.browser"', source)
        self.assertIn("tieToDesktop(tools)", source)

    def test_shell_never_caches_plaintext_keys_and_clears_pending_errors(self):
        source = self.read("apps/shell/main.cpp")
        self.assertIn('it.key() != "api_key" && it.key() != "voice_key" && it.key() != "agent_api_key"', source)
        self.assertGreaterEqual(source.count("pendingConfig.clear()"), 3)

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
        settings = self.read("apps/shell/SettingsWindow.qml")
        note = settings[settings.index("component Note: Text"):settings.index("RowLayout {")]
        self.assertIn("textFormat: Text.PlainText", note)

    def test_build_packages_skills_at_expected_path_without_nesting(self):
        source = self.read("scripts/build-apps.sh")
        self.assertIn('rm -rf "$DEST/usr/local/share/aios/skills"', source)
        self.assertIn('cp -R "$ROOT/apps/skills" "$DEST/usr/local/share/aios/skills"', source)
        self.assertTrue((ROOT / "apps/skills/application-builder/SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
