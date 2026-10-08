from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import copy

import pytest

from src.core.ai_kernel.agent_contracts import (
    AGENT_CONTRACT_SCHEMA_VERSION,
    AGENT_PROFILE_SCHEMA_VERSION,
    AgentBudget,
    AgentBudgetReservation,
    AgentChildLink,
    AgentContractError,
    AgentFanIn,
    AgentFanInResult,
    AgentMessage,
    AgentProfile,
    AgentRun,
    AgentTerminalChildSummary,
    agent_budget_from_payload,
    agent_budget_reservation_from_payload,
    agent_budget_reservation_to_payload,
    agent_child_link_from_payload,
    agent_child_link_to_payload,
    agent_fan_in_from_payload,
    agent_fan_in_result_from_payload,
    agent_fan_in_result_to_payload,
    agent_fan_in_to_payload,
    agent_message_from_payload,
    agent_message_to_payload,
    agent_profile_from_payload,
    agent_profile_to_payload,
    agent_run_from_payload,
    agent_run_to_payload,
    validate_child_delegation,
    validate_fan_in_result,
)


def _budget() -> AgentBudget:
    return AgentBudget(model_calls=4, tool_calls=8, input_tokens=8_000, output_tokens=2_000, wall_time_ms=60_000)


def _main_run() -> AgentRun:
    return AgentRun(
        run_id="run-main-001", turn_id="turn-main-001", project_id="project-alpha",
        profile_id="main.orchestrator", profile_revision=2, role="main", model_tier="standard",
        status="running", depth=0, cancel_epoch=3, budget_limit=_budget(),
        capability_ids=("memory.recall", "library.search"), max_concurrent_children=3, max_depth=2,
        max_steps=16, timeout_ms=60_000, allow_child_spawn=True,
        model_routing_snapshot_ref="crp://routing/project-alpha/main", capability_manifest_ref="crp://capabilities/project-alpha/main",
        context_manifest_ref="crp://context/project-alpha/main", budget_snapshot_ref="crp://budgets/project-alpha/main",
        terminal_receipt_ref=None,
    )


def _child_run() -> AgentRun:
    return AgentRun(
        run_id="run-child-001", turn_id="turn-child-001", project_id="project-alpha",
        profile_id="subagent.explorer", profile_revision=2, role="subagent", model_tier="fast",
        status="queued", depth=1, cancel_epoch=3,
        budget_limit=AgentBudget(1, 2, 2_000, 500, 20_000), capability_ids=("memory.recall",),
        max_concurrent_children=0, max_depth=1, max_steps=8, timeout_ms=20_000,
        allow_child_spawn=False, model_routing_snapshot_ref=None, capability_manifest_ref=None,
        context_manifest_ref=None, budget_snapshot_ref=None, terminal_receipt_ref=None,
        parent_run_id="run-main-001",
    )


def _link() -> AgentChildLink:
    child = _child_run()
    return AgentChildLink(
        link_id="link-main-child-001", parent_run_id="run-main-001", child_run_id=child.run_id,
        parent_project_id="project-alpha", child_project_id="project-alpha", parent_cancel_epoch=3,
        spawn_operation_id="operation-spawn-001",
        child_depth=1, delegated_budget=child.budget_limit,
        delegated_capability_ids=child.capability_ids, status="spawned",
    )


def test_budget_is_immutable_subset_checked_and_rejects_bool() -> None:
    budget = _budget()
    assert budget.remaining_after(AgentBudget(1, 2, 200, 50, 1_000)) == AgentBudget(3, 6, 7_800, 1_950, 59_000)
    with pytest.raises(AgentContractError, match="exceeds"):
        budget.remaining_after(AgentBudget(5, 0, 0, 0, 0))
    with pytest.raises(AgentContractError, match="model_calls"):
        AgentBudget(True, 0, 0, 0, 0)
    with pytest.raises(FrozenInstanceError):
        budget.model_calls = 1  # type: ignore[misc]


