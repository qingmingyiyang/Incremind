from types import SimpleNamespace
import json
import pytest
from backend.api.agent_organization_planner import StewardPlanningPlanner
from backend.api.agent_steward_proposal import is_valid_steward_proposal
from tests.backend.unit.api.test_agent_steward_proposal import _builder, _request, _main_request

class Payloads:
    def get_immutable_payload(self, turn_id, kind):
        if kind != "agent-role-brief-v1": return None
        return "crp://role", {"schema_version":"1.0.0", "kind":"agent.role-brief.v1", "profile_id":"steward.scheduler", "profile_revision":1, "organization_role":"steward", "work_description":"Plan distinct work", "instructions":"Frozen steward instructions"}

class Gateway:
    def __init__(self, output): self.output=output; self.requests=[]
    def invoke(self, request):
        self.requests.append(request)
        if isinstance(self.output, Exception): raise self.output
        return SimpleNamespace(output=self.output)

def planner(gateway, builder):
    from backend.api.agent_steward_decomposition import StewardDecompositionPlanner
    return StewardPlanningPlanner(remote=StewardDecompositionPlanner(gateway, builder), proposal_validator=is_valid_steward_proposal, proposal_builder=lambda request,*_:builder.build(request))

def test_distinct_tasks_and_frozen_instructions_reach_gateway():
    builder=_builder(); profiles=builder.brief(_request())["profiles"]
    selection={"mode":"cluster","assignments":[{"profile_id":p["profile_id"],"task":task} for p,task in zip(profiles, ["research evidence", "review conclusions"])]}
    gateway=Gateway(selection); request=_request() | {"privacy":{"allow_remote":True,"mode":"remote_allowed"}}
    result=planner(gateway,builder).plan(request,[],[],Payloads())
    assert [a["task"] for a in result["arguments"]["assignments"]]==["research evidence","review conclusions"]
    prompt=json.loads(gateway.requests[0].input)
    assert prompt["instruction"]=="Frozen steward instructions"
    assert prompt["profiles"]==profiles
    assert gateway.requests[0].privacy_scope=="remote_allowed"

@pytest.mark.parametrize("output", ["not json",RuntimeError("offline"),{"mode":"cluster","assignments":[]},{"mode":"cluster","assignments":[{"profile_id":"unknown","task":"task"}]},{"mode":"cluster","assignments":[{"profile_id":"subagent.explorer","task":"x"*601}]},{"mode":"cluster","assignments":[{"profile_id":"subagent.explorer","task":"same"},{"profile_id":"subagent.reviewer","task":"s a m e"}]}])
def test_invalid_provider_selection_falls_back_exactly(output):
    builder=_builder(); request=_request() | {"privacy":{"allow_remote":True,"mode":"remote_allowed"}}
    assert planner(Gateway(output),builder).plan(request,[],[],Payloads())["arguments"]==builder.build(request)

def test_main_only_selection():
    builder=_builder(); request=_request() | {"privacy":{"allow_remote":True,"mode":"remote_allowed"}}
    assert planner(Gateway({"mode":"main_only"}),builder).plan(request,[],[],Payloads())["arguments"]=={"mode":"main_only","plan_id":"steward-main-only"}

@pytest.mark.parametrize("world",[False,True])
def test_local_and_world_paths_do_not_invoke_gateway(world):
    from backend.api.workbench_ai_runtime import WORLD_PROJECT_SESSION_ID
    builder=_builder(request=_main_request() | ({"session_id":WORLD_PROJECT_SESSION_ID} if world else {}))
    request=_request() | {"privacy":{"allow_remote":world,"mode":"remote_allowed" if world else "local_only"}}
    gateway=Gateway({"mode":"main_only"})
    assert planner(gateway,builder).plan(request,[],[],Payloads())["arguments"]==builder.build(request)
    assert gateway.requests==[]


