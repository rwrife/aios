"""Stage the pinned starter model; refuse cached or downloaded corrupt weights."""
import hashlib
import json
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'apps'))
from aios.core import download_model

cache, stage = map(Path, sys.argv[1:])
model = json.loads((root / 'apps/aios/models.json').read_text())['smollm2-135m']
weights = cache / 'models/smollm2-135m.gguf'
if weights.exists():
    with weights.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != model['sha256']:
        raise SystemExit('Cached starter model checksum mismatch: ' + str(weights))
else:
    download_model(model['url'], model['sha256'], weights)
destination = stage / 'usr/local/share/aios/models'
destination.mkdir(parents=True, exist_ok=True)
shutil.copyfile(weights, destination / weights.name)
shutil.copyfile(root / 'apps/aios/licenses/SmolLM2-Apache-2.0.txt', destination / 'LICENSE.txt')
(destination / 'MODEL.json').write_text(json.dumps(model, indent=2) + '\n')
print('Bundled verified SmolLM2 starter model (101 MiB)')
