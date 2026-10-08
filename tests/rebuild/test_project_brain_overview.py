"""阶段 5：项目大脑 / 记忆墙聚合用例测试。

验证：
1. 能展示 L0-L4 层级
2. 能展示"AI 现在知道什么"
3. 能展示某条记忆的证据来源
4. 能展示本次上传新增 / 更新 / 冲突 / 待确认
5. 能确认候选记忆（通过候选状态变化体现）
6. 能忽略候选记忆（通过候选状态变化体现）
7. 低置信度系列出现在待确认区，而不是散落在其他页面
"""
from __future__ import annotations

from pathlib import Path

from core.product_core import GetProjectBrainOverview, serialize_project_brain_overview
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _write_source(
    store: JsonObjectStore, source_id: str, title: str = "测试资料", *, project_id: str = "default",
) -> None:
    store.write(
        "sources",
        source_id,
        {
            "schema_version": "1.0.0",
            "id": source_id,
            "project_id": project_id,
            "type": "text",
            "title": title,
            "media_type": "text/plain",
            "processing_state": "ready",
            "trust_status": "trusted",
            "created_at": "2026-07-06T10:00:00+08:00",
            "updated_at": "2026-07-06T10:00:00+08:00",
            "metadata": {"tags": ["测试", "项目"]},
        },
        expected_revision=None,
    )


