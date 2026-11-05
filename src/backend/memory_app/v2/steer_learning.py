from copy import deepcopy
from datetime import datetime
import json
from urllib.parse import urlsplit
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_evidence_json import encoded
from backend.recognition.product_draft_dependencies import product_draft_source
from core.ai_kernel.product_admission import MANIFEST_KIND, admission_identity
from core.ai_kernel.product_snapshot import read_product_turn_snapshot
from core.document_engine import SQLiteDocumentRepository

from ..answer_effective import NativeAnswerSnapshot
from ..answer_proof import read_answer_input
from ..kernel.receipt_projection import frozen_public_turn_requests
from ..kernel.route_source_snapshot import native_source_connection
from .policies import get, version
from .shared_projects import SharedProjectError
from .turn_message_order import COLLECTION as MESSAGES, TurnMessageOrder


COLLECTION = 'v2_turn_steer_feedback'
TERMINALS = 'v2_turn_learning_terminals'
CONSUMED = 'v2_steer_learning_consumed'
ERRORS = 'v2_steer_learning_errors'
EVENT_ERRORS = 'v2_steer_learning_event_errors'


class SteerLearningOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=40)
    conditions: list[str] = Field(default_factory=list, max_length=12)
    event_ids: list[str] = Field(min_length=1)
    kind: Literal['pattern', 'correction']
    target_id: str | None = None
    relation: Literal['new', 'supplement', 'differs'] = 'new'


