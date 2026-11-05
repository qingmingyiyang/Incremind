"""Thin workbench routing orchestration over the durable auxiliary Turn kernel."""
from __future__ import annotations

from .policies import get, override
from .policies.route import v1 as legacy_route
from .policies.pipelines import interfaces_for_turn, versions_for_turn
from datetime import datetime, timedelta, timezone
from collections import Counter
import json
import logging
import re
from time import monotonic
from typing import Literal
import unicodedata
from contextlib import nullcontext, ExitStack
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_serializer, model_validator

from ..kernel.policy_runtime import ProductPolicyRuntime
from core.ai_kernel import ScopedCapabilityRegistry
from core.storage_provider.connection_scope import with_connection_scope
from ..kernel.memory_turn import MemoryTurn
from ..turn_routing import _revision
from ..model_binding import public_model_configuration
from .intent import parse_scope_tag, route_intent, scope_tag_match
from .privacy import egress_allowed
from .projects import DEFAULT_NAME, display_name, project_by_tag
from .turn_requests import freeze_product_turn, validate_frozen_inputs

_KEYS = "v2_route_turn_keys"
_OUTPUT = "workbench-route-output-v1"
_LOG = logging.getLogger(__name__)
# 连接词，以及"记一下""问下""帮我"这类口头指令词：模型拆分时可以不放进片段。
_CONNECTORS = re.compile(r"(?:然后|并且|以及|同时|另外|接着|顺便|再|并|还|和|记一下|记下来|记下|记住|帮我记|记录一下|备忘|问一下|问下|请问|想问|我想问|问|帮我|麻烦|请|一下)+")
_PROMPT = legacy_route.prompt


class RoutePart(BaseModel):
    # DeepSeek's JSON mode adds its own "index"/"content" keys to each part; the
    # spans are checked literally against the input below, so extras are dropped.
    model_config = ConfigDict(extra="ignore", strict=True)
    intent: Literal["remember", "ask", "do", "inspiration"]
    span: str
    instruction: str | None = Field(default=None, max_length=200)
    depends_on: list[StrictInt] = Field(default_factory=list)
    situation: str | None = Field(default=None, max_length=60)

    @model_serializer(mode='wrap')
    def serialize(self, handler):
        value = handler(self)
        if self.situation is None:
            value.pop('situation', None)
        return value


class RouteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    parts: list[RoutePart] = Field(min_length=1, max_length=3)

    @model_validator(mode='wrap')
    @classmethod
    def report_schema_failure(cls, value, handler):
        try:
            return handler(value)
        except ValidationError as error:
            # 只记录固定字段位置及错误分类，正文和未知额外字段名均不进入日志。
            fields = {'parts', 'intent', 'span', 'instruction', 'depends_on', 'situation'}
            failures = [{'location': [part if type(part) is int or part in fields else 'extra'
                                      for part in row['loc']], 'type': row['type']}
                        for row in error.errors(include_input=False, include_context=False, include_url=False)]
            _LOG.warning('workbench_route_schema_invalid: %s', failures)
            raise


class DataRoutePart(RoutePart):
    data_operation: Literal['lexicon.read', 'lexicon.maintain'] | None = Field(exclude=True)


class DataRouteOutput(RouteOutput):
    parts: list[DataRoutePart] = Field(min_length=1, max_length=3)


class RoutePlan(RouteOutput):
    parts: list[DataRoutePart | RoutePart] = Field(min_length=1, max_length=3)
    mode: Literal["model", "rules", "manual"]
    usage: dict | None = None
    egress_receipt_id: str | None = None
    turn_id: str | None = None
    reason: str | None = None


def internal_route_dump(output):
    value = output.model_dump()
    for saved, part in zip(value['parts'], output.parts):
        operation = getattr(part, 'data_operation', None)
        if isinstance(part, DataRoutePart):
            saved['data_operation'] = operation
    return value


