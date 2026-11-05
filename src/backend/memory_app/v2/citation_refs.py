import logging
from uuid import uuid4

from backend.recognition import WorkScope
from backend.security.audited_records import _AuditedTransaction, WRITES, BINDINGS
from core.storage_provider.sqlite_uow import (
    SQLiteStructuredRecord, SQLiteUnitOfWorkConflict, _payload_json,
    _require_expected_revision, _require_segment,
)
from ..transaction_records import TransactionRecords
from .insights import resolve_insight, source_documents


COLLECTION = 'v2_citation_refs'
_LOGGER = logging.getLogger(__name__)


def _target(reader, project, kind, identity):
    if kind == 'document':
        row = reader.read('documents', identity)
        return row if row is not None and row.payload.get('project_id') == project else None
    if kind == 'insight':
        return resolve_insight(reader, WorkScope('local-user', project), identity)
    if kind == 'inspiration':
        row = reader.read('recognition_experiences', identity)
        return row if row is not None and row.payload.get('scope') == {
            'user_id': 'local-user', 'project_id': project} else None
    raise ValueError('citation_target_kind_invalid')


def _turn(reader, project, turn_id, thread_id, use):
    turn, thread = reader.read('v2_turns', turn_id), reader.read('v2_threads', thread_id)
    if (turn is None or thread is None or turn.payload.get('project_id') != project
            or thread.payload.get('project_id') != project or turn.payload.get('thread_id') != thread_id):
        raise ValueError('citation_turn_binding_invalid')
    if use == 'outcome' and turn.payload.get('intent') == 'ask':
        from backend.recognition.product_draft_dependencies import ANSWER_OUTCOMES, product_draft_source
        marker = reader.read(ANSWER_OUTCOMES, turn_id)
        if marker is None or marker.payload.get('project_id') != project:
            raise ValueError('citation_answer_outcome_unavailable')
        bound = product_draft_source(reader, WorkScope('local-user', project), marker.payload['document_id'], 1)
        if bound is None or bound.answer_birth is None or bound.public_turn_id != turn_id:
            raise ValueError('citation_answer_outcome_unavailable')
    elif turn.payload.get('intent') != {'answer': 'ask', 'outcome': 'do'}.get(use):
        raise ValueError('citation_turn_binding_invalid')
    return turn


class _CitationProjection:
    def __init__(self, tx):
        self.parent = tx._transaction if isinstance(tx, TransactionRecords) else tx
        self.base = self.parent.transaction if isinstance(self.parent, _AuditedTransaction) else self.parent
        self.records = []
        self.writer = self
        if isinstance(self.parent, _AuditedTransaction):
            # 原 owner 保留身份及权限；仅派生写入委托到当前 savepoint。
            self.writer = _AuditedTransaction(self, self.parent.owner, self.parent.access)

    def read(self, collection, identity):
        return self.base.read(collection, identity)

    def list(self, collection):
        return self.base.list(collection)

    def put(self, collection, identity, payload, *, expected_revision):
        self.base._require_open()
        self.base._require_writable()
        if collection not in {COLLECTION, WRITES, BINDINGS} or collection == COLLECTION and expected_revision != 0:
            raise ValueError('citation_projection_write_invalid')
        _require_segment('collection', collection)
        _require_segment('object_id', identity)
        _require_expected_revision(expected_revision)
        current = self.read(collection, identity)
        if (current.revision if current else 0) != expected_revision:
            raise SQLiteUnitOfWorkConflict('citation projection revision changed')
        serialized, stored = _payload_json(payload)
        if current is None:
            self.base.connection.execute('''INSERT INTO crp_structured_records
                (collection, object_id, payload_json, revision) VALUES (?, ?, ?, ?)''',
                (collection, identity, serialized, expected_revision + 1))
        else:
            changed = self.base.connection.execute('''UPDATE crp_structured_records
                SET payload_json=?, revision=? WHERE collection=? AND object_id=? AND revision=?''',
                (serialized, expected_revision + 1, collection, identity, expected_revision))
            if changed.rowcount != 1:
                raise SQLiteUnitOfWorkConflict('citation projection revision changed')
        record = SQLiteStructuredRecord(collection, identity, stored, expected_revision + 1)
        self.records.append(record)
        return record

    def enlist(self):
        # 只有成功释放 savepoint 后才并入原事务清单，失败不留下虚构提交项。
        self.base._records.extend(self.records)
        if self.writer is not self:
            self.parent._business.extend(self.writer._business)


