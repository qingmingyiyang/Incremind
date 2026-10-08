"""Read usage statistics from a quiescent data-root copy, never the live root."""
from __future__ import annotations
import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from backend.shared.deployment import resolve_deployment
from backend.api.runtime_root_config import load_runtime_root_config, runtime_root_environment_present
from backend.memory_app.kernel.receipt_projection import kernel_call_groups
from backend.memory_app.v2.signals import SignalService
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import JsonObjectStore, RebuildStorageSettings, SQLiteAggregateAuthorityStore, SQLiteStructuredRecordStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
from core.storage_provider.source_asset_runtime import SourceAssetRuntimeStore


def _formal_roots():
    fallback = ROOT / 'runtime'
    layout = resolve_deployment(fallback)
    if layout.mode == 'server':
        return (layout.server_root,)
    if runtime_root_environment_present() or layout.user_root.is_dir():
        config = load_runtime_root_config(layout.user_root)
        return (config.vault_root, config.model_root, config.media_root)
    return (layout.user_root,)


def _copy_root(root):
    candidate = Path(root).expanduser().absolute().resolve(strict=False)
    for formal in _formal_roots():
        formal = formal.resolve(strict=False)
        if candidate.is_relative_to(formal) or formal.is_relative_to(candidate):
            raise ValueError('formal_root_forbidden')
    if not candidate.is_dir():
        raise ValueError('copy_root_missing')
    return candidate


def _inventory(root):
    result = {}
    for path in root.rglob('*'):
        info = path.lstat()
        if (path.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not path.resolve().is_relative_to(root)):
            raise ValueError('copy_member_escape')
        if path.is_file():
            result[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return result


class _ReadConnection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        if ' '.join(sql.upper().split()).startswith(('BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE')):
            raise sqlite3.OperationalError('readonly_write_transaction')
        return super().execute(sql, *args, **kwargs)


def _connect(path):
    connection = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, factory=_ReadConnection)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


class ReadOnlyRecords(SQLiteStructuredRecordStore):
    def _connect(self):
        return _connect(self.database_path)

    def begin(self):
        connection = self._connect()
        connection.execute('BEGIN')
        return SQLiteStructuredRecordUnitOfWork(connection)


class _ReadOnlyAuthority(SQLiteAggregateAuthorityStore):
    def _connect(self):
        return _connect(self._database_path)


class _ReadOnlyJSON(JsonObjectStore):
    def locked(self, collection, object_id):
        return nullcontext()

    def write(self, *args, **kwargs):
        raise ValueError('copy_readonly')

    def delete(self, *args, **kwargs):
        raise ValueError('copy_readonly')


@dataclass
class SignalCopy:
    root: Path
    scratch_root: Path
    records: ReadOnlyRecords
    documents: object
    sources: object
    groups: list

    def answer_input(self, identity):
        matches = [g.get('answer_input') for g in self.groups if g['turn_id'] == identity]
        return matches[0] if len(matches) == 1 else None

    def query(self):
        from backend.memory_app.workspace_query import WorkspaceQuery
        from backend.recognition import RecognitionService
        from tools.memory_eval import NoModels
        source_records = ReadOnlyRecords(self.scratch_root / '.rebuild-data' / 'structured-records.sqlite3')
        if not source_records.database_path.is_file():
            # The existing shared index is absent, not an empty initialized store.
            raise ValueError('copy_source_index_missing')
        return WorkspaceQuery(self.records, self.documents, self.sources, NoModels(), RecognitionService(self.records),
            read_only=True, source_index_records=source_records)


@contextmanager
def open_signal_copy(root):
    source = _copy_root(root)
    before = _inventory(source)
    with TemporaryDirectory(prefix='c19-') as temporary:
        scratch = Path(temporary).resolve()
        for relative in before:
            origin = source / relative
            destination = scratch / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
        if _inventory(source) != before:
            raise ValueError('copy_changed')
        try:
            settings = RebuildStorageSettings.from_toml(ROOT / 'config/rebuild.toml.example', repository_root=ROOT)
            plain = _ReadOnlyJSON(scratch / '.rebuild-data', legacy_root=scratch / 'library', namespace_id=settings.namespace_id)
            factory = AggregateRepositoryFactory(scratch, settings.namespace_id, plain,
                authority_store_factory=_ReadOnlyAuthority, record_store_factory=ReadOnlyRecords)
            document_resolution = factory.document_repository_resolution()
            source_resolution = factory.source_asset_authority_resolution()
            documents = document_resolution.repository
            if isinstance(documents, SQLiteDocumentRepository):
                records = documents.records
            else:
                database = scratch / 'recognition.sqlite3'
                if not database.is_file():
                    raise ValueError('copy_records_missing')
                records = ReadOnlyRecords(database)
            # Verify the existing structured schema through the actual reader.
            records.list('v2_turns')
            sources = SourceAssetRuntimeStore(json_store=plain, sqlite_records=source_resolution.records,
                library_root=scratch / 'library', authority_identity=source_resolution.authority_identity,
                publication_records=ReadOnlyRecords(scratch / '.rebuild-data' / 'structured-records.sqlite3'))
            groups = kernel_call_groups(scratch, remote_only=False, records=records, include_answer_input=True)
            yield SignalCopy(source, scratch, records, documents, sources, groups)
        finally:
            if _inventory(source) != before:
                raise ValueError('copy_changed')


