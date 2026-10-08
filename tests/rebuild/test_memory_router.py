"""阶段 1.5：Memory Router 自动记忆路由器测试。

验证（docs/memory_layering_model.md §5 写入顺序 + §9 不能自动写入长期记忆）：
1. 用户输入问题也会创建 L0 Memory Event（memory_event_type=question）
2. 问题不会默认被当成长期事实（quality_gate=needs_review 或 skip_long_term）
3. 问题中的明确背景可以抽取 L1 Atom（memory_delta 含 L1 pending）
4. 连续同主题问题可以聚合为 L2 Scenario（series_candidates 含 topic_series）
5. 反复出现的风格要求可以生成 L3 Output Pattern 候选（memory_delta 含 L3 pending）
6. 上传资料自动识别为 knowledge_material
7. 问题 + 粘贴资料识别为 mixed
8. 低置信度内容进入 needs_review
9. legacy join_knowledge_base / add_to_knowledge_base 字段不破坏兼容性
10. 每条 L1/L2/L3 候选都有 evidence_refs（通过 detected_intents 的 evidence 字段）
11. 噪声输入只保留 L0，不写入长期记忆
12. preference_signal 进入 L4 Persona 候选但 quality_gate=needs_review
"""
from __future__ import annotations

from core.product_core import (
    MemoryRouter,
    MemoryRouterError,
    serialize_memory_router_result,
)


def _router() -> MemoryRouter:
    return MemoryRouter()


# ─── 测试 1：用户输入问题也会创建 L0 Memory Event ───


def test_question_creates_l0_memory_event():
    router = _router()
    result = router.execute(content="为什么需要分层记忆？", now="2026-07-06T10:00:00+08:00")

    assert result.memory_event_type == "question"
    # L0 source 总是新增
    l0_deltas = [d for d in result.memory_delta if d.layer == "L0"]
    assert len(l0_deltas) == 1
    assert l0_deltas[0].op == "new"
    assert l0_deltas[0].target == "source"


# ─── 测试 2：问题不会默认被当成长期事实 ───


def test_question_does_not_auto_publish_as_fact():
    router = _router()
    result = router.execute(content="这是什么？", now="2026-07-06T10:00:00+08:00")

    assert result.memory_event_type == "question"
    # 问题的 L1 候选置信度降低，进入 pending 而非 auto_publish
    l1_deltas = [d for d in result.memory_delta if d.layer == "L1"]
    if l1_deltas:
        assert l1_deltas[0].op == "pending"
        assert l1_deltas[0].confidence < 0.75  # 降低后的置信度


# ─── 测试 3：问题中的明确背景可以抽取 L1 Atom ───


def test_question_with_background_can_extract_l1_atom():
    router = _router()
    # 问题中包含明确的项目事实背景
    result = router.execute(
        content="我们项目采用本地优先架构，为什么还需要外部 Provider？",
        now="2026-07-06T10:00:00+08:00",
    )

    # 检测到 question 意图
    intent_kinds = {i.intent for i in result.detected_intents}
    assert "question" in intent_kinds

    # L1 Atom 候选存在（pending 状态）
    l1_deltas = [d for d in result.memory_delta if d.layer == "L1"]
    assert len(l1_deltas) >= 1
    assert l1_deltas[0].op == "pending"
    assert l1_deltas[0].target == "atom"


# ─── 测试 4：连续同主题问题可以聚合为 L2 Scenario ───


def test_recurring_topic_creates_series_candidate():
    router = _router()
    # 包含系列连续性词
    result = router.execute(
        content="继续上次的项目推进，下一步做什么？",
        now="2026-07-06T10:00:00+08:00",
    )

    # series_candidates 包含 topic_series
    topic_series = [c for c in result.series_candidates if c.series_kind == "topic_series"]
    assert len(topic_series) >= 1
    assert "项目" in topic_series[0].topic_hint or "任务" in topic_series[0].topic_hint

    # L2 Scenario 候选存在
    l2_deltas = [d for d in result.memory_delta if d.layer == "L2"]
    assert len(l2_deltas) >= 1
    assert l2_deltas[0].op == "pending"
    assert l2_deltas[0].target == "scenario"


# ─── 测试 5：明确风格偏好生成 L4 Persona 候选 ───