def append_citations(tx, *, project, turn_id, thread_id, use, refs, now):
    _turn(tx, project, turn_id, thread_id, use)
    if not isinstance(now, str) or not now:
        raise ValueError('citation_time_invalid')
    existing = {(row.payload['target_kind'], row.payload['target_id']): row.payload
        for row in tx.list(COLLECTION) if row.payload.get('project_id') == project
        and row.payload.get('turn_id') == turn_id and row.payload.get('use') == use}
    for ref in refs:
        kind, identity, revision = ref['kind'], ref['id'], ref['revision']
        if type(revision) is not int or revision < 1 or _target(tx, project, kind, identity) is None:
            raise ValueError('citation_target_invalid')
        previous = existing.get((kind, identity))
        if previous is not None:
            if previous['target_revision'] != revision or previous['thread_id'] != thread_id:
                raise ValueError('citation_frozen_revision_changed')
            continue
        payload = {'project_id': project, 'target_kind': kind, 'target_id': identity,
            'target_revision': revision, 'use': use, 'turn_id': turn_id,
            'thread_id': thread_id, 'created_at': now}
        identity_key = 'cite-' + uuid4().hex
        tx.put(COLLECTION, identity_key, payload, expected_revision=0)
        existing[(kind, identity)] = payload


def _qualified_targets(reader, project, material):
    if material.get('project_id', project) != project:
        return []
    kind = material.get('type', material.get('kind'))
    identity, revision = material.get('id'), material.get('revision')
    if not isinstance(identity, str) or type(revision) is not int or revision < 1:
        return []
    if kind in {'recognition', 'document'}:
        return [{'kind': 'insight' if kind == 'recognition' else 'document', 'id': identity, 'revision': revision}]
    if kind != 'experience':
        return []
    experience = _target(reader, project, 'inspiration', identity)
    if experience is None or experience.revision != revision:
        return []
    provenance = experience.payload.get('provenance', {})
    if provenance.get('kind') == 'user_statement':
        return [{'kind': 'inspiration', 'id': identity, 'revision': revision}]
    return [{'kind': 'document', 'id': ref['id'], 'revision': ref['revision']}
        for ref in provenance.get('source_refs', []) if ref.get('type') == 'document'
        and isinstance(ref.get('id'), str) and type(ref.get('revision')) is int]


def _reference_keys(kind, identity, namespace):
    collection = {'insight': 'recognitions', 'document': 'documents', 'inspiration': 'recognition_experiences'}[kind]
    refs = {f'crp://default/{collection}/{identity}'}
    if kind == 'document':
        refs.update((f'crp://default/documents/{identity}.json', f'document://{identity}',
                     f'crp://{namespace}/documents/{identity}', f'crp://{namespace}/documents/{identity}.json'))
    return refs


def outcome_citations(tx, *, project, turn_id, evidence_refs, namespace, owner_records):
    from backend.recognition.product_draft_dependencies import product_draft_source
    from ..research_packets import authority_stores
    from ..research_reads import product_read_sources, read_dependencies, turn_tree
    from ..source_egress import SourceEgressService

    if owner_records is None:
        raise ValueError('citation_outcome_owner_unavailable')

    turn = tx.read('v2_turns', turn_id)
    receipt = turn.payload['receipt']['do']
    document = tx.read('documents', receipt['document_id'])
    scope = WorkScope('local-user', project)
    birth = product_draft_source(tx, scope, document.object_id, document.revision)
    if birth is None:
        raise ValueError('citation_outcome_birth_unavailable')
    records = TransactionRecords(tx)
    turns, agents = authority_stores(records)
    product_read_sources(records, turns, agents, birth.request, project,
        authority=SourceEgressService(owner_records, reader=tx))
    completed = next((event for event in reversed(tuple(turns.events_after(birth.request['turn_id'])))
        if event.get('type') == 'turn.completed'), None)
    if completed is None or list(completed['data'].get('evidence_refs', [])) != list(evidence_refs):
        raise ValueError('citation_outcome_completion_changed')
    qualified = {}

    def admit(material):
        aliases = ({f"crp://default/recognition_experiences/{material['id']}"}
            if material.get('type', material.get('kind')) == 'experience' and isinstance(material.get('id'), str) else set())
        for ref in _qualified_targets(tx, project, material):
            for uri in _reference_keys(ref['kind'], ref['id'], namespace) | aliases:
                qualified.setdefault(uri, set()).add((ref['kind'], ref['id'], ref['revision']))

    for material in birth.request['privacy'].get('material_refs', []):
        admit(material)
    for proof in read_dependencies(records, turns, agents, birth.request['turn_id'], project):
        for material in (proof.get('v2_recall') or {}).get('materials', []):
            admit(material)
        for material in proof.get('documents', []):
            admit({'type': 'document', **material})
    identities = set(turn_tree(turns, agents, birth.request['turn_id'], project))
    for operation in tx.list('v2_task_draft_operations'):
        inputs, result = operation.payload.get('inputs', {}), operation.payload.get('result', {})
        if (inputs.get('turn_id') in identities and inputs.get('project_id') == project
                and result.get('receipt_ref') == f'crp://{namespace}/v2_task_draft_operations/{operation.object_id}'):
            admit({'type': 'document', 'id': result.get('document_id'),
                'revision': result.get('document_revision'), 'project_id': project})
    selected = set()
    for uri in evidence_refs:
        matches = qualified.get(uri, set())
        if len(matches) == 1:
            selected.update(matches)
        elif matches:
            _LOGGER.warning('citation_reference_ambiguous')
        else:
            _LOGGER.warning('citation_reference_unresolved')
    return [{'kind': kind, 'id': identity, 'revision': revision}
        for kind, identity, revision in sorted(selected)]


