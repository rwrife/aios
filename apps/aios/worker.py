"""One request per process. JSON lines keep UI text separate from commands."""
import json
import sys
import signal
from pathlib import Path
from .core import chat, load_config, load_history, save_config, save_history, data_dir, download_model, BUNDLED_MODEL


def emit(kind, **values):
    print(json.dumps({"type": kind, **values}), flush=True)


# A graceful Stop unwinds account cancellation and subprocess cleanup.
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


try:
    request = json.loads(sys.stdin.readline())
    action = request["action"]
    if action == "load":
        config = load_config()
        config["has_key"] = bool(config.pop("api_key"))
        config["has_voice_key"] = bool(config.pop("voice_key"))
        config["has_agent_key"] = bool(config.pop("agent_api_key"))
        mode_file = Path("/etc/aios-mode")
        config["live"] = mode_file.exists() and mode_file.read_text().strip() == "live"
        emit("loaded", config=config, messages=load_history())
    elif action == "configure":
        save_config(request["config"])
        emit("saved")
    elif action == "setup-local":
        model = json.loads(Path(__file__).with_name("models.json").read_text())["smollm2-135m"]
        destination = BUNDLED_MODEL if BUNDLED_MODEL.is_file() else data_dir() / "models" / "smollm2-135m.gguf"
        if not destination.exists():
            download_model(model["url"], model["sha256"], destination,
                           lambda n: emit("progress", text=f"Downloading starter model: {n // 1048576} / 101 MiB"))
        save_config({"mode": "local", "model_path": str(destination)})
        emit("installed", path=str(destination))
    elif action == "history":
        save_history(request["messages"])
        emit("saved")
    elif action == "transcribe":
        from .voice import transcribe
        emit("transcribed", text=transcribe(request["path"]))
    elif action == "speak":
        from .voice import synthesize
        emit("spoken", path=synthesize(request["text"], request["path"]))
    elif action == "setup-voice":
        from .voice import setup_local_voice
        path = setup_local_voice(lambda n: emit("progress", text=f"Downloading speech model: {n // 1048576} / 75 MiB"))
        emit("voice-installed", path=path)
    elif action == "attachment":
        from .attachments import read_attachment
        name, text = read_attachment(request["path"])
        emit("attached", name=name, text=text)
    elif action == 'subscription':
        from .subscription import account_action
        account_action(request['operation'], emit, request.get('device', False))
    elif action == "chat":
        if load_config()['mode'] == 'chatgpt':
            from .subscription import chat as subscription_chat
            for event in subscription_chat(request['messages'], request.get('browser_socket')):
                print(json.dumps(event), flush=True)
        elif request.get("browser_socket"):
            from .agent import chat as agent_chat
            for event in agent_chat(request["messages"], request["browser_socket"]):
                print(json.dumps(event), flush=True)
        else:
            for text in chat(request["messages"]):
                emit("token", text=text)
        emit("done")
    else:
        raise ValueError("Unknown action")
except Exception as exc:
    # Network helpers intentionally exclude server bodies and credentials.
    emit("error", text=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Unable to complete the request. Check configuration and available storage.")
    sys.exit(1)
