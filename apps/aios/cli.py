import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from .core import chat, data_dir, download_model, load_config, request, save_config


def main():
    parser = argparse.ArgumentParser(description="Configure and run local or remote AIOS models")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--mode", choices=("local", "remote"), required=True)
    configure.add_argument("--url")
    configure.add_argument("--model")
    configure.add_argument("--model-path")
    configure.add_argument("--ask-key", action="store_true")
    commands.add_parser("status")
    commands.add_parser("models")
    commands.add_parser("serve")
    commands.add_parser("setup-local")
    prompt = commands.add_parser("chat")
    prompt.add_argument("prompt", nargs="?")
    download = commands.add_parser("download")
    download.add_argument("url")
    download.add_argument("--sha256", required=True)
    download.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "configure":
            values = {k: v for k, v in vars(args).items() if k in ("mode", "url", "model", "model_path") and v is not None}
            if args.ask_key:
                values["api_key"] = getpass.getpass("API key: ")
            save_config(values)
            print("Configuration saved.")
        elif args.command == "setup-local":
            model = json.loads(Path(__file__).with_name("models.json").read_text())["smollm2-135m"]
            print(model["name"] + " — " + model["license"])
            print(model["note"])
            destination = data_dir() / "models" / "smollm2-135m.gguf"
            if not destination.exists():
                download_model(model["url"], model["sha256"], destination,
                               lambda n: print(f"\r{n // 1048576} MiB", end="", file=sys.stderr))
            save_config({"mode": "local", "model_path": str(destination)})
            print("\nReady. Run aios-llm serve, then aios-llm chat in another terminal.")
        elif args.command == "serve":
            config = load_config()
            if not config["model_path"]:
                raise ValueError("Import a GGUF model with aios-llm configure --mode local --model-path PATH first.")
            os.execvp("llama-server", ["llama-server", "--model", config["model_path"], "--alias", "local", "--host", "127.0.0.1", "--port", "8080", "--ctx-size", "4096"])
        elif args.command in ("status", "models"):
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
