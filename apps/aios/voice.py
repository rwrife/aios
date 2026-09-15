"""Push-to-talk transcription and optional synthesized replies."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from functools import partial
from .core import load_config, validate_url, NoRedirect, data_dir, download_model, save_config
from .cpu_features import ensure_supported

SPEECH_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-tiny.en.bin"
SPEECH_SHA256 = "921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f"
MAX_AUDIO = 20 * 1024 * 1024


def child_lifetime(parent):
    # Stop local speech subprocesses if the requesting worker is cancelled.
    import ctypes
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)
    if os.getppid() != parent:
        os._exit(1)


def local_command(args, **kwargs):
    try:
        return subprocess.run(args, check=True, timeout=120, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, preexec_fn=partial(child_lifetime, os.getpid()), **kwargs)
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError("Local voice failed. Check the speech model and available memory.") from None


def voice_request(route, data, content_type):
    config = load_config()
    if not config["voice_url"]:
        raise ValueError("Set up voice in the chat options first.")
    base = validate_url(config["voice_url"])
    headers = {"Content-Type": content_type}
    if config["voice_key"]:
        headers["Authorization"] = "Bearer " + config["voice_key"]
    request = urllib.request.Request(base + route, data=data, headers=headers)
    try:
        return urllib.request.build_opener(NoRedirect).open(request, timeout=90)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise RuntimeError("Voice authentication failed. Check the voice API key.") from None
        raise RuntimeError(f"Voice service returned HTTP {error.code}. Check its URL and model settings.") from None
    except (urllib.error.URLError, OSError):
        raise RuntimeError("Cannot reach the voice service.") from None


def setup_local_voice(progress=lambda n: None):
    # Do not download a speech model the bundled whisper-cli cannot execute.
    ensure_supported()
    path = data_dir() / "models" / "whisper-tiny.en.bin"
    if not path.exists():
        download_model(SPEECH_URL, SPEECH_SHA256, path, progress, suffix=".bin")
    save_config({"voice_mode": "local", "speech_model_path": str(path)})
    return str(path)


def transcribe(path):
    path = Path(path)
    if not path.is_file() or not 44 < path.stat().st_size <= MAX_AUDIO:
        raise ValueError("Record a short message, up to 60 seconds.")
    config = load_config()
    if config["voice_mode"] == "local":
        model = Path(config["speech_model_path"])
        if not config["speech_model_path"] or not model.is_file():
            raise ValueError("Download or select a local speech model in Voice settings.")
        # whisper-cli shares the bundled build's instruction-set floor.
        ensure_supported()
        with tempfile.TemporaryDirectory(prefix="aios-stt-") as directory:
            output = str(Path(directory) / "transcript")
            local_command(["whisper-cli", "-m", str(model), "-f", str(path), "-l", "en", "-otxt", "-of", output])
            text = Path(output + ".txt").read_text().replace("[BLANK_AUDIO]", "").strip()
    else:
        boundary = "aios-" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{config["stt_model"]}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recording.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        with voice_request("/audio/transcriptions", body, "multipart/form-data; boundary=" + boundary) as response:
            result = json.loads(response.read(1024 * 1024))
        text = result.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("No speech was recognized. Try again.")
    return text.strip()


def synthesize(text, destination):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("There is no reply to read yet.")
    if len(text) > 4000:
        raise ValueError("This reply is too long to read aloud. Select a shorter reply.")
    config = load_config()
    if config["voice_mode"] == "local":
        local_command(["espeak-ng", "-w", str(destination), "--stdin"], input=text.encode())
    else:
        body = {"model": config["tts_model"], "input": text, "voice": config["voice_name"], "response_format": "wav"}
        with voice_request("/audio/speech", json.dumps(body).encode(), "application/json") as response:
            audio = response.read(MAX_AUDIO + 1)
        if len(audio) > MAX_AUDIO or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise RuntimeError("The voice service did not return a supported WAV recording.")
        Path(destination).write_bytes(audio)
    Path(destination).chmod(0o600)
    return str(destination)
