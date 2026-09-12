"""Linux sandbox and encrypted workspace adapters. Root configuration only.

No user-supplied commands, UID, environment, device, or mount paths are accepted.
GUI launch intentionally requires an independently isolated Wayland socket.
"""
import os
import json
import ctypes
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
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
        return ['/usr/bin/mousepad', '/workspace/' + artifact_path(arguments[0])]
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
        # Scope artifact aliases also hold encrypted devices open. Remove them
        # after killing cgroups, before closing any owner's encrypted volume.
        for path in self.root.iterdir():
            try:
                if str(uuid.UUID(path.name)) != path.name or path.is_symlink():
                    continue
            except ValueError:
                continue
            if os.path.ismount(path):
                self._run(['/bin/umount', str(path)])
            if path.is_dir():
                path.rmdir()
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

    def scheduled(self, scope, root, uid):
        """Fixed broker-only adapter; never accepts a command from the client."""
        owners = [owner for owner, entry in self.config['principals'].items()
                  if entry['uid'] == uid and self.mounts.get(owner) == root]
        if len(owners) != 1 or not os.path.ismount(root):
            raise PermissionError('An unlocked encrypted owner workspace is required')
        return self._spawn(scope, root, uid,
                           ['/usr/bin/python3', '-m', 'aios.scheduled_protected'],
                           scheduled=True)

    def chat(self, scope, root, uid):
        """Fixed broker-only protected foreground chat adapter."""
        owners = [owner for owner, entry in self.config['principals'].items()
                  if entry['uid'] == uid and self.mounts.get(owner) == root]
        endpoint = self.config.get('wayland_sockets', {}).get(str(uid))
        if len(owners) != 1 or not os.path.ismount(root) or uid not in self.ready_displays:
            raise PermissionError('An authorized protected display workspace is required')
        display = Path(endpoint) if endpoint else None
        if display is None or not display.is_socket() or display.stat().st_uid != uid:
            raise PermissionError('Protected chat requires the private display')
        return self._spawn(scope, root, uid,
                           ['/usr/bin/python3', '-m', 'aios.protected_chat'],
                           display, scheduled=True)

    @staticmethod
    def _directory_fd(path):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts:
            raise PermissionError('Workspace paths must be absolute directories')
        flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open('/', flags)
        try:
            for part in path.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                try:
                    parent_info, child_info = os.fstat(descriptor), os.fstat(child)
                    read_only = bool(os.fstatvfs(descriptor).f_flag & os.ST_RDONLY)
                    # Read-only mounts and root-owned sticky parents prevent
                    # replacement too; the latter permits root test dirs in /tmp.
                    sticky_root = (parent_info.st_uid == 0 and parent_info.st_mode & stat.S_ISVTX
                                   and child_info.st_uid == 0)
                    if (not read_only and not sticky_root and
                            (parent_info.st_uid != 0 or parent_info.st_mode & 0o022)):
                        raise PermissionError('Workspace ancestors must prevent unprivileged replacement')
                except BaseException:
                    os.close(child)
                    raise
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _bind_descriptors(source, target):
        # Avoid mount(8) canonicalizing an FD back into a mutable source path.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                               ctypes.c_ulong, ctypes.c_void_p]
        libc.mount.restype = ctypes.c_int
        if libc.mount(f'/proc/self/fd/{source}'.encode(), f'/proc/self/fd/{target}'.encode(),
                      None, 4096, None) != 0:  # MS_BIND
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def _bind_artifacts(self, scope, root, uid, owner):
        workspace = self.root / scope
        with ExitStack() as descriptors:
            def keep(descriptor):
                descriptors.callback(os.close, descriptor)
                return descriptor
            root_fd = keep(self._directory_fd(root))
            runtime_fd = keep(self._directory_fd(self.root))
            root_info, runtime_info = os.fstat(root_fd), os.fstat(runtime_fd)
            if (root_info.st_uid != 0 or root_info.st_mode & 0o022 or
                    runtime_info.st_uid != 0 or runtime_info.st_mode & 0o022):
                raise PermissionError('Workspace and runtime directories must be broker-owned')
            if owner is not None:
                device = (Path('/dev/mapper') / ('aios-' + owner)).stat()
                if not stat.S_ISBLK(device.st_mode) or root_info.st_dev != device.st_rdev:
                    raise PermissionError('Workspace does not belong to the encrypted owner')
            flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
            source_fd = keep(os.open('artifacts', flags, dir_fd=root_fd))
            source = os.fstat(source_fd)
            if source.st_uid != uid or source.st_mode & 0o077:
                raise PermissionError('Artifacts must be a private owner directory')
            identity = (source.st_dev, source.st_ino)
            created = mounted = False
            try:
                try:
                    os.mkdir(scope, mode=0o700, dir_fd=runtime_fd)
                    created = True
                except FileExistsError:
                    pass
                target_fd = keep(os.open(scope, flags, dir_fd=runtime_fd))
                target = os.fstat(target_fd)
                if not created:
                    if (target.st_dev, target.st_ino) != identity:
                        raise PermissionError('Process workspace scope belongs to another directory')
                else:
                    if target.st_uid != 0 or target.st_mode & 0o077:
                        raise PermissionError('Process workspace mountpoint is not private')
                    self._bind_descriptors(source_fd, target_fd)
                    mounted = True
                current = os.stat('artifacts', dir_fd=root_fd, follow_symlinks=False)
                exposed = os.stat(scope, dir_fd=runtime_fd, follow_symlinks=False)
                if any((item.st_dev, item.st_ino) != identity or item.st_uid != uid
                       or not stat.S_ISDIR(item.st_mode) or item.st_mode & 0o077
                       for item in (current, exposed)):
                    raise PermissionError('Artifact directory changed during workspace binding')
                return workspace
            except BaseException:
                if mounted:
                    self._run(['/bin/umount', str(workspace)])
                if created:
                    os.rmdir(scope, dir_fd=runtime_fd)
                raise

    def _spawn(self, scope, root, uid, command, display=None, *, diagnostics=False, scheduled=False):
        """Trusted adapter entry point, never exposed as a service action.

        The headless path is also exercised by kernel isolation tests. Callers
        must select commands in trusted code; the socket API uses launch().
        """
        if str(uuid.UUID(scope)) != scope:
            raise ValueError("Invalid process scope")
        owners = [owner for owner, entry in self.config.get('principals', {}).items()
                  if entry['uid'] == uid]
        if len(owners) > 1 or (not owners and uid != self.config['anonymous_uid']):
            raise PermissionError('Process UID has no unique principal')
        # The root broker pins and validates both directories, then exposes only
        # owner artifacts at an alias bwrap can traverse after UID demotion.
        workspace = self._bind_artifacts(scope, root, uid, owners[0] if owners else None)
        group = self.cgroups / scope
        try:
            group.mkdir(exist_ok=True)
            (group / 'pids.max').write_text('128')
            (group / 'memory.max').write_text('1073741824')
        except OSError:
            self.stop(scope)
            raise
        argv = ['/usr/bin/bwrap', '--unshare-all', '--die-with-parent', '--new-session',
                '--cap-drop', 'ALL', '--clearenv', '--ro-bind', '/usr', '/usr',
                '--ro-bind', '/lib', '/lib', '--ro-bind', '/bin', '/bin',
                '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/run',
                '--tmpfs', '/dev/shm', '--ro-bind-try', '/etc/fonts', '/etc/fonts',
                '--bind', str(workspace), '/workspace', '--chdir', '/workspace',
                '--setenv', 'HOME', '/workspace', '--setenv', 'PATH', '/usr/bin:/bin',
                '--setenv', 'LANG', 'C.UTF-8']
        descriptor = {'owner': owners[0] if owners else None, 'uid': uid,
                      'scope': scope, 'workspace': '/workspace'}
        argv += ['--setenv', 'AIOS_PRINCIPAL', json.dumps(descriptor)]
        if scheduled:
            # Provider traffic uses the same network as desktop providers, but
            # no desktop files, credentials, control socket or display is bound.
            argv += ['--share-net', '--dir', '/run/aios-scheduler',
                     '--setenv', 'PYTHONPATH', '/usr/local/share/aios',
                     '--ro-bind-try', '/etc/ssl', '/etc/ssl',
                     '--ro-bind-try', '/etc/resolv.conf', '/etc/resolv.conf',
                     '--ro-bind-try', '/etc/localtime', '/etc/localtime',
                     '--ro-bind-try', '/etc/timezone', '/etc/timezone',
                     '--setenv', 'XDG_RUNTIME_DIR', '/run/aios-scheduler']
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
        try:
            process = subprocess.Popen(argv + command, preexec_fn=demote, start_new_session=True,
                                       stdin=subprocess.PIPE if scheduled else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE if scheduled else subprocess.DEVNULL,
                                       stderr=None if diagnostics else subprocess.DEVNULL, close_fds=True,
                                       env={'PATH': '/usr/bin:/bin'})
        except Exception:
            self.stop(scope)
            raise
        self.scopes.setdefault(scope, []).append(process)
        # Catch missing binaries, unsupported namespace settings and immediate
        # launcher failures instead of journaling an application that never ran.
        try:
            code = process.wait(timeout=.05)
        except subprocess.TimeoutExpired:
            return process
        if code:
            try:
                self.stop(scope)
            finally:
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
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
        workspace = self.root / scope
        if os.path.ismount(workspace):
            self._run(['/bin/umount', str(workspace)])
        if workspace.exists():
            workspace.rmdir()

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
