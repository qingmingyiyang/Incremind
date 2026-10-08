"""将真实不可变任务和材料核验投影为原边界引擎的当轮批准。"""
from collections.abc import Mapping
import base64
from datetime import timedelta

from backend.security.ai_tool_execution_boundary import AIToolExecutionBoundary, TurnCapabilityBindingGuard
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from core.ai_boundary import BoundaryGrant, BoundaryRequest
from core.ai_boundary.contracts import utc_now
from core.ai_kernel import CapabilityDefinition, validate_turn_request
from core.ai_tooling import tool_boundary_target_identity, tool_contract_identity, tool_destination_identity

from .external_runner import ARCHIVE, CAPABILITY, ExternalRunner, _FIELDS, _encoded, definition


class ExternalExecutionAuthority:
    """只读取原 owner；不写 profile，也不提供 MCP 或系统隔离证明。"""

    def __init__(self, *, boundary_profiles, turn_binding_guard, execution_boundary, runner=None):
        if (not isinstance(boundary_profiles, ProjectBoundaryProfileStore)
                or not isinstance(turn_binding_guard, TurnCapabilityBindingGuard)
                or not isinstance(execution_boundary, AIToolExecutionBoundary)
                or (runner is not None and not isinstance(runner, ExternalRunner))):
            raise TypeError('external_execution_authority_invalid')
        self.runner = runner
        self._profiles = boundary_profiles
        self._binding_guard = turn_binding_guard
        self._boundary = execution_boundary

    def __call__(self, turn_request, capability, boundary_request):
        """签发前后短复验；任何缺失、漂移或材料脱敏都拒绝这份归档。"""
        try:
            return self._grant(turn_request, capability, boundary_request)
        except Exception:
            # 原工厂将 None 映射为固定拒绝，不传播资料或本机路径。
            return None

    def _grant(self, turn_request, capability, boundary_request):
        runner = self.runner
        if (not isinstance(runner, ExternalRunner)
                or not isinstance(capability, CapabilityDefinition)
                or not isinstance(boundary_request, BoundaryRequest)):
            return None
        native = definition()
        if (capability.capability_id != CAPABILITY or type(capability.version) is not int
                or capability.version != native.version or capability.mode != native.mode
                or capability.requires_approval is not native.requires_approval
                or capability.operation_semantics != native.operation_semantics
                or capability.tool_definition is None
                or tool_contract_identity(capability.tool_definition)
                    != tool_contract_identity(native.tool_definition)):
            return None
        request = validate_turn_request(turn_request)
        tool = capability.tool_definition
        scope = request['scope']
        selected = request.get('capability_request')
        if (request['desired_outcome'] != 'project.task' or scope.get('kind') != 'project'
                or type(request['execution_policy']['template_version']) is not int
                or request['execution_policy']['template_version'] != 2
                or not isinstance(selected, Mapping)
                or set(selected) != {'mode', 'capability_id', 'arguments'}
                or selected['mode'] != 'execute_exact_v1' or selected['capability_id'] != CAPABILITY
                or request['privacy']['allow_remote'] is not True
                or request['privacy']['mode'] != 'remote_allowed'):
            return None
        turn_id, project_id = request['turn_id'], scope['project_id']
        if (boundary_request.request_id != f'boundary-{turn_id}-{tool.tool_id}'
                or boundary_request.turn_id != turn_id or boundary_request.project_id != project_id
                or boundary_request.actor_id != 'ai-kernel'
                or boundary_request.operation_id != request['operation_id']
                or boundary_request.idempotency_key != request['idempotency_key']
                or boundary_request.target_id != tool_boundary_target_identity(tool, CAPABILITY)
                or boundary_request.effect != tool.effect
                or boundary_request.destination_kind != tool.destination
                or boundary_request.destination_id != tool_destination_identity(tool, CAPABILITY)
                or boundary_request.data_classes != tuple(sorted(tool.data_classes))
                or boundary_request.scan_state not in {'clean', 'redacted'}
                or boundary_request.reversible is not False
                or boundary_request.same_project is not True
                or boundary_request.requires_receipt is not True):
            return None
        saved = runner.turns.get_immutable_payload(turn_id, ARCHIVE)
        if saved is None or not isinstance(saved[1], dict) or set(saved[1]) != _FIELDS:
            return None
        ref, archive = saved
        if (archive['schema_version'] != '1.0.0' or archive['turn_id'] != turn_id
                or archive['owner_id'] != runner.owner_id
                or runner.context.owner_id != runner.owner_id or runner.host.owner_id != runner.owner_id
                or runner.context.records is not runner.records or runner.host.records is not runner.records
                or runner.context.turns is not runner.turns
                or selected['arguments'] != {'binding_ref':ref}
                or _encoded(archive['request']) != _encoded(request)
                or _encoded(runner.turns.get_request(turn_id)) != _encoded(request)):
            return None
        self._binding_guard.ensure_current(request)
        profile = self._profiles.get(project_id).profile
        if profile.project_id != project_id:
            return None
        delivery, clients = runner._delivery(request, archive['delivery']['turn_id'])
        launch = archive['launch']
        if (_encoded(delivery) != _encoded(archive['delivery'])
                or delivery['client'] != clients.get(launch['executor'])):
            return None
        # 空 arguments 和 opaque ref 不含正文；沿原扫描器核所有实际交付文本。
        materials = {'TASK':request['input']['text'], 'handoff':delivery['handoff']}
        material_snapshot = runner._archive_materials(archive)
        frozen_files = material_snapshot['files'] if material_snapshot is not None else {}
        attachments = {name:base64.b64decode(value['content'], validate=True).decode('utf-8', errors='replace')
            for name, value in frozen_files.items() if name not in {'TASK.md','CONTEXT.md','memory-mcp.json'}}
        if attachments:
            materials['attachments'] = attachments
        sanitized = self._boundary.sanitize_candidate_arguments(capability, materials, turn_id=turn_id)
        if sanitized is None or _encoded(sanitized) != _encoded(materials):
            return None
        plan = runner._archive_plan(archive)
        lease = runner.host.prepare(request, plan, mcp_config=archive['mcp_config'],
            host_permission_refs=archive['host_permission_refs'])
        try:
            runner._materials(lease, delivery, archive['mcp_config'], materials=material_snapshot,
                legacy=material_snapshot is None)
            current_delivery, _ = runner._delivery(request, archive['delivery']['turn_id'])
            self._binding_guard.ensure_current(request)
            if (self.runner is not runner or self._profiles.get(project_id).profile != profile
                    or _encoded(current_delivery) != _encoded(delivery)
                    or _encoded(runner.turns.get_request(turn_id)) != _encoded(request)
                    or runner.turns.get_immutable_payload(turn_id, ARCHIVE) != saved):
                return None
            return BoundaryGrant(grant_id='external-execution-' + turn_id,
                subject_id=boundary_request.actor_id, project_id=boundary_request.project_id,
                target_id=boundary_request.target_id, actions=(boundary_request.effect,),
                data_classes=boundary_request.data_classes,
                destinations=(boundary_request.destination_kind,),
                expires_at=utc_now() + timedelta(milliseconds=tool.timeout_ms), revision=profile.revision,
                revoked=False, redaction_required=boundary_request.scan_state == 'redacted')
        finally:
            lease.close()
