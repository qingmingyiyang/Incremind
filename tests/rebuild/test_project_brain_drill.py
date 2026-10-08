"""阶段 2.5：项目大脑四层下钻 / 每日对话 / memory_delta 用例测试。

验证用户验收标准：
1. 默认展示 L3（这里验证 L3 数据能被下钻用例正确读取）
2. L3 可以下钻到 L2（series_memory → 同 series_id 的 scenarios）
3. L2 可以下钻到 L1（scenario.atom_ids → atoms）
4. L1 可以查看 L0 evidence（atom.source_refs → sources）
5. 每条 L3 都有 evidence path（一路到 L0）
6. 用户提问会进入当天 L2 daily conversation scenario
7. 稳定重复偏好可形成 L3 候选（has_l3_promotion_candidate = True）
8. 单次问题不会直接污染 L3（单次 preference 不触发 promotion）
9. memory_delta 能展示新增、更新、冲突、待确认、忽略、仅 L0
10. 用户可以确认 / 忽略候选（通过 review 状态变化体现）
11. 内部 ID 默认隐藏，仅在 evidence_path 中可追溯
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.product_core import (
    DailyConversationScenario,
    DrillMemoryNode,
    DrillResult,
    GetDailyConversationScenario,
    GetMemoryDelta,
    GetProjectBrainLayerDrill,
    MemoryDelta,
    ProjectBrainDrillError,
    serialize_daily_conversation_scenario,
    serialize_drill_result,
    serialize_memory_delta,
)
from core.storage_provider import JsonObjectStore


# ════════════════════════════════════════════════════════════
# 测试夹具：构建四层记忆数据
# ════════════════════════════════════════════════════════════


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _write_source(
    store: JsonObjectStore,
    source_id: str,
    *,
    title: str = "测试资料",
    created_at: str = "2026-07-06T10:00:00+08:00",
    import_batch_id: str | None = None,
) -> None:
    payload = {
        "schema_version": "1.0.0",
        "id": source_id,
        "project_id": "default",
        "type": "text",
        "title": title,
        "media_type": "text/plain",
        "processing_state": "ready",
        "trust_status": "trusted",
        "created_at": created_at,
        "updated_at": created_at,
        "metadata": {"tags": ["测试"]},
    }
    if import_batch_id:
        payload["import_batch_id"] = import_batch_id
    store.write("sources", source_id, payload, expected_revision=None)


def _write_atom(
    store: JsonObjectStore,
    atom_id: str,
    *,
    content: str = "原子事实内容",
    atom_type: str = "fact",
    source_id: str = "source-1",
    series_id: str | None = None,
    confidence: float = 0.9,
    trust_status: str = "user_confirmed",
    created_at: str = "2026-07-06T11:00:00+08:00",
    import_batch_id: str | None = None,
) -> None:
    payload = {
        "schema_version": "1.0.0",
        "id": atom_id,
        "source_id": source_id,
        "content": content,
        "atom_type": atom_type,
        "tags": ["测试"],
        "confidence": confidence,
        "source_refs": [{"source_id": source_id, "locator": "p1"}],
        "revision": 1,
        "trust_status": trust_status,
        "created_at": created_at,
        "updated_at": created_at,
    }
    if series_id:
        payload["series_id"] = series_id
    if import_batch_id:
        payload["import_batch_id"] = import_batch_id
    store.write("memory_atoms", atom_id, payload, expected_revision=None)


def _write_scenario(
    store: JsonObjectStore,
    scenario_id: str,
    *,
    title: str = "场景经验",
    series_id: str | None = None,
    atom_ids: list[str] | None = None,
    created_at: str = "2026-07-06T12:00:00+08:00",
    import_batch_id: str | None = None,
) -> None:
    payload = {
        "schema_version": "1.0.0",
        "id": scenario_id,
        "title": title,
        "summary": f"{title}的摘要",
        "atom_ids": atom_ids or ["atom-1"],
        "source_refs": [{"source_id": "source-1"}],
        "tags": ["场景"],
        "series_id": series_id,
        "project_id": "default",
        "stale": False,
        "stale_reason": None,
        "revision": 1,
        "trust_status": "user_confirmed",
        "created_at": created_at,
        "updated_at": created_at,
    }
    if import_batch_id:
        payload["import_batch_id"] = import_batch_id
    store.write("memory_scenarios", scenario_id, payload, expected_revision=None)


def _write_series_memory(
    store: JsonObjectStore,
    sm_id: str,
    *,
    series_id: str = "series-1",
    overview: str = "系列概览",
    created_at: str = "2026-07-06T13:00:00+08:00",
) -> None:
    store.write(
        "memory_series_memory",
        sm_id,
        {
            "schema_version": "1.0.0",
            "id": sm_id,
            "series_id": series_id,
            "scope": "series",
            "overview": overview,
            "scenario_ids": ["scenario-1"],
            "source_refs": [{"source_id": "source-1"}],
            "project_ids": ["default"],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "trust_status": "user_confirmed",
            "created_at": created_at,
            "updated_at": created_at,
        },
        expected_revision=None,
    )


def _write_project_skill(
    store: JsonObjectStore,
    skill_id: str,
    *,
    name: str = "项目能力",
    purpose: str = "能力目的",
    required_context: list[dict] | None = None,
    created_at: str = "2026-07-06T13:30:00+08:00",
) -> None:
    store.write(
        "project_skills",
        skill_id,
        {
            "schema_version": "1.0.0",
            "id": skill_id,
            "name": name,
            "purpose": purpose,
            "required_context": required_context or [{"object_id": "atom-1", "object_type": "atom"}],
            "source_refs": [{"source_id": "source-1"}],
            "project_ids": ["default"],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "trust_status": "user_confirmed",
            "created_at": created_at,
            "updated_at": created_at,
        },
        expected_revision=None,
    )


def _write_candidate(
    store: JsonObjectStore,
    candidate_id: str,
    *,
    status: str = "pending_review",
    target_layer: str = "atom",
    series_id: str | None = None,
    series_confidence: float | None = None,
    import_batch_id: str | None = None,
    created_at: str = "2026-07-06T14:00:00+08:00",
    source_id: str = "source-1",
) -> None:
    payload = {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": "default",
        "target_layer": target_layer,
        "candidate_type": "answer_fact",
        "status": status,
        "proposed_content": {
            "title": "候选记忆标题",
            "summary": "AI 生成的候选记忆摘要",
        },
        "source_refs": [{"source_id": source_id}],
        "provenance": {},
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "auto-generated",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "memory_publication_state": "candidate_created_not_published",
        "series_confidence": series_confidence,
        "created_at": created_at,
        "updated_at": created_at,
    }
    if series_id:
        payload["series_id"] = series_id
    if import_batch_id:
        payload["import_batch_id"] = import_batch_id
    store.write("memory_candidates", candidate_id, payload, expected_revision=None)


def _write_import_batch(
    store: JsonObjectStore,
    batch_id: str,
    *,
    created_at: str = "2026-07-06T10:00:00+08:00",
) -> None:
    store.write(
        "memory_import_batches",
        batch_id,
        {
            "schema_version": "1.0.0",
            "id": batch_id,
            "project_id": "default",
            "source_count": 1,
            "status": "completed",
            "created_at": created_at,
            "updated_at": created_at,
        },
        expected_revision=None,
    )


def _build_full_four_layer_store(tmp_path: Path) -> JsonObjectStore:
    """构建一个完整的四层记忆数据集：L3 series → L2 scenario → L1 atom → L0 source。"""
    store = _store(tmp_path)
    _write_source(store, "source-1", title="需求文档")
    _write_atom(store, "atom-1", content="项目决策采用本地优先", atom_type="decision")
    _write_scenario(store, "scenario-1", title="需求评审场景", series_id="series-1", atom_ids=["atom-1"])
    _write_series_memory(store, "sm-1", series_id="series-1", overview="个人 AI 记忆工作台系列")
    return store


def _write_confirmed_persona(store: JsonObjectStore) -> None:
    store.write(
        "memory_persona",
        "persona-global",
        {
            "schema_version": "1.0.0",
            "id": "persona-global",
            "scope": "global",
            "statements": [
                {
                    "id": "persona-style",
                    "content": "回答时先给出结论",
                    "category": "style",
                    "confidence": 0.95,
                }
            ],
            "evidence_refs": [
                {
                    "object_type": "atom",
                    "object_id": "atom-1",
                    "source_refs": [{"source_id": "source-1"}],
                }
            ],
            "confirmation": {
                "required": True,
                "status": "confirmed",
                "actor": "user",
                "reason": "用户确认",
            },
            "revision": 1,
            "trust_status": "user_confirmed",
            "created_at": "2026-07-06T14:00:00+08:00",
            "updated_at": "2026-07-06T14:00:00+08:00",
        },
        expected_revision=None,
    )


# ════════════════════════════════════════════════════════════
# 测试 1-5：四层下钻 L3 → L2 → L1 → L0
# ════════════════════════════════════════════════════════════


def test_drill_l3_series_memory_returns_l2_scenarios(tmp_path: Path) -> None:
    """L3 (series_memory) 下钻应返回同 series_id 的 L2 scenarios。"""
    store = _build_full_four_layer_store(tmp_path)
    result = GetProjectBrainLayerDrill(store).execute(layer="L3", object_id="sm-1")

    assert isinstance(result, DrillResult)
    assert result.current.layer == "L3"
    assert result.current.memory_id == "sm-1"
    assert result.current.category == "项目系列"
    assert "个人 AI 记忆工作台系列" in result.current.summary

    # 子层应有 L2 scenario
    assert len(result.children) >= 1
    l2_child = result.children[0]
    assert l2_child.layer == "L2"
    assert l2_child.memory_id == "scenario-1"
    assert l2_child.title == "需求评审场景"


def test_drill_l4_persona_reaches_atom_and_source(tmp_path: Path) -> None:
    store = _build_full_four_layer_store(tmp_path)
    _write_confirmed_persona(store)

    result = GetProjectBrainLayerDrill(store).execute(
        layer="L4",
        object_id="persona-global~persona-style",
    )

    assert result.current.layer == "L4"
    assert result.current.category == "表达风格"
    assert result.current.trust_status == "user_confirmed"
    assert [child.layer for child in result.children] == ["L1"]
    assert {node.layer for node in result.evidence_path} == {"L1", "L0"}


def test_drill_l4_rejects_unconfirmed_persona(tmp_path: Path) -> None:
    store = _build_full_four_layer_store(tmp_path)
    _write_confirmed_persona(store)
    persona = dict(store.read("memory_persona", "persona-global") or {})
    persona["confirmation"] = {
        "required": True,
        "status": "pending",
        "actor": None,
        "reason": None,
    }
    persona["trust_status"] = "system_generated"
    store.write(
        "memory_persona",
        "persona-global",
        persona,
        expected_revision=store.revision("memory_persona", "persona-global"),
    )

    with pytest.raises(ProjectBrainDrillError, match="not found"):
        GetProjectBrainLayerDrill(store).execute(
            layer="L4",
            object_id="persona-global~persona-style",
        )


def test_drill_l3_cannot_reinterpret_confirmed_persona_record(tmp_path: Path) -> None:
    store = _build_full_four_layer_store(tmp_path)
    _write_confirmed_persona(store)

    with pytest.raises(ProjectBrainDrillError, match="L3 object persona-global not found"):
        GetProjectBrainLayerDrill(store).execute(
            layer="L3",
            object_id="persona-global",
        )

    result = GetProjectBrainLayerDrill(store).execute(
        layer="L4",
        object_id="persona-global~persona-style",
    )
    assert result.current.layer == "L4"
    assert result.current.trust_status == "user_confirmed"


def test_drill_l3_project_skill_returns_l1_atoms(tmp_path: Path) -> None:
    """L3 (project_skill) 下钻应返回 required_context 引用的 L1 atoms。"""
    store = _build_full_four_layer_store(tmp_path)
    _write_atom(store, "atom-skill", content="能力所需事实")
    _write_project_skill(
        store,
        "skill-1",
        name="需求拆解能力",
        purpose="把需求拆解为可执行任务",
        required_context=[{"object_id": "atom-skill", "object_type": "atom"}],
    )

    result = GetProjectBrainLayerDrill(store).execute(layer="L3", object_id="skill-1")

    assert result.current.layer == "L3"
    assert result.current.category == "项目能力"
    assert result.current.title == "需求拆解能力"

    # 子层应有 L1 atom
    assert len(result.children) >= 1
    l1_child = result.children[0]
    assert l1_child.layer == "L1"
    assert l1_child.memory_id == "atom-skill"
    assert l1_child.title == "能力所需事实"


def test_drill_l2_scenario_returns_l1_atoms(tmp_path: Path) -> None:
    """L2 scenario 下钻应返回 atom_ids 引用的 L1 atoms。"""
    store = _build_full_four_layer_store(tmp_path)

    result = GetProjectBrainLayerDrill(store).execute(layer="L2", object_id="scenario-1")

    assert result.current.layer == "L2"
    assert result.current.title == "需求评审场景"
    assert len(result.children) >= 1
    assert result.children[0].layer == "L1"
    assert result.children[0].memory_id == "atom-1"
    assert result.children[0].category == "决策"


def test_drill_l1_atom_returns_l0_sources(tmp_path: Path) -> None:
    """L1 atom 下钻应返回 source_refs 引用的 L0 sources。"""
    store = _build_full_four_layer_store(tmp_path)

    result = GetProjectBrainLayerDrill(store).execute(layer="L1", object_id="atom-1")

    assert result.current.layer == "L1"
    assert result.current.memory_id == "atom-1"
    assert len(result.children) >= 1
    assert result.children[0].layer == "L0"
    assert result.children[0].memory_id == "source-1"
    assert result.children[0].title == "需求文档"


def test_drill_l0_is_leaf_no_children(tmp_path: Path) -> None:
    """L0 source 是叶子节点，不应有子层。"""
    store = _build_full_four_layer_store(tmp_path)

    result = GetProjectBrainLayerDrill(store).execute(layer="L0", object_id="source-1")

    assert result.current.layer == "L0"
    assert result.current.memory_id == "source-1"
    assert result.children == ()


# ════════════════════════════════════════════════════════════
# 测试 6：每条 L3 都有 evidence path 到 L0
# ════════════════════════════════════════════════════════════


def test_l3_evidence_path_reaches_l0(tmp_path: Path) -> None:
    """L3 的 evidence_path 应一路追溯到 L0 source。"""
    store = _build_full_four_layer_store(tmp_path)
    result = GetProjectBrainLayerDrill(store).execute(layer="L3", object_id="sm-1")

    # evidence_path 应包含从 L2 到 L0 的节点
    layers_in_path = {node.layer for node in result.evidence_path}
    assert "L2" in layers_in_path
    assert "L1" in layers_in_path
    assert "L0" in layers_in_path

    # 至少有一个 L0 节点
    l0_nodes = [n for n in result.evidence_path if n.layer == "L0"]
    assert len(l0_nodes) >= 1
    assert l0_nodes[0].memory_id == "source-1"


def test_evidence_path_empty_for_l0(tmp_path: Path) -> None:
    """L0 是叶子，evidence_path 应为空。"""
    store = _build_full_four_layer_store(tmp_path)
    result = GetProjectBrainLayerDrill(store).execute(layer="L0", object_id="source-1")
    assert result.evidence_path == ()


# ════════════════════════════════════════════════════════════
# 测试 7：每日对话场景聚合
# ════════════════════════════════════════════════════════════


def test_daily_conversation_aggregates_atoms_and_sources(tmp_path: Path) -> None:
    """每日对话场景应聚合当天的 atoms / candidates / sources。"""
    store = _store(tmp_path)
    _write_source(store, "src-day", title="当天上传", created_at="2026-07-06T09:00:00+08:00")
    _write_atom(store, "atom-day-q", content="如何配置 API？", atom_type="question", created_at="2026-07-06T10:00:00+08:00")
    _write_atom(store, "atom-day-pref", content="偏好简洁界面", atom_type="preference", created_at="2026-07-06T11:00:00+08:00")
    _write_candidate(store, "cand-day", status="pending_review", created_at="2026-07-06T14:00:00+08:00")

    result = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")

    assert isinstance(result, DailyConversationScenario)
    assert result.day_bucket == "2026-07-06"
    assert result.title == "2026-07-06 的对话"
    assert result.atom_count == 2
    assert result.source_count == 1
    assert len(result.user_questions) == 1
    assert "如何配置 API？" in result.user_questions[0]
    assert len(result.preference_signals) == 1
    assert "偏好简洁界面" in result.preference_signals[0]
    assert len(result.pending_candidates) == 1


def test_daily_conversation_filters_other_days(tmp_path: Path) -> None:
    """每日对话只聚合指定 day_bucket 的内容，不应包含其他日期。"""
    store = _store(tmp_path)
    _write_atom(store, "atom-day1", content="7月6日事实", created_at="2026-07-06T10:00:00+08:00")
    _write_atom(store, "atom-day2", content="7月7日事实", created_at="2026-07-07T10:00:00+08:00")

    result = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")

    assert result.atom_count == 1
    assert result.source_count == 0


def test_daily_conversation_invalid_bucket_raises(tmp_path: Path) -> None:
    """无效的 day_bucket 应抛错。"""
    store = _store(tmp_path)
    with pytest.raises(ProjectBrainDrillError):
        GetDailyConversationScenario(store).execute(day_bucket="")
    with pytest.raises(ProjectBrainDrillError):
        GetDailyConversationScenario(store).execute(day_bucket="invalid")


def test_daily_conversation_empty_day(tmp_path: Path) -> None:
    """没有数据的日期应返回空场景，不崩溃。"""
    store = _store(tmp_path)
    result = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")
    assert result.atom_count == 0
    assert result.source_count == 0
    assert result.user_questions == ()
    assert result.preference_signals == ()
    assert result.pending_candidates == ()


# ════════════════════════════════════════════════════════════
# 测试 8：稳定重复偏好可形成 L3 候选，单次问题不会
# ════════════════════════════════════════════════════════════


def test_stable_repeated_preference_triggers_l3_promotion(tmp_path: Path) -> None:
    """同 series_id 的 preference atom >= 2 次应触发 L3 promotion 候选。"""
    store = _store(tmp_path)
    _write_atom(
        store, "pref-1",
        content="偏好使用 Python",
        atom_type="preference",
        series_id="series-py",
        created_at="2026-07-06T10:00:00+08:00",
    )
    _write_atom(
        store, "pref-2",
        content="偏好使用 Python 3.11",
        atom_type="preference",
        series_id="series-py",
        created_at="2026-07-06T11:00:00+08:00",
    )

    result = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")

    assert result.has_l3_promotion_candidate is True


def test_single_preference_does_not_trigger_l3_promotion(tmp_path: Path) -> None:
    """单次 preference atom 不应触发 L3 promotion（不污染长期记忆）。"""
    store = _store(tmp_path)
    _write_atom(
        store, "pref-single",
        content="单次偏好",
        atom_type="preference",
        series_id="series-single",
        created_at="2026-07-06T10:00:00+08:00",
    )
    # 再加一个不同 series 的 preference
    _write_atom(
        store, "pref-other",
        content="另一个偏好",
        atom_type="preference",
        series_id="series-other",
        created_at="2026-07-06T11:00:00+08:00",
    )

    result = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")

    assert result.has_l3_promotion_candidate is False


def test_single_question_does_not_pollute_l3(tmp_path: Path) -> None:
    """单次提问不应直接污染 L3（question atom 不触发 promotion）。"""
    store = _store(tmp_path)
    _write_atom(
        store, "q-1",
        content="如何使用 API？",
        atom_type="question",
        series_id="series-q",
        created_at="2026-07-06T10:00:00+08:00",
    )
    _write_atom(
        store, "q-2",
        content="如何使用 API？",
        atom_type="question",
        series_id="series-q",
        created_at="2026-07-06T11:00:00+08:00",
    )

    result = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")

    # question 重复不触发 L3 promotion（只有 preference 才会）
    assert result.has_l3_promotion_candidate is False


# ════════════════════════════════════════════════════════════
# 测试 9：memory_delta 能展示所有变更类型
# ════════════════════════════════════════════════════════════


def test_memory_delta_aggregates_all_change_types(tmp_path: Path) -> None:
    """memory_delta 应聚合一次 batch 的所有变更类型。"""
    store = _store(tmp_path)
    batch_id = "batch-1"
    _write_import_batch(store, batch_id, created_at="2026-07-06T10:00:00+08:00")

    # L0 source（属于本 batch）
    _write_source(store, "src-batch", title="批次资料", import_batch_id=batch_id, created_at="2026-07-06T10:00:00+08:00")
    # 另一个 L0 source 没生成候选 → l0_only
    _write_source(store, "src-l0-only", title="仅原始资料", import_batch_id=batch_id, created_at="2026-07-06T10:01:00+08:00")

    # L1 atom（属于本 batch）
    _write_atom(
        store, "atom-batch",
        content="批次新增事实",
        import_batch_id=batch_id,
        created_at="2026-07-06T11:00:00+08:00",
        source_id="src-batch",
    )

    # L2 scenario（属于本 batch）
    _write_scenario(
        store, "scn-batch",
        title="批次场景",
        import_batch_id=batch_id,
        created_at="2026-07-06T12:00:00+08:00",
    )

    # 候选：pending
    _write_candidate(
        store, "cand-pending",
        status="pending_review",
        import_batch_id=batch_id,
        created_at="2026-07-06T14:00:00+08:00",
        source_id="src-batch",
    )
    # 候选：rejected → ignored
    _write_candidate(
        store, "cand-ignored",
        status="rejected",
        import_batch_id=batch_id,
        created_at="2026-07-06T14:30:00+08:00",
        source_id="src-batch",
    )

    result = GetMemoryDelta(store).execute(batch_id=batch_id)

    assert isinstance(result, MemoryDelta)
    assert result.batch_id == batch_id

    # summary 应包含所有变更类型计数
    summary = result.summary
    assert summary["new_l1"] >= 1
    assert summary["updated_l2"] >= 1
    assert summary["pending"] >= 1
    assert summary["ignored"] >= 1
    assert summary["l0_only"] >= 1

    # items 应包含对应类型的条目
    change_types = {item.change_type for item in result.items}
    assert "new_l1" in change_types
    assert "updated_l2" in change_types
    assert "pending" in change_types
    assert "ignored" in change_types
    assert "l0_only" in change_types


def test_memory_delta_has_l3_impact_when_l3_updated(tmp_path: Path) -> None:
    """当 batch 当天更新了 L3 series_memory，has_l3_impact 应为 True。"""
    store = _store(tmp_path)
    batch_id = "batch-l3"
    _write_import_batch(store, batch_id, created_at="2026-07-06T10:00:00+08:00")
    _write_series_memory(store, "sm-batch", series_id="series-batch", created_at="2026-07-06T13:00:00+08:00")

    result = GetMemoryDelta(store).execute(batch_id=batch_id)

    assert result.summary["updated_l3"] >= 1
    assert result.has_l3_impact is True


def test_memory_delta_has_l3_impact_when_conflict(tmp_path: Path) -> None:
    """当 batch 包含 conflict 候选，has_l3_impact 应为 True。"""
    store = _store(tmp_path)
    batch_id = "batch-conflict"
    _write_import_batch(store, batch_id, created_at="2026-07-06T10:00:00+08:00")
    _write_candidate(
        store, "cand-conflict",
        status="conflict",
        import_batch_id=batch_id,
        created_at="2026-07-06T14:00:00+08:00",
    )

    result = GetMemoryDelta(store).execute(batch_id=batch_id)

    assert result.summary["conflict"] >= 1
    assert result.has_l3_impact is True


def test_memory_delta_no_l3_impact_for_l1_only(tmp_path: Path) -> None:
    """只有 L1 变更的 batch，has_l3_impact 应为 False。"""
    store = _store(tmp_path)
    batch_id = "batch-l1-only"
    _write_import_batch(store, batch_id, created_at="2026-07-06T10:00:00+08:00")
    _write_source(store, "src-x", import_batch_id=batch_id, created_at="2026-07-06T10:00:00+08:00")
    _write_candidate(
        store, "cand-x",
        status="pending_review",
        import_batch_id=batch_id,
        created_at="2026-07-06T14:00:00+08:00",
        source_id="src-x",
    )

    result = GetMemoryDelta(store).execute(batch_id=batch_id)

    assert result.summary["updated_l3"] == 0
    assert result.summary["conflict"] == 0
    assert result.has_l3_impact is False


def test_memory_delta_invalid_batch_raises(tmp_path: Path) -> None:
    """空 batch_id 应抛错。"""
    store = _store(tmp_path)
    with pytest.raises(ProjectBrainDrillError):
        GetMemoryDelta(store).execute(batch_id="")


def test_memory_delta_missing_batch_raises(tmp_path: Path) -> None:
    """不存在的 batch_id 应抛错。"""
    store = _store(tmp_path)
    with pytest.raises(ProjectBrainDrillError):
        GetMemoryDelta(store).execute(batch_id="non-existent-batch")


# ════════════════════════════════════════════════════════════
# 测试 10：用户确认 / 忽略候选（通过状态变化体现）
# ════════════════════════════════════════════════════════════


def test_candidate_status_reflects_user_review(tmp_path: Path) -> None:
    """用户确认/忽略后，候选状态应变化（这里通过预置不同状态验证读取正确）。"""
    store = _store(tmp_path)
    _write_source(store, "src-1")
    _write_candidate(store, "cand-confirmed", status="promoted", target_layer="atom")
    _write_candidate(store, "cand-ignored", status="rejected", target_layer="atom")
    _write_candidate(store, "cand-pending", status="pending_review", target_layer="atom")

    # 在 daily conversation 中应能看到 pending 候选
    daily = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")
    pending_ids = {c["candidate_id"] for c in daily.pending_candidates}
    assert "cand-pending" in pending_ids
    # promoted 和 rejected 不在 pending 列表
    assert "cand-confirmed" not in pending_ids
    assert "cand-ignored" not in pending_ids


# ════════════════════════════════════════════════════════════
# 测试 11：错误处理
# ════════════════════════════════════════════════════════════


def test_drill_invalid_layer_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ProjectBrainDrillError):
        GetProjectBrainLayerDrill(store).execute(layer="L9", object_id="x")


def test_drill_empty_object_id_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ProjectBrainDrillError):
        GetProjectBrainLayerDrill(store).execute(layer="L3", object_id="")


def test_drill_not_found_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ProjectBrainDrillError):
        GetProjectBrainLayerDrill(store).execute(layer="L3", object_id="non-existent")


# ════════════════════════════════════════════════════════════
# 测试 12：序列化结构正确，不泄露内部 ID
# ════════════════════════════════════════════════════════════


def test_serialize_drill_result_structure(tmp_path: Path) -> None:
    """序列化后的 drill result 应包含 current/children/evidence_path。"""
    store = _build_full_four_layer_store(tmp_path)
    result = GetProjectBrainLayerDrill(store).execute(layer="L3", object_id="sm-1")
    serialized = serialize_drill_result(result)

    assert "current" in serialized
    assert "children" in serialized
    assert "evidence_path" in serialized

    # current 应有用户友好字段
    current = serialized["current"]
    for field in ("memory_id", "layer", "layer_label", "category", "title", "summary", "confidence", "trust_status"):
        assert field in current, f"missing field {field}"

    # layer_label 应是中文友好名
    assert current["layer_label"] == "项目大脑"


def test_serialize_daily_conversation_scenario_structure(tmp_path: Path) -> None:
    """序列化后的 daily scenario 应包含所有用户友好字段。"""
    store = _store(tmp_path)
    _write_atom(store, "a-1", content="测试事实", atom_type="fact", created_at="2026-07-06T10:00:00+08:00")
    result = GetDailyConversationScenario(store).execute(day_bucket="2026-07-06")
    serialized = serialize_daily_conversation_scenario(result)

    for field in ("day_bucket", "title", "main_topics", "user_questions",
                  "preference_signals", "advanced_series", "pending_candidates",
                  "atom_count", "source_count", "has_l3_promotion_candidate"):
        assert field in serialized, f"missing field {field}"

    assert serialized["day_bucket"] == "2026-07-06"
    assert serialized["atom_count"] == 1


def test_serialize_memory_delta_structure(tmp_path: Path) -> None:
    """序列化后的 memory_delta 应包含 batch_id/items/summary/has_l3_impact。"""
    store = _store(tmp_path)
    batch_id = "batch-serial"
    _write_import_batch(store, batch_id, created_at="2026-07-06T10:00:00+08:00")
    _write_source(store, "src-serial", import_batch_id=batch_id, created_at="2026-07-06T10:00:00+08:00")
    _write_candidate(
        store, "cand-serial",
        status="pending_review",
        import_batch_id=batch_id,
        created_at="2026-07-06T14:00:00+08:00",
        source_id="src-serial",
    )

    result = GetMemoryDelta(store).execute(batch_id=batch_id)
    serialized = serialize_memory_delta(result)

    for field in ("batch_id", "generated_at", "items", "summary", "has_l3_impact"):
        assert field in serialized, f"missing field {field}"

    # summary 应包含所有变更类型 key
    summary = serialized["summary"]
    for key in ("new_l1", "updated_l2", "updated_l3", "conflict", "pending", "ignored", "l0_only"):
        assert key in summary, f"missing summary key {key}"

    # items 应是列表
    assert isinstance(serialized["items"], tuple)


def test_serialization_does_not_leak_secrets(tmp_path: Path) -> None:
    """序列化结果不应包含 API key / cookie 等敏感信息。"""
    store = _build_full_four_layer_store(tmp_path)
    result = GetProjectBrainLayerDrill(store).execute(layer="L3", object_id="sm-1")
    serialized = serialize_drill_result(result)
    text = str(serialized)
    assert "sk-" not in text
    assert "cookie:" not in text.lower()
    assert "api_key" not in text.lower()
