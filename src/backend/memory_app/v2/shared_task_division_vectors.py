from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import get_ident

from backend.recognition import RecognitionConflict
from backend.recognition_retrieval.service import _required_string

from ..privacy_policy import global_egress_allowed
from .privacy import egress_allowed
from .shared_rendered_preparation import PreparedRenderedWireInput, _query_current


RENDERER = 'task-division-text-v1'
_CURRENT = ContextVar('shared_task_division_wire', default=None)
_PREPARED = {}
_BINDING = {'schema_version', 'sample', 'root_request', 'origin_ref', 'execution', 'materials', 'completion_refs'}


def _capture_sample(query, row):
    from .shared_task_context import division_sources, task_query, task_source_scope
    _query_current(query)
    current = query.receipt_records.read('v2_task_divisions', row.object_id)
    if (current is None or current.revision != row.revision or current.payload != row.payload
            or row.payload.get('deleted') or row.payload['outcome'] == 'failed'):
        raise RecognitionConflict('shared_task_division_reference_changed')
    materials = division_sources(query, row)
    execution = query.receipt_records.read('v2_task_executions', row.object_id)
    old_query, root = task_query(query.execution_owner, execution.payload['request'])
    with task_source_scope(old_query, root) as (issued, root, reader):
        origins = issued.origin_admission.restore(root)
        context = issued.outcome_freshness.completion_context(root, reader=reader)
        qualified = issued.outcome_freshness._qualify_source_proofs(
            context['source_proofs'], reader=reader, local_only=True)
        if not qualified['complete'] or 'embedding' not in qualified['privacy_ceiling']['effective_purposes']:
            raise RecognitionConflict('shared_task_division_embedding_source_unavailable')
        return {'schema_version': 1,
            'sample': {'id': row.object_id, 'revision': row.revision, 'payload': deepcopy(dict(row.payload))},
            'root_request': deepcopy(root), 'origin_ref': deepcopy(origins.reference),
            'execution': deepcopy(issued.origin_admission.personal), 'materials': deepcopy(materials),
            'completion_refs': deepcopy(context['proof_refs'])}


def validate_task_division_rendering(item):
    binding = item['rendering'].get('candidate')
    if (set(item['rendering']) != {'candidate'} or not isinstance(binding, dict) or set(binding) != _BINDING
            or type(binding['schema_version']) is not int or binding['schema_version'] != 1
            or not isinstance(binding['sample'], dict) or set(binding['sample']) != {'id', 'revision', 'payload'}
            or not isinstance(binding['sample']['id'], str) or not binding['sample']['id']
            or type(binding['sample']['revision']) is not int or binding['sample']['revision'] < 1
            or not isinstance(binding['sample']['payload'], dict)
            or not isinstance(binding['root_request'], dict) or not isinstance(binding['origin_ref'], dict)
            or not isinstance(binding['execution'], dict) or not isinstance(binding['completion_refs'], list)
            or not isinstance(binding['materials'], list) or not binding['materials']
            or item['qualified_material'] != binding['materials'][0]
            or binding['sample']['payload'].get('source_turn_id') != binding['sample']['id']
            or not isinstance(binding['sample']['payload'].get('task_text'), str)
            or _required_string(binding['sample']['payload']['task_text'], 'content') != item['rendered_text']):
        raise RecognitionConflict('shared_task_division_wire_changed')
    for material in binding['materials']:
        if (not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                or type(material['revision']) is not int or material['revision'] < 1
                or any(not isinstance(material[key], str) or not material[key]
                    for key in ('type', 'id', 'project_id', 'namespace'))):
            raise RecognitionConflict('shared_task_division_wire_changed')


def validate_task_division_item(query, item):
    validate_task_division_rendering(item)
    binding = item['rendering']['candidate']
    row = query.receipt_records.read('v2_task_divisions', binding['sample']['id'])
    if row is None or _capture_sample(query, row) != binding:
        raise RecognitionConflict('shared_task_division_wire_changed')


def validate_task_division_material(items, owner, material):
    from .shared_auxiliary import current_qualified_commit_scope
    scope = current_qualified_commit_scope()
    if scope is None:
        raise RecognitionConflict('shared_task_division_source_scope_missing')
    admission = scope._admission
    factory = admission.application.state.shared_query_factory
    with factory._lock:
        queries = [query for query in factory._issued_queries if query.origin_admission is admission]
    if len(queries) != 1 or material['namespace'] != owner.namespace or material['project_id'] != owner.project_id:
        raise RecognitionConflict('shared_task_division_source_owner_changed')
    for item in items:
        if (item['renderer'] == RENDERER and material in item['rendering']['candidate']['materials']):
            validate_task_division_item(queries[0], item)


