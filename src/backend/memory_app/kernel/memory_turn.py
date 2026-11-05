"""Durable auxiliary memory calls using the existing Turn and Effect authorities."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import inspect
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from backend.recognition import RecognitionConflict
from .policy_runtime import ProductPolicyRuntime, retry_policy_for
from core.ai_kernel import ScopedCapabilityRegistry
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.ai_kernel.recovery import classify_recovery
from core.effect_log import EffectIntent, EffectClass, EffectPurpose, EffectState, EffectLeaseFence
from ..structured_generation import generate_structured, InvalidStructuredOutput
from ..turn_routing import _revision

_OUTPUT = "memory-generation-output-v1"
_KEYS = "v2_memory_turn_keys"
_REJECTED = "memory-generation-rejected-v1"


def proposal_intent(request, key):
    return EffectIntent(session_id=request['session_id'], turn_id=request['turn_id'],
        root_id=request['operation_id'], step_key=key, kind='memory_propose',
        effect_class=EffectClass.IDEMPOTENT, purpose=EffectPurpose.AUX,
        intent_ref='crp://session/' + request['turn_id'] + '/memory-proposal/' + key,
        gate_decision_id=request['turn_id'], rev_set={'template': 1}, payload={'key': key},
        idem_key=request['turn_id'] + ':' + key)


class MemoryTurn:
    """The domain owns validation and writes; the kernel owns execution evidence."""

    @staticmethod
    def store_for(records):
        root = records.database_path.parent
        if records.database_path.name == "recognition.sqlite3":
            root = root / ".rebuild-data"
        return SQLiteAITurnStore(root / "ai-turns.sqlite3")

    def __init__(self, records, models, *, kind, project, key, materials, validate, freeze_request, validate_request, remote_allowed, purpose="generation", retry_token=None, admission=None):
        self.records, self.models = records, models
        self.purpose = purpose
        self.validate_request = validate_request
        self.validate_domain = validate
        self.admission = admission
        self.store = self.store_for(records)
        self._arguments = dict(kind=kind, project=project, materials=materials,
            validate=validate, freeze_request=freeze_request, validate_request=validate_request,
            remote_allowed=remote_allowed, purpose=purpose, admission=admission)
        config = models.public()[purpose]
        self.local = urlsplit(str(config.get("base_url", ""))).hostname in {"localhost", "127.0.0.1", "::1"}
        if not self.local and not remote_allowed(records, models, project, purpose):
            raise RecognitionConflict("memory_turn_remote_disabled")
        # This index has no execution states. The existing Turn is the authority.
        identity = {"kind": kind, "project": project, "key": key, **({"purpose": purpose} if purpose != "generation" else {})}
        old = next((r for r in records.list(_KEYS) if r.payload["identity"] == identity), None)
        if old is None:
            turn_id = "memory-" + uuid4().hex
            request = freeze_request(kind, records=records, models=models,
                project_id=project, materials=materials, load_text=lambda item: "",
                local_only=self.local, model_purpose=purpose, turn_id=turn_id, session_id="aux-" + turn_id,
                operation_id="op-" + turn_id, idempotency_key=turn_id,
                created_at=datetime.now(timezone.utc).isoformat(), capabilities=[])
            if len(request["privacy"]["material_refs"]) != len(materials):
                raise RecognitionConflict("memory_turn_excluded_material")
            with records.begin() as tx:
                old = next((r for r in tx.list(_KEYS) if r.payload["identity"] == identity), None)
                if old is None:
                    prepared = (admission.prepare(request, self.store, tx,
                        producer_identity={'collection': _KEYS, **identity}) if admission is not None else None)
                    payload = {"identity": identity, "request": request}
                    if retry_token is not None:
                        payload["retry_token"] = retry_token
                    old = tx.put(_KEYS, turn_id, payload, expected_revision=0)
                    if admission is not None:
                        admission.stage(request, self.store, prepared, tx)
                    if purpose == 'generation':
                        from .aux_routing import stage_auxiliary_choice
                        stage_auxiliary_choice(models, tx, turn_id)
                        from .provider_store_binding import stage_provider_store_binding
                        stage_provider_store_binding(models, tx, request)
                tx.commit()
        self.turn_id = old.object_id
        self.request = dict(old.payload["request"])
        self._identity = identity
        self._retry_token = old.payload.get("retry_token")
        self.validate()

    def select_insight_attempt(self, retry_token):
        """Resolve one execution's attempt while retaining immutable predecessors."""
        if self._identity["kind"] != "memory.propose_insights":
            raise ValueError("explicit retry is only supported for insight generation")
        if retry_token is not None and (not isinstance(retry_token, str) or not retry_token):
            raise ValueError("invalid insight retry token")
        current, base_key = self, self._identity["key"]
        while True:
            current.validate()
            if (current.store.get_immutable_payload(current.turn_id, _OUTPUT)
                    or retry_token is not None and current._retry_token == retry_token):
                return current
            key = base_key + ":retry-of:" + current.turn_id
            identity = {**self._identity, "key": key}
            successor = next((row for row in self.records.list(_KEYS)
                              if row.payload["identity"] == identity), None)
            if successor is not None:
                current = self._new_attempt(key)
                continue
            if retry_token is None or not current._known_failed_attempt():
                if retry_token is not None or current._retry_token is not None:
                    raise RecognitionConflict("memory_retry_attempt_owned_by_another_execution")
                return current
            # Constructor rechecks identity inside the existing write transaction:
            # concurrent callers observing this predecessor select one successor.
            selected = self._new_attempt(key, retry_token=retry_token)
            if selected._retry_token != retry_token:
                raise RecognitionConflict("memory_retry_attempt_owned_by_another_execution")
            return selected

    def _new_attempt(self, key, *, retry_token=None):
        return MemoryTurn(self.records, self.models, key=key, retry_token=retry_token, **self._arguments)

    def _known_failed_attempt(self):
        events = tuple(self.store.events_after(self.turn_id))
        if not events or events[-1]["type"] != "turn.failed":
            return False
        if classify_recovery(self.turn_id, 1, events, payload_loader=self.store.get).disposition == "quarantine":
            return False
        dispatched = [event for event in events if event["type"] == "model.attempt.dispatched"]
        if dispatched:
            terminals = [event for event in events if event["type"] == "model.attempt.terminal"]
            return len(terminals) == len(dispatched) and all(
                self.store.get(event["data"]["receipt_ref"])["status"] == "succeeded"
                for event in terminals)
        # Legacy adapters lack wire receipts. A rejected returned value is known;
        # a remote transport exception provides no such evidence.
        original_route = self.store.get_immutable_payload(self.turn_id, "memory-model-route-v1")
        if (not any(event['type'].startswith('model.attempt.') for event in events)
                and self.store.get_immutable_payload(self.turn_id, _OUTPUT) is None):
            from .aux_routing import CHOICE_KIND
            choice = self.store.get_immutable_payload(self.turn_id, CHOICE_KIND)
            binding = choice[1] if choice is not None else {}
            configuration = binding.get('parent', {}).get('configuration', {})
            routed = [event for event in events if event['type'] == 'model.routed']
            failed = [event for event in events if event['type'] == 'model.failed']
            requested = [event for event in events if event['type'] == 'model.requested']
            base_url = configuration.get('base_url')
            remote = urlsplit(base_url) if isinstance(base_url, str) else None
            if (choice is not None and 'model' in binding and binding['model'] is None
                    and configuration.get('purpose') == 'generation'
                    and isinstance(configuration.get('model'), str) and configuration['model']
                    and type(configuration.get('revision')) is int and configuration['revision'] > 0
                    and configuration.get('configured') is False and configuration.get('has_api_key') is False
                    and not configuration.get('subscription_binding')
                    and configuration.get('provider') == 'openai' and configuration.get('allow_remote') is True
                    and remote is not None and remote.scheme == 'https' and remote.hostname
                    and remote.hostname not in {'localhost', '127.0.0.1', '::1'}
                    and original_route is not None and len(requested) == len(routed) == len(failed) == 1
                    and original_route[1] == {key: configuration.get(key)
                        for key in ('provider', 'model', 'base_url', 'revision', 'allow_remote')}
                    and routed[0]['data'].get('payload_ref') == original_route[0]
                    and original_route[0] in failed[0]['data'].get('evidence_refs', ())):
                request_id = requested[0]['correlation'].get('model_request_id')
                receipt_ref = failed[0]['data'].get('receipt_ref')
                if (isinstance(request_id, str) and request_id and receipt_ref
                        and routed[0]['correlation'].get('model_request_id') == request_id
                        and failed[0]['correlation'].get('model_request_id') == request_id):
                    receipt = self.store.get(receipt_ref)
                    # 物理预留与派发事件同事务提交；完整配置失败证明才能开启后继提炼。
                    if (isinstance(receipt, dict) and receipt.get('turn_id') == self.turn_id
                            and receipt.get('model_request_id') == request_id and receipt.get('status') == 'failed'
                            and receipt.get('input_recorded') is False and receipt.get('output_recorded') is False):
                        return True
        original_local = original_route is not None and urlsplit(
            str(original_route[1].get("base_url", ""))).hostname in {"localhost", "127.0.0.1", "::1"}
        return bool(self.store.get_immutable_payload(self.turn_id, _REJECTED)) or (
            original_local and any(event["type"] == "model.failed" for event in events))

    def validate(self):
        self.validate_domain()
        self.validate_request(self.records, self.models, self.request, purpose=self.purpose)
        if self.admission is not None:
            self.admission.validate_current(self.request, self.store)
        current_local = urlsplit(str(self.models.public()[self.purpose].get("base_url", ""))).hostname in {"localhost", "127.0.0.1", "::1"}
        if current_local != self.local or (not current_local and not self.request["privacy"]["allow_remote"]):
            raise RecognitionConflict("memory_turn_remote_disabled")

    def input_messages(self, messages):
        return self.admission.messages(self.request, self.store, messages) if self.admission is not None else messages

    def base_request(self):
        return self.admission.base_request(self.request, self.store) if self.admission is not None else deepcopy(self.request)

    def validate_commit(self, reader):
        from ..transaction_records import TransactionRecords
        options = ({'source_owner': self.records}
            if 'source_owner' in inspect.signature(self.validate_request).parameters else {})
        self.validate_request(TransactionRecords(reader), self.models, self.request, purpose=self.purpose, **options)
        if self.admission is not None:
            self.admission.validate_commit(self.request, self.store, reader)

    def generate(self, messages, *, response_model, max_tokens, invoke=None, governed=False, input_observer=None):
        if input_observer is not None and (not callable(input_observer) or invoke is not None):
            raise ValueError('memory_input_observer_invalid')
        if type(governed) is not bool or governed and invoke is not None:
            raise ValueError('memory_governed_call_invalid')
        self.validate()
        messages = self.input_messages(messages)
        cached = self.store.get_immutable_payload(self.turn_id, _OUTPUT)
        if cached:
            if self.admission is not None:
                self.admission.validate_recorded_input(self.request, self.store, messages,
                    response_model=None if governed else response_model, max_tokens=max_tokens)
            output = cached[1]
            return response_model.model_validate(output["output"]), dict(output["metadata"])
        owner = self
        from .aux_routing import auxiliary_models
        failure = []

        class Planner:
            def plan(self, request, events, capabilities, payloads, execution_control):
                control = execution_control
                owner.validate()
                selected = auxiliary_models(owner.models, owner.store, owner.turn_id, records=owner.records) if owner.purpose == 'generation' and not governed else owner.models
                public = selected.public()[owner.purpose]
                if owner.admission is not None:
                    try:
                        owner.admission.input_budget(selected, messages, max_tokens=max_tokens,
                            response_model=None if governed else response_model)
                    except Exception as error:
                        failure.append(error)
                        raise
                # Only public route fields may enter kernel payloads.
                route = {k: public.get(k) for k in ("provider", "model", "base_url", "revision", "allow_remote")}
                if public.get('server_model_binding') is not None:
                    route['server_model_binding'] = dict(public['server_model_binding'])
                if owner.purpose != 'generation':
                    # 辅助 Turn 的 kind 可复用，实际用途必须随原路由冻结供回执投影。
                    route['purpose'] = owner.purpose
                route_ref = owner.store.get_or_create_immutable_payload(owner.turn_id, "memory-model-route-v1", route)
                call = None
                if governed:
                    from ..model_config import ModelConfiguration
                    from ..model_binding import public_model_configuration
                    if owner.purpose != 'generation' or not isinstance(selected, ModelConfiguration):
                        raise ValueError('memory_governed_owner_invalid')
                    # 由原 Planner 冻结完整公开路由，governed 调用独占路由和开始事件。
                    payload = {'turn_id': owner.turn_id, 'project_id': request['scope']['project_id'],
                        'configuration': public_model_configuration(public), 'purpose': 'primary',
                        'execution_location': 'local_loopback' if owner.local else 'remote'}
                    ref = owner.store.get_or_create_immutable_payload(owner.turn_id, 'recognition-model-routing-snapshot-v1', payload)
                    call = ({'payload_ref': ref, 'revision': _revision(payload),
                        'prompt_cache_scope_identity': _revision(payload), 'configuration': payload['configuration'],
                        'execution_location': payload['execution_location']}, None)
                elif owner.purpose == 'generation' and invoke is None:
                    from .provider_store_binding import memory_provider_store_call
                    call = memory_provider_store_call(owner.models, selected, owner.records, owner.store,
                        request, route_ref, route)
                if call is None:
                    control.model_call_routed(snapshot_ref=route_ref, snapshot_revision=_revision(route),
                        prompt_cache_scope_identity=_revision({"turn_id": owner.turn_id, "route": route}),
                        provider=str(route.get("provider") or "domain-adapter"), model=str(route.get("model") or "unreported"),
                        execution_location="local_loopback" if owner.local else "remote", purpose="aux")
                    control.model_call_started(provider=str(route.get("provider") or "domain-adapter"),
                        model=str(route.get("model") or "unreported"))
                method = getattr(selected, "complete_structured", None) or getattr(selected, "complete", None)
                observable = method is not None and "wire_attempt_sink" in inspect.signature(method).parameters
                def validate():
                    control.checkpoint()
                    owner.validate()
                    if owner.admission is not None:
                        owner.admission.input_budget(selected, messages, max_tokens=max_tokens,
                            response_model=None if governed else response_model)
                    if 'server_model_binding' in route:
                        selected.server_model_access.validate_binding(route['server_model_binding'])
                try:
                    if owner.admission is not None:
                        owner.admission.record_input(request, owner.store, model_request_id=control.model_request_id,
                            messages=messages, response_model=None if governed else response_model, max_tokens=max_tokens)
                    if input_observer is not None:
                        validate()
                        input_observer(owner=owner, request=request, model_request_id=control.model_request_id,
                            messages=messages, response_schema=None if governed else response_model.model_json_schema(),
                            result_schema=response_model.model_json_schema(), governed=governed)
                    if invoke is not None:
                        output, metadata = invoke(control, validate)
                    elif call is not None:
                        output, metadata = selected.complete_governed(messages, routing_snapshot=call[0],
                            execution_control=control, metadata_sink=control, wire_attempt_sink=control,
                            response_model=None if governed else response_model, max_tokens=max_tokens, validate_current=validate,
                            purpose='primary' if governed else 'aux', retry_policy=retry_policy_for(request),
                            timeout_seconds=control.remaining_timeout_ms / 1000,
                            provider_store_activation=call[1])
                        if governed:
                            output = response_model.model_validate(output)
                    else:
                        retry = ({'retry_policy': retry_policy_for(request),
                                  'timeout_seconds': control.remaining_timeout_ms / 1000}
                                 if observable and 'retry_policy' in inspect.signature(method).parameters else {})
                        output, metadata = generate_structured(selected, messages,
                            response_model=response_model, max_tokens=max_tokens, validate_current=validate,
                            wire_attempt_sink=control if observable else None, **retry)
                    validate()
                    if not isinstance(metadata, dict):
                        raise ValueError("memory_generation_metadata_invalid")
                    safe = {k: metadata[k] for k in ("model", "configuration_revision", "usage") if k in metadata}
                    safe.update(generation_id=str(UUID(owner.turn_id.removeprefix("memory-"))), completed_at=datetime.now(timezone.utc).isoformat())
                    ref = owner.store.get_or_create_immutable_payload(owner.turn_id, _OUTPUT,
                        {"output": output.model_dump(mode="json"), "metadata": safe})
                    if call is None:
                        control.model_call_completed(usage=metadata.get("usage") or {})
                    return {"type": "complete", "summary": "Memory generation completed", "payload_ref": ref}
                except Exception as error:
                    failure.append(error)
                    if isinstance(error, InvalidStructuredOutput):
                        owner.store.get_or_create_immutable_payload(owner.turn_id, _REJECTED,
                            {"reason": "invalid_structured_output"})
                    if call is None:
                        control.model_call_failed()
                    raise

        runtime = ProductPolicyRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
            events=self.store, payloads=self.store, state=self.store)
        admission = self.admission.admission(self.request, self.store) if self.admission is not None else None
        accepted = runtime.accept_turn(self.request, **({'admission_bundle': admission} if admission is not None else {}))
        if accepted.replayed:
            # A request with no durable result may have reached the provider.
            # Never reinterpret an unknown dispatch as permission to call again.
            raise RecognitionConflict("memory_turn_result_unavailable")
        from .provider_store_binding import validate_provider_store_binding
        if self.purpose == 'generation' and invoke is None and validate_provider_store_binding(
                self.models, self.records, self.request):
            self.validate()
            now = datetime.now(timezone.utc)
            lease = runtime.try_acquire_run_lease(self.turn_id, 'memory-' + uuid4().hex, now=now,
                stale_after=now + timedelta(seconds=retry_policy_for(self.request)({'kind': 'limits'})['total_timeout']))
            if lease is None:
                raise RecognitionConflict('memory_turn_result_unavailable')
            result = runtime.run_accepted_turn(self.turn_id, run_lease=lease)
            if result.status in {'completed', 'failed', 'cancelled'}:
                events = tuple(self.store.events_after(self.turn_id))
                dispatched = [event for event in events if event['type'] == 'model.attempt.dispatched']
                terminals = [event for event in events if event['type'] == 'model.attempt.terminal']
                if len(dispatched) == len(terminals) and all(
                        self.store.effect_runner.log.get(self.store.get(event['data']['payload_ref'])['attempt_id']).state
                        in {EffectState.SETTLED_OK, EffectState.SETTLED_ERR} for event in dispatched):
                    runtime.release_strict_run_lease(lease)
        else:
            runtime.run_accepted_turn(self.turn_id)
        if failure:
            raise failure[0]
        cached = self.store.get_immutable_payload(self.turn_id, _OUTPUT)
        if cached is None:
            raise RecognitionConflict("memory_turn_result_unavailable")
        self.validate()
        return response_model.model_validate(cached[1]["output"]), dict(cached[1]["metadata"])

    def propose(self, *, key, write, existing):
        """Run an idempotent domain proposal under the kernel memory_propose effect."""
        self.validate()
        runner = self.store.effect_runner
        intent = proposal_intent(self.request, key)
        value = []
        def handler(effect):
            self.validate()
            result = existing()
            if result is None:
                result = write()
            value.append(result)
            return self.store.get_or_create_immutable_payload(self.turn_id, "memory-proposal-" + key,
                {"committed": True, "key": key})
        now = int(time.time())
        planned, _ = runner.log.plan(intent, now=now)
        if planned.state == EffectState.INFLIGHT and planned.lease_expires_at <= now:
            # Proposal writes carry domain CAS/idempotency. An expired worker can
            # be fenced and the same handler can safely repair its receipt.
            runner.log.transition(planned.operation_id, expected=EffectState.INFLIGHT,
                target=EffectState.PLANNED, now=now, fence=EffectLeaseFence.from_effect(planned),
                fence_must_be_expired=True, probe_ref="crp://session/" + self.turn_id + "/proposal-recovery")
        outcome = runner.execute(intent, handler, now=now)
        if outcome.state == EffectState.SETTLED_OK:
            return value[0] if value else existing()
        # A committed domain object is authoritative across a receipt-write gap.
        result = existing()
        if result is not None:
            return result
        raise RecognitionConflict("memory_proposal_pending_recovery")


