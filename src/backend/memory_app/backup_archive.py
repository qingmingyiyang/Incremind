from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import unicodedata
import zipfile

from core.storage_provider.vault_backup_restore import (
    VaultBackupSnapshot, _native_io_path, _read_manifest, _write_manifest, _write_verification,
    is_runtime_backup, verification_metadata, verify_runtime_backup, verify_vault_backup,
)
from .server_inspirations import _existing_records
from .v2.shared_projects import segment
from core.storage_provider.sqlite_uow import _create_schema, _schema_ready


_ENVELOPE = 'chriptmas-backup.json'
_MANIFEST = 'vault-backup-manifest.json'
_SCOPES = frozenset({'personal-runtime', 'shared-project'})
_RESERVED = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


class BackupArchiveError(ValueError):
    def __init__(self, code='backup_archive_invalid'):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BackupArchive:
    path: Path
    filename: str
    size_bytes: int
    snapshot_id: str


@dataclass(frozen=True)
class ImportedBackup:
    snapshot: VaultBackupSnapshot
    original_snapshot_id: str
    scope_kind: str


class _BoundedOutput:
    def __init__(self, stream, limit, consume):
        self.stream, self.limit, self.consume = stream, limit, consume
        self.extent = 0

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, value):
        end = self.stream.tell() + len(value)
        if end > self.limit:
            raise BackupArchiveError('backup_archive_limit_exceeded')
        # ZipFile 会回写文件头，只为文件长度实际增长预留空间。
        if self.consume is not None:
            self.consume(max(0, end - self.extent))
        written = self.stream.write(value)
        self.extent = max(self.extent, self.stream.tell())
        return written


def safe_backup_path(value):
    path = Path(value).expanduser().absolute()
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise BackupArchiveError('backup_archive_path_unsafe')
    return path.resolve(strict=False)


def backup_io_path(value):
    return Path(_native_io_path(value))


