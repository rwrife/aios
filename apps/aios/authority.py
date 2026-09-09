"""Deterministic policy and opaque, in-memory, lease-bound capabilities."""
from dataclasses import dataclass
import hashlib
import hmac
import secrets


RULES = {
    'workspace.personal': ('recognized', False),
    'financial.read': ('verified', False),
    'secrets.github.profile': ('verified', True),
    'message.send': ('transaction_confirmed', True),
    'financial.transfer': ('transaction_confirmed', True),
    'system.install': ('transaction_confirmed', True),
    'artifacts.claim': ('verified', True),
    'identity.delete': ('transaction_confirmed', True),
}


@dataclass(frozen=True)
class Capability:
    owner: str
    lease: str
    operation: str
    resource: str
    expires: float
    single_use: bool


class Capabilities:
    def __init__(self, clock):
        self.clock = clock
        self.tokens = {}

    def issue(self, owner, lease, operation, resource, authority, lifetime=120):
        if operation not in RULES:
            raise PermissionError("Unsupported capability")
        required, once = RULES[operation]
        levels = ('anonymous', 'recognized', 'verified', 'transaction_confirmed')
        if levels.index(authority) < levels.index(required):
            raise PermissionError("Verification required")
        if not isinstance(resource, str) or not 1 <= len(resource) <= 256:
            raise ValueError("Invalid resource")
        self.tokens = {k: v for k, v in self.tokens.items() if v.expires > self.clock()}
        if len(self.tokens) >= 256:
            raise PermissionError("Too many capabilities")
        token = secrets.token_urlsafe(32)
        self.tokens[token] = Capability(owner, lease, operation, resource,
                                        self.clock() + min(120, max(1, lifetime)), once)
        return token

    def use(self, token, owner, lease, operation, resource):
        cap = self.tokens.get(token)
        if not cap or cap.expires <= self.clock() or (
            cap.owner, cap.lease, cap.operation, cap.resource
        ) != (owner, lease, operation, resource):
            raise PermissionError("Capability denied")
        if cap.single_use:
            del self.tokens[token]

    def revoke(self):
        self.tokens.clear()


def pin_record(pin):
    if not isinstance(pin, str) or not 4 <= len(pin) <= 128:
        raise ValueError("Use at least four ASCII digits or a passphrase of ten characters")
    numeric = pin.isascii() and pin.isdecimal()
    if (not numeric and len(pin) < 10) or any(ord(c) < 32 for c in pin):
        raise ValueError("Use at least four ASCII digits or a passphrase of ten characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=16384, r=8, p=1)
    return {'salt': salt.hex(), 'digest': digest.hex(), 'failures': 0, 'retry_at': 0}


def verify_pin(record, pin, now):
    """Caller persists counters even on failure. Wall time survives service restart."""
    if record['failures'] >= 10 or now < record['retry_at']:
        return False
    if not isinstance(pin, str) or len(pin) > 128:
        pin = ''
    digest = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(record['salt']), n=16384, r=8, p=1)
    valid = hmac.compare_digest(digest.hex(), record['digest'])
    if valid:
        record.update(failures=0, retry_at=0)
    else:
        record['failures'] += 1
        record['retry_at'] = now + min(300, 2 ** record['failures'])
    return valid
