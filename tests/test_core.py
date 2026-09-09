import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from aios import core


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
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_missing_model_rejected(self):
        with self.assertRaises(ValueError):
            core.save_config({"model_path": "/does/not/exist.gguf"})


if __name__ == "__main__":
    unittest.main()
