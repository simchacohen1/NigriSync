"""Serialize browser work across workers and stop before the cgroup memory limit."""
import contextlib
import fcntl
import multiprocessing
import os
from pathlib import Path
import signal
import time

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

def memory_usage():
    for current, maximum in [('/sys/fs/cgroup/memory.current','/sys/fs/cgroup/memory.max'),
                              ('/sys/fs/cgroup/memory/memory.usage_in_bytes','/sys/fs/cgroup/memory/memory.limit_in_bytes')]:
        try:
            limit = int(Path(maximum).read_text())
            return int(Path(current).read_text()), limit
        except (OSError, ValueError): pass
    return 0, 0

def _worker(send, target, args, kwargs):
    os.setsid()
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
    with browser_slot():
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(send, target, args, kwargs))
        process.start(); send.close()
        peak = 0; started = time.monotonic()
        try:
            while True:
                used, limit = memory_usage(); peak = max(peak, used)
                # Reserve enough headroom for the API and cleanup to stay alive.
                if limit and used > limit * .84:
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
            print('Classtime browser cleanup complete; peak service memory MiB=' + str(round(peak / 1024 / 1024, 1)), flush=True)
