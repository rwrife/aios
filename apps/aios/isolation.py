"""Linux sandbox and encrypted workspace adapters. Root configuration only.

No user-supplied commands, UID, environment, device, or mount paths are accepted.
GUI launch intentionally requires an independently isolated Wayland socket.
"""
import os
import json
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import time
import uuid


def artifact_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 240 or '\\' in value or '\x00' in value:
        raise ValueError("Invalid artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('..', '.') for p in value.split('/')) or value.startswith('-'):
        raise ValueError("Use a workspace-relative path")
    return str(path)


def application(app, arguments):
    if not isinstance(arguments, list) or len(arguments) > 1:
        raise ValueError("Invalid application arguments")
    if app == 'terminal' and not arguments:
        return ['/usr/bin/foot']
    if app == 'calculator' and not arguments:
        return ['/usr/bin/gnome-calculator']
    if app == 'editor' and len(arguments) == 1:
        return ['/usr/bin/mousepad', '--disable-server', '/workspace/' + artifact_path(arguments[0])]
    raise ValueError("Application or arguments are not allowlisted")


class LinuxIsolation:
    def __init__(self, config, config_path=None):
        if os.geteuid() != 0:
            raise PermissionError("The session broker must run as root")
        self.config = config
        self.config_path = config_path
        self.root = Path(config['runtime'])
        self.root.mkdir(parents=True, mode=0o711, exist_ok=True)
        self.root.chmod(0o711)
        self.scopes = {}
        self.mounts = {}
        self.displays = {}
        self.ready_displays = set()
        self.requires_display = config.get('embedded_display', False)
        self.cgroups = Path('/sys/fs/cgroup/aios')
        if not Path('/sys/fs/cgroup/cgroup.controllers').exists():
            raise RuntimeError("cgroup v2 is required")
        controllers = set(Path('/sys/fs/cgroup/cgroup.subtree_control').read_text().split())
        if not {'memory', 'pids'} <= controllers:
            raise RuntimeError("Enable the memory and pids controllers for the broker's cgroup parent")
        self.cgroups.mkdir(exist_ok=True)
        # Reap stale cgroups before recovering encrypted volumes after a crash.
        for group in self.cgroups.iterdir():
            if group.is_dir():
                (group / 'cgroup.kill').write_text('1')
                self._wait_empty(group)
                group.rmdir()
        (self.cgroups / 'cgroup.subtree_control').write_text('+memory +pids')
        from .display import recover
        recover(self.root)
        # A broker crash must not leave any previous user's volume unlocked
        # while a new anonymous context starts. Scope teardown always precedes
        # storage recovery, including volumes for users who never return.
        for owner, entry in self.config.get('principals', {}).items():
            name = 'aios-' + owner
            mount = Path(entry['mount'])
            if os.path.ismount(mount):
                self._run(['/bin/umount', str(mount)])
            if (Path('/dev/mapper') / name).exists():
                self._run(['/sbin/cryptsetup', 'close', name])
        for path in self.root.iterdir():
            try:
                if str(uuid.UUID(path.name)) != path.name or path.is_symlink():
                    continue
            except ValueError:
                continue
            if os.path.ismount(path):
                self._run(['/bin/umount', str(path)])
            # Only remove the empty mountpoint, never recursively delete data.
            if path.is_dir():
                path.rmdir()

    @staticmethod
    def _run(argv):
        subprocess.run(argv, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'})

    @staticmethod
    def _wait_empty(group):
        for _ in range(100):
            if 'populated 0' in (group / 'cgroup.events').read_text():
                return
            time.sleep(.02)
        raise RuntimeError("Process scope did not terminate; workspace remains locked")

    def anonymous(self):
        uid = self.config['anonymous_uid']
        root = self.root / str(uuid.uuid4())
        root.mkdir(mode=0o711)
        self._run(['/bin/mount', '-t', 'tmpfs', '-o', 'nodev,nosuid,noexec,size=128m,mode=0711', 'tmpfs', str(root)])
        (root / 'artifacts').mkdir(mode=0o700)
        os.chown(root / 'artifacts', uid, uid)
        return root, uid

    def provision(self, owner):
        from .provisioning import create
        return create(self.config, self.config_path, owner)

    def provisioned(self, owner):
        return owner in self.config['principals']

    def activate(self, owner):
        entry = self.config['principals'][owner]
        root = Path(entry['mount'])
        name = 'aios-' + owner
        if owner not in self.mounts:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            mapper = Path('/dev/mapper') / name
            if mapper.exists():
                # A previous broker may have died with storage mounted.
                if os.path.ismount(root):
                    self._run(['/bin/umount', str(root)])
                self._run(['/sbin/cryptsetup', 'close', name])
            self._run(['/sbin/cryptsetup', 'open', '--type', 'luks', '--key-file',
                       entry['key_file'], entry['device'], name])
            try:
                self._run(['/bin/mount', '-o', 'nodev,nosuid,noexec', str(mapper), str(root)])
                # The fixed UID must traverse to its artifact mount source;
                # journal files remain root-only and other users cannot list.
                root.chmod(0o711)
                (root / 'artifacts').mkdir(mode=0o700, exist_ok=True)
                os.chown(root / 'artifacts', entry['uid'], entry['uid'])
            except Exception:
                if os.path.ismount(root):
                    self._run(['/bin/umount', str(root)])
                self._run(['/sbin/cryptsetup', 'close', name])
                raise
            self.mounts[owner] = root
        return root, entry['uid']

    def launch(self, scope, root, uid, app, arguments):
        command = application(app, arguments)
        endpoint = self.config.get('wayland_sockets', {}).get(str(uid))
        if not endpoint or not (uid in self.ready_displays or self.config.get('display_isolation_validated', False)):
            raise PermissionError("Isolated display has not passed release validation")
        socket = Path(endpoint)
        if not socket.is_socket() or socket.stat().st_uid != uid:
            raise PermissionError("Invalid private display socket")
        return self._spawn(scope, root, uid, command, socket)

    def display(self, lease, uid):
        if not self.requires_display:
            raise PermissionError('Embedded display is disabled')
        from .display import DisplaySocket
        if uid not in self.displays:
            self.displays[uid] = DisplaySocket(self.root, lease, uid)
            self.config.setdefault('wayland_sockets', {})[str(uid)] = str(self.displays[uid].path)
        return self.displays[uid].listener.fileno()

    def display_ready(self, uid):
        if uid not in self.displays:
            raise PermissionError('Display has not been acquired')
        self.ready_displays.add(uid)

    def _spawn(self, scope, root, uid, command, display=None, *, diagnostics=False):
        """Trusted adapter entry point, never exposed as a service action.

        The headless path is also exercised by kernel isolation tests. Callers
        must select commands in trusted code; the socket API uses launch().
        """
        if str(uuid.UUID(scope)) != scope:
            raise ValueError("Invalid process scope")
        group = self.cgroups / scope
        group.mkdir(exist_ok=True)
        (group / 'pids.max').write_text('128')
        (group / 'memory.max').write_text('1073741824')
        argv = ['/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
                '--cap-drop', 'ALL', '--clearenv', '--ro-bind', '/usr', '/usr',
                '--ro-bind', '/lib', '/lib', '--ro-bind', '/bin', '/bin',
                '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run',
                '--tmpfs', '/dev/shm', '--ro-bind-try', '/etc/fonts', '/etc/fonts',
                '--bind', str(root / 'artifacts'), '/workspace', '--chdir', '/workspace',
                '--setenv', 'HOME', '/workspace', '--setenv', 'PATH', '/usr/bin:/bin',
                '--setenv', 'LANG', 'C.UTF-8']
        owners = [owner for owner, entry in self.config.get('principals', {}).items()
                  if entry['uid'] == uid]
        if len(owners) > 1 or (not owners and uid != self.config['anonymous_uid']):
            raise PermissionError('Process UID has no unique principal')
        descriptor = {'owner': owners[0] if owners else None, 'uid': uid,
                      'scope': scope, 'workspace': '/workspace'}
        argv += ['--setenv', 'AIOS_PRINCIPAL', json.dumps(descriptor)]
        if display:
            argv += ['--dir', '/run/user', '--dir', '/run/user/session',
                     '--bind', str(display), '/run/user/session/wayland-0',
                     '--setenv', 'XDG_RUNTIME_DIR', '/run/user/session',
                     '--setenv', 'WAYLAND_DISPLAY', 'wayland-0',
                     '--setenv', 'QT_QPA_PLATFORM', 'wayland', '--setenv', 'GDK_BACKEND', 'wayland']
        # The broker is single-threaded. Enter the cgroup before exec, so forked
        # descendants cannot escape tracking even if the launcher exits early.
        def demote():
            (group / 'cgroup.procs').write_text(str(os.getpid()))
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        process = subprocess.Popen(argv + command, preexec_fn=demote, start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=None if diagnostics else subprocess.DEVNULL, close_fds=True,
                                   env={'PATH': '/usr/bin:/bin'})
        self.scopes.setdefault(scope, []).append(process)
        # Catch missing binaries, unsupported namespace settings and immediate
        # launcher failures instead of journaling an application that never ran.
        try:
            code = process.wait(timeout=.05)
        except subprocess.TimeoutExpired:
            return process
        if code:
            self.stop(scope)
            raise RuntimeError("Sandboxed application failed to start")
        return process

    def stop(self, scope):
        group = self.cgroups / scope
        if group.exists():
            for pid in (group / 'cgroup.procs').read_text().split():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + .5
            while time.monotonic() < deadline and 'populated 1' in (group / 'cgroup.events').read_text():
                time.sleep(.01)
            # Force termination bounds lock time, including daemonized children.
            (group / 'cgroup.kill').write_text('1')
            self._wait_empty(group)
            group.rmdir()
        for process in self.scopes.pop(scope, []):
            process.wait(timeout=2)

    def release(self, owner, root):
        uid = self.config['principals'][owner]['uid'] if owner else self.config['anonymous_uid']
        if uid in self.displays:
            self.displays.pop(uid).close()
            self.ready_displays.discard(uid)
            self.config.get('wayland_sockets', {}).pop(str(uid), None)
        self._run(['/bin/umount', str(root)])
        if owner:
            self._run(['/sbin/cryptsetup', 'close', 'aios-' + owner])
            self.mounts.pop(owner, None)
        else:
            root.rmdir()


class SimulatorIsolation:
    """Explicit test adapter. Never executes apps or claims OS isolation."""
    def __init__(self, root):
        self.root = Path(root)
        self.launches = []
        self.stopped = []

    def _workspace(self, name):
        root = self.root / name
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (root / 'artifacts').mkdir(mode=0o700, exist_ok=True)
        return root, 65534

    def anonymous(self):
        return self._workspace('anonymous-' + str(uuid.uuid4()))

    def activate(self, owner):
        return self._workspace(owner)

    def provision(self, owner):
        self._workspace(owner)

    def provisioned(self, owner):
        return (self.root / owner / 'artifacts').is_dir()

    def launch(self, scope, root, uid, app, arguments):
        application(app, arguments)
        self.launches.append((scope, str(root), uid, app, arguments))

    def stop(self, scope):
        self.stopped.append(scope)

    def release(self, owner, root):
        if not owner:
            shutil.rmtree(root)
