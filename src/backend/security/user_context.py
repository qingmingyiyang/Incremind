"""Fresh server permissions, with a distinct caller and selected user space."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .device_identity import DeviceIdentity
from pathlib import Path


@dataclass(frozen=True)
class UserAccess:
    caller: DeviceIdentity
    target_user_id: str
    by: str | None


USER_ACCESS = ContextVar('server_user_access', default=None)


class UserError(ValueError):
    """Only fixed, non-sensitive public codes."""


def user_action_allowed(role, caller_user_id, target_user_id, action):
    """One permission rule; owners supply freshly read identity facts."""
    if role not in {'admin', 'user'}:
        return False
    if action in {'space', 'pair_device'}:
        return role == 'admin' or caller_user_id == target_user_id
    if action in {'manage_users', 'manage_resources'}:
        return role == 'admin'
    return False


def json_attribution(runtime_root, namespace):
    """Explicit factory capability, with the real child root frozen at assembly."""
    from backend.shared.server_resources import RESOURCE_POOL
    resources = RESOURCE_POOL.get()
    if resources is None:
        return None
    root = Path(runtime_root).resolve()
    def bind(collection, object_id, revision):
        access = USER_ACCESS.get()
        if access is None:
            return None
        expected = (resources.server_root / 'users' / access.target_user_id).resolve()
        if expected != root or not expected.is_relative_to(resources.server_root / 'users'):
            raise ValueError('admin_target_mismatch')
        if access.by != 'admin':
            return None
        return {'by': 'admin', 'actor_user_id': access.caller.user_id,
            'target_user_id': access.target_user_id, 'device_id': access.caller.device_id,
            'namespace_id': namespace, 'collection': collection, 'object_id': object_id,
            'revision': revision, 'action': 'write'}
    return bind


def authorize_user(users, identity, target_user_id=None):
    target, by = users.authorize_claim(identity, target_user_id)
    return UserAccess(identity, target, by)


@contextmanager
def user_context(access):
    token = USER_ACCESS.set(access)
    try:
        yield access
    finally:
        USER_ACCESS.reset(token)
