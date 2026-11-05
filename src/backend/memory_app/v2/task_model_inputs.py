from copy import deepcopy
from contextlib import ExitStack, nullcontext
from pathlib import Path

from backend.recognition import RecognitionConflict
from core.ai_kernel.agent_store import SQLiteAgentStore
from core.ai_kernel.contracts import validate_model_wire_attempt_dispatch
from core.ai_kernel.product_admission import admission_identity
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from ..transaction_records import TransactionRecords
from ..turn_routing import SNAPSHOT_KIND
from .task_attachment_input import _agent_owner


INPUT_PREFIX = 'product-task-model-input-v1-'
WIRE_PREFIX = 'product-task-model-wire-v1-'
ROOT_KIND = 'product-task-input-root-v1'
INPUT_FIELDS = frozenset({'schema_version', 'turn_id', 'root_turn_id', 'request_identity', 'root_request_ref',
    'root_request_identity', 'profile_chain', 'model_request_id', 'call_index', 'messages', 'response_schema',
    'purpose', 'routing_snapshot_ref', 'outcome_source_preparation_ref', 'run_owner_id', 'run_generation',
    'context_sources', 'context_proof_refs'})


def task_profile_chain(turns, agents, request):
    if (not isinstance(turns, SQLiteAITurnStore) or not isinstance(agents, SQLiteAgentStore)
            or Path(turns._path).resolve() != Path(agents._path).resolve()):
        raise RecognitionConflict('task model input native owner changed')
    current, chain, seen = request, [], set()
    while True:
        identity = current['turn_id']
        if (identity in seen or len(seen) >= 256 or turns.get_request(identity) != current or current['scope'] != request['scope']):
            raise RecognitionConflict('task model input profile chain changed')
        seen.add(identity)
        _agent_owner(turns, agents, current)
        chain.append(deepcopy(current))
        parent_id = current.get('agent_binding', {}).get('parent_run_id')
        if parent_id is None:
            break
        parent = agents.get_run(parent_id)
        if parent is None or parent.project_id != request['scope']['project_id']:
            raise RecognitionConflict('task model input profile parent unavailable')
        current = turns.get_request(parent.turn_id)
        if current is None:
            raise RecognitionConflict('task model input profile request unavailable')
    if chain[-1]['desired_outcome'] != 'project.task':
        raise RecognitionConflict('task model input root is not a product task')
    return tuple(chain)


def read_task_model_input(turns, agents, turn_id, input_ref, *, attempt_id=None):
    request = turns.get_request(turn_id)
    if request is None:
        raise RecognitionConflict('task model input request unavailable')
    chain = task_profile_chain(turns, agents, request)
    root, payload = chain[-1], turns.get(input_ref)
    if (not isinstance(payload, dict) or set(payload) != INPUT_FIELDS or payload['schema_version'] != '1.0.0'
            or payload['turn_id'] != turn_id or payload['root_turn_id'] != root['turn_id']
            or payload['request_identity'] != admission_identity(request) or payload['root_request_identity'] != admission_identity(root)
            or payload['profile_chain'] != [item['turn_id'] for item in chain] or payload['purpose'] != 'primary'
            or type(payload['call_index']) is not int or payload['call_index'] < 1
            or not isinstance(payload['messages'], list) or not payload['messages']
            or payload['response_schema'] is not None and not isinstance(payload['response_schema'], dict)
            or not isinstance(payload['model_request_id'], str) or not payload['model_request_id']
            or not isinstance(payload['run_owner_id'], str) or not payload['run_owner_id']
            or type(payload['run_generation']) is not int or payload['run_generation'] < 1
            or payload['outcome_source_preparation_ref'] is not None and not isinstance(payload['outcome_source_preparation_ref'], str)
            or not isinstance(payload['context_sources'], list) or not isinstance(payload['context_proof_refs'], list)):
        raise RecognitionConflict('task model input binding changed')
    kind = INPUT_PREFIX + payload['model_request_id'] + '-' + str(payload['call_index'])
    if (turns.get_immutable_payload(turn_id, kind) != (input_ref, payload)
            or turns.get_immutable_payload(root['turn_id'], ROOT_KIND) != (payload['root_request_ref'], {'request': root})):
        raise RecognitionConflict('task model input immutable identity changed')
    route = turns.get_immutable_payload(turn_id, SNAPSHOT_KIND)
    requested = [event for event in turns.events_after(turn_id) if event['type'] == 'model.requested'
        and event['correlation']['model_request_id'] == payload['model_request_id'] and event['data'].get('model_call_purpose') == 'primary']
    if route is None or route[0] != payload['routing_snapshot_ref'] or len(requested) != 1:
        raise RecognitionConflict('task model input model owner changed')
    if attempt_id is not None:
        proof = turns.get_immutable_payload(turn_id, WIRE_PREFIX + attempt_id)
        if proof is None:
            raise RecognitionConflict('task model input wire proof unavailable')
        bound = proof[1]
        if (not isinstance(bound, dict) or set(bound) != {'schema_version', 'turn_id', 'model_request_id', 'input_ref', 'dispatch_ref', 'dispatch'}
                or bound['schema_version'] != '1.0.0' or bound['turn_id'] != turn_id
                or bound['model_request_id'] != payload['model_request_id'] or bound['input_ref'] != input_ref):
            raise RecognitionConflict('task model input wire proof changed')
        dispatch = validate_model_wire_attempt_dispatch(turns.get(bound['dispatch_ref']))
        bindings = [item for item in turns.model_attempt_bindings(turn_id) if item['attempt_id'] == attempt_id]
        if (dispatch != bound['dispatch'] or dispatch['attempt_id'] != attempt_id or dispatch['turn_id'] != turn_id
                or dispatch['model_request_id'] != payload['model_request_id'] or len(bindings) != 1
                or bindings[0]['dispatch_ref'] != bound['dispatch_ref'] or bindings[0]['dispatch'] != dispatch
                or bindings[0]['lease_owner_id'] != payload['run_owner_id'] or bindings[0]['lease_generation'] != payload['run_generation']):
            raise RecognitionConflict('task model input wire owner changed')
    return deepcopy(payload)


