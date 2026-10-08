"""阶段 1.5：Memory Router — 自动记忆路由器。

基于阶段 0.5 的 Memory 分层语义模型，把每个用户输入判断为 memory_event_type，
并生成 memory_delta（说明本次新增 / 更新 / 冲突 / 忽略 / 待确认了什么）。

核心原则（docs/memory_layering_model.md §5 写入顺序）：
1. 所有用户输入都先进入 L0 Source / Conversation。
2. 系统自动判断 memory_event_type，决定是否生成 L1/L2/L3/L4 候选。
3. 不再依赖用户手动选择「是否加入知识库」。
4. L1/L2/L3/L4 写入走质量门，低置信度进入 needs_review。
5. 每次写入产生 memory_delta。

本模块是纯函数式路由器，不直接写存储。写入由 OrchestrateWorkbenchAutoIntake
在合适步骤调用本路由器的结果来决定候选生成策略。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


class MemoryRouterError(ValueError):
    """Raised when Memory Router cannot classify an event safely."""


# ── memory_event_type 枚举（docs/memory_layering_model.md §5）──

MemoryEventType = Literal[
    "question",              # 用户正在提问
    "knowledge_material",    # 用户上传或粘贴的知识资料
    "task_context",          # 用户提供任务背景
    "project_update",        # 项目状态更新
    "preference_signal",     # 用户偏好信号
    "tone_style_signal",     # 用户语气、写作风格、表达偏好
    "series_signal",         # 属于某个连续系列的问题或材料
    "daily_conversation",    # 当天普通对话记录
    "external_import",       # 外部 LLM / 知识包导入
    "export_event",          # 阶段 1.6：导出或迁移事件
    "noise_or_ephemeral",    # 临时噪声，不建议长期沉淀
    "mixed",                 # 同时包含提问和知识材料
]


# ── 数据结构 ──


@dataclass(frozen=True, slots=True)
class MemoryEventIntent:
    """检测到的单一意图。"""

    intent: str  # question | knowledge | task | project | preference | tone | series | noise
    confidence: float
    evidence: str  # 关键词或规则依据，用于审计


@dataclass(frozen=True, slots=True)
class SeriesCandidate:
    """检测到的系列候选（当天对话系列 / 主题系列）。"""

    series_kind: str  # daily_conversation | topic_series
    day_bucket: str  # YYYY-MM-DD
    topic_hint: str  # 主题提示（基于关键词）
    confidence: float


@dataclass(frozen=True, slots=True)
class MemoryDeltaEntry:
    """memory_delta 中的单条变化记录。"""

    layer: str  # L0 | L1 | L2 | L3 | L4
    op: str  # new | update | conflict | ignore | pending
    target: str  # atom | scenario | persona | series_memory | project_skill | source
    reason: str
    confidence: float


@dataclass(frozen=True, slots=True)
class MemoryRouterResult:
    """Memory Router 的判断结果。

    OrchestrateWorkbenchAutoIntake 消费此结果来决定：
    - 是否生成 L1 Atom 候选
    - 是否聚合到 L2 Scenario
    - 是否更新 L4 Persona 或 L3 Series / Project Skill 候选
    - 低置信度内容进入 needs_review
    """

    memory_event_type: MemoryEventType
    detected_intents: tuple[MemoryEventIntent, ...]
    confidence: float
    privacy_level: str  # private | local_only | ephemeral
    provider_boundary: str  # local_only | external_allowed
    day_bucket: str
    series_candidates: tuple[SeriesCandidate, ...]
    memory_delta: tuple[MemoryDeltaEntry, ...]
    quality_gate: str  # auto_publish | needs_review | skip_long_term
    user_visible_summary: str


# ── 路由规则 ──

# 问题意图关键词（与 workbench_input_classifier.py 的 question 规则对齐）
_QUESTION_MARKERS = (
    "?", "？", "为什么", "怎么", "如何", "能不能", "是否", "什么",
    "哪", "吗", "怎么办", "怎样", "为何", "请问", "帮我", "能不能",
)

# 偏好信号关键词
_PREFERENCE_MARKERS = (
    "我喜欢", "我偏好", "我不喜欢", "请用", "请保持", "不要用",
    "always use", "i prefer", "格式", "风格", "语气",
)

# 语气 / 风格信号关键词
_TONE_STYLE_MARKERS = (
    "简洁", "正式", "口语", "学术", "技术", "文艺", "幽默",
    "详细", "简短", "要点", "条目", "段落", "markdown",
)

# 任务上下文关键词
_TASK_MARKERS = (
    "下一步", "推进", "任务", "计划", "实现", "修复", "完成",
    "todo", "deadline", "截止", "待办", "进度", "里程碑",
)

# 项目更新关键词
_PROJECT_MARKERS = (
    "项目", "阶段", "目标", "里程碑", "发布", "上线", "重构",
    "迁移", "验收", "回归",
)

# 噪声 / 临时关键词（过短或测试性输入）
_NOISE_MARKERS = (
    "test", "测试", "123", "asdf", "aaa", "bbb", "hello world",
    "ping", "hi", "你好", "嗯", "ok",
)

# 系列信号关键词（与系列主题相关）
_SERIES_MARKERS = (
    "系列", "继续", "上次", "之前", "接着", "另外",
    "今天", "这周", "最近", "前面提到",
)


def _day_bucket(now: str | None = None) -> str:
    """返回 YYYY-MM-DD 格式的 day_bucket。"""
    if now:
        # 已有 ISO 时间戳，提取日期部分
        return now[:10]
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _contains_any(text: str, markers: Sequence[str]) -> bool:
    return any(marker in text for marker in markers)


def _count_matches(text: str, markers: Sequence[str]) -> int:
    return sum(1 for marker in markers if marker in text)


def _is_noise(text: str) -> bool:
    """判断是否为临时噪声：过短、无上下文、测试性输入。"""
    stripped = text.strip()
    if len(stripped) < 4:
        return True
    if _contains_any(stripped.lower(), _NOISE_MARKERS) and len(stripped) < 20:
        return True
    return False


def _detect_intents(text: str) -> tuple[MemoryEventIntent, ...]:
    """从文本中检测所有意图，按置信度排序。"""
    intents: list[MemoryEventIntent] = []

    if _contains_any(text, _QUESTION_MARKERS):
        intents.append(MemoryEventIntent(
            intent="question",
            confidence=0.92,
            evidence="包含问句标记词",
        ))

    # 知识材料：长文本（>200字）或包含结构化标记
    if len(text) > 200 or any(m in text for m in ("```", "##", "定义", "概念", "原理", "方法")):
        intents.append(MemoryEventIntent(
            intent="knowledge",
            confidence=0.85,
            evidence="长文本或包含知识结构标记",
        ))

    if _contains_any(text, _PREFERENCE_MARKERS):
        intents.append(MemoryEventIntent(
            intent="preference",
            confidence=0.88,
            evidence="包含偏好表达词",
        ))

    if _contains_any(text, _TONE_STYLE_MARKERS):
        intents.append(MemoryEventIntent(
            intent="tone",
            confidence=0.82,
            evidence="包含语气/风格词",
        ))

    if _contains_any(text, _TASK_MARKERS):
        intents.append(MemoryEventIntent(
            intent="task",
            confidence=0.84,
            evidence="包含任务上下文词",
        ))

    if _contains_any(text, _PROJECT_MARKERS):
        intents.append(MemoryEventIntent(
            intent="project",
            confidence=0.80,
            evidence="包含项目更新词",
        ))

    if _contains_any(text, _SERIES_MARKERS):
        intents.append(MemoryEventIntent(
            intent="series",
            confidence=0.78,
            evidence="包含系列连续性词",
        ))

    if not intents:
        # 无明确意图，归为日常对话
        intents.append(MemoryEventIntent(
            intent="daily",
            confidence=0.60,
            evidence="未匹配任何明确意图标记",
        ))

    # 按置信度降序排序
    intents.sort(key=lambda x: x.confidence, reverse=True)
    return tuple(intents)


def _detect_series_candidates(
    text: str,
    day_bucket: str,
    detected_intents: tuple[MemoryEventIntent, ...],
) -> tuple[SeriesCandidate, ...]:
    """检测系列候选：当天对话系列 + 主题系列。"""
    candidates: list[SeriesCandidate] = []

    # 当天对话系列：每个输入都隐含属于当天对话
    candidates.append(SeriesCandidate(
        series_kind="daily_conversation",
        day_bucket=day_bucket,
        topic_hint="当天对话",
        confidence=0.70,
    ))

    # 主题系列：如果检测到系列信号或任务信号
    intent_kinds = {i.intent for i in detected_intents}
    if "series" in intent_kinds or "task" in intent_kinds or "project" in intent_kinds:
        topic_hint = "项目推进系列"
        if "task" in intent_kinds:
            topic_hint = "任务推进系列"
        elif "project" in intent_kinds:
            topic_hint = "项目更新系列"
        candidates.append(SeriesCandidate(
            series_kind="topic_series",
            day_bucket=day_bucket,
            topic_hint=topic_hint,
            confidence=0.72,
        ))

    return tuple(candidates)


def _build_memory_delta(
    memory_event_type: MemoryEventType,
    detected_intents: tuple[MemoryEventIntent, ...],
    confidence: float,
) -> tuple[MemoryDeltaEntry, ...]:
    """根据 memory_event_type 和意图构建 memory_delta。"""
    delta: list[MemoryDeltaEntry] = []

    # L0 Source 总是新增
    delta.append(MemoryDeltaEntry(
        layer="L0",
        op="new",
        target="source",
        reason="用户输入自动捕获为 L0 Memory Event",
        confidence=1.0,
    ))

    # L1 Atom 候选：知识材料或包含明确事实的提问
    intent_kinds = {i.intent for i in detected_intents}
    if memory_event_type == "knowledge_material" or "knowledge" in intent_kinds:
        delta.append(MemoryDeltaEntry(
            layer="L1",
            op="pending",
            target="atom",
            reason="从知识资料中抽取原子事实候选",
            confidence=confidence,
        ))
    elif memory_event_type == "question":
        # 问题中的明确背景也可以抽取 L1 Atom
        delta.append(MemoryDeltaEntry(
            layer="L1",
            op="pending",
            target="atom",
            reason="从提问背景中抽取原子事实候选",
            confidence=confidence * 0.7,  # 提问抽取置信度降低
        ))

    # L2 Scenario 候选：任务上下文、项目更新、系列信号
    if memory_event_type in ("task_context", "project_update", "series_signal") or \
       ("task" in intent_kinds or "project" in intent_kinds or "series" in intent_kinds):
        delta.append(MemoryDeltaEntry(
            layer="L2",
            op="pending",
            target="scenario",
            reason="聚合为场景/任务经验候选",
            confidence=confidence,
        ))

    # L4 Persona / L3 Output Pattern 候选：偏好信号、语气信号
    if memory_event_type in ("preference_signal", "tone_style_signal") or \
       ("preference" in intent_kinds or "tone" in intent_kinds):
        target = "persona" if "preference" in intent_kinds else "project_skill"
        delta.append(MemoryDeltaEntry(
            layer="L4" if target == "persona" else "L3",
            op="pending",
            target=target,
            reason=(
                "偏好信号进入 L4 Persona 候选，需多次一致性证据和用户确认"
                if target == "persona"
                else "语气信号进入 L3 Project Skill 候选，需多次一致性证据"
            ),
            confidence=confidence * 0.6,
        ))

    # 噪声：只保留 L0，不写入上层
    if memory_event_type == "noise_or_ephemeral":
        delta.append(MemoryDeltaEntry(
            layer="L1",
            op="ignore",
            target="atom",
            reason="临时噪声，不建议长期沉淀",
            confidence=0.3,
        ))

    # 阶段 1.6：导出事件只更新 L0 导出批次记录，不写入 L1/L2/L3
    if memory_event_type == "export_event":
        delta.append(MemoryDeltaEntry(
            layer="L0",
            op="new",
            target="source",
            reason="记录导出批次作为 L0 审计事件",
            confidence=1.0,
        ))

    return tuple(delta)


def _determine_quality_gate(
    memory_event_type: MemoryEventType,
    confidence: float,
) -> str:
    """决定质量门：auto_publish / needs_review / skip_long_term。"""
    if memory_event_type == "noise_or_ephemeral":
        return "skip_long_term"
    # 阶段 1.6：导出事件本身不写入长期记忆，只作为 L0 审计
    if memory_event_type == "export_event":
        return "skip_long_term"
    if confidence < 0.75:
        return "needs_review"
    # 即使高置信度，L3/L4 候选也需要 review
    if memory_event_type in ("preference_signal", "tone_style_signal"):
        return "needs_review"
    return "auto_publish"


def _user_visible_summary(memory_event_type: MemoryEventType, detected_intents: tuple[MemoryEventIntent, ...]) -> str:
    """生成用户可读的摘要文案。"""
    summaries = {
        "question": "已捕获为提问，AI 会回答并记录到当天对话。",
        "knowledge_material": "已捕获为知识资料，AI 会抽取事实并整理为记忆。",
        "task_context": "已捕获为任务上下文，AI 会归入相关场景。",
        "project_update": "已捕获为项目更新，AI 会更新项目大脑。",
        "preference_signal": "已捕获偏好信号，需多次一致后写入长期画像。",
        "tone_style_signal": "已捕获语气信号，需多次一致后写入输出范式。",
        "series_signal": "已捕获为系列信号，AI 会归入相关系列。",
        "daily_conversation": "已捕获为当天对话记录。",
        "external_import": "已捕获为外部导入，AI 会整理并生成候选。",
        "export_event": "已记录导出事件，AI 会更新导出批次与审计记录。",
        "noise_or_ephemeral": "已捕获为临时记录，不写入长期记忆。",
        "mixed": "已捕获，AI 会同时回答问题并整理资料。",
    }
    return summaries.get(memory_event_type, "已捕获为记忆事件。")


# ── 主路由器 ──


@dataclass(frozen=True, slots=True)
class MemoryRouter:
    """自动记忆路由器：判断 memory_event_type 并生成 memory_delta。

    纯函数式，不依赖存储。调用方（OrchestrateWorkbenchAutoIntake）传入
    分类结果和原始内容，路由器返回 MemoryRouterResult。
    """

    def execute(
        self,
        *,
        content: str = "",
        media_type: str = "",
        file_name: str = "",
        urls: Sequence[str] | None = None,
        input_type: str = "",  # 来自 ClassifyWorkbenchInput 的 input_type
        intent: str = "",  # 来自 ClassifyWorkbenchInput 的 intent
        now: str | None = None,
        trigger_event: str = "",  # 阶段 1.6：显式触发事件（export | external_import）
    ) -> MemoryRouterResult:
        """判断 memory_event_type 并生成路由结果。"""
        clean_content = (content or "").strip()
        clean_media_type = (media_type or "").strip().lower()
        clean_urls = tuple(url.strip() for url in urls or () if isinstance(url, str) and url.strip())
        day_bucket = _day_bucket(now)

        # 阶段 1.6：显式触发事件优先级最高
        if trigger_event == "export":
            return self._build_result(
                memory_event_type="export_event",
                detected_intents=(MemoryEventIntent("export", 0.95, "用户主动导出记忆资产包"),),
                confidence=0.95,
                day_bucket=day_bucket,
                content=clean_content or "memory_asset_package_export",
                privacy_level="private",
                provider_boundary="local_only",
            )

        # 外部导入：显式 trigger 或 file_name 含 import 标记
        if trigger_event == "external_import" or "import" in clean_media_type or "import" in (file_name or "").lower():
            return self._build_result(
                memory_event_type="external_import",
                detected_intents=(MemoryEventIntent("knowledge", 0.90, "外部导入包"),),
                confidence=0.90,
                day_bucket=day_bucket,
                content=clean_content,
                privacy_level="private",
                provider_boundary="local_only",
            )

        # 有附件（media_type 非空）→ knowledge_material
        if clean_media_type or (file_name and file_name.strip()):
            return self._build_result(
                memory_event_type="knowledge_material",
                detected_intents=(MemoryEventIntent("knowledge", 0.92, "用户上传附件"),),
                confidence=0.92,
                day_bucket=day_bucket,
                content=clean_content,
                privacy_level="private",
                provider_boundary="external_allowed",
            )

        # 有 URL → knowledge_material（链接抓取）
        if clean_urls:
            return self._build_result(
                memory_event_type="knowledge_material",
                detected_intents=(MemoryEventIntent("knowledge", 0.90, "用户提交链接"),),
                confidence=0.90,
                day_bucket=day_bucket,
                content=clean_content,
                privacy_level="private",
                provider_boundary="external_allowed",
            )

        # 纯文本输入：检测意图
        if not clean_content:
            raise MemoryRouterError("Memory Router 需要至少一项输入（content / media_type / urls）")

        # 噪声检测
        if _is_noise(clean_content):
            return self._build_result(
                memory_event_type="noise_or_ephemeral",
                detected_intents=(MemoryEventIntent("noise", 0.40, "过短或测试性输入"),),
                confidence=0.40,
                day_bucket=day_bucket,
                content=clean_content,
                privacy_level="ephemeral",
                provider_boundary="local_only",
            )

        detected_intents = _detect_intents(clean_content)
        intent_kinds = {i.intent for i in detected_intents}
        top_confidence = detected_intents[0].confidence if detected_intents else 0.5

        # mixed：同时包含提问和知识
        if "question" in intent_kinds and ("knowledge" in intent_kinds or len(clean_content) > 200):
            return self._build_result(
                memory_event_type="mixed",
                detected_intents=detected_intents,
                confidence=top_confidence,
                day_bucket=day_bucket,
                content=clean_content,
                privacy_level="private",
                provider_boundary="external_allowed",
            )

        # 单一意图路由
        if "question" in intent_kinds:
            event_type: MemoryEventType = "question"
        elif "preference" in intent_kinds:
            event_type = "preference_signal"
        elif "tone" in intent_kinds:
            event_type = "tone_style_signal"
        elif "task" in intent_kinds:
            event_type = "task_context"
        elif "project" in intent_kinds:
            event_type = "project_update"
        elif "series" in intent_kinds:
            event_type = "series_signal"
        elif "knowledge" in intent_kinds:
            event_type = "knowledge_material"
        else:
            event_type = "daily_conversation"

        return self._build_result(
            memory_event_type=event_type,
            detected_intents=detected_intents,
            confidence=top_confidence,
            day_bucket=day_bucket,
            content=clean_content,
            privacy_level="private",
            provider_boundary="external_allowed" if event_type not in ("daily_conversation",) else "local_only",
        )

    def _build_result(
        self,
        *,
        memory_event_type: MemoryEventType,
        detected_intents: tuple[MemoryEventIntent, ...],
        confidence: float,
        day_bucket: str,
        content: str,
        privacy_level: str,
        provider_boundary: str,
    ) -> MemoryRouterResult:
        series_candidates = _detect_series_candidates(content, day_bucket, detected_intents)
        memory_delta = _build_memory_delta(memory_event_type, detected_intents, confidence)
        quality_gate = _determine_quality_gate(memory_event_type, confidence)

        return MemoryRouterResult(
            memory_event_type=memory_event_type,
            detected_intents=detected_intents,
            confidence=confidence,
            privacy_level=privacy_level,
            provider_boundary=provider_boundary,
            day_bucket=day_bucket,
            series_candidates=series_candidates,
            memory_delta=memory_delta,
            quality_gate=quality_gate,
            user_visible_summary=_user_visible_summary(memory_event_type, detected_intents),
        )


# ── 序列化 ──


def serialize_memory_router_result(result: MemoryRouterResult) -> dict[str, object]:
    """把 MemoryRouterResult 序列化为 JSON 友好的 dict。"""
    return {
        "memory_event_type": result.memory_event_type,
        "detected_intents": [
            {
                "intent": i.intent,
                "confidence": i.confidence,
                "evidence": i.evidence,
            }
            for i in result.detected_intents
        ],
        "confidence": result.confidence,
        "privacy_level": result.privacy_level,
        "provider_boundary": result.provider_boundary,
        "day_bucket": result.day_bucket,
        "series_candidates": [
            {
                "series_kind": c.series_kind,
                "day_bucket": c.day_bucket,
                "topic_hint": c.topic_hint,
                "confidence": c.confidence,
            }
            for c in result.series_candidates
        ],
        "memory_delta": [
            {
                "layer": d.layer,
                "op": d.op,
                "target": d.target,
                "reason": d.reason,
                "confidence": d.confidence,
            }
            for d in result.memory_delta
        ],
        "quality_gate": result.quality_gate,
        "user_visible_summary": result.user_visible_summary,
    }
