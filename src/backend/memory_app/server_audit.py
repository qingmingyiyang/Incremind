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
            identity = self._append(tx, {'phase': 'outcome', 'intent_id': intent,
                'actor_user_id': row.payload['actor_user_id'],
                'actor_device_id': row.payload['actor_device_id'],
                'target_user_id': row.payload['target_user_id'], 'status': status})
            tx.commit()
            return identity

    def list_for(self, access):
        authorize_user(self.users, access.caller, access.target_user_id)
        admin = self.users.can(access.caller, action='manage_users')
        return [{'audit_id': row.object_id, **row.payload} for row in self.records.list(AUDIT)
            if admin or row.payload['target_user_id'] == access.caller.user_id]
