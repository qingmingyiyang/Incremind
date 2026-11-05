from contextlib import contextmanager
from contextvars import ContextVar
from threading import get_ident

from backend.recognition import RecognitionConflict


_CURRENT = ContextVar('qualified_material_read_context', default=None)
_SCOPES = {}
_PRIMARY = ContextVar('qualified_primary_input_scope', default=None)
_PRIMARY_INPUTS = {}
_PRIMARY_TYPE = None


def _register_primary_type(value):
    global _PRIMARY_TYPE
    if not isinstance(value, type) or _PRIMARY_TYPE is not None and _PRIMARY_TYPE is not value:
        raise RecognitionConflict('material_origin_prepared_input_type_changed')
    _PRIMARY_TYPE = value


@contextmanager
def prepared_primary_scope(value):
    if _PRIMARY_TYPE is None or type(value) is not _PRIMARY_TYPE or not value._issued():
        raise RecognitionConflict('material_origin_prepared_input_expired')
    token = object()
    _PRIMARY_INPUTS[token] = (value, get_ident())
    previous = _PRIMARY.set(token)
    try:
        yield
    finally:
        _PRIMARY_INPUTS.pop(token, None)
        _PRIMARY.reset(previous)


def is_prepared_primary_input(value):
    saved = _PRIMARY_INPUTS.get(_PRIMARY.get())
    return (_PRIMARY_TYPE is not None and type(value) is _PRIMARY_TYPE and value._issued()
        and saved is not None and saved[0] is value and saved[1] == get_ident())


@contextmanager
def qualified_material_scope(scope):
    token = object()
    _SCOPES[token] = (scope, get_ident())
    previous = _CURRENT.set(token)
    try:
        yield
    finally:
        _SCOPES.pop(token, None)
        _CURRENT.reset(previous)


def current_material_data(registry, actor, lease):
    token = _CURRENT.get()
    if token is None:
        return None
    saved = _SCOPES.get(token)
    if saved is None or saved[1] != get_ident():
        raise RecognitionConflict('material_origin_commit_scope_expired')
    # 资格仍由原实际事务对象完整核验，这里仅传递其受限生命周期。
    return saved[0].material_data(registry, actor, lease)


def qualified_source_records(owner, reader):
    token = _CURRENT.get()
    if token is None:
        return None
    saved = _SCOPES.get(token)
    if saved is None or saved[1] != get_ident():
        raise RecognitionConflict('material_origin_commit_scope_expired')
    return saved[0].source_records(owner, reader)
