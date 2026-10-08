"""Reentrant process and thread locks for JSON object CAS operations."""
from contextlib import contextmanager, nullcontext
from functools import wraps
from threading import RLock, Lock, local

_guard = Lock()
_locks = {}
_owned = local()

@contextmanager
def object_lock(path):
    key = str(path.resolve()).casefold()
    with _guard:
        lock = _locks.setdefault(key, RLock())
    with lock:
        held = getattr(_owned, 'held', None)
        if held is None:
            held = _owned.held = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+b') as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            import os
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def source_locked(method):
    @wraps(method)
    def call(self, collection, object_id, *args, **kwargs):
        with self.locked(collection, object_id):
            return method(self, collection, object_id, *args, **kwargs)
    return call