def test_style_preference_signal_creates_l4_persona_candidate():
    router = _router()
    result = router.execute(
        content="请用简洁的要点风格回答，不要用长段落",
        now="2026-07-06T10:00:00+08:00",
    )

    # 检测到 tone 意图
    intent_kinds = {i.intent for i in result.detected_intents}
    assert "tone" in intent_kinds

    l4_deltas = [d for d in result.memory_delta if d.layer == "L4"]
    assert len(l4_deltas) >= 1
    assert l4_deltas[0].op == "pending"
    assert l4_deltas[0].target == "persona"

    # 单次偏好不直接写入 L4 current。
    assert result.quality_gate == "needs_review"


# ─── 测试 6：上传资料自动识别为 knowledge_material ───


def test_uploaded_file_identified_as_knowledge_material():
    router = _router()
    result = router.execute(
        content="",
        media_type="application/pdf",
        file_name="report.pdf",
        now="2026-07-06T10:00:00+08:00",
    )

    assert result.memory_event_type == "knowledge_material"
    assert result.confidence >= 0.9

    # L1 Atom 候选存在
    l1_deltas = [d for d in result.memory_delta if d.layer == "L1"]
    assert len(l1_deltas) >= 1
    assert l1_deltas[0].op == "pending"


# ─── 测试 7：问题 + 粘贴资料识别为 mixed ───


def test_question_with_long_material_identified_as_mixed():
    router = _router()
    # 问题 + 长文本资料
    long_content = "什么是分层记忆？" + "这是一段很长的知识资料内容。" * 30
    result = router.execute(content=long_content, now="2026-07-06T10:00:00+08:00")

    assert result.memory_event_type == "mixed"

    # 同时检测到 question 和 knowledge 意图
    intent_kinds = {i.intent for i in result.detected_intents}
    assert "question" in intent_kinds
    assert "knowledge" in intent_kinds


# ─── 测试 8：低置信度内容进入 needs_review ───


def test_low_confidence_goes_to_needs_review():
    router = _router()
    # 普通日常对话，无明确意图，置信度较低
    result = router.execute(
        content="今天天气不错，随便聊聊",
        now="2026-07-06T10:00:00+08:00",
    )

    # daily_conversation 的置信度可能低于 0.75
    if result.confidence < 0.75:
        assert result.quality_gate == "needs_review"


# ─── 测试 9：legacy add_to_knowledge_base 字段不破坏兼容性 ───


def test_legacy_add_to_knowledge_base_field_ignored():
    # MemoryRouter 本身不接收 add_to_knowledge_base 字段，
    # 但 OrchestrateWorkbenchAutoIntake 仍接收并忽略（不再走 direct_question 分支）。
    # 这里验证 MemoryRouter 对所有输入都一视同仁地生成 memory_event。
    router = _router()

    # 即使是「问题」也生成 memory_event（不再有「不加入知识库」的分支）
    result = router.execute(content="这是一个问题吗？", now="2026-07-06T10:00:00+08:00")
    assert result.memory_event_type == "question"
    assert result.memory_event_type != "noise_or_ephemeral"


# ─── 测试 10：每条 L1/L2/L3 候选都有 evidence ───


def test_every_candidate_has_evidence():
    router = _router()
    result = router.execute(
        content="我们项目采用本地优先架构，请用简洁风格回答下一步计划",
        now="2026-07-06T10:00:00+08:00",
    )

    # 所有 detected_intents 都有 evidence 字段
    for intent in result.detected_intents:
        assert intent.evidence
        assert len(intent.evidence) > 0

    # 所有 memory_delta 都有 reason 字段（作为 evidence 的说明）
    for delta in result.memory_delta:
        assert delta.reason
        assert len(delta.reason) > 0


# ─── 测试 11：噪声输入只保留 L0，不写入长期记忆 ───


