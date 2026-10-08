"""Governed Agent Profile registry.

Profiles deliberately select only a canonical model tier.  Provider, concrete
model, endpoint, and secret resolution remains in the existing model-routing
authority and is never accepted by this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import RLock
from typing import Protocol

from .agent_contracts import (
    BUILTIN_AGENT_INSTRUCTIONS,
    AgentBudget,
    AgentContractError,
    AgentProfile,
    agent_profile_from_payload,
)


class AgentProfileError(ValueError):
    """Raised when profile configuration is invalid or unsafe."""


class AgentProfileConflict(AgentProfileError):
    """Raised when a custom-profile compare-and-swap cannot be applied."""


class AgentProfileStorePort(Protocol):
    """Small persistence boundary; durable adapters belong outside this module."""

    def get(self, profile_id: str) -> AgentProfile | None: ...

    def list(self) -> tuple[AgentProfile, ...]: ...

    def create(self, profile: AgentProfile) -> None: ...

    def replace(self, profile: AgentProfile, *, expected_revision: int) -> None: ...

    def delete(self, profile_id: str, *, expected_revision: int) -> None: ...


class InMemoryAgentProfileStore:
    """Thread-safe baseline store, also useful for deterministic domain tests.

    ``snapshot`` is intentionally plain immutable domain data, so a caller can
    construct a fresh store instance after a process restart without changing
    profile revisions or silently recreating user-defined profiles.
    """

    def __init__(self, records: Mapping[str, AgentProfile] | None = None) -> None:
        self._lock = RLock()
        self._records: dict[str, AgentProfile] = {}
        for profile_id, profile in (records or {}).items():
            if profile_id != profile.profile_id:
                raise AgentProfileError("agent profile store identity drifted")
            _validate_profile(profile)
            self._records[profile_id] = profile

    def get(self, profile_id: str) -> AgentProfile | None:
        with self._lock:
            return self._records.get(profile_id)

    def list(self) -> tuple[AgentProfile, ...]:
        with self._lock:
            return tuple(self._records[key] for key in sorted(self._records))

    def create(self, profile: AgentProfile) -> None:
        _validate_profile(profile)
        with self._lock:
            if profile.profile_id in self._records:
                raise AgentProfileConflict("agent profile already exists")
            self._records[profile.profile_id] = profile

    def replace(self, profile: AgentProfile, *, expected_revision: int) -> None:
        _validate_profile(profile)
        _expected_revision(expected_revision)
        with self._lock:
            current = self._records.get(profile.profile_id)
            if current is None:
                raise AgentProfileConflict("agent profile does not exist")
            if current.revision != expected_revision:
                raise AgentProfileConflict(
                    f"agent profile revision conflict: expected {expected_revision}, current {current.revision}"
                )
            if profile.revision != current.revision + 1:
                raise AgentProfileConflict("agent profile revision must advance by one")
            self._records[profile.profile_id] = profile

    def delete(self, profile_id: str, *, expected_revision: int) -> None:
        _expected_revision(expected_revision)
        with self._lock:
            current = self._records.get(profile_id)
            if current is None:
                raise AgentProfileConflict("agent profile does not exist")
            if current.revision != expected_revision:
                raise AgentProfileConflict(
                    f"agent profile revision conflict: expected {expected_revision}, current {current.revision}"
                )
            del self._records[profile_id]

    def snapshot(self) -> dict[str, AgentProfile]:
        with self._lock:
            return dict(self._records)


@dataclass(frozen=True, slots=True)
class AgentProfileTierResolution:
    """A profile-derived, provider-free ceiling for a future Agent Run."""

    profile_id: str
    profile_revision: int
    role: str
    model_tier: str
    budget_limit: AgentBudget
    capability_ids: tuple[str, ...]
    max_concurrent_children: int
    max_depth: int
    max_steps: int
    timeout_ms: int
    allow_child_spawn: bool
    model_route_key: str | None
    model_route_revision: int | None


_READ_ONLY_CAPABILITY_TEMPLATE = (
    "agent.list",
    "agent.message",
    "analyze_source",
    "companion.chat.context.read",
    "companion.vision.context.read",
    "memory.candidate.evidence.read",
    "memory.recall",
    "project_skill.evidence.read",
    "source.evidence.read",
    "workbench.input.classification.context.read",
    "workbench.question.answer",
)
_STEWARD_CAPABILITY_TEMPLATE = (
    "agent.list",
    "agent.message",
    "agent.plan",
    "workbench.input.classification.context.read",
)
_WORKER_CAPABILITY_TEMPLATE = tuple(sorted({
    *_READ_ONLY_CAPABILITY_TEMPLATE,
    "agent.fan_in",
    "agent.interrupt",
    "agent.spawn",
    "agent.wait",
    "companion.chat.message.write",
    "companion.vision.analyze.write",
    "document.draft.propose",
    "image.generate",
    "memory.candidate.propose.write",
    "presentation.pptx.fixed",
    "project_skill.draft.propose",
    "series.intake.organize.commit",
    "workbench.input.classification.enhance.write",
}))
_MAIN_CAPABILITY_TEMPLATE = tuple(sorted({
    *_WORKER_CAPABILITY_TEMPLATE,
    "agent.plan",
}))


_PREVIOUS_BUILTIN_LIMITS = {
    "main.orchestrator": (AgentBudget(8, 16, 16000, 4000, 120000), 16, 120000),
    "steward.scheduler": (AgentBudget(2, 3, 4000, 1000, 30000), 6, 30000),
    "subagent.explorer": (AgentBudget(3, 6, 8000, 2000, 60000), 12, 60000),
    "subagent.worker": (AgentBudget(4, 8, 10000, 2500, 90000), 16, 90000),
    "subagent.reviewer": (AgentBudget(3, 6, 10000, 2500, 75000), 12, 75000),
}


_BUILTIN_PROFILES: tuple[AgentProfile, ...] = (
    AgentProfile(
        profile_id="main.orchestrator", revision=1, instructions=BUILTIN_AGENT_INSTRUCTIONS["main.orchestrator"], display_name="编排主 Agent",
        organization_role="主政协调", work_description="统筹目标、优先级、进度汇聚与最终答复。",
        enabled=True, role="main", model_tier="deep",
        budget_limit=AgentBudget(12, 24, 64_000, 12_000, 600_000),
        capability_ids=_MAIN_CAPABILITY_TEMPLATE,
        max_concurrent_children=3, max_depth=2, max_steps=64, timeout_ms=600_000,
        allow_child_spawn=True,
    ),
    AgentProfile(
        profile_id="steward.scheduler", revision=1, instructions=BUILTIN_AGENT_INSTRUCTIONS["steward.scheduler"], display_name="管家调度 Agent",
        organization_role="管家调度", work_description="判断是否启用专家集群，并制定受管调度方案。",
        enabled=True, role="subagent", model_tier="standard",
        budget_limit=AgentBudget(2, 3, 8_000, 2_000, 60_000),
        capability_ids=_STEWARD_CAPABILITY_TEMPLATE,
        # Depth is absolute in AgentRun.  The steward is a direct child at
        # depth 1 even though it remains forbidden from spawning children.
        max_concurrent_children=0, max_depth=1, max_steps=6, timeout_ms=60_000,
        allow_child_spawn=False,
    ),
    AgentProfile(
        profile_id="subagent.explorer", revision=1, instructions=BUILTIN_AGENT_INSTRUCTIONS["subagent.explorer"], display_name="探索子 Agent",
        organization_role="探索专家", work_description="收集证据、定位上下文并返回可复核发现。",
        enabled=True, role="subagent", model_tier="fast",
        budget_limit=AgentBudget(4, 8, 16_000, 3_000, 240_000),
        capability_ids=_READ_ONLY_CAPABILITY_TEMPLATE,
        max_concurrent_children=0, max_depth=4, max_steps=12, timeout_ms=240_000,
        allow_child_spawn=False,
    ),
    AgentProfile(
        profile_id="subagent.worker", revision=1, instructions=BUILTIN_AGENT_INSTRUCTIONS["subagent.worker"], display_name="执行子 Agent",
        organization_role="执行专家", work_description="在授权边界内完成实施任务并提交可审计结果。",
        enabled=True, role="subagent", model_tier="standard",
        budget_limit=AgentBudget(6, 12, 20_000, 5_000, 300_000),
        # This is only an upper bound.  A child Run receives an explicit
        # intersection from its parent; it never inherits these as a grant.
        capability_ids=_WORKER_CAPABILITY_TEMPLATE,
        max_concurrent_children=0, max_depth=4, max_steps=16, timeout_ms=300_000,
        allow_child_spawn=False,
    ),
    AgentProfile(
        profile_id="subagent.reviewer", revision=1, instructions=BUILTIN_AGENT_INSTRUCTIONS["subagent.reviewer"], display_name="审阅子 Agent",
        organization_role="审阅专家", work_description="核验方案与结果，指出风险并提供复核意见。",
        enabled=True, role="subagent", model_tier="deep",
        budget_limit=AgentBudget(4, 8, 16_000, 3_000, 240_000),
        capability_ids=_READ_ONLY_CAPABILITY_TEMPLATE,
        max_concurrent_children=0, max_depth=4, max_steps=12, timeout_ms=240_000,
        allow_child_spawn=False,
    ),
)
_BUILTINS_BY_ID = {profile.profile_id: profile for profile in _BUILTIN_PROFILES}


class AgentProfileRegistry:
    """Combines fixed safe defaults with versioned custom subagent profiles."""

    def __init__(self, store: AgentProfileStorePort | None = None) -> None:
        self._store = store or InMemoryAgentProfileStore()
        self._validate_store_boundary()

    def list_profiles(self) -> tuple[AgentProfile, ...]:
        custom = self._custom_profiles()
        builtins = tuple(
            self._stored_or_default(profile)
            for profile in _BUILTIN_PROFILES
        )
        return builtins + tuple(sorted(custom, key=lambda item: item.profile_id))

    def get(self, profile_id: str) -> AgentProfile | None:
        builtin = _BUILTINS_BY_ID.get(profile_id)
        if builtin is not None:
            return self._stored_or_default(builtin)
        profile = self._store.get(profile_id)
        if profile is None:
            return None
        _validate_custom(profile)
        return profile

    def resolve_tier(self, profile_id: str) -> AgentProfileTierResolution:
        profile = self.get(profile_id)
        if profile is None:
            raise AgentProfileError("agent profile does not exist")
        if not profile.enabled:
            raise AgentProfileError("agent profile is disabled")
        return AgentProfileTierResolution(
            profile_id=profile.profile_id, profile_revision=profile.revision,
            role=profile.role, model_tier=profile.model_tier,
            budget_limit=profile.budget_limit, capability_ids=profile.capability_ids,
            max_concurrent_children=profile.max_concurrent_children,
            max_depth=profile.max_depth, max_steps=profile.max_steps,
            timeout_ms=profile.timeout_ms, allow_child_spawn=profile.allow_child_spawn,
            model_route_key=profile.model_route_key,
            model_route_revision=profile.model_route_revision,
        )

    def upgrade_untouched_builtin_limits(self) -> tuple[str, ...]:
        """CAS-upgrade only persisted overrides still matching all old limits."""
        upgraded = []
        for identity, previous in _PREVIOUS_BUILTIN_LIMITS.items():
            stored = self._store.get(identity)
            if stored is None or (stored.budget_limit, stored.max_steps, stored.timeout_ms) != previous:
                continue
            default = _BUILTINS_BY_ID[identity]
            self.update(replace(stored, revision=stored.revision + 1,
                budget_limit=default.budget_limit, max_steps=default.max_steps,
                timeout_ms=default.timeout_ms), expected_revision=stored.revision)
            upgraded.append(identity)
        return tuple(upgraded)

    def create_custom(self, profile: AgentProfile) -> AgentProfile:
        _validate_custom(profile)
        if profile.revision != 1:
            raise AgentProfileConflict("new custom agent profile revision must be one")
        self._store.create(profile)
        return profile

    def create_custom_from_payload(self, payload: object) -> AgentProfile:
        return self.create_custom(_profile_from_payload(payload))

    def update(self, profile: AgentProfile, *, expected_revision: int) -> AgentProfile:
        """CAS-update a built-in override or a custom subagent profile.

        Built-ins keep their fixed identity and role, while their stored
        configuration overrides the immutable revision-one default.
        """

        _validate_stored(profile)
        _expected_revision(expected_revision)
        current = self.get(profile.profile_id)
        if current is None:
            raise AgentProfileConflict("agent profile does not exist")
        _expect_current(current, expected_revision)
        if profile.role != current.role:
            raise AgentProfileError("agent profile role cannot change")
        if profile.profile_id in {"main.orchestrator", "steward.scheduler"} and not profile.enabled:
            raise AgentProfileError("required built-in agent profile cannot be disabled")
        if profile.revision != current.revision + 1:
            raise AgentProfileConflict("agent profile revision must advance by one")
        stored = self._store.get(profile.profile_id)
        if stored is None:
            # Persisting a first built-in override is compatible with stores
            # that do not seed defaults.  The candidate still advances from
            # the visible default revision, so CAS remains monotonic.
            self._store.create(profile)
        else:
            self._store.replace(profile, expected_revision=expected_revision)
        return profile

    def update_from_payload(self, payload: object, *, expected_revision: int) -> AgentProfile:
        return self.update(_profile_from_payload(payload), expected_revision=expected_revision)

    def update_custom(self, profile: AgentProfile, *, expected_revision: int) -> AgentProfile:
        _validate_custom(profile)
        return self.update(profile, expected_revision=expected_revision)

    def update_custom_from_payload(self, payload: object, *, expected_revision: int) -> AgentProfile:
        return self.update_custom(_profile_from_payload(payload), expected_revision=expected_revision)

    def set_enabled(self, profile_id: str, *, enabled: bool, expected_revision: int) -> AgentProfile:
        if not isinstance(enabled, bool):
            raise AgentProfileError("agent profile enabled must be boolean")
        current = self.get(profile_id)
        if current is None:
            raise AgentProfileConflict("agent profile does not exist")
        if current.profile_id in {"main.orchestrator", "steward.scheduler"} and not enabled:
            raise AgentProfileError("required built-in agent profile cannot be disabled")
        _expect_current(current, expected_revision)
        updated = replace(current, revision=current.revision + 1, enabled=enabled)
        return self.update(updated, expected_revision=current.revision)

    def delete_custom(self, profile_id: str, *, expected_revision: int) -> None:
        if profile_id in _BUILTINS_BY_ID:
            raise AgentProfileError("built-in agent profile cannot be deleted")
        profile = self.get(profile_id)
        if profile is None:
            raise AgentProfileConflict("agent profile does not exist")
        _validate_custom(profile)
        _expect_current(profile, expected_revision)
        self._store.delete(profile_id, expected_revision=expected_revision)

    def _custom_profiles(self) -> tuple[AgentProfile, ...]:
        profiles = tuple(
            profile for profile in self._store.list()
            if profile.profile_id not in _BUILTINS_BY_ID
        )
        for profile in profiles:
            _validate_custom(profile)
        return profiles

    def _validate_store_boundary(self) -> None:
        for profile in self._store.list():
            _validate_stored(profile)

    def _stored_or_default(self, default: AgentProfile) -> AgentProfile:
        stored = self._store.get(default.profile_id)
        if stored is None:
            return default
        _validate_builtin_override(stored, default)
        return stored


def builtin_agent_profiles() -> tuple[AgentProfile, ...]:
    """Return the five built-in profiles in stable UI order."""

    return _BUILTIN_PROFILES


def _profile_from_payload(payload: object) -> AgentProfile:
    try:
        return agent_profile_from_payload(payload)
    except AgentContractError as error:
        raise AgentProfileError("agent profile payload is invalid") from error


def _validate_profile(profile: AgentProfile) -> None:
    if not isinstance(profile, AgentProfile):
        raise AgentProfileError("agent profile must use the governed contract")
    try:
        # Reconstruct to re-run the contract at every persistence boundary.
        AgentProfile(
            profile_id=profile.profile_id, revision=profile.revision,
            display_name=profile.display_name,
            organization_role=profile.organization_role,
            work_description=profile.work_description,
            instructions=profile.instructions,
            enabled=profile.enabled,
            role=profile.role, model_tier=profile.model_tier,
            budget_limit=profile.budget_limit, capability_ids=profile.capability_ids,
            max_concurrent_children=profile.max_concurrent_children,
            max_depth=profile.max_depth, max_steps=profile.max_steps,
            timeout_ms=profile.timeout_ms, allow_child_spawn=profile.allow_child_spawn,
            model_route_key=profile.model_route_key,
            model_route_revision=profile.model_route_revision,
        )
    except AgentContractError as error:
        raise AgentProfileError("agent profile contract is invalid") from error


def _validate_custom(profile: AgentProfile) -> None:
    _validate_profile(profile)
    if not profile.profile_id.startswith("subagent.custom.") or profile.role != "subagent":
        raise AgentProfileError("only custom subagent profiles may be changed")


def _validate_builtin_override(profile: AgentProfile, default: AgentProfile) -> None:
    _validate_profile(profile)
    if profile.profile_id != default.profile_id or profile.role != default.role:
        raise AgentProfileError("built-in agent profile identity or role drifted")
    if profile.revision == default.revision and profile != default:
        raise AgentProfileError("built-in revision-one profile must match its default")
    if profile.profile_id in {"main.orchestrator", "steward.scheduler"} and not profile.enabled:
        raise AgentProfileError("required built-in agent profile cannot be disabled")


def _validate_stored(profile: AgentProfile) -> None:
    default = _BUILTINS_BY_ID.get(profile.profile_id)
    if default is None:
        _validate_custom(profile)
        return
    _validate_builtin_override(profile, default)


def _expected_revision(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AgentProfileError("expected profile revision is invalid")


def _expect_current(profile: AgentProfile, expected_revision: int) -> None:
    _expected_revision(expected_revision)
    if profile.revision != expected_revision:
        raise AgentProfileConflict(
            f"agent profile revision conflict: expected {expected_revision}, current {profile.revision}"
        )