def test_profiles_allow_only_builtin_and_strict_custom_subagents() -> None:
    built_in = AgentProfile("main.orchestrator", 1, "编排主 Agent", True, "main", "deep", _budget(), ("memory.recall",), 3, 2, 16, 60_000, True)
    steward = AgentProfile("steward.scheduler", 1, "管家调度 Agent", True, "subagent", "standard", _budget(), ("agent.list",), 0, 0, 6, 30_000, False)
    custom = AgentProfile("subagent.custom.research-v2", 1, "研究子 Agent", True, "subagent", "fast", _budget(), (), 0, 2, 8, 20_000, False)
    assert built_in.model_tier == "deep"
    assert steward.profile_id == "steward.scheduler"
    assert custom.profile_id == "subagent.custom.research-v2"
    with pytest.raises(AgentContractError, match="profile identity"):
        AgentProfile("main.custom", 1, "错误", True, "main", "standard", _budget(), (), 0, 0, 1, 1, False)
    with pytest.raises(AgentContractError, match="main profile"):
        AgentProfile("main.orchestrator", 1, "错误", True, "subagent", "standard", _budget(), (), 0, 0, 1, 1, False)
    with pytest.raises(AgentContractError, match="subagent role"):
        AgentProfile("steward.scheduler", 1, "错误", True, "main", "standard", _budget(), (), 0, 0, 1, 1, False)
    explorer = AgentProfile("subagent.explorer", 1, "探索子 Agent", True, "subagent", "fast", _budget(), (), 0, 2, 8, 20_000, False)
    assert explorer.max_depth == 2
    with pytest.raises(AgentContractError, match="only worker"):
        AgentProfile("subagent.explorer", 1, "错误", True, "subagent", "fast", _budget(), (), 1, 2, 1, 1, True)
    with pytest.raises(AgentContractError, match="positive concurrency"):
        AgentProfile("main.orchestrator", 1, "错误", True, "main", "standard", _budget(), (), 0, 2, 1, 1, True)


def test_child_delegation_fails_closed_on_expansion() -> None:
    parent = _main_run()
    child = _child_run()
    link = _link()
    validate_child_delegation(parent, child, link)
    with pytest.raises(AgentContractError, match="cross project"):
        validate_child_delegation(parent, child, AgentChildLink(
            link_id=link.link_id, parent_run_id=link.parent_run_id, child_run_id=link.child_run_id,
            parent_project_id="project-alpha", child_project_id="project-beta", parent_cancel_epoch=3,
            spawn_operation_id="operation-cross",
            child_depth=1, delegated_budget=child.budget_limit, delegated_capability_ids=child.capability_ids,
            status="spawned",
        ))
    excessive = AgentRun(
        run_id="run-child-over", turn_id="turn-child-over", project_id="project-alpha",
        profile_id="subagent.worker", profile_revision=1, role="subagent", model_tier="deep",
        status="queued", depth=1, cancel_epoch=3, budget_limit=AgentBudget(5, 0, 0, 0, 0),
        capability_ids=("memory.recall",), max_concurrent_children=0, max_depth=1,
        max_steps=8, timeout_ms=20_000, allow_child_spawn=False,
        model_routing_snapshot_ref=None, capability_manifest_ref=None, context_manifest_ref=None,
        budget_snapshot_ref=None, terminal_receipt_ref=None,
        parent_run_id="run-main-001",
    )
    excessive_link = AgentChildLink("link-over", parent.run_id, excessive.run_id, "project-alpha", "project-alpha", "operation-over", 3, 1, excessive.budget_limit, excessive.capability_ids, "reserved")
    with pytest.raises(AgentContractError, match="budget"):
        validate_child_delegation(parent, excessive, excessive_link)
    expanded = AgentRun(
        run_id="run-child-cap", turn_id="turn-child-cap", project_id="project-alpha",
        profile_id="subagent.explorer", profile_revision=1, role="subagent", model_tier="fast",
        status="queued", depth=1, cancel_epoch=3, budget_limit=AgentBudget(1, 0, 0, 0, 0),
        capability_ids=("memory.recall", "external.send"), max_concurrent_children=0, max_depth=1,
        max_steps=8, timeout_ms=20_000, allow_child_spawn=False,
        model_routing_snapshot_ref=None, capability_manifest_ref=None, context_manifest_ref=None,
        budget_snapshot_ref=None, terminal_receipt_ref=None,
        parent_run_id="run-main-001",
    )
    expanded_link = AgentChildLink("link-cap", parent.run_id, expanded.run_id, "project-alpha", "project-alpha", "operation-cap", 3, 1, expanded.budget_limit, expanded.capability_ids, "reserved")
    with pytest.raises(AgentContractError, match="capabilities"):
        validate_child_delegation(parent, expanded, expanded_link)
    with pytest.raises(AgentContractError, match="execution limit"):
        validate_child_delegation(parent, replace(child, max_steps=17), link)
    with pytest.raises(AgentContractError, match="cannot spawn"):
        validate_child_delegation(replace(parent, allow_child_spawn=False, max_concurrent_children=0), child, link)


