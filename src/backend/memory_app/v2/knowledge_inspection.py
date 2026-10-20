"""已有知识事实的内部只读巡检；诊断不充当证据或自动维护动作。"""
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import HTTPException

from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from backend.shared.document_visibility import LegacyDocumentVisibility
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from ..original_sources import document_roots
from ..source_egress import SourceEgressService
from ..workspace_contracts import _project
from .gaps import GAPS, DISMISSALS
from .external_workspace import validate_task_path
from .insights import insight_view
from .links import InsightLinks, RELATED
from .overviews import ScopeOverviews, COLLECTION as OVERVIEWS
from .policies import get
from .policies.rank import freshness
from .policies.types import RankInput, ScopeInput
from .privacy import is_private_project, privacy_revision
from .projects import scene_of
from .signals import SignalService, _fact_time


def _destination(project, layer='insight', **target):
    # 认识只有原资料库列表入口；文稿和对话才带已存在的直达参数。
    return '#' + urlencode({'view': 'library', 'project_id': project, 'layer': layer, **target})


def _identity(value):
    if not isinstance(value, str):
        return False
    try:
        _project(value)
    except HTTPException:
        return False
    return True


class KnowledgeInspection:
    """调用者须停止写入；只读静止原存储，副本可建立辅助文件。"""

    def __init__(self, database_path, *, namespace_id='default', now=lambda: datetime.now(timezone.utc)):
        self.database_path = Path(database_path).absolute()
        self.namespace_id, self.now = namespace_id, now

    def inspect_copy(self, project, *, work_root, source_root=None, scene=None):
        """调用者显式提供获准的业务库和原件owner目录，绝不复制配置或凭据。"""
        project = _project(project)
        database, destination = self.database_path, Path(work_root).absolute()
        source = Path(source_root).absolute() if source_root is not None else None
        for path in (database, destination, *((source,) if source is not None else ())):
            validate_task_path(path)
        if not database.is_file() or not destination.is_dir():
            raise ValueError('inspection_existing_paths_required')
        if destination == database.parent or destination.is_relative_to(database.parent) or database.parent.is_relative_to(destination):
            raise ValueError('inspection_copy_must_be_disjoint')
        if source is not None and (not source.is_dir() or destination == source
                or destination.is_relative_to(source) or source.is_relative_to(destination)):
            raise ValueError('inspection_copy_must_be_disjoint')
        expected_source = database.parent if database.parent.name == '.rebuild-data' else database.parent / '.rebuild-data'
        if source is not None and source != expected_source:
            raise ValueError('inspection_source_owner_mismatch')
        initial = _original_state(database, source)
        target = destination / ('knowledge-inspection-' + uuid4().hex)
        target.mkdir(exist_ok=False)
        # 保留原owner依文件名和父目录名决定的已有布局，不探测替代资料根。
        parent = target / ('.rebuild-data' if database.parent.name == '.rebuild-data' else 'records')
        parent.mkdir()
        copied = parent / database.name
        _copy_database(database, copied)
        records = SQLiteStructuredRecordStore(copied)
        from ..original_sources import source_store
        owner = source_store(records)
        if source is not None:
            _copy_sources(source, owner.root, owner.namespace_id)
        worker = _CopyInspection(records, RecognitionService(records),
            SQLiteDocumentRepository(records, namespace_id=self.namespace_id), self.now)
        result = worker.collect(project, scene)
        if _original_state(database, source) != initial:
            raise RecognitionConflict('inspection_original_changed')
        return {**result, 'scope': 'original_read_only_copy_may_initialize',
                'copy_root': str(target), 'source_owner_supplied': source is not None}


