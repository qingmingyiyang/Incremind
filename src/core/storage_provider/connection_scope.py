"""Bound SQLite connection reuse; transactions and reads remain fresh."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Condition, RLock, get_ident
from time import monotonic
from os.path import abspath, normcase

_SCOPE = ContextVar("sqlite_connection_scope", default=None)


class _Scope:
    def __init__(self):
        self.lock = RLock()
        self.available = Condition(self.lock)
        self.idle = {}
        self.active = {}
        self.ended = False
        self.owners = 1

    def retain(self):
        with self.lock:
            if self.ended:
                raise RuntimeError("request unit of work is closed")
            self.owners += 1
        return self

    def acquire(self, key, factory):
        with self.available:
            if self.ended:
                raise RuntimeError("request unit of work is closed")
            # Short SQL operations hand off one connection across threads. A
            # nested operation in the leasing thread must get an independent
            # connection, particularly when reading beside an active write.
            deadline = monotonic() + 0.05
            while (not self.idle.get(key) and self.active.get(key)
                   and get_ident() not in self.active[key].values()):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break  # A lease holder may be waiting for this worker.
                self.available.wait(remaining)
            available = self.idle.get(key, [])
            connection = available.pop() if available else None
            if connection is None:
                connection = factory()
            self.active.setdefault(key, {})[id(connection)] = get_ident()
        return _Lease(connection, self, key)

    def release(self, key, connection):
        try:
            if connection.in_transaction:
                connection.rollback()
            connection.execute("PRAGMA query_only=OFF")
        except BaseException:
            connection.close()
            with self.available:
                self.active.get(key, {}).pop(id(connection), None)
                self.available.notify_all()
            raise
        with self.available:
            self.active.get(key, {}).pop(id(connection), None)
            if self.ended:
                connection.close()
            else:
                self.idle.setdefault(key, []).append(connection)
            self.available.notify_all()

    def close(self):
        with self.lock:
            self.owners -= 1
            if self.owners:
                return
            self.ended = True
            for available in self.idle.values():
                for connection in available:
                    connection.close()
            self.idle.clear()


class _Lease:
    def __init__(self, connection, scope, key):
        self.connection, self.scope, self.key = connection, scope, key
        self.closed = False

    def __getattr__(self, name):
        if self.closed:
            raise RuntimeError("connection lease is closed")
        return getattr(self.connection, name)

    def close(self):
        if not self.closed:
            self.closed = True
            self.scope.release(self.key, self.connection)

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        try:
            return self.connection.__exit__(*args)
        finally:
            self.close()


@contextmanager
def connection_scope(scope=None):
    scope = scope or _SCOPE.get()
    scope = scope.retain() if scope is not None else _Scope()
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)
        scope.close()


def capture_connection_scope():
    """Retain before scheduling; the receiving owner must call close()."""
    scope = _SCOPE.get()
    return scope.retain() if scope is not None else None


def create_scoped_task(coroutine):
    """Keep request resources alive when HTTP delivery detaches."""
    import asyncio
    scope = capture_connection_scope()
    async def run():
        with connection_scope(scope):
            return await coroutine
    try:
        task = asyncio.create_task(run())
    except BaseException:
        if scope is not None:
            scope.close()
        coroutine.close()
        raise
    def finish(done):
        if scope is not None:
            scope.close()
        if done.cancelled():
            coroutine.close()
    task.add_done_callback(finish)
    return task


def _database_key(path, group='kernel'):
    # Only factories with compatible schema ownership share connections.
    # Stores already resolve their path once on construction. Resolving it on
    # every lease repeats filesystem I/O hundreds of times in one answer.
    return normcase(abspath(path)), group


def _configure(lease, *, row_factory=None, isolation_level="", timeout_ms=5000):
    raw = lease.connection
    raw.row_factory = row_factory
    raw.isolation_level = isolation_level
    raw.execute(f"PRAGMA busy_timeout={timeout_ms}")
    return lease


def borrow_read_connection(path, factory):
    """Reuse an idle initialized database without initializing a missing one."""
    scope = _SCOPE.get()
    key = _database_key(path)
    if scope is not None:
        with scope.available:
            deadline = monotonic() + 0.05
            while (not scope.idle.get(key) and scope.active.get(key)
                   and get_ident() not in scope.active[key].values()):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    break
                scope.available.wait(remaining)
            idle = scope.idle.get(key)
            if idle:
                connection = idle.pop()
                scope.active.setdefault(key, {})[id(connection)] = get_ident()
                lease = _configure(_Lease(connection, scope, key))
                lease.execute('PRAGMA query_only=ON')
                return lease
    return factory()


def reusable_connection(factory=None, *, row_factory=None, isolation_level="", timeout_ms=5000,
                        group='kernel'):
    if factory is None:
        return lambda function: reusable_connection(function, row_factory=row_factory,
            isolation_level=isolation_level, timeout_ms=timeout_ms, group=group)
    @wraps(factory)
    def connect(store):
        scope = _SCOPE.get()
        if scope is None:
            return factory(store)
        path = (getattr(store, "_database_path", None) or getattr(store, "_path", None)
                or store.database)
        lease = scope.acquire(_database_key(path, group), lambda: factory(store))
        return _configure(lease, row_factory=row_factory, isolation_level=isolation_level,
                          timeout_ms=timeout_ms)
    return connect


def with_connection_scope(function):
    import inspect
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            with connection_scope():
                return await function(*args, **kwargs)
        return asynchronous
    @wraps(function)
    def synchronous(*args, **kwargs):
        with connection_scope():
            return function(*args, **kwargs)
    return synchronous
