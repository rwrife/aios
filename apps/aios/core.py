"""Configuration, history and an OpenAI-compatible streaming transport."""
import contextvars
import hashlib
import json
import os
import re
import tempfile
import shutil
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from .principals import current as current_principal

BUNDLED_MODEL = Path("/usr/local/share/aios/models/qwen3-0.6b.gguf")
THEME_COLORS = ("blue", "teal", "sage", "amber", "copper", "rose", "violet", "slate")
SAFE_AGENT_KEY_ERROR = "Choose a valid agent API key."
SAFE_AGENT_KEY_CONFIG_ERROR = "The agent API key configuration is invalid."
SAFE_REQUEST_VALUE_ERROR = "The model request could not be constructed safely."
MOTION_MIN_CPU_CORES = 2
MOTION_MIN_MEMORY_BYTES = 4 * 1024 ** 3
LOCAL_CONNECT_RETRY_SECONDS = 30
LOCAL_CONNECT_RETRY_DELAY = 0.25
_DEBUG_SINK = contextvars.ContextVar("aios_debug_sink", default=None)


@contextmanager
def capture_debug(sink):
    token = _DEBUG_SINK.set(sink)
    try:
        yield
    finally:
        _DEBUG_SINK.reset(token)


def debug_event(kind, data):
    sink = _DEBUG_SINK.get()
    if sink is not None:
        sink(kind, data)


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


def total_memory_bytes():
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return 0


def motion_enabled_by_default():
    return (os.cpu_count() or 0) >= MOTION_MIN_CPU_CORES and total_memory_bytes() >= MOTION_MIN_MEMORY_BYTES


def load_config():
    defaults = {"mode": "local", "url": "http://127.0.0.1:8080/v1", "model": "local",
                "model_path": "", "api_key": "", "subscription_model": "", "theme_color": "blue",
                "agent_mode": "current", "agent_url": "", "agent_model": "", "agent_api_key": "",
                "voice_mode": "remote", "voice_url": "", "voice_key": "",
                "stt_model": "whisper-1", "tts_model": "tts-1", "voice_name": "alloy",
                "speech_model_path": "", "camera_recognition": False, "camera_device": ""}
    path = config_dir() / "config.json"
    if path.exists():
        defaults.update(json.loads(path.read_text()))
    defaults.setdefault("reduced_motion", not motion_enabled_by_default())
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


