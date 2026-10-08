from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pytest

from core.ai_kernel.agent_contracts import (
    AgentBudget, AgentBudgetReservation, AgentChildLink, AgentFanIn,
    AgentFanInResult, AgentMessage, AgentProfile, AgentRun,
    AgentTerminalChildSummary,
)
from core.ai_kernel.agent_store import (
    AgentStoreConflict, AgentStoreInvalidTransition, AgentStoreLimitExceeded,
    AgentStoreNotFound, AgentStoreStaleEpoch, SQLiteAgentStore,
)
from core.ai_kernel.agent_profiles import AgentProfileRegistry, builtin_agent_profiles
from core.ai_kernel.sqlite_store import SQLiteAITurnStore


def _budget(calls: int = 4) -> AgentBudget:
    return AgentBudget(calls, calls, calls * 10, calls * 10, calls * 100)


def _ref(name: str) -> str:
    return f"crp://session/{name}/snapshot"


def _turn(turns: SQLiteAITurnStore, turn_id: str) -> None:
    turns.claim_turn({"turn_id": turn_id, "session_id": f"session-{turn_id}", "operation_id": f"operation-{turn_id}", "idempotency_key": f"idem-{turn_id}"})


def _main(turn_id: str = "turn-main", *, status: str = "running", epoch: int = 0, concurrency: int = 2) -> AgentRun:
    return AgentRun("run-main", turn_id, "project-a", "main.orchestrator", 1, "main", "deep", status, 0, epoch, _budget(8), ("memory.recall",), concurrency, 2, 8, 8_000, True, _ref("route"), _ref("cap"), _ref("context"), _ref("budget"), _ref("receipt") if status in {"completed", "failed", "cancelled", "timed_out"} else None)


def _child(turn_id: str = "turn-child", *, status: str = "queued", receipt: str | None = None) -> AgentRun:
    return AgentRun("run-child", turn_id, "project-a", "subagent.explorer", 1, "subagent", "fast", status, 1, 0, _budget(2), ("memory.recall",), 0, 1, 4, 4_000, False, _ref("child-route") if status != "queued" else None, _ref("child-cap") if status != "queued" else None, _ref("child-context") if status != "queued" else None, _ref("child-budget") if status != "queued" else None, receipt, "run-main")


def _spawn(child: AgentRun | None = None) -> tuple[AgentRun, AgentChildLink, AgentBudgetReservation]:
    child = child or _child()
    link = AgentChildLink("link-child", "run-main", child.run_id, "project-a", "project-a", "spawn-child", 0, 1, child.budget_limit, child.capability_ids, "reserved")
    reservation = AgentBudgetReservation("reservation-child", "project-a", "run-main", child.run_id, "spawn-child", 0, child.budget_limit, None, "reserved")
    return child, link, reservation


@pytest.fixture
def stores(tmp_path: Path) -> tuple[SQLiteAITurnStore, SQLiteAgentStore]:
    path = tmp_path / "ai-turns.sqlite"
    turns = SQLiteAITurnStore(path)
    return turns, SQLiteAgentStore(path)


def test_requires_existing_turn_authority(tmp_path: Path) -> None:
    with pytest.raises(AgentStoreNotFound):
        SQLiteAgentStore(tmp_path / "missing.sqlite")


def test_terminal_main_convergence_is_durable_and_idempotent(
    stores: tuple[SQLiteAITurnStore, SQLiteAgentStore],
) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    main = _main()
    store.register_run(main, operation_id="register-main-terminal")
    terminal = _main(status="completed")

    stored, changed = store.converge_terminal_main(
        terminal, operation_id="converge-main-terminal",
    )
    replayed, replay_changed = store.converge_terminal_main(
        terminal, operation_id="converge-main-terminal-replay",
    )

    assert changed is True and replay_changed is False
    assert stored == replayed == terminal
    assert store.get_run("run-main") == terminal