def resolve_scope_project(records, text, project_id):
    """Resolve exactly the existing workbench scope names; ambiguity fails closed."""
    tag, _, _ = parse_scope_tag(text)
    if tag is None:
        return project_id
    projects = {row.object_id: display_name(row.object_id, row.payload["name"]) for row in records.list("v2_projects")}
    for identity, name in (("inbox", "收件箱"), ("me", "我"), ("default", DEFAULT_NAME)):
        projects.setdefault(identity, name)
    if tag in projects:
        return tag
    if (legacy := project_by_tag(projects, tag)) is not None:
        return legacy
    matches = [identity for identity, name in projects.items() if name == tag]
    if len(matches) != 1:
        raise ValueError("ambiguous_or_unknown_project_tag")
    return matches[0]


def _gap_allowed(text):
    words = re.split(r"\s+", "".join(" " if unicodedata.category(char).startswith("P") else char for char in text))
    return all(not word or _CONNECTORS.fullmatch(word) for word in words)


def validate_route_output(text, value, *, has_files=False, deadline=None, manual_intent=None):
    """Validate literal spans, coverage and the complete dependency graph."""
    output = (DataRouteOutput if getattr(get('route'), 'data_operations', False) else RouteOutput).model_validate(value)
    parts = output.parts
    policy = get('route')
    detached = [part.model_dump() for part in parts]
    if manual_intent is not None:
        validate_manual = getattr(policy, 'validate_manual', None)
        if manual_intent != 'do' or validate_manual is None:
            raise ValueError('invalid_manual_route_intent')
        validate_manual(detached, has_files)
    shape = getattr(policy, 'validate_shape', None)
    if shape is not None:
        shape(detached, has_files)
    if sum(part.intent == "do" for part in parts) > 1:
        raise ValueError("multiple_do_parts")
    if len({part.intent for part in parts}) != len(parts):
        # 一段笔记不能被拆成几份记住；同一意图出现两次就整体退回规则判断。
        raise ValueError("same_intent_parts")
    for index, part in enumerate(parts):
        if getattr(part, 'data_operation', None) is not None and part.intent != 'do':
            raise ValueError('data_operation_requires_do')
        if not part.span and not getattr(policy, 'allow_empty', legacy_route.allow_empty)(text, detached, index, has_files):
            raise ValueError("empty_span")
        if part.instruction is not None and part.intent not in {"ask", "do"}:
            raise ValueError("instruction_not_allowed")
        if part.situation is not None and part.intent not in {"ask", "do"}:
            raise ValueError("situation_not_allowed")
        if len(set(part.depends_on)) != len(part.depends_on):
            raise ValueError("duplicate_dependency")
        for dependency in part.depends_on:
            if dependency < 0 or dependency >= len(parts) or dependency == index:
                raise ValueError("invalid_dependency")
            source = parts[dependency]
            if (source.intent, part.intent) not in {("remember", "ask"), ("remember", "do"), ("ask", "do")}:
                raise ValueError("dependency_direction")
            if source.depends_on:
                raise ValueError("dependency_depth")
    scope = scope_tag_match(text)
    # A cheap necessary condition prevents repeated short spans from creating
    # a cubic search over long omitted substantive input.
    content = text[:scope[0]] + text[scope[1]:] if scope else text
    required = Counter(char for char in _CONNECTORS.sub("", content)
        if not char.isspace() and not unicodedata.category(char).startswith("P"))
    available = Counter("".join(part.span for part in parts))
    if required - available:
        raise ValueError("uncovered_substantive_text")
    attempts = 0
    # Anchor the least ambiguous spans first without changing model indices.
    span_order = sorted(range(len(parts)), key=lambda index: -len(parts[index].span))

    def assign(index, intervals):
        nonlocal attempts
        if index == len(parts):
            covered = list(intervals) + ([(scope[0], scope[1])] if scope else [])
            cursor = 0
            for start, end in sorted(covered):
                if start > cursor and not _gap_allowed(text[cursor:start]):
                    return False
                cursor = max(cursor, end)
            return _gap_allowed(text[cursor:])
        span = parts[span_order[index]].span
        if not span:
            return assign(index + 1, intervals)
        start = text.find(span)
        while start >= 0:
            attempts += 1
            if attempts > 4096 or (deadline is not None and monotonic() >= deadline):
                raise ValueError("span_validation_budget_exceeded")
            end = start + len(span)
            overlaps = [right for left, right in intervals if end > left and start < right]
            if not overlaps and assign(index + 1, intervals + [(start, end)]):
                return True
            # Every start before the collided interval's end also overlaps it.
            start = text.find(span, max(overlaps) if overlaps else start + 1)
        return False

    if not assign(0, [(scope[0], scope[1])] if scope and scope[4] else []):
        raise ValueError("span_not_literal_overlapping_or_incomplete")
    return output


