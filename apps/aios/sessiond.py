"""Bounded, peer-authenticated Unix socket service; JSON lines, no shell commands.

Experimental service is opt-in. The simulator is deliberately a separate mode
that records launches and never starts applications or uses real credentials.
"""
import argparse
import array
import json
import os
from pathlib import Path
import signal
import socket
import struct
import time

from .identity import identity_id
from .isolation import LinuxIsolation, SimulatorIsolation
from .secure_store import EncryptedStore
from .sessions import Sessions


LIMIT = 65536
SCHEDULED_LIMIT = 128 * 1024 + 1024
FIELDS = {
    'status': (), 'anonymous': (), 'suspend': (), 'cancel_challenge': (),
    'activate': ('title', 'session'), 'launch': ('app', 'arguments'),
    'search': ('query',), 'message': ('role', 'content'),
    'history': ('before',), 'summarize': ('summary',),
    'document_read': ('path',), 'document_save': ('path', 'content'),
    'enroll_manual': ('name', 'pin', 'consent'),
    'enroll_profile': ('name', 'pin', 'consent', 'photo'),
    'profiles': (),
    'recover': ('owner', 'recovery', 'pin'),
    'activate_verified': ('owner', 'pin', 'title', 'session'),
    'display_acquire': (), 'display_ready': ('lease',),
    'display_attest': ('platform', 'embedded'),
    'request_capability': ('operation', 'resource'),
    'verify': ('challenge', 'pin', 'confirmed'), 'github_profile': ('token',),
    'evidence': ('tracks',), 'simulate': ('state',),
    'scheduled_jobs': ('lease', 'scope', 'request'),
    'chat_open': ('lease', 'scope'),
    'chat_send': ('lease', 'scope', 'chat', 'content'),
    'chat_poll': ('lease', 'scope', 'chat'),
    'chat_stop': ('lease', 'scope', 'chat'),
    'chat_close': ('lease', 'scope', 'chat'),
}


def decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("Nonfinite JSON number")
    request = json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(request, dict) or request.get('action') not in FIELDS:
        raise ValueError("Unknown action")
    if set(request) != {'action', *FIELDS[request['action']]}:
        raise ValueError("Invalid request fields")
    return request


def load_config(path):
    path = Path(path)
    if path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
        raise PermissionError("Session configuration must be root-owned and not writable by others")
    config = json.loads(path.read_text())
    if type(config.get('display_isolation_validated', False)) is not bool:
        raise ValueError('Display validation must be a boolean')
    uids = []
    for owner, principal in config['principals'].items():
        identity_id(owner)
        uid = principal['uid']
        if type(uid) is not int or uid < 1000:
            raise ValueError("Personal UID must be an unprivileged account")
        uids.append(uid)
        for field in ('device', 'key_file', 'mount'):
            if not Path(principal[field]).is_absolute():
                raise ValueError("Workspace paths must be absolute")
        key = Path(principal['key_file'])
        if key.stat().st_uid != 0 or key.stat().st_mode & 0o077:
            raise PermissionError("Workspace key must be root-only")
    for role in ('anonymous_uid', 'shell_uid', 'identity_uid'):
        if type(config[role]) is not int or config[role] < 1000:
            raise ValueError("Service identities must be unprivileged")
        uids.append(config[role])
    if len(set(uids)) != len(uids):
        raise ValueError("All workspace and service UIDs must be distinct")
    return config


