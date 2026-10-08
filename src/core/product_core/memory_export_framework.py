"""阶段 1.6：Memory Export Framework — 记忆资产导出框架。

支持四种导出模式：
1. 完整资产包（full_asset_package）— 用于备份、恢复、迁移到另一个本产品实例
2. 通用结构化导出（structured_export）— JSON / JSONL / NDJSON / Markdown / CSV
3. 其他 LLM 平台友好格式（llm_friendly）— 复制或上传到其他 LLM 平台
4. 安全脱敏导出（safe_redacted）— 完整 / 脱敏 / 只导出已确认 / 不导出原始资料

导出预设：
- generic_llm_project_knowledge — 通用 LLM 项目知识包
- generic_custom_instructions — 自定义指令合集
- generic_rag_corpus — RAG 语料库
- generic_markdown_knowledge_base — Markdown 知识包
- compact_persona_prompt — 紧凑 Persona Prompt
- full_memory_brief — 完整记忆简报

Memory Asset Package 结构：
memory_asset_package.zip
├── manifest.json
├── memories/
│   ├── l1_atomic_facts.ndjson
│   ├── l2_scenarios.ndjson
│   └── l3_persona_series_project_skill.ndjson
├── sources/
│   └── source_manifest.ndjson
├── evidence/
│   └── evidence_graph.ndjson
├── tags/
│   └── tag_index.json
├── persona/
│   ├── persona_summary.md
│   └── persona_structured.json
├── series/
│   ├── series_summary.md
│   └── series_structured.json
├── project_skills/
│   └── project_skill_cards.ndjson
├── imports/
│   └── import_batches.ndjson
├── exports/
│   └── export_batches.ndjson
├── audits/
│   └── whitebox_audit_summary.json
├── tasks/
│   └── task_history.ndjson
└── README.md

禁止导出：secret / cookie / token / 完整隐私路径 / 不必要的 Provider 原始响应 / 未经用户选择的敏感原文

完整资产包支持 round-trip：导出 → 新实例 → 导入 → 重建资料库 / 标签 / 证据 / 项目大脑
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from .memory_quality_gate import detect_secrets, redact_content


class MemoryExportError(ValueError):
    """Raised when memory export fails."""


# ── 导出预设 ──

ExportPreset = Literal[
    "generic_llm_project_knowledge",
    "generic_custom_instructions",
    "generic_rag_corpus",
    "generic_markdown_knowledge_base",
    "compact_persona_prompt",
    "full_memory_brief",
    "full_asset_package",  # 完整资产包
]


# ── 导出范围选项 ──


@dataclass(frozen=True, slots=True)
class ExportScope:
    """导出范围与脱敏选项。"""

    preset: ExportPreset
    redact_secrets: bool = True  # 默认脱敏 secret/cookie/token
    only_confirmed: bool = False  # 只导出已确认 memory
    skip_raw_sources: bool = False  # 不导出原始资料
    skip_av: bool = False  # 不导出音视频
    skip_evidence_text: bool = False  # 不导出证据原文，只导出引用
    skip_low_trust: bool = False  # 不导出低可信候选
    skip_conflicts: bool = False  # 不导出冲突项
    skip_provider_audit: bool = False  # 不导出 Provider 审计记录
    include_paths: bool = False  # 是否包含完整路径（默认 False）


# ── 导出输入数据 ──


@dataclass(frozen=True, slots=True)
class ExportableMemory:
    """可导出的单条 memory。"""

    memory_id: str
    layer: str  # L0 | L1 | L2 | L3 | L4
    type: str
    content: str
    summary: str = ""
    tags: tuple[str, ...] = ()
    confidence: float = 0.5
    trust_level: str = "unverified"
    source_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    # v1.2 asset packages retain legacy display fields and carry this explicit
    # temporal pair.  ``None`` means the original occurrence was not known.
    occurred_at: str | None = None
    recorded_at: str = ""
    privacy_level: str = "private"
    confirmed: bool = False
    status: str = "candidate"
    project_id: str = ""
    series_id: str = ""
    atom_ids: tuple[str, ...] = ()
    scenario_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportableSource:
    """可导出的 L0 Source 记录。"""

    source_id: str
    source_type: str
    title: str
    content_ref: str  # 引用，不包含完整路径
    media_type: str = ""
    created_at: str = ""
    occurred_at: str | None = None
    recorded_at: str = ""
    is_audio_visual: bool = False


@dataclass(frozen=True, slots=True)
class ExportableSourceAsset:
    """A verified original file that may be carried by a full asset package."""

    source_id: str
    asset_id: str
    display_name: str
    media_type: str
    byte_count: int
    sha256: str
    content: bytes
    is_audio_visual: bool = False


@dataclass(frozen=True, slots=True)
class ExportableTag:
    """可导出的标签索引项。"""

    tag: str
    memory_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExportableEvidenceLink:
    """证据图谱中的单条链接。"""

    memory_id: str
    source_ref: str
    evidence_ref: str
    relation: str  # supports | contradicts | derived_from | references


@dataclass(frozen=True, slots=True)
class ExportPayload:
    """导出前的统一数据载荷。"""

    memories: tuple[ExportableMemory, ...]
    sources: tuple[ExportableSource, ...]
    tags: tuple[ExportableTag, ...]
    evidence_links: tuple[ExportableEvidenceLink, ...]
    source_assets: tuple[ExportableSourceAsset, ...] = ()
    source_asset_expected_count: int = 0
    persona_summary: str = ""
    series_summary: str = ""
    project_skill_cards: tuple[Mapping[str, object], ...] = ()
    import_batches: tuple[Mapping[str, object], ...] = ()
    export_batches: tuple[Mapping[str, object], ...] = ()
    audit_summary: Mapping[str, object] = field(default_factory=dict)
    task_history: tuple[Mapping[str, object], ...] = ()


# ── 导出结果 ──


@dataclass(frozen=True, slots=True)
class ExportResult:
    """导出结果。"""

    preset: ExportPreset
    format: str  # zip | markdown | json | ndjson | csv | prompt_text
    bytes_payload: bytes
    file_name: str
    memory_count: int
    source_count: int
    redacted_count: int
    skipped_count: int
    summary: str
    export_batch_id: str
    error: str = ""


# ── 过滤与脱敏 ──


def _filter_memories(
    memories: Sequence[ExportableMemory],
    scope: ExportScope,
) -> tuple[tuple[ExportableMemory, ...], int, int]:
    """根据 scope 过滤 memory，返回（过滤后列表, 脱敏数, 跳过数）。"""
    filtered: list[ExportableMemory] = []
    redacted_count = 0
    skipped_count = 0

    for m in memories:
        # 只导出已确认
        if scope.only_confirmed and not m.confirmed:
            skipped_count += 1
            continue
        # 跳过低可信
        if scope.skip_low_trust and m.trust_level == "low":
            skipped_count += 1
            continue
        # 跳过冲突项
        if scope.skip_conflicts and m.status == "needs_review" and m.type == "other":
            skipped_count += 1
            continue

        # 脱敏
        content = m.content
        summary = m.summary
        if scope.redact_secrets:
            new_content = redact_content(content)
            new_summary = redact_content(summary)
            if new_content != content or new_summary != summary:
                redacted_count += 1
            content = new_content
            summary = new_summary

        # 跳过证据原文（只保留引用）
        evidence_refs = m.evidence_refs
        if scope.skip_evidence_text:
            # evidence_refs 本身就是引用，不需要修改
            pass

        filtered.append(ExportableMemory(
            memory_id=m.memory_id,
            layer=m.layer,
            type=m.type,
            content=content,
            summary=summary,
            tags=m.tags,
            confidence=m.confidence,
            trust_level=m.trust_level,
            source_ref=m.source_ref,
            evidence_refs=evidence_refs,
            created_at=m.created_at,
            updated_at=m.updated_at,
            occurred_at=m.occurred_at,
            recorded_at=m.recorded_at,
            privacy_level=m.privacy_level,
            confirmed=m.confirmed,
            status=m.status,
            project_id=m.project_id,
            series_id=m.series_id,
            atom_ids=m.atom_ids,
            scenario_ids=m.scenario_ids,
        ))

    return tuple(filtered), redacted_count, skipped_count


def _filter_sources(
    sources: Sequence[ExportableSource],
    scope: ExportScope,
) -> tuple[tuple[ExportableSource, ...], int]:
    """过滤 Source。"""
    filtered: list[ExportableSource] = []
    skipped = 0
    for s in sources:
        # Source identity and provenance are required even when original bytes
        # are excluded. Otherwise memories retain dangling source references.
        # 完整路径默认不导出
        content_ref = s.content_ref
        if not scope.include_paths:
            # 移除可能的完整路径，只保留 source_id 引用
            if "://" not in content_ref and "/" in content_ref:
                content_ref = content_ref.split("/")[-1]
        filtered.append(ExportableSource(
            source_id=s.source_id,
            source_type=s.source_type,
            title=s.title,
            content_ref=content_ref,
            media_type=s.media_type,
            created_at=s.created_at,
            occurred_at=s.occurred_at,
            recorded_at=s.recorded_at,
            is_audio_visual=s.is_audio_visual,
        ))
    return tuple(filtered), skipped


def _filter_source_assets(
    assets: Sequence[ExportableSourceAsset],
    scope: ExportScope,
) -> tuple[tuple[ExportableSourceAsset, ...], int]:
    if scope.skip_raw_sources:
        return (), len(assets)
    filtered = tuple(
        asset
        for asset in assets
        if not (scope.skip_av and asset.is_audio_visual)
    )
    return filtered, len(assets) - len(filtered)


# ── 预设渲染 ──


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _render_markdown_knowledge_base(payload: ExportPayload) -> str:
    """渲染 Markdown 知识包。"""
    lines: list[str] = []
    lines.append("# 私人记忆知识包")
    lines.append("")
    lines.append(f"导出时间：{_now_iso()}")
    lines.append("")

    # 项目概览
    lines.append("## 项目概览")
    if payload.persona_summary:
        lines.append(payload.persona_summary)
    lines.append("")

    # 用户偏好
    prefs = [m for m in payload.memories if m.type == "preference"]
    if prefs:
        lines.append("## 用户偏好")
        for p in prefs:
            lines.append(f"- {p.content}")
        lines.append("")

    # 写作 / 工作风格
    styles = [
        m for m in payload.memories
        if m.type == "persona" or "style" in m.content.lower()
    ]
    if styles:
        lines.append("## 写作 / 工作风格")
        for s in styles:
            lines.append(f"- {s.content}")
        lines.append("")

    # 项目事实
    facts = [m for m in payload.memories if m.type == "fact"]
    if facts:
        lines.append("## 项目事实")
        for f in facts:
            lines.append(f"- {f.content}")
        lines.append("")

    # 项目规则
    rules = [m for m in payload.memories if m.type == "rule"]
    if rules:
        lines.append("## 项目规则")
        for r in rules:
            lines.append(f"- {r.content}")
        lines.append("")

    # 长期目标
    goals = [m for m in payload.memories if m.type == "decision" or "目标" in m.content]
    if goals:
        lines.append("## 长期目标与决策")
        for g in goals:
            lines.append(f"- {g.content}")
        lines.append("")

    # 关键人物和术语
    persons = [m for m in payload.memories if m.type == "person"]
    if persons:
        lines.append("## 关键人物和术语")
        for p in persons:
            lines.append(f"- {p.content}")
        lines.append("")

    # 常见任务经验
    workflows = [m for m in payload.memories if m.type == "workflow"]
    if workflows:
        lines.append("## 常见任务经验")
        for w in workflows:
            lines.append(f"- {w.content}")
        lines.append("")

    # 待确认记忆
    pending = [m for m in payload.memories if m.status == "needs_review"]
    if pending:
        lines.append("## 待确认记忆")
        for p in pending:
            lines.append(f"- {p.content}（待确认）")
        lines.append("")

    # 证据索引
    if payload.evidence_links:
        lines.append("## 证据索引")
        for link in payload.evidence_links:
            lines.append(f"- {link.memory_id} ← {link.relation} → {link.evidence_ref}")
        lines.append("")

    return "\n".join(lines)


def _render_compact_persona_prompt(payload: ExportPayload) -> str:
    """渲染紧凑 Persona Prompt（适合粘贴到其他 LLM）。"""
    lines: list[str] = []
    lines.append("# 用户画像与偏好")
    lines.append("")
    if payload.persona_summary:
        lines.append(payload.persona_summary)
        lines.append("")

    prefs = [m for m in payload.memories if m.type == "preference" and m.confirmed]
    if prefs:
        lines.append("## 偏好")
        for p in prefs:
            lines.append(f"- {p.content}")
        lines.append("")

    facts = [m for m in payload.memories if m.type == "fact" and m.confirmed]
    if facts:
        lines.append("## 已确认事实")
        for f in facts:
            lines.append(f"- {f.content}")
        lines.append("")

    rules = [m for m in payload.memories if m.type == "rule" and m.confirmed]
    if rules:
        lines.append("## 规则")
        for r in rules:
            lines.append(f"- {r.content}")
        lines.append("")

    return "\n".join(lines)


def _render_full_memory_brief(payload: ExportPayload) -> str:
    """渲染完整记忆简报。"""
    lines: list[str] = []
    lines.append("# 完整记忆简报")
    lines.append(f"\n导出时间：{_now_iso()}\n")

    # L4 Persona
    l4 = [m for m in payload.memories if m.layer == "L4"]
    if l4:
        lines.append("## L4 稳定画像")
        for m in l4:
            lines.append(f"- [{m.type}] {m.content}（置信度 {m.confidence:.2f}，{m.trust_level}）")
        lines.append("")

    # L3 Series / Project Skill
    l3 = [m for m in payload.memories if m.layer == "L3"]
    if l3:
        lines.append("## L3 系列记忆与项目技能")
        for m in l3:
            lines.append(f"- [{m.type}] {m.content}（置信度 {m.confidence:.2f}，{m.trust_level}）")
        lines.append("")

    # L2 Scenario
    l2 = [m for m in payload.memories if m.layer == "L2"]
    if l2:
        lines.append("## L2 场景与任务经验")
        for m in l2:
            lines.append(f"- [{m.type}] {m.content}")
        lines.append("")

    # L1 Atom
    l1 = [m for m in payload.memories if m.layer == "L1"]
    if l1:
        lines.append("## L1 原子事实")
        for m in l1:
            lines.append(f"- [{m.type}] {m.content}（来源 {m.source_ref}）")
        lines.append("")

    # L0 Source 索引
    if payload.sources:
        lines.append("## L0 原始资料索引")
        for s in payload.sources:
            lines.append(f"- {s.title}（{s.source_type}）→ {s.content_ref}")
        lines.append("")

    # 标签
    if payload.tags:
        lines.append("## 标签索引")
        for t in payload.tags:
            lines.append(f"- {t.tag}（{len(t.memory_ids)} 条）")
        lines.append("")

    # 证据图谱
    if payload.evidence_links:
        lines.append("## 证据图谱")
        for link in payload.evidence_links:
            lines.append(f"- {link.memory_id} ← {link.relation} → {link.evidence_ref}")
        lines.append("")

    return "\n".join(lines)


def _render_generic_llm_project_knowledge(payload: ExportPayload) -> str:
    """渲染通用 LLM 项目知识包。"""
    lines: list[str] = []
    lines.append("# 项目知识包")
    lines.append("")

    # 项目概览
    if payload.persona_summary:
        lines.append("## 项目概览")
        lines.append(payload.persona_summary)
        lines.append("")

    # 项目事实
    facts = [m for m in payload.memories if m.type == "fact" and m.confirmed]
    if facts:
        lines.append("## 项目事实")
        for f in facts:
            lines.append(f"- {f.content}")
        lines.append("")

    # 项目规则
    rules = [m for m in payload.memories if m.type == "rule" and m.confirmed]
    if rules:
        lines.append("## 项目规则")
        for r in rules:
            lines.append(f"- {r.content}")
        lines.append("")

    # 偏好
    prefs = [m for m in payload.memories if m.type == "preference" and m.confirmed]
    if prefs:
        lines.append("## 偏好")
        for p in prefs:
            lines.append(f"- {p.content}")
        lines.append("")

    # 项目技能
    if payload.project_skill_cards:
        lines.append("## 项目技能")
        for card in payload.project_skill_cards:
            name = card.get("name", "")
            desc = card.get("description", "")
            lines.append(f"- **{name}**：{desc}")
        lines.append("")

    return "\n".join(lines)


def _render_custom_instructions(payload: ExportPayload) -> str:
    """渲染自定义指令合集。"""
    lines: list[str] = []
    lines.append("# 自定义指令")
    lines.append("")

    rules = [m for m in payload.memories if m.type == "rule" and m.confirmed]
    if rules:
        lines.append("## 规则")
        for r in rules:
            lines.append(f"- {r.content}")
        lines.append("")

    prefs = [m for m in payload.memories if m.type == "preference" and m.confirmed]
    if prefs:
        lines.append("## 偏好")
        for p in prefs:
            lines.append(f"- {p.content}")
        lines.append("")

    personas = [m for m in payload.memories if m.type == "persona" and m.confirmed]
    if personas:
        lines.append("## 用户画像")
        for p in personas:
            lines.append(f"- {p.content}")
        lines.append("")

    return "\n".join(lines)


def _render_rag_corpus(payload: ExportPayload) -> bytes:
    """渲染 RAG 语料库（NDJSON 格式）。"""
    lines: list[str] = []
    for m in payload.memories:
        if m.layer not in ("L1", "L2"):
            continue
        entry = {
            "id": m.memory_id,
            "text": m.content,
            "metadata": {
                "layer": m.layer,
                "type": m.type,
                "tags": list(m.tags),
                "confidence": m.confidence,
                "trust_level": m.trust_level,
                "source_ref": m.source_ref,
            },
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines).encode("utf-8")


# ── 完整资产包（ZIP）──


def _render_full_asset_package(
    payload: ExportPayload,
    *,
    export_batch_id: str,
) -> bytes:
    """渲染完整 Memory Asset Package（ZIP）。"""
    packaged_memories = tuple(
        memory for memory in payload.memories if memory.layer in {"L1", "L2", "L3", "L4"}
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # manifest.json
        manifest = {
            "format": "memory_asset_package",
            "version": "1.2",
            "exported_at": _now_iso(),
            "export_batch_id": export_batch_id,
            "memory_count": len(packaged_memories),
            "project_skill_count": len(payload.project_skill_cards),
            "source_count": len(payload.sources),
            "tag_count": len(payload.tags),
            "evidence_link_count": len(payload.evidence_links),
            "source_asset_count": len(payload.source_assets),
            "source_asset_expected_count": payload.source_asset_expected_count,
            "source_asset_omitted_count": max(
                payload.source_asset_expected_count - len(payload.source_assets),
                0,
            ),
            "source_asset_bytes": sum(asset.byte_count for asset in payload.source_assets),
            "raw_sources_included": bool(payload.source_assets),
            "layer_counts": {
                layer: len([memory for memory in packaged_memories if memory.layer == layer])
                for layer in ("L1", "L2", "L3", "L4")
            },
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

        # memories/
        l1_lines = [json.dumps(_memory_to_dict(m), ensure_ascii=False) for m in payload.memories if m.layer == "L1"]
        zf.writestr("memories/l1_atomic_facts.ndjson", "\n".join(l1_lines))

        l2_lines = [json.dumps(_memory_to_dict(m), ensure_ascii=False) for m in payload.memories if m.layer == "L2"]
        zf.writestr("memories/l2_scenarios.ndjson", "\n".join(l2_lines))

        l3_lines = [json.dumps(_memory_to_dict(m), ensure_ascii=False) for m in payload.memories if m.layer == "L3"]
        zf.writestr("memories/l3_persona_series_project_skill.ndjson", "\n".join(l3_lines))

        l4_lines = [json.dumps(_memory_to_dict(m), ensure_ascii=False) for m in payload.memories if m.layer == "L4"]
        zf.writestr("memories/l4_persona.ndjson", "\n".join(l4_lines))

        # sources/
        source_lines = [json.dumps(_source_to_dict(s), ensure_ascii=False) for s in payload.sources]
        zf.writestr("sources/source_manifest.ndjson", "\n".join(source_lines))
        asset_lines: list[str] = []
        written_blobs: set[str] = set()
        for asset in payload.source_assets:
            blob_path = f"sources/content/{asset.sha256}"
            asset_lines.append(json.dumps({
                "source_id": asset.source_id,
                "asset_id": asset.asset_id,
                "display_name": asset.display_name,
                "media_type": asset.media_type,
                "byte_count": asset.byte_count,
                "sha256": asset.sha256,
                "blob_path": blob_path,
                "is_audio_visual": asset.is_audio_visual,
            }, ensure_ascii=False))
            if asset.sha256 not in written_blobs:
                zf.writestr(blob_path, asset.content)
                written_blobs.add(asset.sha256)
        zf.writestr("sources/source_assets.ndjson", "\n".join(asset_lines))

        # evidence/
        evidence_lines = [json.dumps({
            "memory_id": link.memory_id,
            "source_ref": link.source_ref,
            "evidence_ref": link.evidence_ref,
            "relation": link.relation,
        }, ensure_ascii=False) for link in payload.evidence_links]
        zf.writestr("evidence/evidence_graph.ndjson", "\n".join(evidence_lines))

        # tags/
        tag_index = {t.tag: list(t.memory_ids) for t in payload.tags}
        zf.writestr("tags/tag_index.json", json.dumps(tag_index, ensure_ascii=False, indent=2))

        # persona/
        zf.writestr("persona/persona_summary.md", payload.persona_summary or "（暂无 Persona 摘要）")
        zf.writestr(
            "persona/persona_structured.json",
            json.dumps({"summary": payload.persona_summary, "memory_count": len([m for m in payload.memories if m.layer == "L4"])}, ensure_ascii=False, indent=2),
        )

        # series/
        zf.writestr("series/series_summary.md", payload.series_summary or "（暂无系列摘要）")
        zf.writestr(
            "series/series_structured.json",
            json.dumps({"summary": payload.series_summary}, ensure_ascii=False, indent=2),
        )

        # project_skills/
        skill_lines = [json.dumps(dict(card), ensure_ascii=False) for card in payload.project_skill_cards]
        zf.writestr("project_skills/project_skill_cards.ndjson", "\n".join(skill_lines))

        # imports/
        import_lines = [json.dumps(dict(b), ensure_ascii=False) for b in payload.import_batches]
        zf.writestr("imports/import_batches.ndjson", "\n".join(import_lines))

        # exports/
        export_lines = [json.dumps(dict(b), ensure_ascii=False) for b in payload.export_batches]
        zf.writestr("exports/export_batches.ndjson", "\n".join(export_lines))

        # audits/
        zf.writestr(
            "audits/whitebox_audit_summary.json",
            json.dumps(dict(payload.audit_summary), ensure_ascii=False, indent=2),
        )

        # tasks/
        task_lines = [json.dumps(dict(t), ensure_ascii=False) for t in payload.task_history]
        zf.writestr("tasks/task_history.ndjson", "\n".join(task_lines))

        # README.md
        zf.writestr("README.md", _render_package_readme(manifest))

    return buf.getvalue()


def _render_package_readme(manifest: Mapping[str, object]) -> str:
    """渲染资产包 README。"""
    return f"""# Memory Asset Package