def embedding_request(records, models, project, materials, key, validate, transport, request, *, turn_factory,
                      purpose='embedding', prepare_original_wire=None):
    """用同一辅助 Turn 记录向量或重排的真实调用与费用。"""
    if purpose not in {'embedding', 'rerank'}:
        raise ValueError('invalid_retrieval_wire_purpose')
    from pydantic import RootModel
    turn = turn_factory(records, models, kind="memory.link_suggest", project=project,
        key=key, materials=materials, validate=validate, purpose=purpose)
    wire_guard = None
    original_payload = None
    if prepare_original_wire is not None:
        original_payload = deepcopy(request['payload'])
        passed_payload = deepcopy(original_payload)
        wire_guard = prepare_original_wire(turn, passed_payload)
        if not callable(wire_guard) or passed_payload != original_payload:
            raise RecognitionConflict('memory_original_wire_preparation_changed')
    def validate_original_wire():
        if wire_guard is not None:
            if request['payload'] != original_payload:
                raise RecognitionConflict('memory_original_wire_payload_changed')
            wire_guard()
    validate_original_wire()
    def invoke(control, current):
        current()
        validate_original_wire()
        wrap = getattr(models, 'price_wire_sink', None)
        sink = (wrap(control, purpose=purpose, configuration=models.public()[purpose], wire_request=request)
                if callable(wrap) else control)
        attempt = sink.begin_model_wire_attempt()
        def wire():
            try:
                validate_original_wire()
                result = transport.post_json(**request)
                observe = getattr(attempt, 'observe_metering', None)
                if callable(observe):
                    observe(result)
                usage = result.get("usage", {})
                incoming = usage.get("input_tokens", usage.get("prompt_tokens"))
                if purpose == 'rerank' and incoming is None:
                    incoming = result.get('meta', {}).get('billed_units', {}).get('input_tokens')
                normalized = ({"input_tokens": incoming, "output_tokens": 0, "total_tokens": incoming}
                    if type(incoming) is int and incoming >= 0 else {})
                attempt.succeeded(usage=normalized, cache_observation=None)
                return result
            except Exception:
                attempt.failed_transport(error_code="configured_model_request_failed")
                raise
        result = attempt.invoke_wire(wire)
        current()
        validate_original_wire()
        return RootModel[dict](result), {"usage": result.get("usage", {})}
    from ..model_costs import SharedModelMeteringError
    from ..model_config import _model_request_failure
    try:
        output, _ = turn.generate([], response_model=RootModel[dict], max_tokens=0, invoke=invoke)
    except SharedModelMeteringError as error:
        raise _model_request_failure(error) from None
    validate_original_wire()
    return output.root
