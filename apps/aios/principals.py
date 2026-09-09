"""Explicit process storage context; authorization remains in the OS and broker.

Only the broker constructs sandbox environments. A descriptor is not a token and
never authorizes service calls. Legacy desktop processes have no descriptor.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid


@dataclass(frozen=True)
class Principal:
    owner: str | None
    uid: int
    scope: str
    workspace: Path

    @classmethod
    def decode(cls, value):
        record = json.loads(value)
        if not isinstance(record, dict) or set(record) != {'owner', 'uid', 'scope', 'workspace'}:
            raise ValueError('Invalid process principal')
        for key in ('owner', 'scope'):
            item = record[key]
            if key == 'owner' and item is None:
                continue
            if not isinstance(item, str) or str(uuid.UUID(item)) != item:
                raise ValueError('Invalid process identity')
        if type(record['uid']) is not int or record['uid'] < 1000 or record['uid'] != os.getuid():
            raise PermissionError('Principal does not match process UID')
        # The broker's mount namespace exposes exactly this workspace path.
        if record['workspace'] != '/workspace':
            raise ValueError('Invalid sandbox workspace')
        return cls(record['owner'], record['uid'], record['scope'], Path('/workspace'))

    def directory(self, kind):
        if kind not in ('config', 'data'):
            raise ValueError('Invalid storage class')
        return self.workspace / '.aios' / kind


def current():
    descriptor = os.environ.get('AIOS_PRINCIPAL')
    # Malformed explicit context fails; it never falls back to the desktop home.
    return Principal.decode(descriptor) if descriptor is not None else None