class _CopyInspection:
    def __init__(self, records, service, documents, now):
        self.records, self.service, self.documents, self.now = records, service, documents, now

    def collect(self, project, scene=None):
        project = _project(project)
        if scene is not None and (not isinstance(scene, str) or not scene.strip()):
            raise ValueError('invalid_inspection_scene')
        clock = self.now().astimezone(timezone.utc)
        epoch = privacy_revision(self.records)
        if is_private_project(self.records, project):
            return {'items': [], 'overview_status': 'private_scope'}
        scope = WorkScope('local-user', project)
        authority = SourceEgressService(self.records)
        links = InsightLinks(self.records, self.service)
        proofs, visible, items = [], {}, []

        def in_scene(assigned):
            return get('scope')(ScopeInput(scene, assigned))

        def emit(kind, row, reason, *, href=None, severity='attention'):
            items.append({'kind': kind, 'id': row.object_id, 'revision': row.revision,
                          'reason': reason, 'severity': severity,
                          'href': href or _destination(project)})

        for row in self.records.list('recognitions'):
            if row.payload.get('scope') != {'user_id': scope.user_id, 'project_id': project}:
                continue
            view = insight_view(self.records, scope, row.object_id, service=self.service)
            if not view or view['state'] not in {'active', 'stale'} or not in_scene(view['scene']):
                continue
            try:
                snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': row.object_id,
                                                       'revision': row.revision}])
                authority.require(snapshot, 'generation')
            except RecognitionError:
                continue
            visible[row.object_id] = row
            proofs.append(snapshot)
            if freshness(RankInput(0, 1, view['conditions'], clock.isoformat()))['stale']:
                emit('recognition', row, 'explicit_calendar_expired')

        # 仅本项目拥有且至少一个可见端点的边进入诊断，隐蔽端点编号不输出。
        for collection in (RELATED, 'recognition_relations', 'recognition_relation_proposals'):
            for row in self.records.list(collection):
                value = row.payload
                if collection == 'recognition_relation_proposals' and value.get('state') != 'pending':
                    continue
                a, b = (value.get('a'), value.get('b')) if collection == RELATED else (
                    value.get('from_id'), value.get('to_id'))
                valid_a, valid_b = _identity(a), _identity(b)
                if collection == RELATED:
                    owned = value.get('project_id') == project
                    if not owned:
                        owned = valid_a and valid_b and a in visible and b in visible
                else:
                    owned = value.get('scope') == {'user_id': scope.user_id, 'project_id': project}
                if not owned or not (valid_a and a in visible or valid_b and b in visible):
                    continue
                # 损坏端点只能关联到已获准的锚点；不输出未核验端点的编号或原值。
                if not valid_a or not valid_b:
                    emit('relation', row, 'relation_endpoint_invalid')
                    continue
                if collection != RELATED and not isinstance(value.get('relation'), str):
                    emit('relation', row, 'relation_kind_invalid')
                    continue
                # 其他项目、兄弟场景或私密来源不被解释成损坏，避免披露隐蔽关联。
                endpoints = [self.records.read('recognitions', identity) if isinstance(identity, str) else None
                             for identity in (a, b)]
                if any(endpoint is not None and endpoint.object_id not in visible
                       and endpoint.payload.get('state') == 'active' for endpoint in endpoints):
                    continue
                if any(endpoint is None or links.endpoint(project, endpoint.object_id) is None
                       for endpoint in endpoints):
                    emit('relation', row, 'relation_endpoint_unavailable')
                    continue
                if a not in visible or b not in visible:
                    continue
                if (collection == 'recognition_relation_proposals' and value.get('state') == 'pending'
                        and value.get('relation') in {'refutes', 'supersedes'}):
                    reason = ('relation_endpoint_revision_changed' if any(endpoint.revision != value.get(key)
                              for endpoint, key in zip(endpoints, ('from_revision', 'to_revision')))
                              else 'pending_' + value['relation'])
                    emit('relation_proposal', row, reason)

        overview_status = 'absent'
        overviews = ScopeOverviews(self.records, self.documents, None)
        for row in self.records.list(OVERVIEWS):
            saved = row.payload.get('input_revision')
            if not isinstance(saved, dict) or saved.get('project_id') != project or saved.get('scene') != scene:
                continue
            try:
                if (not isinstance(row.payload.get('text'), str) or not row.payload['text'].strip()
                        or not isinstance(row.payload.get('source_document_ids'), list)
                        or not isinstance(saved.get('members'), list)):
                    overview_status = 'invalid'
                elif saved.get('privacy_revision') != epoch or any(
                        (current := self.records.read('documents', member['id'])) is None
                        or current.payload.get('project_id') != project
                        or current.revision != member['revision'] for member in saved['members']):
                    overview_status = 'stale'
                else:
                    current = overviews.current_metadata(project, scene)
                    # 无法核验可能是过期或保留owner不支持，不冒充确定失效结论。
                    overview_status = 'current' if current is not None else 'unverified'
            except (KeyError, TypeError, ValueError, RecognitionError):
                overview_status = 'invalid'
            if overview_status != 'current':
                emit('overview', row, 'overview_' + overview_status,
                     href=_destination(project, 'summary'))

        self._gaps(project, scene, clock, items)
        visibility = LegacyDocumentVisibility.from_repository(self.documents, project_id=project)
        associated = {identity for row in visible.values()
                      for identity in (insight_view(self.records, scope, row.object_id,
                                      service=self.service) or {}).get('document_ids', [])}
        for document in self.documents.list():
            if document.get('project_id') != project or not visibility.allows(document):
                continue
            row = self.records.read('documents', document['id'])
            assignment = scene_of(self.records, 'document', document['id'])
            if assignment is None:
                item = next((entry for entry in self.records.list('workspace_items')
                    if entry.payload.get('project_id') == project and entry.payload.get('status') == 'confirmed'
                    and entry.payload.get('document_id') == document['id']), None)
                assignment = scene_of(self.records, 'item', item.object_id) if item else None
            assigned = assignment.get('scene') if assignment and assignment.get('project_id') == project else None
            if not in_scene(assigned) or document['id'] in associated:
                continue
            try:
                roots = document_roots(self.records, scope, document.get('source_refs', []))
                snapshot = authority.snapshot(scope, [{'type': kind, 'id': identity, 'revision': revision}
                                                      for kind, identity, revision in roots])
                authority.require(snapshot, 'generation')
            except RecognitionError:
                continue
            proofs.append(snapshot)
            emit('document', row, 'no_visible_active_insight_association', severity='coverage',
                 href=_destination(project, 'note', document_id=document['id']))

        # 输出前重新验证外发资格闭包；巡检本身不外发，也不授予任何后续操作权限。
        if epoch != privacy_revision(self.records) or is_private_project(self.records, project):
            raise RecognitionConflict('inspection_inputs_changed')
        for proof in proofs:
            authority.validate_snapshot(scope, proof)
            authority.require(proof, 'generation')
        return {'items': sorted(items, key=lambda item: (item['kind'], item['id'], item['reason'])),
                'overview_status': overview_status}

    def _gaps(self, project, scene, clock, output):
        settings, _ = SignalService._settings(self.records)
        row = self.records.read(GAPS, project)
        if not settings['enabled'] or row is None:
            return
        cutoff = _fact_time(settings['cleared_at'])
        dismissed = {entry.payload['gap_id'] for entry in self.records.list(DISMISSALS)
                     if entry.payload.get('project_id') == project and _identity(entry.payload.get('gap_id'))}
        retained = row.payload.get('items')
        if not isinstance(retained, list):
            return
        groups = []
        seen = set()
        for item in retained:
            # 既有投影可损坏；逐项核shape后才交纯策略，不让一项破坏其他诊断。
            if (not isinstance(item, dict) or not _identity(item.get('id'))
                    or item['id'] in seen or not _identity(item.get('anchor'))
                    or 'scene' not in item
                    or item.get('scene') is not None and (not isinstance(item['scene'], str) or not item['scene'].strip())
                    or type(item.get('unresolved')) is not bool
                    or not isinstance(item.get('turn_ids'), list) or not item['turn_ids']
                    or any(not _identity(identity) for identity in item['turn_ids'])
                    or len(set(item['turn_ids'])) != len(item['turn_ids'])
                    or item['anchor'] not in item['turn_ids']
                    or _fact_time(item.get('last_at')) is None):
                continue
            seen.add(item['id'])
            anchor = self.records.read('v2_turns', item.get('anchor'))
            if anchor is None:
                continue
            value = anchor.payload
            at = _fact_time(value.get('created_at'))
            if (value.get('project_id') != project or value.get('intent') != 'ask'
                    or value.get('by', 'user') != 'user' or at is None or at > clock
                    or cutoff is not None and at <= cutoff
                    or item.get('anchor_guard') != {'revision': anchor.revision,
                        'created_at': value['created_at'], 'by': value.get('by', 'user')}
                    or not get('scope')(ScopeInput(scene, item.get('scene')))):
                continue
            groups.append(item)
        for item in get('gap')(groups, now=clock, dismissed=dismissed):
            anchor = self.records.read('v2_turns', item['anchor'])
            thread_id = anchor.payload.get('thread_id')
            thread = self.records.read('v2_threads', thread_id) if _identity(thread_id) else None
            if thread is None or thread.payload.get('project_id') != project:
                continue
            # 原问题正文不复制；复用已有工作台轮次定位。
            href = '#' + urlencode({'view': 'workbench', 'project_id': project,
                'turn_id': anchor.object_id, 'thread_id': thread_id})
            output.append({'kind': 'gap', 'id': item['id'], 'revision': row.revision,
                           'reason': 'existing_unresolved_gap', 'severity': 'attention', 'href': href})


