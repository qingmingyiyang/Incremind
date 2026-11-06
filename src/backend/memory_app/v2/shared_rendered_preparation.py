from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import get_ident

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition_retrieval.service import _entry, _required_string
from core.document_engine import SQLiteDocumentRepository
from core.search_and_recall.evidence_windows import EvidenceWindow
from ..context_adapter import format_recognition_content
from ..kernel.answer_continuations import decode
from ..transaction_records import TransactionRecords
from .agentic_rag import _frozen_candidates
from .layers import summary_of
from .shared_rendered_input import validate_rendered_capsule_shape
from .shared_vector_keys import rendered_chunk_text


_CURRENT = ContextVar('shared_rendered_wire_preparation', default=None)
_PREPARED = {}


class RenderedEmbeddingBatch:
    # 沿原检索器规范化比较缓存输入，原文与坐标仍保留在候选里。
    def __init__(self, entries):
        normalized = [_entry(entry) for entry in entries]
        self._entries = {entry.id: entry for entry in normalized}
        if len(self._entries) != len(entries):
            raise RecognitionConflict('shared_rendered_input_batch_changed')
        self._seen, self._missing = set(), []

    def observe(self, entry, cached):
        expected = self._entries.get(entry.id)
        if (expected is None or entry.id in self._seen
                or any(getattr(entry, field) != getattr(expected, field)
                    for field in ('id', 'revision', 'project_id', 'content'))):
            raise RecognitionConflict('shared_rendered_input_batch_changed')
        self._seen.add(entry.id)
        if cached is None:
            self._missing.append(entry.id)

    @property
    def missing_ids(self):
        return tuple(self._missing)


def _query_current(query):
    admission, factory = query.origin_admission, getattr(query, 'origin_factory', None)
    if (admission is None or factory is None or factory.registry is not admission.registry
            or getattr(admission.application.state, 'shared_query_factory', None) is not factory
            or query.models is not admission.application.state.recognition_models):
        raise RecognitionConflict('shared_rendered_input_owner_changed')
    with factory._lock:
        if query not in factory._issued_queries:
            raise RecognitionConflict('shared_rendered_input_owner_changed')
    admission.validate_execution_owner()
    query.validate_retrieval_origin()


def _material(query, candidate):
    if (candidate['scope'].project_id != query.origin_admission.lease.project_id
            or candidate.get('namespace', query.source_namespace) != query.source_namespace):
        raise RecognitionConflict('shared_rendered_input_owner_changed')
    kind = 'original_source' if candidate['kind'] == 'source' else candidate['kind']
    return {'type': kind, 'id': candidate['entry']['id'], 'revision': candidate['entry']['revision'],
        'project_id': candidate['scope'].project_id, 'namespace': query.source_namespace}


class PreparedRenderedWireInput:
    def __init__(self):
        self._token = object()

    def _entry(self):
        saved = _PREPARED.get(self._token)
        if (saved is None or saved['object'] is not self or _CURRENT.get() is not self._token
                or saved['thread'] != get_ident()):
            raise RecognitionConflict('shared_rendered_input_preparation_expired')
        _query_current(saved['query'])
        return saved

    @property
    def query(self):
        return self._entry()['query']

    @property
    def public_payload(self):
        return deepcopy(self._entry()['payload'])

    @property
    def binding(self):
        saved = self._entry()
        return {'purpose': saved['purpose'], 'wire_payload': deepcopy(saved['payload']),
            'items': deepcopy(saved['items'])}

    def capsule(self, *, turn_id, reference, materials):
        saved = self._entry()
        if saved['turn_id'] not in (None, turn_id):
            raise RecognitionConflict('shared_rendered_input_native_changed')
        saved['turn_id'] = turn_id
        capsule = {'schema_version': 1, 'turn_id': turn_id, 'purpose': saved['purpose'],
            'wire_payload': deepcopy(saved['payload']), 'items': deepcopy(saved['items'])}
        auxiliary = {'purpose': saved['purpose'], 'materials': deepcopy(materials), 'rendered_input': capsule,
            'selection': {'schema_version': 3, 'origin_ref': deepcopy(reference), 'origin_materials': deepcopy(materials),
                'sql_identities': [], 'rendered_input_ref': deepcopy(reference)}}
        validate_rendered_capsule_shape(capsule, request={'turn_id': turn_id}, reference=reference, auxiliary=auxiliary)
        saved['capsule'], saved['materials'], saved['reference'] = capsule, deepcopy(materials), deepcopy(reference)
        return deepcopy(capsule)

    def body(self, query, origins, *, materials=None, personal_context=None):
        saved = self._entry()
        if (query is not saved['query'] or origins.admission is not query.origin_admission
                or saved.get('materials') != materials or personal_context is not None
                or saved.get('reference') != origins.reference):
            raise RecognitionConflict('shared_rendered_input_changed')
        return _rendered_body(query, saved['items'])

    def validate_material(self, owner, reader, material):
        saved = self._entry()
        validate_rendered_material(saved['items'], owner, reader, material)