@pytest.mark.parametrize("model_selection", [False, True])
def test_real_worker_permit_preserves_frozen_authority_and_parent_refs(tmp_path, model_selection):
    from tests.backend.unit.api.test_agent_organization_e2e import _organization, _request as organization_request
    from backend.api.agent_steward_proposal import AgentStewardProposalBuilder
    from tests.backend.unit.api.test_agent_steward_proposal import _Capabilities
    composition, runner, organization = _organization(tmp_path)
    request = organization_request(suffix="worker-permit")
    request["desired_outcome"] = "draft.generate"
    request["input"]["text"] = "Research evidence and draft a comparison of three approaches with reviewed constraints."
    request["input"]["refs"] = [{"uri":"crp://source/parent-evidence", "kind":"source", "object_id":"parent-evidence"}]
    request["capability_policy"]["allowed"] += ["agent.spawn"]
    started = organization.start(request, agent_turn_mode=True)
    steward_request = composition.request_loader(started["steward"]["turn_id"])
    builder = AgentStewardProposalBuilder(profiles=composition.profiles, agent_store=composition.store, request_loader=composition.request_loader, capability_profiles=_Capabilities())
    proposal = builder.build(steward_request)
    if model_selection:
        selected = builder.brief(steward_request)["profiles"][:2]
        proposal = builder.complete(steward_request, {"mode":"cluster", "assignments":[{"profile_id":profile["profile_id"], "task":task} for profile,task in zip(selected, ["Research evidence independently", "Review implementation constraints"])]})
    worker = next(a for a in proposal["assignments"] if a["profile_id"]=="subagent.worker")
    assert "agent.spawn" not in worker["capability_ids"]
    composition.coordinator.plan(parent_turn_id=steward_request["turn_id"], operation_id="freeze-worker-plan", project_id="project-alpha", scope=steward_request["scope"], privacy=steward_request["privacy"], arguments=proposal)
    permits = composition.dispatch_store.list_permits(project_id="project-alpha")
    prepared = [composition.coordinator.prepare_child_from_permit(project_id="project-alpha",permit_id=p.permit_id,operation_id="prepare-"+p.permit_id) for p in permits]
    assert len(prepared)==len(proposal["assignments"])
    for child, assignment in zip(prepared, proposal["assignments"]):
        assert tuple(child.run.capability_ids)==tuple(assignment["capability_ids"])
        assert not child.run.allow_child_spawn
        assert child.request["input"]["refs"]==request["input"]["refs"]
        assert child.request["input"]["text"]==assignment["task"]



@pytest.mark.parametrize("use_research_request", [False, True])
def test_production_organization_synthesizes_both_distinct_expert_conclusions(tmp_path, monkeypatch, use_research_request):
    from dataclasses import replace
    from backend.api import ai_runtime
    from tests.backend.unit.api.test_agent_organization_e2e import _request as organization_request
    requests, decomposition_errors = [], []
    from backend.api.agent_steward_decomposition import StewardDecompositionPlanner
    decompose = StewardDecompositionPlanner.plan
    def capture_decomposition(self, *args, **kwargs):
        try:
            return decompose(self, *args, **kwargs)
        except Exception as error:
            decomposition_errors.append(str(error))
            raise
    monkeypatch.setattr(StewardDecompositionPlanner, "plan", capture_decomposition)
    class OrganizationGateway:
        def invoke(self, request):
            prompt = json.loads(request.input)
            requests.append(prompt)
            if "profiles" in prompt:
                output = {"mode":"cluster", "assignments":[{"profile_id":p["profile_id"], "task":task} for p,task in zip(prompt["profiles"], ["research evidence independently", "review risks independently"])]}
            else:
                role = prompt.get("role", {}).get("organization_role")
                text = prompt.get("input", {}).get("text", "")
                summary = "evidence conclusion alpha" if "research evidence independently" in text else "risk conclusion beta"
                if role == "主政协调": summary = "Main synthesized both expert conclusions"
                output = {"type":"complete", "summary":summary, "payload_ref":None,"evidence_refs":[]}
            return SimpleNamespace(output=output)
    gateway = OrganizationGateway()
    original = ai_runtime.resolve_model_gateway_runtime
    def resolve(*args, **kwargs):
        resolution = original(*args, **kwargs)
        return replace(resolution, gateway=gateway, egress_consented=True, enabled=True)
    monkeypatch.setattr(ai_runtime, "resolve_model_gateway_runtime", resolve)
    application = SimpleNamespace(state=SimpleNamespace())
    ai_runtime.get_or_build_ai_runtime(SimpleNamespace(app=application), SimpleNamespace(root_dir=tmp_path))
    runner=application.state.ai_turn_runner
    request=organization_request(suffix="distinct-conclusions")
    request.update(turn_id="turn-0123456789abcdef0123456789abcdef", operation_id="op-distinct-conclusions")
    request["input"]["text"]="Research and compare evidence, review risks, then draft a concise recommendation."
    request["privacy"].update(mode="remote_allowed", allow_remote=True, pii="none")
    if use_research_request:
        from tests.memory_app.v2.research_fixture import research_request
        request = research_request("v2-do-runtime-test", "project-alpha", request["input"]["text"])
    try:
        started=application.state.agent_organization_runtime.start(request,agent_turn_mode=True)
        terminal=runner.wait_for_terminal(started["main"]["turn_id"],timeout_seconds=30)
        assert terminal is not None and terminal.status=="completed", {"decomposition_errors": decomposition_errors, "requests": requests, "events": {run.profile_id: [(e.get("type"), {k:v for k,v in e.get("data",{}).items() if k in ("error_code", "status", "summary", "reason")}) for e in application.state.ai_turn_store.events_after(run.turn_id)[-2:]] for run in application.state.agent_runtime_composition.store.list_runs(project_id="project-alpha")}}
        experts=[p for p in requests if p.get("role",{}).get("organization_role") not in (None,"主政协调","管家调度")]
        assert {p["input"]["text"] for p in experts}=={"research evidence independently","review risks independently"}
        main_inputs=[json.dumps(p,ensure_ascii=False) for p in requests if p.get("role",{}).get("organization_role")=="主政协调"]
        assert any("evidence conclusion alpha" in p and "risk conclusion beta" in p for p in main_inputs)
        if use_research_request:
            assert request["desired_outcome"] == "project.answer"
            runs = application.state.agent_runtime_composition.store.list_runs(project_id="project-alpha")
            for run in runs:
                assert application.state.ai_turn_store.get_immutable_payload(run.turn_id, "turn-model-routing-snapshot-v1") is not None
                assert not any(".write" in capability or ".propose" in capability for capability in run.capability_ids)
    finally:
        assert runner.shutdown(timeout_seconds=5)==()


