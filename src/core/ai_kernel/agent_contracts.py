"""Immutable contracts for governed main-agent and child-agent execution.

These are deliberately domain-only records.  They do not select a provider,
start a Turn, persist data, or make a permission decision.  The coordinator
and store must use these values as the fail-closed boundary for delegation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
import unicodedata


AGENT_CONTRACT_SCHEMA_VERSION = "1.0.0"
AGENT_PROFILE_SCHEMA_VERSION = "1.3.0"
_ROUTE_BOUND_AGENT_PROFILE_SCHEMA_VERSION = "1.2.0"
_LEGACY_AGENT_PROFILE_SCHEMA_VERSION = "1.0.0"
_TIER_ONLY_AGENT_PROFILE_SCHEMA_VERSION = "1.1.0"

BUILTIN_AGENT_INSTRUCTIONS = {
    'main.orchestrator': '你是主 Agent，对用户的原始任务负责，最终答复由你给出。\n- 没有专家时，自己用可用能力完成任务。\n- 有专家时，agent.list 结果里 fan_ins[].result.children[].conclusion 是各位专家交回的结论。逐条阅读，采纳有依据的内容；专家之间有分歧时，说明分歧并给出你的判断和理由；专家失败或没有结论的部分，用你自己的能力补上，或明确告诉用户缺了什么。\n- 不要编造专家没有给出的事实，不要提及内部编号、引用地址或系统细节。\n- 最终 summary 直接回答原始任务，使用用户的语言。',
    'steward.scheduler': '你是管家，只决定要不要请专家、怎么分工，不回答任务本身。\n- 一步就能完成、不需要查资料、也不需要多个角度的任务，返回 main_only。\n- 需要专家时，把任务拆成互不重叠的子任务，每个子任务交给 profiles 里最合适的一位专家，人数不超过 slots。\n- 每个子任务要能独立完成：写清楚做什么、范围、交付什么。专家看不到原任务，只看得到你写的子任务。\n- 每个子任务不超过 300 字。只输出要求的 JSON。',
    'subagent.explorer': '你是探索专家，只读，负责查找和整理证据。\n- 先用可用的读取能力查找与子任务相关的资料，不要凭记忆编造。\n- 最终 summary 是交给主 Agent 的唯一结果：列出结论要点（最多 5 条），每条说明依据来自哪份资料；查不到的明确写"未找到"。\n- 不超过 1500 字。',
    'subagent.worker': '你是执行专家，在给定能力内完成子任务，产出可以直接使用的成果。\n- 需要写入时只使用提案类能力，不直接发布或覆盖用户内容。\n- 最终 summary 是交给主 Agent 的唯一结果：写明完成了什么、成果的要点或正文、还有什么没完成及原因。\n- 不超过 1500 字。',
    'subagent.reviewer': '你是审阅专家，只读，负责核验，不重写方案。\n- 逐条检查子任务中的说法、方案或结果，给出"成立 / 存疑 / 不成立"，附理由和风险。\n- 子任务要求特定输出格式时（例如一行 VERDICT=…），严格按要求输出，不加其他内容。\n- 否则最终 summary 按"结论—理由—风险"写，不超过 1500 字。',
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE_ID_RE = re.compile(r"^(?:main\.orchestrator|steward\.scheduler|subagent\.(?:explorer|worker|reviewer)|subagent\.custom\.[a-z][a-z0-9._-]{0,63})$")
_CAPABILITY_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REF_RE = re.compile(r"^crp://[A-Za-z0-9._~-]{1,64}/[A-Za-z0-9._~/-]{1,384}$")
_MODEL_TIERS = frozenset({"fast", "standard", "deep"})
_ROLES = frozenset({"main", "subagent"})
_RUN_STATUSES = frozenset({"created", "queued", "starting", "running", "waiting", "waiting_approval", "cancelling", "recovery_required", "quarantined", "completed", "failed", "cancelled", "timed_out"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out"})
_LINK_STATUSES = frozenset({"reserved", "spawned", "started", "cancelling", "stopped", "completed", "failed", "cancelled", "timed_out", "quarantined"})
_MESSAGE_KINDS = frozenset({"task", "progress", "result", "control"})
_RESERVATION_STATUSES = frozenset({"reserved", "settled", "released"})
_FAN_IN_STATUSES = frozenset({"open", "collecting", "completed", "failed", "cancelled"})
_FAN_IN_POLICIES = frozenset({"all", "any", "quorum"})
_SENSITIVE_FIELD_NAMES = frozenset({"provider_id", "model_name", "endpoint", "secret", "token", "api_key"})
_MAX_COUNT = 2_147_483_647
_MAX_WALL_TIME_MS = 86_400_000
_RUN_PAYLOAD_FIELDS = {
    "schema_version", "run_id", "turn_id", "project_id", "profile_id", "profile_revision",
    "role", "model_tier", "model_route_key", "model_route_revision", "status", "depth", "cancel_epoch", "budget_limit",
    "capability_ids", "max_concurrent_children", "max_depth", "max_steps", "timeout_ms",
    "allow_child_spawn", "model_routing_snapshot_ref", "capability_manifest_ref",
    "context_manifest_ref", "budget_snapshot_ref", "terminal_receipt_ref", "parent_run_id",
}


class AgentContractError(ValueError):
    """Raised when an internal-agent coordination contract is malformed."""


@dataclass(frozen=True, slots=True)
class AgentBudget:
    model_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    wall_time_ms: int

    def __post_init__(self) -> None:
        for field in ("model_calls", "tool_calls", "input_tokens", "output_tokens"):
            _count(getattr(self, field), field, maximum=_MAX_COUNT)
        _count(self.wall_time_ms, "wall_time_ms", maximum=_MAX_WALL_TIME_MS)

    def is_subset_of(self, parent: "AgentBudget") -> bool:
        return all(
            getattr(self, field) <= getattr(parent, field)
            for field in _BUDGET_FIELDS
        )

    def remaining_after(self, used: "AgentBudget") -> "AgentBudget":
        if not used.is_subset_of(self):
            raise AgentContractError("budget usage exceeds budget limit")
        return AgentBudget(**{
            field: getattr(self, field) - getattr(used, field)
            for field in _BUDGET_FIELDS
        })

    def plus(self, other: "AgentBudget") -> "AgentBudget":
        values = {field: getattr(self, field) + getattr(other, field) for field in _BUDGET_FIELDS}
        return AgentBudget(**values)


_BUDGET_FIELDS = ("model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms")


@dataclass(frozen=True, slots=True)
class AgentProfile:
    profile_id: str
    revision: int
    display_name: str
    enabled: bool
    role: str
    model_tier: str
    budget_limit: AgentBudget
    capability_ids: tuple[str, ...]
    max_concurrent_children: int
    max_depth: int
    max_steps: int
    timeout_ms: int
    allow_child_spawn: bool
    organization_role: str = "未配置岗位"
    work_description: str = "未配置工作介绍"
    model_route_key: str | None = None
    model_route_revision: int | None = None
    instructions: str = ""

    def __post_init__(self) -> None:
        _profile_id(self.profile_id)
        _positive(self.revision, "profile revision")
        _display_name(self.display_name)
        _organization_role(self.organization_role)
        _work_description(self.work_description)
        _instructions(self.instructions)
        _boolean(self.enabled, "agent profile enabled")
        if self.role not in _ROLES:
            raise AgentContractError("agent profile role is invalid")
        if self.profile_id == "main.orchestrator" and self.role != "main":
            raise AgentContractError("main profile must use main role")
        if _is_subagent_profile(self.profile_id) and self.role != "subagent":
            raise AgentContractError("subagent profile must use subagent role")
        if self.model_tier not in _MODEL_TIERS:
            raise AgentContractError("agent profile model tier is invalid")
        if (self.model_route_key is None) != (self.model_route_revision is None):
            raise AgentContractError("agent profile model route binding is incomplete")
        if self.model_route_key is not None:
            _identifier(self.model_route_key, "agent profile model route key")
            _positive(self.model_route_revision, "agent profile model route revision")
        _capabilities(self.capability_ids, "agent profile capabilities")
        _count(self.max_concurrent_children, "agent profile concurrency", maximum=8)
        _count(self.max_depth, "agent profile depth", maximum=4)
        _steps(self.max_steps, "agent profile max steps")
        _timeout(self.timeout_ms, "agent profile timeout")
        _boolean(self.allow_child_spawn, "agent profile child spawn")
        if not self.allow_child_spawn and self.max_concurrent_children != 0:
            raise AgentContractError("profile without child spawn must have zero concurrency")
        if self.allow_child_spawn and (self.max_concurrent_children == 0 or self.max_depth == 0):
            raise AgentContractError("profile with child spawn requires positive concurrency and depth")
        if self.role == "subagent" and self.allow_child_spawn and self.profile_id != "subagent.worker":
            raise AgentContractError("only worker subagent profiles may delegate further")


@dataclass(frozen=True, slots=True)
class AgentRun:
    run_id: str
    turn_id: str
    project_id: str
    profile_id: str
    profile_revision: int
    role: str
    model_tier: str
    status: str
    depth: int
    cancel_epoch: int
    budget_limit: AgentBudget
    capability_ids: tuple[str, ...]
    max_concurrent_children: int
    max_depth: int
    max_steps: int
    timeout_ms: int
    allow_child_spawn: bool
    model_routing_snapshot_ref: str | None
    capability_manifest_ref: str | None
    context_manifest_ref: str | None
    budget_snapshot_ref: str | None
    terminal_receipt_ref: str | None
    parent_run_id: str | None = None
    model_route_key: str | None = None
    model_route_revision: int | None = None

    def __post_init__(self) -> None:
        for label, value in (("run_id", self.run_id), ("turn_id", self.turn_id), ("project_id", self.project_id)):
            _identifier(value, label)
        _profile_id(self.profile_id)
        _positive(self.profile_revision, "run profile revision")
        if self.role not in _ROLES or self.model_tier not in _MODEL_TIERS or self.status not in _RUN_STATUSES:
            raise AgentContractError("agent run state is invalid")
        if (self.model_route_key is None) != (self.model_route_revision is None):
            raise AgentContractError("agent run model route binding is incomplete")
        if self.model_route_key is not None:
            _identifier(self.model_route_key, "agent run model route key")
            _positive(self.model_route_revision, "agent run model route revision")
        _count(self.depth, "run depth", maximum=8)
        _count(self.cancel_epoch, "run cancel epoch", maximum=_MAX_COUNT)
        _capabilities(self.capability_ids, "agent run capabilities")
        _count(self.max_concurrent_children, "run concurrency", maximum=8)
        _count(self.max_depth, "run depth limit", maximum=4)
        _steps(self.max_steps, "run max steps")
        _timeout(self.timeout_ms, "run timeout")
        _boolean(self.allow_child_spawn, "run child spawn")
        if not self.allow_child_spawn and self.max_concurrent_children != 0:
            raise AgentContractError("run without child spawn must have zero concurrency")
        if self.allow_child_spawn and (self.max_concurrent_children == 0 or self.max_depth == 0):
            raise AgentContractError("run with child spawn requires positive concurrency and depth")
        if self.depth > self.max_depth:
            raise AgentContractError("agent run depth exceeds its limit")
        frozen_refs = (
            self.model_routing_snapshot_ref, self.capability_manifest_ref,
            self.context_manifest_ref, self.budget_snapshot_ref,
        )
        if self.status not in {"created", "queued"} and any(item is None for item in frozen_refs):
            raise AgentContractError("started agent run requires frozen snapshots")
        for label, ref in zip(("model routing snapshot", "capability manifest", "context manifest", "budget snapshot"), frozen_refs):
            if ref is not None:
                _ref(ref, label)
        if self.status in _TERMINAL_RUN_STATUSES and self.terminal_receipt_ref is None:
            raise AgentContractError("terminal agent run requires receipt")
        if self.terminal_receipt_ref is not None:
            _ref(self.terminal_receipt_ref, "terminal receipt")
        if self.role == "main":
            if self.parent_run_id is not None or self.depth != 0 or self.profile_id != "main.orchestrator":
                raise AgentContractError("main agent run parent state is invalid")
        else:
            _identifier(self.parent_run_id, "subagent parent run")
            if self.depth < 1 or not _is_subagent_profile(self.profile_id):
                raise AgentContractError("subagent run parent state is invalid")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_RUN_STATUSES


@dataclass(frozen=True, slots=True)
class AgentChildLink:
    link_id: str
    parent_run_id: str
    child_run_id: str
    parent_project_id: str
    child_project_id: str
    spawn_operation_id: str
    parent_cancel_epoch: int
    child_depth: int
    delegated_budget: AgentBudget
    delegated_capability_ids: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        for label, value in (("link_id", self.link_id), ("parent_run_id", self.parent_run_id), ("child_run_id", self.child_run_id), ("parent_project_id", self.parent_project_id), ("child_project_id", self.child_project_id), ("spawn operation", self.spawn_operation_id)):
            _identifier(value, label)
        if self.parent_run_id == self.child_run_id:
            raise AgentContractError("child link cannot self-reference")
        if self.parent_project_id != self.child_project_id:
            raise AgentContractError("child link cannot cross project scope")
        _count(self.parent_cancel_epoch, "child link cancel epoch", maximum=_MAX_COUNT)
        _count(self.child_depth, "child link depth", maximum=8)
        _capabilities(self.delegated_capability_ids, "child link delegated capabilities")
        if self.status not in _LINK_STATUSES:
            raise AgentContractError("child link status is invalid")


@dataclass(frozen=True, slots=True)
class AgentMessage:
    message_id: str
    project_id: str
    sender_run_id: str
    recipient_run_id: str
    operation_id: str
    sequence: int
    kind: str
    payload_ref: str
    cancel_epoch: int
    status: str

    def __post_init__(self) -> None:
        for label, value in (("message_id", self.message_id), ("project_id", self.project_id), ("sender_run_id", self.sender_run_id), ("recipient_run_id", self.recipient_run_id), ("message operation", self.operation_id)):
            _identifier(value, label)
        if self.sender_run_id == self.recipient_run_id:
            raise AgentContractError("agent message cannot target its sender")
        _positive(self.sequence, "message sequence")
        if self.kind not in _MESSAGE_KINDS:
            raise AgentContractError("agent message kind is invalid")
        _ref(self.payload_ref, "message payload ref")
        _count(self.cancel_epoch, "message cancel epoch", maximum=_MAX_COUNT)
        if self.status not in {"pending", "delivered", "acknowledged", "cancelled"}:
            raise AgentContractError("agent message status is invalid")


@dataclass(frozen=True, slots=True)
class AgentBudgetReservation:
    reservation_id: str
    project_id: str
    parent_run_id: str
    child_run_id: str
    operation_id: str
    parent_cancel_epoch: int
    reserved_budget: AgentBudget
    settled_budget: AgentBudget | None
    status: str

    def __post_init__(self) -> None:
        for label, value in (("reservation_id", self.reservation_id), ("project_id", self.project_id), ("parent_run_id", self.parent_run_id), ("child_run_id", self.child_run_id), ("reservation operation", self.operation_id)):
            _identifier(value, label)
        if self.parent_run_id == self.child_run_id:
            raise AgentContractError("budget reservation cannot self-reference")
        _count(self.parent_cancel_epoch, "reservation cancel epoch", maximum=_MAX_COUNT)
        if self.status not in _RESERVATION_STATUSES:
            raise AgentContractError("budget reservation status is invalid")
        if self.status == "reserved" and self.settled_budget is not None:
            raise AgentContractError("reserved budget cannot have settlement")
        if self.status in {"settled", "released"} and self.settled_budget is None:
            raise AgentContractError("terminal budget reservation requires settlement")
        if self.settled_budget is not None and not self.settled_budget.is_subset_of(self.reserved_budget):
            raise AgentContractError("budget settlement exceeds reservation")


@dataclass(frozen=True, slots=True)
class AgentFanIn:
    fan_in_id: str
    project_id: str
    parent_run_id: str
    operation_id: str
    child_run_ids: tuple[str, ...]
    policy: str
    quorum: int | None
    cancel_epoch: int
    status: str

    def __post_init__(self) -> None:
        for label, value in (("fan-in id", self.fan_in_id), ("fan-in project", self.project_id), ("fan-in parent", self.parent_run_id), ("fan-in operation", self.operation_id)):
            _identifier(value, label)
        if not isinstance(self.child_run_ids, tuple) or not self.child_run_ids or len(set(self.child_run_ids)) != len(self.child_run_ids):
            raise AgentContractError("fan-in children are invalid")
        if self.parent_run_id in self.child_run_ids:
            raise AgentContractError("fan-in parent cannot be a child")
        for item in self.child_run_ids:
            _identifier(item, "fan-in child")
        if self.policy not in _FAN_IN_POLICIES or self.status not in _FAN_IN_STATUSES:
            raise AgentContractError("fan-in state is invalid")
        if self.policy == "quorum":
            if not isinstance(self.quorum, int) or isinstance(self.quorum, bool) or not 1 <= self.quorum <= len(self.child_run_ids):
                raise AgentContractError("fan-in quorum is invalid")
        elif self.quorum is not None:
            raise AgentContractError("non-quorum fan-in must not carry quorum")
        _count(self.cancel_epoch, "fan-in cancel epoch", maximum=_MAX_COUNT)


@dataclass(frozen=True, slots=True)
class AgentTerminalChildSummary:
    child_run_id: str
    project_id: str
    status: str
    receipt_ref: str
    summary_ref: str
    evidence_refs: tuple[str, ...]
    usage: AgentBudget
    error_code: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.child_run_id, "terminal child run")
        _identifier(self.project_id, "terminal child project")
        if self.status not in _TERMINAL_RUN_STATUSES:
            raise AgentContractError("terminal child status is invalid")
        _ref(self.receipt_ref, "terminal child receipt ref")
        _ref(self.summary_ref, "terminal child summary ref")
        if (
            not isinstance(self.evidence_refs, tuple)
            or len(self.evidence_refs) != len(set(self.evidence_refs))
            or any(not isinstance(item, str) or not _REF_RE.fullmatch(item) for item in self.evidence_refs)
        ):
            raise AgentContractError("terminal child evidence refs are invalid")
        if self.error_code is not None and not _CAPABILITY_ID_RE.fullmatch(self.error_code):
            raise AgentContractError("terminal child error code is invalid")
        if self.status == "completed" and self.error_code is not None:
            raise AgentContractError("completed child cannot carry an error code")
        if self.status != "completed" and self.error_code is None:
            raise AgentContractError("failed child requires an error code")


@dataclass(frozen=True, slots=True)
class AgentFanInResult:
    result_id: str
    fan_in_id: str
    project_id: str
    parent_run_id: str
    status: str
    child_summaries: tuple[AgentTerminalChildSummary, ...]
    receipt_ref: str
    result_ref: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("fan-in result id", self.result_id), ("fan-in result fan-in", self.fan_in_id), ("fan-in result project", self.project_id), ("fan-in result parent", self.parent_run_id)):
            _identifier(value, label)
        if self.status not in _TERMINAL_RUN_STATUSES:
            raise AgentContractError("fan-in result status is invalid")
        if not isinstance(self.child_summaries, tuple) or not self.child_summaries:
            raise AgentContractError("fan-in result requires child summaries")
        child_ids = tuple(item.child_run_id for item in self.child_summaries)
        if len(child_ids) != len(set(child_ids)):
            raise AgentContractError("fan-in child summaries must be unique")
        if any(item.project_id != self.project_id for item in self.child_summaries):
            raise AgentContractError("fan-in result cannot cross project scope")
        _ref(self.receipt_ref, "fan-in receipt ref")
        if self.result_ref is not None:
            _ref(self.result_ref, "fan-in result ref")


def validate_child_delegation(parent: AgentRun, child: AgentRun, link: AgentChildLink) -> None:
    """Fail closed unless a child run is a strict subset of its parent run."""

    if parent.run_id != link.parent_run_id or child.run_id != link.child_run_id:
        raise AgentContractError("child link run identity drifted")
    if parent.project_id != child.project_id or parent.project_id != link.parent_project_id or child.project_id != link.child_project_id:
        raise AgentContractError("child delegation cannot cross project scope")
    if parent.role not in _ROLES or child.role != "subagent" or child.parent_run_id != parent.run_id:
        raise AgentContractError("child delegation role binding is invalid")
    if not parent.allow_child_spawn:
        raise AgentContractError("parent agent run cannot spawn children")
    if child.depth != parent.depth + 1 or link.child_depth != child.depth or child.depth > parent.max_depth:
        raise AgentContractError("child delegation depth exceeds parent limit")
    if link.parent_cancel_epoch != parent.cancel_epoch:
        raise AgentContractError("child delegation cancel epoch drifted")
    if not child.budget_limit.is_subset_of(parent.budget_limit) or link.delegated_budget != child.budget_limit:
        raise AgentContractError("child delegation budget exceeds parent limit")
    if not set(child.capability_ids).issubset(parent.capability_ids) or link.delegated_capability_ids != child.capability_ids:
        raise AgentContractError("child delegation capabilities exceed parent limit")
    if child.max_concurrent_children > parent.max_concurrent_children or child.max_depth > parent.max_depth:
        raise AgentContractError("child delegation limits exceed parent limit")
    if child.max_steps > parent.max_steps or child.timeout_ms > parent.timeout_ms:
        raise AgentContractError("child delegation execution limit exceeds parent limit")
    if child.allow_child_spawn and not parent.allow_child_spawn:
        raise AgentContractError("child delegation child spawn exceeds parent limit")


def validate_fan_in_result(fan_in: AgentFanIn, result: AgentFanInResult) -> None:
    if (fan_in.fan_in_id, fan_in.project_id, fan_in.parent_run_id) != (result.fan_in_id, result.project_id, result.parent_run_id):
        raise AgentContractError("fan-in result identity drifted")
    summaries = {item.child_run_id for item in result.child_summaries}
    expected = set(fan_in.child_run_ids)
    if not summaries.issubset(expected):
        raise AgentContractError("fan-in result contains an unbound child")
    if fan_in.policy == "all" and summaries != expected:
        raise AgentContractError("all fan-in result is incomplete")
    if fan_in.policy == "any" and not summaries:
        raise AgentContractError("any fan-in result is incomplete")
    if fan_in.policy == "quorum" and len(summaries) < int(fan_in.quorum):
        raise AgentContractError("quorum fan-in result is incomplete")


def agent_budget_to_payload(value: AgentBudget) -> dict[str, object]:
    return {field: getattr(value, field) for field in _BUDGET_FIELDS}


def agent_budget_from_payload(value: object) -> AgentBudget:
    payload = _shape(value, "agent budget", set(_BUDGET_FIELDS))
    return AgentBudget(**{field: _integer(payload[field], field) for field in _BUDGET_FIELDS})


def agent_profile_to_payload(value: AgentProfile) -> dict[str, object]:
    return {"schema_version": AGENT_PROFILE_SCHEMA_VERSION, "profile_id": value.profile_id, "revision": value.revision, "display_name": value.display_name, "organization_role": value.organization_role, "work_description": value.work_description, "instructions": value.instructions, "enabled": value.enabled, "role": value.role, "model_tier": value.model_tier, "model_route_key": value.model_route_key, "model_route_revision": value.model_route_revision, "budget_limit": agent_budget_to_payload(value.budget_limit), "capability_ids": list(value.capability_ids), "max_concurrent_children": value.max_concurrent_children, "max_depth": value.max_depth, "max_steps": value.max_steps, "timeout_ms": value.timeout_ms, "allow_child_spawn": value.allow_child_spawn}


def agent_profile_from_payload(value: object) -> AgentProfile:
    payload = _agent_profile_shape(value)
    profile_id = _string(payload["profile_id"], "profile_id")
    legacy = payload["schema_version"] == _LEGACY_AGENT_PROFILE_SCHEMA_VERSION
    tier_only = payload["schema_version"] in {_LEGACY_AGENT_PROFILE_SCHEMA_VERSION, _TIER_ONLY_AGENT_PROFILE_SCHEMA_VERSION}
    organization_role, work_description = _legacy_profile_metadata(profile_id) if legacy else (
        _string(payload["organization_role"], "organization_role"),
        _string(payload["work_description"], "work_description"),
    )
    return AgentProfile(profile_id=profile_id, revision=_integer(payload["revision"], "revision"), display_name=_string(payload["display_name"], "display_name"), organization_role=organization_role, work_description=work_description, instructions=(_string(payload["instructions"], "instructions") if payload["schema_version"] == AGENT_PROFILE_SCHEMA_VERSION else BUILTIN_AGENT_INSTRUCTIONS.get(profile_id, "")), enabled=_boolean(payload["enabled"], "enabled"), role=_string(payload["role"], "role"), model_tier=_string(payload["model_tier"], "model_tier"), model_route_key=None if tier_only else _optional_identifier(payload["model_route_key"], "agent profile model route key"), model_route_revision=None if tier_only else _optional_positive(payload["model_route_revision"], "agent profile model route revision"), budget_limit=agent_budget_from_payload(payload["budget_limit"]), capability_ids=_string_tuple(payload["capability_ids"], "capability_ids"), max_concurrent_children=_integer(payload["max_concurrent_children"], "max_concurrent_children"), max_depth=_integer(payload["max_depth"], "max_depth"), max_steps=_integer(payload["max_steps"], "max_steps"), timeout_ms=_integer(payload["timeout_ms"], "timeout_ms"), allow_child_spawn=_boolean(payload["allow_child_spawn"], "allow_child_spawn"))


def agent_role_brief_to_payload(profile: AgentProfile) -> dict[str, object]:
    return {"schema_version": "1.0.0", "kind": "agent.role-brief.v1",
            "profile_id": profile.profile_id, "profile_revision": profile.revision,
            "organization_role": profile.organization_role,
            "work_description": profile.work_description, "instructions": profile.instructions}


def agent_role_brief_from_payload(value: object) -> dict[str, str]:
    payload = _shape(value, "agent role brief", {"schema_version", "kind", "profile_id",
        "profile_revision", "organization_role", "work_description", "instructions"})
    if payload["schema_version"] != "1.0.0" or payload["kind"] != "agent.role-brief.v1":
        raise AgentContractError("agent role brief schema is invalid")
    _profile_id(payload["profile_id"])
    _positive(payload["profile_revision"], "profile revision")
    _organization_role(payload["organization_role"])
    _work_description(payload["work_description"])
    _instructions(payload["instructions"])
    return {key: _string(payload[key], key) for key in
            ("organization_role", "work_description", "instructions")}


def agent_run_to_payload(value: AgentRun) -> dict[str, object]:
    return {"schema_version": AGENT_CONTRACT_SCHEMA_VERSION, "run_id": value.run_id, "turn_id": value.turn_id, "project_id": value.project_id, "profile_id": value.profile_id, "profile_revision": value.profile_revision, "role": value.role, "model_tier": value.model_tier, "model_route_key": value.model_route_key, "model_route_revision": value.model_route_revision, "status": value.status, "depth": value.depth, "cancel_epoch": value.cancel_epoch, "budget_limit": agent_budget_to_payload(value.budget_limit), "capability_ids": list(value.capability_ids), "max_concurrent_children": value.max_concurrent_children, "max_depth": value.max_depth, "max_steps": value.max_steps, "timeout_ms": value.timeout_ms, "allow_child_spawn": value.allow_child_spawn, "model_routing_snapshot_ref": value.model_routing_snapshot_ref, "capability_manifest_ref": value.capability_manifest_ref, "context_manifest_ref": value.context_manifest_ref, "budget_snapshot_ref": value.budget_snapshot_ref, "terminal_receipt_ref": value.terminal_receipt_ref, "parent_run_id": value.parent_run_id}


def agent_run_from_payload(value: object) -> AgentRun:
    if not isinstance(value, Mapping):
        raise AgentContractError("agent run must be a mapping")
    normalized = dict(value)
    normalized.setdefault("model_route_key", None)
    normalized.setdefault("model_route_revision", None)
    payload = _shape(normalized, "agent run", _RUN_PAYLOAD_FIELDS)
    _schema(payload)
    parent = payload["parent_run_id"]
    if parent is not None and not isinstance(parent, str):
        raise AgentContractError("parent_run_id must be a string or null")
    refs = {name: payload[name] for name in ("model_routing_snapshot_ref", "capability_manifest_ref", "context_manifest_ref", "budget_snapshot_ref", "terminal_receipt_ref")}
    if any(item is not None and not isinstance(item, str) for item in refs.values()):
        raise AgentContractError("agent run references must be strings or null")
    return AgentRun(run_id=_string(payload["run_id"], "run_id"), turn_id=_string(payload["turn_id"], "turn_id"), project_id=_string(payload["project_id"], "project_id"), profile_id=_string(payload["profile_id"], "profile_id"), profile_revision=_integer(payload["profile_revision"], "profile_revision"), role=_string(payload["role"], "role"), model_tier=_string(payload["model_tier"], "model_tier"), model_route_key=_optional_identifier(payload["model_route_key"], "agent run model route key"), model_route_revision=_optional_positive(payload["model_route_revision"], "agent run model route revision"), status=_string(payload["status"], "status"), depth=_integer(payload["depth"], "depth"), cancel_epoch=_integer(payload["cancel_epoch"], "cancel_epoch"), budget_limit=agent_budget_from_payload(payload["budget_limit"]), capability_ids=_string_tuple(payload["capability_ids"], "capability_ids"), max_concurrent_children=_integer(payload["max_concurrent_children"], "max_concurrent_children"), max_depth=_integer(payload["max_depth"], "max_depth"), max_steps=_integer(payload["max_steps"], "max_steps"), timeout_ms=_integer(payload["timeout_ms"], "timeout_ms"), allow_child_spawn=_boolean(payload["allow_child_spawn"], "allow_child_spawn"), model_routing_snapshot_ref=refs["model_routing_snapshot_ref"], capability_manifest_ref=refs["capability_manifest_ref"], context_manifest_ref=refs["context_manifest_ref"], budget_snapshot_ref=refs["budget_snapshot_ref"], terminal_receipt_ref=refs["terminal_receipt_ref"], parent_run_id=parent)


def agent_child_link_to_payload(value: AgentChildLink) -> dict[str, object]:
    return {"schema_version": AGENT_CONTRACT_SCHEMA_VERSION, "link_id": value.link_id, "parent_run_id": value.parent_run_id, "child_run_id": value.child_run_id, "parent_project_id": value.parent_project_id, "child_project_id": value.child_project_id, "spawn_operation_id": value.spawn_operation_id, "parent_cancel_epoch": value.parent_cancel_epoch, "child_depth": value.child_depth, "delegated_budget": agent_budget_to_payload(value.delegated_budget), "delegated_capability_ids": list(value.delegated_capability_ids), "status": value.status}


def agent_child_link_from_payload(value: object) -> AgentChildLink:
    payload = _shape(value, "agent child link", {"schema_version", "link_id", "parent_run_id", "child_run_id", "parent_project_id", "child_project_id", "spawn_operation_id", "parent_cancel_epoch", "child_depth", "delegated_budget", "delegated_capability_ids", "status"})
    _schema(payload)
    return AgentChildLink(link_id=_string(payload["link_id"], "link_id"), parent_run_id=_string(payload["parent_run_id"], "parent_run_id"), child_run_id=_string(payload["child_run_id"], "child_run_id"), parent_project_id=_string(payload["parent_project_id"], "parent_project_id"), child_project_id=_string(payload["child_project_id"], "child_project_id"), spawn_operation_id=_string(payload["spawn_operation_id"], "spawn_operation_id"), parent_cancel_epoch=_integer(payload["parent_cancel_epoch"], "parent_cancel_epoch"), child_depth=_integer(payload["child_depth"], "child_depth"), delegated_budget=agent_budget_from_payload(payload["delegated_budget"]), delegated_capability_ids=_string_tuple(payload["delegated_capability_ids"], "delegated_capability_ids"), status=_string(payload["status"], "status"))


def agent_message_to_payload(value: AgentMessage) -> dict[str, object]:
    return {"schema_version": AGENT_CONTRACT_SCHEMA_VERSION, "message_id": value.message_id, "project_id": value.project_id, "sender_run_id": value.sender_run_id, "recipient_run_id": value.recipient_run_id, "operation_id": value.operation_id, "sequence": value.sequence, "kind": value.kind, "payload_ref": value.payload_ref, "cancel_epoch": value.cancel_epoch, "status": value.status}


def agent_message_from_payload(value: object) -> AgentMessage:
    payload = _shape(value, "agent message", {"schema_version", "message_id", "project_id", "sender_run_id", "recipient_run_id", "operation_id", "sequence", "kind", "payload_ref", "cancel_epoch", "status"})
    _schema(payload)
    return AgentMessage(message_id=_string(payload["message_id"], "message_id"), project_id=_string(payload["project_id"], "project_id"), sender_run_id=_string(payload["sender_run_id"], "sender_run_id"), recipient_run_id=_string(payload["recipient_run_id"], "recipient_run_id"), operation_id=_string(payload["operation_id"], "operation_id"), sequence=_integer(payload["sequence"], "sequence"), kind=_string(payload["kind"], "kind"), payload_ref=_string(payload["payload_ref"], "payload_ref"), cancel_epoch=_integer(payload["cancel_epoch"], "cancel_epoch"), status=_string(payload["status"], "status"))


def agent_budget_reservation_to_payload(value: AgentBudgetReservation) -> dict[str, object]:
    return {"schema_version": AGENT_CONTRACT_SCHEMA_VERSION, "reservation_id": value.reservation_id, "project_id": value.project_id, "parent_run_id": value.parent_run_id, "child_run_id": value.child_run_id, "operation_id": value.operation_id, "parent_cancel_epoch": value.parent_cancel_epoch, "reserved_budget": agent_budget_to_payload(value.reserved_budget), "settled_budget": None if value.settled_budget is None else agent_budget_to_payload(value.settled_budget), "status": value.status}


def agent_budget_reservation_from_payload(value: object) -> AgentBudgetReservation:
    payload = _shape(value, "agent budget reservation", {"schema_version", "reservation_id", "project_id", "parent_run_id", "child_run_id", "operation_id", "parent_cancel_epoch", "reserved_budget", "settled_budget", "status"})
    _schema(payload)
    settled = payload["settled_budget"]
    if settled is not None and not isinstance(settled, Mapping):
        raise AgentContractError("settled_budget must be a mapping or null")
    return AgentBudgetReservation(reservation_id=_string(payload["reservation_id"], "reservation_id"), project_id=_string(payload["project_id"], "project_id"), parent_run_id=_string(payload["parent_run_id"], "parent_run_id"), child_run_id=_string(payload["child_run_id"], "child_run_id"), operation_id=_string(payload["operation_id"], "operation_id"), parent_cancel_epoch=_integer(payload["parent_cancel_epoch"], "parent_cancel_epoch"), reserved_budget=agent_budget_from_payload(payload["reserved_budget"]), settled_budget=None if settled is None else agent_budget_from_payload(settled), status=_string(payload["status"], "status"))


def agent_fan_in_to_payload(value: AgentFanIn) -> dict[str, object]:
    return {"schema_version": AGENT_CONTRACT_SCHEMA_VERSION, "fan_in_id": value.fan_in_id, "project_id": value.project_id, "parent_run_id": value.parent_run_id, "operation_id": value.operation_id, "child_run_ids": list(value.child_run_ids), "policy": value.policy, "quorum": value.quorum, "cancel_epoch": value.cancel_epoch, "status": value.status}


def agent_fan_in_from_payload(value: object) -> AgentFanIn:
    payload = _shape(value, "agent fan-in", {"schema_version", "fan_in_id", "project_id", "parent_run_id", "operation_id", "child_run_ids", "policy", "quorum", "cancel_epoch", "status"})
    _schema(payload)
    quorum = payload["quorum"]
    if quorum is not None and (not isinstance(quorum, int) or isinstance(quorum, bool)):
        raise AgentContractError("quorum must be an integer or null")
    return AgentFanIn(fan_in_id=_string(payload["fan_in_id"], "fan_in_id"), project_id=_string(payload["project_id"], "project_id"), parent_run_id=_string(payload["parent_run_id"], "parent_run_id"), operation_id=_string(payload["operation_id"], "operation_id"), child_run_ids=_string_tuple(payload["child_run_ids"], "child_run_ids"), policy=_string(payload["policy"], "policy"), quorum=quorum, cancel_epoch=_integer(payload["cancel_epoch"], "cancel_epoch"), status=_string(payload["status"], "status"))


def agent_fan_in_result_to_payload(value: AgentFanInResult) -> dict[str, object]:
    return {"schema_version": AGENT_CONTRACT_SCHEMA_VERSION, "result_id": value.result_id, "fan_in_id": value.fan_in_id, "project_id": value.project_id, "parent_run_id": value.parent_run_id, "status": value.status, "child_summaries": [_terminal_summary_to_payload(item) for item in value.child_summaries], "receipt_ref": value.receipt_ref, "result_ref": value.result_ref}


def agent_fan_in_result_from_payload(value: object) -> AgentFanInResult:
    payload = _shape(value, "agent fan-in result", {"schema_version", "result_id", "fan_in_id", "project_id", "parent_run_id", "status", "child_summaries", "receipt_ref", "result_ref"})
    _schema(payload)
    summaries = payload["child_summaries"]
    if not isinstance(summaries, list):
        raise AgentContractError("child_summaries must be an array")
    result_ref = payload["result_ref"]
    if result_ref is not None and not isinstance(result_ref, str):
        raise AgentContractError("result_ref must be a string or null")
    return AgentFanInResult(result_id=_string(payload["result_id"], "result_id"), fan_in_id=_string(payload["fan_in_id"], "fan_in_id"), project_id=_string(payload["project_id"], "project_id"), parent_run_id=_string(payload["parent_run_id"], "parent_run_id"), status=_string(payload["status"], "status"), child_summaries=tuple(_terminal_summary_from_payload(item) for item in summaries), receipt_ref=_string(payload["receipt_ref"], "receipt_ref"), result_ref=result_ref)


def _terminal_summary_to_payload(value: AgentTerminalChildSummary) -> dict[str, object]:
    return {"child_run_id": value.child_run_id, "project_id": value.project_id, "status": value.status, "receipt_ref": value.receipt_ref, "summary_ref": value.summary_ref, "evidence_refs": list(value.evidence_refs), "usage": agent_budget_to_payload(value.usage), "error_code": value.error_code}


def _terminal_summary_from_payload(value: object) -> AgentTerminalChildSummary:
    payload = _shape(value, "terminal child summary", {"child_run_id", "project_id", "status", "receipt_ref", "summary_ref", "evidence_refs", "usage", "error_code"})
    error = payload["error_code"]
    if error is not None and not isinstance(error, str):
        raise AgentContractError("error_code must be a string or null")
    return AgentTerminalChildSummary(child_run_id=_string(payload["child_run_id"], "child_run_id"), project_id=_string(payload["project_id"], "project_id"), status=_string(payload["status"], "status"), receipt_ref=_string(payload["receipt_ref"], "receipt_ref"), summary_ref=_string(payload["summary_ref"], "summary_ref"), evidence_refs=_string_tuple(payload["evidence_refs"], "evidence_refs"), usage=agent_budget_from_payload(payload["usage"]), error_code=error)


def _shape(value: object, label: str, fields: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AgentContractError(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise AgentContractError(f"{label} keys must be strings")
    _reject_sensitive(value)
    actual = set(value)
    if actual != fields:
        unknown = sorted(actual - fields)
        missing = sorted(fields - actual)
        detail = f"unknown fields: {unknown}" if unknown else f"missing fields: {missing}"
        raise AgentContractError(f"{label} has {detail}")
    return value


_AGENT_PROFILE_LEGACY_FIELDS = {
    "schema_version", "profile_id", "revision", "display_name", "enabled", "role",
    "model_tier", "budget_limit", "capability_ids", "max_concurrent_children",
    "max_depth", "max_steps", "timeout_ms", "allow_child_spawn",
}
_AGENT_PROFILE_TIER_FIELDS = _AGENT_PROFILE_LEGACY_FIELDS | {
    "organization_role", "work_description",
}
_AGENT_PROFILE_ROUTE_FIELDS = _AGENT_PROFILE_TIER_FIELDS | {"model_route_key", "model_route_revision"}
_AGENT_PROFILE_FIELDS = _AGENT_PROFILE_ROUTE_FIELDS | {"instructions"}
_LEGACY_PROFILE_METADATA = {
    "main.orchestrator": ("主政协调", "统筹目标、优先级、进度汇聚与最终答复。"),
    "steward.scheduler": ("管家调度", "判断是否启用专家集群，并制定受管调度方案。"),
    "subagent.explorer": ("探索专家", "收集证据、定位上下文并返回可复核发现。"),
    "subagent.worker": ("执行专家", "在授权边界内完成实施任务并提交可审计结果。"),
    "subagent.reviewer": ("审阅专家", "核验方案与结果，指出风险并提供复核意见。"),
}


def _agent_profile_shape(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AgentContractError("agent profile must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise AgentContractError("agent profile keys must be strings")
    _reject_sensitive(value)
    version = value.get("schema_version")
    fields = (
        _AGENT_PROFILE_LEGACY_FIELDS if version == _LEGACY_AGENT_PROFILE_SCHEMA_VERSION
        else _AGENT_PROFILE_TIER_FIELDS if version == _TIER_ONLY_AGENT_PROFILE_SCHEMA_VERSION
        else _AGENT_PROFILE_ROUTE_FIELDS if version == _ROUTE_BOUND_AGENT_PROFILE_SCHEMA_VERSION
        else _AGENT_PROFILE_FIELDS
    )
    actual = set(value)
    if (
        version == _LEGACY_AGENT_PROFILE_SCHEMA_VERSION
        and actual == _AGENT_PROFILE_LEGACY_FIELDS | {"model_route_key", "model_route_revision"}
        and value.get("model_route_key") is None
        and value.get("model_route_revision") is None
    ):
        return {key: item for key, item in value.items() if key not in {"model_route_key", "model_route_revision"}}
    if actual != fields:
        unknown = sorted(actual - fields)
        missing = sorted(fields - actual)
        detail = f"unknown fields: {unknown}" if unknown else f"missing fields: {missing}"
        raise AgentContractError(f"agent profile has {detail}")
    if version not in {_LEGACY_AGENT_PROFILE_SCHEMA_VERSION, _TIER_ONLY_AGENT_PROFILE_SCHEMA_VERSION, _ROUTE_BOUND_AGENT_PROFILE_SCHEMA_VERSION, AGENT_PROFILE_SCHEMA_VERSION}:
        raise AgentContractError("agent profile schema version is unsupported")
    return value


def _legacy_profile_metadata(profile_id: str) -> tuple[str, str]:
    return _LEGACY_PROFILE_METADATA.get(
        profile_id,
        ("自定义子 Agent", "按已授权能力完成受管任务，并返回可审计结果。"),
    )


def _schema(payload: Mapping[str, object]) -> None:
    if payload["schema_version"] != AGENT_CONTRACT_SCHEMA_VERSION:
        raise AgentContractError("agent contract schema version is unsupported")


def _identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise AgentContractError(f"{label} is invalid")


def _profile_id(value: object) -> None:
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value):
        raise AgentContractError("agent profile identity is invalid")


def _is_subagent_profile(profile_id: str) -> bool:
    return profile_id == "steward.scheduler" or profile_id.startswith("subagent.")


def _ref(value: object, label: str) -> None:
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
        raise AgentContractError(f"{label} is invalid")


def _count(value: object, label: str, *, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise AgentContractError(f"{label} is invalid")


def _positive(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AgentContractError(f"{label} is invalid")


def _optional_identifier(value: object, label: str) -> str | None:
    if value is None:
        return None
    _identifier(value, label)
    return value


def _optional_positive(value: object, label: str) -> int | None:
    if value is None:
        return None
    _positive(value, label)
    return value


def _steps(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 64:
        raise AgentContractError(f"{label} is invalid")


def _timeout(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= _MAX_WALL_TIME_MS:
        raise AgentContractError(f"{label} is invalid")


def _display_name(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise AgentContractError("agent profile display name is invalid")


def _organization_role(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise AgentContractError("agent profile organization role is invalid")


def _work_description(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 240:
        raise AgentContractError("agent profile work description is invalid")


def _instructions(value: object) -> None:
    if (not isinstance(value, str) or len(value) > 2000
            or any(character != "\n" and unicodedata.category(character) == "Cc" for character in value)):
        raise AgentContractError("agent profile instructions are invalid")


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise AgentContractError(f"{label} must be boolean")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AgentContractError(f"{label} must be an integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AgentContractError(f"{label} must be a string")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentContractError(f"{label} must be an array of strings")
    return tuple(value)


def _capabilities(values: tuple[str, ...], label: str) -> None:
    if (
        not isinstance(values, tuple)
        or len(values) != len(set(values))
        or any(not _CAPABILITY_ID_RE.fullmatch(item) for item in values)
    ):
        raise AgentContractError(f"{label} are invalid")


def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise AgentContractError("contract field names must be strings")
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_FIELD_NAMES or normalized.endswith("_secret") or normalized.endswith("_token"):
                raise AgentContractError(f"sensitive field is not allowed: {key}")
            _reject_sensitive(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _reject_sensitive(item)