class Service:
    def __init__(self, sessions, shell_uid, identity_uid, simulator=False, aliases=None, personal_enabled=False):
        self.sessions = sessions
        self.shell_uid, self.identity_uid = shell_uid, identity_uid
        self.simulator, self.aliases = simulator, aliases or {}
        self.personal_enabled = simulator or personal_enabled
        self.last_shell = time.monotonic()
        self.display_pid = self.peer_pid = None
        self.display_platform = None
        self.embedded_shell = False

    def dispatch(self, request, uid, pid=None):
        self.peer_pid = pid
        try:
            return self._dispatch(request, uid)
        finally:
            if uid == self.shell_uid and (not getattr(self.sessions.isolation, 'requires_display', False)
                                          or pid == self.display_pid):
                self.last_shell = time.monotonic()
            self.peer_pid = None

    def _can_personal(self):
        if self.simulator:
            return True
        if not self.personal_enabled:
            return False
        if getattr(self.sessions.isolation, 'requires_display', False):
            return (self.display_platform == 'eglfs' and self.embedded_shell and
                    self.peer_pid is not None and self.peer_pid == self.display_pid)
        return True

    def tick(self):
        self.sessions.tick()
        if self.sessions.root is not None and time.monotonic() - self.last_shell > 3:
            self.sessions.suspend()
        start = (self.personal_enabled and self.display_pid is not None and
                 (not getattr(self.sessions.isolation, 'requires_display', False) or
                  (self.display_platform == 'eglfs' and self.embedded_shell)))
        self.sessions.scheduled_tick(start=start)

    def _dispatch(self, request, uid):
        action = request['action']
        if action == 'evidence':
            if uid != self.identity_uid:
                raise PermissionError("Only the identity service can report evidence")
        elif uid != self.shell_uid:
            raise PermissionError("Only the trusted shell can request sessions")
        s = self.sessions
        if action == 'scheduled_jobs' or action.startswith('chat_'):
            if (not self._can_personal() or self.peer_pid is None
                    or self.peer_pid != self.display_pid):
                raise PermissionError('Protected chat requires the registered trusted display')
        if action == 'scheduled_jobs':
            from .scheduling import SchedulingError
            try:
                return s.scheduled_request(request['lease'], request['scope'], request['request'])
            except SchedulingError as error:
                return {'status': error.code, 'error': str(error)}
        if action == 'chat_open':
            return s.chat_open(request['lease'], request['scope'])
        if action == 'chat_send':
            return s.chat_send(request['lease'], request['scope'], request['chat'], request['content'])
        if action == 'chat_poll':
            return s.chat_poll(request['lease'], request['scope'], request['chat'])
        if action == 'chat_stop':
            return s.chat_stop(request['lease'], request['scope'], request['chat'])
        if action == 'chat_close':
            return s.chat_close(request['lease'], request['scope'], request['chat'])
        if s.owner and getattr(s.isolation, 'requires_display', False) and self.peer_pid != self.display_pid:
            if action not in ('status', 'suspend', 'evidence', 'display_attest'):
                raise PermissionError('Personal requests require the registered display process')
        if action in ('activate', 'activate_verified', 'enroll_manual', 'enroll_profile', 'profiles', 'recover', 'search',
                      'request_capability', 'verify', 'github_profile') and not self._can_personal():
            raise PermissionError("Personal mode requires a validated isolated display and trusted input path")
        if action == 'status':
            status = s.status()
            if s.owner and getattr(s.isolation, 'requires_display', False) and self.peer_pid != self.display_pid:
                status.update(session=None, lease=None)
            return {**status, 'simulator': self.simulator,
                    'profile': s.profile_status() if self._can_personal() else {},
                    'embedded_display': getattr(s.isolation, 'requires_display', False),
                    'display_attested': self.peer_pid is not None and self.peer_pid == self.display_pid,
                    'personal_available': self._can_personal(),
                    'display_ready': s.uid in getattr(s.isolation, 'ready_displays', set())}
        if action == 'anonymous':
            return {'lease': s.anonymous()}
        if action == 'suspend':
            s.suspend()
        elif action == 'cancel_challenge':
            s.challenge = None
        elif action == 'activate':
            return {'session': s.activate(request['title'], request['session'])}
        elif action == 'activate_verified':
            return {'session': s.activate_verified(request['owner'], request['pin'], request['title'], request['session'])}
        elif action == 'enroll_manual':
            return s.enroll(request['name'], request['pin'], request['consent'], None)
        elif action == 'enroll_profile':
            return s.enroll(request['name'], request['pin'], request['consent'], None, request['photo'])
        elif action == 'profiles':
            return {'profiles': s.profiles()}
        elif action == 'recover':
            return {'recovery': s.recover(request['owner'], request['recovery'], request['pin'])}
        elif action == 'display_acquire':
            if self.peer_pid is None or self.peer_pid != self.display_pid:
                raise PermissionError('Only the registered display process may acquire a listener')
            return s.acquire_display()
        elif action == 'display_ready':
            if self.peer_pid is None or self.peer_pid != self.display_pid:
                raise PermissionError('Only the registered display process may acknowledge a listener')
            s.display_ready(request['lease'])
        elif action == 'display_attest':
            if not isinstance(request['platform'], str) or len(request['platform']) > 32 or type(request['embedded']) is not bool:
                raise ValueError('Invalid display declaration')
            if self.peer_pid is None:
                raise PermissionError('Display process credentials unavailable')
            if self.display_pid is not None and self.display_pid != self.peer_pid and s.root is not None:
                s.suspend()
            self.display_pid, self.display_platform = self.peer_pid, request['platform']
            self.embedded_shell = request['embedded']
            if s.owner and not self._can_personal():
                s.suspend()
        elif action == 'launch':
            s.launch(request['app'], request['arguments'])
        elif action == 'search':
            return {'sessions': s.list_work(request['query'])}
        elif action == 'message':
            s.message(request['role'], request['content'])
        elif action == 'history':
            return s.history(request['before'])
        elif action == 'summarize':
            s.summarize(request['summary'])
        elif action == 'document_read':
            return s.document(request['path'])
        elif action == 'document_save':
            if not isinstance(request['content'], str):
                raise ValueError('Invalid document')
            return s.document(request['path'], request['content'])
        elif action == 'request_capability':
            return s.request_capability(request['operation'], request['resource'])
        elif action == 'verify':
            return {'capability': s.verify(request['challenge'], request['pin'], request['confirmed'])}
        elif action == 'github_profile':
            if self.simulator:
                s.use(request['token'], 'secrets.github.profile', 'github')
                return {'login': 'simulated-account'}
            from .secret_broker import SecretBroker
            return SecretBroker(s, s.store).github_profile(request['token'])
        elif action == 'evidence':
            s.evidence(request['tracks'])
        elif action == 'simulate':
            if not self.simulator:
                raise PermissionError("Identity simulation is disabled")
            state = request['state']
            if state not in ('unknown', 'user-a', 'user-b', 'absent', 'conflict'):
                raise ValueError("Invalid simulator state")
            # Deliberately explicit; the demo never runs in a privileged daemon.
            tracks = []
            if state != 'absent':
                owner = self.aliases.get(state)
                tracks = [dict(track='demo', face=owner, voice=owner, face_strength='strong',
                               stable=True, interacting=True, active_speaker=True, live=True)]
                if state == 'conflict':
                    tracks[0].update(face=self.aliases['user-a'], voice=self.aliases['user-b'])
            s.evidence(tracks)
        return {}