def test_profile_cas_and_restart_stability(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    profile = AgentProfile("subagent.explorer", 1, "Explorer", True, "subagent", "fast", _budget(), ("read",), 0, 0, 4, 2_000, False)
    assert store.put_profile(profile) == profile
    with pytest.raises(AgentStoreConflict):
        store.put_profile(profile)
    changed = AgentProfile("subagent.explorer", 2, "Explorer 2", True, "subagent", "fast", _budget(), ("read",), 0, 0, 4, 2_000, False)
    assert store.put_profile(changed, expected_revision=1) == changed
    assert SQLiteAgentStore(store._path).get_profile(changed.profile_id) == changed


def test_profile_registry_persists_first_builtin_override_and_custom_after_restart(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    _, store = stores
    registry = AgentProfileRegistry(store)
    main = registry.get("main.orchestrator")
    assert main is not None
    override = AgentProfile(main.profile_id, 2, main.display_name, main.enabled, main.role, "standard", main.budget_limit, main.capability_ids, main.max_concurrent_children, main.max_depth, main.max_steps, main.timeout_ms, main.allow_child_spawn)
    registry.update(override, expected_revision=1)
    custom = AgentProfile("subagent.custom.storetest", 1, "Store", True, "subagent", "fast", _budget(), ("read",), 0, 1, 4, 2_000, False)
    registry.create_custom(custom)
    restarted = AgentProfileRegistry(SQLiteAgentStore(store._path))
    assert restarted.get("main.orchestrator") == override
    assert restarted.get(custom.profile_id) == custom


def test_sqlite_legacy_profile_normalizes_on_read_and_writes_current_schema(
    stores: tuple[SQLiteAITurnStore, SQLiteAgentStore],
) -> None:
    _, store = stores
    legacy = {
        "schema_version": "1.0.0", "profile_id": "subagent.custom.legacy", "revision": 1,
        "display_name": "旧版研究 Agent", "enabled": True, "role": "subagent", "model_tier": "fast",
        "budget_limit": {"model_calls": 2, "tool_calls": 2, "input_tokens": 20, "output_tokens": 20, "wall_time_ms": 200},
        "capability_ids": ["memory.recall"], "max_concurrent_children": 0, "max_depth": 1,
        "max_steps": 2, "timeout_ms": 200, "allow_child_spawn": False,
    }
    connection = sqlite3.connect(store._path)
    try:
        connection.execute(
            "INSERT INTO ai_agent_profiles(profile_id,revision,payload_json) VALUES(?,?,?)",
            ("subagent.custom.legacy", 1, json.dumps(legacy)),
        )
        connection.commit()
    finally:
        connection.close()

    normalized = store.get_profile("subagent.custom.legacy")
    assert normalized is not None
    assert normalized.organization_role == "自定义子 Agent"
    assert normalized.work_description == "按已授权能力完成受管任务，并返回可审计结果。"
    store.put_profile(replace(normalized, revision=2), expected_revision=1)

    connection = sqlite3.connect(store._path)
    try:
        payload = json.loads(connection.execute(
            "SELECT payload_json FROM ai_agent_profiles WHERE profile_id=?", ("subagent.custom.legacy",),
        ).fetchone()[0])
    finally:
        connection.close()
    assert payload["schema_version"] == "1.3.0"
    assert payload["organization_role"] == "自定义子 Agent"
    assert payload["work_description"] == "按已授权能力完成受管任务，并返回可审计结果。"


def test_profile_changes_govern_new_runs_while_existing_run_and_spawn_stay_frozen(
    stores: tuple[SQLiteAITurnStore, SQLiteAgentStore],
) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    main = _main()
    store.register_run(main, operation_id="register-frozen-main")
    registry = AgentProfileRegistry(store)
    main_profile = registry.get("main.orchestrator")
    explorer_profile = registry.get("subagent.explorer")
    assert main_profile is not None and explorer_profile is not None
    registry.update(replace(main_profile, revision=2, model_tier="standard"), expected_revision=1)
    registry.update(replace(explorer_profile, revision=2, model_tier="standard"), expected_revision=1)

    valid_child = replace(_child(), profile_revision=2, model_tier="standard")
    child, link, reservation = _spawn(valid_child)
    assert store.reserve_spawn(parent=main, child=child, link=link, reservation=reservation)[3] is True

    _turn(turns, "turn-main-new")
    with pytest.raises(AgentStoreInvalidTransition, match="profile snapshot drifted"):
        store.register_run(_main("turn-main-new"), operation_id="register-stale-main")
    fresh = replace(_main("turn-main-new"), run_id="run-main-new", profile_revision=2, model_tier="standard")
    assert store.register_run(fresh, operation_id="register-fresh-main") == fresh

    terminal = _main(status="completed")
    assert store.transition_run(terminal, expected_revision=1, operation_id="finish-frozen-main") == terminal


def test_spawn_reservation_is_idempotent_and_never_inserts_turn(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    assert store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)[3]
    assert not store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)[3]
    assert turns.get_request("turn-child") is None
    with pytest.raises(AgentStoreInvalidTransition):
        store.finalize_spawn("link-child", operation_id="finish-child", expected_cancel_epoch=0)
    _turn(turns, "turn-child")
    assert store.finalize_spawn("link-child", operation_id="finish-child", expected_cancel_epoch=0).status == "spawned"


def test_budget_concurrency_and_stale_epoch_fail_closed(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main(concurrency=1)
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)
    child2 = _child("turn-child-2")
    child2 = AgentRun("run-child-2", child2.turn_id, child2.project_id, child2.profile_id, child2.profile_revision, child2.role, child2.model_tier, child2.status, child2.depth, child2.cancel_epoch, child2.budget_limit, child2.capability_ids, child2.max_concurrent_children, child2.max_depth, child2.max_steps, child2.timeout_ms, child2.allow_child_spawn, child2.model_routing_snapshot_ref, child2.capability_manifest_ref, child2.context_manifest_ref, child2.budget_snapshot_ref, child2.terminal_receipt_ref, child2.parent_run_id)
    link2 = AgentChildLink("link-child-2", "run-main", "run-child-2", "project-a", "project-a", "spawn-child-2", 0, 1, child2.budget_limit, child2.capability_ids, "reserved")
    reservation2 = AgentBudgetReservation("reservation-child-2", "project-a", "run-main", "run-child-2", "spawn-child-2", 0, child2.budget_limit, None, "reserved")
    with pytest.raises(AgentStoreLimitExceeded):
        store.reserve_spawn(parent=parent, child=child2, link=link2, reservation=reservation2)
    cancelled = store.cancel_parent("run-main", expected_revision=1, operation_id="cancel-main")
    assert cancelled.cancel_epoch == 1
    with pytest.raises(AgentStoreStaleEpoch):
        store.release_reservation("reservation-child", operation_id="release-child", expected_cancel_epoch=0)


def test_message_delivery_ack_and_parent_cancel(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)
    message = AgentMessage("message-1", "project-a", "run-main", "run-child", "message-send", 1, "task", _ref("message"), 0, "pending")
    assert store.send_message(message)[1]
    assert store.deliver_message("message-1", operation_id="message-deliver", expected_cancel_epoch=0).status == "delivered"
    assert store.acknowledge_message("message-1", operation_id="message-ack", expected_cancel_epoch=0).status == "acknowledged"
    with pytest.raises(AgentStoreConflict):
        store.send_message(AgentMessage("message-2", "project-a", "run-main", "run-child", "message-send-2", 3, "task", _ref("message2"), 0, "pending"))
    message2 = AgentMessage("message-2", "project-a", "run-main", "run-child", "message-send-2", 2, "task", _ref("message2"), 0, "pending")
    store.send_message(message2)
    assert store.cancel_message("message-2", operation_id="message-cancel", expected_cancel_epoch=0).status == "cancelled"
    assert store.cancel_message("message-2", operation_id="message-cancel", expected_cancel_epoch=0).status == "cancelled"


def test_concurrent_message_sequence_allocation_is_contiguous(
    stores: tuple[SQLiteAITurnStore, SQLiteAgentStore],
) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)
    messages = tuple(
        AgentMessage(
            f"message-concurrent-{index}", "project-a", "run-main", "run-child",
            f"message-send-concurrent-{index}", 1, "task",
            _ref(f"message-concurrent-{index}"), 0, "pending",
        )
        for index in range(8)
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(store.send_message_next, messages))

    assert sorted(message.sequence for message, _created in results) == list(range(1, 9))
    assert all(created for _message, created in results)
    replayed, created = store.send_message_next(messages[0])
    assert created is False
    assert replayed.message_id == messages[0].message_id
    assert 1 <= replayed.sequence <= 8


