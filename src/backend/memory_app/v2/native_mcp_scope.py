from contextlib import contextmanager
from contextvars import ContextVar


NATIVE_REQUEST = ContextVar('native_mcp_request', default=None)
SCOPES = {'memory:read': frozenset({'projects', 'recall', 'methods', 'read'}),
    'memory:remember': frozenset({'remember'}), 'memory:propose': frozenset({'propose_insight'}),
    'memory:use': frozenset({'report_use'})}


class NativeMcpError(ValueError):
    pass


def validate_scopes(values):
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or value not in SCOPES for value in values)
            or len(values) != len(set(values))):
        raise NativeMcpError('native_mcp_scope_invalid')
    return sorted(values)


def require_native_current():
    lease = NATIVE_REQUEST.get()
    if lease is not None:
        lease.validate()


@contextmanager
def native_request(lease):
    lease.validate()
    token = NATIVE_REQUEST.set(lease)
    try:
        yield lease
    finally:
        NATIVE_REQUEST.reset(token)