def serve(path, service, socket_group=None):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("Socket already exists; check for another daemon before removing it")
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        path.chmod(0o600 if socket_group is None else 0o660)
        if socket_group is not None:
            os.chown(path, 0, socket_group)
        server.listen(8)
        server.settimeout(.25)
        try:
            while running:
                try:
                    service.tick()
                except Exception:
                    service.sessions._shield()
                    service.sessions.fault = True
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(.2)
                    descriptor = None
                    scheduled_response = False
                    try:
                        pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if uid not in (service.shell_uid, service.identity_uid):
                            raise PermissionError("Unauthorized peer")
                        data = bytearray()
                        deadline = time.monotonic() + .2
                        while b'\n' not in data and len(data) <= SCHEDULED_LIMIT:
                            if time.monotonic() >= deadline:
                                raise ValueError("Request timeout")
                            chunk = connection.recv(min(4096, SCHEDULED_LIMIT + 1 - len(data)))
                            if not chunk:
                                break
                            data.extend(chunk)
                        if len(data) > SCHEDULED_LIMIT or not data.endswith(b'\n') or data.count(b'\n') != 1:
                            raise ValueError("Invalid request frame")
                        value = decode(data)
                        scheduled_response = value['action'] == 'scheduled_jobs'
                        if not scheduled_response and len(data) > LIMIT:
                            raise ValueError("Invalid request frame")
                        result = service.dispatch(value, uid, pid)
                        from .display import DescriptorReply
                        if isinstance(result, DescriptorReply):
                            descriptor, result = result.descriptor, result.result
                        response = {'ok': True, 'result': result}
                    except (ValueError, TypeError, KeyError, PermissionError):
                        response = {'ok': False, 'error': 'Request denied, invalid, or verification required'}
                    except Exception:
                        response = {'ok': False, 'error': 'Service unavailable'}
                    try:
                        encoded = json.dumps(response, ensure_ascii=False).encode() + b'\n'
                        if len(encoded) > (2 * 1024 * 1024 + 1024 if scheduled_response else 262144):
                            encoded = b'{"ok":false,"error":"Response exceeds transport limit"}\n'
                        if descriptor is not None:
                            sent = connection.sendmsg([encoded], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                       array.array('i', [descriptor]))])
                            connection.sendall(encoded[sent:])
                        else:
                            connection.sendall(encoded)
                    except OSError:
                        pass
        finally:
            service.sessions.suspend()
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/aios/sessiond.json')
    parser.add_argument('--simulate', type=Path, help='Unprivileged, non-executing simulator state directory')
    args = parser.parse_args()
    os.umask(0o077)
    if args.simulate:
        if os.geteuid() == 0:
            raise PermissionError("Do not run the simulator as root")
        from .secure_store import atomic_bytes
        root = args.simulate.resolve()
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        key_path = root / 'key'
        if not key_path.exists():
            atomic_bytes(key_path, os.urandom(32))
        store = EncryptedStore(root / 'records', key_path.read_bytes())
        sessions = Sessions(SimulatorIsolation(root / 'workspaces'), store)
        aliases = store.get('aliases', {})
        if not aliases:
            import getpass
            pin = getpass.getpass('Choose a PIN for the two simulated identities: ')
            samples = {'face': [[1.0] * 16] * 3, 'voice': [[1.0] * 16] * 3}
            for alias in ('user-a', 'user-b'):
                aliases[alias] = sessions.enroll(alias, pin, True, samples)['identity']
            del pin
            store.put('aliases', aliases)
        sessions.fusion.dwell = 0
        serve(root / 'session.sock', Service(sessions, os.getuid(), os.getuid(), True, aliases))
    else:
        config = load_config(args.config)
        key_path = Path(config['master_key'])
        if key_path.stat().st_uid != 0 or key_path.stat().st_mode & 0o077:
            raise PermissionError("Master key must be root-only")
        store = EncryptedStore(config['state'], key_path.read_bytes())
        sessions = Sessions(LinuxIsolation(config, args.config), store)
        serve(config['socket'], Service(sessions, config['shell_uid'], config['identity_uid'],
              personal_enabled=config.get('display_isolation_validated', False)), config['socket_gid'])


if __name__ == '__main__':
    main()
