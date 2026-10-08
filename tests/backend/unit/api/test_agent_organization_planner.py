from __future__ import annotations

from backend.api.agent_organization_planner import (
    AgentRoleDispatchPlanner, MainCoordinationPlanner, StewardPlanningPlanner,
)


class _Planner:
    def __init__(self, result=None, error: Exception | None = None): self.result, self.error, self.calls = result, error, 0
    def plan(self, *_args):
        self.calls += 1
        if self.error: raise self.error
        return self.result


class _Payloads:
    def __init__(self, values): self.values, self.refs = values, []
    def get(self, ref): self.refs.append(ref); return self.values[ref]


def _request(profile=None):
    value = {"turn_id": "turn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
    if profile is not None: value["agent_binding"] = profile
    return value


def _completed(capability, ref):
    return {"type": "tool.completed", "data": {"capability_id": capability, "payload_ref": ref}}


def _ref(kind): return "crp://session/turn-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/" + kind


def test_role_dispatch_is_exact_and_leaves_ordinary_turns_to_delegate():
    delegate, steward, main = _Planner({"from": "delegate"}), _Planner({"from": "steward"}), _Planner({"from": "main"})
    planner = AgentRoleDispatchPlanner(delegate=delegate, steward=steward, main=main)
    assert planner.plan(_request(), (), (), _Payloads({})) == {"from": "delegate"}
    assert planner.plan(_request({"profile_id": "main.orchestrator", "role": "main", "depth": 0}), (), (), _Payloads({})) == {"from": "main"}
    assert planner.plan(_request({"profile_id": "steward.scheduler", "role": "subagent", "depth": 1}), (), (), _Payloads({})) == {"from": "steward"}
    assert planner.plan(_request({"profile_id": "subagent.worker", "role": "subagent", "depth": 1}), (), (), _Payloads({})) == {"from": "delegate"}


def test_steward_uses_only_valid_remote_plan_then_stops_after_completion():
    proposal = {"mode": "main_only", "plan_id": "plan-1"}
    remote = _Planner({"type": "tool", "capability_id": "agent.plan", "arguments": proposal})
    planner = StewardPlanningPlanner(remote=remote, proposal_validator=lambda value: value == proposal, proposal_builder=lambda *_: (_ for _ in ()).throw(AssertionError("fallback")))
    assert planner.plan(_request(), (), (), _Payloads({})) == {"type": "tool", "capability_id": "agent.plan", "arguments": proposal}
    result_ref = _ref("plan-result")
    assert planner.plan(_request(), (_completed("agent.plan", result_ref),), (), _Payloads({result_ref: {"plan_id": "plan-1"}})) ["type"] == "complete"


def test_steward_rejects_wrong_remote_tool_and_never_continues_after_other_tool():
    fallback = {"mode": "main_only", "plan_id": "local-plan"}
    planner = StewardPlanningPlanner(remote=_Planner({"type": "tool", "capability_id": "agent.list", "arguments": {}}), proposal_validator=lambda value: value == fallback, proposal_builder=lambda *_: fallback)
    assert planner.plan(_request(), (), (), _Payloads({}))["arguments"] == fallback
    ref = _ref("other")
    assert planner.plan(_request(), (_completed("agent.list", ref),), (), _Payloads({ref: {}})) ["type"] == "complete"


def test_main_waits_for_dispatched_cluster_experts_then_synthesizes():
    list_ref, wait_ref, final_ref = _ref("list-1"), _ref("wait"), _ref("list-2")
    plan = {"plan_id": "plan-1", "mode": "cluster", "status": "dispatched", "revision": 4, "steward_run_id": "steward"}
    active = {"children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "completed"}, {"run_id": "expert", "profile_id": "subagent.worker", "status": "started"}], "plans": [plan], "fan_ins": []}
    done = {"children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "completed"}, {"run_id": "expert", "profile_id": "subagent.worker", "status": "completed"}], "plans": [plan], "fan_ins": [{"status": "completed", "child_run_ids": ["expert"], "result": {"status": "completed"}}]}
    payloads = _Payloads({list_ref: active, wait_ref: {}, final_ref: done})
    planner = MainCoordinationPlanner(remote=None)
    assert planner.plan(_request(), (), (), payloads)["capability_id"] == "agent.list"
    assert planner.plan(_request(), (_completed("agent.list", list_ref),), (), payloads)["capability_id"] == "agent.wait"
    assert planner.plan(_request(), (_completed("agent.list", list_ref), _completed("agent.wait", wait_ref)), (), payloads)["capability_id"] == "agent.list"
    assert planner.plan(_request(), (_completed("agent.list", list_ref), _completed("agent.wait", wait_ref), _completed("agent.list", final_ref)), (), payloads)["type"] == "complete"


def test_main_uses_remote_only_after_completed_main_only_plan_and_falls_back_on_failure():
    ref = _ref("topology")
    topology = {
        "children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "completed"}],
        "plans": [{"plan_id": "plan-1", "mode": "main_only", "status": "completed", "revision": 4, "steward_run_id": "steward"}],
        "fan_ins": [{"status": "completed", "result": {"status": "completed"}}],
    }
    remote = _Planner({"type": "complete", "summary": "synthesis", "payload_ref": None, "evidence_refs": []})
    planner = MainCoordinationPlanner(remote=remote)
    assert planner.plan(_request(), (_completed("agent.list", ref),), (), _Payloads({ref: topology}))["summary"] == "synthesis"
    fallback = MainCoordinationPlanner(remote=_Planner(error=RuntimeError("offline")))
    assert fallback.plan(_request(), (_completed("agent.list", ref),), (), _Payloads({ref: topology}))["type"] == "complete"


def test_planners_never_read_cross_turn_tool_payload_refs():
    foreign = "crp://session/turn-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/list"
    payloads = _Payloads({foreign: {"children": []}})
    decision = MainCoordinationPlanner(remote=None).plan(_request(), (_completed("agent.list", foreign),), (), payloads)
    assert decision["capability_id"] == "agent.list" and payloads.refs == []


def test_main_never_synthesizes_before_steward_plan_is_visible_and_progressed():
    pending_ref, missing_ref, ready_ref, dispatched_ref = (
        _ref("pending"), _ref("missing"), _ref("ready"), _ref("dispatched"),
    )
    planner = MainCoordinationPlanner(remote=_Planner({"type": "complete", "summary": "unsafe"}))
    pending = {"children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "started"}], "plans": []}
    missing = {"children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "completed"}], "plans": []}
    ready = {"children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "completed"}], "plans": [{"plan_id": "plan", "mode": "cluster", "status": "ready", "revision": 2, "steward_run_id": "steward"}]}
    dispatched_without_expert = {"children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "completed"}], "plans": [{"plan_id": "plan", "mode": "cluster", "status": "dispatched", "revision": 4, "steward_run_id": "steward"}]}
    payloads = _Payloads({pending_ref: pending, missing_ref: missing, ready_ref: ready, dispatched_ref: dispatched_without_expert})
    assert planner.plan(_request(), (_completed("agent.list", pending_ref),), (), payloads)["capability_id"] == "agent.wait"
    assert planner.plan(_request(), (_completed("agent.list", missing_ref),), (), payloads)["capability_id"] == "agent.list"
    assert planner.plan(_request(), (_completed("agent.list", ready_ref),), (), payloads)["capability_id"] == "agent.list"
    assert planner.plan(_request(), (_completed("agent.list", dispatched_ref),), (), payloads)["capability_id"] == "agent.list"


def test_main_requires_parent_owned_fan_in_after_experts_turn_terminal() -> None:
    no_fan_ref, with_fan_ref = _ref("terminal-no-fan-in"), _ref("terminal-with-fan-in")
    children = [
        {"run_id": "steward", "profile_id": "steward.scheduler", "status": "completed"},
        {"run_id": "expert", "profile_id": "subagent.worker", "status": "completed"},
    ]
    plan = {"plan_id": "plan", "mode": "cluster", "status": "dispatched", "revision": 4, "steward_run_id": "steward"}
    no_fan = {"children": children, "plans": [plan], "fan_ins": []}
    with_fan = {
        "children": children,
        "plans": [plan],
        "fan_ins": [{"status": "failed", "child_run_ids": ["expert"], "result": {"status": "failed"}}],
    }
    payloads = _Payloads({no_fan_ref: no_fan, with_fan_ref: with_fan})
    planner = MainCoordinationPlanner(remote=None)
    assert planner.plan(_request(), (_completed("agent.list", no_fan_ref),), (), payloads)["capability_id"] == "agent.list"
    assert planner.plan(_request(), (_completed("agent.list", with_fan_ref),), (), payloads)["type"] == "complete"


def test_main_falls_back_without_cluster_when_steward_failed_before_plan() -> None:
    ref = _ref("failed-steward")
    topology = {
        "children": [{"run_id": "steward", "profile_id": "steward.scheduler", "status": "failed"}],
        "plans": [], "fan_ins": [],
    }
    decision = MainCoordinationPlanner(remote=None).plan(
        _request(), (_completed("agent.list", ref),), (),
        _Payloads({ref: topology}),
    )
    assert decision["type"] == "complete"
    assert "steward fallback=failed" in decision["summary"]