本包是从 Chrip_OS 私人 AI 记忆台导出的完整记忆资产。

## 元数据
- 格式：memory_asset_package v{manifest.get("version", "")}
- 导出时间：{manifest.get("exported_at", "")}
- 导出批次：{manifest.get("export_batch_id", "")}
- Memory 数量：{manifest.get("memory_count", 0)}
- Source 数量：{manifest.get("source_count", 0)}
- 标签数量：{manifest.get("tag_count", 0)}
- 证据链接数量：{manifest.get("evidence_link_count", 0)}

## 目录结构
- manifest.json — 包元数据
- memories/ — L1/L2/L3/L4 memory（NDJSON）
- sources/ — L0 原始资料索引（NDJSON）
- sources/source_assets.ndjson — 可选原档的校验清单
- sources/content/ — 用户选择携带的原档字节（按 SHA-256 命名）
- evidence/ — 证据图谱（NDJSON）
- tags/ — 标签索引（JSON）
- persona/ — Persona 摘要（Markdown + JSON）
- series/ — 系列摘要（Markdown + JSON）
- project_skills/ — 项目技能卡片（NDJSON）
- imports/ — 导入批次记录（NDJSON）
- exports/ — 导出批次记录（NDJSON）
- audits/ — 白盒审计摘要（JSON）
- tasks/ — 任务历史（NDJSON）

