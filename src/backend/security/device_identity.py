"""Request identity seam; desktop authentication remains in its existing owner."""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    user_id: str
    revision: int


@dataclass(frozen=True)
class ServerTicketIdentity:
    caller: DeviceIdentity
    target_user_id: str


def server_mode(connection):
    layout = getattr(connection.app.state, 'deployment', None)
    return layout.mode == 'server' if layout is not None else os.environ.get('CHRIPTMAS_DEPLOY') == 'server'


def server_identity(connection):
    if not server_mode(connection):
        return None
    identity = connection.scope.get('state', {}).get('device_identity')
    registry = getattr(connection.app.state, 'device_registry', None)
    return identity if registry is not None and registry.is_current(identity) else None


def server_authorized(connection):
    return server_identity(connection) is not None