def test_fan_in_requires_terminal_child_receipt(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)
    _turn(turns, "turn-child")
    store.finalize_spawn("link-child", operation_id="child-finalize", expected_cancel_epoch=0)
    fan = AgentFanIn("fanin-1", "project-a", "run-main", "fanin-create", ("run-child",), "all", None, 0, "open")
    store.create_fan_in(fan)
    summary = AgentTerminalChildSummary("run-child", "project-a", "completed", _ref("child-receipt"), _ref("summary"), (), _budget(1))
    result = AgentFanInResult("result-1", "fanin-1", "project-a", "run-main", "completed", (summary,), _ref("fanin-receipt"))
    with pytest.raises(AgentStoreInvalidTransition):
        store.complete_fan_in(result, operation_id="fanin-complete", expected_cancel_epoch=0)
    starting = AgentRun("run-child", "turn-child", "project-a", "subagent.explorer", 1, "subagent", "fast", "starting", 1, 0, _budget(2), ("memory.recall",), 0, 1, 4, 4_000, False, _ref("child-route"), _ref("child-cap"), _ref("child-context"), _ref("child-budget"), None, "run-main")
    store.transition_run(starting, expected_revision=1, operation_id="child-starting")
    running = AgentRun("run-child", "turn-child", "project-a", "subagent.explorer", 1, "subagent", "fast", "running", 1, 0, _budget(2), ("memory.recall",), 0, 1, 4, 4_000, False, _ref("child-route"), _ref("child-cap"), _ref("child-context"), _ref("child-budget"), None, "run-main")
    store.transition_run(running, expected_revision=2, operation_id="child-running")
    completed = AgentRun("run-child", "turn-child", "project-a", "subagent.explorer", 1, "subagent", "fast", "completed", 1, 0, _budget(2), ("memory.recall",), 0, 1, 4, 4_000, False, _ref("child-route"), _ref("child-cap"), _ref("child-context"), _ref("child-budget"), _ref("child-receipt"), "run-main")
    store.transition_run(completed, expected_revision=3, operation_id="child-terminal")
    assert store.transition_child_link("link-child", status="started", operation_id="link-started", expected_cancel_epoch=0).status == "started"
    assert store.transition_child_link("link-child", status="completed", operation_id="link-complete", expected_cancel_epoch=0).status == "completed"
    assert store.complete_fan_in(result, operation_id="fanin-complete", expected_cancel_epoch=0)[1]
    assert not SQLiteAgentStore(store._path).complete_fan_in(result, operation_id="fanin-complete", expected_cancel_epoch=0)[1]


