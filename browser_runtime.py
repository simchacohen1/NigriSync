"""Serialize browser work across workers and stop before the cgroup memory limit."""
import contextlib
import fcntl
import multiprocessing
import os
from pathlib import Path
import signal
import time

STARTED_AT = time.time()
LOCK_PATH = '/tmp/nigrisync-browser.lock'

@contextlib.contextmanager
def browser_slot():
    with open(LOCK_PATH, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another browser operation is running. Please try again after it finishes.') from None
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)

MIB = 1024 * 1024


def _int_env(name, default, low, high):
    """Read a bounded integer setting from the environment; fall back safely."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default


# Render Starter = 512 MiB. Both can be changed in the Render Environment tab
# without a code change; the defaults are exactly the previous behavior.
MEMORY_LIMIT_MIB = _int_env('CLASSTIME_MEMORY_LIMIT_MIB', 512, 128, 8192)
STOP_PERCENT = _int_env('CLASSTIME_MEMORY_STOP_PERCENT', 84, 50, 95)

# (current usage, limit, memory.stat, inactive-file keys) for cgroup v2, then v1.
_CGROUPS = [
    ('/sys/fs/cgroup/memory.current', '/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory.stat', ('inactive_file',)),
    ('/sys/fs/cgroup/memory/memory.usage_in_bytes', '/sys/fs/cgroup/memory/memory.limit_in_bytes',
     '/sys/fs/cgroup/memory/memory.stat', ('total_inactive_file', 'inactive_file')),
]


def _read_stat(path):
    values = {}
    try:
        for line in Path(path).read_text().splitlines():
            key, _, number = line.partition(' ')
            if number.strip().isdigit(): values[key] = int(number)
    except OSError: pass
    return values


def memory_snapshot():
    """Container memory as the kernel sees it, or None if it cannot be read.

    raw          = everything charged to the container, INCLUDING reclaimable
                   file cache (downloaded ZIP, PDFs in /tmp, browser binary).
    working_set  = raw minus inactive file cache. This is the number that
                   actually threatens an out-of-memory kill, and it is what
                   the guard compares against the limit.
    """
    for current, maximum, stat_path, inactive_keys in _CGROUPS:
        try:
            limit = int(Path(maximum).read_text())
            raw = int(Path(current).read_text())
        except (OSError, ValueError): continue
        stat = _read_stat(stat_path)
        inactive = next((stat[k] for k in inactive_keys if k in stat), 0)
        return dict(raw=raw, working_set=max(raw - inactive, 0),
                    limit=min(limit, MEMORY_LIMIT_MIB * MIB), stat=stat)
    return None


def memory_usage():
    """(working set bytes, limit bytes), or (0, 0) when the cgroup is unreadable."""
    snapshot = memory_snapshot()
    return (snapshot['working_set'], snapshot['limit']) if snapshot else (0, 0)


_OP_T0 = None  # monotonic start of the current browser operation (set in parent and child)


def log_memory(label):
    """One log line per stage so the Render logs show where memory peaks."""
    try:
        snapshot = memory_snapshot()
        elapsed = (time.monotonic() - _OP_T0) if _OP_T0 else 0.0
        if not snapshot:
            print('Classtime memory [' + label + '] +' + format(elapsed, '.1f') + 's cgroup memory unreadable', flush=True)
            return
        stat, limit = snapshot['stat'], snapshot['limit']
        def mib(*keys):
            for key in keys:
                if key in stat: return format(stat[key] / MIB, '.0f')
            return '?'
        print('Classtime memory [' + label + '] +' + format(elapsed, '.1f') + 's'
              + ' working_set=' + format(snapshot['working_set'] / MIB, '.1f') + 'MiB'
              + ' (' + format(snapshot['working_set'] / limit * 100, '.0f') + '% of ' + format(limit / MIB, '.0f') + 'MiB, stops at '
              + format(limit * STOP_PERCENT / 100 / MIB, '.0f') + 'MiB)'
              + ' raw_incl_cache=' + format(snapshot['raw'] / MIB, '.1f') + 'MiB'
              + ' anon=' + mib('anon', 'total_rss') + ' file_cache=' + mib('file', 'total_cache')
              + ' shmem=' + mib('shmem', 'total_shmem'), flush=True)
    except Exception:
        pass  # Logging must never break a retrieval.


def _worker(send, target, args, kwargs):
    global _OP_T0
    _OP_T0 = time.monotonic()
    os.setsid()
    log_memory('child process started')
    try: send.send(('ok', target(*args, **kwargs)))
    except ValueError as exc: send.send(('error', str(exc)))
    except Exception: send.send(('error', 'Read-only Classtime operation failed. No Nigri data was changed.'))
    finally: send.close()

def _stop(process):
    if process.pid:
        # Kill only the isolated operation's process group, never other jobs.
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        process.join(timeout=1)
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.join(timeout=1)
        if process.is_alive():
            process.kill(); process.join(timeout=1)
    process.close()

def run_browser(target, *args, **kwargs):
    global _OP_T0
    with browser_slot():
        _OP_T0 = time.monotonic()
        log_memory('operation starting (' + getattr(target, '__name__', 'job') + ')')
        used, limit = memory_usage()
        if os.environ.get('RENDER') and not limit:
            raise ValueError('Classtime retrieval is disabled because the service memory limit cannot be monitored safely.')
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(send, target, args, kwargs))
        process.start(); send.close()
        peak = 0; peak_at = 0.0; peak_raw = 0; started = time.monotonic()
        try:
            while True:
                used, limit = memory_usage()  # working set: the number the guard acts on
                if used > peak: peak, peak_at = used, time.monotonic() - started
                snapshot = memory_snapshot()  # raw figure is for the log only
                if snapshot: peak_raw = max(peak_raw, snapshot['raw'])
                # Reserve enough headroom for the API and cleanup to stay alive.
                if limit and used > limit * STOP_PERCENT / 100:
                    log_memory('SAFE-STOP: guard tripped')
                    raise ValueError('Classtime retrieval stopped safely before the service memory limit. Restore a saved review ZIP instead. No Nigri data was changed.')
                if time.monotonic() - started > 240:
                    raise ValueError('Classtime retrieval timed out and its browser was stopped. No Nigri data was changed.')
                if receive.poll(.1):
                    try: status, result = receive.recv()
                    except EOFError: raise ValueError('Classtime browser exited. No Nigri data was changed.') from None
                    if status == 'error': raise ValueError(result)
                    return result
                if not process.is_alive(): raise ValueError('Classtime browser exited. No Nigri data was changed.')
        finally:
            receive.close(); _stop(process)
            print('Classtime operation cleanup complete; peak working-set MiB=' + str(round(peak / MIB, 1))
                  + ' at +' + str(round(peak_at, 1)) + 's (compare with the stage lines above); peak raw incl. file cache MiB='
                  + str(round(peak_raw / MIB, 1)), flush=True)
            log_memory('after cleanup')


def resource_status():
    snapshot = memory_snapshot()
    used, limit = (snapshot['working_set'], snapshot['limit']) if snapshot else (0, 0)
    browsers = 0
    for path in Path('/proc').glob('[0-9]*/status'):
        try:
            text = path.read_text()
            name = text.splitlines()[0].lower()
            if ('chrome' in name or 'chromium' in name) and '\nState:\tZ' not in text: browsers += 1
        except OSError: pass
    return dict(memory_mib=round(used / 1024**2, 1), memory_limit_mib=round(limit / 1024**2, 1),
                memory_raw_incl_cache_mib=round(snapshot['raw'] / 1024**2, 1) if snapshot else 0,
                stop_at_mib=round(limit * STOP_PERCENT / 100 / 1024**2, 1),
                running_browser_processes=browsers, instance_started_at=STARTED_AT)
