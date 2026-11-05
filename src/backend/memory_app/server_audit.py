"""Durable central access intents and attribution in the actual business UoW."""
from datetime import datetime, timezone
from uuid import uuid4
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from backend.security.user_context import USER_ACCESS, authorize_user, json_attribution

from backend.security.audited_records import AuditedRecordStore, WRITES, BINDINGS
AUDIT = 'admin_audit'
AUDIT_REQUEST = ContextVar('server_audit_request', default=None)


class AuditRequest:
    def __init__(self, owner, intent, access, application):
        self.owner, self.intent, self.access, self.application = owner, intent, access, application
        self.observations = []
        self.active = True

    def validate(self, records, *, method, path):
        state = self.application.state
        access = self.access
        if (not self.active or USER_ACCESS.get() != access
                or authorize_user(self.owner.users, access.caller, access.target_user_id) != access
                or not state.server_context.registry.is_current(access.caller)
                or state.server_user_id != access.target_user_id
                or state.recognition_records is not records
                or Path(state.recognition_runtime_root).resolve() != self.owner.users.root_for(access.target_user_id).resolve()
                or not Path(records.database_path).resolve().is_relative_to(Path(state.recognition_runtime_root).resolve())):
            raise ValueError('admin_audit_binding_invalid')
        row = self.owner.records.read(AUDIT, self.intent)
        if (row is None or row.payload.get('phase') != 'intent'
                or row.payload.get('method') != method or row.payload.get('path') != path
                or any(item.payload.get('phase') == 'outcome' and item.payload.get('intent_id') == self.intent
                       for item in self.owner.records.list(AUDIT))
                or any(row.payload.get(key) != value for key, value in {
                    'actor_user_id': access.caller.user_id, 'actor_device_id': access.caller.device_id,
                    'target_user_id': access.target_user_id}.items())):
            raise ValueError('admin_audit_binding_invalid')


class AdminAudit:
    def __init__(self, users, *, clock=None):
        self.users, self.records = users, users.records
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def request_scope(self, intent, access, application):
        binding = AuditRequest(self, intent, access, application) if intent is not None else None
        token = AUDIT_REQUEST.set(binding)
        try:
            yield binding
        finally:
            if binding is not None:
                binding.active = False
            AUDIT_REQUEST.reset(token)

    def _append(self, tx, payload):
        identity = 'audit-' + uuid4().hex
        tx.put(AUDIT, identity, {**payload, 'at': self.clock().astimezone(timezone.utc).isoformat()}, expected_revision=0)
        return identity

    def intent(self, access, *, method, path):
        current = authorize_user(self.users, access.caller, access.target_user_id)
        if current != access:
            raise ValueError('admin_identity_changed')
        if access.by != 'admin':
            return None
        # Store route/action identity only. No query, body, headers or title input.
        if '?' in path or '#' in path or len(path) > 500 or not path.startswith('/'):
            raise ValueError('admin_audit_path_invalid')
        from .v2.server_users import USERS, DELETIONS
        with self.records.begin() as tx:
            # 当前身份、目标存在性与追加共用短事务，不能在 purge 后复活旧资格。
            self.users._admin(tx, access.caller)
            if (tx.read(USERS, access.target_user_id) is None
                    or (tx.read(DELETIONS, access.target_user_id) is not None
                        and not (method == 'DELETE' and path == '/api/v2/server/users/{user_id}'))):
                raise ValueError('admin_identity_changed')
            identity = self._append(tx, {'phase': 'intent', 'actor_user_id': access.caller.user_id,
                'actor_device_id': access.caller.device_id, 'target_user_id': access.target_user_id,
                'method': method, 'path': path})
            tx.commit()
            return identity

    def outcome(self, intent, *, status):
        if intent is None:
            return None
        with self.records.begin() as tx:
            row = tx.read(AUDIT, intent)
            if row is None or row.payload['phase'] != 'intent':
                raise ValueError('admin_audit_intent_invalid')
            binding = AUDIT_REQUEST.get()
            observations = []
            if binding is not None and binding.owner is self and binding.intent == intent:
                observations = list(binding.observations)
            identity = self._append(tx, {'phase': 'outcome', 'intent_id': intent,
                'actor_user_id': row.payload['actor_user_id'],
                'actor_device_id': row.payload['actor_device_id'],
                'target_user_id': row.payload['target_user_id'], 'status': status,
                **({'observations': observations} if observations else {})})
            tx.commit()
            return identity

    def list_for(self, access):
        authorize_user(self.users, access.caller, access.target_user_id)
        admin = self.users.can(access.caller, action='manage_users')
        return [{'audit_id': row.object_id, **row.payload} for row in self.records.list(AUDIT)
            if admin or row.payload['target_user_id'] == access.caller.user_id]

    def entries_for(self, access):
        rows = self.list_for(access)
        outcomes = {}
        for row in rows:
            if row.get('phase') == 'outcome':
                outcomes.setdefault(row.get('intent_id'), []).append(row)
        return [{**row, 'outcomes': [
            {'audit_id': outcome['audit_id'], 'at': outcome['at'], 'status': outcome['status']}
            for outcome in outcomes.get(row['audit_id'], [])
            if all(outcome.get(key) == row.get(key) for key in ('actor_user_id', 'actor_device_id', 'target_user_id'))],
            'business_state': 'unknown'} for row in rows if row.get('phase') == 'intent']
