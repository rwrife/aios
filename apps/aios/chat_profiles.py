"""Desktop greeting profiles, separate from protected workspace identities.

Stores salted PIN verifiers, never plaintext PINs. No capabilities or workspace
access are granted by this single-user desktop personalization service.
"""
import fcntl
import json
import os
import sys
import time
import uuid

from .authority import pin_record, verify_pin
from .core import data_dir
from .portraits import portrait
from .secure_store import atomic_bytes


def verified_operation(owner, pin, root, operation):
    """Native-only transaction: exact UUID/PIN check immediately before writing.

    The callback runs under the account lock so deletion cannot race its commit.
    It is not an IPC action and confers no broker authority.
    """
    from .face_store import account_uuid
    account_uuid(owner)
    with (root / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / 'profiles.json'
        records = json.loads(path.read_text()) if path.exists() else {}
        if owner not in records:
            raise ValueError('Profile or PIN was not recognized')
        valid = verify_pin(records[owner]['pin'], pin, time.time())
        atomic_bytes(path, json.dumps(records).encode())
        if not valid:
            raise ValueError('Profile or PIN was not recognized, or attempts are temporarily locked')
        return operation({'id': owner, 'name': records[owner]['name']})


def dispatch(request, directory=None):
    root = directory or data_dir() / 'chat-profiles'
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (root / 'lock').open('a') as lock:
        os.chmod(root / 'lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / 'profiles.json'
        records = json.loads(path.read_text()) if path.exists() else {}
        action = request.get('action')
        if action == 'profiles':
            return {'profiles': [{'id': key, 'name': value['name'], 'photo': value.get('photo', '')}
                                 for key, value in records.items()]}
        if action not in ('enroll_manual', 'enroll_profile', 'activate_verified',
                          'verify_profile', 'activate_profile', 'delete_profile'):
            raise ValueError('Unsupported profile action')
        name = request.get('name') if action.startswith('enroll') else request.get('owner')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError('Enter a name of up to 80 characters')
        name = name.strip()
        owner = name if name in records else next(
              (key for key, value in records.items() if value['name'].casefold() == name.casefold()), None)
        if action in ('verify_profile', 'activate_profile'):
            owner = name if name in records else None
        if action == 'delete_profile':
            owner = name if name in records else None
            if request.get('confirmed') is not True:
                raise ValueError('Confirm account deletion')
        if action.startswith('enroll'):
            if request.get('consent') is not True:
                raise ValueError('Confirm creation of your local profile')
            if owner:
                raise ValueError('That name already exists. Choose it from the saved profiles to sign in.')
            if len(records) >= 128:
                raise ValueError('This device has reached its profile limit')
            owner = str(uuid.uuid4())
            records[owner] = {'name': name, 'pin': pin_record(request.get('pin')),
                              'photo': portrait(request.get('photo'))}
        else:
            if not owner:
                raise ValueError('Profile or PIN was not recognized')
            valid = verify_pin(records[owner]['pin'], request.get('pin'), time.time())
            atomic_bytes(path, json.dumps(records).encode())
            if not valid:
                raise ValueError('Profile or PIN was not recognized, or attempts are temporarily locked')
            if action == 'delete_profile':
                from .recognition import revoke
                revoke(owner, root)
                del records[owner]
                atomic_bytes(path, json.dumps(records).encode())
                return {'deleted': owner}
            if action == 'verify_profile':
                return {'profile': {'id': owner, 'name': records[owner]['name']}}
        atomic_bytes(path, json.dumps(records).encode())
        value = records[owner]
        return {'profile': {'id': owner, 'name': value['name'], 'photo': value['photo'], 'detected': False}}


def main():
    os.umask(0o077)
    try:
        data = sys.stdin.buffer.readline(65537)
        if len(data) > 65536:
            raise ValueError('Profile request is too large')
        result = dispatch(json.loads(data))
        response = {'ok': True, 'result': result}
    except ValueError as error:
        response = {'ok': False, 'error': str(error)}
    except Exception:
        response = {'ok': False, 'error': 'Could not save or open this profile'}
    print(json.dumps(response), flush=True)


if __name__ == '__main__':
    main()
