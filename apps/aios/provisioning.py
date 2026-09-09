"""Root-only creation of new encrypted workspace images.

No API accepts a block device or an existing image to format. Allocation starts
with an exclusive UUID directory, and only files created inside it are eligible
for formatting. Failed allocations remain private for explicit administrator
inspection; they are never enrolled or silently reused.
"""
import json
import os
from pathlib import Path
import pwd
import subprocess

from .identity import identity_id
from .secure_store import atomic_bytes


def command(*arguments):
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                   env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})


def create(config, config_path, owner):
    identity_id(owner)
    if os.geteuid() != 0 or not config_path:
        raise PermissionError('Workspace provisioning requires the root broker')
    if owner in config['principals']:
        raise PermissionError('Identity already has a workspace')
    base = Path(config['volume_store'])
    if not base.is_absolute() or base.is_symlink():
        raise PermissionError('Invalid private volume store')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = base.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise PermissionError('Volume store must be root-private')
    size = config.get('workspace_size_mib', 512)
    if type(size) is not int or not 64 <= size <= 65536:
        raise ValueError('Invalid workspace size')
    minimum, maximum = config.get('personal_uid_min', 30000), config.get('personal_uid_max', 39999)
    if type(minimum) is not int or type(maximum) is not int or not 10000 <= minimum <= maximum <= 60000:
        raise ValueError('Invalid reserved personal UID range')
    used = {record.pw_uid for record in pwd.getpwall()}
    used.update(entry['uid'] for entry in config['principals'].values())
    used.update(config.get(role) for role in ('shell_uid', 'identity_uid', 'anonymous_uid'))
    uid = next((value for value in range(minimum, maximum + 1) if value not in used), None)
    if uid is None:
        raise RuntimeError('Reserved personal UID range is exhausted')
    directory = base / owner
    directory.mkdir(mode=0o700)  # Exclusive: never reuse or format pre-existing data.
    image, key = directory / 'workspace.luks', directory / 'volume.key'
    with key.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    with image.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.truncate(size * 1048576)
        stream.flush()
        os.fsync(stream.fileno())
    mapper_name = 'aios-create-' + owner
    mapper = Path('/dev/mapper') / mapper_name
    if mapper.exists():
        raise PermissionError('Workspace creation mapper already exists')
    command('/sbin/cryptsetup', 'luksFormat', '--batch-mode', '--type', 'luks2',
            '--pbkdf', 'pbkdf2', '--iter-time', '100', '--key-file', str(key), str(image))
    # This key is uniformly random, not derived from the person's PIN.
    command('/sbin/cryptsetup', 'open', '--key-file', str(key), str(image), mapper_name)
    try:
        command('/sbin/mkfs.ext4', '-q', str(mapper))
    finally:
        command('/sbin/cryptsetup', 'close', mapper_name)
    entry = {'uid': uid, 'device': str(image), 'key_file': str(key),
             'mount': str(directory / 'mount')}
    atomic_bytes(directory / 'allocation.json', json.dumps(entry).encode())
    descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    updated = {**config, 'principals': {**config['principals'], owner: entry}}
    if config.get('embedded_display', False):
        updated['wayland_sockets'] = {}
    atomic_bytes(config_path, json.dumps(updated, allow_nan=False).encode())
    config['principals'][owner] = entry
    return entry
