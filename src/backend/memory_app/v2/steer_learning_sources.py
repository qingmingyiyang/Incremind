from copy import deepcopy
import json
from uuid import UUID

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_evidence_json import encoded
from backend.recognition.external_turn_facts import source_store
from core.ai_kernel.contracts import validate_model_wire_attempt_dispatch, validate_model_wire_attempt_receipt
from core.ai_kernel.product_admission import admission_identity
from core.ai_kernel.product_snapshot import read_model_attempt_bindings
from core.storage_provider.record_lineage import HEAD_COLLECTIONS, verify_lineage
from core.effect_log import EffectState

from ..kernel.route_source_snapshot import native_source_connection
from ..transaction_records import TransactionRecords
from .outcome_freshness import _lineage
from .policies import get


INPUTS = 'v2_steer_learning_inputs'
CANDIDATES = 'v2_steer_learning_candidate_sources'
RECOGNITIONS = 'v2_steer_learning_recognition_sources'
PUBLICATION_PREFIX = 'steer-learning-publication-origin-'


def _identity_reader(source_owner, reader):
    from ..source_egress import _source_owner_binding
    from .material_origins import SharedMaterialOwner
    from .shared_runtime import SharedWriteRecords
    view = TransactionRecords(reader)
    _source_owner_binding(source_owner, view)
    return view, isinstance(source_owner, (SharedMaterialOwner, SharedWriteRecords))


def _auxiliary_id(value):
    try:
        return 'memory-' + UUID(value).hex
    except ValueError:
        return None


def _native_database_exists(source_owner):
    root = source_store(source_owner).root.parent
    return any(path.is_file() for path in (root / '.rebuild-data/ai-turns.sqlite3', root / 'ai-turns.sqlite3'))


def learning_candidate_identity(source_owner, reader, scope, candidate):
    view, shared = _identity_reader(source_owner, reader)
    actual = view.read('recognition_candidates', candidate.object_id)
    if actual != candidate or candidate.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}:
        raise RecognitionConflict('steer_learning_candidate_owner_changed')
    if view.read(CANDIDATES, candidate.object_id) is not None:
        return True
    generation = candidate.payload.get('generation') or {}
    if generation.get('step_version') == 'candidate-from-steer-v1':
        return True
    if any(row.payload.get('candidate_id') == candidate.object_id for row in view.list('v2_steer_learning_consumed')):
        return True
    pattern = view.read('v2_insight_patterns', candidate.object_id)
    if pattern is not None and any(view.read('v2_turn_steer_feedback', identity) is not None
            for identity in pattern.payload.get('event_ids', [])):
        return True
    if not candidate.object_id.startswith('steer-memory-'):
        return False
    retained = _auxiliary_id(candidate.object_id[len('steer-memory-'):])
    if retained is not None and candidate.object_id == 'steer-' + retained:
        return True
    if shared:
        return False
    possible = {retained}
    if generation.get('id'):
        possible.add(_auxiliary_id(generation['id']))
    possible.discard(None)
    if possible and _native_database_exists(source_owner):
        with native_source_connection(source_owner, reader=view) as connection:
            for identity in possible:
                raw = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (identity,)).fetchone()
                binding = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
                    (identity, 'steer-learning-candidate-origin-v1')).fetchone()
                if (raw is not None and json.loads(raw[0]).get('desired_outcome') == 'memory.consolidate'
                        and candidate.object_id == 'steer-' + identity
                        or binding is not None and json.loads(binding[0]).get('candidate_id') == candidate.object_id):
                    return True
    return False


def _direct_recognition_identity(source_owner, view, scope, recognition, *, shared):
    actual = view.read('recognitions', recognition.object_id)
    if actual != recognition or recognition.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}:
        raise RecognitionConflict('steer_learning_recognition_changed')
    if view.read(RECOGNITIONS, recognition.object_id) is not None:
        return True
    if any(learning_candidate_identity(source_owner, view, WorkScope(**candidate.payload['scope']), candidate)
        for candidate in view.list('recognition_candidates')
        if candidate.payload.get('recognition_id') == recognition.object_id):
        return True
    if shared or not _native_database_exists(source_owner):
        return False
    with native_source_connection(source_owner, reader=view) as connection:
        anchors = tuple(json.loads(row[0]) for row in connection.execute(
            'SELECT payload_json FROM ai_turn_immutable_payloads WHERE kind=?', (PUBLICATION_PREFIX + recognition.object_id,)))
    if not anchors:
        return False
    if view.read(HEAD_COLLECTIONS['recognitions'], recognition.object_id) is None:
        return True
    birth = _lineage(view, 'recognitions', recognition.object_id)
    return any(anchor.get('recognition_birth') == birth for anchor in anchors)


def _learning_family(source_owner, reader, scope, recognition):
    from ..source_egress import _MAX_NODES
    from .steer_learning_lineage import recognition_family
    view, shared = _identity_reader(source_owner, reader)
    rows = recognition_family(view, scope, recognition, max_nodes=_MAX_NODES)
    roots = [rows[identity] for identity in sorted(rows)
        if _direct_recognition_identity(source_owner, view, scope, rows[identity], shared=shared)]
    return view, rows, roots