def _validate_agent_api_key(value, message):
    if (not isinstance(value, str) or len(value) > 8192
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(message)
    return value


def save_config(values):
    config = load_config()
    if "url" in values and isinstance(values["url"], str) and values["url"].rstrip("/") != str(config["url"]).rstrip("/") and "api_key" not in values:
        config["api_key"] = ""
    if "agent_url" in values and isinstance(values["agent_url"], str) and values["agent_url"].rstrip("/") != str(config["agent_url"]).rstrip("/") and "agent_api_key" not in values:
        config["agent_api_key"] = ""
    if "voice_url" in values and isinstance(values["voice_url"], str) and values["voice_url"].rstrip("/") != str(config["voice_url"]).rstrip("/") and "voice_key" not in values:
        config["voice_key"] = ""
    for key in defaults_keys():
        if key in values:
            config[key] = values[key]
    if config["mode"] not in ("local", "remote", "chatgpt"):
        raise ValueError("Choose local, remote, or ChatGPT subscription.")
    if not isinstance(config['subscription_model'], str) or len(config['subscription_model']) > 200:
        raise ValueError('Choose a valid ChatGPT model.')
    if config["agent_mode"] not in ("current", "chatgpt", "remote"):
        raise ValueError("Choose current, ChatGPT, or remote agent routing.")
    if not isinstance(config["agent_url"], str):
        raise ValueError("Choose a valid agent endpoint.")
    if not isinstance(config["agent_model"], str) or len(config["agent_model"]) > 200:
        raise ValueError("Choose a valid agent model.")
    config["agent_model"] = config["agent_model"].strip()
    if any(char in config["agent_model"] for char in "\r\n\"'"):
        raise ValueError("Choose a valid agent model.")
    _validate_agent_api_key(config["agent_api_key"], SAFE_AGENT_KEY_ERROR)
    if config["agent_url"]:
        config["agent_url"] = validate_url(config["agent_url"])
    if config["agent_mode"] == "remote" and (not config["agent_url"] or not config["agent_model"]):
        raise ValueError("Remote agent routing requires an endpoint and model.")
    if config["theme_color"] not in THEME_COLORS:
        raise ValueError("Choose one of the available theme colors.")
    config["url"] = validate_url(str(config["url"]))
    if config["voice_mode"] not in ("local", "remote"):
        raise ValueError("Choose local or remote voice.")
    if config["voice_url"]:
        config["voice_url"] = validate_url(str(config["voice_url"]))
    if type(config["camera_recognition"]) is not bool:
        raise ValueError("Camera recognition must be enabled or disabled.")
    device = config["camera_device"]
    if not isinstance(device, str) or len(device) > 512 or (
            device and not re.fullmatch(r'/dev/v4l/by-id/[A-Za-z0-9._:+-]+-video-index[0-9]+', device)):
        raise ValueError("Choose a stable local camera path under /dev/v4l/by-id.")
    if config["camera_recognition"] and not device:
        raise ValueError("Choose a stable local camera before enabling recognition.")
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
    if "theme_color" in values:
        from .terminal_theme import apply_chrome
        apply_chrome(config["theme_color"])
    return config


def defaults_keys():
    return ("mode", "url", "model", "model_path", "api_key", "subscription_model", "reduced_motion", "theme_color",
            "agent_mode", "agent_url", "agent_model", "agent_api_key",
            "voice_mode", "voice_url", "voice_key", "stt_model", "tts_model", "voice_name", "speech_model_path",
            "camera_recognition", "camera_device")


def _remote_agent_settings(config):
    if config.get("agent_mode") != "remote":
        raise ValueError("The agent model profile is not configured for a remote endpoint.")
    url = config.get("agent_url")
    model = config.get("agent_model")
    key = config.get("agent_api_key", "")
    if not isinstance(url, str) or not url:
        raise ValueError("The remote agent profile requires an endpoint and model.")
    if (not isinstance(model, str) or not model.strip() or len(model) > 200
            or any(char in model for char in "\r\n\"'")):
        raise ValueError("The remote agent profile requires a valid endpoint and model.")
    _validate_agent_api_key(key, SAFE_AGENT_KEY_CONFIG_ERROR)
    return validate_url(url), model.strip(), key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never forward an Authorization header to a different endpoint.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def model_name(profile="current"):
    config = load_config()
    if profile == "current":
        if config["mode"] == "chatgpt":
            raise ValueError("ChatGPT subscriptions use the subscription connection, not this model transport.")
        if config["mode"] == "local":
            return "local"
        if config["mode"] == "remote" and isinstance(config["model"], str) and config["model"].strip():
            return config["model"]
        raise ValueError("The current model configuration is invalid.")
    if profile == "agent":
        _, model, _ = _remote_agent_settings(config)
        return model
    raise ValueError("Unknown model profile.")


def request(route, body=None, timeout=90, profile="current"):
    config = load_config()
    local = False
    if profile == "current":
        if config["mode"] == "chatgpt":
            raise ValueError("ChatGPT subscriptions use the subscription connection, not an API endpoint.")
        if config["mode"] not in ("local", "remote"):
            raise ValueError("The current model configuration is invalid.")
        local = config["mode"] == "local"
        base = "http://127.0.0.1:8080/v1" if local else validate_url(config["url"])
        key = config["api_key"] if config["mode"] == "remote" else ""
    elif profile == "agent":
        base, _, key = _remote_agent_settings(config)
    else:
        raise ValueError("Unknown model profile.")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        req = urllib.request.Request(
            base + route,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        opener = urllib.request.build_opener(NoRedirect)
    except ValueError:
        raise RuntimeError(SAFE_REQUEST_VALUE_ERROR) from None
    deadline = time.monotonic() + min(timeout, LOCAL_CONNECT_RETRY_SECONDS)
    while True:
        try:
            return opener.open(req, timeout=timeout)
        except ValueError:
            raise RuntimeError(SAFE_REQUEST_VALUE_ERROR) from None
        except urllib.error.HTTPError as exc:
            retry = local and exc.code == 503 and time.monotonic() < deadline
            exc.close()
            if retry:
                time.sleep(LOCAL_CONNECT_RETRY_DELAY)
                continue
            reasons = {401: "Authentication failed. Check your API key.", 403: "This model is not permitted.",
                       404: "Endpoint or model not found. Check the URL and model ID.",
                       429: "The provider is busy or rate limited. Try again shortly.",
                       503: "The model is loading or unavailable. Try again shortly."}
            raise RuntimeError(reasons.get(exc.code, f"The model endpoint returned HTTP {exc.code}.")) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if local and time.monotonic() < deadline:
                time.sleep(LOCAL_CONNECT_RETRY_DELAY)
                continue
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


def chat(messages, profile="current"):
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
    model = model_name(profile)
    messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    finished = False
    body = {"model": model, "messages": messages, "stream": True}
    debug_event("llm.request", body)
    with request("/chat/completions", body, profile=profile) as response:
        for event in sse_events(response):
            debug_event("llm.response", event)
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