def test_profile_limits_and_run_snapshot_freeze_are_fail_closed() -> None:
    with pytest.raises(AgentContractError, match="concurrency"):
        AgentProfile("main.orchestrator", 1, "编排主 Agent", True, "main", "standard", _budget(), (), 9, 1, 8, 1_000, True)
    with pytest.raises(AgentContractError, match="depth"):
        AgentProfile("main.orchestrator", 1, "编排主 Agent", True, "main", "standard", _budget(), (), 1, 5, 8, 1_000, True)
    with pytest.raises(AgentContractError, match="frozen snapshots"):
        replace(_child_run(), status="starting")
    with pytest.raises(AgentContractError, match="requires receipt"):
        replace(_main_run(), status="completed")
    terminal = replace(_main_run(), status="completed", terminal_receipt_ref="crp://receipts/project-alpha/main-terminal")
    assert terminal.is_terminal is True


def test_run_message_and_reservation_state_validation() -> None:
    assert _main_run().is_terminal is False
    message = AgentMessage("message-001", "project-alpha", "run-main-001", "run-child-001", "operation-message-001", 1, "task", "crp://agent/project-alpha/message-001", 3, "pending")
    assert agent_message_from_payload(agent_message_to_payload(message)) == message
    reservation = AgentBudgetReservation("reservation-001", "project-alpha", "run-main-001", "run-child-001", "operation-reservation-001", 3, _child_run().budget_limit, None, "reserved")
    assert agent_budget_reservation_from_payload(agent_budget_reservation_to_payload(reservation)) == reservation
    with pytest.raises(AgentContractError, match="terminal budget reservation"):
        AgentBudgetReservation("reservation-bad", "project-alpha", "run-main-001", "run-child-001", "operation-reservation-bad", 3, _child_run().budget_limit, None, "settled")
    with pytest.raises(AgentContractError, match="settlement exceeds"):
        AgentBudgetReservation("reservation-over", "project-alpha", "run-main-001", "run-child-001", "operation-reservation-over", 3, _child_run().budget_limit, _budget(), "settled")


def test_fan_in_requires_bound_terminal_same_project_summaries() -> None:
    fan_in = AgentFanIn("fan-in-001", "project-alpha", "run-main-001", "operation-fan-in-001", ("run-child-001", "run-child-002"), "all", None, 3, "collecting")
    summaries = (
        AgentTerminalChildSummary("run-child-001", "project-alpha", "completed", "crp://receipts/project-alpha/child-001", "crp://summaries/project-alpha/child-001", ("crp://evidence/project-alpha/child-001",), AgentBudget(1, 0, 10, 4, 50)),
        AgentTerminalChildSummary("run-child-002", "project-alpha", "failed", "crp://receipts/project-alpha/child-002", "crp://summaries/project-alpha/child-002", ("crp://evidence/project-alpha/child-002",), AgentBudget(1, 0, 10, 4, 50), "agent.timeout"),
    )
    result = AgentFanInResult("fan-in-result-001", fan_in.fan_in_id, "project-alpha", fan_in.parent_run_id, "completed", summaries, "crp://receipts/project-alpha/fan-in-001", "crp://fan-in/project-alpha/result-001")
    validate_fan_in_result(fan_in, result)
    with pytest.raises(AgentContractError, match="incomplete"):
        validate_fan_in_result(fan_in, AgentFanInResult("fan-in-result-short", fan_in.fan_in_id, "project-alpha", fan_in.parent_run_id, "completed", summaries[:1], "crp://receipts/project-alpha/fan-in-short"))
    with pytest.raises(AgentContractError, match="cross project"):
        AgentFanInResult("fan-in-result-cross", fan_in.fan_in_id, "project-alpha", fan_in.parent_run_id, "completed", (AgentTerminalChildSummary("run-child-001", "project-beta", "completed", "crp://receipts/project-beta/child", "crp://summaries/project-beta/child", (), AgentBudget(0, 0, 0, 0, 0)),), "crp://receipts/project-alpha/fan-in-cross")


