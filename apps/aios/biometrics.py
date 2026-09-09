"""Optional local sensor adapters. No model download and no raw media storage.

Thresholds and model contracts must be supplied by an administrator after target
hardware calibration. Absence of liveness is not treated as successful liveness.
"""
import hashlib
import math
from pathlib import Path
import time

from .identity import match


def verified_model(manifest, name):
    record = manifest[name]
    for field in ('path', 'sha256', 'source', 'license', 'revision', 'input', 'output'):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError("Incomplete model manifest")
    path = Path(record['path'])
    if not path.is_absolute():
        raise ValueError("Model path must be absolute")
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            digest.update(block)
    if digest.hexdigest() != record['sha256']:
        raise ValueError("Model integrity check failed")
    return str(path)


class FaceEncoder:
    def __init__(self, manifest, minimum_size=80):
        import cv2
        self.cv = cv2
        self.minimum_size = minimum_size
        self.detector = cv2.FaceDetectorYN.create(verified_model(manifest, 'yunet'), '', (640, 480))
        self.encoder = cv2.FaceRecognizerSF.create(verified_model(manifest, 'sface'), '')

    def encode(self, frame):
        height, width = frame.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(frame)
        result = []
        for face in ([] if faces is None else faces):
            if min(face[2], face[3]) < self.minimum_size or face[-1] < .9:
                continue
            crop = self.encoder.alignCrop(frame, face)
            embedding = self.encoder.feature(crop).flatten().tolist()
            result.append((face[:4].tolist(), embedding))
            del crop
        return result


class Tracker:
    """Conservative centroid tracks; uncertain associations create fresh tracks."""
    def __init__(self, clock=time.monotonic, maximum_distance=60, ttl=1):
        self.clock, self.maximum_distance, self.ttl = clock, maximum_distance, ttl
        self.tracks = {}
        self.serial = 0

    def update(self, detections):
        now = self.clock()
        self.tracks = {key: value for key, value in self.tracks.items() if now - value['last'] <= self.ttl}
        used = set()
        result = []
        for box, embedding in detections:
            center = (box[0] + box[2] / 2, box[1] + box[3] / 2)
            candidates = [key for key, value in self.tracks.items() if key not in used and
                          math.dist(center, value['center']) < self.maximum_distance]
            if len(candidates) == 1:
                key = candidates[0]
                value = self.tracks[key]
            else:
                self.serial += 1
                key = str(self.serial)
                value = {'first': now}
            value.update(last=now, center=center)
            self.tracks[key] = value
            used.add(key)
            result.append((key, now - value['first'] >= 1, embedding))
        return result


class SpeakerEncoder:
    """Replaceable ONNX speaker encoder with an explicit waveform contract.

    Requires float32 mono 16 kHz input [1, samples], output [1, embedding].
    Energy gating rejects silence; it is not a spoof detector or calibrated VAD.
    """
    def __init__(self, manifest):
        import onnxruntime
        self.model = onnxruntime.InferenceSession(verified_model(manifest, 'speaker'),
                                                  providers=['CPUExecutionProvider'])

    def encode(self, samples):
        import numpy as np
        waveform = np.asarray(samples, dtype=np.float32)
        if waveform.ndim != 1 or not 32000 <= waveform.size <= 160000 or not np.isfinite(waveform).all():
            return None
        if float(np.sqrt(np.mean(waveform ** 2))) < .01:
            return None
        result = self.model.run(None, {self.model.get_inputs()[0].name: waveform[None, :]})[0]
        return result.reshape(-1).tolist()


def face_evidence(tracks, templates, strong, medium, margin, interaction, liveness):
    """Interaction/liveness callbacks are trusted local sensor adapters, not LLMs."""
    records = []
    for track, stable, embedding in tracks:
        owner = match(embedding, templates, strong, margin)
        strength = 'strong' if owner else 'medium'
        owner = owner or match(embedding, templates, medium, margin)
        records.append(dict(track=track, face=owner, voice=None,
                            face_strength=strength if owner else 'none', stable=stable,
                            interacting=bool(interaction(track)), active_speaker=False,
                            live=bool(liveness(track))))
    return records