def safe_record_outcome_citations(tx, *, project, turn_id, evidence_refs, now, namespace='default', owner_records=None):
    if not evidence_refs:
        return
    savepoint = False
    try:
        projection = _CitationProjection(tx)
        tx.connection.execute('SAVEPOINT outcome_citation_refs')
        savepoint = True
        refs = outcome_citations(tx, project=project, turn_id=turn_id, evidence_refs=evidence_refs,
            namespace=namespace, owner_records=owner_records)
        turn = tx.read('v2_turns', turn_id)
        append_citations(projection.writer, project=project, turn_id=turn_id, thread_id=turn.payload['thread_id'],
            use='outcome', refs=refs, now=now)
        tx.connection.execute('RELEASE outcome_citation_refs')
    except Exception as error:
        if getattr(tx, 'closed', False):
            raise
        if savepoint:
            tx.connection.execute('ROLLBACK TO outcome_citation_refs')
            tx.connection.execute('RELEASE outcome_citation_refs')
        _LOGGER.warning('citation_write_failed exception_type=%s', type(error).__name__)
        return
    projection.enlist()


def safe_record_restored_citations(tx, *, project, turn_id, refs, now):
    refs = [ref for ref in refs if ref.get('project_id', project) == project
            and ref.get('kind') in {'insight', 'document', 'inspiration'}]
    if not refs:
        return
    savepoint = False
    try:
        projection = _CitationProjection(tx)
        tx.connection.execute('SAVEPOINT restored_citation_refs')
        savepoint = True
        turn = tx.read('v2_turns', turn_id)
        append_citations(projection.writer, project=project, turn_id=turn_id,
            thread_id=turn.payload['thread_id'], use='outcome', refs=refs, now=now)
        tx.connection.execute('RELEASE restored_citation_refs')
    except Exception as error:
        if getattr(tx, 'closed', False):
            raise
        if savepoint:
            tx.connection.execute('ROLLBACK TO restored_citation_refs')
            tx.connection.execute('RELEASE restored_citation_refs')
        _LOGGER.warning('citation_write_failed exception_type=%s', type(error).__name__)
        return
    projection.enlist()


def used_by(reader, project, layer, identity, *, document_ids=()):
    targets = set()
    if layer in {'note', 'summary'}:
        targets.add(('document', identity))
    elif layer == 'insight':
        insight = resolve_insight(reader, WorkScope('local-user', project), identity)
        if insight is not None:
            targets.add(('insight', insight.object_id))
    elif layer == 'inspiration':
        targets.add(('inspiration', identity))
    elif layer == 'source':
        targets.update(('document', value) for value in document_ids)
    result = {}
    for row in reader.list(COLLECTION):
        value = row.payload
        if value.get('project_id') != project:
            continue
        key = value['target_kind'], value['target_id']
        target = _target(reader, project, *key)
        if target is None:
            continue
        selected = key in targets
        if not selected and layer == 'source' and key[0] == 'insight':
            selected = bool(set(document_ids) & source_documents(reader, WorkScope('local-user', project),
                target.payload.get('source_experience_ids', []), target.payload.get('source_recognition_ids', [])))
        if not selected:
            continue
        try:
            turn = _turn(reader, project, value['turn_id'], value['thread_id'], value['use'])
        except ValueError:
            continue
        title = (turn.payload.get('user_text', '') if value['use'] == 'answer'
            else turn.payload.get('receipt', {}).get('do', {}).get('title', ''))
        if value['use'] == 'outcome' and turn.payload.get('intent') == 'ask':
            from backend.recognition.product_draft_dependencies import ANSWER_OUTCOMES
            saved = reader.read(ANSWER_OUTCOMES, turn.object_id)
            document = reader.read('documents', saved.payload['document_id']) if saved is not None else None
            if document is None or document.payload.get('project_id') != project:
                continue
            title = document.payload['title']
        visible = turn.object_id
        if turn.payload.get('parent_turn_id'):
            parent = reader.read('v2_turns', turn.payload['parent_turn_id'])
            if (parent is None or parent.payload.get('project_id') != project
                    or parent.payload.get('thread_id') != value['thread_id']
                    or parent.payload.get('intent') != 'multi'
                    or turn.object_id not in parent.payload.get('part_turn_ids', [])):
                continue
            visible = parent.object_id
        entry_key = visible, value['use']
        changed = target.revision != value['target_revision']
        previous = result.get(entry_key)
        if previous is None or value['created_at'] > previous['created_at']:
            result[entry_key] = {'turn_id': visible, 'thread_id': value['thread_id'], 'use': value['use'],
                'title': title[:40], 'created_at': value['created_at'], 'changed': changed or bool(previous and previous['changed'])}
        elif changed:
            previous['changed'] = True
    return sorted(result.values(), key=lambda row: (row['created_at'], row['turn_id']), reverse=True)[:20]
