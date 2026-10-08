"""Project skill formats ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from core.product_core.outline import Outline


def _serialize_project_skill_for_editor(
    skill: Mapping[str, object],
    *,
    markdown: str = "",
) -> dict[str, object]:
    """为 Developer Studio outline 编辑器序列化 ProjectSkill，保留 outline 字段。"""
    payload: dict[str, object] = {}
    for key, value in skill.items():
        if key in {"markdown", "structured"}:
            continue
        payload[key] = value
    payload["markdown"] = markdown
    outline = skill.get("outline")
    payload["outline"] = list(outline) if isinstance(outline, list) else []
    return payload


_PROJECT_SKILL_IMPORT_MAX_BYTES = 256 * 1024


_PROJECT_SKILL_IMPORT_MUTABLE_FIELDS = {
    "name", "purpose", "required_context", "output_rules", "style_preferences",
    "update_rules", "source_refs", "evidence_refs", "outline",
}


def _project_skill_import_draft(
    project_id: str,
    body: Mapping[str, object],
    *,
    current: Mapping[str, object] | None,
) -> dict[str, object]:
    format_name = body.get("format")
    content = body.get("content")
    if format_name not in {"markdown", "json"}:
        raise ValueError("format must be markdown or json")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    if len(content.encode("utf-8")) > _PROJECT_SKILL_IMPORT_MAX_BYTES:
        raise ValueError("content exceeds 256 KiB")
    structured = dict(current) if current is not None else _project_skill_default_structured(project_id)
    for key in ("id", "project_id", "markdown_uri", "json_uri", "markdown_revision", "json_revision", "revision", "created_at", "updated_at", "decision_log"):
        structured.pop(key, None)
    if current is not None and isinstance(current.get("id"), str):
        structured["id"] = current["id"]
    if format_name == "json":
        parsed = json.loads(content)
        if not isinstance(parsed, Mapping):
            raise ValueError("JSON root must be an object")
        for key in _PROJECT_SKILL_IMPORT_MUTABLE_FIELDS:
            if key in parsed:
                structured[key] = parsed[key]
        markdown_value = parsed.get("markdown")
        markdown = markdown_value if isinstance(markdown_value, str) and markdown_value.strip() else _project_skill_markdown_fallback(structured)
    else:
        markdown = content.strip() + "\n"
        heading = next((line[2:].strip() for line in content.splitlines() if line.startswith("# ") and line[2:].strip()), None)
        paragraph = next((line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")), None)
        if heading:
            structured["name"] = heading
        if paragraph:
            structured["purpose"] = paragraph
    name = structured.get("name")
    purpose = structured.get("purpose")
    if not isinstance(name, str) or not name.strip() or not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("import requires non-empty name and purpose")
    structured["name"] = name.strip()
    structured["purpose"] = purpose.strip()
    outline = structured.get("outline")
    if outline in (None, []):
        structured.pop("outline", None)
    else:
        structured["outline"] = Outline.from_payload(outline).to_payload()
    structured["status"] = "active"
    structured["trust_status"] = "user_confirmed"
    structured["conflict"] = {"status": "none", "conflict_refs": [], "resolution": None}
    update_rules = structured.get("update_rules")
    if not isinstance(update_rules, Mapping):
        raise ValueError("update_rules must be an object")
    structured["update_rules"] = {**dict(update_rules), "user_edit_policy": "user_wins"}
    for key in ("required_context", "output_rules", "source_refs", "evidence_refs"):
        if not isinstance(structured.get(key), list):
            raise ValueError(f"{key} must be a list")
    if not isinstance(structured.get("style_preferences"), Mapping):
        raise ValueError("style_preferences must be an object")
    return {"format": format_name, "structured": structured, "markdown": markdown}


def _project_skill_default_structured(project_id: str) -> dict[str, object]:
    return {
        "project_id": project_id,
        "name": f"{project_id} 项目规则",
        "purpose": "保存当前项目的长期工作规则和回答结构。",
        "required_context": [], "output_rules": [], "style_preferences": {},
        "update_rules": {"patch_strategy": "patch_existing_first", "user_edit_policy": "user_wins", "allowed_auto_updates": []},
        "source_refs": [], "evidence_refs": [],
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "status": "active", "trust_status": "user_confirmed",
    }


def _project_skill_ai_safe_structured(
    value: Mapping[str, object],
    *,
    allowed_source_refs: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    """Reduce model output to contract-valid, non-authoritative Project Skill fields."""
    normalized = dict(value)
    style = normalized.get("style_preferences")
    style = dict(style) if isinstance(style, Mapping) else {}
    voice = style.get("voice")
    formats = style.get("format_defaults")
    normalized["style_preferences"] = {
        "voice": voice.strip() if isinstance(voice, str) and voice.strip() else "直接、具体、可执行",
        "format_defaults": (
            list(dict.fromkeys(item.strip() for item in formats if isinstance(item, str) and item.strip()))
            if isinstance(formats, list) else ["Markdown", "分节标题", "来源引用"]
        ),
    }
    updates = normalized.get("update_rules")
    updates = dict(updates) if isinstance(updates, Mapping) else {}
    patch_strategy = updates.get("patch_strategy")
    if patch_strategy not in {"patch_existing_first", "add_section_allowed", "rewrite_requires_confirmation"}:
        patch_strategy = "patch_existing_first"
    allowed_values = {"append_low_risk_context", "refresh_stale_refs", "update_style_examples"}
    allowed = updates.get("allowed_auto_updates")
    normalized["update_rules"] = {
        "patch_strategy": patch_strategy,
        "user_edit_policy": "user_wins",
        "allowed_auto_updates": (
            list(dict.fromkeys(item for item in allowed if item in allowed_values))
            if isinstance(allowed, list) else []
        ),
    }
    rules = normalized.get("output_rules")
    if not isinstance(rules, list):
        raise ValueError("output_rules must be a list")
    safe_rules = []
    allowed = {
        (str(ref.get("source_id") or ""), str(ref.get("locator") or "")): {
            "source_id": str(ref.get("source_id") or ""),
            "locator": str(ref.get("locator") or ""),
        }
        for ref in allowed_source_refs
        if ref.get("source_id")
    }
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise ValueError("output_rules must contain objects")
        text = rule.get("rule")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("output rule text is required")
        provided_refs = rule.get("source_refs")
        safe_refs = []
        if isinstance(provided_refs, list):
            for ref in provided_refs:
                if not isinstance(ref, Mapping):
                    continue
                key = (str(ref.get("source_id") or ""), str(ref.get("locator") or ""))
                if key in allowed and allowed[key] not in safe_refs:
                    safe_refs.append(allowed[key])
        if not safe_refs:
            raise ValueError("output rule requires a supplied project evidence ref")
        safe_rules.append({
            "rule_id": f"rule-ai-{index + 1:03d}",
            "origin": "ai",
            "rule": text.strip(),
            "priority": rule.get("priority") if rule.get("priority") in {"must", "should"} else "should",
            "source_refs": safe_refs,
            "locked_by_user": False,
        })
    normalized["output_rules"] = safe_rules
    normalized["source_refs"] = [dict(ref) for ref in allowed_source_refs]
    normalized["evidence_refs"] = [dict(ref) for ref in allowed_source_refs]
    return normalized


def _project_skill_markdown_fallback(skill: Mapping[str, object]) -> str:
    """没有存储 markdown 时从 structured 字段拼装的最小回退，避免 save 失败。"""
    name = str(skill.get("name") or skill.get("id") or "Project Skill")
    purpose = str(skill.get("purpose") or "")
    lines = [f"# {name}"]
    if purpose:
        lines.append("")
        lines.append(purpose)
    return "\n".join(lines)
