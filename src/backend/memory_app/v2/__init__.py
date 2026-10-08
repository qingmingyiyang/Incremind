"""Install the new product routes using the application's existing domains."""
import logging


_LOGGER = logging.getLogger(__name__)


def install_v2_routes(application, *, runtime_root, records, models, documents, service, workspace):
    from .devices import install_device_routes
    install_device_routes(application, registry=application.state.device_registry)
    if documents.namespace_id != "default":
        _LOGGER.info("Skip v2 routes for document namespace %s", documents.namespace_id)
        return
    # Keep importing the package safe for SourceEgress's privacy dependency.
    from .projects import install_project_routes
    from .jobs import install_job_routes
    from .workbench import install_workbench_routes
    from .library import LibraryRead, install_library_routes
    from .subscriptions import install_subscription_routes
    from .settings import install_settings_routes
    from .usage import install_usage_routes
    from .stats import install_stats_routes
    from .todos import install_todo_routes
    from .daily import install_daily_jobs
    from .cache_maintenance import CacheMaintenance
    from .auto_forget import AutoForget
    from .candidate_fade import CandidateFade
    from .links import InsightLinks, install_link_routes
    from .consolidation import Consolidation, install_consolidation_routes
    from .task_drafts import TaskDrafts
    from .external_context import ExternalContext
    from .external_runtime import install_external_runner
    from .external_host_bootstrap import install_local_host

    external_context = ExternalContext(records, owner_id='local-user', documents=documents)
    application.state.external_context = external_context
    application.state.external_context_installer = external_context.install
    from .mcp_memory import install_mcp_memory_routes
    install_mcp_memory_routes(application, workspace=workspace)
    from .mcp_intake import install_mcp_intake_routes
    install_mcp_intake_routes(application, workspace=workspace, records=records,
        service=service, reserve_write=external_context.guard.reserve_write)
    application.state.external_runner_installer = install_external_runner
    install_local_host(application.state, runtime_root=runtime_root, records=records, context=external_context)
    from .external_proxy import install_external_proxy_routes
    install_external_proxy_routes(application, runtime_root=runtime_root, records=records, workspace=workspace)

    application.state.product_task_models = models
    application.state.task_drafts = TaskDrafts(records, documents)
    from .inspirations import validate_task_inputs
    application.state.product_task_guard = lambda request: validate_task_inputs(records,models,request,query=workspace.query)
    from .part_context import validate_bound_context
    application.state.product_task_context = lambda request: validate_bound_context(records, models, request, query=workspace.query)
    original_read_planner = getattr(application.state, 'source_privacy_planner_wrapper', None)
    if original_read_planner is not None:
        from .inspirations import task_read_planner
        application.state.source_privacy_planner_wrapper = lambda planner, turns, agents: task_read_planner(
            original_read_planner(planner, turns, agents), records, models, query=workspace.query)
    from .profile import frozen_task_profile
    from .method_context import frozen_task_methods
    from .style_context import frozen_task_style
    def task_profile(request, *, reader=None):
        current = records if reader is None else reader
        return {**frozen_task_profile(current, service, request),
                'methods': frozen_task_methods(current, workspace.query, request),
                'style': frozen_task_style(current, service, request)}
    application.state.product_task_profile = task_profile
    from ..kernel.task_planner import ProductOutcomeComposition
    from .outcomes import validate_continuation, COMPOSITION
    from .outcome_patches import apply_patch, OutcomePatchError
    from .policies import get
    def validate_task_sources(reader, request):
        # 两种产品冻结来源共用原外发事务，原服务保留其只读证据连接能力。
        validate_continuation(reader, request)
        frozen_task_style(reader, service, request)
    application.state.product_task_outcomes = ProductOutcomeComposition(
        validate=validate_task_sources, policy=lambda version: get('continuation', version=version),
        patch=apply_patch, patch_error=OutcomePatchError, result_kind=COMPOSITION)

    install_project_routes(application, records=records)
    from datetime import datetime, timezone
    from .reminders import ReminderService, install_reminder_routes
    reminders = ReminderService(records, now=lambda: datetime.now(timezone.utc),
        local_timezone=datetime.now().astimezone().tzinfo)
    application.state.memory_reminders = reminders
    install_reminder_routes(application, service=reminders)
    from .skill_export_routes import install_skill_export_routes
    install_skill_export_routes(application, records=records, service=service, models=models)
    from .context_feedback import install_context_feedback_routes
    install_context_feedback_routes(application, records=records)
    install_subscription_routes(application, models=models)
    install_settings_routes(application, runtime_root=runtime_root, records=records, models=models,
                           documents=documents, workspace=workspace)
    install_usage_routes(application, records=records)
    from .signals import install_signal_routes
    install_signal_routes(application, records=records)
    from .gaps import install_gap_routes
    install_gap_routes(application, records=records, runtime_root=runtime_root, query=workspace.query)
    install_stats_routes(application, records=records)
    install_todo_routes(application, records=records, service=service, documents=documents, workspace=workspace)
    install_link_routes(application, records=records, service=service)
    daily_jobs = install_daily_jobs(application, records=records, runtime_root=runtime_root)
    consolidation = Consolidation(records, service, documents, models, completion_clock=daily_jobs.completion_clock)
    application.state.memory_consolidation = consolidation
    install_consolidation_routes(application, records=records, consolidation=consolidation)
    daily_jobs.register('auto_forget', AutoForget(records).run)
    daily_jobs.register('candidate_fade', CandidateFade(records).run)
    daily_jobs.register('embedding_cache', CacheMaintenance(workspace.query).run)
    from .embedding_index import EmbeddingIndex
    application.state.memory_embedding_index = EmbeddingIndex(workspace.query)
    daily_jobs.register('local_embedding_index', application.state.memory_embedding_index.run)
    daily_jobs.register('insight_links', InsightLinks(records, service, models).run)
    daily_jobs.register('consolidation', consolidation.run)
    from .nudges import NudgeService, install_nudge_routes
    nudges = NudgeService(records, library=LibraryRead(records, service, documents, workspace),
        query=workspace.query, models=models, signals=application.state.memory_signals,
        reminders=reminders, now=lambda: reminders.now(), local_timezone=reminders.local_timezone)
    application.state.memory_nudges = nudges
    daily_jobs.register('nudges', nudges.run)
    install_nudge_routes(application, service=nudges)
    install_job_routes(application, records=records, service=service, document_namespace=documents.namespace_id)
    organization = _LazyOrganization(application)
    organization.method_query = workspace.query
    install_workbench_routes(application, records=records, models=models,
                             organization=organization,
                             research_reader=organization.read,
                             topology_reader=organization.topology,
                             usage_reader=organization.usage,
                             documents=documents, service=service, workspace=workspace,
                             reminders=reminders)
    install_library_routes(application, records=records, service=service,
                           documents=documents, workspace=workspace, models=models)


