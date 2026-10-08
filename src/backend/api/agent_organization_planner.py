"""Role-bound control planners for the governed Agent organization path.

These planners only select the next Kernel decision.  They never manufacture
operation ids, read another Turn's payloads, or acquire dispatch authority.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from backend.shared.llm.model_transport import ModelContinuationFailed, ModelInterrupted


_TERMINAL = frozenset({"completed", "failed", "cancelled", "timed_out"})
_STEWARD_PROFILE = "steward.scheduler"


class TurnPlanner(Protocol):
    def plan(
        self, request: Mapping[str, object], events: Sequence[Mapping[str, object]],
        capabilities: Sequence[object], payloads: object, execution_control: object | None = None,
    ) -> Mapping[str, object]: ...


class AgentRoleDispatchPlanner:
    """Route only verified main and steward bindings away from the generic planner."""

    def __init__(self, *, delegate: TurnPlanner, steward: TurnPlanner, main: TurnPlanner) -> None:
        self._delegate, self._steward, self._main = delegate, steward, main

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        binding = request.get("agent_binding") if isinstance(request, Mapping) else None
        if not isinstance(binding, Mapping):
            return self._delegate.plan(request, events, capabilities, payloads, execution_control)
        profile_id, role, depth = binding.get("profile_id"), binding.get("role"), binding.get("depth")
        if profile_id == "main.orchestrator" and role == "main" and depth == 0:
            return self._main.plan(request, events, capabilities, payloads, execution_control)
        if profile_id == _STEWARD_PROFILE and role == "subagent" and isinstance(depth, int) and not isinstance(depth, bool) and depth >= 1:
            return self._steward.plan(request, events, capabilities, payloads, execution_control)
        return self._delegate.plan(request, events, capabilities, payloads, execution_control)


class StewardPlanningPlanner:
    """Permit exactly one validated steward plan, then terminate the Turn."""

    def __init__(
        self, *, remote: TurnPlanner | None,
        proposal_validator: Callable[[Mapping[str, object]], bool],
        proposal_builder: Callable[[Mapping[str, object], Sequence[Mapping[str, object]], Sequence[object], object], Mapping[str, object]],
    ) -> None:
        self._remote = remote
        self._proposal_validator = proposal_validator
        self._proposal_builder = proposal_builder

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        completed = _completed_tools(request, events, payloads)
        if completed:
            if any(capability != "agent.plan" for capability, _ in completed):
                return _complete("steward planning stopped after an unsupported tool")
            return _complete("steward plan published")
        remote_proposal: Mapping[str, object] | None = None
        if self._remote is not None:
            try:
                candidate = self._remote.plan(request, events, capabilities, payloads, execution_control)
                if (
                    isinstance(candidate, Mapping)
                    and candidate.get("type") == "tool"
                    and candidate.get("capability_id") == "agent.plan"
                    and isinstance(candidate.get("arguments"), Mapping)
                ):
                    remote_proposal = dict(candidate["arguments"])
            except ModelInterrupted:
                raise
            except Exception:
                remote_proposal = None
        proposal = remote_proposal if remote_proposal is not None and self._proposal_validator(remote_proposal) else self._proposal_builder(request, events, capabilities, payloads)
        if not isinstance(proposal, Mapping) or not self._proposal_validator(proposal):
            raise ValueError("deterministic steward proposal is invalid")
        return {"type": "tool", "capability_id": "agent.plan", "arguments": dict(proposal)}


class MainCoordinationPlanner:
    """Use list/wait as deterministic control before any main synthesis."""

    def __init__(self, *, remote: TurnPlanner | None, wait_timeout_ms: int = 120_000) -> None:
        if not isinstance(wait_timeout_ms, int) or isinstance(wait_timeout_ms, bool) or not 1 <= wait_timeout_ms <= 120_000:
            raise ValueError("main coordination wait timeout is invalid")
        self._remote, self._wait_timeout_ms = remote, wait_timeout_ms

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        lists = [value for capability, value in _completed_tools(request, events, payloads) if capability == "agent.list"]
        if not lists:
            return _tool("agent.list", {"include_messages": False})
        latest = lists[-1]
        children = latest.get("children") if isinstance(latest, Mapping) else None
        children = list(children) if isinstance(children, Sequence) and not isinstance(children, (str, bytes, bytearray)) else []
        stewards = [item for item in children if isinstance(item, Mapping) and item.get("profile_id") == _STEWARD_PROFILE]
        pending_stewards = [
            str(item["run_id"]) for item in stewards
            if isinstance(item.get("run_id"), str) and item.get("status") not in _TERMINAL
        ]
        if pending_stewards:
            return self._wait_or_refresh(request, events, payloads, pending_stewards)
        # A terminal steward is not itself evidence that its dispatch decision
        # was persisted or progressed.  Do not let main synthesis race the
        # terminal observer: it must see exactly one durable plan first.
        plans = latest.get("plans") if isinstance(latest, Mapping) else None
        plans = list(plans) if isinstance(plans, Sequence) and not isinstance(plans, (str, bytes, bytearray)) else []
        if (
            len(stewards) == 1
            and stewards[0].get("status") in (_TERMINAL - {"completed"})
            and not plans
        ):
            # A failed steward cannot authorize a cluster.  Main may still
            # finish the original task under its own frozen capabilities.
            fallback_topology = dict(latest) if isinstance(latest, Mapping) else {}
            fallback_topology["steward_fallback_status"] = stewards[0].get("status")
            return self._synthesize(
                request, events, capabilities, payloads, execution_control,
                (), fallback_topology,
            )
        if len(stewards) != 1 or len(plans) != 1:
            return _tool("agent.list", {"include_messages": False})
        plan = plans[0] if isinstance(plans[0], Mapping) else None
        if plan is None or plan.get("steward_run_id") != stewards[0].get("run_id"):
            return _tool("agent.list", {"include_messages": False})
        mode, plan_status = plan.get("mode"), plan.get("status")
        if mode == "main_only":
            if plan_status != "completed":
                return _tool("agent.list", {"include_messages": False})
            return self._synthesize(request, events, capabilities, payloads, execution_control, (), latest)
        if mode != "cluster" or plan_status not in {"ready", "dispatching", "dispatched", "completed", "failed"}:
            return _tool("agent.list", {"include_messages": False})
        experts = [item for item in children if isinstance(item, Mapping) and item.get("profile_id") != _STEWARD_PROFILE]
        if not experts:
            return _tool("agent.list", {"include_messages": False})
        pending = [str(item["run_id"]) for item in experts if isinstance(item.get("run_id"), str) and item.get("status") not in _TERMINAL]
        if pending:
            return self._wait_or_refresh(request, events, payloads, pending)
        if plan_status in {"ready", "dispatching"}:
            return _tool("agent.list", {"include_messages": False})
        expert_ids = {
            str(item["run_id"]) for item in experts if isinstance(item.get("run_id"), str)
        }
        if not _has_terminal_fan_in(latest, expert_ids):
            # Child Run convergence and fan-in completion are separate durable
            # writes.  Raw terminal child states are not yet safe synthesis
            # evidence, so refresh until the parent-owned fan-in is visible.
            return _tool("agent.list", {"include_messages": False})
        return self._synthesize(request, events, capabilities, payloads, execution_control, experts, latest)

    def _wait_or_refresh(self, request, events, payloads, child_run_ids):
        last_list_index = _last_completed_index(events, "agent.list")
        if _completed_after(request, events, payloads, "agent.wait", last_list_index):
            return _tool("agent.list", {"include_messages": False})
        return _tool("agent.wait", {"child_run_ids": child_run_ids, "timeout_ms": self._wait_timeout_ms})

    def _synthesize(self, request, events, capabilities, payloads, execution_control, experts, topology):
        # A completed fan-in is a durable, safe indication that result summaries
        # are ready. Main-only and no-expert cases may still use remote synthesis.
        privacy = request.get("privacy")
        allow_remote = isinstance(privacy, Mapping) and privacy.get("allow_remote") is True
        last_list = _last_completed_index(events, "agent.list")
        synthesis_events = [event for index, event in enumerate(events)
            if not (str(event.get("type", "")).startswith("tool.")
                    and isinstance(event.get("data"), Mapping)
                    and event["data"].get("capability_id") in {"agent.list", "agent.wait"})
            or index == last_list]
        if self._remote is not None and (not experts or allow_remote):
            try:
                decision = self._remote.plan(request, synthesis_events, capabilities, payloads, execution_control)
                if isinstance(decision, Mapping):
                    return dict(decision)
            except (ModelInterrupted, ModelContinuationFailed):
                raise
            except Exception:
                pass
        if experts:
            conclusions = {}
            for fan_in in topology.get("fan_ins", ()):
                result = fan_in.get("result") if isinstance(fan_in, Mapping) else None
                if isinstance(result, Mapping):
                    for child in result.get("children", ()):
                        if isinstance(child, Mapping):
                            conclusions[child.get("child_run_id")] = child.get("conclusion")
            paragraphs = ["专家结论："]
            for expert in experts:
                role = expert.get("organization_role") or expert.get("profile_id")
                conclusion = conclusions.get(expert.get("run_id"))
                if not isinstance(conclusion, str) or not conclusion.strip():
                    conclusion = f"未返回结论（{expert.get('status')}）"
                paragraphs.append(f"【{role}】{conclusion}")
            return _complete("\n\n".join(paragraphs)[:6000])
        statuses = _status_counts(experts)
        fan_ins = topology.get("fan_ins") if isinstance(topology, Mapping) else None
        completed_fan_ins = sum(
            1 for item in fan_ins
            if isinstance(item, Mapping) and item.get("status") == "completed" and isinstance(item.get("result"), Mapping)
        ) if isinstance(fan_ins, Sequence) and not isinstance(fan_ins, (str, bytes, bytearray)) else 0
        summary = "main coordination completed"
        if statuses:
            summary += "; expert status " + ", ".join(f"{key}={value}" for key, value in sorted(statuses.items()))
        if completed_fan_ins:
            summary += f"; completed fan-ins={completed_fan_ins}"
        fallback_status = topology.get("steward_fallback_status") if isinstance(topology, Mapping) else None
        if isinstance(fallback_status, str):
            summary += f"; steward fallback={fallback_status}"
        return _complete(summary)


def _completed_tools(request: Mapping[str, object], events: Sequence[Mapping[str, object]], payloads: object) -> list[tuple[str, Mapping[str, object]]]:
    values: list[tuple[str, Mapping[str, object]]] = []
    for event in events:
        if event.get("type") != "tool.completed":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("capability_id"), str):
            continue
        result = _same_turn_tool_result(request, data, payloads)
        if result is not None:
            values.append((str(data["capability_id"]), result))
    return values


def _same_turn_tool_result(request: Mapping[str, object], data: Mapping[str, object], payloads: object) -> Mapping[str, object] | None:
    turn_id, ref = request.get("turn_id"), data.get("payload_ref")
    if not isinstance(turn_id, str) or not isinstance(ref, str) or not ref.startswith(f"crp://session/{turn_id}/"):
        return None
    getter = getattr(payloads, "get", None)
    if not callable(getter):
        return None
    try:
        payload = getter(ref)
    except Exception:
        return None
    if not isinstance(payload, Mapping):
        return None
    # The Kernel stores the provider's inner ``result`` object as the
    # turn-scoped ``tool-result`` payload and points ``tool.completed`` at that
    # object.  Requiring another synthetic ``result`` wrapper made every
    # production organization tool look incomplete and caused repeated plan /
    # list calls until the global step limit was exhausted.
    return dict(payload)


def _last_completed_index(events: Sequence[Mapping[str, object]], capability_id: str) -> int:
    return max((index for index, event in enumerate(events) if event.get("type") == "tool.completed" and isinstance(event.get("data"), Mapping) and event["data"].get("capability_id") == capability_id), default=-1)


def _completed_after(request, events, payloads, capability_id: str, index: int) -> bool:
    return any(capability == capability_id for capability, _ in _completed_tools(request, events[index + 1:], payloads))


def _status_counts(children: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child in children:
        status = child.get("status")
        if isinstance(status, str): counts[status] = counts.get(status, 0) + 1
    return counts


def _has_terminal_fan_in(topology: Mapping[str, object], expert_ids: set[str]) -> bool:
    fan_ins = topology.get("fan_ins")
    if not isinstance(fan_ins, Sequence) or isinstance(fan_ins, (str, bytes, bytearray)):
        return False
    matching = []
    for fan_in in fan_ins:
        if not isinstance(fan_in, Mapping):
            continue
        child_ids = fan_in.get("child_run_ids")
        result = fan_in.get("result")
        if (
            isinstance(child_ids, Sequence)
            and not isinstance(child_ids, (str, bytes, bytearray))
            and set(child_ids) == expert_ids
            and fan_in.get("status") in _TERMINAL
            and isinstance(result, Mapping)
            and result.get("status") in _TERMINAL
        ):
            matching.append(fan_in)
    return len(matching) == 1


def _tool(capability_id: str, arguments: Mapping[str, object]) -> dict[str, object]:
    return {"type": "tool", "capability_id": capability_id, "arguments": dict(arguments)}


def _complete(summary: str) -> dict[str, object]:
    return {"type": "complete", "summary": summary, "payload_ref": None, "evidence_refs": []}