class SteerLearning:
    def __init__(self, application, freshness):
        if freshness.application is not application:
            raise ValueError('steer_learning_owner_changed')
        self.application, self.freshness = application, freshness
        self.records, self.root = freshness.records, freshness.root
        self.order = TurnMessageOrder(freshness)
        from .steer_learning_sources import SteerLearningSources
        self.sources = SteerLearningSources(self)

    def record_message(self, tx, **kwargs):
        return self.order.record_message(tx, **kwargs)

    def _snapshot(self, view, request):
        with native_source_connection(self.records, reader=view) as connection:
            snapshot = read_product_turn_snapshot(connection, request['turn_id'])
            if snapshot is None or snapshot['request'] != request:
                raise RecognitionConflict('steer_learning_native_proof_unavailable')
            snapshot['events'] = [json.loads(raw[0]) for raw in connection.execute(
                'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (request['turn_id'],))]
            if not snapshot['steers'] and connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_turn_steers'").fetchone():
                by_ref = {reference: payload for reference, payload in snapshot['immutable'].values()}
                snapshot['steers'] = [{'payload_ref': row[0], 'payload': by_ref[row[0]], 'state': row[1],
                    'input_ref': row[2], 'decision_ref': row[3], 'revision': row[4]}
                    for row in connection.execute('SELECT payload_ref,state,input_ref,decision_ref,revision '
                        'FROM ai_turn_steers WHERE turn_id=? ORDER BY rowid', (request['turn_id'],))]
        terminals = [event for event in snapshot['events']
            if event['type'] in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
        if not terminals:
            return None
        if len(terminals) != 1 or snapshot['events'][-1] != terminals[0]:
            raise RecognitionConflict('steer_learning_terminal_changed')
        native = NativeAnswerSnapshot(snapshot)
        native.restore_admission(request)
        return native, terminals[0]

    def _output(self, view, entry, native, terminal):
        request = entry['native_request']
        public = view.read('v2_turns', entry['public_id'])
        if public is None:
            raise RecognitionConflict('steer_learning_public_unavailable')
        if terminal['type'] != 'turn.completed':
            return None
        if public.payload['intent'] == 'ask':
            saved = native.get_immutable_payload(request['turn_id'], 'product-answer-result-v2')
            answer = saved[1].get('receipt', {}).get('ask') if saved else None
            actual = public.payload.get('receipt', {}).get('ask')
            if (not isinstance(answer, dict) or not isinstance(actual, dict) or answer.get('error')
                    or 'interruption' in answer or 'interruption' in actual
                    or not isinstance(answer.get('answer'), str)):
                raise RecognitionConflict('steer_learning_answer_unavailable')
            if any(answer.get(key) != actual.get(key) for key in
                   ('answer', 'citations', 'egress_receipt_id', 'unverified_claims', 'unverified_claim_spans', 'no_match')):
                raise RecognitionConflict('steer_learning_answer_changed')
            inputs = read_answer_input(native, request['turn_id'])
            if not answer.get('no_match') and (not answer['answer'].strip() or inputs is None):
                raise RecognitionConflict('steer_learning_answer_input_unavailable')
            return {'text': answer['answer'], 'result_ref': saved[0], 'input_ref': inputs[0] if inputs else None,
                'document_id': None, 'document_revision': None, 'no_match': answer.get('no_match') is True}
        task = public.payload.get('receipt', {}).get('do', {})
        if task.get('state') not in {'done', 'partial'} or not task.get('document_id'):
            raise RecognitionConflict('steer_learning_delivery_unavailable')
        operation = view.read('v2_task_draft_operations', 'deliver-' + request['turn_id'])
        result = operation.payload.get('result') if operation else None
        if (not isinstance(result, dict) or result.get('document_id') != task['document_id']
                or type(result.get('document_revision')) is not int):
            raise RecognitionConflict('steer_learning_delivery_unavailable')
        bound = product_draft_source(self.records, WorkScope('local-user', request['scope']['project_id']),
            task['document_id'], result['document_revision'], turn_id=request['turn_id'], reader=view)
        if bound is None or bound.public_turn_id != entry['public_id'] or bound.request != request:
            raise RecognitionConflict('steer_learning_delivery_changed')
        text = SQLiteDocumentRepository(view, namespace_id=self.freshness.documents.namespace_id).markdown(
            task['document_id'], revision=result['document_revision'])
        if not isinstance(text, str) or not text.strip() or operation.payload['inputs'].get('markdown') != text:
            raise RecognitionConflict('steer_learning_delivery_text_changed')
        return {'text': text, 'result_ref': result['receipt_ref'], 'input_ref': None,
            'document_id': task['document_id'], 'document_revision': result['document_revision'], 'no_match': False}

    def _completion_context(self, view, request, native):
        context = self.freshness.completion_context(request, reader=view)
        requested = {event['correlation']['model_request_id'] for event in native.events_after(request['turn_id'])
            if event['type'] == 'model.requested' and event['data'].get('model_call_purpose') == 'primary'}
        dispatched = {event['correlation']['model_request_id'] for event in native.events_after(request['turn_id'])
            if event['type'] == 'model.attempt.dispatched'} & requested
        consumed = {self.freshness._consumed_basis(proof['basis_ref'])['model_request_id']
            for proof in context['proof_refs'] if proof['turn_id'] == request['turn_id']}
        if not dispatched <= consumed:
            raise RecognitionConflict('steer_learning_consumed_context_unavailable')
        qualified = self.freshness.qualify_learning_context(context, reader=view, local_only=True)
        if not qualified['complete']:
            raise RecognitionConflict('steer_learning_context_unverifiable')
        return context

    def _completed_public(self, view, public_id):
        entries = frozen_public_turn_requests(self.records, self.root, public_id, reader=view)
        result = []
        for entry in entries:
            public = view.read('v2_turns', entry['public_id'])
            if public is not None and public.payload.get('intent') == 'do':
                execution = view.read('v2_task_executions', entry['public_id'])
                if execution is not None and execution.payload.get('executor', 'builtin') != 'builtin':
                    # 外部执行尚无此处的实际模型输入证明，保持未处理，不能授予空来源资格。
                    return None
            if not entry['qualified']:
                raise RecognitionConflict('steer_learning_identity_unavailable')
            request = entry['native_request']
            snapshot = self._snapshot(view, request)
            if snapshot is None:
                return None
            native, terminal = snapshot
            binding = native.get_immutable_payload(request['turn_id'], 'workbench-steer-binding-v1')
            if binding is None or binding[1] != entry['identity']:
                raise RecognitionConflict('steer_learning_identity_changed')
            output = self._output(view, entry, native, terminal)
            context = self._completion_context(view, request, native) if output is not None else None
            result.append({'entry': entry, 'native': native, 'terminal': terminal,
                'binding_ref': binding[0], 'output': output, 'context': context})
        return result

    def _steer_facts(self, view, public_id, completed):
        result = []
        for item in completed:
            entry, native, terminal = item['entry'], item['native'], item['terminal']
            request, identity = entry['native_request'], entry['identity']
            for row in native.workbench_steers(public_id):
                fact, reference = row['payload'], row['payload_ref']
                if fact['turn_id'] != request['turn_id']:
                    continue
                policy = get('steer', version=fact['policy_version'])
                policy.validate(fact['text'], count=0)
                received = [event for event in native.events_after(request['turn_id'])
                    if event['type'] == 'turn.steer.received' and reference in event['data']['evidence_refs']]
                adopted = native.get(row['input_ref']) if row['input_ref'] is not None else None
                applied = [event for event in native.events_after(request['turn_id'])
                    if event['type'] == 'turn.steer.applied' and row['input_ref'] in event['data']['evidence_refs']]
                if (fact.get('binding_ref') != item['binding_ref'] or len(received) != 1
                        or fact.get('policy_version') != request['policy_versions'].get('steer')
                        or any(fact.get(key) != identity.get(key) for key in
                            ('public_turn_id', 'target_turn_id', 'owner_user_id', 'namespace_id', 'origin_kind',
                             'project_id', 'scene', 'thread_id', 'part', 'part_turn_ids'))
                        or received[0]['sequence'] >= terminal['sequence']):
                    raise RecognitionConflict('steer_learning_received_changed')
                used = row['state'] in {'injected', 'released', 'amended'}
                if used and (adopted is None or len(applied) != 1
                        or adopted.get('steer_refs', []).count(reference) != 1
                        or fact.get('lease') != {'owner_id': adopted.get('run_owner_id'),
                            'generation': adopted.get('run_generation')}
                        or not received[0]['sequence'] < applied[0]['sequence'] < terminal['sequence']):
                    raise RecognitionConflict('steer_learning_adoption_changed')
                before_context = fact.get('snapshot', {}).get('context')
                if not isinstance(before_context, dict):
                    raise RecognitionConflict('steer_learning_before_context_unavailable')
                qualified = self.freshness.qualify_learning_context(before_context, reader=view, local_only=True)
                if not qualified['complete']:
                    raise RecognitionConflict('steer_learning_context_unverifiable')
                tail = fact['snapshot'].get('answer_tail', '')
                step = fact['snapshot'].get('step', '')
                if not isinstance(tail, str) or len(tail) > policy.snapshot_characters or not isinstance(step, str):
                    raise RecognitionConflict('steer_learning_before_text_changed')
                classification = policy(fact['text'])
                origin = native.get_immutable_payload(request['turn_id'], 'material-origin-v1')
                public = view.read('v2_turns', entry['public_id'])
                result.append({'version': 1, 'id': 'feedback-' + fact['id'], 'via': 'steer',
                    'public_turn_id': public_id, 'target_turn_id': entry['public_id'],
                    'project_id': identity['project_id'], 'thread_id': identity['thread_id'],
                    'owner': deepcopy(self.freshness.owner), 'native_request': request,
                    'request_identity': admission_identity(request), 'binding_ref': item['binding_ref'],
                    'manifest_ref': native.get_immutable_payload(request['turn_id'], MANIFEST_KIND)[0],
                    'steer_ref': reference, 'received_event': received[0]['event_id'],
                    'input_ref': row['input_ref'], 'applied_event': applied[0]['event_id'] if used else None,
                    'terminal': deepcopy(terminal), 'policy_version': fact['policy_version'],
                    'category': classification['category'], 'direction': classification['direction'],
                    'learnable': bool(classification['learnable'] and used and item['output'] is not None and origin is None
                        and public.payload.get('by') != 'admin'),
                    'before_context': before_context, 'after_context': item['context'],
                    'before': tail or policy.tail(step), 'text': fact['text'],
                    'after': fact['text'] + '\n' + (item['output']['text'][:policy.snapshot_characters] if item['output'] else ''),
                    'output': {key: value for key, value in item['output'].items() if key != 'text'} if item['output'] else None})
        return result

    def _message_finished(self, view, row, completed):
        public = view.read('v2_turns', row.payload['public_id'])
        if (public is None or public.payload.get('project_id') != row.payload['project_id']
                or public.payload.get('thread_id') != row.payload['thread_id']
                or public.payload.get('user_text') != row.payload['text']
                or public.payload.get('parent_turn_id') is not None):
            raise RecognitionConflict('steer_learning_message_changed')
        if public.payload['intent'] in {'ask', 'do', 'multi'}:
            return completed is not None and bool(completed)
        if public.payload['intent'] == 'remember':
            receipt = public.payload.get('receipt', {}).get('remember', {})
            if receipt.get('state') == 'done' and receipt.get('item_id') is None:
                return True
            item = view.read('workspace_items', receipt.get('item_id', ''))
            return bool(item and item.payload.get('project_id') == row.payload['project_id']
                and item.payload.get('status') in {'confirmed', 'failed'})
        if public.payload['intent'] == 'inspiration':
            candidate = public.payload.get('receipt', {}).get('inspiration', {}).get('insight', {})
            actual = view.read('recognition_candidates', candidate.get('id', ''))
            return bool(actual and actual.payload.get('scope') == {'user_id': 'local-user', 'project_id': row.payload['project_id']})
        return False

    def _followup_fact(self, view, public_id, completed):
        pair = self.order.pair(view, public_id)
        if pair is None:
            return None
        previous, current = pair
        if (current.payload['origin'] != 'user' or current.payload['actor'].get('by') is not None
                or not self._message_finished(view, current, completed)):
            return None
        previous_public = view.read('v2_turns', previous.object_id)
        if previous_public is None or previous_public.payload.get('intent') not in {'ask', 'do', 'multi'}:
            return None
        prior = self._completed_public(view, previous.object_id)
        if not prior or any(item['output'] is None for item in prior):
            return None
        latest = max(prior, key=lambda item: datetime.fromisoformat(item['terminal']['occurred_at']))
        policy = get('steer', version=latest['entry']['native_request']['policy_versions']['steer'])
        if not policy.followup(latest['terminal']['occurred_at'], current.payload['accepted_at']):
            return None
        classified = policy(current.payload['text'])
        contexts = [item['context'] for item in prior]
        after_contexts = [item['context'] for item in completed if item['context'] is not None]
        after_text = '\n'.join(item['output']['text'] for item in completed if item['output'] is not None)
        return {'version': 1, 'id': 'followup-' + current.object_id, 'via': 'followup',
            'public_turn_id': public_id, 'target_turn_id': previous.object_id,
            'project_id': current.payload['project_id'], 'thread_id': current.payload['thread_id'],
            'owner': deepcopy(self.freshness.owner), 'previous_message': deepcopy(previous.payload),
            'message': deepcopy(current.payload), 'terminal': deepcopy(latest['terminal']),
            'native_request': latest['entry']['native_request'],
            'request_identity': admission_identity(latest['entry']['native_request']),
            'binding_ref': latest['binding_ref'], 'policy_version': latest['entry']['native_request']['policy_versions']['steer'],
            'category': classified['category'], 'direction': classified['direction'],
            'learnable': bool(classified['learnable'] and all(item['native'].get_immutable_payload(
                item['entry']['native_request']['turn_id'], 'material-origin-v1') is None for item in prior)),
            'before_contexts': contexts, 'after_contexts': after_contexts,
            'before': policy.tail('\n'.join(item['output']['text'] for item in prior)),
            'text': current.payload['text'], 'after': current.payload['text'] + '\n'
                + after_text[:policy.snapshot_characters],
            'outputs': [{key: value for key, value in item['output'].items() if key != 'text'} for item in prior],
            'after_outputs': [{key: value for key, value in item['output'].items() if key != 'text'}
                for item in completed if item['output'] is not None]}

    def stage_public_in_uow(self, tx, public_id):
        view = self.freshness._reader(tx)
        self.freshness._validate()
        public = view.read('v2_turns', public_id)
        if public is None:
            if self.order.read(view, public_id) is not None:
                return ()
            raise RecognitionConflict('steer_learning_public_unavailable')
        completed = (self._completed_public(view, public_id)
            if public.payload.get('intent') in {'ask', 'do', 'multi'} else [])
        if completed is None:
            return ()
        facts = self._steer_facts(view, public_id, completed)
        followup = self._followup_fact(view, public_id, completed)
        if followup is not None:
            facts.append(followup)
        for fact in facts:
            old = view.read(COLLECTION, fact['id'])
            if old is not None and (old.revision != 1 or old.payload != fact):
                raise RecognitionConflict('steer_learning_replay_changed')
            if old is None:
                view.put(COLLECTION, fact['id'], fact, expected_revision=0)
            if fact['learnable'] and view.read(EVENT_ERRORS, fact['id']) is not None:
                self._save_event_observation(view, view.read(COLLECTION, fact['id']), None)
        message = self.order.read(view, public_id)
        if completed or message is not None and self._message_finished(view, message, completed):
            payload = {'owner': deepcopy(self.freshness.owner), 'public_id': public_id,
                'terminals': [item['terminal'] for item in completed], 'feedback_ids': [fact['id'] for fact in facts]}
            old = view.read(TERMINALS, public_id)
            if old is not None and old.payload != payload:
                raise RecognitionConflict('steer_learning_terminal_replay_changed')
            if old is None:
                view.put(TERMINALS, public_id, payload, expected_revision=0)
        return tuple(facts)

    def _observation(self, view, public_id):
        public = view.read('v2_turns', public_id)
        if public is not None:
            return {'project_id': public.payload['project_id'], 'public_revision': public.revision}
        message = self.order.read(view, public_id)
        if message is None:
            raise RecognitionConflict('steer_learning_public_unavailable')
        return {'project_id': message.payload['project_id'], 'public_revision': None}

    def _save_observation(self, view, public_id, observation, *, state, error):
        old = view.read(ERRORS, public_id)
        if old is not None and (old.payload.get('owner') != self.freshness.owner
                or old.payload.get('project_id') != observation['project_id']):
            raise RecognitionConflict('steer_learning_observation_owner_changed')
        payload = {'owner': deepcopy(self.freshness.owner), **observation, 'state': state, 'error': error}
        if old is not None and {key: value for key, value in old.payload.items() if key != 'at'} == payload:
            return
        if old is not None or error is not None:
            view.put(ERRORS, public_id, {**payload, 'at': self.freshness.now()}, expected_revision=old.revision if old else 0)

    def observe_public(self, public_id):
        self.freshness._validate()
        with self.freshness._read_scope() as view:
            observation = self._observation(view, public_id)
        try:
            request = self.freshness._native().get_request(public_id)
            with self.freshness.source_scope(request=request), self.records.begin() as tx:
                view = self.freshness._reader(tx)
                current = self._observation(view, public_id)
                if current != observation:
                    raise RecognitionConflict('steer_learning_public_revision_changed')
                facts = self.stage_public_in_uow(tx, public_id)
                state = 'completed' if view.read(TERMINALS, public_id) is not None else 'pending'
                self._save_observation(view, public_id, current, state=state, error=None)
                tx.commit()
            return {'state': state, 'staged': len(facts), 'error': None}
        except (RecognitionConflict, ValueError) as error:
            code = str(error)
            if not isinstance(error, (RecognitionConflict, SharedProjectError)) and not code.startswith((
                    'public_native_', 'product_snapshot_', 'product_admission_', 'effective_answer_')):
                raise
            self.freshness._validate()
            with self.records.begin() as tx:
                view = self.freshness._reader(tx)
                current = self._observation(view, public_id)
                if current['project_id'] != observation['project_id']:
                    raise RecognitionConflict('steer_learning_observation_owner_changed') from error
                if current != observation:
                    code = 'steer_learning_public_revision_changed'
                self._save_observation(view, public_id, current, state='pending', error=code)
                tx.commit()
            return {'state': 'pending', 'staged': 0, 'error': code}

    def pending_public_ids(self):
        self.freshness._validate()
        pending = {row.object_id for row in self.records.list(MESSAGES)
            if self.records.read(TERMINALS, row.object_id) is None}
        pending.update(row.object_id for row in self.records.list(ERRORS)
            if row.payload.get('owner') == self.freshness.owner and row.payload.get('state') == 'pending')
        pending.update(row.payload['public_turn_id'] for row in self.records.list(EVENT_ERRORS)
            if row.payload.get('owner') == self.freshness.owner and row.payload.get('state') == 'pending')
        with self.freshness._read_scope() as view:
            with native_source_connection(self.records, reader=view) as connection:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_turn_steers'").fetchone():
                    pending.update(row[0] for row in connection.execute('SELECT DISTINCT public_turn_id FROM ai_turn_steers')
                        if view.read(TERMINALS, row[0]) is None)
        return tuple(sorted(pending))

    def refresh(self):
        return sum(self.observe_public(identity)['staged'] for identity in self.pending_public_ids())

    def _canonical(self, view, row):
        if row.revision != 1 or row.payload.get('owner') != self.freshness.owner:
            raise RecognitionConflict('steer_learning_feedback_owner_changed')
        fact = row.payload
        completed = self._completed_public(view, fact['public_turn_id'])
        if completed is None:
            raise RecognitionConflict('steer_learning_feedback_not_terminal')
        if fact['via'] == 'steer':
            canonical = next((item for item in self._steer_facts(view, fact['public_turn_id'], completed)
                if item['id'] == row.object_id), None)
        else:
            canonical = self._followup_fact(view, fact['public_turn_id'], completed)
        if canonical != fact:
            raise RecognitionConflict('steer_learning_feedback_changed')
        return canonical

    def qualify(self, reader, row, *, local_only):
        view = self.freshness._reader(reader)
        fact = self._canonical(view, row)
        contexts = ([fact['before_context'], fact['after_context']] if fact['via'] == 'steer'
            else [*fact['before_contexts'], *fact['after_contexts']])
        contexts = list({encoded(context): context for context in contexts if context is not None}.values())
        qualified = [self.freshness.qualify_learning_context(context, reader=view, local_only=local_only)
            for context in contexts]
        if any(not item['complete'] for item in qualified):
            raise RecognitionConflict('steer_learning_context_unverifiable')
        return fact, qualified

    @staticmethod
    def _source_failure(error):
        return isinstance(error, (RecognitionConflict, SharedProjectError)) or str(error).startswith((
            'public_native_', 'product_snapshot_', 'product_admission_', 'effective_answer_'))

    def _eligible_feedback(self, view, *, project=None, seen_event_ids=()):
        seen = set(seen_event_ids)
        for row in view.list(COLLECTION):
            payload = row.payload
            if (payload.get('owner') != self.freshness.owner or not payload.get('learnable')
                    or payload.get('category') not in {'future', 'correction'}
                    or project is not None and payload.get('project_id') != project
                    or 'steer:' + row.object_id in seen or view.read(CONSUMED, row.object_id) is not None):
                continue
            yield row

    def _save_event_observation(self, view, row, error):
        if view.connection.execute('PRAGMA query_only').fetchone()[0]:
            raise RecognitionConflict('steer_learning_event_writer_required')
        if view.read(COLLECTION, row.object_id) != row:
            raise RecognitionConflict('steer_learning_event_observation_changed')
        old = view.read(EVENT_ERRORS, row.object_id)
        payload = {'owner': deepcopy(self.freshness.owner), 'project_id': row.payload['project_id'],
            'public_turn_id': row.payload['public_turn_id'], 'feedback_revision': row.revision,
            'state': 'pending' if error is not None else 'qualified', 'error': error}
        if old is not None and (old.payload.get('owner') != self.freshness.owner
                or old.payload.get('project_id') != row.payload['project_id']):
            raise RecognitionConflict('steer_learning_event_observation_owner_changed')
        if old is not None and {key: value for key, value in old.payload.items() if key != 'at'} == payload:
            return
        if old is not None or error is not None:
            view.put(EVENT_ERRORS, row.object_id, {**payload, 'at': self.freshness.now()}, expected_revision=old.revision if old else 0)

    def observe_events(self, project, *, local_only):
        with self.freshness._read_scope() as view:
            rows = list(self._eligible_feedback(view, project=project))
        pending = []
        for row in rows:
            try:
                request = self.freshness._native().get_request(row.payload['public_turn_id'])
                with self.freshness.source_scope(request=request), self.records.begin() as tx:
                    view = self.freshness._reader(tx)
                    if view.read(COLLECTION, row.object_id) != row:
                        raise RecognitionConflict('steer_learning_event_observation_changed')
                    if view.read(CONSUMED, row.object_id) is not None:
                        tx.rollback()
                        continue
                    self.qualify(view, row, local_only=local_only)
                    self._save_event_observation(view, row, None)
                    tx.commit()
            except (RecognitionConflict, ValueError) as error:
                if not self._source_failure(error):
                    raise
                with self.records.begin() as tx:
                    view = self.freshness._reader(tx)
                    if view.read(CONSUMED, row.object_id) is not None:
                        tx.rollback()
                        continue
                    self._save_event_observation(view, row, str(error))
                    tx.commit()
                pending.append(row.object_id)
        return tuple(pending)

    def consumer_events(self, project, *, reader=None, local_only):
        if version('consolidate') != '@3':
            return []
        with self.freshness._read_scope(reader) as view:
            result = []
            for row in self._eligible_feedback(view, project=project):
                try:
                    fact, qualified = self.qualify(view, row, local_only=local_only)
                except (RecognitionConflict, ValueError) as error:
                    if not self._source_failure(error):
                        raise
                    continue
                result.append({'event_id': row.object_id, 'via': fact['via'], 'type': fact['category'],
                    'direction': fact['direction'], 'turn_id': fact['target_turn_id'], 'before': fact['before'],
                    'after': fact['after'], '_row': row, '_qualified': qualified})
            return get('consolidate', version='@3')(None, operation='events', events=result)

    def countable_events(self, reader, *, project=None, seen_event_ids=None, record_errors=False):
        view = self.freshness._reader(reader)
        if seen_event_ids is None:
            seen_event_ids = {identity for row in view.list('v2_learning_accumulation')
                if project is None or row.object_id == project for identity in row.payload.get('seen_event_ids', [])}
        result = {}
        for row in self._eligible_feedback(view, project=project, seen_event_ids=seen_event_ids):
            try:
                fact, _ = self.qualify(view, row, local_only=True)
            except (RecognitionConflict, ValueError) as error:
                if not self._source_failure(error):
                    raise
                if record_errors:
                    self._save_event_observation(view, row, str(error))
                continue
            if record_errors:
                self._save_event_observation(view, row, None)
            result.setdefault(fact['project_id'], set()).add('steer:' + row.object_id)
        return result

    def validate_events(self, reader, project, events, *, local_only, consumption=None):
        view = self.freshness._reader(reader)
        for event in events:
            row = view.read(COLLECTION, event['event_id'])
            if row != event['_row'] or row.payload.get('project_id') != project:
                raise RecognitionConflict('steer_learning_consumer_changed')
            fact, qualified = self.qualify(view, row, local_only=local_only)
            if not fact['learnable'] or fact['category'] != event['type']:
                raise RecognitionConflict('steer_learning_consumer_changed')
            consumed = view.read(CONSUMED, event['event_id'])
            if consumed is not None and (consumption is None or consumed.payload != {
                    **consumption, 'project_id': project, 'feedback_revision': row.revision}):
                raise RecognitionConflict('steer_learning_already_consumed')
            event['_qualified'] = qualified

    def pending_projects(self):
        return {item['project_id'] for item in self.sources.pending_proposals()}

    def recover_pending(self, maintenance, project):
        config = maintenance.models.public()['generation'] if maintenance.models is not None else {}
        local = urlsplit(str(config.get('base_url', ''))).hostname in {'localhost', '127.0.0.1', '::1'}
        recovered = 0
        for item in self.sources.pending_proposals(project=project):
            with self.freshness._read_scope() as view:
                events = self.sources.restore_events(view, item['auxiliary_turn_id'], local_only=local)
            recovered += self.consolidate(maintenance, project, events, restore_turn_id=item['auxiliary_turn_id'])
            with self.freshness._read_scope() as view:
                verified = self.sources.validate_candidate_origin(view, WorkScope('local-user', project),
                    view.read('recognition_candidates', item['candidate_id']))
                if verified is None:
                    raise RecognitionConflict('steer_learning_candidate_proof_unavailable')
        return recovered

    def consolidate(self, maintenance, project, events, *, restore_turn_id=None):
        from ..document_recognition import CANDIDATE_SOURCE_CONSTRAINTS
        from ..source_egress import recognition_service
        from .consolidation_events import original_identities
        from .memory_turn import MemoryTurn
        from .turn_requests import freeze_product_turn
        events = tuple(events)
        models = maintenance.models
        if not events or models is None:
            return 0
        config = models.public()['generation']
        if not config.get('configured') or config.get('enabled') is False:
            return 0
        local = urlsplit(str(config.get('base_url', ''))).hostname in {'localhost', '127.0.0.1', '::1'}
        event_ids = sorted(event['event_id'] for event in events)
        signatures = [[event['event_id'], event['_row'].revision, event['_row'].payload]
            for event in sorted(events, key=lambda item: item['event_id'])]
        source_context = [[event['event_id'], deepcopy(event['_qualified'])] for event in sorted(events, key=lambda item: item['event_id'])]
        key = ['steer-learning-v1', signatures, source_context]
        if restore_turn_id is None:
            previous = next((row for row in self.records.list('v2_memory_turn_keys')
                if row.payload.get('identity') == {'kind': 'memory.consolidate', 'project': project, 'key': key}), None)
        else:
            previous = self.records.read('v2_memory_turn_keys', restore_turn_id)
            identity = previous.payload.get('identity') if previous else None
            if (not isinstance(identity, dict) or identity.get('kind') != 'memory.consolidate'
                    or identity.get('project') != project or identity.get('key', [])[:2] != ['steer-learning-v1', signatures]):
                raise RecognitionConflict('steer_learning_recovery_input_changed')
            key = deepcopy(identity['key'])
        consumption = ({'candidate_id': 'steer-' + previous.object_id, 'auxiliary_turn_id': previous.object_id}
            if previous is not None else None)
        with self.freshness._read_scope() as view:
            self.validate_events(view, project, events, local_only=local, consumption=consumption)
            if restore_turn_id is None and source_context != [[event['event_id'], event['_qualified']]
                    for event in sorted(events, key=lambda item: item['event_id'])]:
                raise RecognitionConflict('steer_learning_input_context_changed')
        materials = {}
        for event in events:
            for qualified in event['_qualified']:
                for source in qualified['current_sources']:
                    if not source['privacy_only'] and source['material']['project_id'] in {project, 'me'}:
                        material = source['material']
                        materials[encoded(material)] = material
        with self.freshness._read_scope() as view:
            context_recognitions, feedback_events = [], []
            seen = set()
            for event in events:
                fact = event['_row'].payload
                public = view.read('v2_turns', fact['target_turn_id'])
                if public is None:
                    raise RecognitionConflict('steer_learning_condition_source_unavailable')
                feedback_events.append({**{key: event[key] for key in
                    ('event_id', 'via', 'type', 'direction', 'turn_id', 'before', 'after')},
                    'situation': fact['native_request']['input'].get('situation'), 'question': public.payload['user_text']})
                for qualified in event['_qualified']:
                    for source in qualified['recognitions']:
                        if source['project_id'] not in {project, 'me'}:
                            continue
                        key = source['project_id'], source['id'], source['revision']
                        if key in seen:
                            continue
                        seen.add(key)
                        row = view.read('recognitions', source['id'])
                        if row is None or row.revision != source['revision']:
                            raise RecognitionConflict('steer_learning_context_recognition_changed')
                        context_recognitions.append({**source, 'content': row.payload['content'],
                            'conditions': row.payload.get('conditions', [])})
        feedback = {'steers': feedback_events, 'context_recognitions': context_recognitions}
        rendered = json.dumps(feedback, ensure_ascii=False, sort_keys=True)
        def validate():
            if models.public()['generation'] != config:
                raise RecognitionConflict('steer_learning_model_configuration_changed')
            maintenance._owns_run(self.records)
            with self.freshness._read_scope() as view:
                self.validate_events(view, project, events, local_only=local, consumption=consumption)
        def freeze(kind, **kwargs):
            def feedback_text():
                validate()
                return rendered
            return freeze_product_turn(kind, **{**kwargs, 'load_verified_feedback': feedback_text})
        turn = MemoryTurn(self.records, models, kind='memory.consolidate', project=project,
            key=key, materials=list(materials.values()), validate=validate,
            freeze_request=freeze,
            automatic={'producer': 'consolidation', 'mode': 'project', 'scene': None, 'anchors': []})
        with self.records.begin() as tx:
            self.sources.stage_auxiliary_input(tx, turn, events)
            tx.commit()
        messages = get('consolidate', version='@3')(None, operation='messages',
            text=turn.request['input']['text'], constraints=CANDIDATE_SOURCE_CONSTRAINTS)
        output, metadata = turn.generate(messages, response_model=SteerLearningOutput, max_tokens=1000,
            input_observer=self.sources.capture_model_input)
        selected = get('consolidate', version='@3')(None, operation='selected', events=events, event_ids=output.event_ids)
        selected_roots = set()
        for event in selected:
            for qualified in event['_qualified']:
                for source in qualified['privacy_ceiling']['sources']:
                    selected_roots.update((source['origin']['namespace'], *identity)
                        for identity in original_identities(self.records, source['previous']))
        if not get('consolidate', version='@3')(None, operation='accept', events=events, event_ids=output.event_ids,
                expected_ids=event_ids, kind=output.kind, source_count=len(selected_roots)):
            raise RecognitionConflict('steer_learning_output_evidence_invalid')
        eligible_targets = {item['id']: item for event in selected for qualified in event['_qualified']
            for item in qualified['recognitions'] if item['project_id'] in {project, 'me'}}
        if (output.relation == 'new') != (output.target_id is None) or output.target_id is not None and output.target_id not in eligible_targets:
            raise RecognitionConflict('steer_learning_output_target_invalid')
        candidate_id = 'steer-' + turn.turn_id
        def existing():
            row = self.records.read('recognition_candidates', candidate_id)
            if row is None or self.records.read('v2_steer_learning_candidate_sources', candidate_id) is None:
                return None
            self.sources.capture_candidate_binding(turn.turn_id, candidate_id)
            return 1
        def commit():
            with self.records.begin() as tx:
                view = self.freshness._reader(tx)
                turn.validate_commit(view)
                maintenance._owns_run(view)
                self.validate_events(view, project, events, local_only=local, consumption=consumption)
                recognition_ids = sorted({item['id'] for event in selected for qualified in event['_qualified']
                    for item in qualified['recognitions'] if item['project_id'] == project})
                service = recognition_service(view, source_owner=self.records)
                candidate = service.propose(scope=WorkScope('local-user', project), content=output.text.strip(),
                    source_experience_ids=[], source_recognition_ids=recognition_ids,
                    conditions=self.sources.candidate_conditions(view, output, selected), candidate_id=candidate_id,
                    generation={'id': metadata['generation_id'], 'step_version': 'candidate-from-steer-v1',
                        'model': metadata.get('model'), 'configuration_revision': metadata.get('configuration_revision'),
                        'completed_at': metadata['completed_at']})
                self.sources.stage_candidate_origin(view, candidate=candidate, turn=turn, selected_events=selected, output=output)
                view.put('v2_candidate_hints', candidate.id, {'project_id': project, 'relation': output.relation,
                    'target_id': output.target_id, 'scope_hint': None}, expected_revision=0)
                if output.kind == 'pattern':
                    view.put('v2_insight_patterns', candidate.id, {'kind': output.kind, 'project_id': project,
                        'document_ids': [], 'source_candidate_ids': [], 'event_ids': output.event_ids}, expected_revision=0)
                for event in events:
                    view.put(CONSUMED, event['event_id'], {'project_id': project, 'candidate_id': candidate.id,
                        'auxiliary_turn_id': turn.turn_id, 'feedback_revision': event['_row'].revision}, expected_revision=0)
                tx.commit()
            self.sources.capture_candidate_binding(turn.turn_id, candidate_id)
            return 1
        result = turn.propose(key='steer-learning', write=commit, existing=existing)
        with self.freshness._read_scope() as view:
            verified = self.sources.validate_candidate_origin(view, WorkScope('local-user', project),
                view.read('recognition_candidates', candidate_id))
            if verified is None:
                raise RecognitionConflict('steer_learning_candidate_proof_unavailable')
        return result
