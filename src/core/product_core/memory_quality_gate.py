"""阶段 1.6：Memory Quality Gate — 记忆候选质量门。

对所有从上传、提问、外部导入中产生的 L1/L2/L3 候选做统一质量检查：

检查项：
- 是否有证据（evidence_refs 非空）
- 来源角色（user / assistant / system / tool / attachment / unknown）
- 是否重复
- 是否和已有记忆冲突
- 是否包含敏感信息
- 是否适合长期保存
- 是否只是临时上下文
- 是否需要用户确认
- 是否可以提升到 L3

候选分组：
- needs_review         建议确认
- needs_human_judgment 需要人工判断
- conflict             冲突项
- low_trust            低可信候选（如 assistant 回复）
- ignored              已忽略
- l0_only              仅保存为 L0
- l2_daily_only        仅归入 L2 日常对话系列
- l3_promotion         可提升为 L3 的稳定模式

信任等级规则（docs/memory_layering_model.md §6）：
- 用户明确写下的自我描述、偏好、项目事实：high
- 用户上传的原始文件：high
- 用户确认过的 memory / custom instructions：high
- 用户问题中的上下文：medium
- assistant 回复：low ~ medium，只能作为候选或参考
- assistant 推测、建议、代码解释：默认不能直接进入长期事实
- tool 结果：可作为证据，但需要保留原始引用
- 冲突内容：必须进入待确认
- 没有证据来源的内容：不能自动发布为长期记忆
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal


class MemoryQualityGateError(ValueError):
    """Raised when quality gate cannot evaluate a candidate safely."""


# ── 信任等级 ──

TrustLevel = Literal["high", "medium", "low", "unverified"]


# ── 敏感信息检测正则 ──

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(
        r"(?:\bapi[_-]?key\b\s*[:=]\s*[\"']?[^\s\"';,]{8,}[\"']?|sk-[A-Za-z0-9]{20,})",
        re.IGNORECASE,
    )),
    ("secret_assignment", re.compile(
        r"\b(?:access[_-]?token|refresh[_-]?token|token|secret|password|passwd)\b"
        r"\s*[:=]\s*[\"']?[^\s\"';,]{8,}[\"']?",
        re.IGNORECASE,
    )),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("cookie", re.compile(r"\b(?:cookie|set-cookie)\s*:\s*[^\r\n]{1,4096}", re.IGNORECASE)),
    ("aws_key", re.compile(r"(?i)AKIA[0-9A-Z]{16}")),
    ("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        r".*?(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)",
        re.IGNORECASE | re.DOTALL,
    )),
    ("connection_string", re.compile(r"(?i)(mongodb|postgres|redis|amqp)://[^\s]{20,}")),
    ("long_path", re.compile(r"[A-Za-z]:\\Users\\[^\s\\]{2,}\\[^\s\\]{2,}")),  # 完整 Windows 用户路径
    ("unix_home_path", re.compile(r"/home/[^\s/]{2,}/[^\s/]{2,}")),
    ("phone_cn", re.compile(r"1[3-9]\d{9}")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)


# ── 数据结构 ──


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """质量门评估的输入：一条 memory 候选。"""

    memory_id: str
    layer: str  # L0 | L1 | L2 | L3
    type: str  # preference / fact / rule / person / project / decision / workflow / persona / capability / tag / other
    content: str
    summary: str = ""
    confidence: float = 0.5
    trust_level: TrustLevel = "unverified"
    source_platform: str = ""
    source_type: str = ""  # conversation | workspace | custom_instruction | document | tool_result | file
    source_role: str = "unknown"  # user | assistant | system | tool | attachment | unknown
    source_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    created_at: str = ""
    observed_at: str = ""
    conflict_refs: tuple[str, ...] = ()
    status: str = "candidate"  # candidate | confirmed | rejected | needs_review | archived
    import_batch_id: str = ""
    privacy_level: str = "private"  # private | local_only | ephemeral
    provider_boundary: str = "local_only"


@dataclass(frozen=True, slots=True)
class QualityGateDecision:
    """质量门对单条候选的决策。"""

    memory_id: str
    group: str  # needs_review | needs_human_judgment | conflict | low_trust | ignored | l0_only | l2_daily_only | l3_promotion | auto_publish
    new_status: str  # candidate | confirmed | rejected | needs_review | archived
    reasons: tuple[str, ...]
    redacted_content: str  # 脱敏后的内容（用于展示或导出）
    detected_secrets: tuple[str, ...]  # 检测到的敏感字段类型
    can_promote_to_l3: bool
    suggested_layer: str  # 建议落入的层级


@dataclass(frozen=True, slots=True)
class QualityGateReport:
    """质量门对一批候选的汇总报告。"""

    decisions: tuple[QualityGateDecision, ...]
    group_counts: Mapping[str, int]
    total_candidates: int
    needs_user_action: int
    redacted_count: int
    conflict_count: int
    summary: str


# ── 敏感信息检测 ──


def detect_secrets(content: str) -> tuple[str, ...]:
    """检测内容中是否包含 secret / cookie / token / 完整路径等敏感字段。

    返回检测到的敏感字段类型列表（去重）。
    """
    if not content:
        return ()
    detected: list[str] = []
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            detected.append(kind)
    return tuple(dict.fromkeys(detected))  # 去重保序


def redact_content(content: str, *, replace_with: str = "[REDACTED]") -> str:
    """把内容中的敏感字段替换为占位符。"""
    if not content:
        return ""
    redacted = content
    for _kind, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(replace_with, redacted)
    return redacted


# ── 信任等级评估 ──


def compute_trust_level(
    source_role: str,
    source_type: str,
    confidence: float,
    *,
    user_confirmed: bool = False,
) -> TrustLevel:
    """根据来源角色与类型评估信任等级。

    核心规则：assistant 回复默认不能直接进入长期事实。
    """
    if user_confirmed:
        return "high"
    if source_role == "user" and source_type in ("file", "document", "workspace"):
        return "high"
    if source_role == "user" and source_type == "custom_instruction":
        return "high"
    if source_role == "user" and source_type == "conversation":
        return "medium"
    if source_role == "system":
        return "medium"
    if source_role == "tool":
        return "medium"
    if source_role == "attachment":
        return "medium"
    if source_role == "assistant":
        # assistant 推测、建议、代码解释默认低可信
        return "low"
    return "unverified"


# ── 质量门评估 ──


def evaluate_candidate(
    candidate: MemoryCandidate,
    *,
    existing_memory_signatures: Sequence[str] = (),
    enable_l3_promotion: bool = True,
) -> QualityGateDecision:
    """对单条候选做质量门评估。

    existing_memory_signatures: 已有记忆的内容签名（用于重复与冲突检测）
    enable_l3_promotion: 是否允许提升到 L3

    若候选的 trust_level 为 "unverified" 且 source_role 已知，
    会自动调用 compute_trust_level 推导信任等级（避免上游遗漏）。
    """
    # 自动推导 trust_level（若上游未设置）
    if candidate.trust_level == "unverified" and candidate.source_role != "unknown":
        trust_level = compute_trust_level(
            candidate.source_role,
            candidate.source_type,
            candidate.confidence,
        )
        # 用 dataclasses.replace 重建候选，保留所有字段
        import dataclasses
        candidate = dataclasses.replace(candidate, trust_level=trust_level)

    reasons: list[str] = []
    detected_secrets = detect_secrets(candidate.content)
    redacted = redact_content(candidate.content)
    can_promote_l3 = False
    suggested_layer = candidate.layer
    new_status = candidate.status
    group = "needs_review"

    # 1. 敏感信息检测：含 secret 必须人工判断
    if detected_secrets:
        reasons.append(f"检测到敏感字段：{', '.join(detected_secrets)}")
        group = "needs_human_judgment"
        new_status = "needs_review"
        suggested_layer = "L0"  # 不写入上层，仅保留 L0

    # 2. 证据检查：无证据不能自动发布
    elif not candidate.evidence_refs:
        reasons.append("无证据来源，不能自动发布为长期记忆")
        group = "needs_review"
        new_status = "needs_review"
        suggested_layer = "L0" if candidate.layer in ("L1", "L2", "L3") else candidate.layer

    # 3. assistant 回复：默认低可信
    elif candidate.source_role == "assistant" and candidate.trust_level == "low":
        reasons.append("assistant 回复默认低可信，仅作为候选或参考")
        group = "low_trust"
        new_status = "needs_review"
        suggested_layer = "L0" if candidate.layer in ("L2", "L3") else candidate.layer

    # 4. 冲突检测
    elif candidate.conflict_refs:
        reasons.append(f"与已有记忆冲突：{len(candidate.conflict_refs)} 条")
        group = "conflict"
        new_status = "needs_review"
        suggested_layer = candidate.layer

    # 5. 重复检测
    elif candidate.content in existing_memory_signatures:
        reasons.append("与已有记忆内容完全重复")
        group = "ignored"
        new_status = "archived"
        suggested_layer = "L0"

    # 6. 噪声 / 过短
    elif len(candidate.content.strip()) < 8:
        reasons.append("内容过短，缺乏上下文")
        group = "l0_only"
        new_status = "archived"
        suggested_layer = "L0"

    # 7. 临时上下文（噪声标记）
    elif candidate.type == "other" and candidate.confidence < 0.5:
        reasons.append("临时上下文，不建议长期沉淀")
        group = "l2_daily_only"
        new_status = "archived"
        suggested_layer = "L2"

    # 8. 高可信 + 有证据：可发布
    elif candidate.trust_level == "high" and candidate.confidence >= 0.75:
        # L3 提升需要更严格：必须高可信 + 多次证据
        if candidate.layer == "L3" and enable_l3_promotion:
            if len(candidate.evidence_refs) >= 2:
                reasons.append("高可信 + 多次证据，可提升为 L3 稳定模式")
                group = "l3_promotion"
                new_status = "confirmed"
                can_promote_l3 = True
                suggested_layer = "L3"
            else:
                reasons.append("L3 候选需要至少 2 条证据，进入待确认")
                group = "needs_review"
                new_status = "needs_review"
                suggested_layer = "L2"
        else:
            reasons.append("高可信 + 有证据，可自动发布")
            group = "auto_publish"
            new_status = "confirmed"
            suggested_layer = candidate.layer

    # 9. 中等可信：待确认
    elif candidate.trust_level == "medium":
        reasons.append("中等可信，需要用户确认后发布")
        group = "needs_review"
        new_status = "needs_review"
        suggested_layer = candidate.layer

    # 10. 未验证：必须人工判断
    else:
        reasons.append("未验证来源，需要人工判断")
        group = "needs_human_judgment"
        new_status = "needs_review"
        suggested_layer = "L0"

    return QualityGateDecision(
        memory_id=candidate.memory_id,
        group=group,
        new_status=new_status,
        reasons=tuple(reasons),
        redacted_content=redacted,
        detected_secrets=detected_secrets,
        can_promote_to_l3=can_promote_l3,
        suggested_layer=suggested_layer,
    )


def evaluate_candidates(
    candidates: Sequence[MemoryCandidate],
    *,
    existing_memory_signatures: Sequence[str] = (),
    enable_l3_promotion: bool = True,
) -> QualityGateReport:
    """对一批候选做质量门评估，返回汇总报告。"""
    decisions: list[QualityGateDecision] = []
    group_counts: dict[str, int] = {}

    for candidate in candidates:
        decision = evaluate_candidate(
            candidate,
            existing_memory_signatures=existing_memory_signatures,
            enable_l3_promotion=enable_l3_promotion,
        )
        decisions.append(decision)
        group_counts[decision.group] = group_counts.get(decision.group, 0) + 1

    needs_user_action = sum(
        1 for d in decisions
        if d.new_status == "needs_review"
    )
    redacted_count = sum(1 for d in decisions if d.detected_secrets)
    conflict_count = group_counts.get("conflict", 0)

    summary = (
        f"共评估 {len(candidates)} 条候选："
        f"待确认 {needs_user_action}，"
        f"冲突 {conflict_count}，"
        f"含敏感 {redacted_count}，"
        f"可发布 {group_counts.get('auto_publish', 0)}，"
        f"可提升 L3 {group_counts.get('l3_promotion', 0)}"
    )

    return QualityGateReport(
        decisions=tuple(decisions),
        group_counts=dict(group_counts),
        total_candidates=len(candidates),
        needs_user_action=needs_user_action,
        redacted_count=redacted_count,
        conflict_count=conflict_count,
        summary=summary,
    )


# ── 序列化 ──


def serialize_quality_gate_decision(decision: QualityGateDecision) -> dict[str, object]:
    return {
        "memory_id": decision.memory_id,
        "group": decision.group,
        "new_status": decision.new_status,
        "reasons": list(decision.reasons),
        "redacted_content": decision.redacted_content,
        "detected_secrets": list(decision.detected_secrets),
        "can_promote_to_l3": decision.can_promote_to_l3,
        "suggested_layer": decision.suggested_layer,
    }


def serialize_quality_gate_report(report: QualityGateReport) -> dict[str, object]:
    return {
        "decisions": [serialize_quality_gate_decision(d) for d in report.decisions],
        "group_counts": dict(report.group_counts),
        "total_candidates": report.total_candidates,
        "needs_user_action": report.needs_user_action,
        "redacted_count": report.redacted_count,
        "conflict_count": report.conflict_count,
        "summary": report.summary,
    }


def serialize_memory_candidate(candidate: MemoryCandidate) -> dict[str, object]:
    return {
        "memory_id": candidate.memory_id,
        "layer": candidate.layer,
        "type": candidate.type,
        "content": candidate.content,
        "summary": candidate.summary,
        "confidence": candidate.confidence,
        "trust_level": candidate.trust_level,
        "source_platform": candidate.source_platform,
        "source_type": candidate.source_type,
        "source_role": candidate.source_role,
        "source_ref": candidate.source_ref,
        "evidence_refs": list(candidate.evidence_refs),
        "created_at": candidate.created_at,
        "observed_at": candidate.observed_at,
        "conflict_refs": list(candidate.conflict_refs),
        "status": candidate.status,
        "import_batch_id": candidate.import_batch_id,
        "privacy_level": candidate.privacy_level,
        "provider_boundary": candidate.provider_boundary,
    }