def _parts(name, limits, *, directory=False):
    if (not isinstance(name, str) or not name or '\\' in name
            or len(name.encode('utf8')) > limits['path_bytes']
            or name.startswith('/') or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise BackupArchiveError('backup_archive_path_unsafe')
    parts = name[:-1].split('/') if directory and name.endswith('/') else name.split('/')
    if (len(parts) > limits['depth'] or any(not part or part in {'.', '..'}
            or part[-1:] in {' ', '.'} or any(char in '<>:"|?*' for char in part)
            or len(part.encode('utf-16-le')) // 2 > limits['component_units'] or _RESERVED.match(part) for part in parts)):
        raise BackupArchiveError('backup_archive_path_unsafe')
    return parts


def _empty_desktop_device_store(snapshot, paths):
    if paths != ['server/devices.sqlite3']:
        return False
    database = safe_backup_path(snapshot / 'payload' / 'server' / 'devices.sqlite3')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        if not _schema_ready(source):
            return False
        with closing(sqlite3.connect(':memory:')) as original:
            # 由原 SQLite owner 建立空结构，只比较读取的快照，绝不初始化输入文件。
            _create_schema(original)
            schema = 'SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name'
            if source.execute(schema).fetchall() != original.execute(schema).fetchall():
                return False
            if source.execute('SELECT key, value FROM crp_uow_meta ORDER BY key').fetchall() != original.execute(
                    'SELECT key, value FROM crp_uow_meta ORDER BY key').fetchall():
                return False
        return (source.execute('SELECT 1 FROM crp_structured_records LIMIT 1').fetchone() is None
            and source.execute('SELECT 1 FROM crp_collection_generations LIMIT 1').fetchone() is None
            and all(source.execute('PRAGMA ' + name).fetchone()[0] == 0 for name in ('user_version', 'application_id')))


def _scope(snapshot, expected):
    if expected not in _SCOPES or not is_runtime_backup(snapshot):
        raise BackupArchiveError('backup_archive_scope_invalid')
    manifest = _read_manifest(snapshot)
    if any(PurePosixPath(item['path']).parts[0].casefold() in {'users', 'shared'} for item in manifest['files']):
        raise BackupArchiveError('backup_archive_scope_invalid')
    server_files = [item['path'] for item in manifest['files']
        if PurePosixPath(item['path']).parts[0].casefold() == 'server']
    if server_files and (expected != 'personal-runtime' or not _empty_desktop_device_store(snapshot, server_files)):
        raise BackupArchiveError('backup_archive_scope_invalid')
    records = _existing_records(snapshot / 'payload', 'default')
    if records is None:
        raise BackupArchiveError('backup_archive_scope_invalid')
    binding = records.read('v2_shared_root', 'identity')
    if binding is not None:
        project_id = segment(binding.payload.get('project_id'))
        if not project_id.startswith('shared-'):
            raise BackupArchiveError('backup_archive_scope_invalid')
    if (binding is not None) != (expected == 'shared-project'):
        raise BackupArchiveError('backup_archive_scope_invalid')


def _zip_metadata(path, limits):
    if backup_io_path(path).stat().st_size > limits['archive_bytes']:
        raise BackupArchiveError('backup_archive_limit_exceeded')
    # 标准库先读取目录记录，限制 ZipFile 一次解析的目录内存。
    with backup_io_path(path).open('rb') as stream:
        end = zipfile._EndRecData(stream)
    if end is None:
        raise BackupArchiveError()
    if (end[zipfile._ECD_ENTRIES_TOTAL] > limits['entries'] or end[zipfile._ECD_SIZE] > limits['central_bytes']):
        raise BackupArchiveError('backup_archive_limit_exceeded')
    if (end[zipfile._ECD_DISK_NUMBER] or end[zipfile._ECD_DISK_START]
            or end[zipfile._ECD_ENTRIES_THIS_DISK] != end[zipfile._ECD_ENTRIES_TOTAL]):
        raise BackupArchiveError()


def _entries(archive, limits):
    entries = archive.infolist()
    if len(entries) > limits['entries']:
        raise BackupArchiveError('backup_archive_limit_exceeded')
    seen, explicit, total = {}, set(), 0
    for info in entries:
        if (info.filename != info.orig_filename or info.flag_bits & 1
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
            raise BackupArchiveError()
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (mode not in {0, stat.S_IFREG, stat.S_IFDIR} or info.external_attr & 0x400
                or mode == stat.S_IFDIR and not info.is_dir()
                or info.external_attr & 0x10 and not info.is_dir()):
            raise BackupArchiveError('backup_archive_path_unsafe')
        parts = _parts(info.filename, limits, directory=info.is_dir())
        if (parts[0] != 'snapshot' and info.filename != _ENVELOPE
                or parts[0] == 'snapshot' and len(parts) > 1
                and parts[1] not in {'payload', _MANIFEST, 'backup-verification.json'}):
            raise BackupArchiveError()
        if (not info.is_dir() and parts[0] == 'snapshot' and len(parts) < 3
                and parts[-1] not in {_MANIFEST, 'backup-verification.json'}):
            raise BackupArchiveError()
        for index in range(1, len(parts) + 1):
            name = '/'.join(parts[:index])
            key = unicodedata.normalize('NFC', name).casefold()
            kind = 'directory' if index < len(parts) or info.is_dir() else 'file'
            if key in seen and seen[key] != (name, kind):
                raise BackupArchiveError('backup_archive_duplicate_path')
            seen[key] = name, kind
        if key in explicit:
            raise BackupArchiveError('backup_archive_duplicate_path')
        explicit.add(key)
        if info.file_size < 0 or info.compress_size < 0:
            raise BackupArchiveError()
        total += info.file_size
        if (info.file_size > limits['entry_bytes'] or total > limits['unpacked_bytes']
                or info.file_size > max(1, info.compress_size) * limits['compression_ratio']
                or info.filename == _ENVELOPE and info.file_size > limits['envelope_bytes']
                or info.filename == 'snapshot/' + _MANIFEST and info.file_size > limits['manifest_bytes']):
            raise BackupArchiveError('backup_archive_limit_exceeded')
    if _ENVELOPE not in {entry.filename for entry in entries} or 'snapshot/' + _MANIFEST not in {entry.filename for entry in entries}:
        raise BackupArchiveError()
    return entries, total


def inspect_backup_archive(*, archive_path: Path, limits: Mapping) -> dict:
    source = safe_backup_path(archive_path)
    _zip_metadata(source, limits)
    with zipfile.ZipFile(backup_io_path(source)) as archive:
        _entries_list, total = _entries(archive, limits)
    return {'size_bytes': backup_io_path(source).stat().st_size, 'unpacked_bytes': total}


def verify_backup_archive(*, archive_path: Path, snapshot_root: Path, expected_scope: str,
                          expected_fingerprint: str, limits: Mapping) -> BackupArchive:
    source, root = safe_backup_path(archive_path), safe_backup_path(snapshot_root)
    snapshot = verify_vault_backup(root)
    if snapshot.source_fingerprint != expected_fingerprint:
        raise BackupArchiveError('backup_archive_source_changed')
    _scope(root, expected_scope)
    manifest, metadata = _read_manifest(root), verification_metadata(root)
    _zip_metadata(source, limits)
    with zipfile.ZipFile(backup_io_path(source)) as archive:
        entries, _total = _entries(archive, limits)
        names = {_ENVELOPE, 'snapshot/' + _MANIFEST,
                 *('snapshot/payload/' + item['path'] for item in manifest['files'])}
        if metadata:
            names.add('snapshot/backup-verification.json')
        if {entry.filename for entry in entries} != names:
            raise BackupArchiveError('backup_archive_source_changed')
        control = {_ENVELOPE: {'schema_version': '1.0.0', 'scope': expected_scope, 'snapshot_id': snapshot.snapshot_id},
                   'snapshot/' + _MANIFEST: manifest}
        if metadata:
            control['snapshot/backup-verification.json'] = metadata
        for name, value in control.items():
            encoded = json.dumps(value, ensure_ascii=name != 'snapshot/' + _MANIFEST, sort_keys=True).encode('utf-8')
            if archive.getinfo(name).file_size != len(encoded):
                raise BackupArchiveError('backup_archive_source_changed')
        envelope = json.loads(archive.read(_ENVELOPE))
        if envelope != {'schema_version': '1.0.0', 'scope': expected_scope, 'snapshot_id': snapshot.snapshot_id}:
            raise BackupArchiveError('backup_archive_scope_invalid')
        if json.loads(archive.read('snapshot/' + _MANIFEST)) != manifest:
            raise BackupArchiveError('backup_archive_source_changed')
        if metadata:
            if json.loads(archive.read('snapshot/backup-verification.json')) != metadata:
                raise BackupArchiveError('backup_archive_source_changed')
        for item in manifest['files']:
            name = 'snapshot/payload/' + item['path']
            if archive.getinfo(name).file_size != item['size']:
                raise BackupArchiveError('backup_archive_source_changed')
            digest, size = hashlib.sha256(), 0
            with archive.open(name) as stream:
                for chunk in iter(lambda: stream.read(limits['chunk_bytes']), b''):
                    size += len(chunk)
                    if size > item['size']:
                        raise BackupArchiveError('backup_archive_source_changed')
                    digest.update(chunk)
            if size != item['size'] or digest.hexdigest() != item['sha256']:
                raise BackupArchiveError('backup_archive_source_changed')
    return BackupArchive(source, 'chriptmas-backup-' + snapshot.snapshot_id + '.zip',
                         backup_io_path(source).stat().st_size, snapshot.snapshot_id)


def pack_backup_archive(*, snapshot_root: Path, destination: Path, scope_kind: str, limits: Mapping,
                        consume: Callable[[int], None] | None = None) -> BackupArchive:
    root, destination = safe_backup_path(snapshot_root), safe_backup_path(destination)
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise BackupArchiveError('backup_archive_destination_overlaps_snapshot')
    snapshot = verify_vault_backup(root)
    _scope(snapshot.snapshot_root, scope_kind)
    if destination.exists():
        raise BackupArchiveError('backup_archive_destination_exists')
    manifest = _read_manifest(snapshot.snapshot_root)
    if len(manifest['files']) + 3 > limits['entries'] or sum(item['size'] for item in manifest['files']) > limits['unpacked_bytes']:
        raise BackupArchiveError('backup_archive_limit_exceeded')
    envelope = {'schema_version': '1.0.0', 'scope': scope_kind, 'snapshot_id': snapshot.snapshot_id}
    backup_io_path(destination.parent).mkdir(parents=True, exist_ok=True)
    with backup_io_path(destination).open('xb') as output:
        with zipfile.ZipFile(_BoundedOutput(output, limits['archive_bytes'], consume), 'w',
                compression=zipfile.ZIP_DEFLATED, compresslevel=limits['compression_level']) as archive:
            archive.writestr(_ENVELOPE, json.dumps(envelope, sort_keys=True))
            archive.writestr('snapshot/' + _MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True))
            metadata = verification_metadata(snapshot.snapshot_root)
            if metadata:
                archive.writestr('snapshot/backup-verification.json', json.dumps(metadata, sort_keys=True))
            for item in manifest['files']:
                name = 'snapshot/payload/' + item['path']
                _parts(name, limits)
                if item['size'] > limits['entry_bytes']:
                    raise BackupArchiveError('backup_archive_limit_exceeded')
                source = safe_backup_path(snapshot.snapshot_root / 'payload' / item['path'])
                digest, size = hashlib.sha256(), 0
                with backup_io_path(source).open('rb') as stream, archive.open(name, 'w', force_zip64=True) as target:
                    for chunk in iter(lambda: stream.read(limits['chunk_bytes']), b''):
                        size += len(chunk)
                        if size > item['size']:
                            raise BackupArchiveError('backup_archive_source_changed')
                        target.write(chunk)
                        digest.update(chunk)
                if size != item['size'] or digest.hexdigest() != item['sha256']:
                    raise BackupArchiveError('backup_archive_source_changed')
        output.flush()
        os.fsync(output.fileno())
    _zip_metadata(destination, limits)
    with zipfile.ZipFile(backup_io_path(destination)) as archive:
        _entries(archive, limits)
    return BackupArchive(destination, 'chriptmas-backup-' + snapshot.snapshot_id + '.zip', backup_io_path(destination).stat().st_size, snapshot.snapshot_id)


def unpack_backup_archive(*, archive_path: Path, stage_root: Path, expected_scope: str,
                          limits: Mapping, snapshot_id: str | None = None) -> ImportedBackup:
    source, stage = safe_backup_path(archive_path), safe_backup_path(stage_root)
    if stage.exists():
        raise BackupArchiveError('backup_archive_destination_exists')
    _zip_metadata(source, limits)
    with zipfile.ZipFile(backup_io_path(source)) as archive:
        entries, total = _entries(archive, limits)
        envelope = json.loads(archive.read(_ENVELOPE))
        if (not isinstance(envelope, dict) or envelope.get('schema_version') != '1.0.0'
                or envelope.get('scope') != expected_scope or expected_scope not in _SCOPES):
            raise BackupArchiveError('backup_archive_scope_invalid')
        backup_io_path(stage.parent).mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(backup_io_path(stage.parent)).free < 2 * total:
            raise BackupArchiveError('backup_archive_space_insufficient')
        backup_io_path(stage).mkdir()
        written = 0
        for info in entries:
            path = stage.joinpath(*_parts(info.filename, limits, directory=info.is_dir()))
            if info.is_dir():
                backup_io_path(path).mkdir(parents=True, exist_ok=True)
                continue
            backup_io_path(path.parent).mkdir(parents=True, exist_ok=True)
            size = 0
            with archive.open(info) as stream, backup_io_path(path).open('xb') as target:
                for chunk in iter(lambda: stream.read(limits['chunk_bytes']), b''):
                    size += len(chunk)
                    written += len(chunk)
                    if size > info.file_size or written > limits['unpacked_bytes']:
                        raise BackupArchiveError('backup_archive_limit_exceeded')
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if size != info.file_size:
                raise BackupArchiveError()
    snapshot = verify_vault_backup(stage / 'snapshot')
    if envelope.get('snapshot_id') != snapshot.snapshot_id:
        raise BackupArchiveError()
    original = snapshot.snapshot_id
    _scope(snapshot.snapshot_root, expected_scope)
    verified = verify_runtime_backup(snapshot.snapshot_root)
    if verified.get('verified') is not True:
        raise BackupArchiveError('backup_verification_failed')
    if snapshot_id is not None:
        manifest = _read_manifest(snapshot.snapshot_root)
        manifest['snapshot_id'] = snapshot_id
        _write_manifest(snapshot.snapshot_root, manifest)
        snapshot = verify_vault_backup(snapshot.snapshot_root)
        verified.update(snapshot_id=snapshot_id, automatic=False, job_id=None)
        _write_verification(snapshot.snapshot_root, verified)
    return ImportedBackup(snapshot, original, expected_scope)