class TaskRuntimeProjection:
    """直接持有原运行主人，工作台与 Do 共用原启动校验和读取投影。"""
    def __init__(self, *, runtime, turn_store, composition, organization, method_query=None):
        self.runtime, self.turn_store = runtime, turn_store
        self.composition, self.organization = composition, organization
        self.method_query = method_query

    def available(self):
        return self.organization is not None

    def start(self, request, *, agent_turn_mode):
        if not self.available():
            raise RuntimeError('research_runtime_unavailable')
        store = self.turn_store
        existing = store.get_request(request['turn_id']) if store is not None else None
        if existing is not None:
            if (existing.get('operation_id') != request['operation_id'] or existing.get('scope') != request['scope']
                    or existing.get('input') != request['input']):
                raise RuntimeError('research_binding_changed')
            return {'main': {'turn_id': request['turn_id']}}
        return self.organization.start(request, agent_turn_mode=agent_turn_mode)

    def _frozen_request(self, identity, project):
        store = self.turn_store
        request = store.get_request(identity) if store is not None else None
        if request is None:
            raise RuntimeError('research_request_unavailable')
        if request.get('scope', {}).get('project_id') != project:
            raise RuntimeError('research_scope_changed')
        return request

    def read(self, identity, project):
        if not self.available():
            raise RuntimeError('research_runtime_unavailable')
        request = self._frozen_request(identity, project)
        receipt = self.runtime.receipt_for(identity)
        events = tuple(self.runtime.events_after(identity))
        summary = next((event.get('data', {}).get('summary', '') for event in reversed(events)
            if event.get('type') == 'turn.completed' and event.get('sequence', 0) <= receipt.current_sequence), '')
        result = {'status': receipt.status, 'summary': summary if isinstance(summary, str) else ''}
        if receipt.status == 'completed':
            from .outcomes import completed_composition
            outcome = completed_composition(self.turn_store, request, result['summary'],
                events=[event for event in events if event.get('sequence', 0) <= receipt.current_sequence])
            if outcome is not None:
                result['outcome'] = outcome
        return result

    def usage(self, identity, project, *, descendants=False):
        from core.ai_kernel.execution_projection import build_execution_projection
        if any(owner is None for owner in (self.composition, self.turn_store, self.runtime)):
            return []
        composition = self.composition
        pending, visited, rows = [identity], set(), []
        while pending:
            turn = pending.pop()
            if turn in visited:
                continue
            visited.add(turn)
            request = composition.request_loader(turn)
            if request.get('scope', {}).get('project_id') != project:
                raise RuntimeError('research_scope_changed')
            events = tuple(self.runtime.events_after(turn))
            if events:
                projection = build_execution_projection(events, view='developer', payload_loader=self.turn_store.get)
                rows.extend({**step, 'turn_id': turn} for step in projection['model_steps'])
            if descendants:
                frozen = self._frozen_request(turn, project)
                if 'agent.list' not in frozen.get('capability_policy', {}).get('allowed', []):
                    continue
                children = composition.coordinator.list(parent_turn_id=turn, project_id=project,
                    scope=frozen['scope'], privacy=frozen['privacy'])['children']
                pending.extend(child['turn_id'] for child in children)
        return rows

    def topology(self, identity, project):
        composition = self.composition
        request = self._frozen_request(identity, project)
        if request['desired_outcome'] == 'project.task':
            return self._task_topology(identity, project)
        rows = composition.coordinator.list(parent_turn_id=identity, project_id=project,
            scope=request['scope'], privacy=request['privacy'])['children']
        experts = []
        for row in rows:
            if row.get('profile_id') == 'steward.scheduler':
                continue
            profile = composition.profiles.get(row['profile_id'])
            role = row.get('organization_role') or getattr(profile, 'organization_role', None) or row['profile_id']
            status = row.get('status')
            experts.append({'role': role, 'state': 'done' if status == 'completed' else
                'failed' if status in {'failed', 'cancelled', 'timed_out', 'interrupted', 'quarantined'} else 'running'})
        return experts

    def _task_topology(self, identity, project):
        from .task_receipt_details import task_receipt_details
        composition = self.composition
        main, _ = composition.store.get_run_by_turn_id(identity, project_id=project)
        plans = composition.dispatch_store.list_plans_for_main(project_id=project, main_run_id=main.run_id)
        result = []
        for plan in plans:
            indices = {key:index for index,key in enumerate(plan.assignment_ids)}
            permits = {item.assignment_id:item for item in composition.dispatch_store.list_permits(project_id=project,plan_id=plan.plan_id)}
            for assignment_id in plan.assignment_ids:
                assignment = composition.dispatch_store.get_assignment(assignment_id,project_id=project)
                payload = self.turn_store.get(assignment.task_payload_ref)
                division = payload.get('division', {'goal':payload['task'], 'deliverable':'整理稿', 'depends_on':[]})
                run_id = composition.dispatch_store.get_permit_child_run_binding(permits[assignment_id].permit_id,project_id=project)
                found = composition.store.get_run_with_revision(run_id,project_id=project) if run_id else None
                run = found[0] if found else None
                steps = self.usage(run.turn_id,project) if run else []
                usage = {'input_tokens':0,'output_tokens':0}
                for step in steps:
                    for key in usage:
                        usage[key] += (step.get('usage') or {}).get(key) or 0
                result.append({'assignment_id':assignment_id, 'turn_id':run.turn_id if run else None,
                    'role':assignment.profile_ref.rsplit('/',1)[-1],
                    'goal':division['goal'], 'deliverable':division['deliverable'],
                    'capabilities':list(assignment.capability_ids),
                    'depends_on':[indices[dependency] for dependency in division['depends_on']],
                    'state':'waiting' if run is None else 'done' if run.status == 'completed' else 'failed' if run.is_terminal else 'running',
                    'model_usage':usage,
                    **(task_receipt_details(run.turn_id, self.turn_store.events_after(run.turn_id),
                                           self.turn_store.get) if run else {'tools':[], 'recalled':[]})})
        return result


