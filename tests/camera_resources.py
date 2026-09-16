"""Metadata-only Linux sampling for the opt-in camera lifecycle/soak runner."""
import os
from pathlib import Path
import threading
import time


class Resources:
    def __init__(self, pid, interval=.05):
        self.pid, self.interval = pid, interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.ticks = os.sysconf('SC_CLK_TCK')
        self.started = time.monotonic()
        self.camera_seconds = 0.
        self.cpu_seconds = 0.
        self.peak_rss_kib = self.peak_fds = self.peak_camera_owners = 0
        self.samples = 0
        self.idle_rss = []
        self.errors = 0

    def sample(self):
        root = Path('/proc') / str(self.pid)
        children = (root / 'task' / str(self.pid) / 'children').read_text().split()
        rss = fds = owners = 0
        cpu = 0.
        for pid in [str(self.pid), *children]:
            process = Path('/proc') / pid
            try:
                fields = (process / 'stat').read_text().rsplit(')', 1)[1].split()
                # Own user/system plus already-reaped children's CPU time.
                cpu += sum(int(fields[index]) for index in (11, 12, 13, 14)) / self.ticks
                status = (process / 'status').read_text().splitlines()
                rss += next((int(line.split()[1]) for line in status if line.startswith('VmRSS:')), 0)
                handles = list((process / 'fd').iterdir())
                fds += len(handles)
                camera = False
                for handle in handles:
                    try:
                        target = os.readlink(handle)
                        camera |= target.startswith('/dev/video') and target[10:].isdigit()
                    except FileNotFoundError:
                        pass
                owners += int(camera)
            except (FileNotFoundError, ProcessLookupError):
                continue
        return cpu, rss, fds, owners, not children

    def run(self):
        previous = time.monotonic()
        camera_was_open = False
        while not self.stop.is_set():
            now = time.monotonic()
            if camera_was_open:
                self.camera_seconds += now - previous
            previous = now
            try:
                cpu, rss, fds, owners, idle = self.sample()
                self.cpu_seconds = max(self.cpu_seconds, cpu)
                self.peak_rss_kib = max(self.peak_rss_kib, rss)
                self.peak_fds = max(self.peak_fds, fds)
                self.peak_camera_owners = max(self.peak_camera_owners, owners)
                if idle and rss:
                    self.idle_rss.append(rss)
                    # Bounded memory even for a day-long run.
                    if len(self.idle_rss) > 1200:
                        self.idle_rss = self.idle_rss[:600] + self.idle_rss[-600:]
                camera_was_open = owners > 0
                self.samples += 1
            except FileNotFoundError:
                break  # Service teardown ends sampling normally.
            except (OSError, ValueError):
                self.errors += 1
            self.stop.wait(self.interval)

    def start(self):
        self.thread.start()

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=2)
        elapsed = time.monotonic() - self.started
        return {'sampling_seconds': round(elapsed, 3), 'samples': self.samples,
                'sampling_errors': self.errors, 'sample_interval_seconds': self.interval,
                'peak_service_worker_rss_kib': self.peak_rss_kib,
                'peak_service_worker_fds': self.peak_fds,
                'peak_camera_owners': self.peak_camera_owners,
                'cpu_one_core_percent': round(100 * self.cpu_seconds / elapsed, 3),
                'sampled_camera_duty_fraction': round(self.camera_seconds / elapsed, 4),
                'first_idle_rss_kib': self.idle_rss[0] if self.idle_rss else None,
                'last_idle_rss_kib': self.idle_rss[-1] if self.idle_rss else None}