def signal_report(root):
    with open_signal_copy(root) as opened:
        return SignalService(opened.records).report(kernel_groups=opened.groups, read_model_input=opened.answer_input)


def markdown_report(result):
    """Seven compact numeric summaries, preserving unknown coverage."""
    if not result:
        return '# Usage signals\n\n{}\n'
    layers = result['unused']['layers'].values()
    sent = sum(value['sent'] for value in layers)
    cited_values = [value['cited'] for value in result['unused']['layers'].values()]
    cited = sum(cited_values) if all(value is not None for value in cited_values) else None
    after = result['after_answer']
    dwell = result['dwell']
    edits = result['document_edits']
    interruptions = result['interruptions']['turns']
    rows = [
        ('unused', f"sent={sent}; cited={cited}; unknown_turns={result['unused']['unknown_turns']}"),
        ('corrections', f"groups={len(result['corrections']['groups'])}; unknown_events={result['corrections']['unknown_events']}"),
        ('reask', f"pairs={len(result['reask']['pairs'])}"),
        ('after_answer', f"copy={sum(row['copy'] for row in after['turns'])}; do={sum(row['do'] for row in after['turns'])}; open={after['opens']}"),
        ('dwell', 'objects=' + str(len(dwell['objects'])) + '; medians=' + json.dumps([
            {key:row[key] for key in ('policy','median_seconds','known_durations','objects')} for row in dwell['groups']])),
        ('document_edits', f"paragraphs={sum(row['paragraphs'] for row in edits['objects'])}; unknown_versions={edits['unknown_versions']}"),
        ('interruptions', f"stop={sum(row['stop'] for row in interruptions)}; steer={sum(row['steer'] for row in interruptions if row['steer'] is not None)}; steer_unknown={sum(row['steer'] is None for row in interruptions)}"),
    ]
    return '# Usage signals\n\n| metric | values |\n| --- | --- |\n' + ''.join(f'| {name} | {values} |\n' for name, values in rows)


def _output_file(path, source, *, directory=False):
    raw = Path(path).expanduser().absolute()
    for member in (raw, *raw.parents):
        try:
            info = member.lstat()
        except FileNotFoundError:
            continue
        if member.is_symlink() or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError('report_output_forbidden')
    destination = raw.resolve(strict=False)
    for protected in (source, *_formal_roots()):
        protected = protected.resolve(strict=False)
        if destination.is_relative_to(protected) or protected.is_relative_to(destination):
            raise ValueError('report_output_forbidden')
    if directory:
        return destination
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    raise ValueError('report_output_exists')


def _output_directory(directory, source):
    destination = _output_file(directory, source, directory=True)
    dated = destination / f'{datetime.now(timezone.utc):%Y-%m-%d}'
    _output_file(dated.with_suffix('.json'), source)
    _output_file(dated.with_suffix('.md'), source)
    return dated.expanduser().absolute().resolve(strict=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'work/signals')
    args = parser.parse_args(argv)
    try:
        source = _copy_root(args.root)
        output = _output_directory(args.output_dir, source)
        result = signal_report(source)
        output = _output_directory(args.output_dir, source)
    except (ValueError, sqlite3.Error) as error:
        code = str(error) if str(error) in {'report_output_forbidden','report_output_exists'} else 'signal_copy_invalid'
        parser.error(code)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix('.json').open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    with output.with_suffix('.md').open('x', encoding='utf-8') as stream:
        stream.write(markdown_report(result))
    print(json.dumps({'status':'written','sections':len(result)}))


if __name__ == '__main__':
    main()