def test_expert_and_skill_applicability_uses_each_selected_child_task():
    builder=_builder(); profiles=builder.brief(_request())["profiles"]
    proposal=builder.complete(_request(), {"mode":"cluster", "assignments":[{"profile_id":profiles[0]["profile_id"],"task":"research evidence"},{"profile_id":profiles[1]["profile_id"],"task":"unrelated computation"}]})
    first, second=proposal["assignments"]
    assert first["expert"] is not None and first["skill"] is not None
    assert second["expert"] is None and second["skill"] is None

def test_missing_instructions_and_exceeded_slots_fall_back_exactly():
    builder=_builder(); request=_request() | {"privacy":{"allow_remote":True,"mode":"remote_allowed"}}
    gateway=Gateway({"mode":"main_only"})
    assert planner(gateway,builder).plan(request,[],[],SimpleNamespace(get_immutable_payload=lambda *_:None))["arguments"]==builder.build(request)
    assert gateway.requests==[]
    selection={"mode":"cluster","assignments":[{"profile_id":"subagent.explorer","task":f"Independent task {i}"} for i in range(3)]}
    assert planner(Gateway(selection),builder).plan(request,[],[],Payloads())["arguments"]==builder.build(request)


def test_decomposition_preserves_governed_control_and_routing(monkeypatch):
    from backend.api import agent_steward_decomposition as module
    builder = _builder()
    request = _request() | {"privacy": {"allow_remote": True, "mode": "remote_allowed"}}
    control = object()
    routing = {"_routing_project_id": "project-a", "_model_routing_snapshot_ref": "frozen-ref"}
    monkeypatch.setattr(module, "turn_routing_parameters", lambda req, store: routing)
    gateway = Gateway({"mode": "main_only"})
    result = planner(gateway, builder).plan(request, [], [], Payloads(), control)
    assert result["arguments"] == {"mode": "main_only", "plan_id": "steward-main-only"}
    sent = gateway.requests[0]
    assert sent.execution_control is control
    assert sent.metadata_sink is control
    assert sent.parameters == {"temperature": 0, "response_format": {"type": "json_object"}, **routing}
    assert sent.privacy_scope == "remote_allowed"


def test_decomposition_without_authorized_gateway_falls_back():
    builder = _builder()
    request = _request() | {"privacy": {"allow_remote": True, "mode": "remote_allowed"}}
    assert planner(None, builder).plan(request, [], [], Payloads())["arguments"] == builder.build(request)
