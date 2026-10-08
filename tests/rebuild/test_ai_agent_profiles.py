from __future__ import annotations

from dataclasses import replace

import pytest

from src.core.ai_kernel.agent_contracts import (
    AgentBudget,
    AgentContractError,
    AgentProfile,
)
from src.core.ai_kernel.agent_profiles import (
    AgentProfileConflict,
    AgentProfileError,
    AgentProfileRegistry,
    InMemoryAgentProfileStore,
)


def _custom(*, revision: int = 1, enabled: bool = True) -> AgentProfile:
    return AgentProfile(
        profile_id="subagent.custom.research", revision=revision,
        display_name="研究子 Agent", enabled=enabled, role="subagent", model_tier="fast",
        budget_limit=AgentBudget(2, 4, 4_000, 1_000, 30_000),
        capability_ids=("memory.recall", "source.evidence.read"),
        max_concurrent_children=0, max_depth=4, max_steps=8, timeout_ms=30_000,
        allow_child_spawn=False,
    )


def test_builtin_defaults_are_stable_safe_and_provider_free() -> None:
    registry = AgentProfileRegistry()
    profiles = registry.list_profiles()
    assert [profile.profile_id for profile in profiles] == [
        "main.orchestrator", "steward.scheduler", "subagent.explorer", "subagent.worker", "subagent.reviewer",
    ]
    assert [profile.model_tier for profile in profiles] == ["deep", "standard", "fast", "standard", "deep"]
    assert all(profile.max_concurrent_children <= 8 and profile.max_depth <= 4 for profile in profiles)
    assert registry.get("subagent.explorer").allow_child_spawn is False  # type: ignore[union-attr]
    worker_capabilities = registry.get("subagent.worker").capability_ids  # type: ignore[union-attr]
    assert "document.draft.propose" in worker_capabilities
    assert "agent.spawn" in worker_capabilities
    assert registry.get("subagent.worker").allow_child_spawn is False  # type: ignore[union-attr]
    assert "library.write" not in worker_capabilities
    assert "agent.spawn" in registry.get("main.orchestrator").capability_ids  # type: ignore[union-attr]
    assert "agent.plan" in registry.get("main.orchestrator").capability_ids  # type: ignore[union-attr]
    steward = registry.get("steward.scheduler")
    assert steward is not None and steward.allow_child_spawn is False and steward.max_concurrent_children == 0
    assert steward.max_depth == 1
    assert set(steward.capability_ids) == {"agent.list", "agent.message", "agent.plan", "workbench.input.classification.context.read"}
    assert "agent.spawn" not in steward.capability_ids
    resolution = registry.resolve_tier("subagent.worker")
    assert resolution.model_tier == "standard"
    assert not any("provider" in name or "endpoint" in name or "secret" in name for name in resolution.__dataclass_fields__)


def test_custom_lifecycle_uses_monotonic_compare_and_swap() -> None:
    registry = AgentProfileRegistry()
    created = registry.create_custom(_custom())
    assert registry.get(created.profile_id) == created
    bound = registry.update_custom(
        replace(created, revision=2, model_route_key="synthetic.route", model_route_revision=4),
        expected_revision=1,
    )
    assert registry.resolve_tier(bound.profile_id).model_route_key == "synthetic.route"
    updated = registry.update_custom(replace(bound, revision=3, display_name="深度研究子 Agent"), expected_revision=2)
    assert updated.revision == 3
    with pytest.raises(AgentProfileConflict, match="expected 1, current 3"):
        registry.set_enabled(updated.profile_id, enabled=False, expected_revision=1)
    disabled = registry.set_enabled(updated.profile_id, enabled=False, expected_revision=3)
    assert disabled.revision == 4 and disabled.enabled is False
    with pytest.raises(AgentProfileError, match="disabled"):
        registry.resolve_tier(disabled.profile_id)
    registry.delete_custom(disabled.profile_id, expected_revision=4)
    assert registry.get(disabled.profile_id) is None


