"""Configuration, history and an OpenAI-compatible streaming transport."""
import hashlib
import json
import os
import tempfile
import shutil
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from .principals import current as current_principal

BUNDLED_MODEL = Path("/usr/local/share/aios/models/smollm2-135m.gguf")
THEME_COLORS = ("blue", "teal", "sage", "amber", "copper", "rose", "violet", "slate")


def config_dir():
    principal = current_principal()
    if principal is not None:
        return principal.directory('config')
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "aios"


def data_dir():
    principal = current_principal()
    if principal is not None:
        return principal.directory('data')
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "aios"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temp = Path(name)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    path.chmod(0o600)


def load_config():
    defaults = {"mode": "local", "url": "http://127.0.0.1:8080/v1", "model": "local",
                "model_path": "", "api_key": "", "subscription_model": "", "reduced_motion": True, "theme_color": "blue",
                "voice_mode": "remote", "voice_url": "", "voice_key": "",
                "stt_model": "whisper-1", "tts_model": "tts-1", "voice_name": "alloy",
                "speech_model_path": ""}
    path = config_dir() / "config.json"
    if path.exists():
        defaults.update(json.loads(path.read_text()))
    if defaults["mode"] == "local" and not defaults["model_path"] and BUNDLED_MODEL.is_file():
        defaults["model_path"] = str(BUNDLED_MODEL)
    return defaults


def validate_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use an HTTP(S) endpoint without embedded credentials.")
    if parts.query or parts.fragment:
        raise ValueError("The endpoint must not include a query or fragment.")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote endpoints require HTTPS. HTTP is allowed only on this machine.")
    return url.rstrip("/")


def save_config(values):
    config = load_config()
    if "url" in values and values["url"].rstrip("/") != config["url"].rstrip("/") and "api_key" not in values:
        config["api_key"] = ""
    if "voice_url" in values and values["voice_url"].rstrip("/") != config["voice_url"].rstrip("/") and "voice_key" not in values:
        config["voice_key"] = ""
    for key in defaults_keys():
        if key in values:
            config[key] = values[key]
    if config["mode"] not in ("local", "remote", "chatgpt"):
        raise ValueError("Choose local, remote, or ChatGPT subscription.")
    if not isinstance(config['subscription_model'], str) or len(config['subscription_model']) > 200:
        raise ValueError('Choose a valid ChatGPT model.')
    if config["theme_color"] not in THEME_COLORS:
        raise ValueError("Choose one of the available theme colors.")
    config["url"] = validate_url(str(config["url"]))
    if config["voice_mode"] not in ("local", "remote"):
        raise ValueError("Choose local or remote voice.")
    if config["voice_url"]:
        config["voice_url"] = validate_url(str(config["voice_url"]))
    for key in ("stt_model", "tts_model", "voice_name"):
        if not isinstance(config[key], str) or not config[key].strip() or any(c in config[key] for c in '\r\n"'):
            raise ValueError("Enter a valid voice model and voice name.")
    if not str(config["model"]).strip():
        raise ValueError("Enter a model ID.")
    if config["model_path"] and config["mode"] == "local":
        path = Path(config["model_path"]).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() != ".gguf":
            raise ValueError("Choose an existing GGUF model file.")
        config["model_path"] = str(path)
    write_json(config_dir() / "config.json", config)
    return config


def defaults_keys():
    return ("mode", "url", "model", "model_path", "api_key", "subscription_model", "reduced_motion", "theme_color",
            "voice_mode", "voice_url", "voice_key", "stt_model", "tts_model", "voice_name", "speech_model_path")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never forward an Authorization header to a different endpoint.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(route, body=None, timeout=90):
    config = load_config()
    if config['mode'] == 'chatgpt':
        raise ValueError('ChatGPT subscriptions use the subscription connection, not an API endpoint.')
    base = "http://127.0.0.1:8080/v1" if config["mode"] == "local" else validate_url(config["url"])
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if config["mode"] == "remote" and config["api_key"]:
        headers["Authorization"] = "Bearer " + config["api_key"]
    req = urllib.request.Request(base + route, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        return urllib.request.build_opener(NoRedirect).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        exc.close()
        reasons = {401: "Authentication failed. Check your API key.", 403: "This model is not permitted.",
                   404: "Endpoint or model not found. Check the URL and model ID.",
                   429: "The provider is busy or rate limited. Try again shortly.",
                   503: "The model is loading or unavailable. Try again shortly."}
        raise RuntimeError(reasons.get(exc.code, f"The model endpoint returned HTTP {exc.code}.")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("Cannot reach the model. Check your connection and model service.") from None


def sse_events(stream):
    # HTTPResponse iterates complete lines even when TCP splits a UTF-8 character.
    data = []
    for raw in stream:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


def chat(messages):
    config = load_config()
    if config['mode'] == 'chatgpt':
        from .subscription import chat as subscription_chat
        for event in subscription_chat(messages):
            if event['type'] == 'token':
                yield event['text']
        return
    if not messages or any(m.get("role") not in ("user", "assistant", "system") or
                           not isinstance(m.get("content"), str) for m in messages):
        raise ValueError("Invalid conversation.")
    model = "local" if config["mode"] == "local" else config["model"]
    messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    finished = False
    with request("/chat/completions", {"model": model, "messages": messages, "stream": True}) as response:
        for event in sse_events(response):
            if event == "[DONE]":
                return
            value = json.loads(event)
            if value.get("error"):
                raise RuntimeError("The model could not complete this response.")
            for choice in value.get("choices", []):
                if choice.get("finish_reason"):
                    finished = True
                token = choice.get("delta", {}).get("content")
                if isinstance(token, str):
                    yield token
    if not finished:
        raise RuntimeError("The connection ended before the reply completed. Try again.")


def load_history():
    path = data_dir() / "conversation.json"
    return json.loads(path.read_text()) if path.exists() else []


def save_history(messages):
    write_json(data_dir() / "conversation.json", messages)


def download_model(url, expected_hash, destination, progress=lambda n: None, suffix=".gguf"):
    validate_url(url)
    if not url.startswith("https://") or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash.lower()):
        raise ValueError("Model downloads require HTTPS and a SHA-256 checksum.")
    destination = Path(destination).expanduser()
    if suffix not in (".gguf", ".bin") or destination.suffix.lower() != suffix or destination.exists():
        raise ValueError("Choose a new " + suffix + " destination file.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(suffix + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as output:
            size = int(response.headers.get("Content-Length", 0))
            if size and shutil.disk_usage(destination.parent).free < size + 64 * 1024 * 1024:
                raise ValueError("There is not enough free space for this model.")
            count = 0
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                count += len(chunk)
                progress(count)
        if digest.hexdigest() != expected_hash.lower():
            raise ValueError("Model checksum did not match. The download was discarded.")
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