def _rendered_body(query, items):
    candidates = []
    for item in items:
        if item['role'] != 'material':
            continue
        if item['renderer'] == 'task-division-text-v1':
            from .shared_task_division_vectors import validate_task_division_item
            validate_task_division_item(query, item)
            continue
        decoded = decode(item['rendering']['candidate'])
        if (not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict)
                or _frozen_candidates(decoded) != item['rendering']['candidate']):
            raise RecognitionConflict('shared_rendered_input_body_changed')
        candidate = decoded[0]
        if (not {'kind', 'scope', 'entry', 'excerpt', 'title', 'windows'} <= set(candidate)
                or candidate['kind'] not in {'recognition', 'document', 'source'}
                or type(candidate['scope']) is not WorkScope or not isinstance(candidate['entry'], dict)
                or not isinstance(candidate['entry'].get('id'), str)
                or type(candidate['entry'].get('revision')) is not int or candidate['entry']['revision'] < 1
                or not isinstance(candidate['title'], str) or not isinstance(candidate['excerpt'], str)
                or not isinstance(candidate['windows'], (list, tuple))
                or any(type(window) is not EvidenceWindow for window in candidate['windows'])
                or _material(query, candidate) != item['qualified_material']
                or candidate['scope'].user_id != 'local-user'):
            raise RecognitionConflict('shared_rendered_input_body_changed')
        candidates.append(candidate)
    return {'chosen': candidates, 'profile': {'items': []}}


def restored_rendered_body(origins):
    from .shared_query import SharedQueryFactory
    from .shared_rendered_input import read_rendered_capsule

    admission = origins.admission
    factory = getattr(admission.application.state, 'shared_query_factory', None)
    if type(factory) is not SharedQueryFactory or factory.registry is not admission.registry:
        raise RecognitionConflict('shared_rendered_input_owner_changed')
    capsule = read_rendered_capsule({'turn_id': origins.reference['object_id']},
        origins.auxiliary, reference=origins.reference)
    query = factory.query(admission.application, admission.actor, origins.lease.project_id, lease=origins.lease)
    _query_current(query)
    return query, _rendered_body(query, capsule['items']), capsule['items']


def validate_rendered_material(items, owner, reader, material):
    if any(item['renderer'] == 'task-division-text-v1' for item in items):
        from .shared_task_division_vectors import validate_task_division_material
        validate_task_division_material(items, owner, material)
    for item in items:
        if item['role'] != 'material' or item['qualified_material'] != material:
            continue
        if item['renderer'] == 'task-division-text-v1':
            continue
        candidate = decode(item['rendering']['candidate'])[0]
        if item['renderer'] == 'recognition-content-v1':
            if candidate['kind'] != 'recognition':
                raise RecognitionConflict('shared_rendered_input_body_changed')
            rendered = candidate['entry']['content']
        elif item['renderer'] == 'recognition-condition-v1':
            row = reader.read('recognitions', material['id'])
            index = item['rendering']['condition_index']
            frozen = candidate['entry'].get('conditions', ())
            current = row.payload.get('conditions', ()) if row is not None else ()
            if (candidate['kind'] != 'recognition' or row is None or row.revision != material['revision']
                    or index >= len(frozen) or index >= len(current) or current[index] != frozen[index]):
                raise RecognitionConflict('shared_rendered_condition_changed')
            rendered = current[index]
        elif item['renderer'] == 'rerank-candidate-v1':
            if candidate['kind'] == 'recognition':
                title = '已发布认识'
                excerpt = format_recognition_content(candidate['entry'])
            else:
                title = candidate['entry']['title']
                excerpt = '\n…\n'.join(window.text for window in candidate['windows'])
            if candidate['title'] != title or candidate['excerpt'] != excerpt:
                raise RecognitionConflict('shared_rendered_input_body_changed')
            rendered = title + '\n' + excerpt
        else:
            if candidate['kind'] == 'document':
                repository = SQLiteDocumentRepository(TransactionRecords(reader), namespace_id=owner.documents.namespace_id)
                markdown = repository.markdown(material['id'], revision=material['revision'])
                if markdown is None:
                    raise RecognitionConflict('shared_rendered_input_body_changed')
                summary, _, _ = summary_of(markdown)
            else:
                summary = ''
            if summary != item['rendering']['summary'] or len(candidate['windows']) != 1:
                raise RecognitionConflict('shared_rendered_input_body_changed')
            rendered = rendered_chunk_text(candidate['entry'], summary, candidate['windows'][0].text)
        if item['renderer'] != 'rerank-candidate-v1':
            rendered = _required_string(rendered, 'content')
        if rendered != item['rendered_text']:
            raise RecognitionConflict('shared_rendered_input_body_changed')


