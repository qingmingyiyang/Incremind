from pathlib import PurePosixPath, PureWindowsPath
from weakref import WeakSet
from uuid import uuid4

from backend.memory_app.backup_archive import inspect_backup_archive, safe_backup_path, unpack_backup_archive, _scope
from backend.memory_app.server_inspirations import _existing_records, _ReadonlyAuthority, _ReadonlyRecords
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import JsonObjectStore, SourceAssetRuntimeStore
from core.storage_provider.vault_backup_restore import _read_manifest, verify_runtime_backup, verify_vault_backup
from core.storage_provider.record_lineage import HEAD_COLLECTIONS, verify_lineage
from backend.shared.deployment import runtime_backup_roots
from .material_origin_readers import ReadOnlyMaterialDocuments, ReadOnlyMaterialRecords, ReadOnlyMaterialSources
from .shared_projects import SharedProjectError
from .backup_transfers import command_id
from .shared_backup_qualification import read_sources


_ISSUED = WeakSet()
_TOKEN = object()


class ArchiveProject:
    def __init__(self, token, backups, actor, destination, backup_id, stage, snapshot):
        if token is not _TOKEN:
            raise SharedProjectError('shared_backup_archive_owner_required', status=403)
        self.backups, self.actor, self.destination, self.backup_id = backups, actor, destination, backup_id
        self.stage, self.snapshot, self.root = stage, snapshot, snapshot.snapshot_root / 'payload'
        self._root_identity = self._identity(self.root)
        self._manifest_identity = self._identity(snapshot.snapshot_root / 'vault-backup-manifest.json')
        self._active = True
        _ISSUED.add(self)
        row, _lease = backups._row(actor, destination, backup_id)
        self.project_id, self.namespace = row.payload['project_id'], row.payload['source_namespace'].rsplit('/', 1)[1]
        self.backup_time, self.project_name = row.payload['created_at'], row.payload['project_name']
        records = _existing_records(self.root, self.namespace)
        if records is None:
            raise SharedProjectError('shared_backup_archive_records_unavailable', status=409)
        self._database_identity = self._identity(records.database_path)
        self._database_path = records.database_path
        self.records = ReadOnlyMaterialRecords(records.database_path, validate=self.validate_current)
        self.documents = ReadOnlyMaterialDocuments(SQLiteDocumentRepository(self.records, namespace_id=self.namespace),
            validate=self.validate_current)
        factory = AggregateRepositoryFactory(runtime_root=self.root, namespace_id=self.namespace,
            json_store=JsonObjectStore(self.root / '.rebuild-data', legacy_root=self.root / 'library', namespace_id=self.namespace),
            authority_store_factory=_ReadonlyAuthority, record_store_factory=_ReadonlyRecords)
        source = factory.source_asset_authority_resolution()
        source_records = (ReadOnlyMaterialRecords(source.records.database_path, validate=self.validate_current)
                          if source.records is not None else None)
        self._sources = SourceAssetRuntimeStore(json_store=factory.json_store, sqlite_records=source_records,
            library_root=self.root / 'library', authority_identity=source.authority_identity, publication_records=self.records)
        self.sources = ReadOnlyMaterialSources(self._sources, validate=self.validate_current)
        self.files = {item['path']: item for item in _read_manifest(snapshot.snapshot_root)['files']}
        self.qualifications = read_sources(row, snapshot, self.records)
        binding = self.records.read('v2_shared_root', 'identity')
        if binding is None or binding.payload.get('project_id') != self.project_id:
            raise SharedProjectError('shared_backup_archive_scope_changed', status=409)
        for name in ('secrets.json', 'settings.toml', 'config/settings.toml'):
            if name in self.files:
                raise SharedProjectError('shared_backup_archive_scope_invalid', status=409)

    @staticmethod
    def _identity(path):
        info = safe_backup_path(path).stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def validate_current(self):
        if not self._active or self not in _ISSUED:
            raise SharedProjectError('shared_backup_archive_owner_expired', status=409)
        self.backups._archive(self.actor, self.destination, self.backup_id, _content=False)
        if (self._identity(self.root) != self._root_identity
                or self._identity(self.snapshot.snapshot_root / 'vault-backup-manifest.json') != self._manifest_identity
                or self._identity(self._database_path) != self._database_identity):
            raise SharedProjectError('shared_backup_archive_changed', status=409)

    def verify(self):
        self.validate_current()
        self.backups._archive(self.actor, self.destination, self.backup_id)
        observed = verify_vault_backup(self.snapshot.snapshot_root)
        if observed.source_fingerprint != self.snapshot.source_fingerprint:
            raise SharedProjectError('shared_backup_archive_changed', status=409)
        self.validate_current()

    def rows(self, collection):
        values = self.records.list(collection)
        for row in values:
            project = row.payload.get('project_id', row.payload.get('scope', {}).get('project_id'))
            if project is not None and project != self.project_id:
                raise SharedProjectError('shared_backup_archive_scope_invalid', status=409)
            if collection in HEAD_COLLECTIONS:
                head = self.records.read(HEAD_COLLECTIONS[collection], row.object_id)
                if head is None:
                    raise SharedProjectError('shared_backup_archive_lineage_missing', status=409)
                verify_lineage(self.records, {'collection': collection, 'object_id': row.object_id,
                    'fact_id': head.payload['fact_id']})
        return values

    def file(self, recorded):
        self.validate_current()
        if not isinstance(recorded, str) or not recorded:
            raise SharedProjectError('shared_backup_archive_asset_invalid', status=409)
        reference = PureWindowsPath(recorded)
        if '..' in reference.parts:
            raise SharedProjectError('shared_backup_archive_asset_invalid', status=409)
        matches = []
        for name, entry in self.files.items():
            parts = PurePosixPath(name).parts
            if len(reference.parts) >= len(parts) and tuple(part.casefold() for part in reference.parts[-len(parts):]) == tuple(part.casefold() for part in parts):
                matches.append((name, entry))
        if len(matches) != 1:
            raise SharedProjectError('shared_backup_archive_asset_unavailable', status=409)
        name, entry = matches[0]
        path = safe_backup_path(self.root.joinpath(*PurePosixPath(name).parts))
        if not path.is_relative_to(self.root) or not path.is_file() or path.stat().st_size != entry['size']:
            raise SharedProjectError('shared_backup_archive_asset_changed', status=409)
        return {'path': str(path), 'relative_path': name, 'size': entry['size'], 'sha256': entry['sha256']}

    def close(self):
        self._active = False
        _ISSUED.discard(self)


