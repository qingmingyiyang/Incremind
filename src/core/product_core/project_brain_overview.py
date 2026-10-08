"""阶段 5：项目大脑 / 记忆墙聚合用例。

把分散在 L0/L1/L2/L3/L4 的记忆数据聚合成一个用户可理解的视图，回答三个问题：
1. AI 现在知道什么？（按层级和类别展示已发布的记忆）
2. 这些记忆从哪里来？（每条记忆可回溯到证据来源）
3. 这次上传改变了什么？（最近变更：新增 / 更新 / 冲突 / 待确认）

设计原则：
- 只读，不修改任何记忆数据
- 不暴露内部 ID 作为主视觉，但可在高级详情中显示
- 复用现有 ObjectStore 集合，不引入新的存储
- 本地路径 / secret / cookie 脱敏
- 默认隐藏技术细节，让用户看到「记忆」而不是「数据库记录」
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .ports import ObjectStorePort


class ProjectBrainOverviewError(ValueError):
    """Raised when Project Brain overview cannot be assembled safely."""


# ── 数据结构 ──


@dataclass(frozen=True, slots=True)
class BrainMemoryItem:
    """一条记忆条目（来自任意层级）。"""

    memory_id: str
    layer: str  # L0 | L1 | L2 | L3 | L4
    category: str  # 用户偏好 / 项目事实 / 项目规则 / 关键人物 / 长期目标 / 项目能力 / Persona / 项目系列 / 原始资料
    title: str
    summary: str
    tags: tuple[str, ...]
    confidence: float
    trust_status: str
    updated_at: str
    evidence_refs: tuple[Mapping[str, object], ...]
    related_source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrainCandidateItem:
    """待确认的记忆候选。"""

    candidate_id: str
    target_layer: str
    candidate_type: str
    status: str
    title: str
    summary: str
    created_at: str
    source_refs: tuple[Mapping[str, object], ...]
    memory_publication_state: str
    series_confidence: float | None
    expert_proposal_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrainChangeItem:
    """最近变更（新增 / 更新 / 冲突 / 待确认 / 被忽略）。"""

    change_type: str  # new | updated | conflict | pending | ignored
    layer: str
    memory_id: str
    title: str
    summary: str
    changed_at: str
    evidence_refs: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class BrainLayerSummary:
    """单层概要。"""

    layer: str
    label: str
    count: int
    description: str


@dataclass(frozen=True, slots=True)
class ProjectBrainOverview:
    """项目大脑总览。"""

    scope: str
    layer_summaries: tuple[BrainLayerSummary, ...]
    memories: tuple[BrainMemoryItem, ...]
    candidates: tuple[BrainCandidateItem, ...]
    recent_changes: tuple[BrainChangeItem, ...]
    persona_ready: bool
    persona_revision: int
    total_memories: int
    total_candidates: int
    total_changes: int
    last_updated_at: str | None
    retrieval_status: Mapping[str, object] | None = None


# ── 层级标签 ──

_LAYER_LABELS = {
    "L0": "原始资料",
    "L1": "原子事实",
    "L2": "场景经验",
    "L3": "项目大脑",
    "L4": "稳定画像",
}

_LAYER_DESCRIPTIONS = {
    "L0": "你保存的原始资料：链接、文件、音频、视频、笔记。",
    "L1": "AI 从资料中抽取的原子事实和决策。",
    "L2": "AI 整理出的场景和任务经验。",
    "L3": "系列详细摘要、项目系列记忆和可复用项目方法。",
    "L4": "经你确认的稳定事实、偏好、规则和约束。",
}

# 原子类型 → 用户可理解类别
_ATOM_TYPE_CATEGORY = {
    "fact": "项目事实",
    "opinion": "项目观点",
    "quote": "关键引述",
    "decision": "项目决策",
    "action": "行动项",
    "question": "待澄清问题",
    "preference": "用户偏好",
    "other": "其他事实",
}

_CANDIDATE_TYPE_LABEL = {
    "answer_fact": "回答中的事实",
    "answer_decision": "回答中的决策",
    "answer_action": "回答中的行动",
    "answer_summary": "回答摘要",
    "document_takeaway": "文档要点",
    "other": "其他候选",
}


# ── 主用例 ──


class GetProjectBrainOverview:
    """聚合所有层级的记忆数据，返回项目大脑总览。"""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        memory: object | None = None,
        skills: object | None = None,
        namespace_id: str = "default",
        now: str = "2026-07-06T22:00:00+08:00",
        max_memories: int = 200,
        max_changes: int = 50,
        retrieval_status: Mapping[str, object] | None = None,
    ) -> None:
        self._object_store = object_store
        self._memory = memory
        self._skills = skills
        self._namespace_id = namespace_id
        self._now = now
        self._max_memories = max_memories
        self._max_changes = max_changes
        self._retrieval_status = (
            dict(retrieval_status)
            if isinstance(retrieval_status, Mapping)
            else None
        )

    def execute(
        self, *, scope: str = "global", project_id: str | None = None,
    ) -> ProjectBrainOverview:
        clean_scope = (scope or "global").strip() or "global"
        clean_project_id = (project_id or "").strip() or None
        project_source_ids = self._project_source_ids(clean_project_id)
        project_atom_ids = self._project_atom_ids(clean_project_id, project_source_ids)

        # 收集各层记忆
        memories: list[BrainMemoryItem] = []
        layer_counts: dict[str, int] = {
            "L0": 0,
            "L1": 0,
            "L2": 0,
            "L3": 0,
            "L4": 0,
        }

        # L0：原始资料（sources）
        l0_items = self._collect_l0_sources(clean_project_id)
        memories.extend(l0_items)
        layer_counts["L0"] = len(l0_items)

        # L1：原子事实（memory_atoms）
        l1_items = self._collect_l1_atoms(clean_project_id, project_source_ids, project_atom_ids)
        memories.extend(l1_items)
        layer_counts["L1"] = len(l1_items)

        # L2：场景经验（memory_scenarios）
        l2_items = self._collect_l2_scenarios(clean_project_id, project_source_ids)
        memories.extend(l2_items)
        layer_counts["L2"] = len(l2_items)

        # L3：系列记忆 / 项目能力
        l3_items = self._collect_l3_memories(clean_project_id, project_source_ids)
        memories.extend(l3_items)
        layer_counts["L3"] = len(l3_items)

        # L4：仅已确认的 Persona current
        l4_items = self._collect_l4_persona(clean_scope)
        memories.extend(l4_items)
        layer_counts["L4"] = len(l4_items)

        # 限制总条目数，按 updated_at 倒序
        memories.sort(key=lambda m: m.updated_at or "", reverse=True)
        truncated_memories = tuple(memories[: self._max_memories])

        # 候选记忆
        candidates = self._collect_candidates(clean_project_id, project_source_ids)

        # 最近变更
        recent_changes = self._collect_recent_changes(clean_project_id, project_source_ids)

        # Persona 状态
        persona_ready, persona_revision = self._persona_status(clean_scope)

        # 层级概要
        layer_summaries = tuple(
            BrainLayerSummary(
                layer=layer,
                label=_LAYER_LABELS[layer],
                count=layer_counts[layer],
                description=_LAYER_DESCRIPTIONS[layer],
            )
            for layer in ("L0", "L1", "L2", "L3", "L4")
        )

        last_updated = max(
            (m.updated_at for m in truncated_memories if m.updated_at),
            default=None,
        )

        return ProjectBrainOverview(
            scope=clean_scope,
            layer_summaries=layer_summaries,
            memories=truncated_memories,
            candidates=candidates,
            recent_changes=recent_changes,
            persona_ready=persona_ready,
            persona_revision=persona_revision,
            total_memories=len(memories),
            total_candidates=len(candidates),
            total_changes=len(recent_changes),
            last_updated_at=last_updated,
            retrieval_status=self._retrieval_status,
        )

    # ── 各层收集 ──

    def _collect_l0_sources(self, project_id: str | None = None) -> list[BrainMemoryItem]:
        sources = self._safe_list("sources")
        items: list[BrainMemoryItem] = []
        for src in sources:
            if not isinstance(src, Mapping):
                continue
            if not self._record_matches_project(src, project_id):
                continue
            src_id = str(src.get("id", ""))
            if not src_id:
                continue
            title = str(src.get("title", "")) or "未命名资料"
            media_type = str(src.get("media_type", "")) or "未知"
            summary = f"{media_type} · {src.get('processing_state', 'captured')}"
            tags = tuple(self._extract_tags(src.get("metadata")))
            confidence = 1.0
            trust_status = str(src.get("trust_status", "trusted"))
            updated_at = str(src.get("updated_at") or src.get("created_at", ""))
            evidence_refs = (
                {"object_type": "source", "object_id": src_id, "source_refs": ()},
            )
            items.append(BrainMemoryItem(
                memory_id=src_id,
                layer="L0",
                category="原始资料",
                title=title,
                summary=summary,
                tags=tags,
                confidence=confidence,
                trust_status=trust_status,
                updated_at=updated_at,
                evidence_refs=evidence_refs,
                related_source_ids=(src_id,),
            ))
        return items

    def _collect_l1_atoms(
        self,
        project_id: str | None = None,
        project_source_ids: set[str] | None = None,
        project_atom_ids: set[str] | None = None,
    ) -> list[BrainMemoryItem]:
        atoms = self._safe_list("memory_atoms")
        items: list[BrainMemoryItem] = []
        for atom in atoms:
            if not isinstance(atom, Mapping):
                continue
            atom_id = str(atom.get("id", ""))
            if not self._record_matches_project(atom, project_id, project_source_ids) \
                and atom_id not in (project_atom_ids or set()):
                continue
            if not atom_id:
                continue
            content = str(atom.get("content", ""))
            atom_type = str(atom.get("atom_type", "other"))
            category = _ATOM_TYPE_CATEGORY.get(atom_type, "其他事实")
            title = self._truncate(content, 60) or "原子事实"
            summary = content
            tags = tuple(atom.get("tags", []) or [])
            confidence = float(atom.get("confidence", 0.8) or 0.8)
            trust_status = str(atom.get("trust_status", "system_generated"))
            updated_at = str(atom.get("updated_at") or atom.get("created_at", ""))
            source_refs = tuple(atom.get("source_refs", []) or [])
            source_ids = tuple(
                str(ref.get("source_id", "")) for ref in source_refs
                if isinstance(ref, Mapping) and ref.get("source_id")
            )
            evidence_refs = (
                {"object_type": "atom", "object_id": atom_id, "source_refs": source_refs},
            )
            items.append(BrainMemoryItem(
                memory_id=atom_id,
                layer="L1",
                category=category,
                title=title,
                summary=summary,
                tags=tags,
                confidence=confidence,
                trust_status=trust_status,
                updated_at=updated_at,
                evidence_refs=evidence_refs,
                related_source_ids=source_ids,
            ))
        return items

    def _collect_l2_scenarios(
        self, project_id: str | None = None, project_source_ids: set[str] | None = None,
    ) -> list[BrainMemoryItem]:
        scenarios = self._safe_list("memory_scenarios")
        items: list[BrainMemoryItem] = []
        for scn in scenarios:
            if not isinstance(scn, Mapping):
                continue
            if not self._record_matches_project(scn, project_id, project_source_ids):
                continue
            scn_id = str(scn.get("id", ""))
            if not scn_id:
                continue
            title = str(scn.get("title", "")) or "场景经验"
            summary = str(scn.get("summary", "")) or title
            tags = tuple(scn.get("tags", []) or [])
            confidence = 1.0
            trust_status = str(scn.get("trust_status", "system_generated"))
            updated_at = str(scn.get("updated_at") or scn.get("created_at", ""))
            source_refs = tuple(scn.get("source_refs", []) or [])
            source_ids = tuple(
                str(ref.get("source_id", "")) for ref in source_refs
                if isinstance(ref, Mapping) and ref.get("source_id")
            )
            evidence_refs = (
                {"object_type": "scenario", "object_id": scn_id, "source_refs": source_refs},
            )
            items.append(BrainMemoryItem(
                memory_id=scn_id,
                layer="L2",
                category="场景经验",
                title=title,
                summary=summary,
                tags=tags,
                confidence=confidence,
                trust_status=trust_status,
                updated_at=updated_at,
                evidence_refs=evidence_refs,
                related_source_ids=source_ids,
            ))
        return items

    def _collect_l3_memories(
        self, project_id: str | None = None, project_source_ids: set[str] | None = None,
    ) -> list[BrainMemoryItem]:
        items: list[BrainMemoryItem] = []
        # 系列记忆
        series_memories = self._safe_list("memory_series_memory")
        for sm in series_memories:
            if not isinstance(sm, Mapping):
                continue
            if not self._record_matches_project(sm, project_id, project_source_ids):
                continue
            sm_id = str(sm.get("id", ""))
            if not sm_id:
                continue
            series_id = str(sm.get("series_id", "")) or "默认系列"
            title = f"系列记忆 · {series_id}"
            summary = str(sm.get("overview", "")) or title
            tags = tuple(sm.get("tags", []) or [])
            trust_status = str(sm.get("trust_status", "system_generated"))
            updated_at = str(sm.get("updated_at") or sm.get("created_at", ""))
            source_refs = tuple(sm.get("source_refs", []) or [])
            evidence_refs = (
                {"object_type": "series_memory", "object_id": sm_id, "source_refs": source_refs},
            )
            items.append(BrainMemoryItem(
                memory_id=sm_id,
                layer="L3",
                category="项目系列",
                title=title,
                summary=summary,
                tags=tags,
                confidence=1.0,
                trust_status=trust_status,
                updated_at=updated_at,
                evidence_refs=evidence_refs,
                related_source_ids=tuple(
                    str(ref.get("source_id", "")) for ref in source_refs
                    if isinstance(ref, Mapping) and ref.get("source_id")
                ),
            ))
        # 项目能力
        skills = self._safe_list("project_skills")
        for skill in skills:
            if not isinstance(skill, Mapping):
                continue
            if not self._record_matches_project(skill, project_id, project_source_ids):
                continue
            skill_id = str(skill.get("id", ""))
            if not skill_id:
                continue
            name = str(skill.get("name", "")) or "项目能力"
            purpose = str(skill.get("purpose", "")) or name
            tags = tuple(skill.get("tags", []) or [])
            trust_status = str(skill.get("trust_status", "system_generated"))
            updated_at = str(skill.get("updated_at") or skill.get("created_at", ""))
            source_refs = tuple(skill.get("source_refs", []) or [])
            evidence_refs = (
                {"object_type": "project_skill", "object_id": skill_id, "source_refs": source_refs},
            )
            items.append(BrainMemoryItem(
                memory_id=skill_id,
                layer="L3",
                category="项目能力",
                title=name,
                summary=purpose,
                tags=tags,
                confidence=1.0,
                trust_status=trust_status,
                updated_at=updated_at,
                evidence_refs=evidence_refs,
                related_source_ids=tuple(
                    str(ref.get("source_id", "")) for ref in source_refs
                    if isinstance(ref, Mapping) and ref.get("source_id")
                ),
            ))
        return items

    def _collect_l4_persona(self, scope: str) -> list[BrainMemoryItem]:
        persona = self._confirmed_persona(scope)
        if persona is None:
            return []
        persona_id = str(persona.get("id", ""))
        statements = persona.get("statements", []) or []
        evidence_refs = tuple(
            dict(ref)
            for ref in (persona.get("evidence_refs", []) or [])
            if isinstance(ref, Mapping)
        )
        source_ids = tuple(
            str(source_ref.get("source_id"))
            for ref in evidence_refs
            for source_ref in (ref.get("source_refs", []) or [])
            if isinstance(source_ref, Mapping) and source_ref.get("source_id")
        )
        items: list[BrainMemoryItem] = []
        for index, statement in enumerate(statements):
            if not isinstance(statement, Mapping):
                continue
            content = str(statement.get("content", "")).strip()
            if not content:
                continue
            category = str(statement.get("category", "other"))
            statement_id = str(statement.get("id", "")).strip() or f"statement-{index + 1}"
            confidence = statement.get("confidence", 1.0)
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                confidence = 1.0
            items.append(
                BrainMemoryItem(
                    memory_id=f"{persona_id}~{statement_id}",
                    layer="L4",
                    category=self._persona_category_label(category),
                    title=self._truncate(content, 60),
                    summary=content,
                    tags=(),
                    confidence=float(confidence),
                    trust_status="user_confirmed",
                    updated_at=str(
                        persona.get("updated_at") or persona.get("created_at", "")
                    ),
                    evidence_refs=evidence_refs,
                    related_source_ids=tuple(dict.fromkeys(source_ids)),
                )
            )
        return items

    def _collect_candidates(
        self, project_id: str | None = None, project_source_ids: set[str] | None = None,
    ) -> tuple[BrainCandidateItem, ...]:
        candidates = self._safe_list("memory_candidates")
        items: list[BrainCandidateItem] = []
        for cand in candidates:
            if not isinstance(cand, Mapping):
                continue
            if not self._record_matches_project(cand, project_id, project_source_ids):
                continue
            cand_id = str(cand.get("id", ""))
            if not cand_id:
                continue
            status = str(cand.get("status", "pending_review"))
            # 只展示待确认和已拒绝（被忽略）的候选，已提升的不再显示
            if status not in ("pending_review", "rejected", "withdrawn"):
                continue
            target_layer = str(cand.get("target_layer", "atom"))
            candidate_type = str(cand.get("candidate_type", "other"))
            type_label = _CANDIDATE_TYPE_LABEL.get(candidate_type, "其他候选")
            proposed = cand.get("proposed_content", {})
            if isinstance(proposed, Mapping):
                title = str(proposed.get("title", "")) or type_label
                summary = str(proposed.get("summary", "")) or title
            else:
                title = type_label
                summary = str(proposed or "")
            source_refs = tuple(cand.get("source_refs", []) or [])
            memory_publication_state = str(cand.get("memory_publication_state", "not_started"))
            created_at = str(cand.get("created_at", ""))
            series_confidence = cand.get("series_confidence")
            if not isinstance(series_confidence, (int, float)):
                series_confidence = None
            provenance = cand.get("provenance")
            proposal_id = (
                provenance.get("external_agent_proposal_id")
                if isinstance(provenance, Mapping)
                else None
            )
            expert_proposal_id = (
                proposal_id
                if isinstance(proposal_id, str) and proposal_id.startswith("expert-")
                else None
            )
            items.append(BrainCandidateItem(
                candidate_id=cand_id,
                target_layer=target_layer,
                candidate_type=candidate_type,
                status=status,
                title=title,
                summary=summary,
                created_at=created_at,
                source_refs=source_refs,
                memory_publication_state=memory_publication_state,
                series_confidence=series_confidence,
                expert_proposal_id=expert_proposal_id,
            ))
        # 待确认优先
        items.sort(key=lambda c: (0 if c.status == "pending_review" else 1, c.created_at), reverse=True)
        return tuple(items)

    def _collect_recent_changes(
        self, project_id: str | None = None, project_source_ids: set[str] | None = None,
    ) -> tuple[BrainChangeItem, ...]:
        """从 memory_transitions 收集最近变更。"""
        transitions = self._safe_list("memory_transitions")
        items: list[BrainChangeItem] = []
        for trans in transitions:
            if not isinstance(trans, Mapping):
                continue
            if not self._record_matches_project(trans, project_id, project_source_ids):
                continue
            trans_id = str(trans.get("id", ""))
            if not trans_id:
                continue
            transition_type = str(trans.get("transition_type", ""))
            object_type = trans.get("object_type")
            layer = (
                object_type
                if isinstance(object_type, str) and object_type
                else str(trans.get("layer", ""))
            )
            memory_id = str(trans.get("object_id", ""))
            changed_at = str(trans.get("created_at", ""))
            evidence_refs = tuple(trans.get("evidence_refs", []) or [])
            # 映射 transition_type → change_type
            if transition_type == "confirm":
                change_type = "new"
                title = "新增长期记忆"
                summary = "用户确认了一条长期记忆。"
            elif transition_type == "demote":
                change_type = "ignored"
                title = "回滚长期记忆"
                summary = "用户回滚了一条长期记忆。"
            elif transition_type == "update":
                change_type = "updated"
                title = "更新记忆"
                summary = "记忆被更新。"
            elif transition_type == "conflict":
                change_type = "conflict"
                title = "检测到冲突"
                summary = "新证据与已有记忆冲突。"
            else:
                change_type = "updated"
                title = "记忆变更"
                summary = transition_type or "变更"
            items.append(BrainChangeItem(
                change_type=change_type,
                layer=layer,
                memory_id=memory_id,
                title=title,
                summary=summary,
                changed_at=changed_at,
                evidence_refs=evidence_refs,
            ))
        # 候选记忆作为 pending 变更
        candidates = self._safe_list("memory_candidates")
        for cand in candidates:
            if not isinstance(cand, Mapping):
                continue
            if not self._record_matches_project(cand, project_id, project_source_ids):
                continue
            if str(cand.get("status", "")) != "pending_review":
                continue
            cand_id = str(cand.get("id", ""))
            created_at = str(cand.get("created_at", ""))
            target_layer = str(cand.get("target_layer", "atom"))
            items.append(BrainChangeItem(
                change_type="pending",
                layer=target_layer,
                memory_id=cand_id,
                title="待确认记忆候选",
                summary="AI 生成了新的记忆候选，等待你的确认。",
                changed_at=created_at,
                evidence_refs=(),
            ))
        items.sort(key=lambda c: c.changed_at, reverse=True)
        return tuple(items[: self._max_changes])

    # ── 辅助方法 ──

    def _persona_status(self, scope: str) -> tuple[bool, int]:
        persona = self._confirmed_persona(scope)
        if persona is None:
            return False, 0
        revision = int(persona.get("revision", 0) or 0)
        return True, revision

    def _confirmed_persona(self, scope: str) -> Mapping[str, object] | None:
        persona = self._object_store.read("memory_persona", f"persona-{scope}")
        if not isinstance(persona, Mapping):
            # Compatibility with the original Project Brain fixture key.
            persona = self._object_store.read("memory_persona", scope)
        if not isinstance(persona, Mapping) or not persona.get("id"):
            return None
        confirmation = persona.get("confirmation")
        if (
            not isinstance(confirmation, Mapping)
            or confirmation.get("status") != "confirmed"
            or persona.get("trust_status") != "user_confirmed"
        ):
            return None
        return persona

    def _project_source_ids(self, project_id: str | None) -> set[str]:
        if project_id is None:
            return set()
        return {
            str(source.get("id"))
            for source in self._safe_list("sources")
            if isinstance(source, Mapping)
            and source.get("id")
            and self._record_matches_project(source, project_id)
        }

    def _project_atom_ids(
        self, project_id: str | None, project_source_ids: set[str],
    ) -> set[str]:
        if project_id is None:
            return set()
        atom_ids: set[str] = set()
        for scenario in self._safe_list("memory_scenarios"):
            if not self._record_matches_project(scenario, project_id, project_source_ids):
                continue
            values = scenario.get("atom_ids")
            if isinstance(values, (list, tuple)):
                atom_ids.update(value for value in values if isinstance(value, str) and value)
        return atom_ids

    @classmethod
    def _record_matches_project(
        cls,
        record: Mapping[str, object],
        project_id: str | None,
        project_source_ids: set[str] | None = None,
    ) -> bool:
        if project_id is None:
            return True
        direct = record.get("project_id")
        if isinstance(direct, str) and direct.strip():
            return direct.strip() == project_id
        project_ids = record.get("project_ids")
        if isinstance(project_ids, (list, tuple)):
            return project_id in {
                value.strip() for value in project_ids if isinstance(value, str) and value.strip()
            }
        referenced_sources = cls._record_source_ids(record)
        if referenced_sources:
            return bool(referenced_sources & (project_source_ids or set()))
        return project_id == "default"

    @staticmethod
    def _record_source_ids(record: Mapping[str, object]) -> set[str]:
        source_ids: set[str] = set()
        source_id = record.get("source_id")
        if isinstance(source_id, str) and source_id.strip():
            source_ids.add(source_id.strip())

        def collect(values: object) -> None:
            if not isinstance(values, (list, tuple)):
                return
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                nested_source_id = value.get("source_id")
                if isinstance(nested_source_id, str) and nested_source_id.strip():
                    source_ids.add(nested_source_id.strip())
                collect(value.get("source_refs"))

        collect(record.get("source_refs"))
        collect(record.get("evidence_refs"))
        return source_ids

    def _safe_list(self, collection: str) -> list[Mapping[str, object]]:
        try:
            layer = {"memory_atoms": "atom", "memory_scenarios": "scenario", "memory_series_memory": "series_memory"}.get(collection)
            if layer is not None and self._memory is not None:
                result = self._memory.list(layer)
            elif collection == "project_skills" and self._skills is not None:
                result = self._skills.list_all()
            else:
                result = self._object_store.list(collection)
        except Exception:  # noqa: BLE001
            return []
        return [item for item in result if isinstance(item, Mapping)]

    @staticmethod
    def _extract_tags(metadata: object) -> tuple[str, ...]:
        if not isinstance(metadata, Mapping):
            return ()
        tags = metadata.get("tags")
        if not isinstance(tags, list):
            return ()
        return tuple(str(t) for t in tags if isinstance(t, (str, int, float)))

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[: max_len - 1] + "…"

    @staticmethod
    def _persona_category_label(category: str) -> str:
        mapping = {
            "preference": "用户偏好",
            "constraint": "项目规则",
            "identity": "关键人物",
            "workflow": "工作流偏好",
            "style": "语言风格",
            "other": "其他",
        }
        return mapping.get(category, category or "其他")


# ── 序列化 ──


def serialize_brain_memory_item(item: BrainMemoryItem) -> dict[str, object]:
    return {
        "memory_id": item.memory_id,
        "layer": item.layer,
        "category": item.category,
        "title": item.title,
        "summary": item.summary,
        "tags": list(item.tags),
        "confidence": item.confidence,
        "trust_status": item.trust_status,
        "updated_at": item.updated_at,
        "evidence_refs": [dict(e) for e in item.evidence_refs],
        "related_source_ids": list(item.related_source_ids),
    }


def serialize_brain_candidate_item(item: BrainCandidateItem) -> dict[str, object]:
    return {
        "candidate_id": item.candidate_id,
        "target_layer": item.target_layer,
        "candidate_type": item.candidate_type,
        "status": item.status,
        "title": item.title,
        "summary": item.summary,
        "created_at": item.created_at,
        "source_refs": [dict(s) for s in item.source_refs],
        "memory_publication_state": item.memory_publication_state,
        "series_confidence": item.series_confidence,
        "expert_proposal_id": item.expert_proposal_id,
    }


def serialize_brain_change_item(item: BrainChangeItem) -> dict[str, object]:
    return {
        "change_type": item.change_type,
        "layer": item.layer,
        "memory_id": item.memory_id,
        "title": item.title,
        "summary": item.summary,
        "changed_at": item.changed_at,
        "evidence_refs": [dict(e) for e in item.evidence_refs],
    }


def serialize_brain_layer_summary(summary: BrainLayerSummary) -> dict[str, object]:
    return {
        "layer": summary.layer,
        "label": summary.label,
        "count": summary.count,
        "description": summary.description,
    }


def serialize_project_brain_overview(overview: ProjectBrainOverview) -> dict[str, object]:
    return {
        "scope": overview.scope,
        "layer_summaries": [serialize_brain_layer_summary(s) for s in overview.layer_summaries],
        "memories": [serialize_brain_memory_item(m) for m in overview.memories],
        "candidates": [serialize_brain_candidate_item(c) for c in overview.candidates],
        "recent_changes": [serialize_brain_change_item(c) for c in overview.recent_changes],
        "persona_ready": overview.persona_ready,
        "persona_revision": overview.persona_revision,
        "total_memories": overview.total_memories,
        "total_candidates": overview.total_candidates,
        "total_changes": overview.total_changes,
        "last_updated_at": overview.last_updated_at,
        "retrieval_status": (
            dict(overview.retrieval_status)
            if overview.retrieval_status is not None
            else None
        ),
    }