@contextmanager
def _prepare(query, purpose, payload, *, question, candidates, renderer, summaries=None, condition_slots=None):
    _query_current(query)
    payload = deepcopy(payload)
    if purpose == 'embedding' and question is not None:
        question = _required_string(question, 'query')
    if not isinstance(payload, dict):
        raise RecognitionConflict('shared_rendered_input_changed')
    pool = []
    for index, candidate in enumerate(candidates):
        if renderer == 'recognition-content-v1':
            text = candidate['entry']['content']
        elif renderer == 'recognition-condition-v1':
            text = candidate['entry']['conditions'][condition_slots[index]]
        elif renderer == 'rerank-candidate-v1':
            text = candidate['title'] + '\n' + candidate['excerpt']
        else:
            text = rendered_chunk_text(candidate['entry'], summaries[candidate['entry']['id']], candidate['windows'][0].text)
        if purpose == 'embedding':
            text = _required_string(text, 'content')
        rendering = {'candidate': _frozen_candidates([candidate])}
        if renderer == 'contextual-chunk-v1':
            rendering['summary'] = summaries[candidate['entry']['id']]
        if renderer == 'recognition-condition-v1':
            rendering['condition_index'] = condition_slots[index]
        pool.append({'role': 'material', 'qualified_material': _material(query, candidate), 'renderer': renderer,
            'rendered_text': text, 'rendering': rendering})
    items = []
    if purpose == 'rerank':
        texts = list(enumerate(payload.get('documents', [])))
        if payload.get('query') != question:
            raise RecognitionConflict('shared_rendered_input_changed')
        items.append({'role': 'question', 'position': -1, 'qualified_material': None,
            'renderer': 'user-question-v1', 'rendered_text': question, 'rendering': None})
    else:
        texts = list(enumerate(payload.get('input', [])))
    if purpose == 'embedding' and question is not None:
        if not texts or texts[0] != (0, question):
            raise RecognitionConflict('shared_rendered_input_changed')
        items.append({'role': 'question', 'position': 0, 'qualified_material': None,
            'renderer': 'user-question-v1', 'rendered_text': question, 'rendering': None})
        texts = texts[1:]
    if len(texts) != len(pool):
        raise RecognitionConflict('shared_rendered_input_changed')
    for (position, text), item in zip(texts, pool, strict=True):
        if item['rendered_text'] != text:
            raise RecognitionConflict('shared_rendered_input_changed')
        items.append({**deepcopy(item), 'position': position})
    prepared = PreparedRenderedWireInput()
    _PREPARED[prepared._token] = {'object': prepared, 'query': query, 'purpose': purpose,
        'payload': payload, 'items': items, 'thread': get_ident(), 'turn_id': None}
    token = _CURRENT.set(prepared._token)
    try:
        yield prepared
    finally:
        _PREPARED.pop(prepared._token, None)
        _CURRENT.reset(token)


def recognition_wire_input(query, *, payload, question, entries):
    candidates = [{'kind': 'recognition', 'id': entry['id'], 'entry': deepcopy(entry),
        'scope': WorkScope('local-user', query.origin_admission.lease.project_id), 'namespace': query.source_namespace,
        'excerpt': format_recognition_content(entry), 'title': entry['content'], 'windows': ()}
        for entry in entries]
    return _prepare(query, 'embedding', payload, question=question, candidates=candidates, renderer='recognition-content-v1')


def condition_wire_input(query, *, payload, question, conditions):
    candidates = [{'kind': 'recognition', 'id': entry['id'], 'entry': deepcopy(entry),
        'scope': WorkScope('local-user', query.origin_admission.lease.project_id), 'namespace': query.source_namespace,
        'excerpt': format_recognition_content(entry), 'title': entry['content'], 'windows': ()}
        for entry, _ in conditions]
    return _prepare(query, 'embedding', payload, question=question, candidates=candidates,
        renderer='recognition-condition-v1', condition_slots=[index for _, index in conditions])


def chunk_wire_input(query, *, payload, question, entry, scope, snapshot, layer, span_start, summary, chunks):
    coordinates = ('workspace_source_text_v1' if layer == 'L0' and entry['kind'] == 'document'
        else 'document_markdown_v1' if entry['kind'] == 'document' else 'source_content_v1')
    candidates = [{'kind': entry['kind'], 'id': entry['id'], 'entry': deepcopy(entry), 'scope': scope,
        'namespace': query.source_namespace, 'layer': layer, 'snapshot': deepcopy(snapshot),
        'windows': (EvidenceWindow(span_start + chunk.start, span_start + chunk.end, chunk.text),),
        'coordinate_space': coordinates, 'excerpt': chunk.text, 'title': entry['title']}
        for chunk in chunks]
    return _prepare(query, 'embedding', payload, question=question, candidates=candidates,
        renderer='contextual-chunk-v1', summaries={entry['id']: summary})


def rerank_wire_input(query, *, payload, question, candidates):
    return _prepare(query, 'rerank', payload, question=question, candidates=candidates, renderer='rerank-candidate-v1')
