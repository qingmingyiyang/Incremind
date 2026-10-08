"""项目大脑五层下钻 / 每日对话场景 / memory_delta 聚合。

在 project_brain_overview（平铺总览）之上，补充三个能力：

1. **层级下钻**：L4 Persona → L3/L2/L1 evidence → L0 source
   - `GetProjectBrainLayerDrill` 用例
   - 复用 RelatedMemoryService 的同源/同系列/同项目关联逻辑（简化版）

2. **每日对话 Scenario**：按 day_bucket（YYYY-MM-DD）聚合当天对话
   - `GetDailyConversationScenario` 用例
   - 把当天的 memory_atoms / memory_candidates / sources 聚合为一个 L2 scenario 视图
   - 不写入长期记忆，只读聚合

3. **memory_delta**：某次导入/提问/上传后产生的全部变更
   - `GetMemoryDelta` 用例
   - 按 import_batch_id 聚合：新增 L1 / 更新 L2 / 更新 L3 / 冲突 / 待确认 / 忽略 / 仅 L0
   - 回答"这次上传改变了什么"

设计原则：
- 只读，不修改任何记忆数据
- 内部 ID 默认隐藏，仅在 evidence_path 中可追溯
- 复用现有 ObjectStore 集合
- 不引入新的 collection
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .ports import ObjectStorePort


class ProjectBrainDrillError(ValueError):
    """Raised when drill-down / daily scenario / delta assembly fails."""


# ── 层级标签（与 project_brain_overview 对齐）──

_LAYER_LABELS = {
    "L0": "原始资料",
    "L1": "原子事实",
    "L2": "场景经验",
    "L3": "项目大脑",
    "L4": "稳定画像",
}

_ATOM_TYPE_LABEL = {
    "fact": "原子事实",
    "opinion": "观点",
    "quote": "引述",
    "decision": "决策",
    "action": "行动项",
    "question": "待澄清",
    "preference": "偏好",
    "other": "其他",
}


def _safe_str(value: object, default: str = "") -> str:
    return str(value) if value is not None else default


def _safe_list(store: ObjectStorePort, collection: str) -> list[Mapping[str, object]]:
    try:
        result = store.list(collection)
    except Exception:  # noqa: BLE001
        return []
    return [item for item in result if isinstance(item, Mapping)]


def _day_bucket_from(ts: str) -> str:
    """从 ISO 时间戳取 YYYY-MM-DD；空则返回空串。"""
    if not ts:
        return ""
    return ts[:10] if len(ts) >= 10 else ""


# ════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class DrillMemoryNode:
    """下钻路径中的一个记忆节点。"""

    memory_id: str
    layer: str  # L0 | L1 | L2 | L3 | L4
    category: str
    title: str
    summary: str
    confidence: float
    trust_status: str
    updated_at: str
    evidence_refs: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class DrillResult:
    """单条记忆的下钻结果：自身 + 子层关联。"""

    current: DrillMemoryNode
    children: tuple[DrillMemoryNode, ...]
    evidence_path: tuple[DrillMemoryNode, ...]  # 从当前节点到 L0 的完整路径


@dataclass(frozen=True, slots=True)
class DailyConversationScenario:
    """每日对话场景（按 day_bucket 聚合）。"""

    day_bucket: str
    title: str  # 例如 "2026-07-06 的对话"
    main_topics: tuple[str, ...]
    user_questions: tuple[str, ...]
    preference_signals: tuple[str, ...]
    advanced_series: tuple[str, ...]  # 当天推进的项目系列
    pending_candidates: tuple[Mapping[str, object], ...]
    atom_count: int
    source_count: int
    has_l3_promotion_candidate: bool  # 是否有稳定重复偏好可提升到 L3


@dataclass(frozen=True, slots=True)
class DeltaItem:
    """memory_delta 中的一条变更。"""

    change_type: str  # new_l1 | updated_l2 | updated_l3 | conflict | pending | ignored | l0_only
    layer: str
    title: str
    summary: str
    confidence: float
    changed_at: str
    evidence_refs: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class MemoryDelta:
    """一次导入/提问/上传后的全部变更。"""

    batch_id: str
    generated_at: str
    items: tuple[DeltaItem, ...]
    summary: Mapping[str, int]  # {new_l1, updated_l2, updated_l3, conflict, pending, ignored, l0_only}
    has_l3_impact: bool  # 是否影响 L3（更新或冲突）


# ════════════════════════════════════════════════════════════
# 用例 1：层级下钻
# ════════════════════════════════════════════════════════════


class GetProjectBrainLayerDrill:
    """按层级下钻：给定 layer + object_id，返回自身 + 子层关联 + 证据路径。

    下钻规则：
    - L3（series_memory）→ 同 series_id 的 L2 scenarios
    - L3（project_skill）→ required_context 引用的 L1 atoms
    - L4（persona）→ evidence_refs 引用的 L3/L2/L1 记忆
    - L2（scenario）→ 包含的 L1 atoms（通过 atom_ids 或 source_refs）
    - L1（atom）→ 同 source 的 L0 sources
    - L0 → 自身（叶子节点）
    """

    def __init__(self, object_store: ObjectStorePort, *, memory: object | None = None, skills: object | None = None) -> None:
        self._store = object_store
        self._memory = memory
        self._skills = skills

    def execute(self, *, layer: str, object_id: str) -> DrillResult:
        clean_layer = (layer or "").upper().strip()
        clean_id = (object_id or "").strip()
        if clean_layer not in ("L0", "L1", "L2", "L3", "L4"):
            raise ProjectBrainDrillError(f"unsupported layer: {layer}")
        if not clean_id:
            raise ProjectBrainDrillError("object_id is required")

        current = self._load_node(clean_layer, clean_id)
        if current is None:
            raise ProjectBrainDrillError(f"{clean_layer} object {clean_id} not found")

        children = self._collect_children(clean_layer, clean_id, current)
        evidence_path = self._build_evidence_path(clean_layer, clean_id, children)

        return DrillResult(
            current=current,
            children=children,
            evidence_path=evidence_path,
        )

    def _load_node(self, layer: str, object_id: str) -> DrillMemoryNode | None:
        """从对应 collection 读取单条记忆。L3 只跨 Series 与 Project Skill。"""
        if layer == "L4":
            return self._load_l4_node(object_id)
        if layer == "L3":
            l3_info = self._load_l3_node(object_id)
            return l3_info[0] if l3_info else None
        collection = self._layer_to_collection(layer)
        if not collection:
            return None
        record = self._read(collection, object_id)
        if not isinstance(record, Mapping):
            return None
        return self._to_node(layer, record)

    def _layer_to_collection(self, layer: str) -> str:
        """L3 需要根据 object_id 判断具体 collection，这里返回空让调用方处理。"""
        return {
            "L0": "sources",
            "L1": "memory_atoms",
            "L2": "memory_scenarios",
            # L3 有两个 collection，由 _load_l3_node 处理
        }.get(layer, "")

    def _to_node(self, layer: str, record: Mapping[str, object]) -> DrillMemoryNode:
        if layer == "L0":
            title = _safe_str(record.get("title")) or _safe_str(record.get("name")) or "原始资料"
            summary = _safe_str(record.get("summary")) or _safe_str(record.get("content"))[:200]
            category = "原始资料"
            confidence = 1.0
        elif layer == "L1":
            atom_type = _safe_str(record.get("atom_type"), "other")
            category = _ATOM_TYPE_LABEL.get(atom_type, "原子事实")
            title = _safe_str(record.get("content"))[:80] or "原子事实"
            summary = _safe_str(record.get("content"))
            confidence = float(record.get("confidence", 0.0) or 0.0)
        elif layer == "L2":
            category = "场景经验"
            title = _safe_str(record.get("title")) or "场景经验"
            summary = _safe_str(record.get("summary")) or title
            confidence = 1.0
        else:  # L3
            category = _safe_str(record.get("_brain_category"), "项目大脑")
            title = _safe_str(record.get("title")) or _safe_str(record.get("name")) or "项目大脑"
            summary = _safe_str(record.get("overview")) or _safe_str(record.get("purpose")) or title
            confidence = 1.0

        trust_status = _safe_str(record.get("trust_status"), "system_generated")
        updated_at = _safe_str(record.get("updated_at")) or _safe_str(record.get("created_at"))
        evidence_refs = tuple(record.get("source_refs", []) or [])
        return DrillMemoryNode(
            memory_id=_safe_str(record.get("id")),
            layer=layer,
            category=category,
            title=title,
            summary=summary,
            confidence=confidence,
            trust_status=trust_status,
            updated_at=updated_at,
            evidence_refs=evidence_refs,
        )

    def _load_l4_node(self, object_id: str) -> DrillMemoryNode | None:
        persona_id, separator, statement_id = object_id.partition("~")
        if not separator:
            persona_id = object_id
        for record in _safe_list(self._store, "memory_persona"):
            if _safe_str(record.get("id")) != persona_id:
                continue
            if not self._is_confirmed_persona(record):
                return None
            statements = record.get("statements", []) or []
            statement = next(
                (
                    value
                    for value in statements
                    if isinstance(value, Mapping)
                    and (
                        not statement_id
                        or _safe_str(value.get("id")) == statement_id
                    )
                ),
                None,
            )
            if not isinstance(statement, Mapping):
                return None
            content = _safe_str(statement.get("content")).strip()
            category = self._persona_category_label(
                _safe_str(statement.get("category"), "other")
            )
            confidence = statement.get("confidence", 1.0)
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                confidence = 1.0
            evidence_refs = tuple(
                dict(ref)
                for ref in (record.get("evidence_refs", []) or [])
                if isinstance(ref, Mapping)
            )
            return DrillMemoryNode(
                memory_id=object_id,
                layer="L4",
                category=category,
                title=content[:80] or category,
                summary=content,
                confidence=float(confidence),
                trust_status="user_confirmed",
                updated_at=_safe_str(record.get("updated_at"))
                or _safe_str(record.get("created_at")),
                evidence_refs=evidence_refs,
            )
        return None

    def _load_l3_node(self, object_id: str) -> tuple[DrillMemoryNode, Mapping[str, object], str] | None:
        """L3 只包含 Series Memory 与 Project Skill；Persona 由 L4 读取。"""
        for collection, category in (
            ("memory_series_memory", "项目系列"),
            ("project_skills", "项目能力"),
        ):
            record = self._read(collection, object_id)
            if isinstance(record, Mapping) and record.get("id"):
                node = self._to_node("L3", {**record, "_brain_category": category})
                return node, record, collection
        return None

    def _collect_children(self, layer: str, object_id: str, current: DrillMemoryNode) -> tuple[DrillMemoryNode, ...]:
        if layer == "L4":
            return self._l4_children(current)
        if layer == "L3":
            return self._l3_children(object_id, current)
        if layer == "L2":
            return self._l2_children(object_id)
        if layer == "L1":
            return self._l1_children(current)
        return ()  # L0 是叶子

    def _l4_children(self, current: DrillMemoryNode) -> tuple[DrillMemoryNode, ...]:
        children: list[DrillMemoryNode] = []
        collection_by_type = {
            "source": ("L0", "sources"),
            "atom": ("L1", "memory_atoms"),
            "scenario": ("L2", "memory_scenarios"),
            "series_memory": ("L3", "memory_series_memory"),
        }
        for ref in current.evidence_refs:
            if not isinstance(ref, Mapping):
                continue
            object_type = _safe_str(ref.get("object_type"))
            object_id = _safe_str(ref.get("object_id"))
            layer_collection = collection_by_type.get(object_type)
            if not object_id or layer_collection is None:
                continue
            layer, collection = layer_collection
            record = self._read(collection, object_id)
            if isinstance(record, Mapping):
                children.append(self._to_node(layer, record))
        return tuple(children[:20])

    def _l3_children(self, object_id: str, current: DrillMemoryNode) -> tuple[DrillMemoryNode, ...]:
        l3_info = self._load_l3_node(object_id)
        if l3_info is None:
            return ()
        _, record, collection = l3_info
        children: list[DrillMemoryNode] = []

        if collection == "memory_series_memory":
            series_id = _safe_str(record.get("series_id"))
            if series_id:
                for scn in self._list("memory_scenarios"):
                    if _safe_str(scn.get("series_id")) == series_id:
                        children.append(self._to_node("L2", scn))
        elif collection == "project_skills":
            required_context = record.get("required_context", []) or []
            for ctx in required_context:
                if isinstance(ctx, Mapping):
                    ref_id = _safe_str(ctx.get("object_id")) or _safe_str(ctx.get("id"))
                    if ref_id:
                        atom = self._read("memory_atoms", ref_id)
                        if isinstance(atom, Mapping):
                            children.append(self._to_node("L1", atom))
        return tuple(children[:20])

    def _l2_children(self, scenario_id: str) -> tuple[DrillMemoryNode, ...]:
        scn = self._read("memory_scenarios", scenario_id)
        if not isinstance(scn, Mapping):
            return ()
        atom_ids = scn.get("atom_ids", []) or []
        children: list[DrillMemoryNode] = []
        for aid in atom_ids:
            aid_str = _safe_str(aid)
            if aid_str:
                atom = self._read("memory_atoms", aid_str)
                if isinstance(atom, Mapping):
                    children.append(self._to_node("L1", atom))
        return tuple(children[:20])

    def _l1_children(self, current: DrillMemoryNode) -> tuple[DrillMemoryNode, ...]:
        """L1 atom 的子层是 L0 sources（通过 source_refs）。"""
        children: list[DrillMemoryNode] = []
        for ref in current.evidence_refs:
            if isinstance(ref, Mapping):
                source_id = _safe_str(ref.get("source_id")) or _safe_str(ref.get("object_id"))
                if source_id:
                    src = self._store.read("sources", source_id)
                    if isinstance(src, Mapping):
                        children.append(self._to_node("L0", src))
        return tuple(children[:20])

    def _read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        layer = {"memory_atoms": "atom", "memory_scenarios": "scenario", "memory_series_memory": "series_memory"}.get(collection)
        if layer is not None and self._memory is not None:
            return self._memory.get(layer, object_id)
        if collection == "project_skills" and self._skills is not None:
            return self._skills.get(object_id)
        return self._store.read(collection, object_id)

    def _list(self, collection: str) -> tuple[Mapping[str, object], ...]:
        layer = {"memory_atoms": "atom", "memory_scenarios": "scenario", "memory_series_memory": "series_memory"}.get(collection)
        if layer is not None and self._memory is not None:
            return tuple(self._memory.list(layer))
        if collection == "project_skills" and self._skills is not None:
            return tuple(self._skills.list_all())
        return tuple(_safe_list(self._store, collection))

    def _build_evidence_path(
        self, layer: str, object_id: str, children: tuple[DrillMemoryNode, ...]
    ) -> tuple[DrillMemoryNode, ...]:
        """构建从当前节点到 L0 的完整证据路径（递归下钻两层以确保触达 L0）。"""
        if layer == "L0":
            return ()
        if not children:
            return ()
        # 递归下钻：把子层 + 子层的子层都加入路径，直到 L0
        path: list[DrillMemoryNode] = list(children)
        for child in children:
            if child.layer == "L0":
                continue
            sub = self._collect_children(child.layer, child.memory_id, child)
            for sub_node in sub:
                path.append(sub_node)
                # 再下钻一层到 L0
                if sub_node.layer != "L0":
                    sub_sub = self._collect_children(sub_node.layer, sub_node.memory_id, sub_node)
                    for l0_node in sub_sub:
                        if l0_node.layer == "L0":
                            path.append(l0_node)
        return tuple(path)

    @staticmethod
    def _is_confirmed_persona(record: Mapping[str, object]) -> bool:
        confirmation = record.get("confirmation")
        return (
            isinstance(confirmation, Mapping)
            and confirmation.get("status") == "confirmed"
            and record.get("trust_status") == "user_confirmed"
        )

    @staticmethod
    def _persona_category_label(category: str) -> str:
        return {
            "preference": "稳定偏好",
            "constraint": "稳定约束",
            "identity": "稳定事实",
            "workflow": "工作方式",
            "style": "表达风格",
            "other": "其他稳定记忆",
        }.get(category, category or "其他稳定记忆")


# ════════════════════════════════════════════════════════════
# 用例 2：每日对话 Scenario
# ════════════════════════════════════════════════════════════


class GetDailyConversationScenario:
    """按 day_bucket 聚合当天对话为 L2 scenario 视图。

    不写入长期记忆，只读聚合。当天产生的 atoms + candidates + sources
    被聚合成一个"每日对话场景"，主要主题/用户问题/偏好信号从中提取。

    只有稳定、重复、有证据的偏好才会作为 L3 候选提示，不直接污染 L3。
    """

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._store = object_store

    def execute(self, *, day_bucket: str) -> DailyConversationScenario:
        clean_day = (day_bucket or "").strip()
        if not clean_day or len(clean_day) != 10:
            raise ProjectBrainDrillError(f"invalid day_bucket: {day_bucket}")

        # 收集当天的 atoms
        atoms = _safe_list(self._store, "memory_atoms")
        day_atoms = [
            a for a in atoms
            if _day_bucket_from(_safe_str(a.get("created_at")) or _safe_str(a.get("updated_at"))) == clean_day
        ]

        # 收集当天的 candidates
        candidates = _safe_list(self._store, "memory_candidates")
        day_candidates = [
            c for c in candidates
            if _day_bucket_from(_safe_str(c.get("created_at"))) == clean_day
        ]

        # 收集当天的 sources
        sources = _safe_list(self._store, "sources")
        day_sources = [
            s for s in sources
            if _day_bucket_from(_safe_str(s.get("created_at")) or _safe_str(s.get("imported_at"))) == clean_day
        ]

        # 提取主要主题（从 atom content 关键词）
        main_topics = self._extract_topics(day_atoms)

        # 提取用户问题（atom_type == question）
        user_questions = tuple(
            _safe_str(a.get("content"))[:100]
            for a in day_atoms
            if _safe_str(a.get("atom_type")) == "question"
        )[:10]

        # 提取偏好信号（atom_type == preference）
        preference_signals = tuple(
            _safe_str(a.get("content"))[:100]
            for a in day_atoms
            if _safe_str(a.get("atom_type")) == "preference"
        )[:10]

        # 当天推进的项目系列（从 candidates 的 series_id 或 atoms 的 series_id）
        advanced_series = tuple(
            _safe_str(c.get("series_id"))
            for c in day_candidates
            if _safe_str(c.get("series_id"))
        )
        # 去重
        advanced_series = tuple(dict.fromkeys(advanced_series))[:5]

        # 待确认候选
        pending = tuple(
            {
                "candidate_id": _safe_str(c.get("id")),
                "target_layer": _safe_str(c.get("target_layer")),
                "summary": _safe_str(c.get("proposed_content", {}).get("summary", ""))[:100] if isinstance(c.get("proposed_content"), Mapping) else _safe_str(c.get("summary"))[:100],
                "confidence": float(c.get("series_confidence", 0.0) or 0.0),
            }
            for c in day_candidates
            if _safe_str(c.get("status")) == "pending_review"
        )[:20]

        # 判断是否有稳定重复偏好可提升到 L3
        # 简单规则：同 series_id 出现 >= 2 次的 preference atom
        has_l3_promotion_candidate = self._has_stable_preference(day_atoms)

        return DailyConversationScenario(
            day_bucket=clean_day,
            title=f"{clean_day} 的对话",
            main_topics=main_topics,
            user_questions=user_questions,
            preference_signals=preference_signals,
            advanced_series=advanced_series,
            pending_candidates=pending,
            atom_count=len(day_atoms),
            source_count=len(day_sources),
            has_l3_promotion_candidate=has_l3_promotion_candidate,
        )

    def _extract_topics(self, atoms: list[Mapping[str, object]]) -> tuple[str, ...]:
        """从 atoms 中提取主要主题（简单分词取高频词，不做 NLP）。"""
        word_freq: dict[str, int] = {}
        for atom in atoms:
            content = _safe_str(atom.get("content"))
            # 简单按标点切分，取 4-12 字的片段
            import re
            segments = re.findall(r"[\u4e00-\u9fa5a-zA-Z]{4,12}", content)
            for seg in segments:
                word_freq[seg] = word_freq.get(seg, 0) + 1
        # 取前 5 个高频词
        sorted_topics = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:5]
        return tuple(word for word, _ in sorted_topics)

    def _has_stable_preference(self, atoms: list[Mapping[str, object]]) -> bool:
        """判断是否有稳定重复偏好：同 series_id 的 preference atom >= 2 次。"""
        pref_by_series: dict[str, int] = {}
        for atom in atoms:
            if _safe_str(atom.get("atom_type")) == "preference":
                series_id = _safe_str(atom.get("series_id"), "default")
                pref_by_series[series_id] = pref_by_series.get(series_id, 0) + 1
        return any(count >= 2 for count in pref_by_series.values())


# ════════════════════════════════════════════════════════════
# 用例 3：memory_delta
# ════════════════════════════════════════════════════════════


class GetMemoryDelta:
    """按 import_batch_id 聚合一次导入/提问/上传后的全部变更。

    变更类型：
    - new_l1：新增 L1 atom（created_at 在 batch 之后）
    - updated_l2：更新 L2 scenario
    - updated_l3：更新 L3 series_memory / project_skill / persona
    - conflict：冲突候选
    - pending：待确认候选
    - ignored：被忽略的候选
    - l0_only：仅保存为 L0 的内容（未生成候选的 source）
    """

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._store = object_store

    def execute(self, *, batch_id: str) -> MemoryDelta:
        clean_batch = (batch_id or "").strip()
        if not clean_batch:
            raise ProjectBrainDrillError("batch_id is required")

        # 读取 batch 记录
        batch = self._store.read("memory_import_batches", clean_batch)
        if not isinstance(batch, Mapping):
            raise ProjectBrainDrillError(f"batch {batch_id} not found")

        batch_created = _safe_str(batch.get("created_at"))
        batch_day = _day_bucket_from(batch_created)

        items: list[DeltaItem] = []

        # 1. 新增 L1 atoms（import_batch_id 匹配 或 created_at 在 batch 之后当天）
        for atom in _safe_list(self._store, "memory_atoms"):
            atom_batch = _safe_str(atom.get("import_batch_id"))
            atom_day = _day_bucket_from(_safe_str(atom.get("created_at")))
            if atom_batch == clean_batch or (batch_day and atom_day == batch_day):
                items.append(DeltaItem(
                    change_type="new_l1",
                    layer="L1",
                    title=_safe_str(atom.get("content"))[:80] or "新增原子事实",
                    summary=_safe_str(atom.get("content")),
                    confidence=float(atom.get("confidence", 0.0) or 0.0),
                    changed_at=_safe_str(atom.get("created_at")),
                    evidence_refs=tuple(atom.get("source_refs", []) or []),
                ))

        # 2. 更新 L2 scenarios（同 batch 关联）
        for scn in _safe_list(self._store, "memory_scenarios"):
            scn_batch = _safe_str(scn.get("import_batch_id"))
            scn_day = _day_bucket_from(_safe_str(scn.get("updated_at")) or _safe_str(scn.get("created_at")))
            if scn_batch == clean_batch or (batch_day and scn_day == batch_day):
                items.append(DeltaItem(
                    change_type="updated_l2",
                    layer="L2",
                    title=_safe_str(scn.get("title")) or "更新场景",
                    summary=_safe_str(scn.get("summary")) or "",
                    confidence=1.0,
                    changed_at=_safe_str(scn.get("updated_at")) or _safe_str(scn.get("created_at")),
                    evidence_refs=tuple(scn.get("source_refs", []) or []),
                ))

        # 3. 更新 L3（series_memory / project_skills）
        for collection, category_label in (
            ("memory_series_memory", "项目系列"),
            ("project_skills", "项目能力"),
        ):
            for rec in _safe_list(self._store, collection):
                rec_day = _day_bucket_from(_safe_str(rec.get("updated_at")) or _safe_str(rec.get("created_at")))
                if batch_day and rec_day == batch_day:
                    items.append(DeltaItem(
                        change_type="updated_l3",
                        layer="L3",
                        title=f"{category_label} · {_safe_str(rec.get('series_id')) or _safe_str(rec.get('name'))}",
                        summary=_safe_str(rec.get("overview")) or _safe_str(rec.get("purpose")) or "",
                        confidence=1.0,
                        changed_at=_safe_str(rec.get("updated_at")) or _safe_str(rec.get("created_at")),
                        evidence_refs=tuple(rec.get("source_refs", []) or []),
                    ))

        # 4-6. 候选：conflict / pending / ignored
        for cand in _safe_list(self._store, "memory_candidates"):
            cand_batch = _safe_str(cand.get("import_batch_id"))
            if cand_batch != clean_batch:
                continue
            status = _safe_str(cand.get("status"))
            if status == "rejected":
                change_type = "ignored"
            elif status == "conflict":
                change_type = "conflict"
            else:  # pending_review 或其他
                change_type = "pending"

            proposed = cand.get("proposed_content")
            summary = ""
            if isinstance(proposed, Mapping):
                summary = _safe_str(proposed.get("summary")) or _safe_str(proposed.get("content"))[:200]
            else:
                summary = _safe_str(cand.get("summary"))[:200]

            items.append(DeltaItem(
                change_type=change_type,
                layer=f"target:{_safe_str(cand.get('target_layer'))}",
                title=summary[:80] or f"{change_type} 候选",
                summary=summary,
                confidence=float(cand.get("series_confidence", 0.0) or 0.0),
                changed_at=_safe_str(cand.get("created_at")),
                evidence_refs=tuple(cand.get("source_refs", []) or []),
            ))

        # 7. l0_only：batch 中未生成候选的 source
        candidate_source_ids = {
            _safe_str(c.get("source_refs", [{}])[0].get("source_id")) if isinstance(c.get("source_refs"), list) and c.get("source_refs") else ""
            for c in _safe_list(self._store, "memory_candidates")
            if _safe_str(c.get("import_batch_id")) == clean_batch
        }
        for src in _safe_list(self._store, "sources"):
            src_batch = _safe_str(src.get("import_batch_id"))
            if src_batch != clean_batch:
                continue
            src_id = _safe_str(src.get("id"))
            if src_id and src_id not in candidate_source_ids:
                items.append(DeltaItem(
                    change_type="l0_only",
                    layer="L0",
                    title=_safe_str(src.get("title")) or _safe_str(src.get("name")) or "原始资料",
                    summary=_safe_str(src.get("summary")) or _safe_str(src.get("content"))[:200],
                    confidence=0.0,
                    changed_at=_safe_str(src.get("created_at")),
                    evidence_refs=(),
                ))

        # 汇总
        summary: dict[str, int] = {
            "new_l1": 0, "updated_l2": 0, "updated_l3": 0,
            "conflict": 0, "pending": 0, "ignored": 0, "l0_only": 0,
        }
        for item in items:
            summary[item.change_type] = summary.get(item.change_type, 0) + 1

        has_l3_impact = summary["updated_l3"] > 0 or summary["conflict"] > 0

        # 按时间倒序
        items.sort(key=lambda x: x.changed_at, reverse=True)

        return MemoryDelta(
            batch_id=clean_batch,
            generated_at=batch_created,
            items=tuple(items),
            summary=summary,
            has_l3_impact=has_l3_impact,
        )


# ════════════════════════════════════════════════════════════
# 序列化
# ════════════════════════════════════════════════════════════


def serialize_drill_memory_node(node: DrillMemoryNode) -> dict[str, object]:
    return {
        "memory_id": node.memory_id,
        "layer": node.layer,
        "layer_label": _LAYER_LABELS.get(node.layer, node.layer),
        "category": node.category,
        "title": node.title,
        "summary": node.summary,
        "confidence": node.confidence,
        "trust_status": node.trust_status,
        "updated_at": node.updated_at,
        "evidence_refs": tuple(node.evidence_refs),
    }


def serialize_drill_result(result: DrillResult) -> dict[str, object]:
    return {
        "current": serialize_drill_memory_node(result.current),
        "children": tuple(serialize_drill_memory_node(n) for n in result.children),
        "evidence_path": tuple(serialize_drill_memory_node(n) for n in result.evidence_path),
    }


def serialize_daily_conversation_scenario(scn: DailyConversationScenario) -> dict[str, object]:
    return {
        "day_bucket": scn.day_bucket,
        "title": scn.title,
        "main_topics": tuple(scn.main_topics),
        "user_questions": tuple(scn.user_questions),
        "preference_signals": tuple(scn.preference_signals),
        "advanced_series": tuple(scn.advanced_series),
        "pending_candidates": tuple(scn.pending_candidates),
        "atom_count": scn.atom_count,
        "source_count": scn.source_count,
        "has_l3_promotion_candidate": scn.has_l3_promotion_candidate,
    }


def serialize_delta_item(item: DeltaItem) -> dict[str, object]:
    return {
        "change_type": item.change_type,
        "layer": item.layer,
        "title": item.title,
        "summary": item.summary,
        "confidence": item.confidence,
        "changed_at": item.changed_at,
        "evidence_refs": tuple(item.evidence_refs),
    }


def serialize_memory_delta(delta: MemoryDelta) -> dict[str, object]:
    return {
        "batch_id": delta.batch_id,
        "generated_at": delta.generated_at,
        "items": tuple(serialize_delta_item(i) for i in delta.items),
        "summary": dict(delta.summary),
        "has_l3_impact": delta.has_l3_impact,
    }
