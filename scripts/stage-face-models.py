"""Build-time pinned artifacts only. Never create calibration or approval."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'apps/aios/face-models.lock.json'
DESTINATION = 'usr/local/share/aios/face-models'


def verify(path, record):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != record['bytes']:
        raise ValueError('Face model size or file type mismatch')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
            raise ValueError('Face model checksum mismatch')


def stage(cache, destination, enabled):
    target = destination / DESTINATION
    # A default build may reuse an identity build's staging directory.
    if target.is_symlink():
        raise ValueError('Invalid face model staging directory')
    if target.exists():
        shutil.rmtree(target)
    if not enabled:
        return
    lock = json.loads(LOCK.read_text())
    target.mkdir(parents=True)
    cache = cache / 'face-models'
    cache.mkdir(parents=True, exist_ok=True)
    for name in ('yunet', 'sface'):
        record = lock[name]
        weight = cache / Path(record['path']).name
        if not weight.exists():
            partial = weight.with_suffix('.part')
            try:
                with urllib.request.urlopen(record['source'], timeout=60) as source, partial.open('wb') as output:
                    remaining = record['bytes'] + 1
                    while remaining:
                        block = source.read(min(1048576, remaining))
                        if not block:
                            break
                        output.write(block)
                        remaining -= len(block)
                verify(partial, record)
                partial.replace(weight)
            finally:
                partial.unlink(missing_ok=True)
        verify(weight, record)
        shutil.copyfile(weight, target / weight.name)
        shutil.copyfile(ROOT / 'apps/aios/licenses' / record['license_file'], target / record['license_file'])
    shutil.copyfile(LOCK, target / 'models.lock.json')
    for path in target.iterdir():
        path.chmod(0o644)
    target.chmod(0o755)
    print('Bundled checksum-verified experimental YuNet/SFace; calibration and approval absent')


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[3] not in ('0', '1'):
        raise SystemExit('Usage: stage-face-models.py CACHE STAGE 0|1')
    stage(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == '1')