def test_concurrent_idempotent_spawn_has_one_winner(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation), range(4)))
    assert sum(created for _, _, _, created in results) == 1


def test_run_state_machine_and_project_scoped_readers(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    backwards = _main(status="created")
    with pytest.raises(AgentStoreInvalidTransition):
        store.transition_run(backwards, expected_revision=1, operation_id="main-backwards")
    completed = _main(status="completed")
    # The terminal receipt is included by _main for terminal statuses.
    assert store.transition_run(completed, expected_revision=1, operation_id="main-completed").status == "completed"
    with pytest.raises(AgentStoreInvalidTransition):
        store.transition_run(completed, expected_revision=2, operation_id="main-terminal-mutated")
    assert store.list_runs(project_id="project-a") == (completed,)
    assert store.list_runs(project_id="project-b") == ()


def test_any_and_quorum_fanins_accept_direct_child_subsets(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)
    child2 = replace(_child("turn-child-2"), run_id="run-child-2")
    link2 = replace(link, link_id="link-child-2", child_run_id="run-child-2", spawn_operation_id="spawn-child-2")
    reservation2 = replace(reservation, reservation_id="reservation-child-2", child_run_id="run-child-2", operation_id="spawn-child-2")
    store.reserve_spawn(parent=parent, child=child2, link=link2, reservation=reservation2)
    _turn(turns, "turn-child")
    _turn(turns, "turn-child-2")
    store.finalize_spawn("link-child", operation_id="finalize-child", expected_cancel_epoch=0)
    store.finalize_spawn("link-child-2", operation_id="finalize-child-2", expected_cancel_epoch=0)
    any_fan = AgentFanIn("fanin-any", "project-a", "run-main", "create-any", ("run-child",), "any", None, 0, "open")
    quorum_fan = AgentFanIn("fanin-quorum", "project-a", "run-main", "create-quorum", ("run-child",), "quorum", 1, 0, "open")
    assert store.create_fan_in(any_fan)[0] == any_fan
    assert store.create_fan_in(quorum_fan)[0] == quorum_fan
    assert {fan.fan_in_id for fan in store.list_fan_ins(project_id="project-a", parent_run_id="run-main")} == {"fanin-any", "fanin-quorum"}


def test_terminal_convergence_is_atomic_and_idempotent(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)
    _turn(turns, "turn-child")
    store.finalize_spawn("link-child", operation_id="finalize-child", expected_cancel_epoch=0)
    fan = AgentFanIn("fanin-terminal", "project-a", "run-main", "fanin-terminal-create", ("run-child",), "all", None, 0, "open")
    store.create_fan_in(fan)
    terminal = replace(
        child, status="completed", model_routing_snapshot_ref=_ref("child-route"),
        capability_manifest_ref=_ref("child-cap"), context_manifest_ref=_ref("child-context"),
        budget_snapshot_ref=_ref("child-budget"),
        terminal_receipt_ref=_ref("child-receipt"),
    )

    run, terminal_link, settled, changed = store.converge_terminal_child(
        terminal, usage=_budget(1), operation_id="converge-child",
    )

    assert changed and run.status == "completed" and terminal_link.status == "completed"
    assert settled.status == "settled" and settled.settled_budget == _budget(1)
    summary = AgentTerminalChildSummary("run-child", "project-a", "completed", run.terminal_receipt_ref or "", _ref("summary"), (), _budget(1))
    fan_result = AgentFanInResult("fanin-terminal-result", fan.fan_in_id, "project-a", "run-main", "completed", (summary,), _ref("fanin-receipt"))
    assert store.complete_fan_in(fan_result, operation_id="fanin-terminal-complete", expected_cancel_epoch=0)[0] == fan_result
    replay = store.converge_terminal_child(terminal, usage=_budget(1), operation_id="converge-child")
    assert replay[3] is False and replay[0] == run


def test_recovery_candidates_are_bounded_nonterminal_children(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)

    assert store.list_recovery_candidates(limit=1) == (child,)
    with pytest.raises(ValueError):
        store.list_recovery_candidates(limit=257)


def test_organization_start_pairs_are_bounded_direct_nonterminal_topology(
    stores: tuple[SQLiteAITurnStore, SQLiteAgentStore],
) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    steward_profile = next(
        item for item in builtin_agent_profiles()
        if item.profile_id == "steward.scheduler"
    )
    # The scanner is topology-only; persist the governed built-in profile so
    # the child record can be created through the same durable store contract.
    store.put_profile(steward_profile, expected_revision=None)
    parent = replace(_main(), capability_ids=("agent.plan",))
    store.register_run(parent, operation_id="register-main")
    steward = replace(
        _child("turn-steward"), run_id="run-steward",
        profile_id="steward.scheduler", model_tier="standard",
        capability_ids=("agent.plan",), max_depth=1, max_steps=6,
        timeout_ms=4_000,
    )
    child, link, reservation = _spawn(steward)
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)

    assert store.list_organization_start_pairs(limit=1) == ((parent, steward),)
    assert store.list_organization_start_pairs(limit=1)[0][1].parent_run_id == parent.run_id
    with pytest.raises(ValueError):
        store.list_organization_start_pairs(limit=257)
    terminal = replace(parent, status="completed", terminal_receipt_ref=_ref("main-receipt"))
    store.transition_run(terminal, expected_revision=1, operation_id="terminal-main")
    assert store.list_organization_start_pairs(limit=1) == ()