def _write_atom(
    store: JsonObjectStore, atom_id: str, content: str, atom_type: str = "fact", *, source_id: str = "source-1",
) -> None:
    store.write(
        "memory_atoms",
        atom_id,
        {
            "schema_version": "1.0.0",
            "id": atom_id,
            "source_id": source_id,
            "content": content,
            "atom_type": atom_type,
            "tags": ["项目事实"],
            "confidence": 0.9,
            "source_refs": [{"source_id": source_id, "locator": "p1"}],
            "revision": 1,
            "trust_status": "user_confirmed",
            "created_at": "2026-07-06T11:00:00+08:00",
            "updated_at": "2026-07-06T11:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_scenario(
    store: JsonObjectStore, scenario_id: str, title: str, *, project_id: str = "default", source_id: str = "source-1",
) -> None:
    store.write(
        "memory_scenarios",
        scenario_id,
        {
            "schema_version": "1.0.0",
            "id": scenario_id,
            "title": title,
            "summary": f"{title}的摘要",
            "atom_ids": ["atom-1"],
            "source_refs": [{"source_id": source_id}],
            "tags": ["场景"],
            "series_id": None,
            "project_id": project_id,
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "trust_status": "user_confirmed",
            "created_at": "2026-07-06T12:00:00+08:00",
            "updated_at": "2026-07-06T12:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_series_memory(
    store: JsonObjectStore, sm_id: str, series_id: str, *, project_ids: list[str] | None = None, source_id: str = "source-1",
) -> None:
    store.write(
        "memory_series_memory",
        sm_id,
        {
            "schema_version": "1.0.0",
            "id": sm_id,
            "series_id": series_id,
            "scope": "series",
            "overview": f"{series_id}的概览",
            "scenario_ids": ["scenario-1"],
            "source_refs": [{"source_id": source_id}],
            "project_ids": project_ids or ["default"],
            "stale": False,
            "stale_reason": None,
            "revision": 1,
            "trust_status": "user_confirmed",
            "created_at": "2026-07-06T13:00:00+08:00",
            "updated_at": "2026-07-06T13:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_persona(
    store: JsonObjectStore,
    *,
    status: str = "confirmed",
    trust_status: str = "user_confirmed",
) -> None:
    store.write(
        "memory_persona",
        "persona-global",
        {
            "schema_version": "1.0.0",
            "id": "persona-global",
            "scope": "global",
            "statements": [
                {
                    "id": "persona-statement-style",
                    "content": "回答时先给出结论",
                    "category": "style",
                    "confidence": 0.95,
                },
                {
                    "id": "persona-statement-constraint",
                    "content": "重要判断必须保留证据引用",
                    "category": "constraint",
                    "confidence": 1.0,
                },
            ],
            "evidence_refs": [
                {
                    "object_type": "atom",
                    "object_id": "atom-1",
                    "source_refs": [{"source_id": "source-1", "locator": "p1"}],
                }
            ],
            "confirmation": {
                "required": True,
                "status": status,
                "actor": "user" if status == "confirmed" else None,
                "reason": "用户确认" if status == "confirmed" else None,
            },
            "revision": 2,
            "trust_status": trust_status,
            "created_at": "2026-07-06T14:00:00+08:00",
            "updated_at": "2026-07-06T14:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_candidate(
    store: JsonObjectStore,
    candidate_id: str,
    *,
    status: str = "pending_review",
    target_layer: str = "atom",
    series_confidence: float | None = None,
    project_id: str = "default",
    source_id: str = "source-1",
    provenance: dict | None = None,
) -> None:
    store.write(
        "memory_candidates",
        candidate_id,
        {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": project_id,
            "target_layer": target_layer,
            "candidate_type": "answer_fact",
            "status": status,
            "proposed_content": {
                "title": "候选记忆标题",
                "summary": "AI 生成的候选记忆摘要",
            },
            "source_refs": [{"source_id": source_id}],
            "provenance": provenance if provenance is not None else {},
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "reason": "auto-generated",
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "memory_publication_state": "candidate_created_not_published",
            "series_confidence": series_confidence,
            "created_at": "2026-07-06T14:00:00+08:00",
            "updated_at": "2026-07-06T14:00:00+08:00",
        },
        expected_revision=None,
    )


def _write_transition(store: JsonObjectStore, trans_id: str, transition_type: str, created_at: str) -> None:
    store.write(
        "memory_transitions",
        trans_id,
        {
            "schema_version": "1.0.0",
            "id": trans_id,
            "transition_type": transition_type,
            "layer": "atom",
            "object_id": "atom-1",
            "from_trust_status": "system_generated",
            "to_trust_status": "user_confirmed",
            "from_revision": 0,
            "to_revision": 1,
            "evidence_refs": [
                {
                    "object_type": "source",
                    "object_id": "source-1",
                    "source_refs": [{"source_id": "source-1"}],
                }
            ],
            "created_at": created_at,
        },
        expected_revision=None,
    )


# ─── 测试 1：能展示 L0-L4 层级 ───


def test_brain_overview_shows_all_five_layers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    _write_atom(store, "atom-1", "项目偏好使用 Python")
    _write_scenario(store, "scenario-1", "需求评审场景")
    _write_series_memory(store, "sm-1", "个人 AI 记忆工作台")
    _write_persona(store)

    result = GetProjectBrainOverview(store).execute()

    layer_codes = [s.layer for s in result.layer_summaries]
    assert layer_codes == ["L0", "L1", "L2", "L3", "L4"]
    layer_labels = [s.label for s in result.layer_summaries]
    assert "原始资料" in layer_labels
    assert "原子事实" in layer_labels
    assert "场景经验" in layer_labels
    assert "项目大脑" in layer_labels
    assert "稳定画像" in layer_labels

    # 每层都有计数
    counts = {s.layer: s.count for s in result.layer_summaries}
    assert counts["L0"] >= 1
    assert counts["L1"] >= 1
    assert counts["L2"] >= 1
    assert counts["L3"] >= 1
    assert counts["L4"] == 2


def test_brain_overview_isolates_named_projects_but_keeps_global_persona(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-alpha", "Alpha 原始资料", project_id="project-alpha")
    _write_source(store, "source-beta", "Beta 原始资料", project_id="project-beta")
    _write_atom(store, "atom-alpha", "Alpha 原子事实", source_id="source-alpha")
    _write_atom(store, "atom-beta", "Beta 原子事实", source_id="source-beta")
    _write_scenario(
        store, "scenario-alpha", "Alpha 场景", project_id="project-alpha", source_id="source-alpha",
    )
    _write_scenario(
        store, "scenario-beta", "Beta 场景", project_id="project-beta", source_id="source-beta",
    )
    _write_series_memory(
        store, "series-alpha", "Alpha 系列", project_ids=["project-alpha"], source_id="source-alpha",
    )
    _write_series_memory(
        store, "series-beta", "Beta 系列", project_ids=["project-beta"], source_id="source-beta",
    )
    _write_candidate(
        store, "candidate-alpha", project_id="project-alpha", source_id="source-alpha",
    )
    _write_candidate(
        store, "candidate-beta", project_id="project-beta", source_id="source-beta",
    )
    _write_persona(store)

    result = GetProjectBrainOverview(store).execute(scope="global", project_id="project-alpha")
    memory_ids = {memory.memory_id for memory in result.memories}
    candidate_ids = {candidate.candidate_id for candidate in result.candidates}

    assert {"source-alpha", "atom-alpha", "scenario-alpha", "series-alpha"} <= memory_ids
    assert not {"source-beta", "atom-beta", "scenario-beta", "series-beta"} & memory_ids
    assert candidate_ids == {"candidate-alpha"}
    assert any(memory.layer == "L4" and memory.memory_id.startswith("persona-global~") for memory in result.memories)


# ─── 测试 2：能展示"AI 现在知道什么" ───


def test_brain_overview_shows_what_ai_knows(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1", "需求文档")
    _write_atom(store, "atom-1", "用户偏好简洁界面", atom_type="preference")
    _write_atom(store, "atom-2", "项目决策采用本地优先", atom_type="decision")

    result = GetProjectBrainOverview(store).execute()

    # 记忆条目包含用户偏好和项目决策类别
    categories = {m.category for m in result.memories}
    assert "用户偏好" in categories
    assert "项目决策" in categories
    assert "原始资料" in categories

    # 每条记忆都有标题和摘要
    for memory in result.memories:
        assert memory.title
        assert memory.summary
        assert memory.layer in ("L0", "L1", "L2", "L3", "L4")


def test_brain_overview_places_only_confirmed_persona_in_l4(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    _write_atom(store, "atom-1", "用户偏好先看结论", atom_type="preference")
    _write_persona(store)

    result = GetProjectBrainOverview(store).execute()
    l4 = [memory for memory in result.memories if memory.layer == "L4"]

    assert [memory.category for memory in l4] == ["语言风格", "项目规则"]
    assert all(memory.trust_status == "user_confirmed" for memory in l4)
    assert all(memory.evidence_refs[0]["object_id"] == "atom-1" for memory in l4)
    assert result.persona_ready is True
    assert result.persona_revision == 2
    assert not any(
        memory.layer == "L3" and memory.category == "Persona"
        for memory in result.memories
    )


def test_brain_overview_excludes_pending_persona_from_counts_and_last_updated(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _write_persona(store, status="pending", trust_status="system_generated")

    result = GetProjectBrainOverview(store).execute()
    counts = {summary.layer: summary.count for summary in result.layer_summaries}

    assert counts["L4"] == 0
    assert result.total_memories == 0
    assert result.last_updated_at is None
    assert result.persona_ready is False
    assert all(memory.layer != "L4" for memory in result.memories)


# ─── 测试 3：能展示某条记忆的证据来源 ───


def test_brain_overview_shows_evidence_for_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1", "需求文档")
    _write_atom(store, "atom-1", "项目决策采用本地优先")

    result = GetProjectBrainOverview(store).execute()

    atom_memory = next(m for m in result.memories if m.layer == "L1")
    # 原子记忆必须有证据来源
    assert len(atom_memory.evidence_refs) > 0
    evidence = atom_memory.evidence_refs[0]
    assert evidence["object_type"] == "atom"
    assert evidence["object_id"] == "atom-1"
    # 必须能回溯到原始资料
    assert "source-1" in atom_memory.related_source_ids


# ─── 测试 4：能展示本次上传新增 / 更新 / 冲突 / 待确认 ───


def test_brain_overview_shows_recent_changes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    _write_transition(store, "trans-1", "confirm", "2026-07-06T15:00:00+08:00")
    _write_transition(store, "trans-2", "demote", "2026-07-06T15:30:00+08:00")
    _write_candidate(store, "cand-1")  # pending

    result = GetProjectBrainOverview(store).execute()

    change_types = {c.change_type for c in result.recent_changes}
    # 应包含新增、被忽略（回滚）、待确认
    assert "new" in change_types
    assert "ignored" in change_types
    assert "pending" in change_types


# ─── 测试 5 & 6：候选记忆状态变化（确认 / 忽略） ───


def test_brain_overview_lists_pending_candidates_for_review(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    _write_candidate(store, "cand-pending", status="pending_review")
    _write_candidate(store, "cand-rejected", status="rejected")

    result = GetProjectBrainOverview(store).execute()

    # 候选列表包含 pending 和 rejected 两种状态
    statuses = {c.status for c in result.candidates}
    assert "pending_review" in statuses
    assert "rejected" in statuses
    # pending 排在前面
    first_pending = next(c for c in result.candidates if c.status == "pending_review")
    assert first_pending.candidate_id == "cand-pending"


def test_brain_overview_hides_promoted_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    # 已提升的候选不再展示（已变成正式记忆）
    _write_candidate(store, "cand-promoted", status="promoted")
    _write_candidate(store, "cand-pending", status="pending_review")

    result = GetProjectBrainOverview(store).execute()

    candidate_ids = {c.candidate_id for c in result.candidates}
    assert "cand-pending" in candidate_ids
    assert "cand-promoted" not in candidate_ids


def test_brain_overview_surfaces_expert_proposal_id_only_for_expert_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    _write_candidate(
        store, "cand-expert", provenance={"external_agent_proposal_id": "expert-abc123def4567890"},
    )
    _write_candidate(
        store, "cand-external",
        provenance={"external_agent_proposal_id": "ext-regular-agent-01"},
    )
    _write_candidate(store, "cand-plain")

    result = GetProjectBrainOverview(store).execute()

    by_id = {c.candidate_id: c for c in result.candidates}
    assert by_id["cand-expert"].expert_proposal_id == "expert-abc123def4567890"
    assert by_id["cand-external"].expert_proposal_id is None
    assert by_id["cand-plain"].expert_proposal_id is None


# ─── 测试 7：低置信度系列出现在待确认区 ───


def test_low_confidence_candidate_appears_in_pending_area(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    # 低置信度候选（series_confidence 低于阈值）
    _write_candidate(
        store,
        "cand-low-conf",
        status="pending_review",
        target_layer="series_memory",
        series_confidence=0.45,
    )
    # 高置信度候选
    _write_candidate(
        store,
        "cand-high-conf",
        status="pending_review",
        target_layer="atom",
        series_confidence=0.92,
    )

    result = GetProjectBrainOverview(store).execute()

    # 两个候选都出现在待确认区
    candidate_ids = {c.candidate_id for c in result.candidates}
    assert "cand-low-conf" in candidate_ids
    assert "cand-high-conf" in candidate_ids

    # 低置信度候选的 series_confidence 被保留
    low_conf_cand = next(c for c in result.candidates if c.candidate_id == "cand-low-conf")
    assert low_conf_cand.series_confidence == 0.45
    assert low_conf_cand.target_layer == "series_memory"

    # 也出现在 recent_changes 的 pending 区
    pending_changes = [c for c in result.recent_changes if c.change_type == "pending"]
    pending_memory_ids = {c.memory_id for c in pending_changes}
    assert "cand-low-conf" in pending_memory_ids


# ─── 测试 8：序列化结构正确 ───


def test_brain_overview_serialization(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-1")
    _write_atom(store, "atom-1", "项目事实")
    _write_candidate(store, "cand-1")

    result = GetProjectBrainOverview(store).execute()
    serialized = serialize_project_brain_overview(result)

    assert "scope" in serialized
    assert "layer_summaries" in serialized
    assert "memories" in serialized
    assert "candidates" in serialized
    assert "recent_changes" in serialized
    assert "persona_ready" in serialized
    assert "total_memories" in serialized
    assert "total_candidates" in serialized

    # 序列化后的记忆条目含必要字段
    if serialized["memories"]:
        m = serialized["memories"][0]
        for field in ("memory_id", "layer", "category", "title", "summary", "evidence_refs"):
            assert field in m, f"missing field {field} in serialized memory"

    # 不应包含内部路径/secret
    serialized_text = str(serialized)
    assert "sk-" not in serialized_text
    assert "cookie:" not in serialized_text.lower()


def test_brain_overview_serializes_safe_retrieval_status(tmp_path: Path) -> None:
    status = {
        "project_id": "default",
        "status": "ready",
        "label": "项目概况已准备",
        "message": "回答问题时会优先参考这些已发布的项目记忆。",
        "source": {
            "kind": "published_project_memory",
            "label": "已发布项目记忆",
            "series_count": 2,
            "source_count": 5,
        },
        "generated_at": "2026-07-06T09:45:00+08:00",
    }
    result = GetProjectBrainOverview(
        _store(tmp_path),
        retrieval_status=status,
    ).execute()

    assert serialize_project_brain_overview(result)["retrieval_status"] == status


# ─── 测试 9：空数据库不崩溃 ───


def test_brain_overview_handles_empty_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    result = GetProjectBrainOverview(store).execute()

    # 空数据库应返回零计数，不崩溃
    assert result.total_memories == 0
    assert result.total_candidates == 0
    assert result.persona_ready is False
    assert len(result.layer_summaries) == 5
    # 所有计数都是 0
    for summary in result.layer_summaries:
        assert summary.count == 0