class _LazyOrganization:
    """继续懒加载应用的原主人，读取逻辑委托给同一产品投影。"""
    def __init__(self, application):
        self.application = application

    def available(self):
        state = self.application.state
        if getattr(state, 'agent_organization_runtime', None) is not None:
            return True
        if getattr(state, 'container', None) is None:
            return False
        dispatcher = getattr(state, 'recognition_turn_dispatcher', None)
        if dispatcher is None:
            return False
        dispatcher._runtime()
        return getattr(state, 'agent_organization_runtime', None) is not None

    def _projection(self):
        state = self.application.state
        return TaskRuntimeProjection(runtime=getattr(state, 'ai_runtime', None),
            turn_store=getattr(state, 'ai_turn_store', None),
            composition=getattr(state, 'agent_runtime_composition', None),
            organization=getattr(state, 'agent_organization_runtime', None),
            method_query=getattr(self, 'method_query', None))

    def start(self, request, *, agent_turn_mode):
        if not self.available():
            raise RuntimeError('research_runtime_unavailable')
        return self._projection().start(request, agent_turn_mode=agent_turn_mode)

    def _frozen_request(self, identity, project):
        return self._projection()._frozen_request(identity, project)

    def read(self, identity, project):
        if not self.available():
            raise RuntimeError('research_runtime_unavailable')
        return self._projection().read(identity, project)

    def usage(self, identity, project, *, descendants=False):
        return self._projection().usage(identity, project, descendants=descendants)

    def topology(self, identity, project):
        # 保留原 adapter 在读取请求前取得 composition 的顺序。
        self.application.state.agent_runtime_composition
        return self._projection().topology(identity, project)

    def _task_topology(self, identity, project):
        self.application.state.agent_runtime_composition
        return self._projection()._task_topology(identity, project)