def open_archive_project(backups, actor, destination, backup_id, identity, check, *, input_stage=None):
    archive = backups._archive(actor, destination, backup_id)
    command_id(identity)
    parent = runtime_backup_roots(destination.root)[2].parent / 'bi'
    if input_stage is not None:
        input_stage = command_id(input_stage)
    # 输入目录采用独立短身份，原命令和完整来源绑定保留在个人事务记录中。
    stage = safe_backup_path(parent / (input_stage or uuid4().hex[:16]))
    if not stage.exists():
        inspected = inspect_backup_archive(archive_path=archive.path, limits=backups.limits)
        required = 2 * inspected['unpacked_bytes'] + backups.limits['envelope_bytes']
        cap = check.reserve_up_to(required)
        try:
            if cap.remaining < required:
                from backend.memory_app.server_jobs import QuotaError
                raise QuotaError('storage_quota_exceeded')
            unpacked = unpack_backup_archive(archive_path=archive.path, stage_root=stage,
                expected_scope='shared-project', limits=backups.limits)
            snapshot = unpacked.snapshot
        finally:
            cap.close()
    else:
        snapshot = verify_vault_backup(stage / 'snapshot')
        _scope(snapshot.snapshot_root, 'shared-project')
        if not verify_runtime_backup(snapshot.snapshot_root)['verified']:
            raise SharedProjectError('shared_backup_archive_verification_failed', status=409)
    owner = ArchiveProject(_TOKEN, backups, actor, destination, backup_id, stage, snapshot)
    owner.verify()
    return owner
