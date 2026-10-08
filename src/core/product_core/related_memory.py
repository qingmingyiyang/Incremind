"""跨层关联记忆查询服务（Related Memory Service）。

给定一个记忆对象（atom/scenario/series_memory/project_skill/persona），
返回与之关联的其他记忆对象，模拟"图遍历"能力。

关联规则（基于分层 ID 引用模型）：
- atom → 同 source 的其他 atom / 包含它的 scenario / 它的 series_memory
- scenario → 同 atom_ids 的其他 scenario / 同 series_id 的 scenario / 它的 series_memory
- series_memory → 同 scenario_ids 的其他 series_memory / 同 project_ids 的 series_memory
- project_skill → required_context 引用的对象 / 同 project_id 的其他 project_skill
- persona → evidence_refs 引用的对象

关联是双向的：如果 A 引用 B，则 B 的相关记忆包含 A。

查询策略：全表扫描 + 集合运算（与现有 list_by_source / list_by_project 一致），
不引入 graph 后端，保持与现有存储层兼容。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RelatedMemoryHit:
    """一条相关记忆结果。"""
    object_id: str
    layer: str
    title: str
    summary: str
    relation: str  # 关系类型：same_source / contains / contained_in / same_series / same_project / evidence
    source_refs: tuple[Mapping[str, object], ...]

    def to_payload(self) -> Mapping[str, object]:
        return {
            "object_id": self.object_id,
            "layer": self.layer,
            "title": self.title,
            "summary": self.summary,
            "relation": self.relation,
            "source_refs": [dict(ref) for ref in self.source_refs],
        }


@dataclass(frozen=True, slots=True)
class RelatedMemoryQuery:
    """查询参数。"""
    object_id: str
    layer: str
    limit: int = 8

    LAYER_ALIASES = {
        "atom": "atom",
        "scenario": "scenario",
        "series_memory": "series_memory",
        "project_skill": "project_skill",
        "persona": "persona",
    }

    def supported_layer(self) -> bool:
        return self.layer in self.LAYER_ALIASES


class RelatedMemoryError(ValueError):
    """Raised when related memory query is invalid."""


@dataclass(frozen=True, slots=True)
class RelatedMemoryService:
    """跨层关联记忆查询服务。

    通过 ObjectStore 全表扫描 + 集合运算，模拟图遍历。
    不引入 graph 后端，保持与现有存储层兼容。
    """

    object_store: object  # Persona and legacy/default ObjectStore reader.
    memory: object | None = None  # MemoryReaderPort
    skills: object | None = None  # ProjectSkillRepositoryPort

    def query(self, query: RelatedMemoryQuery) -> tuple[RelatedMemoryHit, ...]:
        """查询与指定对象关联的其他记忆对象。"""
        if not query.object_id:
            raise RelatedMemoryError("object_id is required")
        if not query.supported_layer():
            raise RelatedMemoryError(f"unsupported layer: {query.layer}")
        limit = max(1, min(query.limit, 20))

        # 读取起始对象
        seed = self._read_seed(query.object_id, query.layer)
        if seed is None:
            return ()

        # 按层分派查询策略
        match query.layer:
            case "atom":
                hits = self._related_to_atom(seed)
            case "scenario":
                hits = self._related_to_scenario(seed)
            case "series_memory":
                hits = self._related_to_series_memory(seed)
            case "project_skill":
                hits = self._related_to_project_skill(seed)
            case "persona":
                hits = self._related_to_persona(seed)
            case _:
                hits = ()

        # 去重（按 object_id）、排除自己、限制数量
        seen: set[str] = set()
        unique: list[RelatedMemoryHit] = []
        for hit in hits:
            if hit.object_id == query.object_id:
                continue
            if hit.object_id in seen:
                continue
            seen.add(hit.object_id)
            unique.append(hit)
            if len(unique) >= limit:
                break
        return tuple(unique)

    # ── 读取起始对象 ──

    def _read_seed(self, object_id: str, layer: str) -> Mapping[str, object] | None:
        collection = _collection_for_layer(layer)
        if layer in {"atom", "scenario", "series_memory"} and self.memory is not None:
            return self.memory.get(layer, object_id)
        if layer == "project_skill" and self.skills is not None:
            return self.skills.get(object_id)
        return self.object_store.read(collection, object_id)

    # ── atom 的关联 ──

    def _related_to_atom(self, atom: Mapping[str, object]) -> list[RelatedMemoryHit]:
        hits: list[RelatedMemoryHit] = []
        atom_id = _str(atom.get("id"))
        source_id = _str(atom.get("source_id"))

        # 1. 同 source 的其他 atom
        if source_id:
            for other in self._list("memory_atoms"):
                if _str(other.get("id")) == atom_id:
                    continue
                if _references_source(other, source_id):
                    hits.append(_hit(other, "atom", "same_source"))

        # 2. 包含此 atom 的 scenario
        for scenario in self._list("memory_scenarios"):
            atom_ids = _string_items(scenario.get("atom_ids"))
            if atom_id in atom_ids:
                hits.append(_hit(scenario, "scenario", "contains"))

        # 3. 此 atom 所属 series_memory（通过 scenario 的 series_id）
        series_id = _str(atom.get("series_id"))
        if series_id:
            for sm in self._list("memory_series_memory"):
                if _str(sm.get("series_id")) == series_id:
                    hits.append(_hit(sm, "series_memory", "same_series"))

        return hits

    # ── scenario 的关联 ──

    def _related_to_scenario(self, scenario: Mapping[str, object]) -> list[RelatedMemoryHit]:
        hits: list[RelatedMemoryHit] = []
        scenario_id = _str(scenario.get("id"))
        atom_ids = set(_string_items(scenario.get("atom_ids")))
        series_id = _str(scenario.get("series_id"))

        # 1. 同 series_id 的其他 scenario
        if series_id:
            for other in self._list("memory_scenarios"):
                if _str(other.get("id")) == scenario_id:
                    continue
                if _str(other.get("series_id")) == series_id:
                    hits.append(_hit(other, "scenario", "same_series"))

        # 2. 包含的 atom
        for atom in self._list("memory_atoms"):
            if _str(atom.get("id")) in atom_ids:
                hits.append(_hit(atom, "atom", "contained_in"))

        # 3. 此 scenario 所属 series_memory
        if series_id:
            for sm in self._list("memory_series_memory"):
                if _str(sm.get("series_id")) == series_id:
                    hits.append(_hit(sm, "series_memory", "same_series"))

        return hits

    # ── series_memory 的关联 ──

    def _related_to_series_memory(self, sm: Mapping[str, object]) -> list[RelatedMemoryHit]:
        hits: list[RelatedMemoryHit] = []
        sm_id = _str(sm.get("id"))
        scenario_ids = set(_string_items(sm.get("scenario_ids")))
        project_ids = set(_string_items(sm.get("project_ids")))

        # 1. 同 project_ids 的其他 series_memory
        for other in self._list("memory_series_memory"):
            if _str(other.get("id")) == sm_id:
                continue
            other_projects = set(_string_items(other.get("project_ids")))
            if project_ids & other_projects:
                hits.append(_hit(other, "series_memory", "same_project"))

        # 2. 包含的 scenario
        for scenario in self._list("memory_scenarios"):
            if _str(scenario.get("id")) in scenario_ids:
                hits.append(_hit(scenario, "scenario", "contained_in"))

        return hits

    # ── project_skill 的关联 ──

    def _related_to_project_skill(self, skill: Mapping[str, object]) -> list[RelatedMemoryHit]:
        hits: list[RelatedMemoryHit] = []
        skill_id = _str(skill.get("id"))
        project_id = _str(skill.get("project_id"))

        # 1. 同 project_id 的其他 project_skill
        if project_id:
            for other in self._list("project_skills"):
                if _str(other.get("id")) == skill_id:
                    continue
                if _str(other.get("project_id")) == project_id:
                    hits.append(_hit(other, "project_skill", "same_project"))

        # 2. required_context 引用的对象
        required_context = skill.get("required_context")
        if isinstance(required_context, list):
            for ctx in required_context:
                if not isinstance(ctx, dict):
                    continue
                ctx_kind = _str(ctx.get("kind"))
                ctx_object_id = _str(ctx.get("object_id"))
                if not ctx_object_id:
                    continue
                layer = _layer_from_context_kind(ctx_kind)
                if layer is None:
                    continue
                referenced = self._read_seed(ctx_object_id, layer)
                if referenced is not None:
                    hits.append(_hit(referenced, layer, "evidence"))

        return hits

    # ── persona 的关联 ──

    def _related_to_persona(self, persona: Mapping[str, object]) -> list[RelatedMemoryHit]:
        hits: list[RelatedMemoryHit] = []
        evidence_refs = persona.get("evidence_refs")
        if not isinstance(evidence_refs, list):
            return hits

        for ref in evidence_refs:
            if not isinstance(ref, dict):
                continue
            object_type = _str(ref.get("object_type"))
            object_id = _str(ref.get("object_id"))
            if not object_id:
                continue
            layer = _layer_from_object_type(object_type)
            if layer is None:
                continue
            referenced = self._read_seed(object_id, layer)
            if referenced is not None:
                hits.append(_hit(referenced, layer, "evidence"))

        return hits

    # ── 工具方法 ──

    def _list(self, collection: str) -> Sequence[Mapping[str, object]]:
        layer = _LAYER_FOR_COLLECTION.get(collection)
        if layer is not None and self.memory is not None:
            return self.memory.list(layer)
        if collection == "project_skills" and self.skills is not None:
            return self.skills.list_all()
        return self.object_store.list(collection)


# ── 模块级工具函数 ──

_LAYER_TO_COLLECTION = {
    "atom": "memory_atoms",
    "scenario": "memory_scenarios",
    "series_memory": "memory_series_memory",
    "project_skill": "project_skills",
    "persona": "memory_persona",
}

_LAYER_FOR_COLLECTION = {collection: layer for layer, collection in _LAYER_TO_COLLECTION.items() if layer != "persona"}

_CONTEXT_KIND_TO_LAYER = {
    "source": None,  # source 不是记忆对象，跳过
    "atom": "atom",
    "scenario": "scenario",
    "persona": "persona",
    "series_memory": "series_memory",
    "document": None,  # document 不是记忆对象，跳过
    "project_skill": "project_skill",
}

_OBJECT_TYPE_TO_LAYER = {
    "source": None,
    "atom": "atom",
    "scenario": "scenario",
    "document": None,
}


def _collection_for_layer(layer: str) -> str:
    if layer not in _LAYER_TO_COLLECTION:
        raise RelatedMemoryError(f"unsupported layer: {layer}")
    return _LAYER_TO_COLLECTION[layer]


def _layer_from_context_kind(kind: str) -> str | None:
    return _CONTEXT_KIND_TO_LAYER.get(kind)


def _layer_from_object_type(object_type: str) -> str | None:
    return _OBJECT_TYPE_TO_LAYER.get(object_type)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _string_items(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _references_source(item: Mapping[str, object], source_id: str) -> bool:
    if item.get("source_id") == source_id:
        return True
    source_refs = item.get("source_refs")
    if not isinstance(source_refs, list):
        return False
    return any(isinstance(ref, dict) and ref.get("source_id") == source_id for ref in source_refs)


def _hit(item: Mapping[str, object], layer: str, relation: str) -> RelatedMemoryHit:
    """从存储记录构造 RelatedMemoryHit。"""
    title = _str(item.get("title")) or _str(item.get("name")) or _str(item.get("id"))
    summary = _str(item.get("summary")) or _str(item.get("structured_summary"))
    source_refs_raw = item.get("source_refs")
    source_refs: tuple[Mapping[str, object], ...] = ()
    if isinstance(source_refs_raw, list):
        source_refs = tuple(
            dict(ref) for ref in source_refs_raw if isinstance(ref, dict)
        )
    return RelatedMemoryHit(
        object_id=_str(item.get("id")),
        layer=layer,
        title=title,
        summary=summary,
        relation=relation,
        source_refs=source_refs,
    )
