import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch
from aios import cli, core


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["model"] == "bad-key":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"secret-provider-diagnostic")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for token in ("Hello ", "世界"):
            event = "data: " + json.dumps({"choices": [{"delta": {"content": token}}]}, ensure_ascii=False) + "\n\n"
            # Deliberately split UTF-8 bytes and SSE records.
            for byte in event.encode():
                self.wfile.write(bytes([byte]))
                self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name + "/config", "XDG_DATA_HOME": self.tmp.name + "/data"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_sse_comments_multiline_and_final_event(self):
        self.assertEqual(list(core.sse_events(io.BytesIO(b": ping\r\ndata: first\r\ndata: second\r\n\r\ndata: last"))), ["first\nsecond", "last"])

    def test_bundled_model_default_preserves_user_choices(self):
        model = Path(self.tmp.name) / "starter.gguf"
        with patch.object(core, "BUNDLED_MODEL", model):
            self.assertEqual(core.load_config()["model_path"], "")
            model.touch()
            self.assertEqual(core.load_config()["model_path"], str(model))
            custom = Path(self.tmp.name) / "custom.gguf"
            custom.touch()
            core.save_config({"model_path": str(custom)})
            self.assertEqual(core.load_config()["model_path"], str(custom))
            core.save_config({"mode": "remote", "url": "https://example.com/v1", "model_path": ""})
            config = core.load_config()
            self.assertEqual(config["mode"], "remote")
            self.assertEqual(config["model_path"], "")

    def test_config_preserves_key_and_rejects_insecure_remote(self):
        core.save_config({"mode": "remote", "url": "https://example.com/v1", "api_key": "private"})
        core.save_config({"model": "test"})
        self.assertEqual(core.load_config()["api_key"], "private")
        core.save_config({"url": "https://different.example/v1"})
        self.assertEqual(core.load_config()["api_key"], "")
        self.assertEqual((core.config_dir() / "config.json").stat().st_mode & 0o777, 0o600)
        for url in ("http://example.com/v1", "file:///etc/passwd", "https://key@example.com/v1", "https://example.com/v1?key=secret"):
            with self.assertRaises(ValueError):
                core.save_config({"url": url})

    def test_agent_config_defaults_persistence_and_key_independence(self):
        config = core.load_config()
        self.assertEqual(
            {key: config[key] for key in ("agent_mode", "agent_url", "agent_model", "agent_api_key")},
            {"agent_mode": "current", "agent_url": "", "agent_model": "", "agent_api_key": ""},
        )
        self.assertTrue({"agent_mode", "agent_url", "agent_model", "agent_api_key"} <= set(core.defaults_keys()))

        core.save_config({
            "mode": "remote",
            "url": "https://ordinary.example/v1",
            "model": "ordinary-model",
            "api_key": "ordinary-secret",
            "agent_mode": "remote",
            "agent_url": "https://agent.example/v1",
            "agent_model": "  agent-model  ",
            "agent_api_key": "agent-secret",
        })
        config = core.load_config()
        self.assertEqual(config["agent_model"], "agent-model")
        self.assertEqual(config["api_key"], "ordinary-secret")
        self.assertEqual(config["agent_api_key"], "agent-secret")

        core.save_config({"url": "https://ordinary-two.example/v1"})
        config = core.load_config()
        self.assertEqual(config["api_key"], "")
        self.assertEqual(config["agent_api_key"], "agent-secret")
        core.save_config({"api_key": "ordinary-replacement", "agent_url": "https://agent-two.example/v1"})
        config = core.load_config()
        self.assertEqual(config["api_key"], "ordinary-replacement")
        self.assertEqual(config["agent_api_key"], "")
        core.save_config({"agent_url": "https://agent-three.example/v1", "agent_api_key": "agent-replacement"})
        self.assertEqual(core.load_config()["agent_api_key"], "agent-replacement")
        self.assertEqual((core.config_dir() / "config.json").stat().st_mode & 0o777, 0o600)

    def test_agent_config_validation_and_remote_requirements(self):
        for values in (
            {"agent_mode": "other"},
            {"agent_mode": "remote"},
            {"agent_mode": "remote", "agent_url": "https://agent.example/v1"},
            {"agent_mode": "remote", "agent_url": "http://example.com/v1", "agent_model": "model"},
            {"agent_url": "https://user:secret@agent.example/v1"},
            {"agent_url": "https://agent.example/v1?secret=yes"},
            {"agent_model": 7},
            {"agent_model": "x" * 201},
            {"agent_model": 'bad"model'},
            {"agent_model": "bad'model"},
            {"agent_model": "bad\nmodel"},
            {"agent_api_key": 7},
            {"agent_api_key": "x" * 8193},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                core.save_config(values)

        saved = core.save_config({
            "agent_mode": "remote",
            "agent_url": "http://127.0.0.1:8081/v1/",
            "agent_model": " agent ",
            "agent_api_key": "x" * 8192,
        })
        self.assertEqual(saved["agent_url"], "http://127.0.0.1:8081/v1")
        self.assertEqual(saved["agent_model"], "agent")

    def test_model_name_profiles(self):
        self.assertEqual(core.model_name(), "local")
        core.save_config({"mode": "remote", "url": "https://ordinary.example/v1", "model": "ordinary"})
        self.assertEqual(core.model_name("current"), "ordinary")
        with self.assertRaisesRegex(ValueError, "agent"):
            core.model_name("agent")
        core.save_config({
            "agent_mode": "remote",
            "agent_url": "https://agent.example/v1",
            "agent_model": "agent-model",
        })
        self.assertEqual(core.model_name("agent"), "agent-model")
        core.save_config({"mode": "chatgpt"})
        with self.assertRaisesRegex(ValueError, "subscription"):
            core.model_name("current")
        with self.assertRaises(ValueError):
            core.model_name("unknown")

    def test_request_profiles_use_only_their_own_endpoint_and_key(self):
        core.save_config({
            "mode": "remote",
            "url": "https://ordinary.example/v1",
            "model": "ordinary",
            "api_key": "ordinary-secret",
            "agent_mode": "remote",
            "agent_url": "https://agent.example/v1",
            "agent_model": "agent",
            "agent_api_key": "agent-secret",
        })
        opener = Mock()
        opener.open.return_value = object()
        with patch("urllib.request.build_opener", return_value=opener):
            core.request("/models", profile="current")
            current_request = opener.open.call_args.args[0]
            self.assertEqual(current_request.full_url, "https://ordinary.example/v1/models")
            self.assertEqual(current_request.get_header("Authorization"), "Bearer ordinary-secret")
            core.request("/models", profile="agent")
            agent_request = opener.open.call_args.args[0]
            self.assertEqual(agent_request.full_url, "https://agent.example/v1/models")
            self.assertEqual(agent_request.get_header("Authorization"), "Bearer agent-secret")
            self.assertNotIn("ordinary-secret", repr(opener.open.call_args))
        with self.assertRaises(ValueError):
            core.request("/models", profile="unknown")
        core.save_config({"agent_mode": "current"})
        with self.assertRaises(ValueError):
            core.request("/models", profile="agent")

    def test_ordinary_chat_explicitly_uses_current_model_profile(self):
        core.save_config({
            "mode": "remote",
            "url": "https://ordinary.example/v1",
            "model": "ordinary-model",
            "api_key": "ordinary-secret",
            "agent_mode": "remote",
            "agent_url": "https://agent.example/v1",
            "agent_model": "agent-model",
            "agent_api_key": "agent-secret",
        })
        payload = io.BytesIO(
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        with patch("aios.core.request", return_value=payload) as request:
            self.assertEqual(
                "".join(core.chat([{"role": "user", "content": "hello"}])),
                "ok",
            )
        self.assertEqual(request.call_args.args[1]["model"], "ordinary-model")
        self.assertEqual(request.call_args.kwargs["profile"], "current")
        self.assertNotIn("agent-model", repr(request.call_args))

    def test_cli_configure_agent_flags_and_key_prompt(self):
        argv = [
            "aios-llm", "configure",
            "--agent-mode", "remote",
            "--agent-url", "https://agent.example/v1",
            "--agent-model", "agent-model",
            "--ask-agent-key",
        ]
        with patch("sys.argv", argv), patch("aios.cli.getpass.getpass", return_value="agent-secret") as prompt, patch(
            "aios.cli.save_config"
        ) as save:
            self.assertEqual(cli.main(), 0)
        prompt.assert_called_once_with("Agent API key: ")
        save.assert_called_once_with({
            "agent_mode": "remote",
            "agent_url": "https://agent.example/v1",
            "agent_model": "agent-model",
            "agent_api_key": "agent-secret",
        })

    def test_history_roundtrip_and_delete(self):
        messages = [{"role": "user", "content": "hello 世界"}]
        core.save_history(messages)
        self.assertEqual(core.load_history(), messages)
        core.save_history([])
        self.assertEqual(core.load_history(), [])

    def test_theme_colors_persist_without_changing_model_settings(self):
        self.assertEqual(core.load_config()["theme_color"], "blue")
        core.save_config({"mode": "remote", "url": "https://example.com/v1", "api_key": "private"})
        for color in core.THEME_COLORS:
            core.save_config({"theme_color": color})
            config = core.load_config()
            self.assertEqual(config["theme_color"], color)
            self.assertEqual(config["mode"], "remote")
            self.assertEqual(config["api_key"], "private")
        with self.assertRaises(ValueError):
            core.save_config({"theme_color": "unknown"})
        self.assertEqual(core.load_config()["theme_color"], "slate")

    def test_real_http_stream_and_redacted_error(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            core.save_config({"mode": "remote", "url": f"http://127.0.0.1:{server.server_port}/v1", "model": "test"})
            self.assertEqual("".join(core.chat([{"role": "user", "content": "hi"}])), "Hello 世界")
            core.save_config({"model": "bad-key"})
            with self.assertRaisesRegex(RuntimeError, "Authentication failed") as error:
                list(core.chat([{"role": "user", "content": "hi"}]))
            self.assertNotIn("secret-provider", str(error.exception))
            core.save_config({
                "agent_mode": "remote",
                "agent_url": f"http://127.0.0.1:{server.server_port}/v1",
                "agent_model": "bad-key",
                "agent_api_key": "agent-secret",
            })
            with self.assertRaisesRegex(RuntimeError, "Authentication failed") as agent_error:
                core.request(
                    "/chat/completions",
                    {"model": "bad-key"},
                    profile="agent",
                )
            self.assertNotIn("secret-provider", str(agent_error.exception))
            self.assertNotIn("agent-secret", str(agent_error.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_missing_model_rejected(self):
        with self.assertRaises(ValueError):
            core.save_config({"model_path": "/does/not/exist.gguf"})


if __name__ == "__main__":
    unittest.main()