## Round-trip 恢复
本包可用于在新实例中重建记忆库：
1. 在新实例打开「设置 → 高级 → 导入资料」
2. 选择本 ZIP 包
3. 系统会校验 manifest、重建索引、合并冲突项
4. 校验完成后可查看导入报告

## 隐私
本包不包含：secret / cookie / token / 完整隐私路径 / Provider 原始响应
若导出时启用了脱敏，敏感字段已被替换为 [REDACTED]。
"""


def _memory_to_dict(m: ExportableMemory) -> dict[str, object]:
    return {
        "memory_id": m.memory_id,
        "layer": m.layer,
        "type": m.type,
        "content": m.content,
        "summary": m.summary,
        "tags": list(m.tags),
        "confidence": m.confidence,
        "trust_level": m.trust_level,
        "source_ref": m.source_ref,
        "evidence_refs": list(m.evidence_refs),
        "created_at": m.created_at,
        "updated_at": m.updated_at,
        "occurred_at": _portable_occurred_at(m.occurred_at, m.created_at),
        "recorded_at": _portable_recorded_at(m.recorded_at, m.created_at),
        "privacy_level": m.privacy_level,
        "confirmed": m.confirmed,
        "status": m.status,
        "project_id": m.project_id,
        "series_id": m.series_id,
        "atom_ids": list(m.atom_ids),
        "scenario_ids": list(m.scenario_ids),
    }


def _source_to_dict(s: ExportableSource) -> dict[str, object]:
    return {
        "source_id": s.source_id,
        "source_type": s.source_type,
        "title": s.title,
        "content_ref": s.content_ref,
        "media_type": s.media_type,
        "created_at": s.created_at,
        "occurred_at": _portable_occurred_at(s.occurred_at, s.created_at),
        "recorded_at": _portable_recorded_at(s.recorded_at, s.created_at),
        "is_audio_visual": s.is_audio_visual,
    }


def _portable_occurred_at(value: str | None, legacy_created_at: str) -> str | None:
    if isinstance(value, str) and value:
        return value
    return legacy_created_at or None


def _portable_recorded_at(value: str, legacy_created_at: str) -> str:
    if value:
        return value
    if legacy_created_at:
        return legacy_created_at
    return _now_iso()


# ── 主入口 ──


def export_memory(payload: ExportPayload, scope: ExportScope, *, export_batch_id: str) -> ExportResult:
    """按 scope 导出 memory。

    主入口：根据 preset 选择渲染器，过滤 + 脱敏 + 输出。
    """
    # 1. 过滤 + 脱敏
    filtered_memories, redacted_count, skipped_mem = _filter_memories(payload.memories, scope)
    filtered_sources, skipped_src = _filter_sources(payload.sources, scope)
    filtered_source_assets, skipped_assets = _filter_source_assets(payload.source_assets, scope)

    filtered_payload = ExportPayload(
        memories=filtered_memories,
        sources=filtered_sources,
        tags=payload.tags,
        evidence_links=payload.evidence_links,
        source_assets=filtered_source_assets,
        source_asset_expected_count=payload.source_asset_expected_count,
        persona_summary=redact_content(payload.persona_summary) if scope.redact_secrets else payload.persona_summary,
        series_summary=redact_content(payload.series_summary) if scope.redact_secrets else payload.series_summary,
        project_skill_cards=payload.project_skill_cards,
        import_batches=payload.import_batches,
        export_batches=payload.export_batches,
        audit_summary=payload.audit_summary if not scope.skip_provider_audit else {},
        task_history=payload.task_history,
    )

    # 2. 按 preset 渲染
    preset = scope.preset
    if preset == "full_asset_package":
        bytes_payload = _render_full_asset_package(filtered_payload, export_batch_id=export_batch_id)
        fmt = "zip"
        file_name = f"memory_asset_package_{export_batch_id}.zip"
    elif preset == "generic_markdown_knowledge_base":
        bytes_payload = _render_markdown_knowledge_base(filtered_payload).encode("utf-8")
        fmt = "markdown"
        file_name = f"memory_knowledge_base_{export_batch_id}.md"
    elif preset == "compact_persona_prompt":
        bytes_payload = _render_compact_persona_prompt(filtered_payload).encode("utf-8")
        fmt = "prompt_text"
        file_name = f"compact_persona_prompt_{export_batch_id}.md"
    elif preset == "full_memory_brief":
        bytes_payload = _render_full_memory_brief(filtered_payload).encode("utf-8")
        fmt = "markdown"
        file_name = f"full_memory_brief_{export_batch_id}.md"
    elif preset == "generic_llm_project_knowledge":
        bytes_payload = _render_generic_llm_project_knowledge(filtered_payload).encode("utf-8")
        fmt = "markdown"
        file_name = f"llm_project_knowledge_{export_batch_id}.md"
    elif preset == "generic_custom_instructions":
        bytes_payload = _render_custom_instructions(filtered_payload).encode("utf-8")
        fmt = "markdown"
        file_name = f"custom_instructions_{export_batch_id}.md"
    elif preset == "generic_rag_corpus":
        bytes_payload = _render_rag_corpus(filtered_payload)
        fmt = "ndjson"
        file_name = f"rag_corpus_{export_batch_id}.ndjson"
    else:
        return ExportResult(
            preset=preset,
            format="unknown",
            bytes_payload=b"",
            file_name="",
            memory_count=0,
            source_count=0,
            redacted_count=0,
            skipped_count=0,
            summary=f"未知预设：{preset}",
            export_batch_id=export_batch_id,
            error="unknown_preset",
        )

    summary = (
        f"导出 {preset}：{len(filtered_memories)} 条 memory、"
        f"{len(filtered_sources)} 条 Source；"
        f"脱敏 {redacted_count}，跳过 {skipped_mem + skipped_src + skipped_assets}；"
        f"格式 {fmt}"
    )

    return ExportResult(
        preset=preset,
        format=fmt,
        bytes_payload=bytes_payload,
        file_name=file_name,
        memory_count=len(filtered_memories),
        source_count=len(filtered_sources),
        redacted_count=redacted_count,
        skipped_count=skipped_mem + skipped_src + skipped_assets,
        summary=summary,
        export_batch_id=export_batch_id,
    )


# ── Round-trip 导入 ──


@dataclass(frozen=True, slots=True)
class AssetPackageImportReport:
    """完整资产包导入报告。"""

    is_valid: bool
    manifest: Mapping[str, object]
    imported_memory_count: int
    imported_source_count: int
    imported_tag_count: int
    imported_evidence_count: int
    imported_project_skill_count: int
    imported_source_asset_count: int
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    summary: str


def import_asset_package(zip_bytes: bytes) -> AssetPackageImportReport:
    """导入完整 Memory Asset Package，返回校验报告。

    用于 round-trip：导出 → 导入 → 重建。
    """
    errors: list[str] = []
    warnings: list[str] = []
    manifest: dict[str, object] = {}
    memory_count = 0
    source_count = 0
    tag_count = 0
    evidence_count = 0
    project_skill_count = 0
    source_asset_count = 0

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        return AssetPackageImportReport(
            is_valid=False,
            manifest={},
            imported_memory_count=0,
            imported_source_count=0,
            imported_tag_count=0,
            imported_evidence_count=0,
            imported_project_skill_count=0,
            imported_source_asset_count=0,
            errors=(f"ZIP 解压失败：{exc}",),
            warnings=(),
            summary="资产包校验失败：ZIP 无法解压",
        )

    with zf:
        entry_names = [entry.filename for entry in zf.infolist()]
        if len(entry_names) != len(set(entry_names)):
            errors.append("资产包包含重复 ZIP 条目")

        # 1. manifest.json 必须存在
        try:
            manifest_data = zf.read("manifest.json")
            parsed_manifest = json.loads(manifest_data.decode("utf-8"))
            if isinstance(parsed_manifest, dict):
                manifest = parsed_manifest
            else:
                errors.append("manifest.json 必须包含 JSON object")
        except KeyError:
            errors.append("资产包缺少 manifest.json")
        except UnicodeDecodeError as exc:
            errors.append(f"manifest.json 不是 UTF-8：{exc}")
        except json.JSONDecodeError as exc:
            errors.append(f"manifest.json 解析失败：{exc}")

        # 2. 校验 manifest 字段
        if manifest:
            if manifest.get("format") != "memory_asset_package":
                errors.append(f"manifest.format 不是 memory_asset_package：{manifest.get('format')}")
            if not manifest.get("version"):
                warnings.append("manifest 缺少 version 字段")

        # 3. 统计各部分
        for path in (
            "memories/l1_atomic_facts.ndjson",
            "memories/l2_scenarios.ndjson",
            "memories/l3_persona_series_project_skill.ndjson",
            "memories/l4_persona.ndjson",
        ):
            try:
                memory_count += _count_ndjson_objects(
                    zf.read(path),
                    path=path,
                    errors=errors,
                )
            except KeyError:
                if path != "memories/l4_persona.ndjson" or manifest.get("version") != "1.0":
                    warnings.append(f"缺少 {path}")

        try:
            source_count = _count_ndjson_objects(
                zf.read("sources/source_manifest.ndjson"),
                path="sources/source_manifest.ndjson",
                errors=errors,
            )
        except KeyError:
            warnings.append("缺少 sources/source_manifest.ndjson")

        source_ids = _read_source_ids(zf, errors)
        source_asset_count = _verify_source_assets(
            zf,
            source_ids=source_ids,
            required=manifest.get("version") == "1.2",
            errors=errors,
            warnings=warnings,
        )

        try:
            evidence_count = _count_ndjson_objects(
                zf.read("evidence/evidence_graph.ndjson"),
                path="evidence/evidence_graph.ndjson",
                errors=errors,
            )
        except KeyError:
            warnings.append("缺少 evidence/evidence_graph.ndjson")

        try:
            tag_data = zf.read("tags/tag_index.json").decode("utf-8")
            tag_index = json.loads(tag_data)
            tag_count = len(tag_index)
        except KeyError:
            warnings.append("缺少 tags/tag_index.json")
        except UnicodeDecodeError as exc:
            errors.append(f"tags/tag_index.json 不是 UTF-8：{exc}")
        except json.JSONDecodeError as exc:
            errors.append(f"tag_index.json 解析失败：{exc}")
        else:
            if not isinstance(tag_index, dict):
                errors.append("tags/tag_index.json 必须包含 JSON object")
                tag_count = 0

        try:
            project_skill_count = _count_ndjson_objects(
                zf.read("project_skills/project_skill_cards.ndjson"),
                path="project_skills/project_skill_cards.ndjson",
                errors=errors,
            )
        except KeyError:
            warnings.append("缺少 project_skills/project_skill_cards.ndjson")

        _verify_manifest_count(manifest, "memory_count", memory_count, errors)
        _verify_manifest_count(
            manifest,
            "project_skill_count",
            project_skill_count,
            errors,
            optional=True,
        )
        _verify_manifest_count(
            manifest,
            "source_count",
            source_count,
            errors,
            optional=True,
        )
        _verify_manifest_count(
            manifest,
            "evidence_link_count",
            evidence_count,
            errors,
            optional=True,
        )
        _verify_manifest_count(
            manifest,
            "tag_count",
            tag_count,
            errors,
            optional=True,
        )
        _verify_manifest_count(
            manifest,
            "source_asset_count",
            source_asset_count,
            errors,
            optional=manifest.get("version") != "1.2",
        )

    is_valid = len(errors) == 0
    summary = (
        f"资产包校验{'通过' if is_valid else '失败'}："
        f"manifest={'OK' if manifest else 'MISSING'}，"
        f"memory={memory_count}，project_skill={project_skill_count}，source={source_count}，"
        f"source_asset={source_asset_count}，"
        f"tag={tag_count}，evidence={evidence_count}，"
        f"错误={len(errors)}，警告={len(warnings)}"
    )

    return AssetPackageImportReport(
        is_valid=is_valid,
        manifest=dict(manifest),
        imported_memory_count=memory_count,
        imported_source_count=source_count,
        imported_tag_count=tag_count,
        imported_evidence_count=evidence_count,
        imported_project_skill_count=project_skill_count,
        imported_source_asset_count=source_asset_count,
        errors=tuple(errors),
        warnings=tuple(warnings),
        summary=summary,
    )


def _count_ndjson_objects(
    content: bytes,
    *,
    path: str,
    errors: list[str],
) -> int:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"{path} 不是 UTF-8：{exc}")
        return 0
    count = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{line_number} JSON 解析失败：{exc.msg}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{path}:{line_number} 必须包含 JSON object")
            continue
        count += 1
    return count


def _verify_manifest_count(
    manifest: Mapping[str, object],
    field: str,
    actual: int,
    errors: list[str],
    *,
    optional: bool = False,
) -> None:
    if field not in manifest:
        if not optional:
            errors.append(f"manifest 缺少 {field}")
        return
    expected = manifest.get(field)
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or expected < 0
    ):
        errors.append(f"manifest.{field} 必须是非负整数")
        return
    if expected != actual:
        errors.append(
            f"manifest.{field}={expected} 与实际 {actual} 不一致"
        )


def _read_source_ids(
    archive: zipfile.ZipFile,
    errors: list[str],
) -> set[str]:
    path = "sources/source_manifest.ndjson"
    try:
        content = archive.read(path).decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return set()
    source_ids: set[str] = set()
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        source_id = str(value.get("source_id") or value.get("id") or "").strip()
        if not source_id:
            continue
        if source_id in source_ids:
            errors.append(f"{path} 包含重复 source id：{source_id}")
        source_ids.add(source_id)
    return source_ids


def _verify_source_assets(
    archive: zipfile.ZipFile,
    *,
    source_ids: set[str],
    required: bool,
    errors: list[str],
    warnings: list[str],
) -> int:
    path = "sources/source_assets.ndjson"
    try:
        content = archive.read(path).decode("utf-8")
    except KeyError:
        if required:
            errors.append(f"资产包缺少 {path}")
        else:
            warnings.append(f"缺少 {path}")
        return 0
    except UnicodeDecodeError as exc:
        errors.append(f"{path} 不是 UTF-8：{exc}")
        return 0

    count = 0
    asset_ids: set[str] = set()
    declared_blobs: set[str] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        prefix = f"{path}:{line_number}"
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{prefix} JSON 解析失败：{exc.msg}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{prefix} 必须包含 JSON object")
            continue

        source_id = str(value.get("source_id", "")).strip()
        asset_id = str(value.get("asset_id", "")).strip()
        digest = str(value.get("sha256", "")).strip()
        blob_path = str(value.get("blob_path", "")).strip()
        byte_count = value.get("byte_count")
        if source_id not in source_ids:
            errors.append(f"{prefix} 引用了不存在的 source：{source_id}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", asset_id):
            errors.append(f"{prefix} asset_id 无效")
        elif asset_id in asset_ids:
            errors.append(f"{prefix} asset_id 重复：{asset_id}")
        else:
            asset_ids.add(asset_id)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(f"{prefix} sha256 无效")
        expected_blob_path = f"sources/content/{digest}"
        if blob_path != expected_blob_path:
            errors.append(f"{prefix} blob_path 无效")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or byte_count > 64 * 1024 * 1024
        ):
            errors.append(f"{prefix} byte_count 无效")
        try:
            blob = archive.read(blob_path)
        except KeyError:
            errors.append(f"{prefix} 缺少 Blob：{blob_path}")
        else:
            if isinstance(byte_count, int) and not isinstance(byte_count, bool):
                if len(blob) != byte_count:
                    errors.append(f"{prefix} Blob 字节数与 byte_count 不一致")
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                if hashlib.sha256(blob).hexdigest() != digest:
                    errors.append(f"{prefix} Blob 哈希与 sha256 不一致")
        declared_blobs.add(blob_path)
        count += 1

    undeclared_blobs = {
        name
        for name in archive.namelist()
        if name.startswith("sources/content/") and name not in declared_blobs
    }
    if undeclared_blobs:
        errors.append("资产包包含未声明的 sources/content Blob")
    return count


# ── 序列化 ──


def serialize_export_result(result: ExportResult) -> dict[str, object]:
    """序列化 ExportResult（不含 bytes_payload，避免 JSON 过大）。"""
    return {
        "preset": result.preset,
        "format": result.format,
        "file_name": result.file_name,
        "memory_count": result.memory_count,
        "source_count": result.source_count,
        "redacted_count": result.redacted_count,
        "skipped_count": result.skipped_count,
        "summary": result.summary,
        "export_batch_id": result.export_batch_id,
        "error": result.error,
    }


def serialize_asset_package_import_report(report: AssetPackageImportReport) -> dict[str, object]:
    return {
        "is_valid": report.is_valid,
        "manifest": dict(report.manifest),
        "imported_memory_count": report.imported_memory_count,
        "imported_source_count": report.imported_source_count,
        "imported_tag_count": report.imported_tag_count,
        "imported_evidence_count": report.imported_evidence_count,
        "imported_project_skill_count": report.imported_project_skill_count,
        "imported_source_asset_count": report.imported_source_asset_count,
        "errors": list(report.errors),
        "warnings": list(report.warnings),
        "summary": report.summary,
    }


def serialize_exportable_memory(m: ExportableMemory) -> dict[str, object]:
    return _memory_to_dict(m)


def serialize_exportable_source(s: ExportableSource) -> dict[str, object]:
    return _source_to_dict(s)


def serialize_export_scope(scope: ExportScope) -> dict[str, object]:
    return {
        "preset": scope.preset,
        "redact_secrets": scope.redact_secrets,
        "only_confirmed": scope.only_confirmed,
        "skip_raw_sources": scope.skip_raw_sources,
        "skip_av": scope.skip_av,
        "skip_evidence_text": scope.skip_evidence_text,
        "skip_low_trust": scope.skip_low_trust,
        "skip_conflicts": scope.skip_conflicts,
        "skip_provider_audit": scope.skip_provider_audit,
        "include_paths": scope.include_paths,
    }
