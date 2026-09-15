import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from .core import chat, download_model, load_config, request, save_config


def main():
    parser = argparse.ArgumentParser(description="Configure and run local or remote AIOS models")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--mode", choices=("local", "remote", "chatgpt"))
    configure.add_argument('--subscription-model')
    configure.add_argument("--url")
    configure.add_argument("--model")
    configure.add_argument("--model-path")
    configure.add_argument("--ask-key", action="store_true")
    configure.add_argument("--agent-mode", choices=("current", "chatgpt", "remote"))
    configure.add_argument("--agent-url")
    configure.add_argument("--agent-model")
    configure.add_argument("--ask-agent-key", action="store_true")
    configure.add_argument("--voice-mode", choices=("local", "remote"))
    for name in ("voice-url", "stt-model", "tts-model", "voice-name", "speech-model-path"):
        configure.add_argument("--" + name)
    configure.add_argument("--ask-voice-key", action="store_true")
    commands.add_parser("status")
    subscription = commands.add_parser('subscription')
    subscription.add_argument('operation', choices=('login', 'logout', 'status'))
    subscription.add_argument('--device', action='store_true')
    commands.add_parser("models")
    commands.add_parser("serve")
    setup = commands.add_parser("setup-local")
    setup.add_argument("model_id", nargs="?", default="qwen3-0.6b")
    commands.add_parser("local-models")
    commands.add_parser("setup-voice")
    transcription = commands.add_parser("transcribe")
    transcription.add_argument("file")
    speech = commands.add_parser("speak")
    speech.add_argument("text")
    speech.add_argument("--output", required=True)
    prompt = commands.add_parser("chat")
    prompt.add_argument("prompt", nargs="?")
    download = commands.add_parser("download")
    download.add_argument("url")
    download.add_argument("--sha256", required=True)
    download.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "configure":
            values = {k: v for k, v in vars(args).items() if k in ("mode", "url", "model", "model_path", "subscription_model", "agent_mode", "agent_url", "agent_model", "voice_mode", "voice_url", "stt_model", "tts_model", "voice_name", "speech_model_path") and v is not None}
            if args.ask_key:
                values["api_key"] = getpass.getpass("API key: ")
            if args.ask_agent_key:
                values["agent_api_key"] = getpass.getpass("Agent API key: ")
            if args.ask_voice_key:
                values["voice_key"] = getpass.getpass("Voice API key: ")
            save_config(values)
            print("Configuration saved.")
        elif args.command == 'subscription':
            from .subscription import account_action
            account_action(args.operation, lambda kind, **data: print(json.dumps({'type': kind, **data}), flush=True), args.device)
        elif args.command == "setup-voice":
            from .voice import setup_local_voice
            print("Whisper tiny.en · MIT · English speech recognition")
            print(setup_local_voice(lambda n: print(f"\r{n // 1048576} MiB", end="", file=sys.stderr)))
        elif args.command == "transcribe":
            from .voice import transcribe
            print(transcribe(args.file))
        elif args.command == "speak":
            from .voice import synthesize
            destination = Path(args.output).expanduser()
            if destination.exists():
                raise ValueError("Choose a new output file.")
            print(synthesize(args.text, destination))
        elif args.command == "setup-local":
            from .local_models import install
            install(args.model_id, lambda text: print(text, file=sys.stderr))
            print("Ready. Run aios-llm serve, then aios-llm chat in another terminal.")
        elif args.command == "local-models":
            from .local_models import list_models
            print(json.dumps(list_models(), indent=2))
        elif args.command == "serve":
            from .cpu_features import ensure_supported
            config = load_config()
            if not config["model_path"]:
                raise ValueError("Import a GGUF model with aios-llm configure --mode local --model-path PATH first.")
            # The bundled llama-server would die with SIGILL below the build's
            # instruction-set floor; refuse with an explanation instead.
            ensure_supported()
            os.execvp("llama-server", ["llama-server", "--model", config["model_path"], "--alias", "local", "--host", "127.0.0.1", "--port", "8080", "--ctx-size", "8192", "--jinja", "--chat-template-kwargs", '{"enable_thinking":false}'])
        elif args.command in ("status", "models"):
            if load_config()['mode'] == 'chatgpt':
                from .subscription import account_action
                account_action('status', lambda kind, **data: print(json.dumps(data, indent=2)))
            else:
                with request("/models", timeout=5) as response:
                    print(json.dumps(json.load(response), indent=2))
        elif args.command == "download":
            download_model(args.url, args.sha256, args.output, lambda n: print(f"\r{n // 1048576} MiB", end="", file=sys.stderr))
            print("\nModel verified and saved.")
        elif args.command == "chat":
            prompt = args.prompt or input("You: ")
            for token in chat([{"role": "user", "content": prompt}]):
                print(token, end="", flush=True)
            print()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
