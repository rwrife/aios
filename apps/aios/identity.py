"""Local evidence fusion. Recognition is a candidate, never an authorization."""
from dataclasses import dataclass
import math
import uuid


def identity_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Invalid identity")
    return value


@dataclass(frozen=True)
class Evidence:
    track: str
    face: str | None
    voice: str | None
    face_strength: str
    stable: bool
    interacting: bool
    active_speaker: bool
    live: bool

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls.__annotations__):
            raise ValueError("Invalid evidence fields")
        if not isinstance(value['track'], str) or not 1 <= len(value['track']) <= 64:
            raise ValueError("Invalid track")
        for key in ('face', 'voice'):
            if value[key] is not None:
                identity_id(value[key])
        if value['face_strength'] not in ('none', 'medium', 'strong'):
            raise ValueError("Invalid strength")
        for key in ('stable', 'interacting', 'active_speaker', 'live'):
            if type(value[key]) is not bool:
                raise ValueError("Invalid evidence flag")
        return cls(**value)


class Fusion:
    def __init__(self, clock, ttl=3.0, dwell=1.0):
        self.clock, self.ttl, self.dwell = clock, ttl, dwell
        self.candidate = None
        self.since = self.updated = float('-inf')
        self.reason = 'unavailable'

    def feed(self, records):
        if not isinstance(records, list) or len(records) > 16:
            raise ValueError("Invalid tracks")
        tracks = [Evidence.parse(record) for record in records]
        if len({t.track for t in tracks}) != len(tracks):
            raise ValueError("Duplicate track")
        now = self.clock()
        if now - self.updated > self.ttl:
            self.candidate = None
            self.since = now
        self.updated = now
        self.reason = 'unknown'
        candidate = None
        if any(t.face and t.voice and t.face != t.voice for t in tracks):
            self.reason = 'conflict'
        else:
            active = [t for t in tracks if t.interacting and (len(tracks) == 1 or t.active_speaker)]
            if len(active) == 1:
                t = active[0]
                if t.live and t.stable and t.face and (
                    t.face_strength == 'strong' or
                    (t.face_strength == 'medium' and t.voice == t.face and t.active_speaker)
                ):
                    candidate = t.face
                    self.reason = 'candidate'
            elif len(tracks) > 1:
                self.reason = 'ambiguous'
        if candidate != self.candidate:
            self.since = now
        self.candidate = candidate
        return self.current()

    def current(self):
        now = self.clock()
        if now - self.updated > self.ttl:
            self.reason = 'unavailable'
            # Expired evidence cannot contribute to a subsequent dwell interval.
            self.candidate = None
            self.since = now
            return None
        return self.candidate if now - self.since >= self.dwell else None


def cosine(left, right):
    if not left or len(left) != len(right):
        raise ValueError("Incompatible embeddings")
    if not all(math.isfinite(x) for x in (*left, *right)):
        raise ValueError("Nonfinite embedding")
    denominator = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    if not denominator:
        raise ValueError("Empty embedding")
    return sum(a*b for a, b in zip(left, right)) / denominator


def match(embedding, templates, threshold, margin):
    """Require an absolute match and separation from the next identity."""
    scores = sorted(((max(cosine(embedding, sample) for sample in samples), owner)
                     for owner, samples in templates.items() if samples), reverse=True)
    if not scores or scores[0][0] < threshold:
        return None
    if len(scores) > 1 and scores[0][0] - scores[1][0] < margin:
        return None
    return scores[0][1]
