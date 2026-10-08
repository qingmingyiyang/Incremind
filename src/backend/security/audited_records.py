"""The single SQLite business-transaction attribution adapter."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from backend.security.user_context import USER_ACCESS
from backend.shared.server_resources import RESOURCE_POOL

WRITES = 'server_admin_writes'
BINDINGS = 'server_admin_write_bindings'

class AuditedRecordStore:
    def __init__(self, records, *, namespace, user_id, clock=None):
        self.records, self.namespace, self.user_id = records, namespace, user_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __getattr__(self, name):
        return getattr(self.records, name)

    def begin(self):
        access = USER_ACCESS.get()
        if access is not None and access.target_user_id != self.user_id:
            raise ValueError('admin_target_mismatch')
        return _AuditedTransaction(self.records.begin(), self, access)

    def writer_for(self, collection, object_id, revision):
        binding = self.records.read(BINDINGS, object_id)
        if binding is None:
            return None
        identity = binding.payload.get(self.namespace, {}).get(collection, {}).get(str(revision))
        row = self.records.read(WRITES, identity) if identity else None
        if (row is None or row.payload['namespace'] != self.namespace or row.payload['collection'] != collection
                or row.payload['object_id'] != object_id or row.payload['object_revision'] != revision
                or row.payload['target_user_id'] != self.user_id):
            return None
        return dict(row.payload)


class _AuditedTransaction:
    def __init__(self, transaction, owner, access):
        self.transaction, self.owner, self.access = transaction, owner, access
        self._business = []

    def __getattr__(self, name):
        return getattr(self.transaction, name)

    def __enter__(self):
        self.transaction.__enter__()
        return self

    def __exit__(self, *args):
        return self.transaction.__exit__(*args)

    def _attribution(self, record, action):
        if self.access is None or self.access.by != 'admin' or record.collection in {WRITES, BINDINGS}:
            return
        identity = 'write-' + uuid4().hex
        self.transaction.put(WRITES, identity, {
            'namespace': self.owner.namespace, 'collection': record.collection,
            'object_id': record.object_id, 'object_revision': record.revision,
            'by': 'admin', 'actor_user_id': self.access.caller.user_id,
            'actor_device_id': self.access.caller.device_id,
            'target_user_id': self.access.target_user_id, 'action': action,
            'at': self.owner.clock().astimezone(timezone.utc).isoformat()}, expected_revision=0)
        # Stable object ID remains valid at the store's 128-character boundary.
        # Nested namespace/collection/revision separates every authority binding.
        current = self.transaction.read(BINDINGS, record.object_id)
        payload = dict(current.payload) if current else {}
        namespace = dict(payload.get(self.owner.namespace, {}))
        revisions = dict(namespace.get(record.collection, {}))
        revisions[str(record.revision)] = identity
        namespace[record.collection] = revisions
        payload[self.owner.namespace] = namespace
        self.transaction.put(BINDINGS, record.object_id, payload, expected_revision=current.revision if current else 0)

    def put(self, *args, **kwargs):
        record = self.transaction.put(*args, **kwargs)
        self._attribution(record, 'write')
        self._business.append(record)
        return record

    def delete(self, *args, **kwargs):
        record = self.transaction.delete(*args, **kwargs)
        self._attribution(record, 'delete')
        self._business.append(record)
        return record

    def commit(self):
        self.transaction.commit()
        return tuple(self._business)


def audit_records(records, runtime_root, namespace):
    resources = RESOURCE_POOL.get()
    if resources is None or records is None or isinstance(records, AuditedRecordStore):
        return records
    root = Path(runtime_root).resolve()
    if root.parent != (resources.server_root / 'users').resolve():
        raise ValueError('admin_target_mismatch')
    return AuditedRecordStore(records, namespace=namespace, user_id=root.name)
