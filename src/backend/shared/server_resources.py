"""Explicit shared model weights; user data, outputs and credentials stay outside."""
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import RLock

RESOURCE_POOL = ContextVar('server_resources', default=None)


class SharedModelUnavailable(ValueError):
    def __init__(self):
        super().__init__('shared_model_not_installed')


def _materialize(value, control_check=None):
    if isinstance(value, Iterator):
        values=[]
        while True:
            if control_check is not None:
                control_check()
            try:
                part=next(value)
            except StopIteration:
                break
            if control_check is not None:
                control_check()
            values.append(part)
        return iter(values)
    if isinstance(value, tuple):
        return tuple(_materialize(part,control_check) for part in value)
    return value


class _LockedModel:
    def __init__(self, engine):
        self.engine, self.lock = engine, RLock()

    def __getattr__(self, name):
        value = getattr(self.engine, name)
        if not callable(value):
            return value
        def invoke(*args, **kwargs):
            with self.lock:
                # Whisper/embedding yield inference lazily. Finish the provider
                # iterator under the same lock, then hand detached results out.
                return _materialize(value(*args, **kwargs))
        return invoke

    def controlled_call(self, name, control_check, *args, **kwargs):
        with self.lock:
            if control_check is not None:
                control_check()
            return _materialize(getattr(self.engine,name)(*args,**kwargs),control_check)


class SharedResources:
    def __init__(self, server_root):
        self.server_root = Path(server_root).resolve()
        self.model_root = self.server_root / 'models'
        self._models, self._lock = {}, RLock()
        # 原留痕工厂由服务器装配，资源层不反向导入安全层。
        self.provider_file_attribution_factory = None

    def model_path(self, *parts):
        path = self.model_root.joinpath(*parts).resolve()
        if not path.is_relative_to(self.server_root) or not path.is_relative_to(self.model_root):
            raise SharedModelUnavailable()
        return path

    def acquire(self, kind, identity, loader):
        key = (kind, tuple(identity))
        with self._lock:
            if key not in self._models:
                self._models[key] = _LockedModel(loader())
            return self._models[key]

    def load_value(self, kind, identity, loader):
        return self.acquire(kind, identity, loader).engine


def shared_model(kind, identity, loader):
    pool = RESOURCE_POOL.get()
    return loader() if pool is None else pool.acquire(kind, identity, loader)


@contextmanager
def resource_context(pool):
    token = RESOURCE_POOL.set(pool)
    try:
        yield pool
    finally:
        RESOURCE_POOL.reset(token)
