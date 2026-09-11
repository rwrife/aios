"""Non-isolating pipe fixture. Provider completion is deterministic, not a VM."""
import os
import fcntl
from pathlib import Path
import signal
import sys
from unittest.mock import patch

from aios.principals import Principal
from aios.scheduled_protected import serve
from aios.scheduler import Scheduler


root = Path(sys.argv[1])
principal = Principal(sys.argv[2], os.getuid(), sys.argv[3], root / 'artifacts')
runtime = root / 'runtime'
runtime.mkdir(mode=0o700, exist_ok=True)
original_lstat = Path.lstat


def fixture_lstat(path, *args, **kwargs):
    # DrvFS without metadata cannot represent mode bits. This fixture tests
    # broker authorization, not the kernel's storage permission enforcement.
    info = original_lstat(path, *args, **kwargs)
    values = list(info)
    values[0] = (info.st_mode & ~0o777) | 0o700
    return os.stat_result(values)


def fixture_lock(path, *, blocking=False):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    return fd


def path(value):
    return runtime if value == '/run/aios-scheduler' else Path(value)


with patch('aios.principals.current', return_value=principal), \
        patch('aios.core.current_principal', return_value=principal), \
        patch('aios.scheduler.Path', side_effect=path), \
        patch('pathlib.Path.lstat', fixture_lstat), \
        patch('aios.scheduler.locked_file', fixture_lock):
    scheduler = Scheduler(protected=True)

    def launch(run):
        scheduler.store.start(run['id'])
        if run['snapshot']['prompt'] != 'WAIT':
            scheduler.store.finish(run['id'], 'succeeded', result='PRIVATE OWNER RESULT')
    scheduler.launch = launch

    def stop(*_):
        scheduler.stopping = True
    signal.signal(signal.SIGTERM, stop)
    try:
        sys.stdout.buffer.write(b'{"ready":true}\n')
        sys.stdout.buffer.flush()
        serve(scheduler, sys.stdin.buffer, sys.stdout.buffer)
    except InterruptedError:
        pass
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        scheduler.close()
