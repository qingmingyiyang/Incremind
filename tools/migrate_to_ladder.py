"""Offline, replayable migration of uniquely grounded legacy memory.

Unspecified projections remain in their original authority and are reported;
this tool never calls models or reviews an unreviewed source on the user's behalf.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.v2.layers import mark_verified
from backend.memory_app.v2.projects import _BUILTINS, assign_scene
from backend.memory_app.v2.privacy import is_private_project
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
_COUNTS = {'candidates': 'recognition_candidates', 'recognitions': 'recognitions',
           'experiences': 'recognition_experiences', 'verified': 'v2_verifications'}


def _valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _source_ids(payload):
    return {ref['source_id'] for ref in payload.get('source_refs', ())
            if isinstance(ref, dict) and isinstance(ref.get('source_id'), str)}


def _scene(records, project, name):
    with records.begin() as tx:
        row = tx.read('v2_projects', project)
        if row is None:
            payload = {'name': _BUILTINS.get(project, '默认' if project == 'default' else project),
                       'scenes': [], 'private': is_private_project(tx, project),
                       'builtin': project if project in _BUILTINS else None}
        else:
            payload = dict(row.payload)
        if name not in payload['scenes']:
            tx.put('v2_projects', project, {**payload, 'scenes': sorted([*payload['scenes'], name])},
                   expected_revision=row.revision if row else 0)
        tx.commit()


def migrate(records, documents, legacy, *, reviews=None):
    """Project selected legacy authority through existing domain write services."""
    collections = ('sources', 'workspace_items', 'memory_scenarios', 'library_series',
                   'memory_series_memory', 'project_skills', 'memory_atoms',
                   'memory_publications', 'workspace_review_intents')
    inventory = {collection: tuple(legacy(collection)) for collection in collections}
    legacy = lambda collection: inventory[collection]
    before = {key: len(records.list(collection)) for key, collection in _COUNTS.items()}
    scene_before = sum(len(row.payload['scenes']) for row in records.list('v2_projects'))
    report = {'seen': {collection: len(rows) for collection, rows in inventory.items()},
              'added': {}, 'skipped': [], 'samples': []}
    service = RecognitionService(records)

    def skipped(kind, payload, reason):
        identity = payload.get('id')
        report['skipped'].append({'type': kind, 'id': identity if _valid_id(identity) else None,
                                  'reason': reason})

    def sample(kind, identity, target, state):
        if len(report['samples']) < 20:
            report['samples'].append({'type': kind, 'id': identity if _valid_id(identity) else None,
                                       'target_id': target, 'state': state})

    sources_by_id = {row.get('id'): row for row in legacy('sources')}
    for item in legacy('workspace_items'):
        if item.get('status') != 'confirmed':
            continue
        identity = item.get('id')
        if not _valid_id(identity):
            skipped('workspace_source', item, 'workspace_identity_invalid')
            continue
        source_id = 'source-' + identity
        if not item.get('source_id'):
            skipped('workspace_source', item, 'workspace_source_backfill_required')
            continue
        source = sources_by_id.get(source_id)
        operation = records.read('workspace_confirmation_operations', 'confirm-' + identity)
        if (item['source_id'] != source_id or source is None
                or source.get('workspace_item_id') != identity
                or source.get('confirmation_operation_id') != 'confirm-' + identity
                or operation is None or operation.payload.get('state') != 'committed'
                or operation.payload.get('source_id') != source_id):
            skipped('workspace_source', item, 'workspace_source_binding_unavailable')
        else:
            sample('workspace_source', identity, source_id, 'retained')

    scenarios = tuple(legacy('memory_scenarios'))
    for row in scenarios:
        project, name = row.get('project_id'), row.get('title')
        if (not _valid_id(project) or not isinstance(name, str) or not name.strip()
                or row.get('lifecycle_status', 'active') != 'active' or row.get('stale', False)):
            skipped('scenario', row, 'scenario_scope_or_state_unavailable')
            continue
        _scene(records, project, name.strip())
        sample('scenario', row.get('id'), project, 'scene')

    names = {}
    for row in legacy('library_series'):
        if row.get('status', 'active') == 'active' and isinstance(row.get('name'), str) and row['name'].strip():
            names.setdefault(row.get('id'), set()).add(row['name'].strip())
    for row in legacy('memory_series_memory'):
        projects, name = row.get('project_ids', []), names.get(row.get('series_id'), set())
        if len(name) != 1:
            skipped('series', row, 'series_name_unavailable')
        elif (len(set(projects)) != 1 or not _valid_id(projects[0])
              or row.get('stale', False) or row.get('lifecycle_status', 'active') != 'active'):
            skipped('series', row, 'series_scope_or_state_unavailable')
        else:
            _scene(records, projects[0], next(iter(name)))
            sample('series', row.get('id'), projects[0], 'scene')

    for row in legacy('project_skills'):
        # The legacy schema mixes AI/user rules, style and required context.
        # No plan-defined projection promotes these composite fields to constraints.
        skipped('project_skill', row, 'skill_projection_unspecified')

    all_documents = documents.list(include_archived=True) if documents is not None else ()
    publications = tuple(legacy('memory_publications'))
    for atom in legacy('memory_atoms'):
        identity, revision = atom.get('id'), atom.get('revision')
        linked = [row for row in scenarios if identity in row.get('atom_ids', ())
                  and row.get('lifecycle_status', 'active') == 'active' and not row.get('stale', False)]
        projects = {row.get('project_id') for row in linked}
        if len(projects) != 1 or not _valid_id(next(iter(projects), None)):
            skipped('atom', atom, 'atom_project_ambiguous')
            continue
        project = next(iter(projects))
        scenes = {row.get('title') for row in linked if isinstance(row.get('title'), str) and row['title'].strip()}
        if len(scenes) > 1:
            skipped('atom', atom, 'atom_scene_ambiguous')
            continue
        candidate_id, recognition_id = f'candidate-legacy-{identity}-r{revision}', f'recognition-legacy-{identity}-r{revision}'
        if (not _valid_id(identity) or not _valid_id(candidate_id) or not _valid_id(recognition_id)
                or type(revision) is not int or revision < 1 or not isinstance(atom.get('content'), str)
                or not atom['content'].strip()):
            skipped('atom', atom, 'atom_record_invalid')
            continue
        if atom.get('lifecycle_status', 'active') != 'active':
            skipped('atom', atom, 'atom_inactive')
            continue
        sources = _source_ids(atom) | ({atom['source_id']} if isinstance(atom.get('source_id'), str) else set())
        docs = [doc for doc in all_documents if doc.get('project_id') == project
                and doc.get('status') != 'archived' and sources.intersection(_source_ids(doc))]
        if len(docs) != 1:
            skipped('atom', atom, 'atom_document_unavailable' if not docs else 'atom_document_ambiguous')
            continue
        try:
            experience, _ = ensure_document_experience(documents, service, project, docs[0]['id'])
            scope = WorkScope('local-user', project)
            current = records.read('recognition_candidates', candidate_id)
            if current is None:
                current = service.propose(scope=scope, content=atom['content'],
                    source_experience_ids=[experience], candidate_id=candidate_id)
                current = records.read('recognition_candidates', current.id)
            if (current.payload.get('project_id') != project or current.payload.get('content') != atom['content'].strip()
                    or current.payload.get('source_experience_ids') != [experience]):
                skipped('atom', atom, 'atom_target_conflict')
                continue
            proof = any(p.get('published_object_id') == identity and p.get('published_revision') == revision
                and p.get('layer') == 'atom' and p.get('object_type') == 'atom'
                and p.get('status') == 'published' and p.get('reviewer') == 'user'
                and isinstance(p.get('review_ref'), str) and p['review_ref'].strip() for p in publications)
            if proof and current.payload['state'] == 'pending':
                service.publish(scope=scope, candidate_id=candidate_id, expected_revision=current.revision,
                                reviewer='user', recognition_id=recognition_id)
                current = records.read('recognition_candidates', candidate_id)
            if scenes:
                assign_scene(records, 'candidate', candidate_id, project, next(iter(scenes)), if_absent=True)
                if current.payload['state'] == 'published':
                    assign_scene(records, 'recognition', current.payload['recognition_id'], project,
                                 next(iter(scenes)), if_absent=True)
            sample('atom', identity, candidate_id, current.payload['state'])
        except Exception:
            skipped('atom', atom, 'atom_domain_evidence_unavailable')

    for intent in legacy('workspace_review_intents'):
        if intent.get('state') != 'confirmed':
            skipped('source', intent, 'source_not_confirmed')
            continue
        if reviews is None or documents is None:
            skipped('source', intent, 'document_cutover_required')
            continue
        try:
            projection = reviews.get(intent['source_id'], intent['project_id'])
            if (projection is None or projection['status'] != 'confirmed'
                    or projection['document_id'] != intent.get('document_id')
                    or projection['document_revision'] != intent.get('document_revision')):
                raise ValueError('binding')
            mark_verified(records, projection['document_id'], projection['document_revision'])
            sample('source', intent['source_id'], projection['document_id'], 'verified')
        except Exception:
            skipped('source', intent, 'source_confirmation_binding_unavailable')
    report['added'] = {key: len(records.list(collection)) - before[key] for key, collection in _COUNTS.items()}
    report['added']['scenes'] = sum(len(row.payload['scenes']) for row in records.list('v2_projects')) - scene_before
    report['skipped_counts'] = {}
    for row in report['skipped']:
        reason = row['reason']
        report['skipped_counts'][reason] = report['skipped_counts'].get(reason, 0) + 1
    return report


def _execute(root):
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.memory_app.legacy_intake_review import LegacyIntakeReview
    from core.aggregate_repository_factory import AggregateRepositoryFactory
    from core.storage_provider import SQLiteAggregateAuthorityStore
    store, settings = build_rebuild_object_store(root)
    factory = AggregateRepositoryFactory(root, settings.namespace_id, store)
    authority = factory.memory_publication_authority_resolution()
    selected = factory.document_repository_resolution().repository
    documents = selected if isinstance(selected, SQLiteDocumentRepository) else None
    if documents is not None:
        doc_authority = SQLiteAggregateAuthorityStore(root / '.rebuild-data' / 'aggregate-authority.sqlite3').get(settings.namespace_id, 'documents')
        if doc_authority.evidence.verification_method != 'exact_records':
            documents = None
    records = (documents.records if documents is not None else
               SQLiteStructuredRecordStore(root / 'recognition.sqlite3'))
    def legacy(collection):
        if collection in {'memory_atoms', 'memory_scenarios', 'memory_series_memory', 'memory_publications', 'project_skills'}:
            if authority.records is not None:
                return tuple(row.payload for row in authority.records.list(collection))
        if collection in {'workspace_review_intents', 'workspace_items'}:
            return tuple(row.payload for row in records.list(collection))
        return store.list(collection)
    reviews = LegacyIntakeReview(root, records, documents, object_store=store) if documents is not None else None
    return migrate(records, documents, legacy, reviews=reviews)


def _filesystem_path(path):
    """Use native extended Windows paths without changing repository semantics."""
    path = Path(path).expanduser().absolute()
    value = str(path)
    if os.name != 'nt' or value.startswith('\\\\?\\'):
        return path
    return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)


def _sqlite_read_uri(path):
    # Encode the native pathname as an opaque file: path, not a URI authority.
    # Path.as_uri() treats the extended prefix as the invalid host "%3F".
    return 'file:' + quote(str(path), safe='/:') + '?mode=ro'


def run_migration(app_root, *, dry_run):
    root = _filesystem_path(app_root).resolve(strict=True)
    if dry_run:
        with tempfile.TemporaryDirectory(prefix='chriptmas-ladder-') as temporary:
            temporary_root = _filesystem_path(temporary).resolve(strict=True)
            clone = temporary_root / 'runtime'
            try:
                shutil.copytree(root, clone, ignore=shutil.ignore_patterns('*.sqlite3', '*-wal', '*-shm'))
                for path in root.rglob('*.sqlite3'):
                    destination = clone / path.relative_to(root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with closing(sqlite3.connect(_sqlite_read_uri(path), uri=True)) as source, closing(sqlite3.connect(str(destination))) as target:
                        source.backup(target)
                report = _execute(clone)
            finally:
                # Remove only this tool's clone using the same native long-path
                # spelling; TemporaryDirectory then removes its short parent.
                if clone.parent != temporary_root:
                    raise RuntimeError('migration_temporary_boundary_invalid')
                if clone.exists():
                    shutil.rmtree(clone)
    else:
        report = _execute(root)
    return {'mode': 'dry-run' if dry_run else 'commit', **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--commit', action='store_true')
    args = parser.parse_args()
    try:
        report = run_migration(args.app_root, dry_run=args.dry_run)
    except Exception:
        print(json.dumps({'error': 'migration_authority_unavailable'}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
