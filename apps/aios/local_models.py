"""Curated, checksum-pinned local models and installation preflight."""
import hashlib
import json
import shutil
from pathlib import Path

from . import core

GIB = 1024 ** 3
DISK_RESERVE = 512 * 1024 ** 2
DEFAULT_MODEL = "qwen3-0.6b"


def catalog():
    return json.loads(Path(__file__).with_name("models.json").read_text())


def memory_bytes():
    # MemTotal reflects RAM assigned to the guest, not the host or swap.
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return None


def destination(model_id):
    if model_id == DEFAULT_MODEL and core.BUNDLED_MODEL.is_file():
        return core.BUNDLED_MODEL
    return core.data_dir() / "models" / (model_id + ".gguf")


def free_disk():
    path = core.data_dir()
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def availability(model_id, model, ram, disk):
    installed = destination(model_id).is_file()
    required = 0 if installed else model["bytes"] + DISK_RESERVE
    reason = ""
    # Allow the small kernel reservation on a VM configured with exactly this RAM.
    if ram is not None and ram < model["ram_gib"] * GIB * 0.95:
        reason = f"Needs about {model['ram_gib']} GiB total RAM."
    if disk < required:
        reason += (" " if reason else "") + "Not enough free disk space."
    return {**model, "id": model_id, "installed": installed,
            "required_disk_bytes": required, "available": not reason, "reason": reason}


def list_models():
    ram, disk = memory_bytes(), free_disk()
    return {"models": [availability(key, model, ram, disk) for key, model in catalog().items()],
            "ram_bytes": ram, "free_disk_bytes": disk}


def install(model_id=DEFAULT_MODEL, progress=lambda text: None):
    models = catalog()
    if not isinstance(model_id, str) or model_id not in models:
        raise ValueError("Choose a model from the local model catalog.")
    model = models[model_id]
    state = availability(model_id, model, memory_bytes(), free_disk())
    if not state["available"]:
        raise ValueError(state["reason"] + " Close apps or increase VM resources, then refresh.")
    path = destination(model_id)
    if path.is_file():
        progress("Verifying " + model["name"] + "…")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != model["sha256"]:
            raise ValueError("Saved model checksum did not match. Remove the damaged model file and download again.")
    else:
        core.download_model(model["url"], model["sha256"], path,
                            lambda n: progress(f"Downloading {model['name']}: {n // 1048576} / {(model['bytes'] + 1048575) // 1048576} MiB"))
    core.save_config({"mode": "local", "model_path": str(path)})
    return path
