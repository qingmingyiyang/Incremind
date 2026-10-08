"""OutlineApplier 单元测试 —— 验证章节树渲染逻辑。

覆盖：
- OutlineSection / Outline dataclass 行为
- Outline.from_payload 校验（空、非 list、缺字段、dup section_id、非法 kind、非 bool required）
- OutlineApplier.render 按 kind 渲染（prompt/series/summary/key_points/body/uncertain/sources）
- required=False 空内容跳过
- required=True 空内容用 fallback
- 列表项自动补 "- "
- 多 section 顺序渲染
- 隐私：无敏感串
"""

from __future__ import annotations

import pytest

from core.product_core.outline import (
    Outline,
    OutlineApplier,
    OutlineError,
    OutlineRenderInputs,
    OutlineSection,
    SUPPORTED_KINDS,
)


def _inputs(
    *,
    template_label: str = "回答手册",
    title: str = "测试文档",
    series_name: str = "测试系列",
    prompt: str = "生成提示词内容",
    summary: str = "这是摘要。",
    key_points: tuple[str, ...] = ("要点一", "要点二"),
    body: str = "正文段落。",
    sources: tuple[str, ...] = ("- Source: src-1", "- Structure: ref-1"),
    uncertain_notes: tuple[str, ...] = (),
) -> OutlineRenderInputs:
    return OutlineRenderInputs(
        template_label=template_label,
        title=title,
        series_name=series_name,
        prompt=prompt,
        summary=summary,
        key_points=key_points,
        body=body,
        sources=sources,
        uncertain_notes=uncertain_notes,
    )


# ---------------------------------------------------------------------------
# OutlineSection / Outline dataclass
# ---------------------------------------------------------------------------


def test_section_to_payload_round_trip() -> None:
    section = OutlineSection(section_id="s1", title="结论", kind="summary", required=True)
    payload = section.to_payload()
    assert payload == {
        "section_id": "s1",
        "title": "结论",
        "kind": "summary",
        "required": True,
    }


def test_section_required_defaults_to_true() -> None:
    section = OutlineSection(section_id="s1", title="结论", kind="summary")
    assert section.required is True


def test_outline_to_payload_round_trip() -> None:
    outline = Outline.from_payload(
        [
            {"section_id": "s1", "title": "结论", "kind": "summary", "required": True},
            {"section_id": "s2", "title": "来源", "kind": "sources", "required": False},
        ]
    )
    payload = outline.to_payload()
    assert len(payload) == 2
    assert payload[0]["section_id"] == "s1"
    assert payload[1]["required"] is False


# ---------------------------------------------------------------------------
# Outline.from_payload 校验
# ---------------------------------------------------------------------------


def test_from_payload_rejects_non_list() -> None:
    with pytest.raises(OutlineError, match="must be a list"):
        Outline.from_payload({"section_id": "s1"})


def test_from_payload_rejects_string() -> None:
    with pytest.raises(OutlineError, match="must be a list"):
        Outline.from_payload("not a list")


def test_from_payload_rejects_empty_list() -> None:
    with pytest.raises(OutlineError, match="at least one section"):
        Outline.from_payload([])


def test_from_payload_rejects_non_object_section() -> None:
    with pytest.raises(OutlineError, match="must be an object"):
        Outline.from_payload(["not an object"])


def test_from_payload_rejects_missing_section_id() -> None:
    with pytest.raises(OutlineError, match="missing non-empty 'section_id'"):
        Outline.from_payload([{"title": "T", "kind": "summary", "required": True}])


def test_from_payload_rejects_missing_title() -> None:
    with pytest.raises(OutlineError, match="missing non-empty 'title'"):
        Outline.from_payload([{"section_id": "s1", "kind": "summary", "required": True}])


def test_from_payload_rejects_missing_kind() -> None:
    with pytest.raises(OutlineError, match="missing non-empty 'kind'"):
        Outline.from_payload([{"section_id": "s1", "title": "T", "required": True}])


def test_from_payload_rejects_unsupported_kind() -> None:
    with pytest.raises(OutlineError, match="unsupported kind 'unknown'"):
        Outline.from_payload(
            [{"section_id": "s1", "title": "T", "kind": "unknown", "required": True}]
        )


