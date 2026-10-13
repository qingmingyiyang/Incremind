"""服务器外部任务借用当前已鉴权设备，仅在同一child内保留短期内存凭据。"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

from backend.security.user_context import USER_ACCESS, authorize_user
from .external_workspace import task_path_identity


_CURRENT = ContextVar('external_server_memory_request', default=None)


class ServerMemoryLease:
    def __init__(self, state, identity, access, key):
        self.state, self.identity, self.access = state, identity, access
        self._key = key
        self.target = access.target_user_id
        self.root = state.recognition_runtime_root
        self.root_identity = task_path_identity(self.root, directory=True)
        self.context = state.server_context
        self.expires = time.monotonic() + 1320
        self.validate()

    def __repr__(self):
        return 'ServerMemoryLease()'

    def validate(self):
        if (not self._key or time.monotonic() >= self.expires
                or self.state.deployment.mode != 'server'
                or self.state.server_context is not self.context
                or self.state.server_user_id != self.target
                or self.state.recognition_runtime_root != self.root
                or self.context.users.root_for(self.target) != self.root
                or self.context.users.get(self.target)['disabled_at'] is not None
                or task_path_identity(self.root, directory=True) != self.root_identity
                or not self.context.registry.is_current(self.identity)
                or authorize_user(self.context.users, self.identity, self.target) != self.access):
            raise ValueError('external_server_identity_unavailable')

    def environment(self):
        self.validate()
        return {'CHRIPTMAS_DEVICE_KEY': self._key, 'CHRIPTMAS_MCP_TARGET_USER': self.target}

    def close(self):
        self._key = ''


@contextmanager
def request_memory_identity(request, state):
    """只消费原dispatcher已核的请求，不从请求正文或任意header声明注册用户。"""
    if state.deployment.mode != 'server':
        yield
        return
    context = getattr(state, 'server_context', None)
    scope = request.scope.get('state', {})
    access, identity = scope.get('user_access'), scope.get('device_identity')
    values = request.headers.getlist('authorization')
    if (context is None or scope.get('server_dispatch_authority') is not context.auth
            or access is None or access != USER_ACCESS.get() or identity != access.caller
            or len(values) != 1 or not values[0].startswith('Bearer ')):
        raise ValueError('external_server_identity_unavailable')
    key = values[0][7:]
    if (not 1 <= len(key) <= 4096 or any(not 33 <= ord(value) <= 126 for value in key)
            or context.registry.authenticate(key) != identity):
        raise ValueError('external_server_identity_unavailable')
    binding = ServerMemoryLease(state, identity, access, key)
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        _CURRENT.reset(token)
        # 成功准备的任务由Host接管，HTTP结束不能撤销已经接受的任务。
        if not getattr(binding, '_adopted', False):
            binding.close()


def current_memory_identity(state):
    value = _CURRENT.get()
    if not isinstance(value, ServerMemoryLease) or value.state is not state:
        raise ValueError('external_server_identity_unavailable')
    value.validate()
    return value
