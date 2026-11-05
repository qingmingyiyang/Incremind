"""Submit source organization to the existing durable Turn runtime.

Domain parsers still own chunk/evidence validation. Checkpoints retain completed
model outputs separately from metadata receipts; unknown dispatches never retry.
"""
from datetime import datetime, timezone
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from functools import wraps
import inspect
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from ..kernel.policy_runtime import ProductPolicyRuntime
from core.ai_kernel import ScopedCapabilityRegistry
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.ai_kernel.recovery import classify_recovery
from ..model_config import ModelConfigurationError
from ..turn_routing import _revision
from ..model_binding import public_model_configuration
from .turn_requests import freeze_product_turn, validate_frozen_inputs
from .policies import get, override
from .policies.pipelines import interfaces_for_turn, versions_for_turn

STEPS = "workspace_organize_steps"
INPUTS = "workspace_organize_inputs"


def _policy_bound_complete(complete):
    @wraps(complete)
    def bound(self, messages, *, max_tokens, validate_current, timeout_seconds=None,
              stage=None, validate_output=None):
        key = f"organize-{self.storage_key}-{stage or self.index + 1}"
        saved = self.records.read(STEPS, key)
        frozen = None
        if saved is not None and not saved.payload.get('rejected'):
            source = self.records.read(INPUTS, self.storage_key)
            row = self.source_item()
            request = self.store.get_request(saved.payload['turn_id'])
            public = self.models.public() if callable(getattr(self.models, 'public', None)) else {}
            configuration = public_model_configuration(public.get('generation', {}))
            inputs = {'messages': messages, 'max_tokens': max_tokens,
                      'configuration': configuration, 'project_id': self.project_id}
            material = {'type': 'original_item', 'id': self.item_id,
                        'revision': row.revision if row else None, 'project_id': self.project_id}
            if (source is not None and source.payload == {'project_id': self.project_id, 'source': self.source}
                    and row is not None and row.payload.get('source_text') == self.source
                    and row.payload.get('project_id') == self.project_id
                    and saved.payload['inputs'] == inputs and request is not None
                    and request['desired_outcome'] == 'memory.organize'
                    and request['privacy']['material_refs'] == [material]):
                frozen = request
        if frozen is None:
            selections = versions_for_turn('memory.organize')
        else:
            selections = frozen.get('policy_versions')
            if selections is None:
                selections = {name: '@1' for name in interfaces_for_turn('memory.organize')}
        with override(**selections):
            return get('organize')(complete, self, messages, max_tokens=max_tokens,
                validate_current=validate_current, timeout_seconds=timeout_seconds,
                stage=stage, validate_output=validate_output)
    return bound