def test_supervision_candidates_exclude_terminal_main_without_fan_in(
    stores: tuple[SQLiteAITurnStore, SQLiteAgentStore],
) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    main = _main()
    store.register_run(main, operation_id="register-terminal-supervision-main")
    terminal = replace(main, status="completed", terminal_receipt_ref=_ref("main-receipt"))
    store.transition_run(terminal, expected_revision=1, operation_id="complete-supervision-main")

    assert store.list_supervision_candidates(limit=1) == ()
    with pytest.raises(ValueError):
        store.list_supervision_candidates(limit=257)


def test_supervision_candidates_include_completed_fan_in(
    stores: tuple[SQLiteAITurnStore, SQLiteAgentStore],
) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    main = _main()
    store.register_run(main, operation_id="register-supervision-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=main, child=child, link=link, reservation=reservation)
    _turn(turns, "turn-child")
    store.finalize_spawn("link-child", operation_id="finalize-supervision-child", expected_cancel_epoch=0)
    terminal_child = replace(
        child, status="completed", model_routing_snapshot_ref=_ref("child-route"),
        capability_manifest_ref=_ref("child-cap"), context_manifest_ref=_ref("child-context"),
        budget_snapshot_ref=_ref("child-budget"), terminal_receipt_ref=_ref("child-receipt"),
    )
    store.converge_terminal_child(terminal_child, usage=_budget(1), operation_id="converge-supervision-child")
    fan = AgentFanIn("fanin-supervision", "project-a", "run-main", "fanin-supervision-create", ("run-child",), "all", None, 0, "open")
    store.create_fan_in(fan)
    summary = AgentTerminalChildSummary("run-child", "project-a", "completed", _ref("child-receipt"), _ref("summary"), (), _budget(1))
    result = AgentFanInResult("result-supervision", fan.fan_in_id, "project-a", "run-main", "completed", (summary,), _ref("fanin-receipt"))
    store.complete_fan_in(result, operation_id="complete-supervision-fan-in", expected_cancel_epoch=0)

    assert store.list_supervision_candidates(limit=1) == (main,)


def test_abort_reserved_spawn_and_profile_expansion_are_fail_closed(stores: tuple[SQLiteAITurnStore, SQLiteAgentStore]) -> None:
    turns, store = stores
    _turn(turns, "turn-main")
    parent = _main()
    store.register_run(parent, operation_id="register-main")
    child, link, reservation = _spawn()
    store.reserve_spawn(parent=parent, child=child, link=link, reservation=reservation)
    cancelled, released = store.abort_reserved_spawn("link-child", "reservation-child", operation_id="abort-child", expected_cancel_epoch=0)
    assert cancelled.status == "cancelled" and released.status == "released" and released.settled_budget == AgentBudget(0, 0, 0, 0, 0)
    assert store.abort_reserved_spawn("link-child", "reservation-child", operation_id="abort-child", expected_cancel_epoch=0) == (cancelled, released)
    expanded = replace(parent, capability_ids=("memory.recall", "external.admin"))
    with pytest.raises(AgentStoreInvalidTransition):
        store.transition_run(expanded, expected_revision=1, operation_id="expanded-main")
    assert store.get_run_with_revision("run-main", project_id="project-a") == (parent, 1)
    assert store.get_run_with_revision("run-main", project_id="project-b") is None
    assert store.get_run_by_turn_id("turn-main", project_id="project-a") == (parent, 1)
    assert store.get_run_by_turn_id("turn-main", project_id="project-b") is None



def test_sqlite_12_builtin_override_keeps_default_instructions_and_custom_empty(stores):
    from core.ai_kernel.agent_contracts import agent_profile_to_payload
    from core.ai_kernel.agent_profiles import builtin_agent_profiles
    _, store = stores
    builtin = replace(builtin_agent_profiles()[0], revision=2, work_description="旧版自定义岗位说明。")
    custom = replace(builtin_agent_profiles()[2], profile_id="subagent.custom.old-audit", revision=2)
    with sqlite3.connect(store._path) as connection:
        for profile in (builtin, custom):
            payload = agent_profile_to_payload(profile); payload.pop("instructions"); payload["schema_version"] = "1.2.0"
            connection.execute("INSERT INTO ai_agent_profiles(profile_id,revision,payload_json) VALUES(?,?,?)",
                (profile.profile_id, profile.revision, json.dumps(payload)))
    registry = AgentProfileRegistry(store)
    assert registry.get(builtin.profile_id).instructions == builtin.instructions
    assert registry.get(builtin.profile_id).work_description == builtin.work_description
    assert registry.get(custom.profile_id).instructions == ""
    updated = replace(registry.get(builtin.profile_id), revision=3, instructions="新版岗位指令。")
    registry.update(updated, expected_revision=2)
    assert AgentProfileRegistry(SQLiteAgentStore(store._path)).get(builtin.profile_id) == updated