def test_all_contracts_round_trip_json_safe() -> None:
    profile = AgentProfile("subagent.worker", 3, "执行子 Agent", True, "subagent", "standard", _budget(), ("memory.recall",), 1, 2, 16, 60_000, True)
    main = _main_run()
    child = _child_run()
    link = _link()
    message = AgentMessage("message-001", "project-alpha", main.run_id, child.run_id, "operation-message-001", 1, "progress", "crp://agent/project-alpha/message-001", 3, "delivered")
    reservation = AgentBudgetReservation("reservation-001", "project-alpha", main.run_id, child.run_id, "operation-reservation-001", 3, child.budget_limit, child.budget_limit, "settled")
    fan_in = AgentFanIn("fan-in-001", "project-alpha", main.run_id, "operation-fan-in-001", (child.run_id,), "all", None, 3, "completed")
    result = AgentFanInResult("result-001", fan_in.fan_in_id, "project-alpha", main.run_id, "completed", (AgentTerminalChildSummary(child.run_id, "project-alpha", "completed", "crp://receipts/project-alpha/child", "crp://summaries/project-alpha/child", (), child.budget_limit),), "crp://receipts/project-alpha/fan-in-result")
    assert agent_budget_from_payload(agent_budget_to_payload := {"model_calls": 1, "tool_calls": 2, "input_tokens": 3, "output_tokens": 4, "wall_time_ms": 5}) == AgentBudget(1, 2, 3, 4, 5)
    assert agent_profile_from_payload(agent_profile_to_payload(profile)) == profile
    assert agent_run_from_payload(agent_run_to_payload(main)) == main
    assert agent_child_link_from_payload(agent_child_link_to_payload(link)) == link
    assert agent_message_from_payload(agent_message_to_payload(message)) == message
    assert agent_budget_reservation_from_payload(agent_budget_reservation_to_payload(reservation)) == reservation
    assert agent_fan_in_from_payload(agent_fan_in_to_payload(fan_in)) == fan_in
    assert agent_fan_in_result_from_payload(agent_fan_in_result_to_payload(result)) == result


def test_profile_payload_normalizes_legacy_sqlite_records_and_versions_new_metadata() -> None:
    legacy = agent_profile_to_payload(AgentProfile(
        "main.orchestrator", 1, "编排主 Agent", True, "main", "standard", _budget(), (), 1, 1, 8, 30_000, True,
    ))
    legacy["schema_version"] = "1.0.0"
    legacy.pop("organization_role")
    legacy.pop("work_description")
    legacy.pop("instructions")
    normalized = agent_profile_from_payload(legacy)
    assert normalized.organization_role == "主政协调"
    assert normalized.work_description == "统筹目标、优先级、进度汇聚与最终答复。"
    payload = agent_profile_to_payload(normalized)
    assert payload["schema_version"] == AGENT_PROFILE_SCHEMA_VERSION == "1.3.0"
    assert payload["organization_role"] == "主政协调"


