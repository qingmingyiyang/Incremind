"""Deterministic, provider-free proposal construction for the steward Agent."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import re
import json
from typing import Protocol

from core.ai_kernel import AgentBudget, AgentProfile, AgentRun
from backend.api.workbench_ai_runtime import WORLD_PROJECT_SESSION_ID


class AgentStewardProposalError(ValueError):
    """The durable steward/main boundary cannot produce a safe plan DTO."""


class AgentRunReader(Protocol):
    def get_run(self, run_id: str) -> AgentRun | None: ...
    def list_child_links(self, *, project_id: str, parent_run_id: str | None = None) -> Sequence[object]: ...
    def list_reservations(self, *, project_id: str, parent_run_id: str | None = None) -> Sequence[object]: ...


class AgentProfileReader(Protocol):
    def get(self, profile_id: str) -> AgentProfile | None: ...


class ProjectCapabilityReader(Protocol):
    def get(self, project_id: str) -> object: ...


RequestLoader = Callable[[str], Mapping[str, object]]
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class _Capacity:
    slots: int
    budget: AgentBudget


class AgentStewardProposalBuilder:
    """Build the exact ``agent.plan`` DTO from previously governed inputs.

    This is an advisory read model.  It does not persist, invoke tools, select
    providers/models, or claim a permit.  The dispatch runtime remains the
    authority that validates and freezes the resulting DTO.
    """

    def __init__(
        self,
        *,
        profiles: AgentProfileReader,
        agent_store: AgentRunReader,
        request_loader: RequestLoader,
        capability_profiles: ProjectCapabilityReader,
        expert_catalog: object | None = None,
        expert_bindings: object | None = None,
        capability_definition: Callable[[str], object] | None = None,
    ) -> None:
        if not callable(request_loader):
            raise AgentStewardProposalError("main request loader is invalid")
        self._profiles = profiles
        self._store = agent_store
        self._request_loader = request_loader
        self._capabilities = capability_profiles
        self._expert_catalog = expert_catalog
        self._expert_bindings = expert_bindings
        self._capability_definition = capability_definition

    def _task_allowed(self, allowed: set[str], outcome: str) -> set[str]:
        if outcome != 'project.task':
            return allowed
        from core.ai_tooling import tool_from_capability
        permitted = set()
        for identity in allowed:
            definition = self._capability_definition(identity) if self._capability_definition else None
            if definition is None:
                continue
            tool = tool_from_capability(definition)
            if tool.execution_mode == 'parallel' and (tool.effect == 'read' or (
                    tool.effect == 'write' and tool.destination == 'local' and
                    'draft_create_only' in tool.boundary_requirements)):
                permitted.add(identity)
        return permitted

    def brief(self, steward_request: Mapping[str, object]) -> dict[str, object]:
        _, main, main_request = self._durable_parent(steward_request)
        capacity = self._capacity(main)
        task, refs, outcome, allowed = _main_task(main_request)
        allowed = self._task_allowed(allowed, outcome)
        examples = []
        override = None
        if outcome == 'project.task':
            try:
                product = json.loads(task)
            except (ValueError, TypeError):
                product = None
            if isinstance(product, dict) and set(product).difference({'outcome_selection', 'outcome_input', 'style_input'}) in ({'task','division_examples'}, {'task','division_examples','division_override'}) and isinstance(product['task'],str):
                task = product['task']
                examples = product['division_examples'] if isinstance(product['division_examples'],list) else []
                override = product.get('division_override')
        return {
            "task": task if len(task) <= 2500 else task[:2500] + "（已截断）",
            "outcome": outcome,
            "slots": min(2, capacity.slots),
            "max_items": min(8, capacity.budget.model_calls) if outcome == 'project.task' and capacity.slots else min(2, capacity.slots),
            "profiles": [{"profile_id": item.profile_id,
                          "organization_role": item.organization_role,
                          "work_description": item.work_description,
                          "capabilities": _profile_capabilities(main,item,allowed)}
                         for item in self._eligible_profiles(main, allowed, outcome, refs)],
            "world_action": main_request.get("session_id") == WORLD_PROJECT_SESSION_ID,
            "budget_usable": _usable(capacity.budget),
            "division_examples": examples[:3],
            "division_override": override,
        }

    def from_override(self, request):
        brief = self.brief(request)
        items = brief['division_override']
        if not isinstance(items,list) or not items:
            raise AgentStewardProposalError('task division override is invalid')
        assignments = []
        for item in items:
            eligible = next((profile for profile in brief['profiles']
                             if set(item['capabilities']).issubset(profile['capabilities'])), None)
            if eligible is None:
                raise AgentStewardProposalError('task division has no capable profile')
            assignments.append({'profile_id':eligible['profile_id'], 'task':item['goal'],
                'goal':item['goal'],'deliverable':item['deliverable'],
                'capabilities':item['capabilities'] or ['memory.recall'],
                'depends_on':[index+1 for index in item['depends_on']]})
        return self.complete(request,{'mode':'cluster','assignments':assignments})

    def complete(self, steward_request: Mapping[str, object], selection: object) -> dict[str, object]:
        brief = self.brief(steward_request)
        if not isinstance(selection, Mapping):
            raise AgentStewardProposalError("model selection must be an object")
        if selection.get("mode") == "main_only" and set(selection) == {"mode"}:
            return {"mode": "main_only", "plan_id": "steward-main-only"}
        assignments = selection.get("assignments")
        if (selection.get("mode") != "cluster" or set(selection) != {"mode", "assignments"}
                or not isinstance(assignments, list) or not 1 <= len(assignments) <= brief["max_items"]
                or not brief["budget_usable"]):
            raise AgentStewardProposalError("model cluster selection is invalid")
        eligible = {item["profile_id"] for item in brief["profiles"]}
        selected: list[AgentProfile] = []
        tasks: list[str] = []
        seen: set[str] = set()
        for item in assignments:
            fields = {'profile_id','task','goal','deliverable','capabilities','depends_on'} if brief['outcome'] == 'project.task' else {'profile_id','task'}
            if not isinstance(item, Mapping) or set(item) != fields:
                raise AgentStewardProposalError("model assignment fields are invalid")
            profile_id, task = item["profile_id"], item["task"]
            if not isinstance(profile_id, str) or profile_id not in eligible or not isinstance(task, str):
                raise AgentStewardProposalError("model assignment profile or task is invalid")
            task = task.strip()
            normalized = re.sub(r"\s+", "", task)
            if not normalized or len(task) > 600 or normalized in seen:
                raise AgentStewardProposalError("model assignment tasks are empty, repeated or too long")
            seen.add(normalized)
            profile = self._profiles.get(profile_id)
            if profile is None or not profile.enabled:
                raise AgentStewardProposalError("model assignment profile is unavailable")
            selected.append(profile)
            tasks.append(task)
        _, main, main_request = self._durable_parent(steward_request)
        capacity = self._capacity(main)
        _, _, _, allowed = _main_task(main_request)
        allowed = self._task_allowed(allowed, brief['outcome'])
        if brief['outcome'] != 'project.task' and len(selected) > capacity.slots:
            raise AgentStewardProposalError("model assignments exceed current capacity")
        proposal = self._complete_profiles(main, capacity, allowed, selected, tasks, tasks, strict=True)
        if brief['outcome'] == 'project.task':
            from backend.shared.task_division_graph import validate_divisions
            for item, assignment in zip(assignments, proposal['assignments']):
                caps, deps = item['capabilities'], item['depends_on']
                if (not isinstance(caps,list) or not caps or any(not isinstance(cap,str) for cap in caps)
                        or len(caps) != len(set(caps)) or not set(caps).issubset(assignment['capability_ids'])):
                    raise AgentStewardProposalError('task capabilities exceed frozen subset')
                if not isinstance(deps,list) or any(type(index) is not int or not 1 <= index <= len(assignments) for index in deps):
                    raise AgentStewardProposalError('task dependencies are invalid')
                assignment['capability_ids'] = caps
                assignment['division'] = {'goal':item['goal'], 'deliverable':item['deliverable'],
                    'depends_on':[f'steward-assignment-{index}' for index in deps]}
                assignment['task'] += ('\n交付：' + str(item['deliverable'])
                    + '\n按本项需要召回本项目与我画像，遵守冻结隐私及预算。只新建草稿，不改已有资料。'
                    + '\ndocument.draft.propose 接收 title、markdown，可选 final_for。'
                    + '只有本次草稿已是本项最终交付物时，final_for 才填写上方交付物的完整原文；'
                    + '中间草稿和非草稿交付物省略 final_for，继续完成任务。'
                    + '最终草稿成功后自动结束，说明由标题及正文摘要生成，不再另调模型总结。')
            validate_divisions(proposal['assignments'], {item['assignment_id']:item['assignment_id'] for item in proposal['assignments']})
        return proposal

    def build(self, steward_request: Mapping[str, object]) -> dict[str, object]:
        brief = self.brief(steward_request)
        if brief.get('division_override') is not None:
            return self.from_override(steward_request)
        _, main, main_request = self._durable_parent(steward_request)
        capacity = self._capacity(main)
        task, refs, outcome, allowed = _main_task(main_request)
        allowed = self._task_allowed(allowed, outcome)
        complexity = _complexity(task, refs, outcome)
        world_action = brief["world_action"]
        if brief["slots"] < 1 or not brief["budget_usable"] or (complexity == 0 and not world_action):
            return {"mode": "main_only", "plan_id": "steward-main-only"}
        profiles = self._eligible_profiles(main, allowed, outcome, refs)
        if world_action:
            reviewer = next((profile for profile in profiles if profile.profile_id == "subagent.reviewer"), None)
            if reviewer is None:
                return {"mode": "main_only", "plan_id": "steward-main-only"}
            selected = [reviewer]
            if complexity >= 2 and capacity.slots >= 2:
                selected.extend(profile for profile in profiles if profile.profile_id != reviewer.profile_id)
                selected = selected[:2]
        else:
            selected = profiles[:min(2, capacity.slots, len(profiles), 2 if complexity >= 2 else 1)]
        if not selected:
            return {"mode": "main_only", "plan_id": "steward-main-only"}
        tasks = [_world_reviewer_task(task, outcome)
                 if world_action and profile.profile_id == "subagent.reviewer" else task
                 for profile in selected]
        # Legacy rules intentionally share the original task and retain their
        # historical text ceiling. Only provider selections use strict tasks.
        contexts = [f"{outcome} {task}".strip()] * len(selected)
        return self._complete_profiles(main, capacity, allowed, selected, tasks, contexts,
                                       strict=False, world_action=world_action)

    def _complete_profiles(
        self, main: AgentRun, capacity: _Capacity, allowed: set[str],
        selected: Sequence[AgentProfile], tasks: Sequence[str], contexts: Sequence[str],
        *, strict: bool, world_action: bool = False,
    ) -> dict[str, object]:
        budgets = _slice_budget(capacity.budget, tuple(item.budget_limit for item in selected), len(selected))
        expert = self._default_expert(main.project_id)
        enabled_skills = _enabled_skills(self._capabilities, main.project_id)
        assignments = []
        for index, profile in enumerate(selected, start=1):
            capabilities = _profile_capabilities(main, profile, allowed)
            if not capabilities or not _usable(budgets[index - 1]):
                if strict:
                    raise AgentStewardProposalError("model assignment has no usable authority or budget")
                continue
            applicable_expert = _expert_for_assignment(expert, capabilities, contexts[index - 1])
            assignments.append({
                "assignment_id": f"steward-assignment-{index}",
                "profile_id": profile.profile_id, "profile_revision": profile.revision,
                "task": tasks[index - 1], "budget": _budget_payload(budgets[index - 1]),
                "capability_ids": capabilities, "expert": applicable_expert,
                "skill": _skill_for_assignment(expert, applicable_expert, enabled_skills, contexts[index - 1]),
            })
        if not assignments or (world_action and not any(item["profile_id"] == "subagent.reviewer" for item in assignments)):
            return {"mode": "main_only", "plan_id": "steward-main-only"}
        return {"mode": "cluster", "plan_id": "steward-cluster", "cluster_id": "steward-experts", "assignments": assignments}

    def _durable_parent(self, request: Mapping[str, object]) -> tuple[AgentRun, AgentRun, Mapping[str, object]]:
        if not isinstance(request, Mapping) or request.get("desired_outcome") != "agent.steward.plan":
            raise AgentStewardProposalError("steward request outcome is invalid")
        binding = request.get("agent_binding")
        scope = request.get("scope")
        if not isinstance(binding, Mapping) or not isinstance(scope, Mapping):
            raise AgentStewardProposalError("verified steward binding is required")
        project_id, run_id = scope.get("project_id"), binding.get("run_id")
        if not isinstance(project_id, str) or not isinstance(run_id, str):
            raise AgentStewardProposalError("steward request identity is invalid")
        steward = self._store.get_run(run_id)
        if (
            steward is None or steward.project_id != project_id or steward.turn_id != request.get("turn_id")
            or steward.role != "subagent" or steward.profile_id != "steward.scheduler"
            or steward.parent_run_id is None
            or any(binding.get(key) != getattr(steward, key) for key in ("run_id", "profile_id", "profile_revision", "role", "parent_run_id", "depth", "cancel_epoch", "budget_snapshot_ref"))
        ):
            raise AgentStewardProposalError("steward request is not a verified durable child")
        main = self._store.get_run(steward.parent_run_id)
        if main is None or main.role != "main" or main.project_id != project_id or main.run_id != steward.parent_run_id:
            raise AgentStewardProposalError("steward durable main parent is unavailable")
        try:
            main_request = self._request_loader(main.turn_id)
        except Exception as error:
            raise AgentStewardProposalError("durable main request is unavailable") from error
        if not isinstance(main_request, Mapping) or _project(main_request) != project_id:
            raise AgentStewardProposalError("durable main request project drifted")
        return steward, main, main_request

    def _capacity(self, main: AgentRun) -> _Capacity:
        try:
            links = self._store.list_child_links(project_id=main.project_id, parent_run_id=main.run_id)
            reservations = self._store.list_reservations(project_id=main.project_id, parent_run_id=main.run_id)
        except Exception as error:
            raise AgentStewardProposalError("durable main workload is unavailable") from error
        occupied = sum(getattr(link, "status", None) in {"reserved", "spawned", "started", "cancelling"} for link in links)
        used = AgentBudget(0, 0, 0, 0, 0)
        for reservation in reservations:
            if getattr(reservation, "status", None) not in {"reserved", "settled"}: continue
            budget = getattr(reservation, "settled_budget", None) if getattr(reservation, "status", None) == "settled" else getattr(reservation, "reserved_budget", None)
            if not isinstance(budget, AgentBudget):
                raise AgentStewardProposalError("durable reservation budget is invalid")
            used = used.plus(budget)
        try:
            return _Capacity(max(0, main.max_concurrent_children - occupied), main.budget_limit.remaining_after(used))
        except Exception as error:
            raise AgentStewardProposalError("durable main budget is exhausted or drifted") from error

    def _eligible_profiles(self, main: AgentRun, allowed: set[str], outcome: str, refs: int) -> list[AgentProfile]:
        preferred = _profile_order(outcome, refs)
        result: list[AgentProfile] = []
        for profile_id in preferred:
            profile = self._profiles.get(profile_id)
            if (
                profile is not None and profile.enabled and profile.role == "subagent"
                and profile.profile_id == profile_id and profile.revision >= 1
                and _profile_capabilities(main, profile, allowed)
            ):
                result.append(profile)
        return result

    def _default_expert(self, project_id: str) -> Mapping[str, object] | None:
        if self._expert_catalog is None or self._expert_bindings is None:
            return None
        try:
            bindings = self._expert_bindings.list_for_project(project_id)
            candidates = []
            for binding in bindings:
                if not isinstance(binding, Mapping) or binding.get("default") is not True or binding.get("selection_mode") == "disabled": continue
                expert_id = binding.get("expert_id")
                expert = self._expert_catalog.get(expert_id) if isinstance(expert_id, str) else None
                if isinstance(expert, Mapping) and expert.get("status") == "active" and binding.get("enabled_expert_revision") == expert.get("revision"):
                    candidates.append(expert)
            if len(candidates) != 1: return None
            return dict(candidates[0])
        except Exception:
            return None


def _profile_capabilities(main: AgentRun, profile: AgentProfile, allowed: set[str]) -> list[str]:
    capabilities = set(main.capability_ids) & set(profile.capability_ids) & allowed
    if not profile.allow_child_spawn:
        capabilities.discard("agent.spawn")
    return sorted(capabilities)


def _main_task(request: Mapping[str, object]) -> tuple[str, int, str, set[str]]:
    input_value, policy, outcome = request.get("input"), request.get("capability_policy"), request.get("desired_outcome")
    if not isinstance(input_value, Mapping) or not isinstance(policy, Mapping) or not isinstance(outcome, str) or not outcome.strip():
        raise AgentStewardProposalError("durable main request is incomplete")
    task, refs, allowed = input_value.get("text"), input_value.get("refs"), policy.get("allowed")
    if not isinstance(task, str) or not task.strip() or len(task) > 16_000 or not isinstance(refs, list) or not isinstance(allowed, list):
        raise AgentStewardProposalError("durable main request fields are invalid")
    values = {item for item in allowed if isinstance(item, str)}
    if len(values) != len(allowed): raise AgentStewardProposalError("durable main capabilities are invalid")
    return task.strip(), len(refs), outcome.strip(), values


def _project(request: Mapping[str, object]) -> str | None:
    scope = request.get("scope")
    return scope.get("project_id") if isinstance(scope, Mapping) and isinstance(scope.get("project_id"), str) else None


def _complexity(task: str, refs: int, outcome: str) -> int:
    words = (task + " " + outcome).lower()
    if len(task) < 160 and refs == 0 and not any(token in words for token in ("research", "review", "analy", "draft", "write", "生成", "分析", "审")):
        return 0
    return 2 if refs >= 3 or len(task) >= 900 or any(token in words for token in ("research", "review", "analy", "compare", "审", "分析")) else 1


def _profile_order(outcome: str, refs: int) -> tuple[str, ...]:
    value = outcome.lower()
    if any(token in value for token in ("review", "validate", "audit")): return ("subagent.reviewer", "subagent.explorer", "subagent.worker")
    if any(token in value for token in ("draft", "write", "create", "generate")): return ("subagent.worker", "subagent.explorer", "subagent.reviewer")
    return ("subagent.explorer", "subagent.reviewer", "subagent.worker") if refs else ("subagent.worker", "subagent.explorer", "subagent.reviewer")


def _world_reviewer_task(task: str, outcome: str) -> str:
    prefix = (
        "Independently verify the World action hypothesis, expected outcome, "
        "falsification evidence, and stop conditions without requesting any "
        "additional authority. Final summary MUST be exactly one line: "
        "VERDICT=<supported|weakened|refuted|inconclusive>;"
        "DISPOSITION=<continue|replan_required|stop_required|escalate_user>;"
        "FINDING=<bounded text>. Do not include receipt, ref, path, or secret. "
    )
    expected = f"Expected outcome: {outcome[:1200]}. Task: "
    return prefix + expected + task[: max(1, 16_000 - len(prefix) - len(expected))]


def _slice_budget(remaining: AgentBudget, profiles: tuple[AgentBudget, ...], count: int) -> tuple[AgentBudget, ...]:
    fields = ("model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms")
    result = []
    for profile in profiles:
        result.append(AgentBudget(**{field: min(getattr(profile, field), getattr(remaining, field) // count) for field in fields}))
    return tuple(result)


def _usable(value: AgentBudget) -> bool:
    return (
        value.model_calls > 0
        and value.input_tokens > 0
        and value.output_tokens > 0
        and value.wall_time_ms > 0
    )


def _budget_payload(value: AgentBudget) -> dict[str, int]:
    return {field: getattr(value, field) for field in ("model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms")}


def _enabled_skills(store: ProjectCapabilityReader, project_id: str) -> set[str]:
    try:
        snapshot = store.get(project_id)
        profile = getattr(snapshot, "profile", snapshot)
        values = getattr(profile, "enabled_skill_ids", ())
        return {item for item in values if isinstance(item, str)}
    except Exception:
        return set()


def _expert_for_assignment(expert: Mapping[str, object] | None, capabilities: Sequence[str], outcome: str) -> dict[str, object] | None:
    if expert is None: return None
    expert_id, tools, intents = expert.get("expert_id"), expert.get("tools"), expert.get("applicable_tasks")
    if not isinstance(expert_id, str) or not isinstance(tools, list) or not isinstance(intents, list): return None
    tool_ids = {item for item in tools if isinstance(item, str)}
    task_intents = [item for item in intents if isinstance(item, str)][:3]
    if not task_intents or not tool_ids.issubset(set(capabilities)) or not _task_matches(task_intents, outcome): return None
    return {"expert_id": expert_id, "task_intents": task_intents, "budget": "standard"}


def _skill_for_assignment(record: Mapping[str, object] | None, expert: Mapping[str, object] | None, enabled: set[str], outcome: str) -> dict[str, object] | None:
    if record is None or expert is None or not _task_matches(expert.get("task_intents", ()), outcome): return None
    values = record.get("skills")
    if not isinstance(values, list): return None
    skill_ids = []
    for value in values:
        skill_id = value.get("skill_id") if isinstance(value, Mapping) else value
        if isinstance(skill_id, str) and skill_id in enabled and skill_id not in skill_ids:
            skill_ids.append(skill_id)
    return {"skill_ids": skill_ids[:3]} if skill_ids else None


def _task_matches(intents: object, outcome: str) -> bool:
    if not isinstance(intents, Sequence) or isinstance(intents, str): return False
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", outcome.casefold())
    return any(
        isinstance(item, str)
        and bool(item.strip())
        and re.sub(r"[^\w\u4e00-\u9fff]+", " ", item.casefold()).strip() in normalized
        for item in intents
    )


def is_valid_steward_proposal(value: Mapping[str, object]) -> bool:
    """Check only the provider-facing DTO shape; dispatch revalidates authority facts."""

    if not isinstance(value, Mapping):
        return False
    mode, plan_id = value.get("mode"), value.get("plan_id")
    if not isinstance(plan_id, str) or _IDENTITY.fullmatch(plan_id) is None:
        return False
    if mode == "main_only":
        return set(value) == {"mode", "plan_id"}
    if mode != "cluster" or set(value) != {"mode", "plan_id", "cluster_id", "assignments"}:
        return False
    cluster_id, assignments = value.get("cluster_id"), value.get("assignments")
    if (
        not isinstance(cluster_id, str) or _IDENTITY.fullmatch(cluster_id) is None
        or not isinstance(assignments, list) or not 1 <= len(assignments) <= (8 if all(isinstance(item, Mapping) and 'division' in item for item in assignments) else 2)
    ):
        return False
    required = {
        "assignment_id", "profile_id", "profile_revision", "task", "budget",
        "capability_ids", "expert", "skill",
    }
    for item in assignments:
        if not isinstance(item, Mapping) or set(item) - {'division'} != required:
            return False
        if any(
            not isinstance(item.get(field), str)
            or _IDENTITY.fullmatch(str(item[field])) is None
            for field in ("assignment_id", "profile_id")
        ):
            return False
        if not isinstance(item.get("profile_revision"), int) or isinstance(item.get("profile_revision"), bool) or int(item["profile_revision"]) < 1:
            return False
        if not isinstance(item.get("task"), str) or not str(item["task"]).strip() or len(str(item["task"])) > 16_000:
            return False
        budget, capability_ids = item.get("budget"), item.get("capability_ids")
        if not isinstance(budget, Mapping) or set(budget) != {"model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms"}:
            return False
        if any(not isinstance(amount, int) or isinstance(amount, bool) or amount < 0 for amount in budget.values()):
            return False
        if not isinstance(capability_ids, list) or not capability_ids:
            return False
        if any(not isinstance(capability, str) or _IDENTITY.fullmatch(capability) is None for capability in capability_ids):
            return False
        if len(capability_ids) != len(set(capability_ids)):
            return False
        expert, skill = item.get("expert"), item.get("skill")
        if expert is not None and (
            not isinstance(expert, Mapping)
            or set(expert) != {"expert_id", "task_intents", "budget"}
            or not isinstance(expert.get("expert_id"), str)
            or not isinstance(expert.get("task_intents"), list)
            or not expert.get("task_intents")
            or any(not isinstance(intent, str) or not intent.strip() for intent in expert.get("task_intents", ()))
            or not isinstance(expert.get("budget"), str)
            or not str(expert.get("budget")).strip()
        ):
            return False
        if skill is not None and (
            not isinstance(skill, Mapping)
            or set(skill) != {"skill_ids"}
            or not isinstance(skill.get("skill_ids"), list)
            or not skill.get("skill_ids")
            or any(not isinstance(skill_id, str) or _IDENTITY.fullmatch(skill_id) is None for skill_id in skill.get("skill_ids", ()))
        ):
            return False
        if isinstance(skill, Mapping) and len(skill.get("skill_ids", ())) != len(set(skill.get("skill_ids", ()))):
            return False
    return True