def _copy_database(source, target):
    # 静止库才能使用immutable读取，非空WAL明确拒绝，不能忽略已提交但未检查点的数据。
    _require_quiescent_database(source)
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)) as reader:
        reader.execute('PRAGMA query_only=ON')
        if reader.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crp_structured_records'").fetchone() is None:
            raise ValueError('inspection_initialized_store_required')
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer)


def _original_state(database, source):
    _require_quiescent_database(database)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as reader:
        reader.execute('PRAGMA query_only=ON')
        rows = tuple(reader.execute('SELECT collection, object_id, revision, payload_json '
                                    'FROM crp_structured_records ORDER BY collection, object_id'))
    files = [('database', _database_files(database))]
    if source is not None:
        namespace = 'recognition' if database.name == 'recognition.sqlite3' else 'default'
        directory = source / 'objects' / namespace / 'sources'
        if directory.exists():
            for path in sorted(directory.iterdir()):
                validate_task_path(path)
                if path.is_file() and path.suffix in {'.json', '.lock'}:
                    stat = path.stat()
                    files.append((str(path.relative_to(source)), stat.st_size, stat.st_mtime_ns,
                                  path.read_bytes()))
        projected = source / 'structured-records.sqlite3'
        if projected.exists():
            _require_quiescent_database(projected)
            files.append(('projection_database', _database_files(projected)))
    return rows, tuple(files)


