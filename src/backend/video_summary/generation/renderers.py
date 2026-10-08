from __future__ import annotations

from typing import Any

from .prompts import format_timestamp


def render_markdown(summary_data: dict[str, Any]) -> str:
    lines: list[str] = [f"# {summary_data['title']}", ""]
    if summary_data.get("content_type"):
        lines.extend([f"> 内容类型：{summary_data['content_type']}", ""])
    lines.append("## 30 秒摘要")
    lines.append(summary_data.get("thirty_second_summary") or summary_data.get("one_sentence_summary", ""))
    lines.append("")
    lines.append("## 一句话总结")
    lines.append(summary_data.get("one_sentence_summary", ""))
    lines.append("")
    lines.append("## 核心问题")
    lines.append(summary_data["core_problem"])
    lines.append("")
    lines.append("## 章节摘要")
    lines.append("")

    for chapter in summary_data.get("chapters", []):
        start = format_timestamp(chapter["start_seconds"])
        end = format_timestamp(chapter["end_seconds"])
        lines.append(f"### {chapter['title']} ({start} - {end})")
        lines.append(f"<a id=\"{chapter['id']}\"></a>")
        lines.append(chapter["summary"])
        lines.append("")
        if chapter["key_points"]:
            for point in chapter["key_points"]:
                lines.append(f"- {point}")
            lines.append("")

        if chapter.get("evidence_ids"):
            lines.append("证据：" + "、".join(chapter["evidence_ids"]))
            lines.append("")

    lines.append("## 关键结论")
    for point in summary_data.get("key_takeaways", []):
        lines.append(f"- {point}")
    lines.append("")

    _append_list_section(lines, "详细结构化笔记", summary_data.get("detailed_notes", []))
    _append_knowledge_section(lines, "人物", summary_data.get("people", []))
    _append_knowledge_section(lines, "术语", summary_data.get("terms", []))
    _append_knowledge_section(lines, "案例", summary_data.get("examples", []))
    _append_knowledge_section(lines, "数据", summary_data.get("data_points", []))
    _append_knowledge_section(lines, "观点", summary_data.get("viewpoints", []))

    actions = summary_data.get("action_items", [])
    if actions:
        lines.extend(["## 可执行事项", ""])
        for item in actions:
            evidence = _evidence_suffix(item.get("evidence_ids", []))
            lines.append(f"- **{item.get('action', '')}**：{item.get('rationale', '')}{evidence}")
        lines.append("")

    relations = summary_data.get("relations", [])
    if relations:
        lines.extend(["## 信息关联", ""])
        for item in relations:
            lines.append(
                f"- {item.get('source', '')} → {item.get('relation', '')} → {item.get('target', '')}"
                f"{_evidence_suffix(item.get('evidence_ids', []))}"
            )
        lines.append("")

    evidence = summary_data.get("evidence", [])
    if evidence:
        lines.extend(["## 原文证据", ""])
        for item in evidence:
            start = format_timestamp(float(item.get("start_seconds", 0.0)))
            end = format_timestamp(float(item.get("end_seconds", 0.0)))
            lines.append(f"### {item.get('id', '')} · {start}–{end}")
            lines.append(str(item.get("statement", "")))
            quote = str(item.get("quote", "")).strip()
            if quote:
                lines.append(f"> {quote}")
            lines.append("")

    visual = summary_data.get("visual_attention") or {}
    if visual:
        lines.extend(["## 画面阅读提示", "", str(visual.get("reason", "")), ""])

    _append_list_section(lines, "仍待回答的问题", summary_data.get("open_questions", []))
    return "\n".join(lines).strip() + "\n"


def _append_list_section(lines: list[str], title: str, items: list[Any]) -> None:
    if not items:
        return
    lines.extend([f"## {title}", ""])
    lines.extend(f"- {item}" for item in items)
    lines.append("")


def _append_knowledge_section(lines: list[str], title: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    lines.extend([f"## {title}", ""])
    for item in items:
        lines.append(
            f"- **{item.get('name', '')}**：{item.get('description', '')}"
            f"{_evidence_suffix(item.get('evidence_ids', []))}"
        )
    lines.append("")


def _evidence_suffix(evidence_ids: list[str]) -> str:
    return f" 证据：{'、'.join(evidence_ids)}" if evidence_ids else ""
