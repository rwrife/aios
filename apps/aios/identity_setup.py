"""Initialize root-private broker state using pre-existing service accounts.

Invoked by the Alpine setup wrapper. Never enables personal display access or
overwrites an existing installation. Run on the target AIOS machine, not a host
development account.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd

from .secure_store import atomic_bytes


def initialize(config_path, state_root):
    if os.geteuid() != 0:
        raise PermissionError('Setup requires root')
    config_path, state_root = Path(config_path), Path(state_root)
    if not config_path.is_absolute() or not state_root.is_absolute():
        raise ValueError('Setup paths must be absolute')
    if config_path.exists() or state_root.exists():
        raise FileExistsError('Existing identity state will not be overwritten')
    roles = {role: pwd.getpwnam(name).pw_uid for role, name in
             [('shell_uid', 'aios'), ('identity_uid', 'aios-identity'), ('anonymous_uid', 'aios-anonymous')]}
    if min(roles.values()) < 1000 or len(set(roles.values())) != 3:
        raise ValueError('Service accounts must have distinct unprivileged UIDs')
    group = grp.getgrnam('aios-broker').gr_gid
    state_root.mkdir(parents=True, mode=0o700)
    for name in ('records', 'volumes'):
        (state_root / name).mkdir(mode=0o700)
    atomic_bytes(state_root / 'master.key', os.urandom(32))
    config = {**roles, 'state': str(state_root / 'records'), 'master_key': str(state_root / 'master.key'),
              'volume_store': str(state_root / 'volumes'), 'workspace_size_mib': 512,
              'personal_uid_min': 30000, 'personal_uid_max': 39999,
              'runtime': '/run/aios-workspaces', 'socket': '/run/aios-broker/session.sock',
              'socket_gid': group, 'principals': {}, 'wayland_sockets': {},
              'embedded_display': True,
              'display_isolation_validated': False}
    atomic_bytes(config_path, json.dumps(config, indent=2).encode())
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/aios/sessiond.json')
    parser.add_argument('--state', default='/var/lib/aios-identity')
    args = parser.parse_args()
    initialize(args.config, args.state)
    print('Identity storage initialized. Personal mode remains disabled until display isolation is validated.')


if __name__ == '__main__':
    main()
