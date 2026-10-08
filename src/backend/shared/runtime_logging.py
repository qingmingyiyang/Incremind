"""Application-owned redacted disk logs and transport correlation.

Only records in this application's ASGI/task context reach its handler. No
process-wide levels, factories, or existing handlers are replaced.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
import stat
from threading import RLock
from time import monotonic
from uuid import uuid4
import weakref

from .secret_detection import redact_secrets


@dataclass(frozen=True)
class _Context:
    root: Path
    request_id: str = '-'
    turn_id: str = '-'
    lease: object = None
    correlation: object = None


_context = ContextVar('runtime_log_context', default=None)
_registry = {}
_registry_lock = RLock()
_turn_log = logging.getLogger('chriptmas.turn')
_turn_log.setLevel(logging.INFO)
_LOG_FILE = re.compile(r'app-(\d{4}-\d{2}-\d{2})(?:\.[1-9]\d*)?\.log')
_URL_QUERY = re.compile(r'((?:https?://|/)[^\s"\'<>?]*)\?[^\s"\'<>]*')


def _is_link(path):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(getattr(metadata, 'st_file_attributes', 0)
        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x0400))


def redact_log_text(text):
    return redact_secrets(_URL_QUERY.sub(r'\1', text))


@contextmanager
def runtime_log_scope(root, *, request_id='-', turn_id='-', lease=None, correlation=None):
    token = _context.set(_Context(Path(root).absolute(), request_id, turn_id, lease, correlation))
    try:
        yield
    finally:
        _context.reset(token)


def bind_turn_id(turn_id):
    context = _context.get()
    if context is not None:
        _context.set(replace(context, turn_id=turn_id))
        if context.correlation is not None:
            context.correlation.setdefault('state', {})['runtime_log_turn_id'] = turn_id
        _turn_log.info('turn_bound')


class RuntimeLogHandler(logging.Handler):
    def __init__(self, root, *, now=lambda: datetime.now(timezone.utc)):
        super().__init__()
        self.root = Path(root).absolute()
        self.now = now
        self.max_bytes = 10 * 1024 * 1024
        self.stream = None
        self.day = None
        self.segment = 0
        self.setFormatter(logging.Formatter('%(levelname)s %(name)s %(message)s'))

    def filter(self, record):
        context = _context.get()
        return (context is not None and context.root == self.root
                and (context.lease is None or context.lease.finalizer.alive))

    def _prune(self, today):
        cutoff = today - timedelta(days=13)
        parent = self.root.resolve(strict=True)
        for path in self.root.iterdir():
            match = _LOG_FILE.fullmatch(path.name)
            if match is None or _is_link(path) or not path.is_file():
                continue
            try:
                day = datetime.strptime(match[1], '%Y-%m-%d').date()
            except ValueError:
                continue
            if day < cutoff and path.resolve(strict=True).parent == parent:
                path.unlink()

    def _open(self, day, size):
        if _is_link(self.root):
            raise OSError('log_directory_link')
        if self.day != day:
            if self.stream is not None:
                self.stream.close()
            self.stream = None
            self.day, self.segment = day, 0
            self.root.mkdir(parents=True, exist_ok=True)
            self._prune(day)
        while True:
            suffix = '' if self.segment == 0 else '.' + str(self.segment)
            path = self.root / f'app-{day.isoformat()}{suffix}.log'
            if _is_link(path):
                self.segment += 1
                continue
            length = path.stat().st_size if path.exists() else 0
            if length and length + size > self.max_bytes:
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
                self.segment += 1
                continue
            if self.stream is None:
                self.stream = path.open('ab')
            return

    def emit(self, record):
        try:
            context = _context.get()
            # Format a copy. Exception messages/source lines can contain the
            # request body; retain only the exception's class name.
            safe = logging.makeLogRecord(record.__dict__.copy())
            safe.exc_info = safe.exc_text = safe.stack_info = None
            if record.exc_info:
                safe.msg, safe.args = 'logged_exception', ()
            elif isinstance(safe.msg, BaseException):
                safe.msg, safe.args = type(safe.msg).__name__, ()
            elif isinstance(safe.args, dict):
                safe.args = {key: type(value).__name__ if isinstance(value, BaseException) else value
                             for key, value in safe.args.items()}
            elif isinstance(safe.args, tuple):
                safe.args = tuple(type(value).__name__ if isinstance(value, BaseException) else value for value in safe.args)
            message = self.format(safe)
            if record.exc_info:
                message += ' exception_type=' + record.exc_info[0].__name__
            message = redact_log_text(message).replace('\r', '\\r').replace('\n', '\\n')
            prefix = f'{self.now().isoformat()} request_id={context.request_id} turn_id={context.turn_id} '
            header = prefix.encode('utf8')
            data = message.encode('utf8')
            while data:
                limit = self.max_bytes - len(header) - 1
                chunk = data[:limit]
                if len(data) > limit:
                    chunk = chunk.decode('utf8', errors='ignore').encode('utf8')
                entry = header + chunk + b'\n'
                self._open(self.now().date(), len(entry))
                self.stream.write(entry)
                self.stream.flush()
                data = data[len(chunk):]
        except Exception:
            # Diagnostic writes never change a request or background job result.
            return

    def close(self):
        with self.lock:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
        super().close()


def _acquire(root):
    with _registry_lock:
        if root not in _registry:
            handler = RuntimeLogHandler(root)
            logging.getLogger().addHandler(handler)
            _registry[root] = [handler, 0]
        value = _registry[root]
        value[1] += 1
        return value[0]


def _release(root):
    with _registry_lock:
        value = _registry[root]
        value[1] -= 1
        if value[1] == 0:
            logging.getLogger().removeHandler(value[0])
            value[0].close()
            del _registry[root]


class _Lease:
    # Context copies keep this lease until detached asyncio/threadpool work has
    # finished, including applications used without an ASGI lifespan.
    def __init__(self, root):
        _acquire(root)
        self.finalizer = weakref.finalize(self, _release, root)

    def close(self):
        self.finalizer()


class _RuntimeLoggingMiddleware:
    def __init__(self, app, root, access):
        self.app, self.root, self.access = app, root, access
        self.leases = weakref.WeakSet()

    async def __call__(self, scope, receive, send):
        kind = scope['type']
        if kind not in {'http', 'lifespan', 'websocket'}:
            return await self.app(scope, receive, send)
        lease = _Lease(self.root)
        self.leases.add(lease)
        request_id = uuid4().hex if kind == 'http' else '-'
        started, status = monotonic(), 500
        with runtime_log_scope(self.root, request_id=request_id, lease=lease, correlation=scope if kind == 'http' else None):
            async def correlated_send(message):
                nonlocal status
                if message['type'] == 'http.response.start':
                    status = message['status']
                    message = dict(message)
                    message['headers'] = [(key, value) for key, value in message.get('headers', []) if key.lower() != b'x-request-id'] + [(b'x-request-id', request_id.encode('ascii'))]
                await send(message)
            try:
                await self.app(scope, receive, correlated_send)
            except BaseException as error:
                if kind == 'http':
                    turn_id = scope.get('state', {}).get('runtime_log_turn_id', '-')
                    with runtime_log_scope(self.root, request_id=request_id, turn_id=turn_id, lease=lease):
                        self.access.error('request_failed exception_type=%s', type(error).__name__)
                raise
            finally:
                if kind == 'http':
                    # Endpoint binding reaches this request's server-owned
                    # ASGI state, independent of BaseHTTPMiddleware child tasks.
                    turn_id = scope.get('state', {}).get('runtime_log_turn_id', '-')
                    with runtime_log_scope(self.root, request_id=request_id, turn_id=turn_id, lease=lease):
                        self.access.info('http method=%s path=%s status=%s duration_ms=%s',
                            scope['method'], scope['path'], status, int((monotonic()-started)*1000))
                elif kind == 'lifespan':
                    for owned in tuple(self.leases):
                        owned.close()



def install_runtime_logging(application, layout):
    if getattr(application.state, 'runtime_logging_installed', False):
        return
    root = ((layout.server_root / 'server') if layout.mode == 'server' else layout.user_root) / 'logs'
    root = root.absolute()
    access = logging.getLogger('chriptmas.access.' + uuid4().hex)
    access.setLevel(logging.INFO)
    access.propagate = True
    build = application.build_middleware_stack
    # Wrap outside the original ServerErrorMiddleware so its unchanged default
    # or custom 500 response also gets the correlation header.
    application.build_middleware_stack = lambda: _RuntimeLoggingMiddleware(build(), root, access)
    application.state.runtime_logging_installed = True
    application.state.runtime_log_root = root
