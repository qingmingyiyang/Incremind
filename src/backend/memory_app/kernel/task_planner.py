"""Use the configured product gateway inside the existing organization planner."""
import json
from pydantic import BaseModel, ConfigDict
from copy import deepcopy
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from core.ai_kernel.model_planner import ModelGatewayAgentPlanner
from core.model_gateway import ModelResult
from backend.api.agent_organization_planner import AgentRoleDispatchPlanner, MainCoordinationPlanner, StewardPlanningPlanner
from backend.api.agent_steward_decomposition import StewardDecompositionPlanner
from backend.api.agent_steward_proposal import is_valid_steward_proposal
from ..turn_routing import SNAPSHOT_KIND, RecognitionRoutingSnapshot, _revision
from .product_routing import ProductGenerationRouting
from .task_division_authority import frozen_division_binding
from .task_draft_capability import final_draft_decision
from .policy_runtime import retry_policy_for
from .task_continuations import TaskContinuations, TaskModelInterrupted
from ..model_config import ModelConfigurationError, ModelResponseDecodeError, _preserve_turn_control_error
from backend.shared.llm.model_transport import ModelContinuationFailed, ModelInterrupted, is_provider_closed_witness


class _TaskDecision(BaseModel):
    # The complete dictionary still goes through the original decision validator.
    model_config = ConfigDict(extra='allow')
    summary: str = ''


@dataclass(frozen=True)
class ProductOutcomeComposition:
    """由产品装配注入原领域能力，规划器只委托调用。"""
    validate: Callable
    policy: Callable
    patch: Callable
    patch_error: type[ValueError]
    result_kind: str

    def __post_init__(self):
        if (not all(callable(value) for value in (self.validate, self.policy, self.patch))
                or not isinstance(self.patch_error, type) or not issubclass(self.patch_error, ValueError)
                or not isinstance(self.result_kind, str) or not self.result_kind):
            raise ValueError('invalid outcome composition capability')


class _MainCompletionMetadata:
    """一次逻辑汇总共用同一路由，真实尝试和费用仍归原外发回执。"""
    def __init__(self, inner):
        self.inner, self.route, self.usage, self.cache = inner, None, [], []
        self.current_cache = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def model_call_routed(self, **route):
        if self.route is None:
            self.inner.model_call_routed(**route)
            self.route = deepcopy(route)
        elif self.route != route:
            raise ValueError('outcome retry route changed')

    def model_call_started(self, **values):
        self.current_cache = None
        self.inner.model_call_started(**values)

    def model_call_cache_observed(self, *, observation):
        if self.current_cache is not None:
            raise ValueError('outcome attempt cache was already recorded')
        self.current_cache = deepcopy(observation)

    def model_call_completed(self, *, usage):
        self.usage.append(deepcopy(usage))
        self.cache.append(self.current_cache)

    def finish(self):
        from .receipt_projection import aggregate_usage
        usage = aggregate_usage([{'usage': value} for value in self.usage]) or {}
        # 未报告部分保持未知，原逻辑终态仍按完整三项计数判定。
        if usage.get('observed_only'):
            usage = {}
        self.inner.model_call_completed(usage=usage)
        keys = ('cache_read_input_tokens', 'cache_creation_input_tokens', 'cache_miss_input_tokens')
        cache = {key: sum(value[key] for value in self.cache)
                 for key in keys if self.cache and all(isinstance(value, dict)
                    and type(value.get(key)) is int and value[key] >= 0 for value in self.cache)}
        if cache:
            self.inner.model_call_cache_observed(observation=cache)
        return usage