def test_builtins_are_configurable_but_not_deletable_and_main_stays_enabled() -> None:
    registry = AgentProfileRegistry()
    main = registry.get("main.orchestrator")
    worker = registry.get("subagent.worker")
    assert main is not None and worker is not None
    main_override = registry.update(replace(main, revision=2, model_tier="standard"), expected_revision=1)
    worker_override = registry.update(replace(worker, revision=2, model_tier="deep"), expected_revision=1)
    assert registry.resolve_tier(main_override.profile_id).model_tier == "standard"
    assert registry.resolve_tier(worker_override.profile_id).model_tier == "deep"
    assert [item.model_tier for item in registry.list_profiles()[:5]] == ["standard", "standard", "fast", "deep", "deep"]
    with pytest.raises(AgentProfileError, match="cannot be disabled"):
        registry.set_enabled("main.orchestrator", enabled=False, expected_revision=2)
    with pytest.raises(AgentProfileError, match="cannot be disabled"):
        registry.update(replace(main_override, revision=3, enabled=False), expected_revision=2)
    steward = registry.get("steward.scheduler")
    assert steward is not None
    steward_override = registry.update(replace(steward, revision=2, model_tier="fast", budget_limit=AgentBudget(1, 2, 2_000, 500, 15_000)), expected_revision=1)
    assert steward_override.model_tier == "fast"
    with pytest.raises(AgentProfileError, match="cannot be disabled"):
        registry.set_enabled("steward.scheduler", enabled=False, expected_revision=2)
    with pytest.raises(AgentProfileError, match="built-in.*deleted"):
        registry.delete_custom("steward.scheduler", expected_revision=2)
    disabled = registry.set_enabled("subagent.worker", enabled=False, expected_revision=2)
    assert disabled.enabled is False and disabled.revision == 3
    with pytest.raises(AgentProfileError, match="disabled"):
        registry.resolve_tier("subagent.worker")
    with pytest.raises(AgentProfileError, match="built-in.*deleted"):
        registry.delete_custom("main.orchestrator", expected_revision=1)
    with pytest.raises(AgentProfileError, match="only custom subagent"):
        registry.create_custom(registry.get("main.orchestrator"))  # type: ignore[arg-type]
    with pytest.raises(AgentProfileConflict, match="revision must be one"):
        registry.create_custom(_custom(revision=2))


def test_payload_rejects_sensitive_and_unknown_model_configuration_fields() -> None:
    registry = AgentProfileRegistry()
    payload = {
        "schema_version": "1.1.0", "profile_id": "subagent.custom.research",
        "revision": 1, "display_name": "研究子 Agent", "enabled": True,
        "organization_role": "研究专家", "work_description": "整理证据并返回可复核研究结论。",
        "role": "subagent", "model_tier": "fast",
        "budget_limit": {"model_calls": 2, "tool_calls": 4, "input_tokens": 4_000, "output_tokens": 1_000, "wall_time_ms": 30_000},
        "capability_ids": ["memory.recall", "source.evidence.read"],
        "max_concurrent_children": 0, "max_depth": 4, "max_steps": 8,
        "timeout_ms": 30_000, "allow_child_spawn": False,
    }
    registry.create_custom_from_payload(payload)
    for field in ("provider_id", "model_name", "endpoint", "api_key"):
        bad = dict(payload)
        bad[field] = "forbidden"
        with pytest.raises(AgentProfileError, match="payload is invalid"):
            registry.create_custom_from_payload(bad)


def test_agent_profiles_accept_only_text_execution_tiers() -> None:
    with pytest.raises(AgentContractError, match="model tier"):
        replace(_custom(), model_tier="vision")
    with pytest.raises(AgentContractError, match="model tier"):
        replace(_custom(), model_tier="image_generation")


def test_builtin_profiles_have_stable_organization_metadata() -> None:
    profiles = {profile.profile_id: profile for profile in AgentProfileRegistry().list_profiles()}
    assert profiles["main.orchestrator"].organization_role == "主政协调"
    assert profiles["steward.scheduler"].organization_role == "管家调度"
    assert profiles["subagent.explorer"].work_description == "收集证据、定位上下文并返回可复核发现。"
    assert profiles["subagent.worker"].work_description == "在授权边界内完成实施任务并提交可审计结果。"
    assert profiles["subagent.reviewer"].work_description == "核验方案与结果，指出风险并提供复核意见。"


def test_store_snapshot_survives_a_fresh_registry_instance_with_builtin_override() -> None:
    first_store = InMemoryAgentProfileStore()
    first = AgentProfileRegistry(first_store)
    first.create_custom(_custom())
    main = first.get("main.orchestrator")
    assert main is not None
    first.update(replace(main, revision=2, model_tier="standard"), expected_revision=1)
    snapshot = first_store.snapshot()
    second = AgentProfileRegistry(InMemoryAgentProfileStore(snapshot))
    assert second.get("subagent.custom.research") == _custom()
    assert second.resolve_tier("subagent.custom.research").profile_revision == 1
    assert second.resolve_tier("main.orchestrator").model_tier == "standard"


def test_profile_contract_is_checked_at_storage_boundary() -> None:
    store = InMemoryAgentProfileStore()
    registry = AgentProfileRegistry(store)
    with pytest.raises(AgentProfileError, match="governed contract"):
        registry.create_custom(object())  # type: ignore[arg-type]
    default_main = AgentProfileRegistry().get("main.orchestrator")
    assert default_main is not None
    with pytest.raises(AgentProfileError, match="revision-one"):
        AgentProfileRegistry(InMemoryAgentProfileStore({
            default_main.profile_id: replace(default_main, model_tier="standard"),
        }))