def test_from_payload_rejects_non_bool_required() -> None:
    with pytest.raises(OutlineError, match="'required' must be boolean"):
        Outline.from_payload(
            [{"section_id": "s1", "title": "T", "kind": "summary", "required": "yes"}]
        )


def test_from_payload_rejects_duplicated_section_id() -> None:
    with pytest.raises(OutlineError, match="duplicated"):
        Outline.from_payload(
            [
                {"section_id": "s1", "title": "A", "kind": "summary", "required": True},
                {"section_id": "s1", "title": "B", "kind": "body", "required": True},
            ]
        )


def test_from_payload_strips_whitespace_in_strings() -> None:
    outline = Outline.from_payload(
        [{"section_id": "  s1  ", "title": "  结论  ", "kind": "summary", "required": True}]
    )
    assert outline.sections[0].section_id == "s1"
    assert outline.sections[0].title == "结论"


def test_supported_kinds_contains_all_expected() -> None:
    assert SUPPORTED_KINDS == frozenset(
        {"prompt", "series", "summary", "key_points", "body", "uncertain", "sources"}
    )


# ---------------------------------------------------------------------------
# OutlineApplier.render —— 按 kind 渲染
# ---------------------------------------------------------------------------


def test_render_starts_with_title_heading() -> None:
    outline = Outline.from_payload(
        [{"section_id": "s1", "title": "结论", "kind": "summary", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(template_label="回答手册", title="文档A"))
    assert result.startswith("# 回答手册：文档A\n")


def test_render_prompt_section() -> None:
    outline = Outline.from_payload(
        [{"section_id": "p", "title": "生成提示词", "kind": "prompt", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(prompt="PROMPT-XYZ"))
    assert "## 生成提示词" in result
    assert "PROMPT-XYZ" in result


def test_render_series_section() -> None:
    outline = Outline.from_payload(
        [{"section_id": "s", "title": "系列", "kind": "series", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(series_name="系列A"))
    assert "## 系列" in result
    assert "系列A" in result


def test_render_summary_section() -> None:
    outline = Outline.from_payload(
        [{"section_id": "sum", "title": "结论", "kind": "summary", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(summary="这是结论内容。"))
    assert "## 结论" in result
    assert "这是结论内容。" in result


def test_render_key_points_section_with_items() -> None:
    outline = Outline.from_payload(
        [{"section_id": "kp", "title": "关键点", "kind": "key_points", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(key_points=("要点A", "要点B")))
    assert "## 关键点" in result
    assert "- 要点A" in result
    assert "- 要点B" in result


def test_render_key_points_section_empty_uses_fallback() -> None:
    outline = Outline.from_payload(
        [{"section_id": "kp", "title": "关键点", "kind": "key_points", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(key_points=()))
    assert "- 待补充关键点" in result


def test_render_body_section() -> None:
    outline = Outline.from_payload(
        [{"section_id": "b", "title": "正文", "kind": "body", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(body="正文段落。"))
    assert "## 正文" in result
    assert "正文段落。" in result


def test_render_body_section_empty_uses_fallback() -> None:
    outline = Outline.from_payload(
        [{"section_id": "b", "title": "正文", "kind": "body", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(body=""))
    assert "- 待补充正文" in result


def test_render_uncertain_section_with_default_notes() -> None:
    outline = Outline.from_payload(
        [{"section_id": "u", "title": "待确认", "kind": "uncertain", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(uncertain_notes=()))
    assert "## 待确认" in result
    assert "Memory Candidate" in result


def test_render_uncertain_section_with_custom_notes() -> None:
    outline = Outline.from_payload(
        [{"section_id": "u", "title": "待确认", "kind": "uncertain", "required": True}]
    )
    result = OutlineApplier().render(
        outline, _inputs(uncertain_notes=("- 自定义提示一", "- 自定义提示二"))
    )
    assert "- 自定义提示一" in result
    assert "- 自定义提示二" in result


def test_render_sources_section() -> None:
    outline = Outline.from_payload(
        [{"section_id": "src", "title": "来源", "kind": "sources", "required": True}]
    )
    result = OutlineApplier().render(
        outline, _inputs(sources=("- Source: src-1", "- Structure: ref-1"))
    )
    assert "## 来源" in result
    assert "- Source: src-1" in result
    assert "- Structure: ref-1" in result


def test_render_sources_section_empty_uses_fallback() -> None:
    outline = Outline.from_payload(
        [{"section_id": "src", "title": "来源", "kind": "sources", "required": True}]
    )
    result = OutlineApplier().render(outline, _inputs(sources=()))
    assert "- 暂无来源信息" in result


# ---------------------------------------------------------------------------
# required=False 行为
# ---------------------------------------------------------------------------


def test_render_skips_optional_section_when_empty() -> None:
    outline = Outline.from_payload(
        [
            {"section_id": "s", "title": "结论", "kind": "summary", "required": True},
            {"section_id": "o", "title": "可选", "kind": "body", "required": False},
        ]
    )
    result = OutlineApplier().render(outline, _inputs(summary="有摘要", body=""))
    assert "## 可选" not in result
    assert "## 结论" in result


def test_render_includes_optional_section_when_has_content() -> None:
    outline = Outline.from_payload(
        [
            {"section_id": "s", "title": "结论", "kind": "summary", "required": True},
            {"section_id": "o", "title": "可选", "kind": "body", "required": False},
        ]
    )
    result = OutlineApplier().render(outline, _inputs(summary="有摘要", body="有正文"))
    assert "## 可选" in result
    assert "有正文" in result


def test_render_skips_optional_uncertain_when_no_notes() -> None:
    """uncertain + required=False + 无 notes → 跳过（fallback=None）"""
    outline = Outline.from_payload(
        [
            {"section_id": "s", "title": "结论", "kind": "summary", "required": True},
            {"section_id": "u", "title": "待确认", "kind": "uncertain", "required": False},
        ]
    )
    result = OutlineApplier().render(outline, _inputs(uncertain_notes=()))
    assert "## 待确认" not in result


# ---------------------------------------------------------------------------
# 多 section 顺序与列表项格式
# ---------------------------------------------------------------------------


def test_render_preserves_section_order() -> None:
    outline = Outline.from_payload(
        [
            {"section_id": "s1", "title": "第一", "kind": "summary", "required": True},
            {"section_id": "s2", "title": "第二", "kind": "body", "required": True},
            {"section_id": "s3", "title": "第三", "kind": "sources", "required": True},
        ]
    )
    result = OutlineApplier().render(outline, _inputs())
    s1_pos = result.index("## 第一")
    s2_pos = result.index("## 第二")
    s3_pos = result.index("## 第三")
    assert s1_pos < s2_pos < s3_pos


def test_render_key_points_items_with_existing_dash_not_doubled() -> None:
    """列表项已含 "- " 前缀时不重复添加。"""
    outline = Outline.from_payload(
        [{"section_id": "kp", "title": "关键点", "kind": "key_points", "required": True}]
    )
    result = OutlineApplier().render(
        outline, _inputs(key_points=("- 已有短横线", "无短横线"))
    )
    assert "- 已有短横线" in result
    assert "- - 已有短横线" not in result
    assert "- 无短横线" in result


def test_render_key_points_filters_non_string_and_empty() -> None:
    outline = Outline.from_payload(
        [{"section_id": "kp", "title": "关键点", "kind": "key_points", "required": True}]
    )
    # OutlineRenderInputs.key_points 类型是 tuple[str, ...]，但运行时仍可能含空串
    result = OutlineApplier().render(
        outline, _inputs(key_points=("有效", "", "  "))  # type: ignore[arg-type]
    )
    assert "- 有效" in result
    # 空串被过滤后只剩 1 项有效，无需 fallback
    assert result.count("- ") == 1


# ---------------------------------------------------------------------------
# 隐私
# ---------------------------------------------------------------------------


def test_render_excludes_secrets() -> None:
    """渲染结果不应含敏感凭据串。"""
    outline = Outline.from_payload(
        [
            {"section_id": "s", "title": "结论", "kind": "summary", "required": True},
            {"section_id": "src", "title": "来源", "kind": "sources", "required": True},
        ]
    )
    result = OutlineApplier().render(
        outline,
        _inputs(
            summary="正常摘要",
            sources=("- Source: src-1",),
        ),
    )
    lowered = result.lower()
    assert "sk-" not in lowered
    assert "cookie" not in lowered
    assert "authorization" not in lowered
    assert "password" not in lowered
    assert "token" not in lowered
