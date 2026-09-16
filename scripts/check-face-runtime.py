"""Synthetic, camera-free compatibility test. No accuracy or approval claim."""
import argparse
import json
from pathlib import Path
import resource
import time

import cv2
import numpy as np
from aios.biometrics import FaceEncoder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('/usr/local/share/aios/face-models'))
    args = parser.parse_args()
    manifest = json.loads((args.directory / 'models.lock.json').read_text())
    for name in ('yunet', 'sface'):
        manifest[name]['path'] = str((args.directory / Path(manifest[name]['path']).name).resolve())
    started = time.monotonic()
    encoder = FaceEncoder(manifest)
    load_seconds = time.monotonic() - started
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    crop = np.zeros((112, 112, 3), dtype=np.uint8)
    latencies = []
    for _ in range(4):
        started = time.monotonic()
        assert encoder.encode(blank) == [], 'Blank image must not detect a face'
        feature = encoder.encoder.feature(crop)
        assert feature.shape == (1, 128) and np.isfinite(feature).all()
        del feature
        latencies.append(time.monotonic() - started)
    print(json.dumps({'kind': 'synthetic-runtime-only', 'opencv': cv2.__version__,
                      'load_seconds': round(load_seconds, 4),
                      'cold_seconds': round(latencies[0], 4),
                      'warm_seconds': [round(x, 4) for x in latencies[1:]],
                      'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      'accuracy_measured': False, 'approval_created': False}))


if __name__ == '__main__':
    main()