def _database_files(database):
    # 物理文件和边车也参加前后比较，防止immutable看不到期间新增的WAL写入。
    result = []
    for suffix in ('', '-wal', '-shm', '-journal'):
        path = database.with_name(database.name + suffix)
        validate_task_path(path)
        if path.exists():
            stat = path.stat()
            result.append((suffix, stat.st_size, stat.st_mtime_ns, path.read_bytes()))
    return tuple(result)


def _require_quiescent_database(database):
    validate_task_path(database)
    for suffix in ('-wal', '-journal'):
        pending = database.with_name(database.name + suffix)
        validate_task_path(pending)
        if pending.exists() and pending.stat().st_size:
            raise ValueError('inspection_quiescent_store_required')


def _copy_sources(source, target, namespace):
    # 仅复制既有来源集合及其SQLite投影，不复制整个运行根、设置或秘密文件。
    directory = source / 'objects' / namespace / 'sources'
    if directory.exists():
        validate_task_path(directory)
        files = tuple(directory.iterdir())
        for path in files:
            validate_task_path(path)
            if not path.is_file() or path.suffix not in {'.json', '.lock'}:
                raise ValueError('inspection_source_owner_entry_invalid')
        copied = target / 'objects' / namespace / 'sources'
        copied.mkdir(parents=True, exist_ok=True)
        for path in files:
            if path.suffix == '.json':
                shutil.copyfile(path, copied / path.name)
    projected = source / 'structured-records.sqlite3'
    if projected.exists():
        validate_task_path(projected)
        target.mkdir(parents=True, exist_ok=True)
        _copy_database(projected, target / projected.name)