class TaskDivisionWireInput(PreparedRenderedWireInput):
    def _entry(self):
        saved = _PREPARED.get(self._token)
        if (saved is None or saved['object'] is not self or _CURRENT.get() is not self._token
                or saved['thread'] != get_ident()):
            raise RecognitionConflict('shared_task_division_preparation_expired')
        _query_current(saved['query'])
        return saved


class DivisionVectorProducer:
    def __init__(self, query, rows, text):
        _query_current(query)
        self.query, self.rows = query, tuple(rows)
        self.question = _required_string(text, 'query')
        self.bindings = [_capture_sample(query, row) for row in self.rows]
        self.materials = []
        for binding in self.bindings:
            for material in binding['materials']:
                if material not in self.materials:
                    self.materials.append(deepcopy(material))

    def validate(self):
        _query_current(self.query)
        if [_capture_sample(self.query, row) for row in self.rows] != self.bindings:
            raise RecognitionConflict('shared_task_division_source_changed')

    def allowed(self, project):
        self.validate()
        return (project == self.query.origin_admission.lease.project_id
            and global_egress_allowed(self.query.models, 'embedding')
            and egress_allowed(self.query.records, self.query.models, project, 'embedding'))

    @contextmanager
    def prepare(self, payload, *, missing_ids):
        self.validate()
        values = payload.get('input')
        bindings = {'task-division-' + binding['sample']['id']: binding for binding in self.bindings}
        if (not self.materials or not isinstance(values, list) or not values or values[0] != self.question
                or type(missing_ids) is not tuple or len(missing_ids) != len(values) - 1
                or len(bindings) != len(self.bindings) or len(set(missing_ids)) != len(missing_ids)
                or any(not isinstance(identity, str) or identity not in bindings for identity in missing_ids)
                or tuple(identity for identity in bindings if identity in missing_ids) != missing_ids):
            raise RecognitionConflict('shared_task_division_wire_changed')
        items = [{'role': 'question', 'position': 0, 'qualified_material': None,
            'renderer': 'user-question-v1', 'rendered_text': self.question, 'rendering': None}]
        # 缓存观察提供原有顺序的缺失编号，同文条目仍绑定各自的真实旧轮。
        for index, (identity, text) in enumerate(zip(missing_ids, values[1:], strict=True), start=1):
            binding = bindings[identity]
            if _required_string(binding['sample']['payload']['task_text'], 'content') != text:
                raise RecognitionConflict('shared_task_division_wire_changed')
            if not binding['materials']:
                raise RecognitionConflict('shared_task_division_empty_sources')
            items.append({'role': 'material', 'position': index,
                'qualified_material': deepcopy(binding['materials'][0]), 'renderer': RENDERER,
                'rendered_text': text, 'rendering': {'candidate': deepcopy(binding)}})
        prepared = TaskDivisionWireInput()
        _PREPARED[prepared._token] = {'object': prepared, 'producer': self, 'query': self.query,
            'purpose': 'embedding', 'payload': deepcopy(payload), 'items': items, 'thread': get_ident(), 'turn_id': None}
        token = _CURRENT.set(prepared._token)
        try:
            yield prepared
        finally:
            _PREPARED.pop(prepared._token, None)
            _CURRENT.reset(token)

    def turn(self, prepared, *, key):
        from .shared_auxiliary import SharedAuxiliaryTurn
        if type(prepared) is not TaskDivisionWireInput or prepared._entry()['producer'] is not self:
            raise RecognitionConflict('shared_task_division_preparation_expired')
        return SharedAuxiliaryTurn(self.query.origin_admission, self.query.models, kind='memory.link_suggest',
            project=self.query.origin_admission.lease.project_id, key=key, materials=self.materials,
            validate=self.validate, purpose='embedding', rendered_input=prepared)

    def __call__(self, records, models, project, materials, key, validate, transport, request, *, missing_ids):
        from ..kernel.memory_turn import embedding_request
        if (records is not self.query.receipt_records or models is not self.query.models
                or project != self.query.origin_admission.lease.project_id or materials):
            raise RecognitionConflict('shared_task_division_execution_owner_changed')
        with self.prepare(request['payload'], missing_ids=missing_ids) as prepared:
            def factory(actual_records, actual_models, **options):
                if (actual_records is not records or actual_models is not models
                        or options['materials'] != self.materials or options['purpose'] != 'embedding'):
                    raise RecognitionConflict('shared_task_division_execution_owner_changed')
                return self.turn(prepared, key=options['key'])

            def original_wire(turn, payload):
                if turn.rendered_payload != payload or prepared.public_payload != payload:
                    raise RecognitionConflict('shared_task_division_wire_changed')
                def current():
                    validate()
                    turn.validate()
                    if turn.rendered_payload != payload:
                        raise RecognitionConflict('shared_task_division_wire_changed')
                return current

            return embedding_request(records, models, project, self.materials,
                {'producer': RENDERER, 'key': key, 'wire_payload': prepared.public_payload}, validate,
                transport, request, turn_factory=factory, prepare_original_wire=original_wire)
