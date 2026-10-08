"""Durable central access intents and attribution in the actual business UoW."""
from datetime import datetime, timezone
from uuid import uuid4

from backend.security.user_context import USER_ACCESS, authorize_user, json_attribution

from backend.security.audited_records import AuditedRecordStore, WRITES, BINDINGS
AUDIT = 'admin_audit'


class AdminAudit:
    def __init__(self, users, *, clock=None):
        self.users, self.records = users, users.records
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _append(self, payload):
        identity = 'audit-' + uuid4().hex
        with self.records.begin() as tx:
            tx.put(AUDIT, identity, {**payload, 'at': self.clock().astimezone(timezone.utc).isoformat()}, expected_revision=0)
            tx.commit()
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
        return self._append({'phase': 'intent', 'actor_user_id': access.caller.user_id,
            'actor_device_id': access.caller.device_id, 'target_user_id': access.target_user_id,
            'method': method, 'path': path})

    def outcome(self, intent, *, status):
        if intent is None:
            return None
        row = self.records.read(AUDIT, intent)
        if row is None or row.payload['phase'] != 'intent':
            raise ValueError('admin_audit_intent_invalid')
        return self._append({'phase': 'outcome', 'intent_id': intent,
            'actor_user_id': row.payload['actor_user_id'],
            'actor_device_id': row.payload['actor_device_id'],
            'target_user_id': row.payload['target_user_id'], 'status': status})

    def list_for(self, access):
        authorize_user(self.users, access.caller, access.target_user_id)
        admin = self.users.can(access.caller, action='manage_users')
        return [{'audit_id': row.object_id, **row.payload} for row in self.records.list(AUDIT)
            if admin or row.payload['target_user_id'] == access.caller.user_id]