def is_fast_route(text, has_files=False):
    """Only explicit single-intent rules qualify; default remember is uncertain."""
    _, _, body = parse_scope_tag(text)
    if not body:
        return has_files
    if has_files:
        return False
    if re.fullmatch(r"https?://\S+", body, flags=re.IGNORECASE):
        return True
    single = not re.search(r"[。！？!?；;\n\r]", body.rstrip("。！？!?；; "))
    # A remember word next to a question ("记一下……另外问下……") is mixed input for the model.
    single = single and not re.search(r"然后|并且|同时|顺便|再帮|另外|还有|此外|顺带|记一下|记下|记住|帮我记|备忘", body)
    if body.startswith("灵感") and single:
        return True
    return len(body) <= 30 and single and route_intent(text, False) in {"ask", "do"}


def _fallback(text, has_files, *, reason, turn_id=None, usage=None, egress_receipt_id=None, intent=None):
    _, _, body = parse_scope_tag(text)
    if body not in text:
        body = text
    return RoutePlan(parts=[RoutePart(intent=intent or route_intent(text, has_files), span=body)],
        mode="manual" if intent else "rules", reason=reason, turn_id=turn_id,
        usage=usage, egress_receipt_id=egress_receipt_id)


class RouteService:
    """Callers pass the current project; an explicit scope tag is resolved again.

    request_key identifies a submission, not its content. Replays reuse frozen
    input and the existing Turn; an unknown previous dispatch is never resent.
    route_model is the same governed model path without the fast-path policy,
    for model evaluation and explicit model-routing callers.
    """
    def __init__(self, records, models, *, examples=None, shared_source=None):
        self.records, self.models = records, models
        self.store = MemoryTurn.store_for(records)
        self.examples = examples
        self.shared_source = shared_source
        if shared_source is not None:
            from .shared_route import SharedRouteSource
            if (not isinstance(shared_source, SharedRouteSource)
                    or shared_source.records is not records or shared_source.models is not models):
                raise ValueError('shared_route_owner_changed')

    def _project(self, text, project):
        return (self.shared_source.project(text, project) if self.shared_source is not None
            else resolve_scope_project(self.records, text, project))

    def _identity(self, project, request_key):
        return (self.shared_source.identity(project, request_key) if self.shared_source is not None
            else {'project_id': project, 'request_key': request_key})

    def _allowed(self, project):
        return (self.shared_source.allowed() if self.shared_source is not None
            else egress_allowed(self.records, self.models, project, 'generation'))

    def route(self, text, *, project_id, request_key, has_files=False, intent=None):
        self._validate_input(text, project_id, request_key, has_files)
        old = None
        try:
            scoped_project = self._project(text, project_id)
        except ValueError:
            pass  # The existing entry owns the scope-unavailable fallback.
        else:
            identity = self._identity(scoped_project, request_key)
            inputs = {'original_text': text, 'project_id': scoped_project, 'examples': [], 'has_files': has_files}
            if intent == 'do' and getattr(get('route'), 'classify_manual_do', False):
                inputs['manual_intent'] = 'do'
            old = self.records.read(_KEYS, _revision(identity))
            if old is not None:
                inputs['examples'] = old.payload['inputs'].get('examples', [])
                if 'source_origin' in old.payload['inputs']:
                    inputs['source_origin'] = old.payload['inputs']['source_origin']
            if old is not None and (old.payload['identity'] != identity or old.payload['inputs'] != inputs):
                old = None  # Leave the existing entry's identity-conflict behavior intact.
        if old is None:
            selections = versions_for_turn('workbench.route')
        else:
            selections = old.payload['request'].get('policy_versions')
            if selections is None:
                selections = {name: '@1' for name in interfaces_for_turn('workbench.route')}
        with override(**selections):
            return get('route')(self._route, text, project_id=project_id, request_key=request_key,
                                has_files=has_files, intent=intent)

    def _route(self, text, *, project_id, request_key, has_files=False, intent=None):
        self._validate_input(text, project_id, request_key, has_files)
        if intent not in {None, "auto"}:
            if intent == 'do' and getattr(get('route'), 'classify_manual_do', False):
                return self.route_model(text, project_id=project_id, request_key=request_key,
                    has_files=has_files, manual_intent='do')
            return _fallback(text, has_files, reason="manual", intent=intent)
        # 完整词表指令固定为单一干活，保留原文；这里只路由，不保存词表。
        if not has_files and get('agent_data', version='@1').parse_instruction(parse_scope_tag(text)[2]) is not None:
            plan = _fallback(text, False, reason='explicit_lexicon_instruction', intent='do')
            if getattr(get('route'), 'data_operations', False):
                plan.parts[0] = DataRoutePart(**plan.parts[0].model_dump(), data_operation='lexicon.maintain')
            return plan
        requires_model = getattr(get('route'), 'requires_model_routing', None)
        if is_fast_route(text, has_files) and not (requires_model and requires_model(parse_scope_tag(text)[2])):
            if getattr(get('route'), 'corrected_examples_route', False):
                if self.examples is None:
                    raise ValueError('route_example_owner_unavailable')
                scoped_project = self._project(text, project_id)
                previous = self.records.read(_KEYS, _revision(self._identity(scoped_project, request_key)))
                if previous is not None or self._allowed(scoped_project) and any(
                        example['corrected'] for example in self.examples.similar(scoped_project, text)):
                    return self.route_model(text, project_id=project_id, request_key=request_key, has_files=has_files)
            return _fallback(text, has_files, reason="fast_path")
        return self.route_model(text, project_id=project_id, request_key=request_key, has_files=has_files)

    @staticmethod
    def _validate_input(text, project_id, request_key, has_files):
        if not isinstance(text, str) or len(text) > 60000 or type(has_files) is not bool:
            raise ValueError("invalid_route_input")
        if not isinstance(project_id, str) or not project_id or not isinstance(request_key, str) or not request_key:
            raise ValueError("invalid_route_identity")

    def _receipt(self, turn_id):
        for event in reversed(tuple(self.store.events_after(turn_id))):
            ref = event.get("data", {}).get("receipt_ref")
            if event["type"].startswith("model.") and ref:
                receipt = self.store.get(ref)
                return {"usage": receipt.get("usage"), "egress_receipt_id": receipt["receipt_id"]}
        return {}

    @with_connection_scope
    def route_model(self, text, *, project_id, request_key, has_files=False, manual_intent=None):
        self._validate_input(text, project_id, request_key, has_files)
        deadline = monotonic() + 15
        try:
            project_id = self._project(text, project_id)
        except ValueError:
            return _fallback(text, has_files, reason="scope_unavailable", intent=manual_intent)
        if not self._allowed(project_id):
            return _fallback(text, has_files, reason="remote_disabled", intent=manual_intent)
        inputs = {"original_text": text, "project_id": project_id, "examples": [], "has_files": has_files}
        if manual_intent is not None:
            if manual_intent != 'do':
                raise ValueError('invalid_manual_route_intent')
            inputs['manual_intent'] = manual_intent
        public = self.models.public()["generation"]
        configuration = public_model_configuration(public)
        identity = self._identity(project_id, request_key)
        record_key = _revision(identity)
        old = self.records.read(_KEYS, record_key)
        if getattr(get('route'), 'route_examples', False) and self.examples is None:
            raise ValueError('route_example_owner_unavailable')
        if getattr(get('route'), 'route_examples', False) and self.examples is not None:
            inputs['source_origin'] = (old.payload['inputs']['source_origin'] if old is not None
                else self.examples.current(project_id))
            if self.examples.current(project_id) != inputs['source_origin']:
                raise ValueError('route_example_current_source_changed')
            inputs['examples'] = (old.payload['inputs']['examples'] if old is not None
                else self.examples.similar(project_id, text))
            self.examples.validate_frozen(inputs['examples'])
        if old is not None and (old.payload["identity"] != identity or old.payload["inputs"] != inputs):
            raise ValueError("route_identity_conflict")
        if old is None:
            try:
                turn_id = "route-" + uuid4().hex
                text_input = json.dumps(inputs, ensure_ascii=False, sort_keys=True)
                identities = {'turn_id': turn_id, 'session_id': 'aux-' + turn_id, 'operation_id': 'op-' + turn_id,
                    'idempotency_key': turn_id, 'created_at': datetime.now(timezone.utc).isoformat(), 'capabilities': []}
                source_admission = None
                if self.shared_source is not None:
                    request, source_admission = self.shared_source.freeze(text_input, **identities)
                else:
                    request = freeze_product_turn('workbench.route', records=self.records, models=self.models,
                        project_id=project_id, load_text=lambda item: '', text=text_input, **identities)
                context = self.shared_source.scope() if self.shared_source is not None else nullcontext(None)
                with context as qualification, self.records.begin() as tx:
                    old = tx.read(_KEYS, record_key)
                    if old is None:
                        old = tx.put(_KEYS, record_key, {"identity": identity, "inputs": inputs, "request": request,
                            "configuration": configuration,
                            **({'source_admission': source_admission} if source_admission is not None else {})}, expected_revision=0)
                        from ..kernel.aux_routing import stage_auxiliary_choice
                        stage_auxiliary_choice(self.models, tx, turn_id)
                        from ..kernel.provider_store_binding import stage_provider_store_binding
                        stage_provider_store_binding(self.models, tx, request)
                    if qualification is not None:
                        from ..transaction_records import TransactionRecords
                        qualification._actor_reader(TransactionRecords(tx))
                    tx.commit()
            except Exception:
                return _fallback(text, has_files, reason="freeze_failed", intent=manual_intent)
            if old.payload["identity"] != identity or old.payload["inputs"] != inputs:
                raise ValueError("route_identity_conflict")
        request = old.payload["request"]
        turn_id = request["turn_id"]
        configuration = old.payload["configuration"]

        def guard():
            if monotonic() >= deadline:
                raise TimeoutError("route_deadline_exceeded")
            if not request["privacy"]["allow_remote"]:
                raise ValueError("route_remote_disabled")
            # This rechecks current egress whenever frozen remote authority is true.
            if self.shared_source is not None:
                self.shared_source.validate(request, old.payload.get('source_admission'))
            else:
                validate_frozen_inputs(self.records, self.models, request)
            if inputs['examples']:
                if self.examples is None:
                    raise ValueError('route_example_owner_unavailable')
                self.examples.validate_frozen(inputs['examples'])
            if 'source_origin' in inputs and self.examples.current(project_id) != inputs['source_origin']:
                raise ValueError('route_example_current_source_changed')
            public = self.models.public()["generation"]
            current = public_model_configuration(public)
            if current != configuration:
                raise ValueError("route_configuration_changed")

        def fallback(reason):
            return _fallback(text, has_files, reason=reason, turn_id=turn_id, intent=manual_intent, **self._receipt(turn_id))

        owner = self
        class Planner:
            def plan(self, frozen, events, capabilities, payloads, execution_control):
                guard()
                execution_control.checkpoint()
                from ..kernel.aux_routing import auxiliary_models
                models = auxiliary_models(owner.models, owner.store, turn_id, records=owner.records)
                public = models.public()['generation']
                selected_configuration = public_model_configuration(public)
                local = urlsplit(str(selected_configuration.get("base_url", ""))).hostname in {"localhost", "127.0.0.1", "::1"}
                route = {"configuration": selected_configuration, "project_id": project_id, "turn_id": turn_id,
                    "purpose": "aux", "execution_location": "local_loopback" if local else "remote"}
                ref = owner.store.get_or_create_immutable_payload(turn_id, "workbench-route-model-v1", route)
                binding = {"payload_ref": ref, "revision": _revision(route), "prompt_cache_scope_identity": _revision(route),
                    "configuration": selected_configuration, "execution_location": route["execution_location"]}
                from ..kernel.provider_store_binding import route_provider_store_activation
                activation = route_provider_store_activation(owner.models, models, owner.records,
                    owner.store, frozen, binding)
                response_model = DataRouteOutput if getattr(get('route'), 'data_operations', False) else RouteOutput
                schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False, separators=(',', ':'))
                messages = [{"role": "system", "content": getattr(get('route'), 'prompt', _PROMPT) + '\nJSON Schema:\n' + schema},
                            {"role": "user", "content": frozen["input"]["text"]}]
                output, metadata = models.complete_governed(messages, routing_snapshot=binding,
                    execution_control=execution_control, metadata_sink=execution_control, wire_attempt_sink=execution_control,
                    max_tokens=500 if response_model is DataRouteOutput else 400,
                    response_model=response_model, validate_current=guard, purpose="aux", retry_policy=get('retry'),
                    **({'provider_store_activation': activation} if activation is not None else {}))
                guard()
                execution_control.checkpoint()
                value = internal_route_dump(output) if isinstance(output, BaseModel) else output
                try:
                    validated = validate_route_output(text, value, has_files=has_files, deadline=deadline,
                        manual_intent=manual_intent)
                except ValueError as error:
                    codes = {'attachment_without_remember', 'multiple_do_parts', 'same_intent_parts', 'empty_span', 'instruction_not_allowed',
                        'situation_not_allowed', 'duplicate_dependency', 'invalid_dependency', 'dependency_direction',
                        'dependency_depth', 'uncovered_substantive_text', 'span_validation_budget_exceeded',
                        'span_not_literal_overlapping_or_incomplete'}
                    code = str(error) if type(error) is ValueError and str(error) in codes else 'output_invalid'
                    _LOG.warning('workbench_route_output_rejected: %s', code)
                    raise
                guard()
                ref = owner.store.get_or_create_immutable_payload(turn_id, _OUTPUT, internal_route_dump(validated))
                return {"type": "complete", "summary": "Workbench route validated", "payload_ref": ref}

        runtime = ProductPolicyRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
            events=self.store, payloads=self.store, state=self.store,
            planner_timeout_ms=max(1, int((deadline - monotonic()) * 1000)))
        try:
            guard()
            cached = self.store.get_immutable_payload(turn_id, _OUTPUT)
            events = tuple(self.store.events_after(turn_id))
            if cached and events and events[-1]["type"] == "turn.completed":
                return RoutePlan(**cached[1], mode="model", turn_id=turn_id, **self._receipt(turn_id))
            context = self.shared_source.scope() if self.shared_source is not None else nullcontext()
            with context, ExitStack() as admission:
                if self.shared_source is not None:
                    self.shared_source.validate(request, old.payload.get('source_admission'))
                    from core.ai_kernel.product_admission import bundle_from_payload
                    admission.enter_context(self.store.admission_bundle(request,
                        bundle_from_payload(old.payload['source_admission'])))
                accepted = runtime.accept_turn(request)
            if accepted.replayed:
                return fallback("prior_result_unavailable")
            from ..kernel.provider_store_binding import validate_provider_store_binding
            if validate_provider_store_binding(self.models, self.records, request):
                guard()
                now = datetime.now(timezone.utc)
                lease = self.store.try_acquire_run_lease(turn_id, 'route-' + uuid4().hex, now=now,
                    stale_after=now + timedelta(seconds=deadline - monotonic()))
                if lease is None:
                    return fallback("prior_result_unavailable")
                result = runtime.run_accepted_turn(turn_id, run_lease=lease)
                if result.status in {'completed', 'failed', 'cancelled'}:
                    self.store.release_strict_run_lease(lease)
            else:
                result = runtime.run_accepted_turn(turn_id)
            guard()
            cached = self.store.get_immutable_payload(turn_id, _OUTPUT)
            if result.status != "completed" or cached is None:
                return fallback("model_route_failed")
            return RoutePlan(**cached[1], mode="model", turn_id=turn_id, **self._receipt(turn_id))
        except Exception:
            return fallback("model_route_failed")