class ProductTaskPlanner:
    def __init__(self, *, models, store, composition, guard, builder, fallback,
                 profile_reader=None, drafts=None, context_reader=None,
                 records=None, runtime_root=None, read_control_type=None, frame_factory=None, outcome=None):
        self.models, self.store, self.composition = models, store, composition
        self.guard, self.builder, self.fallback = guard, builder, fallback
        self.routing = ProductGenerationRouting(models, store, records=records,
            agent_binding_verifier=getattr(composition.coordinator, 'verify_agent_binding', None),
            steward_parent_check=self.handles)
        self.profile_reader = profile_reader
        self.drafts = drafts
        self.context_reader = context_reader
        self.frame_factory = frame_factory
        self.continuations = (TaskContinuations(self, records, runtime_root, read_control_type=read_control_type)
            if records is not None and runtime_root is not None else None)
        self.outcome = outcome

    def profile_request(self, request):
        current, seen = request, set()
        while current.get('agent_binding', {}).get('parent_run_id'):
            parent_id = current['agent_binding']['parent_run_id']
            if parent_id in seen:
                raise ValueError('cyclic task ancestry')
            seen.add(parent_id)
            parent = self.composition.store.get_run(parent_id)
            if parent is None:
                raise ValueError('task parent unavailable')
            current = self.composition.request_loader(parent.turn_id)
            if current['scope'] != request['scope']:
                raise ValueError('task profile scope changed')
        return current

    def handles(self, request):
        if request.get('desired_outcome') == 'project.task':
            policy = request.get('execution_policy')
            exact = request.get('capability_request')
            # 原 Kernel 先验证请求；精确外部任务不进入模型分工路径。
            external = (isinstance(policy, Mapping) and type(policy.get('template_version')) is int
                and policy['template_version'] == 2 and isinstance(exact, Mapping)
                and exact.get('mode') == 'execute_exact_v1'
                and exact.get('capability_id') == 'external.task.execute')
            return not external
        if request.get('desired_outcome') != 'agent.steward.plan':
            return False
        parent_id = request.get('agent_binding',{}).get('parent_run_id')
        parent = self.composition.store.get_run(parent_id) if parent_id else None
        return parent is not None and self.composition.request_loader(parent.turn_id).get('desired_outcome') == 'project.task'

    def acquire(self, **kwargs):
        return self.routing.acquire(**kwargs)

    def _main_frames(self, request, recipe, continuation):
        if self.continuations is None or self.frame_factory is None or request.get('desired_outcome') != 'project.task':
            return None
        try:
            run = self.composition.store.get_run(request.get('agent_binding', {}).get('run_id'))
            if (run is None or run.role != 'main' or run.turn_id != request['turn_id']
                    or self.store.get_request(request['turn_id']) != request
                    or not request['session_id'].startswith('session-')):
                return None
            identity = request['session_id'][len('session-'):]
            product = self.continuations.records.read('v2_turns', identity)
            execution = self.continuations.records.read('v2_task_executions', identity)
            if (product is None or execution is None or product.payload.get('intent') != 'do'
                    or product.payload.get('project_id') != request['scope']['project_id']
                    or product.payload['receipt']['do'].get('kernel_turn_id') != request['turn_id']):
                return None
            self.continuations.validate_product_request(execution.payload['request'], request)
            thread = self.continuations.records.read('v2_threads', product.payload['thread_id'])
            if thread is None or thread.payload.get('project_id') != request['scope']['project_id']:
                return None
            turn = {'id': identity, **{key: product.payload[key] for key in
                    ('thread_id', 'intent', 'user_text', 'created_at')}}
            return self.frame_factory(self.continuations.records, turn_id=identity,
                project_id=request['scope']['project_id'], recipe=recipe, turn=turn,
                projection={'kernel_turn_id': request['turn_id'], 'request': execution.payload['request']},
                request=request, text_prefix=continuation['partial'] if continuation is not None else None)
        except Exception:
            # Derived text storage cannot become a new model execution gate.
            return None

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        if not self.handles(request):
            return self.fallback.plan(request,events,capabilities,payloads,execution_control=execution_control)
        saved = self.store.get_immutable_payload(request['turn_id'], SNAPSHOT_KIND)
        if saved is None:
            raise ValueError('task model configuration was not frozen')
        route = RecognitionRoutingSnapshot(saved[0],_revision(saved[1]),saved[1])
        owner = self
        profile_request = self.profile_request(request)
        try:
            root_input = json.loads(profile_request['input']['text'])
        except (ValueError, TypeError):
            root_input = None
        outcome = root_input.get('outcome_input') if isinstance(root_input, dict) else None
        style = root_input.get('style_input') if isinstance(root_input, dict) else None
        is_outcome_main = (outcome is not None and outcome.get('document_id') is not None
                           and request['turn_id'] == profile_request['turn_id'])
        is_style_main = (style is not None and bool(style.get('text'))
                         and request['turn_id'] == profile_request['turn_id'])
        if outcome is not None or style is not None:
            if self.outcome is None:
                raise ValueError('outcome composition capability is unavailable')
            if execution_control is None or not callable(getattr(execution_control, 'with_validator', None)):
                raise ValueError('outcome wire authority is unavailable')
            execution_control = execution_control.with_validator(
                lambda records, _: self.outcome.validate(records, profile_request))
        def validate():
            owner.guard(request)
            if profile_request['turn_id'] != request['turn_id']:
                owner.guard(profile_request)
            if owner.profile_reader:
                owner.profile_reader(profile_request)
            if execution_control is not None:
                execution_control.checkpoint()
        decision = final_draft_decision(request, events, payloads,
            binding=frozen_division_binding(self.composition, request, self.store), drafts=self.drafts)
        if decision is not None and not is_outcome_main and not is_style_main:
            validate()
            return decision
        class Gateway:
            def invoke(self, model_request):
                validate()
                profile = owner.profile_reader(profile_request) if owner.profile_reader else {}
                messages = [{'role':'user','content':model_request.input}]
                if owner.context_reader and profile_request['turn_id'] != request['turn_id']:
                    context = owner.context_reader(profile_request)
                    if context:
                        messages.insert(0, {'role':'user', 'content':context['text']})
                if profile.get('text'):
                    messages.insert(0, {'role':'system', 'content':profile['text']})
                methods = profile.get('methods', {})
                if methods.get('text'):
                    messages.insert(-1, {'role':'system', 'content':methods['text']})
                writing = profile.get('style', {})
                if is_style_main and writing.get('text'):
                    messages.insert(0, {'role':'system', 'content':writing['text']})
                policy = None
                metadata_sink = execution_control
                if is_outcome_main:
                    policy = owner.outcome.policy(outcome['continuation_policy'])
                    metadata_sink = _MainCompletionMetadata(execution_control)
                    messages.insert(-1, {'role': 'system', 'content': policy.patch_instruction(outcome['current_markdown'])})
                options = {}
                prefix = {'raw': None}
                recipe = retry_policy_for(profile_request)
                continuation, resume_source = None, None
                expected_continuation = owner.continuations and request['turn_id'] in owner.continuations.active
                try:
                    continuation = owner.continuations.take(request['turn_id']) if owner.continuations else None
                    if owner.routing.records is not None and owner.routing._provider_store_owner(request, ()):
                        from .provider_store_binding import main_provider_store_activation, steward_provider_store_activation
                        activate = (steward_provider_store_activation if request.get('desired_outcome') == 'agent.steward.plan'
                            else main_provider_store_activation)
                        activation = activate(owner.models, owner.routing.records, request, route.payload)
                        if activation is not None:
                            if continuation is not None and owner.routing._main_owner(request, ()):
                                resume_source = owner.continuations.provider_resume_source(request['turn_id'],
                                    capsule=continuation, execution_control=execution_control)
                                if resume_source is not None:
                                    activation = activate(owner.models, owner.routing.records, request, route.payload,
                                        provider_resume_source=resume_source)
                            options['provider_store_activation'] = activation
                except Exception as error:
                    if _preserve_turn_control_error(error, execution_control):
                        raise
                    if expected_continuation:
                        # 原 Main 会兜底一般异常；选定继续的数据拒绝必须保持失败。
                        raise ModelContinuationFailed() from None
                    raise
                if continuation is not None and resume_source is None:
                    messages = retry_policy_for(profile_request)({'kind': 'continue_task_messages',
                        'messages': continuation['messages'], 'partial': continuation['partial']})
                frames, interrupted = None, False
                resumed_text = {'full': '', 'sent': 0}
                if 'decision_contract' in json.loads(model_request.input):
                    frames = owner._main_frames(request, recipe, continuation)
                    def observe(value):
                        prefix.update(raw=value['raw'])
                        if (frames is not None and frames.text
                                and recipe({'kind': 'pure_text_prefix', 'raw': prefix['raw']}) is not True):
                            frames.close(completed=True)
                    def delta(text):
                        if resume_source is not None:
                            resumed_text['full'] += text
                            prior, full = continuation['partial'], resumed_text['full']
                            if not full.startswith(prior):
                                if len(full) < len(prior) and prior.startswith(full):
                                    return
                                raise ModelContinuationFailed()
                            text = full[len(prior) + resumed_text['sent']:]
                            resumed_text['sent'] += len(text)
                        if (frames is not None and recipe(
                                {'kind': 'pure_text_prefix', 'raw': prefix['raw']}) is True):
                            if not frames.stream:
                                frames.start()
                            frames.delta(text)
                    options.update(response_model=_TaskDecision, stream_field='summary',
                                   on_delta=delta, observe_decoding=observe)
                    if frames is not None:
                        options['on_retry'] = frames.retry
                def complete():
                    nonlocal interrupted
                    try:
                        output, metadata = owner.models.complete_governed(
                            messages, routing_snapshot=route.generation_binding(),
                            execution_control=execution_control, metadata_sink=metadata_sink,
                            wire_attempt_sink=execution_control, max_tokens=2000, validate_current=validate, purpose='primary',
                            retry_policy=recipe, **options)
                    except ModelInterrupted as error:
                        validate()
                        interrupted = (is_provider_closed_witness(error.close_witness)
                            and recipe({'kind': 'pure_text_prefix', 'raw': prefix['raw']}) is True)
                        if continuation is not None and resume_source is None:
                            error.partial = continuation['partial'] + error.partial
                        raise TaskModelInterrupted(error, owner=owner, request=request, control=execution_control,
                            messages=messages, route=route.generation_binding(), validate=validate,
                            raw_prefix=prefix['raw']) from None
                    except ModelResponseDecodeError as error:
                        if continuation is not None:
                            raise ModelContinuationFailed() from None
                        if not is_outcome_main:
                            raise
                        validate()
                        # 已付费的拒绝也进入原用量汇总，再由原补丁校验决定一次重试。
                        metadata_sink.model_call_completed(usage=error.usage)
                        return None, {'usage': error.usage}
                    except ModelConfigurationError:
                        if continuation is not None:
                            raise ModelContinuationFailed() from None
                        raise
                    validate()
                    try:
                        decision = output.model_dump(exclude_unset=True) if isinstance(output, _TaskDecision) else json.loads(output)
                    except (ValueError, TypeError):
                        if not is_outcome_main:
                            raise
                        decision = None
                    if resume_source is not None:
                        # 完整正文的前缀一致只是数据校验，原动作和租约仍分别复核。
                        if (decision.get('type') != 'complete' or type(decision.get('summary')) is not str
                                or not decision['summary'].startswith(continuation['partial'])
                                or recipe({'kind': 'pure_text_prefix', 'raw': prefix['raw']}) is not True):
                            raise ModelContinuationFailed()
                    if (continuation is not None and owner.continuations.automatic_text(request['turn_id'], continuation)
                            and (decision.get('type') != 'complete' or recipe(
                                {'kind': 'pure_text_prefix', 'raw': prefix['raw']}) is not True)):
                        raise ValueError('automatic_task_continuation_requires_pure_text')
                    if continuation is not None and resume_source is None and decision.get('type') == 'complete':
                        decision['summary'] = continuation['partial'] + decision['summary']
                    return decision, metadata
                try:
                    value, metadata = complete()
                    if is_outcome_main and (not isinstance(value, dict) or value.get('type') != 'tool'):
                        fallback = False
                        for attempt in range(2):
                            try:
                                patched = owner.outcome.patch(outcome['current_markdown'], outcome['birth_ai_markdown'],
                                    value.get('patches') if isinstance(value, dict) and value.get('type') == 'complete' else None)
                                break
                            except owner.outcome.patch_error as error:
                                if attempt == 0:
                                    messages.insert(-1, {'role': 'system', 'content': policy.retry_instruction(
                                        {'code': error.code, 'operation_index': error.operation_index})})
                                    value, metadata = complete()
                                else:
                                    messages.insert(-1, {'role': 'system', 'content': policy.fallback_instruction()})
                                    value, metadata = complete()
                                    if not isinstance(value, dict) or value.get('type') != 'complete' or not isinstance(value.get('summary'), str) or not value['summary'].strip():
                                        raise ValueError('outcome fallback requires a new draft')
                                    patched = {'markdown': value['summary'], 'changes': []}
                                    fallback = True
                        owner.store.get_or_create_immutable_payload(request['turn_id'], owner.outcome.result_kind,
                            {'turn_id': request['turn_id'], 'model_request_id': execution_control.model_request_id,
                             'input': request['input'],
                             'markdown': patched['markdown'], 'changes': patched['changes'], 'fallback_new': fallback})
                        value = {'type': 'complete', 'summary': patched['markdown']}
                    if is_outcome_main:
                        metadata['usage'] = metadata_sink.finish()
                    return ModelResult(value, str(route.payload['configuration'].get('provider') or ''),
                        str(route.payload['configuration'].get('model') or ''),metadata.get('usage',{}))
                finally:
                    if frames is not None:
                        frames.close(completed=not interrupted)
        gateway = Gateway()
        worker = ModelGatewayAgentPlanner(gateway)
        planner = AgentRoleDispatchPlanner(delegate=worker,
            steward=StewardPlanningPlanner(remote=StewardDecompositionPlanner(gateway,self.builder),
                proposal_validator=is_valid_steward_proposal,
                proposal_builder=lambda value,*_: self.builder.build(value)),
            main=MainCoordinationPlanner(remote=worker,wait_timeout_ms=30000))
        return planner.plan(request,events,capabilities,payloads,execution_control=execution_control)