def learning_recognition_identity(source_owner, reader, scope, recognition):
    return bool(_learning_family(source_owner, reader, scope, recognition)[2])


def _published_privacy_qualifications(current, frozen):
    result = []
    for values, floor in ((current, False), (frozen, True)):
        for item in values:
            value = deepcopy(item)
            value['current_sources'], value['recognitions'] = [], []
            if floor:
                value['privacy_ceiling']['sources'] = []
            result.append(value)
    return list({encoded(item): item for item in result}.values())


class SteerLearningSources:
    def __init__(self, learning):
        self.learning, self.freshness = learning, learning.freshness

    def is_learning_candidate(self, reader, scope, candidate):
        self.freshness._validate()
        return learning_candidate_identity(self.freshness.records, reader, scope, candidate)

    def is_learning_recognition(self, reader, scope, recognition):
        self.freshness._validate()
        return learning_recognition_identity(self.freshness.records, reader, scope, recognition)

    def candidate_conditions(self, reader, output, selected_events):
        first = selected_events[0]['_row'].payload
        request = first['native_request']
        view = self.freshness._reader(reader)
        public = view.read('v2_turns', first['target_turn_id'])
        if public is None:
            raise RecognitionConflict('steer_learning_condition_source_unavailable')
        return get('consolidate', version='@3')(None, operation='conditions', conditions=output.conditions,
            situation=request['input'].get('situation'), question=public.payload['user_text'])

    def stage_auxiliary_input(self, tx, turn, events):
        view = self.freshness._reader(tx)
        key = view.read('v2_memory_turn_keys', turn.turn_id)
        if key is None or key.payload.get('request') != turn.request or turn.request['desired_outcome'] != 'memory.consolidate':
            raise RecognitionConflict('steer_learning_auxiliary_owner_changed')
        selected = []
        for event in events:
            row = view.read('v2_turn_steer_feedback', event['event_id'])
            if row != event['_row']:
                raise RecognitionConflict('steer_learning_feedback_changed')
            self.learning.qualify(view, row, local_only=turn.local)
            selected.append({'id': row.object_id, 'revision': row.revision, 'payload': deepcopy(row.payload)})
        payload = {'version': 1, 'owner': deepcopy(self.freshness.owner), 'request': deepcopy(turn.request),
            'key': deepcopy(key.payload['identity']), 'feedback': selected}
        old = view.read(INPUTS, turn.turn_id)
        if old is not None and (old.revision != 1 or old.payload != payload):
            raise RecognitionConflict('steer_learning_auxiliary_replay_changed')
        if old is None:
            view.put(INPUTS, turn.turn_id, payload, expected_revision=0)
        return deepcopy(payload)

    def _frozen_auxiliary_request(self, reader, request, *, local_only):
        view = self.freshness._reader(reader)
        self.freshness._validate()
        sidecar = view.read(INPUTS, request['turn_id'])
        if sidecar is None:
            return None
        payload = sidecar.payload
        key = view.read('v2_memory_turn_keys', request['turn_id'])
        if (sidecar.revision != 1 or payload.get('owner') != self.freshness.owner
                or payload.get('request') != request or request['desired_outcome'] != 'memory.consolidate'
                or key is None or key.payload.get('request') != request or key.payload.get('identity') != payload['key']):
            raise RecognitionConflict('steer_learning_auxiliary_request_changed')
        identity_key = payload['key'].get('key')
        if (not isinstance(identity_key, list) or len(identity_key) != 3 or identity_key[0] != 'steer-learning-v1'
                or not isinstance(identity_key[1], list) or not isinstance(identity_key[2], list)):
            raise RecognitionConflict('steer_learning_auxiliary_key_changed')
        expected_feedback = [{'id': item[0], 'revision': item[1], 'payload': item[2]} for item in identity_key[1]]
        if (len({item['id'] for item in payload['feedback']}) != len(payload['feedback'])
                or sorted(payload['feedback'], key=lambda item: item['id']) != expected_feedback
                or [item[0] for item in identity_key[2]] != [item['id'] for item in expected_feedback]):
            raise RecognitionConflict('steer_learning_auxiliary_feedback_binding_changed')
        frozen_qualified = [item for _, contexts in identity_key[2] for item in contexts]
        if any(not item.get('complete') or item.get('unknown') for item in frozen_qualified):
            raise RecognitionConflict('steer_learning_auxiliary_source_incomplete')
        if not local_only and any('generation' not in item['privacy_ceiling']['effective_purposes'] for item in frozen_qualified):
            raise RecognitionConflict('private_source_remote_blocked')
        feedback = []
        for item in payload['feedback']:
            row = view.read('v2_turn_steer_feedback', item['id'])
            if row is None or row.revision != item['revision'] or row.payload != item['payload']:
                raise RecognitionConflict('steer_learning_feedback_changed')
            fact = deepcopy(row.payload)
            if fact['project_id'] != request['scope']['project_id'] or not fact['learnable']:
                raise RecognitionConflict('steer_learning_feedback_scope_changed')
            feedback.append(fact)
        frozen_ceiling = [{'complete': True, 'unknown': [], 'current_sources': [], 'recognitions': [],
            'privacy_ceiling': deepcopy(item['privacy_ceiling'])} for item in frozen_qualified]
        return {'input_ref': {'collection': INPUTS, 'id': request['turn_id'], 'revision': sidecar.revision},
            'feedback': feedback, 'qualified': frozen_ceiling,
            'frozen_input': deepcopy(payload)}

    def validate_auxiliary_request(self, reader, request, *, local_only):
        view = self.freshness._reader(reader)
        prepared = self._frozen_auxiliary_request(view, request, local_only=local_only)
        if prepared is None:
            return None
        qualified = []
        for fact in prepared['feedback']:
            row = view.read('v2_turn_steer_feedback', fact['id'])
            canonical, sources = self.learning.qualify(view, row, local_only=local_only)
            if canonical != fact:
                raise RecognitionConflict('steer_learning_feedback_changed')
            qualified.extend(sources)
        return {**prepared, 'qualified': [*qualified, *prepared['qualified']]}

    def capture_model_input(self, *, owner, request, model_request_id, messages, response_schema,
                            result_schema, governed):
        from ..kernel.memory_turn import MemoryTurn
        from .steer_learning import SteerLearningOutput
        native = self.freshness._native()
        if (not isinstance(owner, MemoryTurn) or owner.records is not self.freshness.records
                or owner.store._path != native._path or native.get_request(request['turn_id']) != request
                or owner.request != request or governed is not False
                or response_schema != SteerLearningOutput.model_json_schema()
                or result_schema != response_schema):
            raise RecognitionConflict('steer_learning_model_input_owner_changed')
        expected = self._model_messages(request)
        if messages != expected:
            raise RecognitionConflict('steer_learning_model_messages_changed')
        requested = [event for event in native.events_after(request['turn_id'])
            if event['type'] == 'model.requested' and event['correlation']['model_request_id'] == model_request_id]
        if len(requested) != 1:
            raise RecognitionConflict('steer_learning_model_request_unavailable')
        with self.freshness._read_scope() as view:
            prepared = self.validate_auxiliary_request(view, request, local_only=owner.local)
            if prepared is None:
                raise RecognitionConflict('steer_learning_model_input_preparation_unavailable')
        payload = {'version': 1, 'turn_id': request['turn_id'], 'model_request_id': model_request_id,
            'request_identity': admission_identity(request), 'input_ref': prepared['input_ref'],
            'input_facts': prepared['frozen_input'],
            'messages': deepcopy(messages), 'response_schema': deepcopy(response_schema),
            'result_schema': deepcopy(result_schema), 'governed': False}
        return native.get_or_create_immutable_payload(request['turn_id'], 'steer-learning-model-input-' + model_request_id, payload)

    @staticmethod
    def _constraints():
        from ..document_recognition import CANDIDATE_SOURCE_CONSTRAINTS
        return CANDIDATE_SOURCE_CONSTRAINTS

    def _model_messages(self, request):
        from .automatic_constraint_input import KIND, automatic_bundle, constraint_markers, frozen_messages
        native = self.freshness._native()
        messages = get('consolidate', version='@3')(None, operation='messages',
            text=request['input']['text'], constraints=self._constraints())
        if constraint_markers(request):
            return frozen_messages(automatic_bundle(request, native.restore_admission(request)), messages)
        if native.get_immutable_payload(request['turn_id'], KIND) is not None:
            raise RecognitionConflict('automatic_constraint_marker_changed')
        return messages

    def _generated(self, view, identity, *, settled):
        native = self.freshness._native()
        request = native.get_request(identity)
        if request is None:
            raise RecognitionConflict('steer_learning_auxiliary_native_unavailable')
        qualified = self.validate_auxiliary_request(view, request, local_only=True)
        if qualified is None:
            raise RecognitionConflict('steer_learning_auxiliary_input_unavailable')
        return self._generation_facts(view, request, qualified, settled=settled)

    def _generation_facts(self, view, request, qualified, *, settled):
        native, identity = self.freshness._native(), request['turn_id']
        saved = native.get_immutable_payload(identity, 'memory-generation-output-v1')
        terminals = [event for event in native.events_after(identity)
            if event['type'] in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
        if (saved is None or len(terminals) != 1 or terminals[0]['type'] != 'turn.completed'
                or terminals[0]['data'].get('payload_ref') != saved[0]):
            raise RecognitionConflict('steer_learning_generation_unavailable')
        with native_source_connection(self.freshness.records, reader=view) as connection:
            attempts = read_model_attempt_bindings(connection, identity)
        succeeded, model_inputs = [], set()
        for attempt in attempts:
            if attempt['dispatch'] is None or attempt['terminal'] is None:
                continue
            dispatched = validate_model_wire_attempt_dispatch(attempt['dispatch'])
            terminal = validate_model_wire_attempt_receipt(attempt['terminal'])
            if (terminal['status'] == 'succeeded' and attempt['status'] == 'terminal' and attempt['effect_state'] == 'SETTLED_OK'
                    and all(dispatched[key] == terminal[key] for key in
                        ('turn_id', 'model_request_id', 'attempt_id', 'attempt_number', 'routing_snapshot_revision', 'provider_id', 'model_id'))
                    and terminal['turn_id'] == identity and attempt['attempt_id'] == terminal['attempt_id']):
                from .steer_learning import SteerLearningOutput
                recorded = native.get_immutable_payload(identity, 'steer-learning-model-input-' + terminal['model_request_id'])
                expected = {'version': 1, 'turn_id': identity, 'model_request_id': terminal['model_request_id'],
                    'request_identity': admission_identity(request), 'input_ref': qualified['input_ref'],
                    'input_facts': qualified['frozen_input'],
                    'messages': self._model_messages(request),
                    'response_schema': SteerLearningOutput.model_json_schema(),
                    'result_schema': SteerLearningOutput.model_json_schema(), 'governed': False}
                if recorded is None or recorded[1] != expected:
                    raise RecognitionConflict('steer_learning_model_input_unavailable')
                succeeded.append(attempt)
                model_inputs.add(recorded[0])
        if not succeeded:
            raise RecognitionConflict('steer_learning_generation_wire_unavailable')
        proposal = native.get_immutable_payload(identity, 'memory-proposal-steer-learning')
        if settled and (proposal is None or proposal[1] != {'committed': True, 'key': 'steer-learning'}):
            raise RecognitionConflict('steer_learning_proposal_unsettled')
        if settled:
            self.validate_proposal_effect(view, request, proposal)
        qualified['model_input_refs'] = sorted(model_inputs)
        return request, saved, proposal, qualified

    def validate_proposal_effect(self, reader, request, proposal=None):
        from ..kernel.memory_turn import proposal_intent
        view, native = self.freshness._reader(reader), self.freshness._native()
        proposal = native.get_immutable_payload(request['turn_id'], 'memory-proposal-steer-learning') if proposal is None else proposal
        if proposal is None or proposal[1] != {'committed': True, 'key': 'steer-learning'}:
            raise RecognitionConflict('steer_learning_proposal_unsettled')
        intent = proposal_intent(request, 'steer-learning')
        log = native.effect_runner.log
        with native_source_connection(self.freshness.records, reader=view) as connection:
            actual_request = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (request['turn_id'],)).fetchone()
            if actual_request is None or json.loads(actual_request[0]) != request:
                raise RecognitionConflict('steer_learning_proposal_request_changed')
            try:
                effect = log.get_in_connection(connection, intent.operation_id)
            except KeyError as error:
                raise RecognitionConflict('steer_learning_proposal_unsettled') from error
            try:
                log._require_frozen_effect_identity(effect, intent)
            except RuntimeError as error:
                raise RecognitionConflict('steer_learning_proposal_intent_changed') from error
            receipt = connection.execute('SELECT receipt_ref,receipt_kind,receipt_schema_version FROM effect_receipt '
                'WHERE operation_id=?', (intent.operation_id,)).fetchone()
            saved = connection.execute('SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads '
                'WHERE turn_id=? AND kind=?', (request['turn_id'], 'memory-proposal-steer-learning')).fetchone()
            if (effect.state != EffectState.SETTLED_OK or effect.result_ref != proposal[0] or effect.settled_at is None
                    or receipt is None or tuple(receipt) != (proposal[0], 'memory_propose-receipt', 'legacy-v1')
                    or saved is None or saved[0] != proposal[0] or proposal[1] != json.loads(saved[1])):
                raise RecognitionConflict('steer_learning_proposal_unsettled')
        return effect.operation_id

    def stage_candidate_origin(self, tx, *, candidate, turn, selected_events, output):
        view = self.freshness._reader(tx)
        actual = view.read('recognition_candidates', candidate.id)
        request, saved, _, qualified = self._generated(view, turn.turn_id, settled=False)
        proposed = saved[1]['output']
        if (candidate.id != 'steer-' + turn.turn_id or actual is None or actual.revision != 1 or actual.payload.get('state') != 'pending'
                or actual.payload.get('scope') != {'user_id': 'local-user', 'project_id': request['scope']['project_id']}
                or proposed != output.model_dump(mode='json')
                or actual.payload.get('content') != proposed['text'].strip()
                or actual.payload.get('conditions') != self.candidate_conditions(view, output, selected_events)
                or actual.payload.get('generation', {}).get('id') != saved[1]['metadata']['generation_id']
                or set(proposed['event_ids']) != {event['event_id'] for event in selected_events}
                or not set(proposed['event_ids']) <= {fact['id'] for fact in qualified['feedback']}):
            raise RecognitionConflict('steer_learning_candidate_output_changed')
        payload = {'version': 1, 'owner': deepcopy(self.freshness.owner),
            'candidate_id': candidate.id, 'candidate_birth': _lineage(view, 'recognition_candidates', candidate.id),
            'candidate': deepcopy(actual.payload), 'auxiliary_turn_id': turn.turn_id,
            'request_identity': admission_identity(request), 'generation_ref': saved[0],
            'input_ref': qualified['input_ref'], 'model_input_refs': qualified['model_input_refs'],
            'event_ids': sorted(proposed['event_ids']), 'proposal_key': 'steer-learning'}
        view.put(CANDIDATES, candidate.id, payload, expected_revision=0)
        return deepcopy(payload)

    def capture_candidate_binding(self, identity, candidate_id):
        with self.freshness._read_scope() as view:
            row = view.read(CANDIDATES, candidate_id)
            candidate = view.read('recognition_candidates', candidate_id)
            if (row is None or row.revision != 1 or row.payload.get('owner') != self.freshness.owner
                    or row.payload.get('auxiliary_turn_id') != identity or candidate_id != 'steer-' + identity
                    or candidate is None):
                raise RecognitionConflict('steer_learning_candidate_binding_unavailable')
            verify_lineage(view, row.payload['candidate_birth'])
            self._generated(view, identity, settled=False)
            payload = deepcopy(row.payload)
        return self.freshness._native().get_or_create_immutable_payload(identity,
            'steer-learning-candidate-origin-v1', payload)

    def validate_candidate_origin(self, reader, scope, candidate):
        view = self.freshness._reader(reader)
        seed = self._candidate_seed(view, scope, candidate)
        if seed is None:
            return None
        payload, binding, actual = seed
        generated = self._generated(view, payload['auxiliary_turn_id'], settled=True)
        fact = self._candidate_native(scope, payload, binding, actual, generated)
        selected_qualified = [item for feedback in fact['feedback']
            for item in self.learning.qualify(view, view.read('v2_turn_steer_feedback', feedback['id']), local_only=True)[1]]
        return {**fact, 'target_qualified': selected_qualified}

    def _candidate_seed(self, view, scope, candidate):
        self.freshness._validate()
        if candidate is None:
            raise RecognitionConflict('steer_learning_candidate_unavailable')
        identity = candidate.object_id
        row = view.read(CANDIDATES, identity)
        if row is None:
            if self.is_learning_candidate(view, scope, candidate):
                raise RecognitionConflict('steer_learning_candidate_proof_unavailable')
            return None
        actual = view.read('recognition_candidates', identity)
        payload = row.payload
        if (row.revision != 1 or payload.get('owner') != self.freshness.owner or actual is None or actual != candidate
                or actual.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}
                or scope.user_id != 'local-user'):
            raise RecognitionConflict('steer_learning_candidate_owner_changed')
        verify_lineage(view, payload['candidate_birth'])
        binding = self.freshness._native().get_immutable_payload(payload['auxiliary_turn_id'], 'steer-learning-candidate-origin-v1')
        if (identity != 'steer-' + payload['auxiliary_turn_id'] or binding is None or binding[1] != payload):
            raise RecognitionConflict('steer_learning_candidate_binding_changed')
        original = payload['candidate']
        if any(actual.payload.get(key) != original.get(key) for key in
               ('id', 'scope', 'source_experience_ids', 'source_recognition_ids',
                'source_experience_revisions', 'source_recognition_revisions', 'generation', 'created_at')):
            raise RecognitionConflict('steer_learning_candidate_sources_changed')
        return payload, binding, actual

    def _candidate_native(self, scope, payload, binding, actual, generation):
        request, generated, proposal, qualified = generation
        identity, original = actual.object_id, payload['candidate']
        if (admission_identity(request) != payload['request_identity'] or generated[0] != payload['generation_ref']
                or generated[1]['output']['text'].strip() != original['content']
                or sorted(generated[1]['output']['event_ids']) != payload['event_ids']
                or payload['input_ref'] != qualified['input_ref'] or payload['model_input_refs'] != qualified['model_input_refs']):
            raise RecognitionConflict('steer_learning_candidate_native_changed')
        prepared_contexts = qualified['frozen_input']['key']['key'][2]
        expected_parents = {item['id']: item['revision'] for event_id, contexts in prepared_contexts
            if event_id in payload['event_ids'] for context in contexts for item in context['recognitions']
            if item['project_id'] == scope.project_id}
        if (original['source_experience_ids'] != [] or original['source_experience_revisions'] != {}
                or original['source_recognition_ids'] != sorted(expected_parents)
                or original['source_recognition_revisions'] != expected_parents):
            raise RecognitionConflict('steer_learning_candidate_native_sources_changed')
        selected = [fact for fact in qualified['feedback'] if fact['id'] in payload['event_ids']]
        return {'candidate_id': identity, 'candidate_revision': actual.revision,
            'candidate_birth': deepcopy(payload['candidate_birth']), 'auxiliary_turn_id': request['turn_id'],
            'request': request, 'generation_ref': generated[0], 'proposal_ref': proposal[0],
            'candidate_origin_ref': binding[0], 'input_ref': deepcopy(payload['input_ref']),
            'model_input_refs': deepcopy(payload['model_input_refs']),
            'feedback': selected, 'qualified': qualified['qualified']}

    def candidate_relation(self, reader, scope, candidate):
        view = self.freshness._reader(reader)
        fact = self.validate_candidate_origin(view, scope, candidate)
        if fact is None:
            return None
        generated = self.freshness._native().get(fact['generation_ref'])['output']
        hint = view.read('v2_candidate_hints', fact['candidate_id'])
        if (hint is None or hint.revision != 1 or hint.payload != {'project_id': scope.project_id,
                'relation': generated['relation'], 'target_id': generated['target_id'], 'scope_hint': None}):
            raise RecognitionConflict('steer_learning_candidate_hint_changed')
        if generated['relation'] == 'new':
            return None
        wanted = {item['id']: item for context in fact['target_qualified'] for item in context['recognitions']}
        target = wanted.get(generated['target_id'])
        if target is None or target['project_id'] not in {scope.project_id, 'me'}:
            raise RecognitionConflict('steer_learning_candidate_target_unavailable')
        return {'target_id': target['id'], 'target_project_id': target['project_id'],
            'target_revision': target['revision'], 'relation': generated['relation'], 'candidate_id': fact['candidate_id'],
            'feedback_ids': [item['id'] for item in fact['feedback']], 'generation_ref': fact['generation_ref'],
            'proposal_ref': fact['proposal_ref'], 'auxiliary_turn_id': fact['auxiliary_turn_id']}

    def stage_published_origin(self, tx, scope, candidate, recognition):
        view = self.freshness._reader(tx)
        current_candidate = view.read('recognition_candidates', candidate.object_id)
        if current_candidate is None:
            raise RecognitionConflict('steer_learning_publication_candidate_changed')
        if not self.is_learning_candidate(view, scope, current_candidate):
            return None
        if candidate != current_candidate:
            expected = {**deepcopy(candidate.payload), 'state': 'published',
                'reviewed_at': current_candidate.payload.get('reviewed_at'),
                'recognition_id': current_candidate.payload.get('recognition_id')}
            if (candidate.payload.get('state') != 'pending' or current_candidate.revision != candidate.revision + 1
                    or current_candidate.payload != expected):
                raise RecognitionConflict('steer_learning_publication_candidate_changed')
        fact = self.validate_candidate_origin(view, scope, current_candidate)
        if fact is None:
            return None
        identity = recognition.object_id if hasattr(recognition, 'object_id') else recognition.id
        actual = view.read('recognitions', identity)
        source = view.read('recognition_candidates', fact['candidate_id'])
        if (actual is None or actual.revision != 1 or source is None
                or source.payload.get('state') != 'published' or source.payload.get('recognition_id') != identity
                or actual.payload.get('content') != source.payload.get('content')
                or actual.payload.get('conditions') != source.payload.get('conditions')):
            raise RecognitionConflict('steer_learning_publication_changed')
        versions = [row for row in view.list('recognition_versions')
            if row.payload.get('recognition_id') == identity and row.payload.get('action') == 'publish']
        if len(versions) != 1 or versions[0].payload.get('snapshot') != actual.payload:
            raise RecognitionConflict('steer_learning_publication_version_changed')
        payload = {'owner': deepcopy(self.freshness.owner), 'candidate_id': fact['candidate_id'],
            'candidate_birth': deepcopy(fact['candidate_birth']), 'recognition_birth': _lineage(view, 'recognitions', identity),
            'publication': {'id': versions[0].object_id, 'revision': versions[0].revision}}
        anchor = {'version': 1, **deepcopy(payload), 'recognition_id': identity,
            'candidate_origin_ref': fact['candidate_origin_ref'], 'auxiliary_turn_id': fact['auxiliary_turn_id']}
        payload['publication_ref'] = self.freshness._native().get_or_create_immutable_payload(
            fact['auxiliary_turn_id'], PUBLICATION_PREFIX + identity, anchor)
        view.put(RECOGNITIONS, identity, payload, expected_revision=0)
        return deepcopy(payload)

    def source_view(self, reader, scope, row):
        view = self.freshness._reader(reader)
        if row.collection == 'recognitions':
            fact = self.recognition_authority(self.freshness.records, view, scope, row)
        else:
            fact = self.validate_candidate_origin(view, scope, row)
        if fact is None:
            return None
        feedback = fact['feedback']
        if not feedback:
            raise RecognitionConflict('steer_learning_candidate_feedback_unavailable')
        first = feedback[0]
        public = view.read('v2_turns', first['public_turn_id'])
        if public is None:
            raise RecognitionConflict('steer_learning_source_public_unavailable')
        if first['via'] == 'followup':
            message = first['message']
            if (message['public_id'] != public.object_id or public.payload.get('project_id') != message['project_id']
                    or public.payload.get('thread_id') != message['thread_id'] or public.payload.get('user_text') != first['text']):
                raise RecognitionConflict('steer_learning_source_public_changed')
        else:
            binding = self.freshness._native().get(first['binding_ref'])
            parent = binding.get('parent') or binding
            if (binding['public_turn_id'] != public.object_id or public.payload.get('project_id') != parent['project_id']
                    or public.payload.get('thread_id') != parent['thread_id']):
                raise RecognitionConflict('steer_learning_source_public_changed')
        return {'via': 'steer', 'text': first['text'][:12], 'turn': public.object_id,
            'project_id': public.payload['project_id']}

    def recognition_authority(self, actual_owner, uow, scope, recognition):
        if actual_owner is not self.freshness.records:
            raise RecognitionConflict('steer_learning_recognition_actual_owner_changed')
        view = self.freshness._reader(uow)
        view, rows, roots = _learning_family(actual_owner, view, scope, recognition)
        if not roots:
            return None
        from .steer_learning_lineage import validate_family
        nodes = validate_family(view, scope, recognition, rows)
        if recognition.object_id in {root.object_id for root in roots}:
            return self._publication_authority(view, scope, recognition)
        facts = [self._publication_authority(view, scope, root) for root in roots]
        births = {node['id']: node['birth'] for node in nodes}
        identity = {'owner': deepcopy(self.freshness.owner), 'recognition_id': recognition.object_id,
            'recognition_revision': recognition.revision, 'recognition_birth': deepcopy(births[recognition.object_id]),
            'nodes': nodes, 'roots': [{'id': root.object_id, 'revision': root.revision,
                'birth': deepcopy(births[root.object_id]),
                'origin_identity': fact['dependency_revisions']['steer_learning_identity']}
                for root, fact in zip(roots, facts, strict=True)]}
        ancestors = [{'id': row.object_id, 'revision': row.revision, 'birth': deepcopy(births[row.object_id]),
            'state': row.payload['state'], **{key: deepcopy(row.payload[key]) for key in
                ('source_experience_ids', 'source_recognition_ids', 'source_experience_revisions', 'source_recognition_revisions')}}
            for row in (rows[key] for key in sorted(rows)) if row.object_id != recognition.object_id]
        qualified = [item for fact in facts for item in fact['qualified']]
        feedback = [item for fact in facts for item in fact['feedback']]
        return {'recognition_id': recognition.object_id, 'recognition_revision': recognition.revision,
            'recognition_birth': deepcopy(births[recognition.object_id]),
            'qualified': list({encoded(item): item for item in qualified}.values()),
            'feedback': list({encoded(item): item for item in feedback}.values()), 'lineage_recognitions': ancestors,
            'dependency_revisions': {'steer_learning_lineage_identity': encoded(identity)}}

    def _publication_authority(self, view, scope, recognition):
        from .steer_learning_publication import _issue_published_learning_proof
        seed = self._publication_seed(view, scope, recognition)
        proof = _issue_published_learning_proof(self, reader=view, scope=scope, recognition=recognition, seed=seed)
        qualified = [self.freshness.qualify_published_learning_context(context, reader=view,
            publication_proof=proof, local_only=True) for context in seed['contexts']]
        authority = seed['authority']
        return {**authority, 'qualified': _published_privacy_qualifications(qualified, authority['qualified'])}

    def _publication_seed(self, view, scope, recognition):
        from .steer_learning_lineage import retained_recognition_record
        actual = view.read('recognitions', recognition.object_id)
        if actual != recognition or actual.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}:
            raise RecognitionConflict('steer_learning_recognition_changed')
        row = view.read(RECOGNITIONS, recognition.object_id)
        if row is None:
            raise RecognitionConflict('steer_learning_publication_proof_unavailable')
        if row.revision != 1 or row.payload.get('owner') != self.freshness.owner:
            raise RecognitionConflict('steer_learning_publication_owner_changed')
        verify_lineage(view, row.payload['recognition_birth'])
        retained_recognition_record(self.freshness.records, view, scope, recognition.object_id,
            expected_birth=row.payload['recognition_birth'], expected_revision=recognition.revision)
        candidate = view.read('recognition_candidates', row.payload['candidate_id'])
        if candidate is None or candidate.payload.get('state') != 'published' or candidate.payload.get('recognition_id') != recognition.object_id:
            raise RecognitionConflict('steer_learning_publication_candidate_changed')
        candidate_seed = self._candidate_seed(view, scope, candidate)
        if candidate_seed is None:
            raise RecognitionConflict('steer_learning_publication_candidate_changed')
        payload, binding, current = candidate_seed
        request = self.freshness._native().get_request(payload['auxiliary_turn_id'])
        if request is None:
            raise RecognitionConflict('steer_learning_auxiliary_native_unavailable')
        prepared = self._frozen_auxiliary_request(view, request, local_only=True)
        if prepared is None:
            raise RecognitionConflict('steer_learning_auxiliary_input_unavailable')
        generated = self._generation_facts(view, request, prepared, settled=True)
        fact = self._candidate_native(scope, payload, binding, current, generated)
        if fact['candidate_birth'] != row.payload['candidate_birth']:
            raise RecognitionConflict('steer_learning_publication_candidate_changed')
        anchor = self.freshness._native().get_immutable_payload(fact['auxiliary_turn_id'], PUBLICATION_PREFIX + recognition.object_id)
        expected_anchor = {'version': 1, **{key: deepcopy(row.payload[key]) for key in
            ('owner', 'candidate_id', 'candidate_birth', 'recognition_birth', 'publication')},
            'recognition_id': recognition.object_id, 'candidate_origin_ref': fact['candidate_origin_ref'],
            'auxiliary_turn_id': fact['auxiliary_turn_id']}
        if anchor is None or anchor[0] != row.payload.get('publication_ref') or anchor[1] != expected_anchor:
            raise RecognitionConflict('steer_learning_publication_native_changed')
        versions = [row for row in view.list('recognition_versions')
            if row.payload.get('recognition_id') == recognition.object_id and row.payload.get('action') == 'publish']
        if (len(versions) != 1 or versions[0].payload.get('snapshot', {}).get('content') != candidate.payload.get('content')
                or versions[0].payload.get('snapshot', {}).get('conditions') != candidate.payload.get('conditions')
                or row.payload['publication'] != {'id': versions[0].object_id, 'revision': versions[0].revision}):
            raise RecognitionConflict('steer_learning_recognition_publish_changed')
        publication = {'id': versions[0].object_id, 'revision': versions[0].revision}
        identity = {'owner': deepcopy(self.freshness.owner), 'candidate_id': fact['candidate_id'],
            'candidate_birth': deepcopy(fact['candidate_birth']), 'recognition_birth': deepcopy(row.payload['recognition_birth']),
            'auxiliary_turn_id': fact['auxiliary_turn_id'], 'generation_ref': fact['generation_ref'],
            'proposal_ref': fact['proposal_ref'], 'candidate_origin_ref': fact['candidate_origin_ref'],
            'publication': publication, 'feedback_ids': sorted(item['id'] for item in fact['feedback']),
            'input_ref': deepcopy(fact['input_ref']), 'model_input_refs': deepcopy(fact['model_input_refs']),
            'publication_ref': anchor[0]}
        authority = {**fact, 'recognition_id': recognition.object_id, 'recognition_revision': recognition.revision,
            'recognition_birth': deepcopy(row.payload['recognition_birth']), 'publication': publication,
            'dependency_revisions': {'steer_learning_identity': encoded(identity)}}
        contexts = []
        for feedback in prepared['feedback']:
            if feedback['via'] == 'steer':
                values = [feedback['before_context'], feedback['after_context']]
            elif feedback['via'] == 'followup':
                values = [*feedback['before_contexts'], *feedback['after_contexts']]
            else:
                raise RecognitionConflict('steer_learning_publication_feedback_changed')
            contexts.extend(context for context in values if context is not None)
        return {'authority': authority, 'candidate_revision': candidate.revision,
            'contexts': list({encoded(context): deepcopy(context) for context in contexts}.values())}

    def pending_proposals(self, *, project=None, reader=None):
        with self.freshness._read_scope(reader) as view:
            result = []
            for row in view.list(CANDIDATES):
                candidate = view.read('recognition_candidates', row.object_id)
                if candidate is None or project is not None and candidate.payload.get('project_id') != project:
                    continue
                if row.revision != 1 or row.payload.get('owner') != self.freshness.owner:
                    raise RecognitionConflict('steer_learning_candidate_owner_changed')
                verify_lineage(view, row.payload['candidate_birth'])
                _, _, proposal, _ = self._generated(view, row.payload['auxiliary_turn_id'], settled=False)
                binding = self.freshness._native().get_immutable_payload(row.payload['auxiliary_turn_id'], 'steer-learning-candidate-origin-v1')
                if proposal is None or binding is None:
                    result.append({'project_id': candidate.payload['project_id'], 'candidate_id': row.object_id,
                        'auxiliary_turn_id': row.payload['auxiliary_turn_id']})
            return result

    def restore_events(self, reader, identity, *, local_only):
        view = self.freshness._reader(reader)
        request = self.freshness._native().get_request(identity)
        if request is None:
            raise RecognitionConflict('steer_learning_auxiliary_native_unavailable')
        qualified = self.validate_auxiliary_request(view, request, local_only=local_only)
        if qualified is None:
            raise RecognitionConflict('steer_learning_auxiliary_input_unavailable')
        result = []
        for fact in qualified['feedback']:
            row = view.read('v2_turn_steer_feedback', fact['id'])
            _, sources = self.learning.qualify(view, row, local_only=local_only)
            result.append({'event_id': row.object_id, 'via': fact['via'], 'type': fact['category'],
                'direction': fact['direction'], 'turn_id': fact['target_turn_id'], 'before': fact['before'],
                'after': fact['after'], '_row': row, '_qualified': sources})
        return result