def test_profile_metadata_is_strictly_typed_bounded_and_required_in_current_schema() -> None:
    profile = AgentProfile("subagent.custom.research-v2", 1, "研究子 Agent", True, "subagent", "fast", _budget(), (), 0, 2, 8, 20_000, False)
    payload = agent_profile_to_payload(profile)
    for field, value, match in (
        ("organization_role", "", "organization role"),
        ("work_description", "", "work description"),
        ("organization_role", 1, "organization_role must be a string"),
        ("work_description", 1, "work_description must be a string"),
    ):
        invalid = dict(payload)
        invalid[field] = value
        with pytest.raises(AgentContractError, match=match):
            agent_profile_from_payload(invalid)
    missing = dict(payload)
    missing.pop("work_description")
    with pytest.raises(AgentContractError, match="missing fields"):
        agent_profile_from_payload(missing)
    with pytest.raises(AgentContractError, match="organization role"):
        replace(profile, organization_role="岗" * 81)
    with pytest.raises(AgentContractError, match="work description"):
        replace(profile, work_description="介" * 241)


def test_profile_route_binding_requires_an_exact_non_secret_identity_pair() -> None:
    profile = AgentProfile(
        "subagent.custom.research-v2", 1, "研究子 Agent", True, "subagent", "fast",
        _budget(), (), 0, 2, 8, 20_000, False,
        model_route_key="synthetic.route", model_route_revision=4,
    )
    assert agent_profile_from_payload(agent_profile_to_payload(profile)) == profile
    with pytest.raises(AgentContractError, match="binding is incomplete"):
        replace(profile, model_route_revision=None)
    payload = agent_profile_to_payload(profile)
    payload["endpoint"] = "forbidden"
    with pytest.raises(AgentContractError, match="sensitive"):
        agent_profile_from_payload(payload)


@pytest.mark.parametrize("factory", [
    lambda: agent_profile_to_payload(AgentProfile("main.orchestrator", 1, "编排主 Agent", True, "main", "standard", _budget(), (), 1, 1, 8, 30_000, True)),
    lambda: agent_run_to_payload(_main_run()),
    lambda: agent_child_link_to_payload(_link()),
    lambda: agent_message_to_payload(AgentMessage("message-001", "project-alpha", "run-main-001", "run-child-001", "operation-message-001", 1, "task", "crp://agent/project-alpha/message-001", 0, "pending")),
])
def test_payload_codecs_reject_unknown_and_sensitive_fields(factory) -> None:
    payload = factory()
    unknown = copy.deepcopy(payload)
    unknown["unrecognized"] = "value"
    sensitive = copy.deepcopy(payload)
    sensitive["provider_id"] = "forbidden"
    parsers = {
        "profile_id": agent_profile_from_payload,
        "run_id": agent_run_from_payload,
        "link_id": agent_child_link_from_payload,
        "message_id": agent_message_from_payload,
    }
    parser = next(parser for key, parser in parsers.items() if key in payload)
    with pytest.raises(AgentContractError, match="unknown fields"):
        parser(unknown)
    with pytest.raises(AgentContractError, match="sensitive"):
        parser(sensitive)


def test_payload_codecs_reject_nested_sensitive_fields_and_boolean_counts() -> None:
    profile = agent_profile_to_payload(AgentProfile("main.orchestrator", 1, "编排主 Agent", True, "main", "standard", _budget(), (), 1, 1, 8, 30_000, True))
    profile["budget_limit"]["api_key"] = "forbidden"  # type: ignore[index]
    with pytest.raises(AgentContractError, match="sensitive"):
        agent_profile_from_payload(profile)
    payload = {"model_calls": True, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "wall_time_ms": 0}
    with pytest.raises(AgentContractError, match="integer"):
        agent_budget_from_payload(payload)
    assert AGENT_CONTRACT_SCHEMA_VERSION == "1.0.0"