class TaskModelInput:
    def __init__(self, planner, *, request, source_request, messages, response_model, control, routing_ref, call_index, sink, validate):
        self.planner, self.turns, self.agents = planner, planner.store, planner.composition.store
        self.request, self.messages = deepcopy(request), deepcopy(messages)
        self.control, self._validate = control, validate
        if type(call_index) is not int or call_index < 1:
            raise RecognitionConflict('task model input call index changed')
        runtime = planner.continuations.stops.runtime if planner.continuations is not None and planner.continuations.stops is not None else None
        lease = runtime._run_lease_context.get() if runtime is not None else None
        if (runtime is None or runtime._state is not self.turns or lease is None or lease.turn_id != request['turn_id']
                or self.turns.assert_active_run_lease(lease) is None):
            raise RecognitionConflict('task model input active owner unavailable')
        self.lease = lease
        self.runtime = runtime
        chain = task_profile_chain(self.turns, self.agents, self.request)
        if chain[-1] != source_request or planner.profile_request(self.request) != source_request:
            raise RecognitionConflict('task model input source owner changed')
        model_id = control.model_request_id
        self.kind = INPUT_PREFIX + model_id + '-' + str(call_index)
        root_ref = self.turns.get_or_create_immutable_payload(source_request['turn_id'], ROOT_KIND, {'request': source_request})
        freshness = planner.outcome_freshness
        preparation = (freshness.prepare_input_basis(request=self.request, input_kind=self.kind, messages=self.messages,
            source_request=source_request, source_request_ref=root_ref, ancestry_root=source_request['turn_id']) if freshness is not None else None)
        context = self.turns.get(preparation)['context'] if preparation is not None else {'materials': [], 'proof_refs': []}
        self.payload = {'schema_version': '1.0.0', 'turn_id': self.request['turn_id'], 'root_turn_id': source_request['turn_id'],
            'request_identity': admission_identity(self.request), 'root_request_ref': root_ref,
            'root_request_identity': admission_identity(source_request), 'profile_chain': [item['turn_id'] for item in chain],
            'model_request_id': model_id, 'call_index': call_index, 'messages': deepcopy(self.messages),
            'response_schema': response_model.model_json_schema() if response_model is not None else None,
            'purpose': 'primary', 'routing_snapshot_ref': routing_ref, 'outcome_source_preparation_ref': preparation,
            'run_owner_id': lease.owner_id, 'run_generation': lease.generation,
            'context_sources': deepcopy(context['materials']), 'context_proof_refs': deepcopy(context['proof_refs'])}
        self.input_ref = self.turns.get_or_create_immutable_payload(self.request['turn_id'], self.kind, self.payload)
        self.basis_ref = (freshness.capture_input_basis(request=self.request, input_kind=self.kind, input_ref=self.input_ref)
            if freshness is not None else None)
        self.execution_control = control
        if preparation is not None:
            if not callable(getattr(control, 'with_validator', None)):
                raise RecognitionConflict('task model input source wire authority unavailable')
            previous = getattr(control, 'validator', None)
            def validate_sources(reader, native_request):
                if reader is freshness.records:
                    current_reader = None
                elif isinstance(reader, TransactionRecords):
                    current_reader = reader
                else:
                    raise RecognitionConflict('task model input source reader changed')
                if previous is not None:
                    previous(reader, native_request)
                freshness.validate_input_preparation(request=self.request, input_kind=self.kind,
                    preparation_ref=preparation, input_ref=self.input_ref, reader=current_reader)
            self.execution_control = control.with_validator(validate_sources)
        self.wire_attempt_sink = _TaskWireSink(sink, self)
        self.validate()

    def validate(self):
        self._validate()
        if (self.turns.assert_active_run_lease(self.lease) is None or self.messages != self.payload['messages']
                or read_task_model_input(self.turns, self.agents, self.request['turn_id'], self.input_ref) != self.payload):
            raise RecognitionConflict('task model input changed before wire')
        if self.payload['outcome_source_preparation_ref'] is not None:
            self.planner.outcome_freshness.validate_input_preparation(request=self.request, input_kind=self.kind,
                preparation_ref=self.payload['outcome_source_preparation_ref'], input_ref=self.input_ref)

    def source_scope(self):
        freshness = self.planner.outcome_freshness
        if freshness is None:
            return nullcontext()
        root = self.turns.get(self.payload['root_request_ref'])
        if (not isinstance(root, dict) or set(root) != {'request'}
                or root['request'] != self.turns.get_request(self.payload['root_turn_id'])
                or admission_identity(root['request']) != self.payload['root_request_identity']):
            raise RecognitionConflict('task model input source owner changed')
        return freshness.source_scope(request=root['request'])


