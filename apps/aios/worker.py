"""One request per process. JSON lines keep UI text separate from commands."""
import json
import signal
import sys
from pathlib import Path
from .core import chat, load_config, load_history, save_config, save_history


def emit(kind, **values):
    print(json.dumps({"type": kind, **values}), flush=True)


def handle(request):
    action = request["action"]
    if action == "load":
        config = load_config()
        config["has_key"] = bool(config.pop("api_key"))
        config["has_voice_key"] = bool(config.pop("voice_key"))
        config["has_agent_key"] = bool(config.pop("agent_api_key"))
        mode_file = Path("/etc/aios-mode")
        config["live"] = mode_file.exists() and mode_file.read_text().strip() == "live"
        from .local_models import list_models
        emit("loaded", config=config, messages=load_history(), local_models=list_models())
    elif action == "configure":
        save_config(request["config"])
        emit("saved")
    elif action == "setup-local":
        from .local_models import install, list_models
        destination = install(request.get("model_id", "smollm2-135m"),
                              lambda text: emit("progress", text=text))
        emit("installed", path=str(destination), local_models=list_models())
    elif action == "local-models":
        from .local_models import list_models
        emit("local-models", local_models=list_models())
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
        tool_socket = request.get("tool_socket")
        if isinstance(tool_socket, str) and tool_socket:
            from .agent import chat as agent_chat
            for event in agent_chat(request["messages"], tool_socket):
                print(json.dumps(event), flush=True)
        else:
            for text in chat(request["messages"]):
                emit("token", text=text)
        emit("done")
    else:
        raise ValueError("Unknown action")


def main():
    # A graceful Stop unwinds account cancellation and subprocess cleanup.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        handle(json.loads(sys.stdin.readline()))
    except Exception as exc:
        # Network helpers intentionally exclude server bodies and credentials.
        emit("error", text=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "Unable to complete the request. Check configuration and available storage.")
        sys.exit(1)


if __name__ == "__main__":
    main()