class OrganizeTurns:
    def __init__(self, *, root, records, models, item_id, project_id, source, validate_current,
                 lease=None, run_id=None, generation_requests=None, source_context=None):
        self.records, self.models = records, models
        self.item_id, self.project_id, self.source = item_id, project_id, source
        self.validate_current = validate_current
        self.source_context = source_context
        self.storage_key = item_id
        if source_context is not None:
            from .shared_organize import SharedOrganizeSource
            if (not isinstance(source_context, SharedOrganizeSource) or source_context.records is not records
                    or source_context.models is not models or source_context.item_id != item_id
                    or source_context.project_id != project_id or source_context.run_id != run_id
                    or source_context.admission.destination.root != root):
                raise ValueError('shared_organize_owner_changed')
            source_context.validate()
            self.storage_key = source_context.storage_key
        self.store = source_context.store if source_context is not None else SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3")
        self.index = 0
        self.lease, self.run_id = lease, run_id
        from .memory_turn import automatic_admission
        self.constraint_admission = (source_context.constraints() if source_context is not None else
            automatic_admission(records, models, kind='memory.organize', project=project_id,
            automatic={'producer': 'item-organize', 'mode': 'object', 'scene': None,
                       'anchors': [{'type': 'item', 'id': item_id}]}))
        self.generation_requests = generation_requests if generation_requests is not None else {}
        self.input_limit = None

    def _guard_write(self, tx):
        if self.source_context is not None:
            self.source_context.guard(tx)
        elif self.lease is not None:
            self.lease._owned(tx, self.item_id, self.project_id, self.run_id)
        self.validate_commit(tx)

    def source_item(self, reader=None):
        return (self.source_context.item(reader) if self.source_context is not None
            else (reader if reader is not None else self.records).read('workspace_items', self.item_id))

    def source_reader(self, reader=None):
        return (self.source_context.source_reader(reader) if self.source_context is not None
            else nullcontext(reader if reader is not None else self.records))

    def write(self):
        return self.source_context.write() if self.source_context is not None else self.records.begin()

    def commit(self, tx):
        if self.source_context is not None:
            self._guard_write(tx)
        return tx.commit()

    @contextmanager
    def native_scope(self):
        if self.source_context is None:
            yield
        else:
            from .shared_auxiliary import _qualified_commit
            with _qualified_commit(self.source_context.admission):
                yield

    def validate_commit(self, tx):
        from ..transaction_records import TransactionRecords
        for request in tuple(self.generation_requests.values()):
            if self.source_context is not None:
                self.source_context.validate_request(request, actor_reader=tx)
            else:
                validate_frozen_inputs(TransactionRecords(tx), self.models, request, source_owner=self.records)
            if self.constraint_admission is not None:
                self.constraint_admission.validate_commit(request, self.store, tx)

    def reserve_input_limit(self, input_limit, *, error_code, bind_limit=True):
        from .task_attachment_input import input_tokens
        if bind_limit:
            self.input_limit = input_limit
        block = self.constraint_admission.preview_messages([]) if self.constraint_admission is not None else []
        remaining = input_limit - max(0, input_tokens(block) - input_tokens([]))
        if remaining <= 0:
            raise ValueError(error_code)
        return remaining

    def retain_stage_proofs(self, stages):
        if self.constraint_admission is None:
            return
        for stage in stages:
            request = self.store.get_request(stage['turn_id'])
            if request is None or request['scope']['project_id'] != self.project_id:
                raise ValueError('automatic_constraint_stage_unavailable')
            self.constraint_admission.validate_current(request, self.store)
            self.generation_requests[request['turn_id']] = deepcopy(request)

    @_policy_bound_complete
    def complete(self, messages, *, max_tokens, validate_current, timeout_seconds=None,
                 stage=None, validate_output=None):
        self.index += 1
        key = f"organize-{self.storage_key}-{stage or self.index}"
        validate_current()
        public = self.models.public() if callable(getattr(self.models, 'public', None)) else {}
        configuration = public_model_configuration(public.get('generation', {}))
        inputs = {'messages':messages, 'max_tokens':max_tokens,
                  'configuration':configuration, 'project_id':self.project_id}
        with self.write() as tx:
            self._guard_write(tx)
            original = tx.read(INPUTS, self.storage_key)
            if original is None:
                tx.put(INPUTS, self.storage_key, {'project_id':self.project_id, 'source':self.source},
                       expected_revision=0)
                self.commit(tx)
            elif original.payload != {'project_id':self.project_id, 'source':self.source}:
                raise ModelConfigurationError('remote_processing_target_changed')
        saved = self.records.read(STEPS, key)
        if saved:
            events = self.store.events_after(saved.payload['turn_id'])
            completed = [event for event in events if event['type'] == 'model.completed']
            if events and classify_recovery(saved.payload['turn_id'], 1, events,
                                            payload_loader=self.store.get).disposition == 'quarantine':
                raise ModelConfigurationError('model_request_failed:organize_interrupted')
            if saved.payload.get('rejected') and not completed:
                raise ModelConfigurationError('model_request_failed:organize_result_unavailable')
            if 'output' in saved.payload and completed and not saved.payload.get('rejected'):
                if saved.payload['inputs'] != inputs:
                    raise ModelConfigurationError('remote_processing_target_changed')
                validate_current()
                if self.constraint_admission is not None:
                    request = self.store.get_request(saved.payload['turn_id'])
                    self.constraint_admission.validate_current(request, self.store)
                    self.constraint_admission.validate_recorded_input(request, self.store,
                        self.constraint_admission.messages(request, self.store, messages),
                        response_model=None, max_tokens=max_tokens)
                    self.generation_requests[request['turn_id']] = deepcopy(request)
                if events[-1]['type'] != 'turn.completed':
                    class RecoveryPlanner:
                        def plan(self, *args, **kwargs):
                            raise ModelConfigurationError('model_request_failed:organize_result_unavailable')
                    runtime = ProductPolicyRuntime(planner=RecoveryPlanner(), registry=ScopedCapabilityRegistry(),
                        events=self.store, payloads=self.store, state=self.store)
                    with self.write() as tx:
                        self._guard_write(tx)
                        receipt = runtime.run_accepted_turn(saved.payload['turn_id'])
                    if receipt.status != 'completed':
                        raise ModelConfigurationError('model_request_failed:organize_turn_failed')
                return saved.payload['output'], dict(saved.payload['metadata'])
            # Once a wire was dispatched, absence of output is an unknown
            # external result. An explicit retry must not charge it again.
            if (completed or 'output' in saved.payload) and not saved.payload.get('rejected'):
                raise ModelConfigurationError('model_request_failed:organize_result_unavailable')
            if events and events[-1]['type'] not in {'turn.completed', 'turn.failed', 'turn.cancelled'}:
                raise ModelConfigurationError('model_request_failed:organize_interrupted')
        row = self.source_item()
        if row.payload.get('source_text') != self.source:
            raise ModelConfigurationError('remote_processing_target_changed')
        identity = uuid4().hex
        turn_id = 'turn-' + identity
        local = not bool(public.get('generation', {}).get('allow_remote'))
        if configuration.get('base_url'):
            local = urlsplit(configuration['base_url']).hostname in {'localhost','127.0.0.1','::1'}
        privacy_models = self.models if public else SimpleNamespace(public=lambda: {'generation':{'allow_remote':False}})
        freeze = self.source_context.freeze if self.source_context is not None else freeze_product_turn
        request = freeze('memory.organize', records=self.records, models=privacy_models,
            project_id=self.project_id, local_only=local,
            materials=[{'type':'original_item','id':self.item_id,'revision':row.revision,'project_id':self.project_id}],
            load_text=lambda material: json.dumps(messages, ensure_ascii=False),
            turn_id=turn_id, session_id='organize-'+self.item_id,
            operation_id='organize-'+identity, idempotency_key='organize-'+identity,
            created_at=datetime.now(timezone.utc).isoformat(), capabilities=[],
            budget={'max_steps':1, 'planner_timeout_ms':min(120000, int((timeout_seconds or 120)*1000))})
        if not request['privacy']['material_refs']:
            raise ModelConfigurationError('private_source_remote_blocked')

        def guard():
            validate_current()
            if self.source_context is not None:
                self.source_context.validate_request(request)
            else:
                validate_frozen_inputs(self.records, privacy_models, request)
            if self.constraint_admission is not None:
                self.constraint_admission.validate_current(request, self.store)

        with self.write() as tx:
            self._guard_write(tx)
            current = tx.read(STEPS, key)
            if (current.revision if current else 0) != (saved.revision if saved else 0):
                raise ModelConfigurationError('remote_processing_target_changed')
            admission = (self.constraint_admission.prepare(request, self.store, tx,
                producer_identity={'collection': STEPS, 'kind': 'memory.organize', 'project': self.project_id, 'key': key},
                previous_producer=current) if self.constraint_admission is not None else None)
            checkpoint = tx.put(STEPS, key, {'turn_id':turn_id, 'inputs':inputs},
                                expected_revision=current.revision if current else 0)
            if self.constraint_admission is not None:
                self.constraint_admission.stage(request, self.store, admission, tx)
            from ..kernel.aux_routing import stage_auxiliary_choice
            stage_auxiliary_choice(self.models, tx, turn_id)
            self.commit(tx)
        if self.constraint_admission is not None:
            messages = self.constraint_admission.messages(request, self.store, messages)
        if self.input_limit is not None:
            from .task_attachment_input import input_tokens
            if input_tokens(messages) > self.input_limit:
                raise ValueError('material_analysis_budget_insufficient')
        owner = self
        failures = []

        class Planner:
            def plan(self, frozen, events, capabilities, payloads, execution_control):
                try:
                    return self.execute(frozen, events, capabilities, payloads, execution_control)
                except Exception as error:
                    failures.append(error)
                    raise

            def execute(self, frozen, events, capabilities, payloads, execution_control):
                guard()
                execution_control.checkpoint()
                from ..kernel.aux_routing import auxiliary_models
                selected = auxiliary_models(owner.models, owner.store, turn_id, records=owner.records)
                selected_public = selected.public() if callable(getattr(selected, 'public', None)) else {}
                selected_configuration = public_model_configuration(selected_public.get('generation', {}))
                if owner.constraint_admission is not None:
                    owner.constraint_admission.input_budget(selected, messages, max_tokens=max_tokens)
                    owner.constraint_admission.record_input(request, owner.store,
                        model_request_id=execution_control.model_request_id, messages=messages,
                        response_model=None, max_tokens=max_tokens)
                def wire_guard():
                    guard()
                    if owner.constraint_admission is not None:
                        owner.constraint_admission.input_budget(selected, messages, max_tokens=max_tokens)
                governed = getattr(selected, 'complete_governed', None)
                if callable(governed):
                    route = {'configuration':selected_configuration, 'project_id':owner.project_id,
                             'turn_id':turn_id, 'purpose':'aux',
                             'execution_location':'local_loopback' if local else 'remote'}
                    ref = owner.store.get_or_create_immutable_payload(turn_id, 'organize-model-route-v1', route)
                    binding = {'payload_ref':ref, 'revision':_revision(route),
                        'prompt_cache_scope_identity':_revision(route), 'configuration':selected_configuration,
                        'execution_location':route['execution_location']}
                    output, metadata = governed(messages, routing_snapshot=binding,
                        execution_control=execution_control, metadata_sink=execution_control,
                        wire_attempt_sink=execution_control, max_tokens=max_tokens, validate_current=wire_guard,
                        purpose='aux', retry_policy=get('retry'))
                else:
                    # Existing injected/domain adapters remain usable, but are
                    # invoked inside a real Turn with its control and receipt.
                    options = {'max_tokens':max_tokens, 'validate_current':wire_guard}
                    if timeout_seconds is not None and 'timeout_seconds' in inspect.signature(selected.complete).parameters:
                        options['timeout_seconds'] = timeout_seconds
                    output, metadata = selected.complete(messages, **options)
                    execution_control.model_call_completed(usage=metadata.get('usage', {}))
                guard()
                execution_control.checkpoint()
                rejected = False
                if validate_output is not None:
                    try:
                        validate_output(output)
                    except (ValueError, TypeError):
                        rejected = True
                # Keep only public metadata; provider bodies/credentials never
                # enter a checkpoint or model receipt.
                safe = {name:metadata[name] for name in ('model','usage') if name in metadata}
                result_ref = None
                if owner.source_context is not None:
                    result_ref = owner.store.get_or_create_immutable_payload(turn_id, 'organize-model-result-v1',
                        {'output': output, 'metadata': safe, 'rejected': rejected})
                with owner.write() as tx:
                    owner._guard_write(tx)
                    from ..transaction_records import TransactionRecords
                    if owner.source_context is not None:
                        owner.source_context.validate_request(request, actor_reader=tx)
                    else:
                        validate_frozen_inputs(TransactionRecords(tx), privacy_models, request, source_owner=owner.records)
                    if owner.constraint_admission is not None:
                        owner.constraint_admission.validate_commit(request, owner.store, tx)
                    current = tx.read(STEPS, key)
                    if current.revision != checkpoint.revision or current.payload['turn_id'] != turn_id:
                        raise ModelConfigurationError('remote_processing_target_changed')
                    tx.put(STEPS, key, {**current.payload, 'output':output, 'metadata':safe, 'rejected':rejected},
                           expected_revision=current.revision)
                    owner.commit(tx)
                return {'type':'complete', 'summary':'Source organization model output saved',
                    **({'payload_ref': result_ref} if result_ref is not None else {})}

        runtime = ProductPolicyRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
            events=self.store, payloads=self.store, state=self.store)
        with self.native_scope():
            if self.source_context is not None:
                self.constraint_admission.validate_current(request, self.store)
            # 共享 store 从原 AUXPREP 复验完整封装后独占注册，个人路径沿原接纳参数。
            supplied = {'admission_bundle': admission} if self.source_context is None and admission is not None else {}
            accepted = runtime.accept_turn(request, **supplied)
        receipt = accepted if accepted.replayed else runtime.run_accepted_turn(accepted.turn_id)
        if receipt.status != 'completed':
            if failures and isinstance(failures[0], ModelConfigurationError):
                raise failures[0]
            raise ModelConfigurationError('model_request_failed:organize_turn_failed')
        guard()
        completed = self.records.read(STEPS, key).payload
        if self.constraint_admission is not None and not completed.get('rejected'):
            self.generation_requests[turn_id] = deepcopy(request)
        return completed['output'], dict(completed['metadata'])
