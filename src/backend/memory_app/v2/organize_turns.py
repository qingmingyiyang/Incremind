"""Submit source organization to the existing durable Turn runtime.

Domain parsers still own chunk/evidence validation. Checkpoints retain completed
model outputs separately from metadata receipts; unknown dispatches never retry.
"""
from datetime import datetime, timezone
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
from ..turn_routing import CONFIGURATION_FIELDS, _revision
from .turn_requests import freeze_product_turn, validate_frozen_inputs
from .policies import get, override
from .policies.pipelines import interfaces_for_turn, versions_for_turn

STEPS = "workspace_organize_steps"
INPUTS = "workspace_organize_inputs"


def _policy_bound_complete(complete):
    @wraps(complete)
    def bound(self, messages, *, max_tokens, validate_current, timeout_seconds=None,
              stage=None, validate_output=None):
        key = f"organize-{self.item_id}-{stage or self.index + 1}"
        saved = self.records.read(STEPS, key)
        frozen = None
        if saved is not None and not saved.payload.get('rejected'):
            source = self.records.read(INPUTS, self.item_id)
            row = self.records.read('workspace_items', self.item_id)
            request = self.store.get_request(saved.payload['turn_id'])
            public = self.models.public() if callable(getattr(self.models, 'public', None)) else {}
            configuration = {field: public.get('generation', {}).get(field) for field in CONFIGURATION_FIELDS}
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
                 lease=None, run_id=None):
        self.records, self.models = records, models
        self.item_id, self.project_id, self.source = item_id, project_id, source
        self.validate_current = validate_current
        self.store = SQLiteAITurnStore(root / ".rebuild-data" / "ai-turns.sqlite3")
        self.index = 0
        self.lease, self.run_id = lease, run_id

    def _guard_write(self, tx):
        if self.lease is not None:
            self.lease._owned(tx, self.item_id, self.project_id, self.run_id)

    @_policy_bound_complete
    def complete(self, messages, *, max_tokens, validate_current, timeout_seconds=None,
                 stage=None, validate_output=None):
        self.index += 1
        key = f"organize-{self.item_id}-{stage or self.index}"
        validate_current()
        public = self.models.public() if callable(getattr(self.models, 'public', None)) else {}
        configuration = {field: public.get('generation', {}).get(field) for field in CONFIGURATION_FIELDS}
        inputs = {'messages':messages, 'max_tokens':max_tokens,
                  'configuration':configuration, 'project_id':self.project_id}
        with self.records.begin() as tx:
            self._guard_write(tx)
            original = tx.read(INPUTS, self.item_id)
            if original is None:
                tx.put(INPUTS, self.item_id, {'project_id':self.project_id, 'source':self.source},
                       expected_revision=0)
                tx.commit()
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
                if events[-1]['type'] != 'turn.completed':
                    class RecoveryPlanner:
                        def plan(self, *args, **kwargs):
                            raise ModelConfigurationError('model_request_failed:organize_result_unavailable')
                    runtime = ProductPolicyRuntime(planner=RecoveryPlanner(), registry=ScopedCapabilityRegistry(),
                        events=self.store, payloads=self.store, state=self.store)
                    with self.records.begin() as tx:
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
        row = self.records.read('workspace_items', self.item_id)
        if row.payload.get('source_text') != self.source:
            raise ModelConfigurationError('remote_processing_target_changed')
        identity = uuid4().hex
        turn_id = 'turn-' + identity
        local = not bool(public.get('generation', {}).get('allow_remote'))
        if configuration.get('base_url'):
            local = urlsplit(configuration['base_url']).hostname in {'localhost','127.0.0.1','::1'}
        privacy_models = self.models if public else SimpleNamespace(public=lambda: {'generation':{'allow_remote':False}})
        request = freeze_product_turn('memory.organize', records=self.records, models=privacy_models,
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
            validate_frozen_inputs(self.records, privacy_models, request)

        with self.records.begin() as tx:
            self._guard_write(tx)
            current = tx.read(STEPS, key)
            if (current.revision if current else 0) != (saved.revision if saved else 0):
                raise ModelConfigurationError('remote_processing_target_changed')
            checkpoint = tx.put(STEPS, key, {'turn_id':turn_id, 'inputs':inputs},
                                expected_revision=current.revision if current else 0)
            from ..kernel.aux_routing import stage_auxiliary_choice
            stage_auxiliary_choice(self.models, tx, turn_id)
            tx.commit()
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
                selected_configuration = {field: selected_public.get('generation', {}).get(field) for field in CONFIGURATION_FIELDS}
                if selected_public.get('generation', {}).get('subscription_binding'):
                    selected_configuration['subscription_binding'] = dict(selected_public['generation']['subscription_binding'])
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
                        wire_attempt_sink=execution_control, max_tokens=max_tokens, validate_current=guard,
                        purpose='aux', retry_policy=get('retry'))
                else:
                    # Existing injected/domain adapters remain usable, but are
                    # invoked inside a real Turn with its control and receipt.
                    options = {'max_tokens':max_tokens, 'validate_current':guard}
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
                with owner.records.begin() as tx:
                    owner._guard_write(tx)
                    current = tx.read(STEPS, key)
                    if current.revision != checkpoint.revision or current.payload['turn_id'] != turn_id:
                        raise ModelConfigurationError('remote_processing_target_changed')
                    tx.put(STEPS, key, {**current.payload, 'output':output, 'metadata':safe, 'rejected':rejected},
                           expected_revision=current.revision)
                    tx.commit()
                return {'type':'complete', 'summary':'Source organization model output saved'}

        runtime = ProductPolicyRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
            events=self.store, payloads=self.store, state=self.store)
        receipt = runtime.submit_turn(request)
        if receipt.status != 'completed':
            if failures and isinstance(failures[0], ModelConfigurationError):
                raise failures[0]
            raise ModelConfigurationError('model_request_failed:organize_turn_failed')
        guard()
        completed = self.records.read(STEPS, key).payload
        return completed['output'], dict(completed['metadata'])
