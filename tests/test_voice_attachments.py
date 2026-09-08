import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from aios import core, voice
from aios.attachments import read_attachment


def wav_bytes():
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(b"\0\0" * 1600)
    return data.getvalue()


class VoiceHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append((self.path, dict(self.headers), body))
        if self.headers.get("Authorization") != "Bearer voice-test":
            self.send_response(401); self.end_headers(); self.wfile.write(b"private-diagnostic"); return
        self.send_response(200); self.end_headers()
        if self.path.endswith("transcriptions"):
            self.wfile.write(json.dumps({"text": "A dictated message"}).encode())
        else:
            self.wfile.write(wav_bytes())


class VoiceAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data")})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()

    def test_voice_transport_and_separate_credentials(self):
        server = HTTPServer(("127.0.0.1", 0), VoiceHandler); server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1"
            core.save_config({"voice_url": url, "voice_key": "voice-test", "api_key": "chat-only"})
            recording = self.root / "recording.wav"; recording.write_bytes(wav_bytes())
            self.assertEqual(voice.transcribe(recording), "A dictated message")
            route, headers, body = server.requests[0]
            self.assertEqual(route, "/v1/audio/transcriptions")
            self.assertIn('multipart/form-data; boundary=', headers["Content-Type"])
            self.assertIn(b'filename="recording.wav"', body)
            self.assertNotIn(b"chat-only", body)
            output = self.root / "speech.wav"
            voice.synthesize("Hello", output)
            self.assertEqual(output.read_bytes(), wav_bytes())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            core.save_config({"voice_key": "wrong"})
            with self.assertRaisesRegex(RuntimeError, "authentication") as error:
                voice.transcribe(recording)
            self.assertNotIn("private-diagnostic", str(error.exception))
            core.save_config({"voice_url": "https://another.example/v1"})
            self.assertEqual(core.load_config()["voice_key"], "")
            self.assertEqual(core.load_config()["api_key"], "chat-only")
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_attachment_content_and_limits(self):
        text = self.root / "hello.py"; text.write_text("print('hello 世界')")
        self.assertEqual(read_attachment(text), ("hello.py", "print('hello 世界')"))
        text.write_bytes(b"a\x00b")
        with self.assertRaisesRegex(ValueError, "readable text"):
            read_attachment(text)
        text.write_bytes(b"x" * (256 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "too much text"):
            read_attachment(text)

    def test_voice_requires_explicit_endpoint_and_valid_settings(self):
        with self.assertRaisesRegex(ValueError, "Set up voice"):
            voice.voice_request("/audio/speech", b"{}", "application/json")
        for values in ({"voice_url": "http://remote.example/v1"}, {"stt_model": 'bad"\r\nmodel'}, {"voice_mode": "invalid"}):
            with self.assertRaises(ValueError):
                core.save_config(values)
        with self.assertRaisesRegex(ValueError, "too long"):
            voice.synthesize("x" * 4001, self.root / "speech.wav")


if __name__ == "__main__":
    unittest.main()
