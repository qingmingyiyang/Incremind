"""Model selection of bounded subtasks; authority stays with the rule builder."""
from __future__ import annotations
import json
from core.model_gateway import ModelRequest
from core.ai_kernel.agent_contracts import agent_role_brief_from_payload
from core.ai_kernel.model_planner import turn_routing_parameters, privacy_scope_of

class StewardDecompositionPlanner:
    def __init__(self, gateway, builder):
        self._gateway, self._builder = gateway, builder

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        brief = self._builder.brief(request)
        if brief.get('division_override') is not None:
            return {'type':'tool','capability_id':'agent.plan','arguments':self._builder.from_override(request)}
        if (self._gateway is None or privacy_scope_of(request.get("privacy")) != "remote_allowed"
                or brief["world_action"] or brief["slots"] < 1
                or not brief["budget_usable"] or not brief["profiles"]):
            raise ValueError("model decomposition is unavailable for this governed request")
        stored = payloads.get_immutable_payload(request["turn_id"], "agent-role-brief-v1")
        if stored is None:
            raise ValueError("frozen steward instructions are unavailable")
        role = agent_role_brief_from_payload(stored[1])
        prompt = {"instruction": role["instructions"],
                  **{key: brief[key] for key in ("task", "outcome", "slots", "profiles")},
                  "output": {"mode": "main_only | cluster", "assignments": [
                      {"profile_id": "profiles 中的一个", "task": "可独立完成的子任务"}]}}
        if brief['outcome'] == 'project.task':
            prompt['max_items'] = brief['max_items']
            prompt['division_examples'] = brief['division_examples']
            prompt['output']['assignments'][0].update(goal='一行目标', deliverable='需要交付的草稿',
                capabilities=['memory.recall', 'document.draft.propose'], depends_on=[])
            prompt['dependency_rule'] = 'depends_on 是从1开始的工作项序号，只填必需依赖，禁止环。能力只选档位允许的子集。'
        result = self._gateway.invoke(ModelRequest(
            capability="structured", input=json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
            parameters={"temperature": 0, "response_format": {"type": "json_object"},
                        **turn_routing_parameters(request, payloads)},
            privacy_scope=privacy_scope_of(request.get("privacy")),
            execution_control=execution_control, metadata_sink=execution_control,
        ))
        proposal = self._builder.complete(request, result.output)
        return {"type": "tool", "capability_id": "agent.plan", "arguments": proposal}