@pytest.mark.parametrize("version", ["1.0.0", "1.1.0", "1.2.0"])
def test_old_profiles_receive_builtin_instructions_and_custom_empty(version):
    from core.ai_kernel.agent_profiles import builtin_agent_profiles
    for profile in builtin_agent_profiles():
        payload = agent_profile_to_payload(profile)
        payload["schema_version"] = version
        payload.pop("instructions", None)
        if version in {"1.0.0", "1.1.0"}:
            payload.pop("model_route_key")
            payload.pop("model_route_revision")
        if version == "1.0.0":
            payload.pop("organization_role")
            payload.pop("work_description")
        decoded = agent_profile_from_payload(payload)
        assert decoded.instructions == profile.instructions
        assert decoded.instructions
        payload["profile_id"] = "subagent.custom.legacy"
        payload["role"] = "subagent"
        payload["allow_child_spawn"] = False
        payload["max_concurrent_children"] = 0
        assert agent_profile_from_payload(payload).instructions == ""


@pytest.mark.parametrize("instructions", [None, 1, "x" * 2001, "a\t", "a\r", "a\x00", "a\x7f", "a\x85"])
def test_profile_instructions_reject_invalid_values(instructions):
    with pytest.raises(AgentContractError):
        replace(AgentProfile("main.orchestrator", 1, "主 Agent", True, "main", "standard", _budget(), (), 1, 1, 8, 30_000, True), instructions=instructions)


def test_profile_instructions_roundtrip_new_schema_and_newline():
    profile = replace(AgentProfile("main.orchestrator", 1, "主 Agent", True, "main", "standard", _budget(), (), 1, 1, 8, 30_000, True), instructions="第一行\n第二行" + "x" * 1993)
    payload = agent_profile_to_payload(profile)
    assert payload["schema_version"] == "1.3.0"
    assert agent_profile_from_payload(payload) == profile


def test_builtin_instructions_match_the_planned_role_text_verbatim():
    from core.ai_kernel.agent_profiles import builtin_agent_profiles
    expected = {'main.orchestrator': '你是主 Agent，对用户的原始任务负责，最终答复由你给出。\n- 没有专家时，自己用可用能力完成任务。\n- 有专家时，agent.list 结果里 fan_ins[].result.children[].conclusion 是各位专家交回的结论。逐条阅读，采纳有依据的内容；专家之间有分歧时，说明分歧并给出你的判断和理由；专家失败或没有结论的部分，用你自己的能力补上，或明确告诉用户缺了什么。\n- 不要编造专家没有给出的事实，不要提及内部编号、引用地址或系统细节。\n- 最终 summary 直接回答原始任务，使用用户的语言。', 'steward.scheduler': '你是管家，只决定要不要请专家、怎么分工，不回答任务本身。\n- 一步就能完成、不需要查资料、也不需要多个角度的任务，返回 main_only。\n- 需要专家时，把任务拆成互不重叠的子任务，每个子任务交给 profiles 里最合适的一位专家，人数不超过 slots。\n- 每个子任务要能独立完成：写清楚做什么、范围、交付什么。专家看不到原任务，只看得到你写的子任务。\n- 每个子任务不超过 300 字。只输出要求的 JSON。', 'subagent.explorer': '你是探索专家，只读，负责查找和整理证据。\n- 先用可用的读取能力查找与子任务相关的资料，不要凭记忆编造。\n- 最终 summary 是交给主 Agent 的唯一结果：列出结论要点（最多 5 条），每条说明依据来自哪份资料；查不到的明确写"未找到"。\n- 不超过 1500 字。', 'subagent.worker': '你是执行专家，在给定能力内完成子任务，产出可以直接使用的成果。\n- 需要写入时只使用提案类能力，不直接发布或覆盖用户内容。\n- 最终 summary 是交给主 Agent 的唯一结果：写明完成了什么、成果的要点或正文、还有什么没完成及原因。\n- 不超过 1500 字。', 'subagent.reviewer': '你是审阅专家，只读，负责核验，不重写方案。\n- 逐条检查子任务中的说法、方案或结果，给出"成立 / 存疑 / 不成立"，附理由和风险。\n- 子任务要求特定输出格式时（例如一行 VERDICT=…），严格按要求输出，不加其他内容。\n- 否则最终 summary 按"结论—理由—风险"写，不超过 1500 字。'}
    assert {profile.profile_id: profile.instructions for profile in builtin_agent_profiles()} == expected