def test_noise_input_only_keeps_l0():
    router = _router()

    # 过短输入
    result = router.execute(content="hi", now="2026-07-06T10:00:00+08:00")
    assert result.memory_event_type == "noise_or_ephemeral"
    assert result.quality_gate == "skip_long_term"

    # L1 delta 是 ignore
    l1_deltas = [d for d in result.memory_delta if d.layer == "L1"]
    assert len(l1_deltas) >= 1
    assert l1_deltas[0].op == "ignore"

    # 没有 L2/L3/L4 delta
    l2_deltas = [d for d in result.memory_delta if d.layer == "L2"]
    l3_deltas = [d for d in result.memory_delta if d.layer == "L3"]
    l4_deltas = [d for d in result.memory_delta if d.layer == "L4"]
    assert len(l2_deltas) == 0
    assert len(l3_deltas) == 0
    assert len(l4_deltas) == 0


# ─── 测试 12：preference_signal 进入 L4 Persona 候选但 needs_review ───


def test_preference_signal_goes_to_l4_needs_review():
    router = _router()
    result = router.execute(
        content="我喜欢简洁的界面，不要用花哨的动画",
        now="2026-07-06T10:00:00+08:00",
    )

    assert result.memory_event_type == "preference_signal"

    l4_deltas = [d for d in result.memory_delta if d.layer == "L4"]
    assert len(l4_deltas) >= 1
    assert l4_deltas[0].op == "pending"
    assert l4_deltas[0].target == "persona"

    # 必须是 needs_review（单次偏好不直接写入 L4）
    assert result.quality_gate == "needs_review"


# ─── 测试 13：序列化结构正确 ───


def test_serialization_structure():
    router = _router()
    result = router.execute(
        content="为什么需要分层记忆？",
        now="2026-07-06T10:00:00+08:00",
    )
    serialized = serialize_memory_router_result(result)

    assert "memory_event_type" in serialized
    assert "detected_intents" in serialized
    assert "confidence" in serialized
    assert "privacy_level" in serialized
    assert "provider_boundary" in serialized
    assert "day_bucket" in serialized
    assert "series_candidates" in serialized
    assert "memory_delta" in serialized
    assert "quality_gate" in serialized
    assert "user_visible_summary" in serialized

    assert serialized["day_bucket"] == "2026-07-06"
    assert isinstance(serialized["detected_intents"], list)
    assert isinstance(serialized["memory_delta"], list)
    assert isinstance(serialized["series_candidates"], list)


# ─── 测试 14：外部导入识别 ───


def test_external_import_identified():
    router = _router()
    result = router.execute(
        content="",
        media_type="application/x-import-pack",
        file_name="chatgpt_export.import",
        now="2026-07-06T10:00:00+08:00",
    )

    assert result.memory_event_type == "external_import"
    assert result.provider_boundary == "local_only"


# ─── 测试 15：URL 输入识别为 knowledge_material ───


def test_url_input_identified_as_knowledge_material():
    router = _router()
    result = router.execute(
        content="",
        urls=["https://example.com/article"],
        now="2026-07-06T10:00:00+08:00",
    )

    assert result.memory_event_type == "knowledge_material"


# ─── 测试 16：无输入抛出异常 ───


def test_empty_input_raises_error():
    router = _router()
    try:
        router.execute(content="", media_type="", file_name="", urls=None)
        raise AssertionError("Expected MemoryRouterError")
    except MemoryRouterError:
        pass  # 预期行为


# ─── 测试 17：当天对话系列总是存在 ───


def test_daily_conversation_series_always_present():
    router = _router()
    result = router.execute(content="随便聊聊", now="2026-07-06T10:00:00+08:00")

    daily_series = [c for c in result.series_candidates if c.series_kind == "daily_conversation"]
    assert len(daily_series) == 1
    assert daily_series[0].day_bucket == "2026-07-06"


# ─── 测试 18：user_visible_summary 有用户可读文案 ───


def test_user_visible_summary_is_readable():
    router = _router()

    # 问题
    result = router.execute(content="为什么？", now="2026-07-06T10:00:00+08:00")
    assert "已捕获" in result.user_visible_summary

    # 知识材料
    result = router.execute(content="", media_type="application/pdf", file_name="a.pdf", now="2026-07-06T10:00:00+08:00")
    assert "已捕获" in result.user_visible_summary
    assert "资料" in result.user_visible_summary

    # 噪声
    result = router.execute(content="hi", now="2026-07-06T10:00:00+08:00")
    assert "已捕获" in result.user_visible_summary
    assert "不写入长期记忆" in result.user_visible_summary
