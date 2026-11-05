from backend.security.device_identity import DeviceIdentity
from backend.security.user_context import USER_ACCESS

from .external_context import ExternalContextError
from .native_mcp_credentials import NativeLease, RESTRICTIONS
from .native_mcp_scope import NATIVE_REQUEST
from .privacy import external_catalog_allowed, is_private_project


class SharedMcpAuthority:
    def __init__(self, application, factory):
        context = application.state.server_context
        if (context.shared_query_factory is not factory or context.shared_projects is not factory.registry
                or context.native_mcp.credentials.records is not factory.registry.records):
            raise ExternalContextError('external_context_owner_changed')
        self.application, self.factory, self.registry = application, factory, factory.registry
        self.credentials = context.native_mcp.credentials

    @staticmethod
    def _fact(lease):
        return {'device_id': lease.identity.device_id, 'user_id': lease.identity.user_id,
            'device_revision': lease.identity.revision, 'restriction_revision': lease.restriction_revision}

    def current(self, client, tool):
        lease = NATIVE_REQUEST.get()
        access = USER_ACCESS.get()
        if (lease is None or lease.owner is not self.credentials or lease.tool != tool
                or lease.restriction.client != client
                or lease.restriction.target_user_id != self.application.state.server_user_id
                or access is None or access.caller != lease.identity
                or access.target_user_id != lease.restriction.target_user_id):
            raise ExternalContextError('external_context_owner_changed')
        lease.validate()
        return self._fact(lease), lease.identity

    def validate(self, central, fact, client, tool):
        if (not isinstance(fact, dict) or set(fact) != {'device_id', 'user_id', 'device_revision', 'restriction_revision'}
                or any(not isinstance(fact[field], str) or not fact[field] for field in ('device_id', 'user_id'))
                or any(type(fact[field]) is not int or fact[field] < 1
                       for field in ('device_revision', 'restriction_revision'))):
            raise ExternalContextError('external_context_binding_invalid')
        row = central.read(RESTRICTIONS, fact['device_id'])
        if row is None or row.revision != fact['restriction_revision']:
            raise ExternalContextError('external_context_owner_changed')
        identity = DeviceIdentity(fact['device_id'], fact['user_id'], fact['device_revision'])
        restriction = self.credentials._restriction(row)
        if (restriction.client != client or restriction.target_user_id != self.application.state.server_user_id):
            raise ExternalContextError('external_context_owner_changed')
        self.credentials.validate_enlisted(central, NativeLease(self.credentials, identity, row.revision, restriction, tool))
        active = NATIVE_REQUEST.get()
        access = USER_ACCESS.get()
        if (active is not None and (active.owner is not self.credentials or self._fact(active) != fact)
                or access is not None and (access.caller != identity or access.target_user_id != restriction.target_user_id)):
            raise ExternalContextError('external_context_owner_changed')
        return identity

    def require_external(self, actor_reader, client, *, project=None, source_reader=None):
        if not external_catalog_allowed(actor_reader, client):
            raise ExternalContextError('external_agent_disabled')
        if project is not None and (source_reader is None or is_private_project(source_reader, project)):
            raise ExternalContextError('external_agent_private')