class _TaskWireSink:
    def __init__(self, sink, prepared):
        self.sink, self.prepared = sink, prepared

    def __getattr__(self, name):
        return getattr(self.sink, name)

    def begin_model_wire_attempt(self):
        with self.prepared.source_scope():
            return self._begin_qualified_attempt()

    def _begin_qualified_attempt(self):
        self.prepared.validate()
        handle = self.sink.begin_model_wire_attempt()
        try:
            dispatch = validate_model_wire_attempt_dispatch(handle.dispatch)
            if dispatch['turn_id'] != self.prepared.request['turn_id'] or dispatch['model_request_id'] != self.prepared.control.model_request_id:
                raise RecognitionConflict('task model input dispatch owner changed')
            self.prepared.turns.get_or_create_immutable_payload(dispatch['turn_id'], WIRE_PREFIX + dispatch['attempt_id'],
                {'schema_version': '1.0.0', 'turn_id': dispatch['turn_id'], 'model_request_id': dispatch['model_request_id'],
                 'input_ref': self.prepared.input_ref, 'dispatch_ref': handle.dispatch_ref, 'dispatch': dispatch})
            steers = getattr(self.prepared.runtime, 'product_steers', None)
            if steers is not None and steers.registered(dispatch['turn_id']):
                steers.bind_model_context(dispatch['turn_id'], self.prepared.input_ref, input_kind=self.prepared.kind)
        except BaseException:
            def reject():
                handle.failed_transport(error_code='ai.task_input_proof_failed')
            handle.invoke_wire(reject)
            raise
        return _TaskWireHandle(handle, self.prepared)


class _TaskWireHandle:
    def __init__(self, handle, prepared):
        self.handle, self.prepared = handle, prepared

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def invoke_wire(self, handler):
        with ExitStack() as reads:
            try:
                reads.enter_context(self.prepared.source_scope())
            except BaseException:
                def reject():
                    self.handle.failed_transport(error_code='ai.task_input_proof_changed')
                self.handle.invoke_wire(reject)
                raise
            return self._invoke_qualified_wire(handler, reads)

    def _invoke_qualified_wire(self, handler, reads):
        def guarded():
            try:
                self.prepared.validate()
                read_task_model_input(self.prepared.turns, self.prepared.agents, self.prepared.request['turn_id'],
                    self.prepared.input_ref, attempt_id=self.handle.dispatch['attempt_id'])
                if self.prepared.payload['outcome_source_preparation_ref'] is not None:
                    # 沿原实际来源发送接点复验，同一短事务保留已有约束及最终输入证明。
                    with self.prepared.execution_control._qualified_sources():
                        pass
                # 所有原发送复验完成后释放资料资格事务，真实请求沿原 handle 执行。
                reads.close()
            except BaseException:
                self.handle.failed_transport(error_code='ai.task_input_proof_changed')
                raise
            return handler()
        return self.handle.invoke_wire(guarded)
